# Revize codebase fve-collector + fve-portal

Datum revize: 2026-09-18
Rozsah: `collector/` (Python), `fve-portal/` (Next.js 16, Drizzle, SQLite), Docker a dokumentace.

Co bylo ověřeno:
- `python3 -m py_compile collector/*.py` → OK
- `npx tsc --noEmit` v fve-portal → OK, bez chyb
- `npx oxlint` v fve-portal → chyby (viz níže)
- `npx drizzle-kit check` → hlásí OK, ale je to zavádějící (viz první bod)
- dry-run `drizzle-kit generate` do dočasné kopie → potvrdil chybějící snapshot

Stav: body 1–7 opravené 2026-09-18 (collector i fve-portal, viz git log). Body 8–19 zůstávají otevřené.

---

## Kritické a vysoké

### 1. Chybí `fve-portal/drizzle/meta/0002_snapshot.json`
- [x] Opraveno 2026-09-18

Migrace `0002_measurement_workflow.sql` byla psaná ručně a nemá snapshot v `drizzle/meta/`
(existují jen `0000_snapshot.json` a `0001_snapshot.json`). Dry-run ukázal, že příští
`npm run db:generate` vygeneruje `0003`, která znovu vytváří všech 7 tabulek z 0002
(`audit_log`, `economics_settings`, `electricity_meter_readings`, …). Na NASu pak
`docker-entrypoint.sh` spadne na "table already exists" a kontejner nenastartuje.
`drizzle-kit check` to nehlásí.

Postup opravy:
1. `npx drizzle-kit generate --name fix-snapshot` → vznikne `0003_fix-snapshot.sql` a `meta/0003_snapshot.json`.
2. `meta/0003_snapshot.json` přejmenovat na `meta/0002_snapshot.json`, uvnitř nastavit `prevId` na `id` ze `0001_snapshot.json`.
3. Smazat `0003_fix-snapshot.sql` a jeho záznam z `meta/_journal.json`.
4. Znovu spustit dry-run `generate` a ověřit, že už nic negeneruje.

### 2. Tajemství se zapékají do Docker image collectoru
- [x] Opraveno 2026-09-18

`collector/Dockerfile:14` dělá `COPY . .` a collector nemá `.dockerignore`, takže
`config.yml` (heslo DeltaGreen, InfluxDB token) skončí ve vrstvě image. Compose ho
stejně mountuje jako volume.

Oprava: přidat `collector/.dockerignore` s obsahem:
```
config.yml
__pycache__/
*.pyc
```

### 3. Zápis přes `/api/data` přepisuje existující data nulami
- [x] Opraveno 2026-09-18

- `fve-portal/app/api/data/route.ts:55` dělá upsert se `set: values`, kde jsou všechny
  sloupce včetně `null`. Collector posílá `meterNtKwh`, `meterVtKwh`,
  `pvSelfUseReportedKwh` vždy jako `null` (`collector/main.py:70-77`), takže ručně
  zadaný stav elektroměru za dané období smaže.
- `fve-portal/lib/interval-records.ts:14` propíše `null` pole do update a do
  `interval_field_sources` zapíše zdroj "DeltaGreen" i pro hodnoty, které DeltaGreen nedodal.
- README collectoru tvrdí "žádný upsert" a větev pro 400 "existuje" v
  `collector/main.py:88` je po přechodu na upsert mrtvý kód.

Oprava: v obou upsertech vynechat klíče s hodnotou `null` (update jen dodaných hodnot),
zdroj zapisovat jen pro ne-null pole, opravit README collectoru a odstranit mrtvou větev.

### 4. Chybějící hodnoty z DeltaGreen se maskují na nulu
- [x] Opraveno 2026-09-18

`collector/deltagreen.py:203-210` dělá `or 0.0`, takže nenalezený sloupec se uloží jako
0 Kč / 0 kWh místo chyby nebo `null`. V kombinaci s bodem 3 jde o tichou korupci dat.

Oprava: pro povinné sloupce vyhodit `RuntimeError`, pro volitelné poslat `None`
(portál `null` akceptuje).

### 5. Zmeškaný měsíc se nikdy nedožene
- [x] Opraveno 2026-09-18

APScheduler drží joby jen v paměti a výchozí `misfire_grace_time` je 1 s. Když je
kontejner 3. v měsíci v 6:00 dole nebo DeltaGreen selže, měsíc se ztratí a další běh
sbírá zase jen předchozí měsíc.

Oprava: při každém běhu stáhnout `GET /api/data`, spočítat chybějící období od
`backfill_start` do minulého měsíce a doplnit je. Sběr je tím idempotentní a
`backfill` jako zvláštní režim odpadne. Volitelně nastavit `misfire_grace_time`
(např. 3600 s) a spustit kontrolu i při startu kontejneru.

---

## Střední

### 6. InfluxDB okno na hranici měsíce
- [x] Opraveno 2026-09-18

`collector/influx_ha.py:47` používá `aggregateWindow(every: 1mo)` v UTC, ale rozsah je
v Europe/Prague. Vzniknou dvě řádky (1 až 2 hodinový střípek na začátku měsíce plus
zbytek) a smyčka na řádcích 66-71 bere poslední. Ztrácí se noční odběr z první hodiny
měsíce.

Oprava: nahradit `aggregateWindow` + `pivot` za `|> spread()` přes celý rozsah a číst
`friendly_name` z tagu záznamu (`record.values["friendly_name"]`).

### 7. DeltaGreen parser je křehký
- [x] Opraveno 2026-09-18, ověřit na prvním ostrém běhu

