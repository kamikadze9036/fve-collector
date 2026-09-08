"""
DeltaGreen collector — stahuje měsíční vyúčtování nákupu a výroby elektřiny
z https://moje.deltagreen.cz (Selenium, žádné veřejné API).

Portál má pro dané odběrné místo dvě stránky:
  /pdt/<consumption_id>/consumption  — nákup (spotřeba, platby, vyrovnávání sítě)
  /pdt/<production_id>/production    — výroba (prodej elektřiny)

Selektory byly zjištěny živou inspekcí DOM (accessibility strom) v době psaní
tohoto souboru. Pokud DeltaGreen změní layout, nejpravděpodobnějším místem
k opravě je `_switch_to_monthly` a `_click_prev_year` — ověř na prvním
reálném běhu.
"""

import logging
import re
import time
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


def _parse_number(text: str | None) -> float | None:
    if text is None:
        return None
    text = text.strip().replace("\xa0", " ")
    if text in ("", "-", "—"):
        return None
    match = re.search(r"-?[\d ]+(?:,\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _parse_monthly_table(html: str) -> dict[str, list[float | None]]:
    """
    Najde tabulku 'Detaily spotřeby'/'Detaily výroby' a vrátí
    {český název měsíce: [zbylé sloupce jako čísla]}.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        raise RuntimeError("DeltaGreen: v HTML nenalezena žádná tabulka")

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

    return rows


def _scrape_monthly_table(driver, wait, entity_id: str, section: str) -> dict[str, list[float | None]]:
    driver.get(f"{BASE_URL}/pdt/{entity_id}/{section}")
    time.sleep(2)
    _dismiss_cookie_banner(driver)
    _switch_to_monthly(driver, wait)
    wait.until(EC.presence_of_element_located((By.TAG_NAME, "table")))
    return _parse_monthly_table(driver.page_source)


def fetch_month(config: dict, target: date) -> dict:
    """
    Vrátí {"gridImportKwh", "pndExportKwh", "purchaseCostCzk",
    "flexibilityRevenueCzk", "saleRevenueCzk"} za měsíc `target`.
    """
    cfg = config["deltagreen"]
    month_name = CZ_MONTHS[target.month - 1]
    today = date.today()

    driver = _build_driver()
    try:
        wait = WebDriverWait(driver, 20)
        _login(driver, wait, cfg["email"], cfg["password"])

        consumption = _scrape_monthly_table(driver, wait, cfg["consumption_id"], "consumption")
        if target.year != today.year:
            if today.year - target.year != 1:
                raise RuntimeError("DeltaGreen: podporován je jen dotaz na aktuální nebo bezprostředně minulý rok")
            _click_prev_year(driver, wait)
            consumption = _parse_monthly_table(driver.page_source)

        production = _scrape_monthly_table(driver, wait, cfg["production_id"], "production")
        if target.year != today.year:
            _click_prev_year(driver, wait)
            production = _parse_monthly_table(driver.page_source)
    finally:
        driver.quit()

    cons_row = consumption.get(month_name)
    prod_row = production.get(month_name)
    if cons_row is None or len(cons_row) < 5:
        raise RuntimeError(f"DeltaGreen: nenalezen/neúplný řádek '{month_name}' na /consumption")
    if prod_row is None or len(prod_row) < 3:
        raise RuntimeError(f"DeltaGreen: nenalezen/neúplný řádek '{month_name}' na /production")

    # consumption sloupce (za měsícem, index 0 = Spotřeba): Spotřeba, Cena, Platba za
    # silovou elektřinu, Regulované platby a ostatní poplatky, Vyrovnávání sítě, Celková platba
    spotreba      = cons_row[0] or 0.0
    platba_silova = cons_row[2] or 0.0
    regulovane    = cons_row[3] or 0.0
    vyrovnani     = cons_row[4] or 0.0

    # production sloupce (za měsícem): Výroba, Cena, Platba za silovou elektřinu
    vyroba       = prod_row[0] or 0.0
    sale_revenue = prod_row[2] or 0.0

    return {
        "gridImportKwh": round(spotreba, 2),
        "pndExportKwh": round(vyroba, 2),
        "purchaseCostCzk": round(platba_silova + regulovane, 2),
        "flexibilityRevenueCzk": round(-vyrovnani, 2),
        "saleRevenueCzk": round(sale_revenue, 2),
    }
