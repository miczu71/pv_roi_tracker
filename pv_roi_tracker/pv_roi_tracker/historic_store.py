"""Load and save /data/historic.json with atomic writes and .bak fallback."""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .models import MonthlyRecord

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path('/data/historic.json')

# v1 → v2 (0.35.0): kWh basis moved from monthly utility_meter snapshots to
# lifetime total_increasing meters (see live_reader.py module docstring).
# No migration step needed on read — the new MonthlyRecord fields are all
# Optional with default None, so v1 documents deserialize unchanged via
# MonthlyRecord.from_dict()'s known-fields filter; they simply read as
# un-rebased until /api/historic/apply-rebase runs (see rebase.py).
SCHEMA_VERSION = 2


# ── Internal helpers ─────────────────────────────────────────────────────────

def _atomic_write(path: Path, doc: dict) -> None:
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.rename(path)


def _empty_document() -> dict:
    return {'schema_version': SCHEMA_VERSION, 'source': 'unknown',
            'system_kwp': 6.72, 'months': []}


def _load_document(path: Path) -> dict:
    """Load the JSON document, falling back to .bak on parse error OR if missing.

    A missing path with a present .bak means a crash landed between the
    backup step and the atomic rename in an older version of _save_document
    (pre-fix: rename-then-write left a window where neither file existed) —
    recover from .bak rather than silently returning an empty document.
    """
    if not path.exists():
        bak = path.with_suffix('.json.bak')
        if bak.exists():
            logger.error("%s missing but .bak present — loading .bak", path)
            try:
                return json.loads(bak.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, OSError):
                logger.exception("%s.bak also unreadable — starting empty", path)
        return _empty_document()
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as exc:
        bak = path.with_suffix('.json.bak')
        if bak.exists():
            logger.error("historic.json corrupt (%s); loading .bak", exc)
            return json.loads(bak.read_text(encoding='utf-8'))
        raise


def _save_document(doc: dict, path: Path) -> None:
    # Back up via copy (not rename) so `path` never momentarily disappears —
    # a crash between the backup and the write below must never leave
    # neither file readable. The rename in _atomic_write is the only step
    # that touches `path` itself, and POSIX rename is atomic.
    if path.exists():
        shutil.copy2(path, path.with_suffix('.json.bak'))
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


def has_energy_data(produced_kwh: Optional[float]) -> bool:
    """True if a month row carries an actual production reading, not a placeholder.

    A row can exist in historic.json with every energy field null — e.g. a
    Google Sheets pivot CSV import seeds one row per calendar month of the
    year, including months that haven't happened yet, with blank kWh cells
    parsed as None. Such a row is indistinguishable from a real zero-yield
    month by key alone, which is exactly what let append_month's old
    key-only existence check silently swallow July 2026's real snapshot
    (incident 2026-08-01): the placeholder row was already there, so the
    idempotency guard treated it as "already recorded" and skipped writing.
    """
    return produced_kwh is not None and produced_kwh > 0


