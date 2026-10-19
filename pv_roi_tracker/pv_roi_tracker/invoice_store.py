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
import shutil
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .invoice_parser import InvoiceData

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path('/data/invoices.json')
PDF_DIR_NAME = 'pdfs'

# Fields captured in the pre-reconcile snapshot (used for revert on Remove)
_SNAPSHOT_FIELDS = (
    'purchased_kwh', 'purchased_kwh_peak', 'purchased_kwh_offpeak',
    'exported_kwh', 'buy_price_pln_kwh', 'self_consumed_kwh',
    'self_consumed_savings_pln', 'feedin_revenue_pln', 'purchase_cost_pln',
)


def _warnings_need_training(warnings: list) -> bool:
    """True when at least one warning reports a field the parser could not
    find at all — as opposed to a plausibility check on a value it DID find
    ('walidacja: …' — e.g. avg_price looking too high in a low-import summer
    month, not a parse failure) or a field it defaulted rather than left
    missing ('… (użyto 0)'). See docs/AUDIT_2026_08_10.md: an earlier version
    of upsert() always set needs_training=False regardless of warnings, so
    invoices with real gaps (five missing fixed-fee components on 2024-01,
    understating cost_breakdown for that month) never surfaced for layout
    training."""
    for w in warnings:
        if 'użyto' in w:
            continue
        if 'nie znalezion' in w or 'nieznaleziona' in w or 'nieobliczon' in w:
            return True
    return False


# ── Internal helpers ─────────────────────────────────────────────────────────

def _atomic_write(path: Path, doc: dict) -> None:
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.rename(path)


def _load_document(path: Path) -> dict:
    """Load the JSON document, falling back to .bak on parse error OR if missing
    (see historic_store._load_document for why the missing case matters)."""
    if not path.exists():
        bak = path.with_suffix('.json.bak')
        if bak.exists():
            logger.error('%s missing but .bak present — loading .bak', path)
            try:
                return json.loads(bak.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, OSError):
                logger.exception('%s.bak also unreadable — starting empty', path)
        return {'schema_version': 2, 'invoices': {}}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as exc:
        bak = path.with_suffix('.json.bak')
        if bak.exists():
            logger.error('invoices.json corrupt (%s); loading .bak', exc)
            return json.loads(bak.read_text(encoding='utf-8'))
        raise


def _save_document(doc: dict, path: Path) -> None:
    # Back up via copy (not rename) so `path` never disappears mid-write —
    # see historic_store._save_document for the rationale.
    if path.exists():
        shutil.copy2(path, path.with_suffix('.json.bak'))
    _atomic_write(path, doc)


# ── Original PDF persistence ───────────────────────────────────────────────
# Stored alongside invoices.json under a sibling "pdfs/" directory so the
# original upload can always be re-served or re-parsed (e.g. after a parser
# improvement), without asking the user to find and re-upload the file.

def pdf_dir(path: Path = DEFAULT_PATH) -> Path:
    return path.parent / PDF_DIR_NAME


def pdf_path_for(key: str, path: Path = DEFAULT_PATH) -> Path:
    safe_key = re.sub(r'[^a-zA-Z0-9_\-]', '_', key)
    return pdf_dir(path) / f'{safe_key}.pdf'


