r"""
Pivot CSV parser for the Google Sheets PV ROI export.

Layout (after the 2-row summary block):
  2023                         <- year header: first cell matches ^\d{4}$
  [empty], Jan, Feb, ..., SUMA <- month column headers (may be on same row as year,
                                  or on its own row; Polish or English abbreviations)
  wyprodukowane, v, v, ...     <- metric rows with Polish labels in col 0
  kWh/kWp, v, v, ...
  ...
  2024
  ...
"""
from __future__ import annotations

import csv
import io
import logging
import re
import unicodedata
from typing import Optional

from .models import MonthlyRecord

logger = logging.getLogger(__name__)

YEAR_RE = re.compile(r'^\d{4}$')


def _norm(s: str) -> str:
    """NFC-normalise + lowercase + strip — used for both label matching and month detection."""
    return unicodedata.normalize('NFC', s.strip()).lower()


# ── Month name lookup (Polish + English abbreviations) ────────────────────────────────────────────
MONTH_MAP: dict[str, int] = {_norm(k): v for k, v in {
    # Polish abbreviations
    'Sty': 1, 'Lut': 2, 'Mar': 3, 'Kwi': 4, 'Maj': 5, 'Cze': 6,
    'Lip': 7, 'Sie': 8, 'Wrz': 9, 'Paź': 10, 'Paz': 10, 'Lis': 11, 'Gru': 12,
    # English abbreviations
    'Jan': 1, 'Feb': 2, 'Apr': 4, 'May': 5, 'Jun': 6,
    'Jul': 7, 'Aug': 8, 'Sep': 9, 'Oct': 10, 'Nov': 11, 'Dec': 12,
    # Full Polish names (safety net)
    'Styczeń': 1, 'Luty': 2, 'Marzec': 3, 'Kwiecień': 4,
    'Czerwiec': 6, 'Lipiec': 7, 'Sierpień': 8, 'Wrzesień': 9,
    'Październik': 10, 'Listopad': 11, 'Grudzień': 12,
}.items()}

SUMA_LABELS = {'suma', 'sum', 'razem', 'total'}

# ── Polish metric label → internal field name ─────────────────────────────────────────────────
LABEL_MAP: dict[str, str] = {_norm(k): v for k, v in {
    'wyprodukowane':                'produced_kwh',
    'kWh/kWp':                      'specific_yield',
    'zużyte':                       'consumed_kwh',
    'kupione':                      'purchased_kwh',
    'autokonsumpcja':               'self_consumed_kwh',
    'autokonsumpcja oszczędności':  'self_consumed_savings_pln',
    'autokonsumpcja oszczędność':   'self_consumed_savings_pln',  # actual label in the spreadsheet
    'sprzedane':                    'exported_kwh',
    'koszt zakupu':                 'buy_price_pln_kwh',
    'suma zakupu':                  'purchase_cost_pln',
    'cena sprzedaży RCEm':          'feedin_price_pln_kwh',
    'suma sprzedaży':               'feedin_revenue_pln',
}.items()}


def _parse_float(s: str) -> Optional[float]:
    """
    Parse a number that may use Polish format with:
    - currency suffix:          '291,80 zł'
    - non-breaking space (U+00A0) as thousands separator: '4\xa0249,56 zł'
    - regular decimal comma:    '1,29 zł'
    - plain float:              '120.31'
    """
    s = s.strip()
    if not s:
        return None
    # Strip trailing currency suffix ("zł") plus any surrounding whitespace/NBSP
    s = re.sub(r'[\s\xa0]*zł[\s\xa0]*$', '', s, flags=re.IGNORECASE).strip()
    if not s:
        return None
    # Normalise NBSP thousands separator to a regular space
    s = s.replace('\xa0', ' ')
    if ',' in s and '.' in s:
        # "1.234,56" — dot is thousands sep, comma is decimal
        s = s.replace('.', '').replace(',', '.').replace(' ', '')
    elif ',' in s:
        # "4 249,56" or "291,80" — spaces are thousands sep, comma is decimal
        s = s.replace(' ', '').replace(',', '.')
    else:
        s = s.replace(' ', '')
    try:
        return float(s)
    except ValueError:
        return None


def _detect_month_columns(row: list[str]) -> Optional[dict[int, int]]:
    """
    Return {col_index: month_number} if this row looks like a month header row.
    month_number == 0 marks the SUMA column (skipped during metric summation).
    Returns None if the row does not look like a month header.
    """
    mapping: dict[int, int] = {}
    for i, cell in enumerate(row[1:], start=1):
        n = _norm(cell)
        if n in SUMA_LABELS:
            mapping[i] = 0
        elif n in MONTH_MAP:
            mapping[i] = MONTH_MAP[n]
    # Require at least 8 real month matches to avoid false positives on data rows
    if sum(1 for v in mapping.values() if v > 0) >= 8:
        return mapping
    return None


def parse_csv(csv_text: str) -> list[MonthlyRecord]:
    records: dict[tuple[int, int], dict] = {}
    current_year: Optional[int] = None
    col_to_month: dict[int, int] = {}

    reader = csv.reader(io.StringIO(csv_text))
    for row in reader:
        if not any(cell.strip() for cell in row):
            continue

        first = row[0].strip() if row[0] else ''

        # ── Year header? ──────────────────────────────────────────────────────────────────────────────
        if YEAR_RE.match(first):
            current_year = int(first)
            # Month labels may sit on the same row as the year (e.g. "2023,Jan,Feb,...")
            month_cols = _detect_month_columns(row)
            if month_cols:
                col_to_month = month_cols
            continue

        # ── Month column header row? ───────────────────────────────────────────────────────────────────
        month_cols = _detect_month_columns(row)
        if month_cols is not None:
            col_to_month = month_cols
            continue

        # ── Wait until both year and column mapping are established ───────────────────────────────────────
        if current_year is None or not col_to_month:
            continue

        # ── Metric row ─────────────────────────────────────────────────────────────────────────────
        first_norm = _norm(first)
        if not first_norm:
            continue

        internal_key = LABEL_MAP.get(first_norm)
        if internal_key is None:
            logger.warning("Unrecognised metric label in year %d: %r", current_year, first)
            continue

        for col_idx, month_num in col_to_month.items():
            if month_num == 0:  # SUMA column — never double-count
                continue
            if col_idx >= len(row):
                continue
            val = _parse_float(row[col_idx])
            if val is None:
                continue
            key = (current_year, month_num)
            if key not in records:
                records[key] = {'year': current_year, 'month': month_num, 'rcem_status': 'confirmed'}
            records[key][internal_key] = val

    result = [
        MonthlyRecord.from_dict(d)
        for d in sorted(records.values(), key=lambda r: (r['year'], r['month']))
    ]
    logger.info("Parsed %d monthly records from CSV", len(result))
    return result