def append_month(record: MonthlyRecord, path: Path = DEFAULT_PATH,
                 overwrite_empty: bool = True) -> bool:
    """Append a month record to historic.json.

    Idempotent for months that already carry real data — returns False and
    leaves the row untouched. If a row for this month already exists but is
    a data-less placeholder (see has_energy_data), overwrite_empty=True (the
    default) replaces it in place instead of silently skipping — this is the
    fix for the 2026-08-01 incident where a March-vintage CSV import had
    pre-seeded empty rows for the rest of calendar 2026, and month-close's
    skip-if-exists check dropped July's real snapshot on the floor.
    """
    doc = _load_document(path)
    months: list[dict] = doc.get('months', [])
    existing_idx = next((i for i, m in enumerate(months)
                         if (m['year'], m['month']) == record.key()), None)
    if existing_idx is not None:
        existing = months[existing_idx]
        if not (overwrite_empty and not has_energy_data(existing.get('produced_kwh'))):
            logger.info("Month %d-%02d already in historic.json — skipping", record.year, record.month)
            return False
        logger.warning(
            "Month %d-%02d already in historic.json but has no energy data (placeholder) — "
            "overwriting with real snapshot", record.year, record.month)
        new_dict = record.to_dict()
        for preserve_field in ('tariff', 'rcem_status'):
            if new_dict.get(preserve_field) is None and existing.get(preserve_field) is not None:
                new_dict[preserve_field] = existing[preserve_field]
        months[existing_idx] = new_dict
        _save_document(doc, path)
        logger.info("Overwrote placeholder %d-%02d in historic.json", record.year, record.month)
        return True
    months.append(record.to_dict())
    months.sort(key=lambda m: (m['year'], m['month']))
    doc['months'] = months
    _save_document(doc, path)
    logger.info("Appended %d-%02d to historic.json", record.year, record.month)
    return True


def prune_future_months(today, path: Path = DEFAULT_PATH) -> int:
    """Remove placeholder rows for months after `today` with no energy data.

    A future month can only ever be a real snapshot once month-close actually
    runs for it — until then any row for it is necessarily a placeholder
    (e.g. from a Google Sheets pivot CSV import that seeds a full calendar
    year at once). Left in place, such rows are invisible in the UI (the web
    layer filters to <= current month) but still block append_month's
    idempotency check the same way the 2026-08-01 incident's July row did,
    if the overwrite-empty logic above is ever bypassed. Called at startup
    as defense in depth; idempotent, no-op once a month is in the past or
    has real data.
    """
    doc = _load_document(path)
    months: list[dict] = doc.get('months', [])
    current_ym = (today.year, today.month)
    keep = []
    removed = 0
    for m in months:
        if (m['year'], m['month']) > current_ym and not has_energy_data(m.get('produced_kwh')):
            logger.warning('prune_future_months: removing empty placeholder row %d-%02d',
                           m['year'], m['month'])
            removed += 1
            continue
        keep.append(m)
    if removed:
        doc['months'] = keep
        _save_document(doc, path)
        logger.info('prune_future_months: removed %d placeholder row(s)', removed)
    return removed


def replace_month(record: MonthlyRecord, path: Path = DEFAULT_PATH) -> bool:
    """
    Nadpisz istniejący rekord miesiąca (lub dopisz jeśli brak).

    Używane do backfillu: gdy month_close zapisał zerowy rekord z powodu błędu
    strefy czasowej, replace_month pozwala go zastąpić danymi z długoterminowych
    statystyk HA. Pola `tariff` i `rcem_status` ze starego rekordu są zachowane
    jeśli nowy rekord ich nie zawiera (None).
    Returns True jeśli rekord istniał i został zastąpiony; False jeśli był nowy (append).
    """
    doc = _load_document(path)
    months: list[dict] = doc.get('months', [])
    new_dict = record.to_dict()
    for i, m in enumerate(months):
        if m['year'] == record.year and m['month'] == record.month:
            # Zachowaj tariff i rcem_status ze starego rekordu jeśli nowy nie ma lepszej wartości
            for preserve_field in ('tariff', 'rcem_status'):
                if new_dict.get(preserve_field) is None and m.get(preserve_field) is not None:
                    new_dict[preserve_field] = m[preserve_field]
            months[i] = new_dict
            _save_document(doc, path)
            logger.info('replace_month: nadpisano %d-%02d w historic.json', record.year, record.month)
            return True
    # Brak rekordu — dopisz
    months.append(new_dict)
    months.sort(key=lambda m: (m['year'], m['month']))
    doc['months'] = months
    _save_document(doc, path)
    logger.info('replace_month: dopisano %d-%02d do historic.json', record.year, record.month)
    return False


