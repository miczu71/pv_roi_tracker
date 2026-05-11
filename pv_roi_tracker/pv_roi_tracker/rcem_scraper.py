"""
RCEm (Rynkowa Cena Energii Miesięczna) scraper.

Fetches RCEm prices from the PSE website. On every run the full table is
parsed so that 'skorygowana RCEm' corrections are detected and applied
retroactively. Stores results in /data/rcem_history.json (keyed YYYY-MM,
values in PLN/kWh gross including 23% VAT).
"""
from __future__ import annotations

import json
import logging
import re
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


# ── Year/month extraction ─────────────────────────────────────────────────────

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


def _parse_year_month(text: str) -> Optional[tuple[int, int]]:
    """Extract (year, month) from a table cell. Returns None if not parseable."""
    t = unicodedata.normalize('NFC', text.strip())
    # Numeric: "2026-04", "04.2026", "2026/04"
    for pat in (r'(20\d{2})[.\-/](\d{1,2})\b', r'\b(\d{1,2})[.\-/](20\d{2})'):
        m = re.search(pat, t)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            year, month = (a, b) if a > 2000 else (b, a)
            if 1 <= month <= 12:
                return year, month
    # Polish name: "kwiecień 2026", "maja 2026"
    year_m = re.search(r'(20\d{2})', t)
    if year_m:
        year = int(year_m.group(1))
        t_lower = t.lower()
        for name, num in _PL_MONTHS.items():
            if name in t_lower:
                return year, num
    return None


# ── Column detection ──────────────────────────────────────────────────────────

def _find_rcem_columns(header_cells: list[str]) -> tuple[int, int]:
    """
    Scan header row for RCEm price columns.
    Returns (base_col, corrected_col), each -1 if not found.
    'corrected_col' matches headers containing 'skorygowan'.
    'base_col' matches headers with 'rcem' but not 'skorygowan' or 'różni'.
    """
    base_col = corrected_col = -1
    for i, h in enumerate(header_cells):
        h_n = unicodedata.normalize('NFC', h.strip().lower())
        if 'skorygowan' in h_n:
            corrected_col = i
        elif 'rcem' in h_n and 'różni' not in h_n and base_col == -1:
            base_col = i
    return base_col, corrected_col


# ── Core scrape ───────────────────────────────────────────────────────────────

def scrape_all_months() -> dict[str, float]:
    """
    Fetch the PSE page once and return all available months as {YYYY-MM: PLN/kWh gross}.
    Uses 'skorygowana RCEm' where present, falling back to the base RCEm price.
    PLN/MWh (net) is converted to PLN/kWh gross by dividing by 1000 and adding 23% VAT.
    """
    try:
        resp = requests.get(PSE_URL, timeout=30,
                            headers={'User-Agent': 'Mozilla/5.0 (compatible; pv_roi_tracker)'})
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error('PSE fetch failed: %s', exc)
        return {}

    soup = BeautifulSoup(resp.content, 'html.parser')
    results: dict[str, float] = {}

    for table in soup.find_all('table'):
        rows = table.find_all('tr')
        if not rows:
            continue

        header_cells = [c.get_text(' ', strip=True) for c in rows[0].find_all(['td', 'th'])]

        # Skip tables unrelated to RCEm
        if not any('rcem' in unicodedata.normalize('NFC', h.strip().lower())
                   for h in header_cells):
            continue

        base_col, corrected_col = _find_rcem_columns(header_cells)

        for row in rows[1:]:
            cells = [c.get_text(' ', strip=True) for c in row.find_all(['td', 'th'])]
            if not cells:
                continue
            ym = _parse_year_month(cells[0])
            if ym is None:
                continue
            year, month = ym
            key = f'{year}-{month:02d}'

            # Prefer corrected price, fall back to base, then first numeric cell
            price_mwh: Optional[float] = None
            if corrected_col > 0 and corrected_col < len(cells):
                price_mwh = _parse_pln_mwh(cells[corrected_col])
                if price_mwh is not None:
                    logger.debug('Using skorygowana RCEm for %s: %.2f PLN/MWh', key, price_mwh)
            if price_mwh is None and base_col > 0 and base_col < len(cells):
                price_mwh = _parse_pln_mwh(cells[base_col])
            if price_mwh is None:
                for cell in cells[1:]:
                    price_mwh = _parse_pln_mwh(cell)
                    if price_mwh is not None:
                        break

            if price_mwh is not None:
                results[key] = round(price_mwh / 1000.0 * 1.23, 6)  # net → gross PLN/kWh

    logger.info('PSE page: %d months parsed', len(results))
    return results


def scrape_rcem(target_month: Optional[str] = None) -> Optional[float]:
    """
    Return the effective RCEm price (PLN/kWh gross) for target_month.
    Defaults to the previous calendar month. Returns None if not published.
    Used by the CLI; production code calls run_scheduled_scrape instead.
    """
    today = date.today()
    if target_month is None:
        y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        target_month = f'{y}-{m:02d}'
    logger.info('Scraping RCEm for %s', target_month)
    return scrape_all_months().get(target_month)


# ── Scheduled entry points ────────────────────────────────────────────────────

def get_current_month_rcem(path: Path = DEFAULT_HISTORY_PATH) -> Optional[float]:
    """Return the RCEm price for the current calendar month if already known."""
    today = date.today()
    return _load_history(path).get(f'{today.year}-{today.month:02d}')


def run_scheduled_scrape(
    target_month: Optional[str] = None,
    history_path: Path = DEFAULT_HISTORY_PATH,
    historic_json_path: Optional[Path] = None,
    on_update=None,   # called (no args) once if any price was new or corrected
    on_success=None,  # legacy alias; ignored when on_update is provided
) -> bool:
    """
    Fetch all months from PSE. Update rcem_history.json and backfill historic.json
    for any month whose price is new or has changed (skorygowana correction).
    Calls on_update() once if anything changed.
    Returns True if target_month price is now known.
    """
    from . import historic_store

    today = date.today()
    if target_month is None:
        y, m = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        target_month = f'{y}-{m:02d}'

    if historic_json_path is None:
        historic_json_path = historic_store.DEFAULT_PATH

    callback = on_update or on_success

    scraped = scrape_all_months()
    if not scraped:
        logger.warning('PSE returned no data — keeping existing history')
        return target_month in _load_history(history_path)

    history = _load_history(history_path)
    updated: list[str] = []

    for key, new_price in scraped.items():
        old_price = history.get(key)
        if old_price is None:
            logger.info('New RCEm price for %s: %.4f PLN/kWh', key, new_price)
            updated.append(key)
        elif abs(new_price - old_price) > 1e-6:
            pct = (new_price - old_price) / old_price * 100
            logger.info('RCEm correction for %s: %.4f → %.4f PLN/kWh (%+.2f%%)',
                        key, old_price, new_price, pct)
            updated.append(key)

    if updated:
        for key in updated:
            history[key] = scraped[key]
        _save_history(history, history_path)
        for key in updated:
            year, month = int(key[:4]), int(key[5:])
            historic_store.backfill_rcem(year, month, history[key], historic_json_path)
            logger.info('Backfilled historic.json for %s', key)
        if callback:
            callback()

    return target_month in history
