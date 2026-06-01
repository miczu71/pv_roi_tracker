"""
Persistent store for parsed Tauron invoice data.

/data/invoices.json  —  keyed by "YYYY-MM".
Mirrors the atomic-write / .bak safety pattern from historic_store.py.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .invoice_parser import InvoiceData

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path('/data/invoices.json')


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

def _to_record(data: InvoiceData, filename: str, reconciled: bool) -> dict:
    d = asdict(data)
    d['parsed_at'] = datetime.now(timezone.utc).isoformat()
    d['filename'] = filename
    d['reconciled'] = reconciled
    return d


def _from_record(d: dict) -> dict:
    """Return the raw dict (callers use it directly; no dataclass round-trip needed)."""
    return d


# ── Public API ────────────────────────────────────────────────────────────────

def upsert(
    data: InvoiceData,
    filename: str = '',
    reconciled: bool = False,
    path: Path = DEFAULT_PATH,
) -> None:
    """Store or overwrite the invoice for data.year / data.month."""
    key = f'{data.year}-{data.month:02d}'
    doc = _load_document(path)
    invoices: dict = doc.setdefault('invoices', {})
    invoices[key] = _to_record(data, filename, reconciled)
    _save_document(doc, path)
    logger.info('Invoice stored: %s (reconciled=%s)', key, reconciled)


def mark_reconciled(month_key: str, path: Path = DEFAULT_PATH) -> None:
    """Set reconciled=True for an existing entry."""
    doc = _load_document(path)
    if month_key in doc.get('invoices', {}):
        doc['invoices'][month_key]['reconciled'] = True
        _save_document(doc, path)
        logger.info('Invoice %s marked reconciled', month_key)


def load(path: Path = DEFAULT_PATH) -> dict[str, dict]:
    """Return all stored invoices as {YYYY-MM: raw_dict}."""
    return _load_document(path).get('invoices', {})


def get(month_key: str, path: Path = DEFAULT_PATH) -> Optional[dict]:
    """Return the stored invoice dict for a given YYYY-MM, or None."""
    return _load_document(path).get('invoices', {}).get(month_key)


def pending(path: Path = DEFAULT_PATH) -> list[dict]:
    """Return all unreconciled invoice records."""
    return [v for v in load(path).values() if not v.get('reconciled', False)]


def warnings_for(month_key: str, path: Path = DEFAULT_PATH) -> list:
    """Return the warnings list for a stored invoice, or [] if missing/old record."""
    rec = get(month_key, path)
    if rec is None:
        return []
    return rec.get('warnings', [])