_PATCHABLE_FIELDS = {
    'produced_kwh', 'consumed_kwh', 'purchased_kwh', 'exported_kwh',
    'self_consumed_kwh', 'buy_price_pln_kwh', 'feedin_price_pln_kwh',
    'self_consumed_savings_pln', 'feedin_revenue_pln',
    'purchased_kwh_peak', 'purchased_kwh_offpeak',
    'battery_arbitrage_savings_pln', 'purchase_cost_pln',
    'battery_charge_kwh', 'battery_discharge_kwh',
}

# Fields overwritten by reconcile_invoice — captured for revert snapshots
_RECONCILE_FIELDS = (
    'purchased_kwh', 'purchased_kwh_peak', 'purchased_kwh_offpeak',
    'exported_kwh', 'buy_price_pln_kwh', 'self_consumed_kwh',
    'self_consumed_savings_pln', 'feedin_revenue_pln', 'purchase_cost_pln',
    'tariff',
)


def snapshot_month(year: int, month: int, path: Path = DEFAULT_PATH) -> Optional[dict]:
    """
    Capture the current values of the reconcilable fields for a month.
    Returns a snapshot dict (may contain None values), or None if month not found.
    """
    doc = _load_document(path)
    for m in doc.get('months', []):
        if m['year'] == year and m['month'] == month:
            return {f: m.get(f) for f in _RECONCILE_FIELDS}
    return None


def restore_month(year: int, month: int, snapshot: dict, path: Path = DEFAULT_PATH) -> bool:
    """
    Write back a pre-reconcile snapshot to a month record.
    Returns True if the month was found and restored.
    """
    def _apply(m: dict) -> None:
        for field_key, value in snapshot.items():
            m[field_key] = value
    found = _mutate_month(year, month, path, _apply)
    if found:
        logger.info('Restored pre-reconcile snapshot for %d-%02d', year, month)
    return found


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


def reconcile_invoice(
    data,   # InvoiceData — duck-typed to avoid circular import
    path: Path = DEFAULT_PATH,
) -> bool:
    """Update a closed month with billed invoice figures and recompute derived fields."""
    year, month = data.year, data.month

    def _apply(m: dict) -> None:
        m['purchased_kwh']         = data.imported_kwh
        m['purchased_kwh_peak']    = data.imported_kwh_peak
        m['purchased_kwh_offpeak'] = data.imported_kwh_offpeak
        m['tariff']                = data.tariff
        m['exported_kwh']          = data.exported_kwh
        if data.blended_gross is not None:
            m['buy_price_pln_kwh'] = data.blended_gross
        produced = m.get('produced_kwh')
        if produced is not None:
            sc = max(0.0, produced - data.exported_kwh)
            m['self_consumed_kwh'] = round(sc, 3)
            buy = m.get('buy_price_pln_kwh')
            if buy is not None:
                m['self_consumed_savings_pln'] = round(sc * buy, 2)
        rcem = m.get('feedin_price_pln_kwh')
        if rcem is not None:
            m['feedin_revenue_pln'] = round(data.exported_kwh * rcem, 2)
        buy = m.get('buy_price_pln_kwh')
        if buy is not None:
            m['purchase_cost_pln'] = round(data.imported_kwh * buy, 2)

    found = _mutate_month(year, month, path, _apply)
    if found:
        logger.info('Reconciled invoice for %d-%02d', year, month)
    else:
        logger.debug('reconcile_invoice: %d-%02d not in historic.json yet', year, month)
    return found


def _invoice_kwh_diverged(inv: dict, m: dict) -> bool:
    """Czy kWh z faktury różnią się od rekordu historic (utracona rekonsyliacja)."""
    for inv_field, hist_field in (('exported_kwh', 'exported_kwh'),
                                  ('imported_kwh', 'purchased_kwh')):
        inv_v = inv.get(inv_field)
        if inv_v is None:
            continue
        hist_v = m.get(hist_field)
        if hist_v is None or abs(inv_v - hist_v) > 0.01:
            return True
    return False


