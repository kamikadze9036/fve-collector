# Nasazení oprav na Synology NAS

Postup pro nasazení commitů `fve-portal@a43e9cb` a `fve-collector@343958a`
(opravy bodů 1–7 z `REVIEW.md`). Obojí už je pushnuté na GitHubu:

- https://github.com/kamikadze9036/fve-portal (branch `main`)
- https://github.com/kamikadze9036/fve-collector (branch `main`)

## Kde co běží

| Projekt | Adresář na NASu | Kontejner | Port |
|---|---|---|---|
| fve-portal | `/volume1/docker/fve-portal` | `fve-portal-fve-portal-1` | 8787 |
| fve-collector | `/volume1/docker/fve-collector` | `fve-collector` | – |

Databáze portálu: `/volume1/docker/fve-portal/data/fve.db` (SQLite, WAL).
Konfigurace collectoru s hesly: `/volume1/docker/fve-collector/collector/config.yml`
(není v gitu, na NASu už je vyplněná, **nepřepisovat**).

## Přístup na NAS

- `ssh kamikadze-nas` (alias v `~/.ssh/config`, klíč `~/.ssh/id_ed25519_synology`).
- Docker jen přes `sudo -n /usr/local/bin/docker …` (uživatel nemá přístup k socketu).
- **rsync na tento NAS nefunguje.** Soubory kopíruj přes `tar` streamovaný do ssh.
- Nesahat na kontejnery `grafana-1`, `influxdb1`, `home_assistant` (spravované přes GUI).

## Pořadí: nejdřív portál, pak collector

Nový collector posílá `null` pole jen novému portálu. Starý portál na NASu (bez
upsertu) by u měsíců, které už jsou v seedu, vrátil 400. Proto portál první.

### 1. Záloha databáze portálu

```bash
ssh kamikadze-nas 'cd /volume1/docker/fve-portal && sudo -n /usr/local/bin/docker compose exec -T fve-portal node -e "require(\"better-sqlite3\")(\"/data/fve.db\").backup(\"/data/fve_backup_$(date +%Y%m%d_%H%M%S).db\").then(()=>console.log(\"backup ok\"))"'
```

Pokud `exec` selže (kontejner neběží), stačí kontejner zastavit a zkopírovat
`data/fve.db` + `fve.db-wal` + `fve.db-shm` do `data/backup-<datum>/`.

### 2. Nasazení fve-portal

Z lokálního klonu `fve-portal` (musí být na `main` v `a43e9cb` nebo novější):

```bash
cd fve-portal
git fetch && git checkout main && git pull
tar -czf - --exclude=node_modules --exclude=.git --exclude=.next --exclude=data . \
  | ssh kamikadze-nas 'tar -xzf - -C /volume1/docker/fve-portal'
ssh kamikadze-nas 'cd /volume1/docker/fve-portal && sudo -n /usr/local/bin/docker compose up --build -d'
```

`--exclude=data` je důležité: složka `data/` na NASu obsahuje živou databázi.

Ověření:

```bash
ssh kamikadze-nas 'sudo -n /usr/local/bin/docker ps --filter name=fve-portal'
ssh kamikadze-nas 'sudo -n /usr/local/bin/docker logs --tail 30 fve-portal-fve-portal-1'
ssh kamikadze-nas 'ls /volume1/docker/fve-portal/drizzle/meta'   # musí obsahovat 0002_snapshot.json
curl -s http://192.168.20.9:8787/api/data | head -c 300
```

Kontejner musí být `healthy`, log bez chyby migrace, `/api/data` vrací JSON.

### 3. Nasazení fve-collector

Z lokálního klonu `fve-collector` (`main` v `343958a` nebo novější):

```bash
cd fve-collector
git fetch && git checkout main && git pull
tar -czf - --exclude=.git --exclude=fve-portal --exclude='*.xlsx' --exclude=__pycache__ \
  --exclude=collector/config.yml . \
  | ssh kamikadze-nas 'tar -xzf - -C /volume1/docker/fve-collector'
ssh kamikadze-nas 'cd /volume1/docker/fve-collector && sudo -n /usr/local/bin/docker compose up --build -d'
```

`--exclude=collector/config.yml` je důležité: lokální kopie by přepsala tu na NASu.

Ověření:

```bash
ssh kamikadze-nas 'sudo -n /usr/local/bin/docker logs --tail 50 fve-collector'
```

Očekávaný log po startu:
- `Všechny měsíce do 2026-08 už jsou ve fve-portal, nic k doplnění` (seed
  pokrývá 2026-01 až 08), nebo `Chybí N měsíc(ů): …` následované sběrem,
- `Scheduler spuštěn — sběr každý měsíc 3. v 6:00`.

Chyba `fve-portal odpověděl 400` znamená, že portál ještě běží ve staré verzi
(krok 2 se nepovedl).

### 4. Volitelně: ostrý test DeltaGreen parseru

Parser sloupců podle hlavičky ještě neběžel proti živému DeltaGreen. Test na
jednom měsíci, který přepíše hodnoty ze seedu jen tam, kde collector něco sebral:

```bash
ssh kamikadze-nas 'cd /volume1/docker/fve-collector && sudo -n /usr/local/bin/docker compose run --rm fve-collector python main.py 2026-08'
```

V logu porovnat řádek `Payload za 2026-08-01` s tabulkou na moje.deltagreen.cz.
Když skončí chybou `v hlavičce tabulky chybí sloupec …`, log obsahuje nalezené
hlavičky; oprava je v `CONSUMPTION_COLUMNS` / `PRODUCTION_COLUMNS`
v `collector/deltagreen.py`.

## Rollback

- Portál: `docker compose down`, vrátit zálohu z kroku 1 do `data/fve.db`
  (smazat `-wal` a `-shm`), nasadit předchozí commit stejným postupem.
- Collector: nasadit předchozí commit (`1bb98f7`) stejným postupem. Databázi
  neovlivňuje.
