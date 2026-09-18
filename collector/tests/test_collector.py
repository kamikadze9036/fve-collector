"""Spuštění: cd collector && python -m unittest discover tests"""

import unittest
from datetime import date
from unittest import mock

import deltagreen
import influx_ha
import main


CONSUMPTION_HTML = """
<table>
  <thead><tr>
    <th>Měsíc</th><th>Spotřeba (kWh)</th><th>Cena (Kč/kWh)</th>
    <th>Platba za silovou elektřinu (Kč)</th><th>Regulované platby a ostatní poplatky (Kč)</th>
    <th>Vyrovnávání sítě (Kč)</th><th>Celková platba (Kč)</th>
  </tr></thead>
  <tbody>
    <tr><td>Leden</td><td>1 234,5</td><td>3,21</td><td>3 962,80</td><td>2 100</td><td>−150,25</td><td>5 912,55</td></tr>
    <tr><td>Únor</td><td>900</td><td>3,10</td><td>-</td><td>-</td><td></td><td>-</td></tr>
    <tr><td>Celkem</td><td>2 134,5</td><td></td><td>3 962,80</td><td>2 100</td><td>−150,25</td><td>5 912,55</td></tr>
  </tbody>
</table>
"""

NO_HEADER_HTML = """
<table><tbody>
  <tr><td>Březen</td><td>10</td><td>1</td><td>20</td><td>30</td><td>-5</td><td>45</td></tr>
</tbody></table>
"""


class ParseNumberTest(unittest.TestCase):
    def test_czech_formats(self):
        cases = {
            "1 234,56 Kč": 1234.56,
            "1\xa0234,5": 1234.5,
            "12": 12.0,
            "-12,5": -12.5,
            "−12,5": -12.5,       # unicode minus
            "1.234": 1234.0,      # tečka jako oddělovač tisíců
            "1.234.567,89": 1234567.89,
            "12.5": 12.5,         # tečka jako desetinná, když nejde o tisíce
            "0": 0.0,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(deltagreen._parse_number(text), expected)

    def test_empty_values(self):
        for text in (None, "", "-", "—", "–", "N/A"):
            with self.subTest(text=text):
                self.assertIsNone(deltagreen._parse_number(text))


class ParseTableTest(unittest.TestCase):
    def test_headers_and_rows(self):
        headers, rows = deltagreen._parse_monthly_table(CONSUMPTION_HTML)
        self.assertEqual(len(headers), 6)
        self.assertIn("spotreba (kwh)", headers[0])
        self.assertEqual(set(rows), {"Leden", "Únor"})  # "Celkem" se přeskočí
        self.assertEqual(rows["Leden"], [1234.5, 3.21, 3962.8, 2100.0, -150.25, 5912.55])
        self.assertEqual(rows["Únor"], [900.0, 3.1, None, None, None, None])

    def test_extract_by_header(self):
        headers, rows = deltagreen._parse_monthly_table(CONSUMPTION_HTML)
        values = deltagreen._extract(headers, rows["Leden"], deltagreen.CONSUMPTION_COLUMNS)
        self.assertEqual(values, {
            "spotreba": 1234.5, "platba_silova": 3962.8, "regulovane": 2100.0, "vyrovnani": -150.25,
        })

    def test_extract_by_header_ignores_column_order(self):
        headers = ["vyrovnavani site", "spotreba (kwh)", "regulovane platby", "platba za silovou elektrinu"]
        values = deltagreen._extract(headers, [-1.0, 2.0, 3.0, 4.0], deltagreen.CONSUMPTION_COLUMNS)
        self.assertEqual(values, {"spotreba": 2.0, "platba_silova": 4.0, "regulovane": 3.0, "vyrovnani": -1.0})

    def test_missing_header_column_raises(self):
        headers = ["spotreba", "cena"]
        with self.assertRaises(RuntimeError):
            deltagreen._extract(headers, [1.0, 2.0], deltagreen.CONSUMPTION_COLUMNS)

    def test_no_header_falls_back_to_position(self):
        headers, rows = deltagreen._parse_monthly_table(NO_HEADER_HTML)
        self.assertEqual(headers, [])
        values = deltagreen._extract(headers, rows["Březen"], deltagreen.CONSUMPTION_COLUMNS)
        self.assertEqual(values, {"spotreba": 10.0, "platba_silova": 20.0, "regulovane": 30.0, "vyrovnani": -5.0})

    def test_required_value_missing_raises(self):
        with self.assertRaises(RuntimeError):
            deltagreen._required({"spotreba": None}, "spotreba", "consumption", "Únor")

    def test_no_table_raises(self):
        with self.assertRaises(RuntimeError):
            deltagreen._parse_monthly_table("<html><body>nic</body></html>")


class InfluxBoundsTest(unittest.TestCase):
    def test_summer_time(self):
        self.assertEqual(influx_ha._month_bounds_utc(2026, 6), ("2026-05-31T22:00:00Z", "2026-06-30T22:00:00Z"))

    def test_winter_time_and_year_rollover(self):
        self.assertEqual(influx_ha._month_bounds_utc(2026, 12), ("2026-11-30T23:00:00Z", "2026-12-31T23:00:00Z"))

    def test_dst_transition_month(self):
        # březen začíná v CET (+1), končí v CEST (+2)
        self.assertEqual(influx_ha._month_bounds_utc(2026, 3), ("2026-02-28T23:00:00Z", "2026-03-31T22:00:00Z"))

    def test_flux_uses_spread_without_window(self):
        flux = influx_ha._flux("homeassistant", "2026-05-31T22:00:00Z", "2026-06-30T22:00:00Z")
        self.assertIn("|> spread()", flux)
        self.assertNotIn("aggregateWindow", flux)


class MonthMathTest(unittest.TestCase):
    def test_previous_month(self):
        self.assertEqual(main._previous_month(date(2026, 1, 15)), date(2025, 12, 1))
        self.assertEqual(main._previous_month(date(2026, 9, 3)), date(2026, 8, 1))

    def test_months_between(self):
        self.assertEqual(
            main._months_between(date(2025, 11, 10), date(2026, 2, 1)),
            [date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1)],
        )
        self.assertEqual(main._months_between(date(2026, 3, 1), date(2026, 2, 1)), [])

    def test_missing_months_skips_existing(self):
        config = {"backfill_start": "2026-05-01"}
        existing = {"2026-05-01", "2026-07-01"}
        self.assertEqual(
            main._missing_months(config, date(2026, 9, 18), existing),
            [date(2026, 6, 1), date(2026, 8, 1)],
        )


