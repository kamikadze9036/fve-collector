"""
InfluxDB (Home Assistant) collector — čte měsíční přírůstky kumulativních
čítačů (Total PV Generation, Meter Total Energy import/export), které HA
zapisuje lokálně z měniče/elektroměru (bez závislosti na SEMS cloudu).

Ekvivalent Grafana Flux dotazu "Statistika FVE", omezený na jeden konkrétní
kalendářní měsíc.
"""

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo
from calendar import monthrange

from influxdb_client import InfluxDBClient

logger = logging.getLogger(__name__)

PRAGUE = ZoneInfo("Europe/Prague")

FIELDS = {
    "Total PV Generation":            "pvGenerationKwh",
    "Meter Total Energy (export)":    "gridExportKwh",
    "Meter Total Energy (import)":    "pvPurchaseKwh",
}


def _month_bounds_utc(year: int, month: int) -> tuple[str, str]:
    """Vrátí (start, stop) daného měsíce v Europe/Prague, převedené na RFC3339 UTC."""
    start_local = datetime(year, month, 1, tzinfo=PRAGUE)
    last_day = monthrange(year, month)[1]
    stop_local = datetime(year, month, last_day, 23, 59, 59, tzinfo=PRAGUE)
    return (
        start_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
        stop_local.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _flux(bucket: str, start: str, stop: str) -> str:
    friendly_names = " or ".join(f'r["friendly_name"] == "{name}"' for name in FIELDS)
    return f'''
from(bucket: "{bucket}")
  |> range(start: {start}, stop: {stop})
  |> filter(fn: (r) => r["_measurement"] == "kWh")
  |> filter(fn: (r) => r["_field"] == "value")
  |> filter(fn: (r) => {friendly_names})
  |> aggregateWindow(every: 1mo, fn: spread, createEmpty: false)
  |> pivot(rowKey:["_time"], columnKey: ["friendly_name"], valueColumn: "_value")
'''


def fetch_month(config: dict, target: date) -> dict:
    """Vrátí {"pvGenerationKwh", "gridExportKwh", "pvPurchaseKwh"} za měsíc `target`."""
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
            for influx_name, field in FIELDS.items():
                value = record.values.get(influx_name)
                if value is not None:
                    result[field] = round(float(value), 2)

    missing = [k for k, v in result.items() if v is None]
    if missing:
        logger.warning("InfluxDB: chybí hodnoty pro %s — zkontroluj friendly_name v HA", missing)

    return result
