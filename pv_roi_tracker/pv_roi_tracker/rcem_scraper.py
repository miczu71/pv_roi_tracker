"""
RCEm (Rynkowa Cena Energii Miesięczna) scraper.

Fetches the previous month's feed-in price from the PSE website on the 11th
of each month at 20:00 UTC, with daily retries through the 20th.
Stores results in /data/rcem_history.json (keyed YYYY-MM, values in PLN/kWh).
"""
from __future__ import annotations

import json
import logging
import unicodedata
from datetime import date
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

PSE_URL = 'https://www.pse.pl/oire/rcem-rynkowa-miesieczna-cena-energii-elektrycznej'
DEFAULT_HISTORY_PATH = Path('/data/rcem_history.json')
_HISTORY_MAX_ENTRIES = 36
_MAX_PLN_MWH = 2000.0


# ── History helpers ───────────────────────────────────────────────────────────

def _load_history(path: Path = DEFAULT_HISTORY_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_history(history: dict, path: Path = DEFAULT_HISTORY_PATH) -> None:
    if len(history) > _HISTORY_MAX_ENTRIES:
        for k in sorted(history)[:-_HISTORY_MAX_ENTRIES]:
            del history[k]
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.rename(path)


# ── Number parsing ────────────────────────────────────────────────────────────

def _parse_pln_mwh(text: str) -> Optional[float]:
    """Parse a Polish-formatted PLN/MWh value like '376,52' → 376.52."""
    s = unicodedata.normalize('NFC', text.strip())
    s = s.replace('\xa0', '').replace(' ', '').replace(',', '.')
    try:
        v = float(s)
        return v if 0 < v <= _MAX_PLN_MWH else None
    except ValueError:
        return None


# ── Month matching ────────────────────────────────────────────────────────────

_PL_MONTHS = {
    'styczeń': 1, 'stycznia': 1, 'sty': 1,
    'luty': 2, 'lutego': 2, 'lut': 2,
    'marzec': 3, 'marca': 3, 'mar': 3,
    'kwiecień': 4, 'kwietnia': 4, 'kwi': 4,
    'maj': 5, 'maja': 5,
    'czerwiec': 6, 'czerwca': 6, 'cze': 6,
    'lipiec': 7, 'lipca': 7, 'lip': 7,
    'sierpień': 8, 'sierpnia': 8, 'sie': 8,
    'wrzesień': 9, 'września': 9, 'wrz': 9,
    'październik': 10, 'października': 10, 'paź': 10,
    'listopad': 11, 'listopada': 11, 'lis': 11,
    'grudzień': 12, 'grudnia': 12, 'gru': 12,
}


def _row_matches(cell_text: str, year: int, month: int) -> bool:
    """Return True if a table cell text refers to the given year+month."""
    t = unicodedata.normalize('NFC', cell_text.strip().lower())
    year_s = str(year)
    month_s = f'{month:02d}'

    # Numeric patterns: "2026-05", "05.2026", "2026/05"
    if year_s in t and month_s in t:
        return True
    # Polish name patterns: "maj 2026", "maja 2026"
    if year_s in t:
        for name, num in _PL_MONTHS.items():
            if num == month and name in t:
                return True
    return False


# ── Core scrape ───────────────────────────────────────────────────────────────

def scrape_rcem(target_month: Optional[str] = None) -> Optional[float]:
    """
    Fetch the PSE page and return the RCEm price (PLN/kWh) for target_month (YYYY-MM).
    Defaults to the previous calendar month. Returns None if not yet published.
    """
    today = date.today()
    if target_month is None:
        y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        target_month = f'{y}-{m:02d}'

    year, month = int(target_month[:4]), int(target_month[5:])
    logger.info('Scraping RCEm for %s from PSE', target_month)

    try:
        resp = requests.get(PSE_URL, timeout=30,
                            headers={'User-Agent': 'Mozilla/5.0 (compatible; pv_roi_tracker)'})
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error('PSE fetch failed: %s', exc)
        return None

    soup = BeautifulSoup(resp.content, 'html.parser')

    for table in soup.find_all('table'):
        for row in table.find_all('tr'):
            cells = [c.get_text(' ', strip=True) for c in row.find_all(['td', 'th'])]
            if not cells:
                continue
            if _row_matches(cells[0], year, month):
                for cell in cells[1:]:
                    val = _parse_pln_mwh(cell)
                    if val is not None:
                        price_kwh = round(val / 1000.0 * 1.23, 6)  # PSE publishes net; add 23% VAT
                        logger.info('RCEm %s = %.4f zł/kWh (%.2f zł/MWh)', target_month, price_kwh, val)
                        return price_kwh

    logger.info('RCEm for %s not yet published on PSE page', target_month)
    return None


# ── Scheduled entry points ────────────────────────────────────────────────────

def get_current_month_rcem(path: Path = DEFAULT_HISTORY_PATH) -> Optional[float]:
    """Return the RCEm price for the current calendar month if already known."""
    today = date.today()
    return _load_history(path).get(f'{today.year}-{today.month:02d}')


def run_scheduled_scrape(
    target_month: Optional[str] = None,
    history_path: Path = DEFAULT_HISTORY_PATH,
    historic_json_path: Optional[Path] = None,
    on_success=None,
) -> bool:
    """
    Attempt one scrape. Saves to rcem_history.json, backfills historic.json, calls
    on_success(price) when a new price is found. Returns True if price is known.
    """
    from . import historic_store

    today = date.today()
    if target_month is None:
        y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        target_month = f'{y}-{m:02d}'

    if historic_json_path is None:
        historic_json_path = historic_store.DEFAULT_PATH

    history = _load_history(history_path)
    if target_month in history:
        logger.debug('RCEm for %s already cached (%.4f) — no scrape needed', target_month, history[target_month])
        return True

    price = scrape_rcem(target_month)
    if price is None:
        return False

    history[target_month] = price
    _save_history(history, history_path)

    year, month = int(target_month[:4]), int(target_month[5:])
    historic_store.backfill_rcem(year, month, price, historic_json_path)

    if on_success:
        on_success(price)

    return True
