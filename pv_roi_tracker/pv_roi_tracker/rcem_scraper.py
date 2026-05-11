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
DEFAULT_HISTORY_PATH     = Path('/data/rcem_history.json')
DEFAULT_CORRECTIONS_PATH = Path('/data/rcem_corrections.json')
_HISTORY_MAX_ENTRIES = 60
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


# ── Correction history helpers ────────────────────────────────────────────────

def _load_corrections(path: Path = DEFAULT_CORRECTIONS_PATH) -> dict:
    """Load {YYYY-MM: [{price, recorded_at}, ...]} correction history."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_corrections(corrections: dict, path: Path = DEFAULT_CORRECTIONS_PATH) -> None:
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(corrections, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.rename(path)


def get_price_history(month_key: str, path: Path = DEFAULT_CORRECTIONS_PATH) -> list:
    """Return [{price, recorded_at}, ...] for month_key, oldest first. Empty if never corrected."""
    return _load_corrections(path).get(month_key, [])


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


def _match_month(text: str) -> Optional[int]:
    """Return month number if text contains a Polish month name, else None."""
    t = unicodedata.normalize('NFC', text.strip().lower())
    for name, num in _PL_MONTHS.items():
        if name in t:
            return num
    return None


# ── Core scrape ───────────────────────────────────────────────────────────────

def scrape_all_months() -> dict[str, float]:
    """
    Fetch the PSE page and return all available months as {YYYY-MM: PLN/kWh gross}.

    PSE table structure (one table per year):
      row 0:  ['2026']                    ← year
      row 1:  ['', 'cena [zł/MWh]', ...]  ← column headers
      row N:  ['styczeń']                 ← month name (Polish, no year)
      row N+1:['RCEm', '551,96', ...]     ← base price
      row N+2:['skorygowana RCEm*', '-' or price, ...]  ← correction (dash = none)
      ...repeated for each month

    PLN/MWh net is converted to PLN/kWh gross by ÷1000 and ×1.23 (23% VAT).
    Corrected price takes precedence over the base price when present.
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
        if len(rows) < 3:
            continue

        # Row 0 must be a bare year number
        year_text = rows[0].get_text(strip=True)
        if not re.fullmatch(r'20\d{2}', year_text):
            continue
        year = int(year_text)

        current_month: Optional[int] = None
        base_mwh: Optional[float] = None
        corrected_mwh: Optional[float] = None

        def _store(y: int, m: int, base: float, corrected: Optional[float]) -> None:
            effective = corrected if corrected is not None else base
            key = f'{y}-{m:02d}'
            results[key] = round(effective / 1000.0 * 1.23, 6)
            if corrected is not None:
                logger.info('Month %s: RCEm=%.2f → skorygowana=%.2f PLN/MWh', key, base, corrected)

        for row in rows[1:]:
            cells = [c.get_text(' ', strip=True) for c in row.find_all(['td', 'th'])]
            if not cells:
                continue

            first = unicodedata.normalize('NFC', cells[0].strip().lower())

            # Month-name row: contains a Polish month name
            month_num = _match_month(first)
            if month_num is not None:
                if current_month is not None and base_mwh is not None:
                    _store(year, current_month, base_mwh, corrected_mwh)
                current_month = month_num
                base_mwh = corrected_mwh = None
                continue

            if current_month is None:
                continue

            # Base RCEm row: first cell is exactly 'rcem'
            if first == 'rcem' and len(cells) > 1:
                base_mwh = _parse_pln_mwh(cells[1])
                continue

            # Correction row: first cell contains 'skorygowan'
            if 'skorygowan' in first and len(cells) > 1 and cells[1] not in ('-', '', '–'):
                corrected_mwh = _parse_pln_mwh(cells[1])
                continue

        # Commit last month in table
        if current_month is not None and base_mwh is not None:
            _store(year, current_month, base_mwh, corrected_mwh)

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
    corrections_path: Path = DEFAULT_CORRECTIONS_PATH,
    on_update=None,   # called (no args) once if any price was new or corrected
    on_success=None,  # legacy alias; ignored when on_update is provided
) -> bool:
    """
    Fetch all months from PSE. Update rcem_history.json and backfill historic.json
    for any month whose price is new or has changed (skorygowana correction).
    Records price change history to rcem_corrections.json.
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
        today_str = str(today)
        corrections = _load_corrections(corrections_path)
        for key in updated:
            old_price = history.get(key)
            new_price = scraped[key]
            new_entry = {'price': new_price, 'recorded_at': today_str}
            if key not in corrections:
                if old_price is not None:
                    corrections[key] = [
                        {'price': old_price, 'recorded_at': 'original'},
                        new_entry,
                    ]
                else:
                    corrections[key] = [new_entry]
            else:
                corrections[key].append(new_entry)
        _save_corrections(corrections, corrections_path)

        for key in updated:
            history[key] = scraped[key]
        _save_history(history, history_path)
        for key in updated:
            year, month = int(key[:4]), int(key[5:])
            historic_store.backfill_rcem(year, month, scraped[key], historic_json_path)
            logger.info('Backfilled historic.json for %s', key)
        if callback:
            callback()

    return target_month in history