- Sloupce se berou podle pozice (`collector/deltagreen.py:203-210`), ne podle hlavičky tabulky.
- `_parse_number` (`deltagreen.py:118`) neumí unicode minus (U+2212) ani tečku jako
  oddělovač tisíců, takže záporné "Vyrovnávání sítě" může přijít jako kladné.
- Podle README collector nikdy reálně neběžel. První ostrý běh porovnat ručně s portálem.

Oprava: mapovat sloupce podle textu `<th>`, rozšířit regex o `−` a tečku, přidat testy
nad uloženým HTML fixture.

### 8. Stránka posílá klientovi celý seed a ukazuje zastaralá data
- [ ] Opravit

`fve-portal/app/page.tsx` vykreslí 1500 řádků statického seedu, klient pak v efektu
(`components/fve-dashboard.tsx:68`) stáhne skutečná data a přepíše je. Uživatel nejdřív
vidí stará čísla, oxlint hlásí setState v efektu. Formulář má navíc natvrdo
`period: '2026-09'` (`fve-dashboard.tsx:48`).

Oprava: v `page.tsx` zavolat `getAllData()` na serveru a předat jako `initialData`,
efekt s refetchem odstranit. Výchozí období odvodit z aktuálního data.

### 9. Healthcheck každých 15 s dumpuje 9 tabulek
- [ ] Opravit

`fve-portal/compose.yaml:14` volá `/api/data`, což vrací všechna data a spouští kontrolu
seedu. Přidat lehký `GET /api/health` (např. `SELECT 1`) a healthcheck přesměrovat.

### 10. Chybová odpověď POST /api/data
- [ ] Opravit

`fve-portal/app/api/data/route.ts:63-66` vrací 400 i na chybu databáze a větev
s `UNIQUE` je po přechodu na upsert nedosažitelná. Rozlišit 400 (validace) a 500 (DB).

### 11. Lint neprochází
- [ ] Opravit

`npm run lint` hlásí chyby i v aplikačním kódu:
- nepoužitý parametr `year` (`components/fve-dashboard.tsx:237`)
- deprecated `FormEvent` (`fve-dashboard.tsx:96`, `:197`)
- deprecated `Cell` z recharts (`fve-dashboard.tsx:204`)
- `role="status"` / `role="img"` místo sémantických tagů (`fve-dashboard.tsx:151`, `:269`)
- setState v efektu (`fve-dashboard.tsx:68`, `hooks/use-mobile.ts:16`)

Zbytek chyb je v nepoužitých shadcn komponentách (viz bod 14).

### 12. Duplicitní výpočty s odlišnou sémantikou
- [ ] Opravit

`components/fve-dashboard.tsx:40-47` má vlastní `ownUse`, `distributorConsumption`,
`inverterConsumption` a `netCost` (s `Math.max(0, …)` a `?? 0`), zatímco
`lib/electricity-calc.ts` a `lib/economics-calc.ts` počítají totéž bez ořezu.
Dashboard a záložka Ekonomika tak mohou ukázat různá čísla za stejný měsíc.

Oprava: sjednotit na funkce z `lib/`, rozhodnout jednu sémantiku pro záporné hodnoty.

### 13. Žádné testy ani CI
- [ ] Doplnit

Ani jeden projekt nemá testy. Nejvíc by pomohly:
- collector: `_parse_number`, `_parse_monthly_table` nad uloženým HTML fixture, `_months_between`
- portál: `lib/*-calc.ts`, `lastDayOfMonth`, validace v `readings-validation.ts`

---

## Nízké a hygiena

### 14. Nepoužité shadcn komponenty a závislosti
- [ ] Vyčistit

Používá se 7 z 60 komponent v `components/ui/` (button, card, dialog, input, label,
native-select, table). Zbytek tahá závislosti `embla-carousel-react`, `cmdk`,
`input-otp`, `react-day-picker`, `react-resizable-panels`. Zvětšuje image a lint výstup.

### 15. Mrtvé zbytky
- [ ] Vyčistit

- `fve-portal/db/env.d.ts` (Cloudflare D1 typ, už se nepoužívá)
- root `db/.gitkeep` a ignore `db/*.db` (historická SQLite)
- komentář "ČEZ PND scraper" v `collector/Dockerfile:3` (je to DeltaGreen)
- `"name": "sites-project"` v `fve-portal/package.json`
- import na konci souboru `fve-portal/lib/economics-calc.ts:30`

### 16. Formátování dashboardu
- [ ] Spustit `npm run format`

`components/fve-dashboard.tsx` má 19 řádků delších než 400 znaků a funkce na jeden
řádek. Diff a review jsou prakticky nemožné.

### 17. Drobnosti v API
- [ ] Zvážit

- PATCH routy (`app/api/readings/*/[id]/route.ts`) čtou `current` mimo `serializeWrite`,
  audit log tak může zapsat starou hodnotu při souběžné změně.
- `updatedAt` míchá ISO formát (`new Date().toISOString()`) a SQLite `CURRENT_TIMESTAMP`.
- API nemá autentizaci. Na LAN v pořádku, ale kdokoli v síti může přepsat data.

### 18. Závislosti collectoru
- [ ] Zvážit aktualizaci

`requests==2.31.0` má známou CVE-2024-35195 (pro tento use case bez dopadu),
`selenium==4.18.1` je z roku 2024. Funkčně OK, ale piny stárnou.

### 19. Struktura repozitářů
- [ ] Zvážit

Root `.gitignore` ignoruje celý `fve-portal/`, ale README na něj odkazuje
(`fve-portal/DOCKER.md`). Klon `fve-collector` z GitHubu portál neobsahuje.
Zvážit git submodule nebo aspoň odkaz na repo portálu v README.
