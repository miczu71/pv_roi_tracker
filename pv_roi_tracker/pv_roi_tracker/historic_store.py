"""Load and save /data/historic.json with atomic writes and .bak fallback."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import MonthlyRecord

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path('/data/historic.json')
SCHEMA_VERSION = 1


# ── Internal helpers ────────────────────────────────────────────────────────────────────────────────────

def _atomic_write(path: Path, doc: dict) -> None:
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.rename(path)


def _load_document(path: Path) -> dict:
    """Load the JSON document, falling back to .bak on parse error."""
    if not path.exists():
        return {'schema_version': SCHEMA_VERSION, 'source': 'unknown',
                'system_kwp': 6.72, 'months': []}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as exc:
        bak = path.with_suffix('.json.bak')
        if bak.exists():
            logger.error("historic.json corrupt (%s); loading .bak", exc)
            return json.loads(bak.read_text(encoding='utf-8'))
        raise


def _save_document(doc: dict, path: Path) -> None:
    if path.exists():
        path.rename(path.with_suffix('.json.bak'))
    _atomic_write(path, doc)


# ── Public API ───────────────────────────────────────────────────────────────────────────────────────

def load(path: Path = DEFAULT_PATH) -> list[MonthlyRecord]:
    doc = _load_document(path)
    return [MonthlyRecord.from_dict(m) for m in doc.get('months', [])]


def save(
    records: list[MonthlyRecord],
    path: Path = DEFAULT_PATH,
    source: str = 'google_sheets_csv',
    system_kwp: float = 6.72,
) -> None:
    doc = {
        'schema_version': SCHEMA_VERSION,
        'imported_at': datetime.now(timezone.utc).isoformat(),
        'source': source,
        'system_kwp': system_kwp,
        'months': [r.to_dict() for r in records],
    }
    _save_document(doc, path)
    logger.info("Saved %d records to %s", len(records), path)


def append_month(record: MonthlyRecord, path: Path = DEFAULT_PATH) -> bool:
    """Append a month record to historic.json. Idempotent — returns False if already present."""
    doc = _load_document(path)
    months: list[dict] = doc.get('months', [])
    existing_keys = {(m['year'], m['month']) for m in months}
    if record.key() in existing_keys:
        logger.info("Month %d-%02d already in historic.json — skipping", record.year, record.month)
        return False
    months.append(record.to_dict())
    months.sort(key=lambda m: (m['year'], m['month']))
    doc['months'] = months
    _save_document(doc, path)
    logger.info("Appended %d-%02d to historic.json", record.year, record.month)
    return True


def backfill_rcem(
    year: int,
    month: int,
    price_pln_kwh: float,
    path: Path = DEFAULT_PATH,
) -> bool:
    """
    Write feedin_price_pln_kwh and feedin_revenue_pln into an existing month record
    that was saved without RCEm data (rcem_status='pending').
    Only touches the three RCEm fields — all other fields are unchanged.
    Returns True on success, False if the month was not found.
    """
    doc = _load_document(path)
    months: list[dict] = doc.get('months', [])
    for m in months:
        if m['year'] == year and m['month'] == month:
            m['feedin_price_pln_kwh'] = price_pln_kwh
            exported = m.get('exported_kwh')
            m['feedin_revenue_pln'] = round(exported * price_pln_kwh, 2) if exported is not None else None
            m['rcem_status'] = 'confirmed'
            doc['months'] = months
            _save_document(doc, path)
            logger.info("Backfilled RCEm for %d-%02d: %.4f zł/kWh", year, month, price_pln_kwh)
            return True
    logger.warning("backfill_rcem: month %d-%02d not found in %s", year, month, path)
    return False