class CollectMonthTest(unittest.TestCase):
    def test_payload_omits_none_and_posts(self):
        config = {"fve_portal": {"api_url": "http://portal/api/data"}}
        with mock.patch.object(main.influx_ha, "fetch_month", return_value={"pvGenerationKwh": 100.0, "gridExportKwh": None, "pvPurchaseKwh": 5.0}), \
             mock.patch.object(main.deltagreen, "fetch_month", return_value={"gridImportKwh": 50.0, "pndExportKwh": 40.0, "purchaseCostCzk": 300.0, "flexibilityRevenueCzk": None, "saleRevenueCzk": 80.0}), \
             mock.patch.object(main.requests, "post") as post:
            post.return_value = mock.Mock(status_code=201, text="")
            main.collect_month(config, date(2026, 8, 1))
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload, {
            "period": "2026-08-01", "pvGenerationKwh": 100.0, "pvPurchaseKwh": 5.0,
            "gridImportKwh": 50.0, "pndExportKwh": 40.0, "purchaseCostCzk": 300.0, "saleRevenueCzk": 80.0,
        })
        self.assertNotIn("gridExportKwh", payload)
        self.assertNotIn("flexibilityRevenueCzk", payload)

    def test_non_201_raises(self):
        config = {"fve_portal": {"api_url": "http://portal/api/data"}}
        with mock.patch.object(main.influx_ha, "fetch_month", return_value={}), \
             mock.patch.object(main.deltagreen, "fetch_month", return_value={"gridImportKwh": 1.0}), \
             mock.patch.object(main.requests, "post", return_value=mock.Mock(status_code=500, text="boom")):
            with self.assertRaises(RuntimeError):
                main.collect_month(config, date(2026, 8, 1))


if __name__ == "__main__":
    unittest.main()
