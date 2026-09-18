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
│   ├── .dockerignore       ← config.yml se do image nikdy nekopíruje
│   ├── requirements.txt
│   ├── config.yml          ← ⚠️  VYPLŇ PŘED SPUŠTĚNÍM
│   ├── main.py             ← scheduler + dohledání chybějících měsíců (vstupní bod)
│   ├── deltagreen.py       ← DeltaGreen — odběr/dodávka, náklady na nákup, tržby z prodeje
│   ├── influx_ha.py        ← InfluxDB (Home Assistant) — výroba FVE, přetok/nákup
│   └── tests/              ← unit testy parseru a výpočtů (bez sítě)
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

## Krok 2 — Dohledání chybějících měsíců

Collector si při každém startu kontejneru a při každém měsíčním běhu stáhne
z fve-portal seznam existujících období a doplní všechny měsíce od
`backfill_start` do posledního uplynulého měsíce, které tam chybí. Zmeškaný běh
(výpadek NASu, chyba DeltaGreen) se tedy dožene sám.

Měsíc se považuje za hotový, když ve fve-portal existuje záznam za dané období,
včetně řádků importovaných ze sešitu. Pokud chceš historii doplnit ručně mimo
scheduler:

```bash
docker compose run --rm fve-collector python main.py backfill
```

Chybu u jednoho měsíce dohledání nezastaví — pokračuje dál, chybu zaloguje a
měsíc zkusí znovu při příštím běhu. Testy parseru jdou spustit bez sítě:

```bash
cd collector && python -m unittest discover tests
```

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

Vynucené zpracování jednoho konkrétního měsíce (např. pro test, nebo když chceš
přepsat hodnoty importované ze sešitu):

```bash
docker compose run --rm fve-collector python main.py 2026-08
```

Přepíšou se jen hodnoty, které collector reálně sebral. Pole, která nepošle
(stav elektroměru, chybějící čítač v InfluxDB), zůstanou ve fve-portal beze změny.

## ⚠️ Poznámky

- **DeltaGreen** scraper používá Selenium — poprvé může trvat déle (spouštění
  headless Chromia).
- Data z DeltaGreen bývají zpožděná — proto se sbírá až **předchozí** celý
  měsíc, ne aktuální.
- SEMS API se už nepoužívá (nahrazeno InfluxDB) a OTE-ČR spotové ceny se nesbírají
  (fve-portal pro ně nemá tabulku).
- Zápis do fve-portal je POST za měsíc. Endpoint `/api/data` dělá upsert, ale
  `null`/chybějící pole nikdy nepřepisuje. Automatický běh se existujícím měsícům
  vyhýbá (viz Krok 2), přepsat je jde jen ručně přes `python main.py YYYY-MM`.
- Když DeltaGreen za daný měsíc ještě nemá vyúčtování (prázdná spotřeba, výroba
  nebo platba), collector měsíc nezapíše a zkusí to při příštím běhu. Chybět smí
  jen „Vyrovnávání sítě“.
- Sloupce tabulky DeltaGreen se hledají podle textu hlavičky. Když DeltaGreen
  hlavičku přejmenuje, collector skončí chybou se seznamem nalezených hlaviček
  (oprava v `CONSUMPTION_COLUMNS` / `PRODUCTION_COLUMNS` v `deltagreen.py`).
