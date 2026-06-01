"""
Persistent store for parsed Tauron invoice data.

/data/invoices.json  —  keyed by "YYYY-MM" for successfully parsed invoices
                        or "unparsed-<epoch>-<filename>" for failed ones.
Mirrors the atomic-write / .bak safety pattern from historic_store.py.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .invoice_parser import InvoiceData

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path('/data/invoices.json')

# Fields captured in the pre-reconcile snapshot (used for revert on Remove)
_SNAPSHOT_FIELDS = (
    'purchased_kwh', 'purchased_kwh_peak', 'purchased_kwh_offpeak',
    'exported_kwh', 'buy_price_pln_kwh', 'self_consumed_kwh',
    'self_consumed_savings_pln', 'feedin_revenue_pln', 'purchase_cost_pln',
)


# ── Internal helpers ─────────────────────────────────────────────────────────

def _atomic_write(path: Path, doc: dict) -> None:
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.rename(path)


def _load_document(path: Path) -> dict:
    if not path.exists():
        return {'schema_version': 1, 'invoices': {}}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as exc:
        bak = path.with_suffix('.json.bak')
        if bak.exists():
            logger.error('invoices.json corrupt (%s); loading .bak', exc)
            return json.loads(bak.read_text(encoding='utf-8'))
        raise


def _save_document(doc: dict, path: Path) -> None:
    if path.exists():
        path.rename(path.with_suffix('.json.bak'))
    _atomic_write(path, doc)


# ── Stored invoice record ─────────────────────────────────────────────────────

def _to_record(
    data: InvoiceData,
    filename: str,
    reconciled: bool,
    pre_reconcile: Optional[dict] = None,
    raw_text: Optional[str] = None,
) -> dict:
    d = asdict(data)
    d['parsed_at'] = datetime.now(timezone.utc).isoformat()
    d['filename'] = filename
    d['reconciled'] = reconciled
    d['needs_training'] = False
    if pre_reconcile:
        d['pre_reconcile'] = pre_reconcile
    # Only store raw_text when the parse had warnings (saves space for clean parses)
    if raw_text and data.warnings:
        d['raw_text'] = raw_text
    return d


# ── Public API ────────────────────────────────────────────────────────────────

def upsert(
    data: InvoiceData,
    filename: str = '',
    reconciled: bool = False,
    pre_reconcile: Optional[dict] = None,
    raw_text: Optional[str] = None,
    path: Path = DEFAULT_PATH,
) -> str:
    """Store or overwrite the invoice for data.year / data.month. Returns the key."""
    key = f'{data.year}-{data.month:02d}'
    doc = _load_document(path)
    invoices: dict = doc.setdefault('invoices', {})
    invoices[key] = _to_record(data, filename, reconciled, pre_reconcile, raw_text)
    _save_document(doc, path)
    logger.info('Invoice stored: %s (reconciled=%s)', key, reconciled)
    return key


def upsert_stub(
    filename: str,
    raw_text: str,
    error: str,
    path: Path = DEFAULT_PATH,
) -> str:
    """
    Store a failed-parse PDF as a stub under a synthetic key.
    Returns the key (used by Train and Remove).
    """
    safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', filename)[:40]
    key = f'unparsed-{int(time.time())}-{safe_name}'
    doc = _load_document(path)
    doc.setdefault('invoices', {})[key] = {
        'filename': filename,
        'parse_error': error,
        'raw_text': raw_text,
        'needs_training': True,
        'reconciled': False,
        'parsed_at': datetime.now(timezone.utc).isoformat(),
    }
    _save_document(doc, path)
    logger.info('Invoice stub stored: %s (parse failed: %s)', key, error[:80])
    return key


def remove(key: str, path: Path = DEFAULT_PATH) -> Optional[dict]:
    """
    Delete an invoice record by key. Returns the removed record (for snapshot revert),
    or None if the key was not found.
    """
    doc = _load_document(path)
    record = doc.get('invoices', {}).pop(key, None)
    if record is not None:
        _save_document(doc, path)
        logger.info('Invoice removed: %s', key)
    return record


def mark_reconciled(month_key: str, path: Path = DEFAULT_PATH) -> None:
    """Set reconciled=True for an existing entry."""
    doc = _load_document(path)
    if month_key in doc.get('invoices', {}):
        doc['invoices'][month_key]['reconciled'] = True
        doc['invoices'][month_key]['needs_training'] = False
        _save_document(doc, path)
        logger.info('Invoice %s marked reconciled', month_key)


def load(path: Path = DEFAULT_PATH) -> dict[str, dict]:
    """Return all stored invoices as {key: raw_dict}."""
    return _load_document(path).get('invoices', {})


def get(key: str, path: Path = DEFAULT_PATH) -> Optional[dict]:
    """Return the stored invoice dict for a given key, or None."""
    return _load_document(path).get('invoices', {}).get(key)


def pending(path: Path = DEFAULT_PATH) -> list[dict]:
    """Return all unreconciled invoice records that are not stubs."""
    return [
        v for v in load(path).values()
        if not v.get('reconciled', False) and not v.get('needs_training', False)
    ]


def warnings_for(key: str, path: Path = DEFAULT_PATH) -> list:
    """Return the warnings list for a stored invoice, or [] if missing/old record."""
    rec = get(key, path)
    if rec is None:
        return []
    return rec.get('warnings', [])
