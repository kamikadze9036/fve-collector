"""
InfluxDB (Home Assistant) collector — čte měsíční přírůstky kumulativních
čítačů (Total PV Generation, Meter Total Energy import/export), které HA
zapisuje lokálně z měniče/elektroměru (bez závislosti na SEMS cloudu).

Přírůstek = spread() (max − min) čítače přes celý kalendářní měsíc
v Europe/Prague. Záměrně bez aggregateWindow: to dělí okna podle UTC
a na hranici měsíce vznikal 1–2hodinový střípek, který se zahazoval.
"""

import logging
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from influxdb_client import InfluxDBClient

logger = logging.getLogger(__name__)

PRAGUE = ZoneInfo("Europe/Prague")

FIELDS = {
    "Total PV Generation":            "pvGenerationKwh",
    "Meter Total Energy (export)":    "gridExportKwh",
    "Meter Total Energy (import)":    "pvPurchaseKwh",
}


def _month_bounds_utc(year: int, month: int) -> tuple[str, str]:
    """
    Vrátí (start, stop) kalendářního měsíce v Europe/Prague jako RFC3339 UTC.
    `stop` je začátek následujícího měsíce (Flux range má stop exkluzivní).
    """
    start_local = datetime(year, month, 1, tzinfo=PRAGUE)
    if month == 12:
        stop_local = datetime(year + 1, 1, 1, tzinfo=PRAGUE)
    else:
        stop_local = datetime(year, month + 1, 1, tzinfo=PRAGUE)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (
        start_local.astimezone(timezone.utc).strftime(fmt),
        stop_local.astimezone(timezone.utc).strftime(fmt),
    )


def _flux(bucket: str, start: str, stop: str) -> str:
    friendly_names = " or ".join(f'r["friendly_name"] == "{name}"' for name in FIELDS)
    return f'''
from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r["_measurement"] == "kWh")
  |> filter(fn: (r) => r["_field"] == "value")
  |> filter(fn: (r) => {friendly_names})
  |> spread()
'''


def fetch_month(config: dict, target: date) -> dict:
    """Vrátí {"pvGenerationKwh", "gridExportKwh", "pvPurchaseKwh"} za měsíc `target`.
    Chybějící čítač vrací jako None (fve-portal None/null ignoruje)."""
    start, stop = _month_bounds_utc(target.year, target.month)
    cfg = config["influxdb"]

    logger.info("InfluxDB: čtu měsíční přírůstky za %s (%s – %s)", target.strftime("%Y-%m"), start, stop)

    client = InfluxDBClient(url=cfg["url"], token=cfg["token"], org=cfg["org"])
    try:
        tables = client.query_api().query(_flux(cfg["bucket"], start, stop), org=cfg["org"])
    finally:
        client.close()

    result = {v: None for v in FIELDS.values()}
    for table in tables:
        for record in table.records:
            field = FIELDS.get(record.values.get("friendly_name"))
            value = record.get_value()
            if field is None or value is None:
                continue
            if result[field] is not None:
                logger.warning("InfluxDB: %s má víc sérií (více entit se stejným friendly_name?), beru poslední", field)
            result[field] = round(float(value), 2)

    missing = [k for k, v in result.items() if v is None]
    if missing:
        logger.warning("InfluxDB: chybí hodnoty pro %s — zkontroluj friendly_name v HA", missing)

    return result