def reconcile_pending_invoices(
    invoice_path: Path,
    historic_path: Path = DEFAULT_PATH,
) -> int:
    """Apply stored invoices to historic.json at startup/month-close.

    Rekonsyliuje dwie grupy faktur ROZLICZENIOWYCH:
      • pending — reconciled=False,
      • diverged — reconciled=True, ale kWh różnią się od historic.json.
        Flaga bywa „stale": rekonsyliacja przeszła złym parsem, poprawka
        trafiła tylko do invoice store i historic nigdy się nie naprawił.
        Faktura jest źródłem prawdy dla zamkniętych miesięcy — rozjazd kWh
        nadpisuje też ręczne patche exported/purchased z /api/historic/patch.

    Korekty, noty i stuby są pomijane w OBU grupach. Noty nie niosą kWh
    (exported/imported = 0), a są składowane z reconciled=False na stałe —
    dawna pętla po invoice_store.pending() rekonsyliowała je przy każdym
    starcie i zerowała kWh miesiąca (incydent 2026-03: nota K1NBN567872/025
    nadpisywała eksport 307 kWh zerem po każdym restarcie add-onu).
    """
    from . import invoice_store
    from .invoice_parser import InvoiceData

    months_by_key = {(m.get('year'), m.get('month')): m
                     for m in _load_document(historic_path).get('months', [])}

    candidates: list[dict] = []
    for key, rec in invoice_store.load(invoice_path).items():
        if (rec.get('needs_training', False)
                or key.startswith('unparsed-') or '~' in key
                or rec.get('doc_type', 'rozliczeniowa') != 'rozliczeniowa'):
            continue   # stuby/korekty/noty nie rekonsyliują historic
        if not rec.get('reconciled', False):
            candidates.append(rec)   # pending
            continue
        m = months_by_key.get((rec.get('year'), rec.get('month')))
        if m is not None and _invoice_kwh_diverged(rec, m):
            logger.warning(
                'Invoice %s oznaczona reconciled, ale kWh rozjechane z historic '
                '(eksport %s vs %s, import %s vs %s) — ponowna rekonsyliacja',
                key, rec.get('exported_kwh'), m.get('exported_kwh'),
                rec.get('imported_kwh'), m.get('purchased_kwh'))
            candidates.append(rec)

    count = 0
    for rec in candidates:
        try:
            fields = {k: rec.get(k) for k in InvoiceData.__dataclass_fields__}
            data = InvoiceData(**fields)  # type: ignore[arg-type]
            if reconcile_invoice(data, historic_path):
                invoice_store.mark_reconciled(f'{data.year}-{data.month:02d}', invoice_path)
                count += 1
        except Exception:
            logger.exception('Failed to auto-reconcile invoice %s-%s',
                             rec.get('year'), rec.get('month'))
    if count:
        logger.info('Auto-reconciled %d invoice(s) (pending + diverged)', count)
    return count


def backfill_tariff(path: Path = DEFAULT_PATH) -> int:
    """Tag legacy reconciled months that predate the `tariff` field.

    purchased_kwh_peak / purchased_kwh_offpeak are written only by invoice
    reconciliation, so a month with purchased_kwh_peak set was definitely
    reconciled: offpeak None => single-zone G11 (całodobowa), otherwise
    two-zone G12W. Idempotent — only writes when something actually changes.
    Returns the number of months tagged.
    """
    doc = _load_document(path)
    changed = 0
    for m in doc.get('months', []):
        if m.get('tariff') is not None:
            continue
        if m.get('purchased_kwh_peak') is None:
            continue
        m['tariff'] = 'G11' if m.get('purchased_kwh_offpeak') is None else 'G12W'
        changed += 1
    if changed:
        _save_document(doc, path)
        logger.info('Backfilled tariff for %d legacy month(s)', changed)
    return changed


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
