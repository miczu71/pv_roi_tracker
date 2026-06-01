"""Load and save /data/historic.json with atomic writes and .bak fallback."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .models import MonthlyRecord

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path('/data/historic.json')
SCHEMA_VERSION = 1


# ── Internal helpers ─────────────────────────────────────────────────────────

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


def _mutate_month(year: int, month: int, path: Path, mutate_fn: Callable[[dict], None]) -> bool:
    """Find a month record, apply mutate_fn in place, and save. Returns False if not found."""
    doc = _load_document(path)
    for m in doc.get('months', []):
        if m['year'] == year and m['month'] == month:
            mutate_fn(m)
            _save_document(doc, path)
            return True
    return False


# ── Public API ────────────────────────────────────────────────────────────────

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


_PATCHABLE_FIELDS = {
    'produced_kwh', 'consumed_kwh', 'purchased_kwh', 'exported_kwh',
    'self_consumed_kwh', 'buy_price_pln_kwh', 'feedin_price_pln_kwh',
    'self_consumed_savings_pln', 'feedin_revenue_pln',
    'purchased_kwh_peak', 'purchased_kwh_offpeak',
    'battery_arbitrage_savings_pln',
}


def patch_month_field(
    year: int,
    month: int,
    field: str,
    value: float,
    path: Path = DEFAULT_PATH,
) -> bool:
    if field not in _PATCHABLE_FIELDS:
        raise ValueError(f"Field '{field}' is not patchable")
    found = _mutate_month(year, month, path, lambda m: m.__setitem__(field, value))
    if found:
        logger.info("Patched %d-%02d %s = %s", year, month, field, value)
    else:
        logger.warning("patch_month_field: month %d-%02d not found in %s", year, month, path)
    return found


def backfill_rcem(
    year: int,
    month: int,
    price_pln_kwh: float,
    path: Path = DEFAULT_PATH,
) -> bool:
    def _apply(m: dict) -> None:
        m['feedin_price_pln_kwh'] = price_pln_kwh
        exported = m.get('exported_kwh')
        m['feedin_revenue_pln'] = round(exported * price_pln_kwh, 2) if exported is not None else None
        m['rcem_status'] = 'confirmed'

    found = _mutate_month(year, month, path, _apply)
    if found:
        logger.info("Backfilled RCEm for %d-%02d: %.4f zł/kWh", year, month, price_pln_kwh)
    else:
        logger.warning("backfill_rcem: month %d-%02d not found in %s", year, month, path)
    return found
