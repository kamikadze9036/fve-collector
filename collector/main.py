"""
FVE Collector — hlavní vstupní bod.

Jednou měsíčně (viz `scheduler` v config.yml) dohledá, které měsíce od
`backfill_start` do PŘEDCHOZÍHO kalendářního měsíce ve fve-portal chybí,
a pro každý z nich sesbírá data ze dvou zdrojů:
  - InfluxDB (Home Assistant)  → výroba FVE, přetok/nákup na měniči
  - DeltaGreen (Selenium)      → odběr/dodávka do sítě, náklady na nákup,
                                  tržby z prodeje, vyrovnávání sítě

a výsledek POSTne na `/api/data` endpoint aplikace fve-portal. Totéž
dohledání proběhne i při startu kontejneru, takže zmeškaný běh (výpadek,
chyba DeltaGreen) se dožene při dalším startu nebo dalším měsíci.

Spuštění:
    python main.py            # dožene chybějící měsíce a spustí scheduler (běží trvale)
    python main.py backfill   # jen dožene chybějící měsíce a skončí
    python main.py 2026-03    # vynuceně zpracuje jeden měsíc (přepíše existující hodnoty)
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

HTTP_TIMEOUT = 60


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


def _existing_periods(config: dict) -> set[str]:
    """Období (YYYY-MM-01), za která už fve-portal má záznam v electricity_readings."""
    response = requests.get(config["fve_portal"]["api_url"], timeout=HTTP_TIMEOUT)
    response.raise_for_status()
    rows = response.json().get("electricity", [])
    return {row["period"] for row in rows if isinstance(row.get("period"), str)}


def _missing_months(config: dict, today: date, existing: set[str]) -> list[date]:
    start = date.fromisoformat(config["backfill_start"])
    end = _previous_month(today)
    return [m for m in _months_between(start, end) if m.strftime("%Y-%m-01") not in existing]


def collect_month(config: dict, target: date) -> None:
    period = target.strftime("%Y-%m-01")
    logger.info("=== Sběr dat za měsíc %s ===", target.strftime("%Y-%m"))

    influx_data = influx_ha.fetch_month(config, target)
    deltagreen_data = deltagreen.fetch_month(config, target)

    # None se neposílá: fve-portal chybějící pole nikdy nepřepisuje, takže
    # ručně zadané hodnoty (stav elektroměru apod.) zůstanou zachované.
    payload = {"period": period}
    payload.update({k: v for k, v in {**influx_data, **deltagreen_data}.items() if v is not None})

    logger.info("Payload za %s: %s", period, payload)

    response = requests.post(config["fve_portal"]["api_url"], json=payload, timeout=HTTP_TIMEOUT)
    if response.status_code != 201:
        raise RuntimeError(f"fve-portal odpověděl {response.status_code}: {response.text[:300]}")

    logger.info("=== Hotovo: %s ===", target.strftime("%Y-%m"))


def collect_missing(config: dict) -> None:
    """Dožene všechny měsíce od backfill_start po minulý měsíc, které ve fve-portal chybí."""
    today = date.today()
    months = _missing_months(config, today, _existing_periods(config))
    if not months:
        logger.info("Všechny měsíce do %s už jsou ve fve-portal, nic k doplnění", _previous_month(today).strftime("%Y-%m"))
        return

    logger.info("Chybí %d měsíc(ů): %s", len(months), ", ".join(m.strftime("%Y-%m") for m in months))
    failed = []
    for month in months:
        try:
            collect_month(config, month)
        except Exception:
            logger.exception("Sběr selhal pro měsíc %s, pokračuji dál", month.strftime("%Y-%m"))
            failed.append(month)
    if failed:
        logger.error("Nepodařilo se doplnit: %s — zkusím znovu při příštím běhu",
                     ", ".join(m.strftime("%Y-%m") for m in failed))


def _safe_collect_missing(config: dict) -> None:
    try:
        collect_missing(config)
    except Exception:
        logger.exception("Dohledání chybějících měsíců selhalo (fve-portal nedostupný?)")


def run_scheduler(config: dict) -> None:
    _safe_collect_missing(config)

    scheduler = BlockingScheduler(timezone=config["scheduler"]["timezone"])
    scheduler.add_job(
        func=lambda: _safe_collect_missing(config),
        trigger=CronTrigger(
            day=config["scheduler"]["day_of_month"],
            hour=config["scheduler"]["hour"],
            minute=0,
            timezone=config["scheduler"]["timezone"],
        ),
        id="monthly_collect",
        name="Měsíční sběr dat FVE",
        replace_existing=True,
        coalesce=True,
        misfire_grace_time=6 * 3600,
    )
    logger.info("Scheduler spuštěn — sběr každý měsíc %s. v %s:00",
                config["scheduler"]["day_of_month"], config["scheduler"]["hour"])
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler zastaven.")


def main() -> None:
    config = load_config()

    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        collect_missing(config)
    elif len(sys.argv) > 1:
        target = date.fromisoformat(f"{sys.argv[1]}-01")
        collect_month(config, target)
    else:
        run_scheduler(config)


if __name__ == "__main__":
    main()
