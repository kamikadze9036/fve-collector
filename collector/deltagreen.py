"""
DeltaGreen collector — stahuje měsíční vyúčtování nákupu a výroby elektřiny
z https://moje.deltagreen.cz (Selenium, žádné veřejné API).

Portál má pro dané odběrné místo dvě stránky:
  /pdt/<consumption_id>/consumption  — nákup (spotřeba, platby, vyrovnávání sítě)
  /pdt/<production_id>/production    — výroba (prodej elektřiny)

Sloupce tabulky se hledají podle textu hlavičky (viz CONSUMPTION_COLUMNS /
PRODUCTION_COLUMNS). Když tabulka hlavičku nemá, použije se záložní pořadí
sloupců zjištěné živou inspekcí DOM v době psaní tohoto souboru.

Selektory pro přepínání granularity a roku (`_switch_to_monthly`,
`_click_prev_year`) jsou nejpravděpodobnějším místem k opravě, pokud
DeltaGreen změní layout.
"""

import logging
import re
import time
import unicodedata
from datetime import date

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

logger = logging.getLogger(__name__)

BASE_URL   = "https://moje.deltagreen.cz"
LOGIN_URL  = f"{BASE_URL}/auth/login"

CZ_MONTHS = [
    "Leden", "Únor", "Březen", "Duben", "Květen", "Červen",
    "Červenec", "Srpen", "Září", "Říjen", "Listopad", "Prosinec",
]

# klíč -> (klíčové slovo v hlavičce bez diakritiky, záložní index sloupce za měsícem)
CONSUMPTION_COLUMNS = {
    "spotreba":      ("spotreba", 0),
    "platba_silova": ("silovou", 2),
    "regulovane":    ("regulovan", 3),
    "vyrovnani":     ("vyrovnav", 4),
}
PRODUCTION_COLUMNS = {
    "vyroba":        ("vyroba", 0),
    "platba_silova": ("silovou", 2),
}

_NUMBER_RE = re.compile(r"[-−–]?\s*\d[\d\s.]*(?:,\d+)?")
_THOUSANDS_DOT_RE = re.compile(r"\d{1,3}(?:\.\d{3})+")


def _build_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1440,900")
    options.binary_location = "/usr/bin/chromium"

    service = Service("/usr/bin/chromedriver")
    return webdriver.Chrome(service=service, options=options)


def _dismiss_cookie_banner(driver: webdriver.Chrome) -> None:
    """Zavře Cybot cookie lištu, pokud se zobrazí — jinak blokuje kliky na formulář."""
    try:
        driver.find_element(
            By.ID, "CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll"
        ).click()
        time.sleep(0.5)
    except Exception:
        pass


def _login(driver: webdriver.Chrome, wait: WebDriverWait, email: str, password: str) -> None:
    driver.get(LOGIN_URL)
    email_input = wait.until(EC.presence_of_element_located(
        (By.XPATH, "//input[@type='email' or contains(@placeholder, 'e-mail')]")
    ))
    _dismiss_cookie_banner(driver)
    email_input.send_keys(email)
    driver.find_element(
        By.XPATH, "//input[@type='password' or contains(@placeholder, 'eslo')]"
    ).send_keys(password)
    _dismiss_cookie_banner(driver)
    driver.find_element(
        By.XPATH, "//button[contains(normalize-space(.), 'Přihlásit')]"
    ).click()

    wait.until(lambda d: "/auth/login" not in d.current_url)
    logger.info("DeltaGreen: přihlášení úspěšné")