def save_pdf(key: str, pdf_bytes: bytes, path: Path = DEFAULT_PATH) -> str:
    """Persist the original uploaded PDF for `key`. Returns the path string
    (stored on the invoice record so the UI can offer download/reparse)."""
    target = pdf_path_for(key, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(pdf_bytes)
    return str(target)


def load_pdf(key: str, path: Path = DEFAULT_PATH) -> Optional[bytes]:
    target = pdf_path_for(key, path)
    if target.exists():
        return target.read_bytes()
    return None


def delete_pdf(key: str, path: Path = DEFAULT_PATH) -> None:
    target = pdf_path_for(key, path)
    if target.exists():
        try:
            target.unlink()
        except OSError:
            logger.warning('Could not delete stored PDF for %s', key)


# ── Stored invoice record ─────────────────────────────────────────────────────

def _to_record(
    data: InvoiceData,
    filename: str,
    reconciled: bool,
    pre_reconcile: Optional[dict] = None,
    raw_text: Optional[str] = None,
    pdf_path: Optional[str] = None,
) -> dict:
    d = asdict(data)
    d['parsed_at'] = datetime.now(timezone.utc).isoformat()
    d['filename'] = filename
    d['reconciled'] = reconciled
    d['needs_training'] = _warnings_need_training(data.warnings)
    if pre_reconcile:
        d['pre_reconcile'] = pre_reconcile
    # Only store raw_text when the parse had warnings (saves space for clean parses)
    if raw_text and data.warnings:
        d['raw_text'] = raw_text
    if pdf_path:
        d['pdf_path'] = pdf_path
    return d


# ── Public API ────────────────────────────────────────────────────────────────

def _doc_signature(filename: str) -> str:
    """Znormalizowana sygnatura dokumentu z nazwy pliku: 'ebok_T-K1-0123456-24.pdf' i
    'T_K1_0123456_24.pdf' dają to samo. Pusta, gdy nazwa nie wygląda na sygnaturę
    dokumentu (np. 'scan.pdf') — wtedy nie wolno jej używać do klucza ani do scalania."""
    from .ebok_client import normalize_signature
    stem = re.sub(r'\.pdf$', '', filename or '', flags=re.I)
    if stem.startswith('ebok_'):
        stem = stem[len('ebok_'):]
    sig = normalize_signature(stem)
    return sig if re.search(r'\d{4,}', sig) else ''


def _make_key(data: InvoiceData, filename: str = '') -> str:
    """Build the storage key for an InvoiceData record.

    Billing invoices:  'YYYY-MM'              (unchanged, backward-compatible)
    Korekty:           'YYYY-MM~kor~<suffix>'  (suffix = safe invoice_number tail)
    Noty:              'YYYY-MM~nota~<suffix>'

    The suffix is derived from the invoice_number so that re-uploading the
    same document overwrites the same key (idempotent upsert / dedup). Gdy parser
    nie odczytał numeru (starsze układy PDF), sufiksem jest sygnatura z nazwy pliku;
    timestamp tylko ostatecznie — inaczej każdy ponowny import dokładał kopię
    (19.10.2026) i klucz timestampowy mógł wygrać wybór „ostatniej” korekty.
    """
    base = f'{data.year}-{data.month:02d}'
    doc_type = getattr(data, 'doc_type', 'rozliczeniowa')
    if doc_type in ('korekta', 'nota'):
        tag = 'kor' if doc_type == 'korekta' else 'nota'
        inv_num = data.invoice_number or ''
        # Keep only alphanumeric and slash; replace the rest; take last 30 chars
        safe = (re.sub(r'[^a-zA-Z0-9/]', '_', inv_num)[-30:]
                or _doc_signature(filename)[-30:]
                or str(int(time.time())))
        return f'{base}~{tag}~{safe}'
    return base


def upsert(
    data: InvoiceData,
    filename: str = '',
    reconciled: bool = False,
    pre_reconcile: Optional[dict] = None,
    raw_text: Optional[str] = None,
    pdf_bytes: Optional[bytes] = None,
    path: Path = DEFAULT_PATH,
) -> str:
    """Store or overwrite the invoice record for data.year / data.month. Returns the key.

    Key depends on doc_type:
      - 'rozliczeniowa' (default) → 'YYYY-MM' (overwrites existing billing record)
      - 'korekta'                 → 'YYYY-MM~kor~<inv_num_suffix>'
      - 'nota'                    → 'YYYY-MM~nota~<inv_num_suffix>'
    """
    key = _make_key(data, filename)
    pdf_path = save_pdf(key, pdf_bytes, path) if pdf_bytes else None
    doc = _load_document(path)
    invoices: dict = doc.setdefault('invoices', {})
    invoices[key] = _to_record(data, filename, reconciled, pre_reconcile, raw_text, pdf_path)
    _save_document(doc, path)
    logger.info('Invoice stored: %s (reconciled=%s)', key, reconciled)
    return key


def upsert_stub(
    filename: str,
    raw_text: str,
    error: str,
    path: Path = DEFAULT_PATH,
    pdf_bytes: Optional[bytes] = None,
) -> str:
    """
    Store a failed-parse PDF as a stub under a synthetic key.
    Returns the key (used by Train and Remove).
    """
    safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', filename)[:40]
    key = f'unparsed-{int(time.time())}-{safe_name}'
    rec = {
        'filename': filename,
        'parse_error': error,
        'raw_text': raw_text,
        'needs_training': True,
        'reconciled': False,
        'parsed_at': datetime.now(timezone.utc).isoformat(),
    }
    if pdf_bytes:
        rec['pdf_path'] = save_pdf(key, pdf_bytes, path)
    doc = _load_document(path)
    doc.setdefault('invoices', {})[key] = rec
    _save_document(doc, path)
    logger.info('Invoice stub stored: %s (parse failed: %s)', key, error[:80])
    return key


def remove(key: str, path: Path = DEFAULT_PATH) -> Optional[dict]:
    """
    Delete an invoice record by key. Returns the removed record (for snapshot revert),
    or None if the key was not found. Also deletes the stored PDF, if any.
    """
    doc = _load_document(path)
    record = doc.get('invoices', {}).pop(key, None)
    if record is not None:
        _save_document(doc, path)
        delete_pdf(key, path)
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


def filter_real(stored: dict) -> dict:
    """Exclude failed-parse stubs from an already-loaded invoices dict.

    Stub keys ("unparsed-<epoch>-<name>") sort lexicographically *after*
    every real "YYYY-MM" key, so any caller taking max() over keys to find
    the latest invoice must filter stubs first — otherwise a stub (with none
    of the rate/amount fields populated) silently wins.
    """
    return {k: v for k, v in stored.items() if not k.startswith('unparsed-')}


def load_real(path: Path = DEFAULT_PATH) -> dict[str, dict]:
    """load() + filter_real() in one call, for callers that only care about
    successfully parsed invoices (e.g. picking the latest by billing month)."""
    return filter_real(load(path))


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


def filter_billing(stored: dict) -> dict:
    """Return only billing invoice records — strips stubs, korekty, and noty.

    Use this (instead of or after filter_real) for 'source of truth' consumers
    such as MQTT rate sensors, tariff drift, and cost-breakdown: those must only
    see rozliczeniowa records, never corrections (which share the same rates but
    would corrupt key-based max() logic) or notas (which carry no rates at all).
    """
    return {
        k: v for k, v in stored.items()
        if not k.startswith('unparsed-')
        and '~kor~' not in k
        and '~nota~' not in k
    }


def corrections_for(stored: dict, month_key: str) -> list:
    """Return all correction/nota records associated with a billing month, sorted by key.

    Each returned dict is the raw stored record with its 'key' inserted.
    """
    result = []
    for k in sorted(stored):
        if k.startswith(f'{month_key}~kor~') or k.startswith(f'{month_key}~nota~'):
            result.append({'key': k, **stored[k]})
    return result


def _latest_correction_key(stored: dict, kor_keys: list) -> str:
    """Klucz korekty obowiązującej: ta, której nie zastąpiła żadna późniejsza w łańcuchu
    (`corrects_number` → `invoice_number`, albo `prev_deposit_previous_pln` późniejszej =
    `deposit_previous_pln` wcześniejszej). Jednoznaczny wynik wygrywa; przy braku łańcucha
    lub wielu „głowach” zostaje dawna reguła — ostatni klucz (sortowanie po kluczu nie
    jest chronologią: klucze timestampowe sortują się przed numerami z literą)."""
    recs = {k: stored[k] for k in kor_keys}

    def superseded(a: dict, b: dict) -> bool:
        """b zastępuje a."""
        if a.get('invoice_number') and b.get('corrects_number') == a['invoice_number']:
            return True
        pb, da, db = (b.get('prev_deposit_previous_pln'), a.get('deposit_previous_pln'),
                      b.get('deposit_previous_pln'))
        return (pb is not None and da is not None and db is not None
                and abs(pb - da) < 0.005 and abs(db - da) >= 0.005)

    heads = [k for k, a in recs.items()
             if not any(superseded(a, b) for k2, b in recs.items() if k2 != k)]
    return heads[0] if len(heads) == 1 else kor_keys[-1]


def effective_by_month(stored: dict) -> dict:
    """Return {YYYY-MM: record} with each billing month's deposit values overlaid
    from the latest korekta for that month (if any).

    This is the correct input for deposit.calculate() so the FIFO ledger sees
    corrected deposit values without needing to know about korektas explicitly.
    Corrections are keyed under '~kor~' so deposit.calculate() never sees them
    directly — this function produces a clean 'YYYY-MM' → record view.
    """
    from . import invoice_store as _self  # avoid circular at module level

    billing = filter_billing(filter_real(stored))
    result = {}
    for month_key, rec in billing.items():
        merged = dict(rec)  # shallow copy — safe since we only overlay scalar fields
        # Find all korrektas for this month; take the latest by key sort (suffix = invoice_num)
        kor_keys = sorted(k for k in stored if k.startswith(f'{month_key}~kor~'))
        if kor_keys:
            latest_kor = stored[_latest_correction_key(stored, kor_keys)]
            for field_name in ('deposit_current_pln', 'deposit_previous_pln', 'deposit_used_pln',
                               'energy_sale_gross_pln', 'deposit_capped'):
                val = latest_kor.get(field_name)
                if val is not None:
                    merged[field_name] = val
            # Also propagate the corrected month total so downstream callers stay consistent
            if latest_kor.get('amount_due_pln') is not None:
                merged['amount_due_pln'] = latest_kor['amount_due_pln']
        result[month_key] = merged
    return result


_DEDUPE_FIELDS = ('invoice_number', 'doc_type', 'corrects_number', 'amount_due_pln',
                  'deposit_current_pln', 'deposit_previous_pln', 'deposit_used_pln',
                  'prev_deposit_previous_pln', 'energy_sale_gross_pln')
_DEDUPE_MIN_COMPARED = 3        # tyle pól musi być niepustych po obu stronach, żeby uznać zgodność
_DEDUPE_MAX_REMOVALS = 20       # bezpiecznik: więcej usunięć naraz = podejrzany warunek scalania


def _same_document(a: dict, b: dict) -> bool:
    """Kopie tego samego dokumentu: żadne z pól kluczowych nie jest sprzeczne (obie wartości
    niepuste i różne) i co najmniej `_DEDUPE_MIN_COMPARED` pól da się porównać. Brak wartości
    po jednej stronie to nie konflikt — kopia z wcześniejszej wersji parsera mogła jeszcze
    nie wypełniać części pól — ale rekord z samymi pustymi polami nie scala się z niczym."""
    compared = 0
    for f in _DEDUPE_FIELDS:
        x, y = a.get(f), b.get(f)
        if x in (None, '') or y in (None, ''):
            continue
        if x != y:
            return False
        compared += 1
    return compared >= _DEDUPE_MIN_COMPARED


def dedupe_corrections(path: Path = DEFAULT_PATH) -> dict:
    """Jednorazowa migracja (znacznik `migrations`: `dedupe_corrections_v2`): scala kopie tej samej
    korekty/noty, które powstały, bo klucze bez numeru faktury były timestampowe.
    Kopie = ten sam miesiąc i rodzaj, ta sama sygnatura z nazwy pliku (musi wyglądać jak
    sygnatura dokumentu) ORAZ wzajemnie zgodne pola liczbowe (każda z każdą, nie tylko z
    zachowywanym). Zostaje rekord o największym stanie (numer faktury, reconciled, PDF,
    najnowszy `parsed_at`), przenoszony pod klucz kanoniczny, żeby kolejny import tego
    dokumentu go nadpisał; luki w polach kluczowych uzupełniane z usuwanych kopii, a PDF
    usuwanej kopii przenoszony, gdy zachowywany go nie ma. Przed zmianą kopia
    `<plik>.pre-dedupe-v2` (nie nadpisuje kopii z v1); PDF-y ruszane dopiero po udanym
    zapisie. Więcej niż `_DEDUPE_MAX_REMOVALS` usunięć naraz przerywa migrację bez zmian.
    Zwraca {'removed': [klucze], 'skipped': bool}."""
    doc = _load_document(path)
    migrations = doc.setdefault('migrations', [])
    if 'dedupe_corrections_v2' in migrations:
        return {'removed': [], 'skipped': True}
    invoices: dict = doc.setdefault('invoices', {})

    groups: dict = {}
    for key, rec in invoices.items():
        if '~kor~' not in key and '~nota~' not in key:
            continue
        sig = _doc_signature(rec.get('filename', ''))
        if sig:
            groups.setdefault((key.split('~', 1)[0], '~kor~' in key, sig), []).append(key)

    def rank(k):
        r = invoices[k]
        return (bool(r.get('invoice_number')), bool(r.get('reconciled')),
                bool(r.get('pdf_path')), r.get('parsed_at') or '', k)

    plan = []        # (keep, [dupes], final_key)
    for (month, is_kor, sig), keys in groups.items():
        ordered = sorted(keys, key=rank, reverse=True)
        keep, dupes = ordered[0], []
        for k in ordered[1:]:
            if (_same_document(invoices[keep], invoices[k])
                    and all(_same_document(invoices[d], invoices[k]) for d in dupes)):
                dupes.append(k)
        if not dupes:
            continue
        final = keep
        if not invoices[keep].get('invoice_number'):
            canonical = f"{month}~{'kor' if is_kor else 'nota'}~{sig[-30:]}"
            if canonical != keep and (canonical not in invoices or canonical in dupes):
                final = canonical
        plan.append((keep, dupes, final))

    removed = sorted(k for _, dupes, _ in plan for k in dupes)
    if len(removed) > _DEDUPE_MAX_REMOVALS:
        logger.error('dedupe_corrections: %d usunięć przekracza limit %d — migracja przerwana, '
                     'dane bez zmian: %s', len(removed), _DEDUPE_MAX_REMOVALS, ', '.join(removed))
        return {'removed': [], 'skipped': False, 'aborted': True}

    if not removed:
        migrations.append('dedupe_corrections_v2')
        _save_document(doc, path)
        return {'removed': [], 'skipped': False}

    logger.info('dedupe_corrections: plan — usunąć %d: %s; przenieść: %s', len(removed),
                ', '.join(removed), ', '.join(f'{k}→{f}' for k, _, f in plan if k != f))
    if path.exists():
        shutil.copy2(path, path.with_suffix('.json.pre-dedupe-v2'))

    pdf_moves = []                       # (źródło, cel) — wykonywane po zapisie JSON
    for keep, dupes, final in plan:
        rec = invoices[keep]
        for dupe in dupes:               # nie gub danych, które ma tylko usuwana kopia
            for field_name in _DEDUPE_FIELDS:
                value = invoices[dupe].get(field_name)
                if value not in (None, '') and rec.get(field_name) in (None, ''):
                    rec[field_name] = value
        target_pdf = pdf_path_for(final, path)
        source_pdf = pdf_path_for(keep, path) if rec.get('pdf_path') and pdf_path_for(keep, path).exists() else None
        if source_pdf is None:           # zachowywany bez PDF → weź PDF z najlepszej kopii
            source_pdf = next((pdf_path_for(d, path) for d in dupes
                               if invoices[d].get('pdf_path') and pdf_path_for(d, path).exists()), None)
        if source_pdf is not None:
            rec['pdf_path'] = str(target_pdf)
            if source_pdf != target_pdf:
                pdf_moves.append((source_pdf, target_pdf))
        if final != keep:
            invoices[final] = invoices.pop(keep)
    for key in removed:
        if key in invoices and not any(key == f for _, _, f in plan):
            del invoices[key]
    migrations.append('dedupe_corrections_v2')
    _save_document(doc, path)          # PDF-y ruszamy dopiero po udanym zapisie

    targets = {dst for _, dst in pdf_moves} | {pdf_path_for(f, path) for _, _, f in plan}
    for src, dst in pdf_moves:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            src.replace(dst)
        except OSError:
            logger.warning('dedupe_corrections: nie przeniesiono PDF %s', src)
    for key in removed:
        if pdf_path_for(key, path) not in targets:
            delete_pdf(key, path)
    logger.info('dedupe_corrections: usunięto %d duplikatów: %s', len(removed), ', '.join(removed))
    return {'removed': removed, 'skipped': False}
