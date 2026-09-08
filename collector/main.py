"""
FVE Collector — hlavní vstupní bod.

Jednou měsíčně (viz `scheduler` v config.yml) sesbírá data za PŘEDCHOZÍ
kalendářní měsíc ze dvou zdrojů:
  - InfluxDB (Home Assistant)  → výroba FVE, přetok/nákup na měniči
  - DeltaGreen (Selenium)      → odběr/dodávka do sítě, náklady na nákup,
                                  tržby z prodeje, vyrovnávání sítě

a výsledek POSTne na existující `/api/data` endpoint aplikace fve-portal.

Spuštění:
    python main.py            # spustí scheduler (běží trvale)
    python main.py backfill   # jednorázově dobere historii od `backfill_start` po minulý měsíc
    python main.py 2026-03    # jednorázově zpracuje jeden konkrétní měsíc
"""

import logging
import sys
from datetime import date

import requests
import yaml
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

import deltagreen
import influx_ha

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")


def load_config(path: str = "/app/config.yml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _previous_month(today: date) -> date:
    first_of_this_month = today.replace(day=1)
    last_of_prev_month = first_of_this_month - date.resolution
    return last_of_prev_month.replace(day=1)


def _months_between(start: date, end_inclusive: date) -> list[date]:
    months = []
    cursor = start.replace(day=1)
    while cursor <= end_inclusive:
        months.append(cursor)
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)
    return months


def collect_month(config: dict, target: date) -> None:
    period = target.strftime("%Y-%m-01")
    logger.info("=== Sběr dat za měsíc %s ===", target.strftime("%Y-%m"))

    influx_data = influx_ha.fetch_month(config, target)
    deltagreen_data = deltagreen.fetch_month(config, target)

    payload = {
        "period": period,
        "meterNtKwh": None,
        "meterVtKwh": None,
        "pndExportKwh": deltagreen_data["pndExportKwh"],
        "gridImportKwh": deltagreen_data["gridImportKwh"],
        "pvGenerationKwh": influx_data["pvGenerationKwh"],
        "gridExportKwh": influx_data["gridExportKwh"],
        "pvPurchaseKwh": influx_data["pvPurchaseKwh"],
        "pvSelfUseReportedKwh": None,
        "saleRevenueCzk": deltagreen_data["saleRevenueCzk"],
        "purchaseCostCzk": deltagreen_data["purchaseCostCzk"],
        "flexibilityRevenueCzk": deltagreen_data["flexibilityRevenueCzk"],
    }

    logger.info("Payload za %s: %s", period, payload)

    response = requests.post(config["fve_portal"]["api_url"], json=payload, timeout=30)
    if response.status_code == 201:
        logger.info("Uloženo do fve-portal (%s)", period)
    elif response.status_code == 400 and "existuje" in response.text:
        logger.warning("Záznam za %s už ve fve-portal existuje, přeskakuji zápis", period)
    else:
        response.raise_for_status()

    logger.info("=== Hotovo: %s ===", target.strftime("%Y-%m"))


def run_scheduler(config: dict) -> None:
    scheduler = BlockingScheduler(timezone=config["scheduler"]["timezone"])
    scheduler.add_job(
        func=lambda: collect_month(config, _previous_month(date.today())),
        trigger=CronTrigger(
            day=config["scheduler"]["day_of_month"],
            hour=config["scheduler"]["hour"],
            minute=0,
            timezone=config["scheduler"]["timezone"],
        ),
        id="monthly_collect",
        name="Měsíční sběr dat FVE",
        replace_existing=True,
    )
    logger.info("Scheduler spuštěn — sběr každý měsíc %s. v %s:00",
                config["scheduler"]["day_of_month"], config["scheduler"]["hour"])
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler zastaven.")


def run_backfill(config: dict) -> None:
    start = date.fromisoformat(config["backfill_start"])
    end = _previous_month(date.today())
    for month in _months_between(start, end):
        try:
            collect_month(config, month)
        except Exception:
            logger.exception("Backfill selhal pro měsíc %s, pokračuji dál", month.strftime("%Y-%m"))


def main() -> None:
    config = load_config()

    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        run_backfill(config)
    elif len(sys.argv) > 1:
        target = date.fromisoformat(f"{sys.argv[1]}-01")
        collect_month(config, target)
    else:
        run_scheduler(config)


if __name__ == "__main__":
    main()