def _switch_to_monthly(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    """Přepne graf/tabulku z výchozí granularity 'Dny' na 'Měsíce'."""
    granularity_btn = wait.until(EC.element_to_be_clickable(
        (By.XPATH, "//*[self::button or @role='button'][normalize-space(.)='Dny']")
    ))
    granularity_btn.click()
    time.sleep(0.5)
    option = wait.until(EC.element_to_be_clickable(
        (By.XPATH, "//*[self::button or @role='button' or @role='option'][normalize-space(.)='Měsíce']")
    ))
    option.click()
    time.sleep(1)


def _click_prev_year(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    """
    Klikne na šipku 'předchozí rok' vlevo od rozsahu (výchozí "Tento rok").
    Hledá tlačítko bez viditelného textu ve stejné řadě, nalevo od labelu.
    """
    label = wait.until(EC.presence_of_element_located(
        (By.XPATH, "//*[contains(normalize-space(.), 'Tento rok')]")
    ))
    label_rect = label.rect
    same_row = [
        b for b in driver.find_elements(By.CSS_SELECTOR, "button")
        if b.rect["height"] > 0
        and abs(b.rect["y"] - label_rect["y"]) < 15
        and b.rect["x"] < label_rect["x"]
    ]
    if not same_row:
        raise RuntimeError("DeltaGreen: nenašel jsem šipku 'předchozí rok' vedle rozsahu 'Tento rok'")
    prev_btn = max(same_row, key=lambda b: b.rect["x"])  # nejbližší nalevo od labelu
    prev_btn.click()
    time.sleep(1)


def _strip_diacritics(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    ).casefold()


def _parse_number(text: str | None) -> float | None:
    """
    Převede české číslo z tabulky na float: "1 234,56 Kč" → 1234.56,
    "−12,5" (unicode minus) → -12.5, "1.234" (tečka jako tisíce) → 1234.
    Prázdná buňka nebo pomlčka → None.
    """
    if text is None:
        return None
    text = text.replace("\xa0", " ").replace("\u202f", " ").strip()
    if text in ("", "-", "—", "–"):
        return None
    match = _NUMBER_RE.search(text)
    if not match:
        return None
    raw = match.group(0).strip()
    negative = raw[0] in "-−–"
    digits = raw.lstrip("-−– ").replace(" ", "")
    if "," in digits:
        digits = digits.replace(".", "").replace(",", ".")
    elif _THOUSANDS_DOT_RE.fullmatch(digits):
        digits = digits.replace(".", "")
    try:
        value = float(digits)
    except ValueError:
        return None
    return -value if negative else value


def _table_headers(table) -> list[str]:
    """Vrátí texty hlaviček sloupců (bez diakritiky, casefold), nebo [] když tabulka hlavičku nemá."""
    header_row = None
    thead = table.find("thead")
    if thead is not None:
        header_row = thead.find("tr")
    if header_row is None:
        for tr in table.find_all("tr"):
            if tr.find("th"):
                header_row = tr
                break
    if header_row is None:
        return []
    return [_strip_diacritics(cell.get_text(" ", strip=True)) for cell in header_row.find_all(["th", "td"])]


def _parse_monthly_table(html: str) -> tuple[list[str], dict[str, list[float | None]]]:
    """
    Najde tabulku 'Detaily spotřeby'/'Detaily výroby' a vrátí
    (hlavičky sloupců za měsícem, {český název měsíce: [zbylé sloupce jako čísla]}).
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise RuntimeError("DeltaGreen: v HTML nenalezena žádná tabulka")

    headers = _table_headers(table)[1:]  # první sloupec je měsíc
    rows: dict[str, list[float | None]] = {}
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        month_name = cells[0].get_text(strip=True)
        if month_name not in CZ_MONTHS:
            continue  # přeskočí řádek "Celkem" apod.
        rows[month_name] = [_parse_number(td.get_text()) for td in cells[1:]]

    return headers, rows


def _column_index(headers: list[str], keyword: str, fallback: int, label: str) -> int:
    if headers:
        for index, header in enumerate(headers):
            if keyword in header:
                return index
        raise RuntimeError(
            f"DeltaGreen: v hlavičce tabulky chybí sloupec '{label}' (hledám '{keyword}'), "
            f"nalezené hlavičky: {headers}"
        )
    logger.warning("DeltaGreen: tabulka nemá hlavičku, sloupec '%s' beru podle pořadí (index %d)", label, fallback)
    return fallback


def _extract(headers: list[str], row: list[float | None], columns: dict[str, tuple[str, int]]) -> dict[str, float | None]:
    values: dict[str, float | None] = {}
    for key, (keyword, fallback) in columns.items():
        index = _column_index(headers, keyword, fallback, key)
        values[key] = row[index] if index < len(row) else None
    return values


def _scrape_monthly_table(driver, wait, entity_id: str, section: str):
    driver.get(f"{BASE_URL}/pdt/{entity_id}/{section}")
    time.sleep(2)
    _dismiss_cookie_banner(driver)
    _switch_to_monthly(driver, wait)
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "table")))
    return _parse_monthly_table(driver.page_source)


def _required(values: dict[str, float | None], key: str, page: str, month_name: str) -> float:
    value = values.get(key)
    if value is None:
        raise RuntimeError(
            f"DeltaGreen: /{page} nemá pro '{month_name}' hodnotu '{key}' — "
            "vyúčtování zřejmě ještě není hotové, zkusím příště"
        )
    return value


def fetch_month(config: dict, target: date) -> dict:
    """
    Vrátí {"gridImportKwh", "pndExportKwh", "purchaseCostCzk",
    "flexibilityRevenueCzk", "saleRevenueCzk"} za měsíc `target`.

    Chybějící povinná hodnota (spotřeba, výroba, platby) je chyba — měsíc se
    nezapíše a dožene se při příštím běhu. Jen 'Vyrovnávání sítě' smí chybět
    (→ flexibilityRevenueCzk = None).
    """
    cfg = config["deltagreen"]
    month_name = CZ_MONTHS[target.month - 1]
    today = date.today()

    if target.year != today.year and today.year - target.year != 1:
        raise RuntimeError("DeltaGreen: podporován je jen dotaz na aktuální nebo bezprostředně minulý rok")

    driver = _build_driver()
    try:
        wait = WebDriverWait(driver, 20)
        _login(driver, wait, cfg["email"], cfg["password"])

        cons_headers, consumption = _scrape_monthly_table(driver, wait, cfg["consumption_id"], "consumption")
        if target.year != today.year:
            _click_prev_year(driver, wait)
            cons_headers, consumption = _parse_monthly_table(driver.page_source)

        prod_headers, production = _scrape_monthly_table(driver, wait, cfg["production_id"], "production")
        if target.year != today.year:
            _click_prev_year(driver, wait)
            prod_headers, production = _parse_monthly_table(driver.page_source)
    finally:
        driver.quit()

    cons_row = consumption.get(month_name)
    prod_row = production.get(month_name)
    if cons_row is None:
        raise RuntimeError(f"DeltaGreen: nenalezen řádek '{month_name}' na /consumption")
    if prod_row is None:
        raise RuntimeError(f"DeltaGreen: nenalezen řádek '{month_name}' na /production")

    cons = _extract(cons_headers, cons_row, CONSUMPTION_COLUMNS)
    prod = _extract(prod_headers, prod_row, PRODUCTION_COLUMNS)

    spotreba      = _required(cons, "spotreba", "consumption", month_name)
    platba_silova = _required(cons, "platba_silova", "consumption", month_name)
    regulovane    = _required(cons, "regulovane", "consumption", month_name)
    vyrovnani     = cons.get("vyrovnani")
    vyroba        = _required(prod, "vyroba", "production", month_name)
    sale_revenue  = _required(prod, "platba_silova", "production", month_name)

    return {
        "gridImportKwh": round(spotreba, 2),
        "pndExportKwh": round(vyroba, 2),
        "purchaseCostCzk": round(platba_silova + regulovane, 2),
        "flexibilityRevenueCzk": None if vyrovnani is None else round(-vyrovnani, 2),
        "saleRevenueCzk": round(sale_revenue, 2),
    }
