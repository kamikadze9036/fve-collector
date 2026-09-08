# FVE Collector — návod k nasazení

Sesbírá jednou měsíčně data za předchozí kalendářní měsíc a zapíše je do
`electricity_readings` tabulky aplikace **fve-portal** (`fve-portal/`, viz
`fve-portal/DOCKER.md`) přes její `/api/data` endpoint.

## Struktura

```
fve-collector/
├── docker-compose.yml
├── collector/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── config.yml          ← ⚠️  VYPLŇ PŘED SPUŠTĚNÍM
│   ├── main.py             ← scheduler + orchestrace (vstupní bod)
│   ├── deltagreen.py       ← DeltaGreen — odběr/dodávka, náklady na nákup, tržby z prodeje
│   └── influx_ha.py        ← InfluxDB (Home Assistant) — výroba FVE, přetok/nákup
└── fve-portal/             ← samostatná Next.js dashboard aplikace (vlastní nasazení)
```

## Zdroje dat

| Pole v `electricity_readings` | Zdroj |
|---|---|
| `pvGenerationKwh`, `gridExportKwh`, `pvPurchaseKwh` | InfluxDB (bucket `homeassistant`, HA čte měnič lokálně) |
| `gridImportKwh` | DeltaGreen `/consumption` → Spotřeba (kWh) |
| `pndExportKwh` | DeltaGreen `/production` → Výroba (kWh) |
| `saleRevenueCzk` | DeltaGreen `/production` → Platba za silovou elektřinu |
| `purchaseCostCzk`, `flexibilityRevenueCzk` | DeltaGreen `/consumption` |

`pvSelfUseReportedKwh` se nezapisuje — fve-portal dashboard si vlastní využití dopočítá sám
z `pvGenerationKwh - gridExportKwh`.

`meterNtKwh`/`meterVtKwh` (fyzický stav elektroměru po tarifech VT/NT) se automaticky
nesbírají — DeltaGreen fakturuje vlastní proměnlivou cenou za jednotku, ne klasickým
VT/NT tarifem, takže tenhle údaj už není pro výpočet nákladů potřeba. Pokud bys ho
přesto chtěl sledovat, jde zadat ručně přes formulář v dashboardu.

## Krok 1 — Vyplň config.yml

`config.yml` obsahuje reálná hesla/tokeny, takže se necommituje (viz `.gitignore`).
Zkopíruj šablonu a vyplň ji:

```bash
cp collector/config.yml.example collector/config.yml
```

Otevři `collector/config.yml` a doplň všechny hodnoty označené `DOPLŇ_ZDE`:

| Hodnota | Kde ji najdeš |
|---|---|
| `fve_portal.api_url` | IP NASu + port, na kterém běží fve-portal kontejner (výchozí 8787) |
| `influxdb.url` / `token` / `org` | IP NASu, InfluxDB UI → Data → API Tokens / vpravo nahoře. Bucket je `homeassistant`. |
| `deltagreen.email` / `password` | Přihlašovací údaje na moje.deltagreen.cz |
| `deltagreen.consumption_id` / `production_id` | ID z URL `/pdt/<id>/consumption` a `/pdt/<id>/production` |

## Krok 2 — Jednorázový backfill historie

Protože collector nikdy předtím reálně neběžel, po nastavení `config.yml` spusť
jednorázově dobrání historie od `backfill_start` do posledního uplynulého měsíce:

```bash
docker compose run --rm fve-collector python main.py backfill
```

Chybu u jednoho měsíce backfill nezastaví — pokračuje dál a chybu jen zaloguje.

## Krok 3 — Nasazení (trvalý běh)

```bash
docker compose up -d --build
```

Od teď poběží scheduler, který každý měsíc (den nastavený v `scheduler.day_of_month`)
zpracuje předchozí kalendářní měsíc.

## Krok 4 — Ověření

```bash
docker compose logs -f fve-collector
```

Ruční spuštění jednoho konkrétního měsíce (např. pro test):

```bash
docker compose run --rm fve-collector python main.py 2026-08
```

## ⚠️ Poznámky

- **DeltaGreen** scraper používá Selenium — poprvé může trvat déle (spouštění
  headless Chromia).
- Data z DeltaGreen bývají zpožděná — proto se sbírá až **předchozí** celý
  měsíc, ne aktuální.
- SEMS API se už nepoužívá (nahrazeno InfluxDB) a OTE-ČR spotové ceny se nesbírají
  (fve-portal pro ně nemá tabulku).
- Zápis do fve-portal je jednorázový POST za měsíc (žádný upsert) — pokud záznam
  za dané období už existuje, collector to jen zaloguje a pokračuje.
