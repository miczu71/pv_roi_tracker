"""
Payment-due reminder logic for eBOK-imported invoices
(docs/ROADMAP_EBOK_PAYMENT_STATUS.md Etap 1).

`decide_reminders` is a pure function — no I/O, no scheduler — so it is
directly unit-testable. State persistence mirrors the existing
_load_ebok_state/_save_ebok_state pattern in main.py, kept separate from
`invoice_store` (financial reconciliation) since this is purely informational.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def decide_reminders(signature: str, due_date: Optional[date], paid: bool,
                      today: date, state: dict) -> list:
    """Which reminders ("before" / "due") should fire now for one invoice.

    - Paid or missing due_date (e.g. a nota without one) → never reminds.
    - "before": due_date is tomorrow and not yet sent for this signature.
    - "due": today is on or past due_date and not yet sent for this signature.
    Both can be returned together (e.g. after a restart skipped a day).
    """
    if paid or due_date is None:
        return []
    sent = state.get(signature, {})
    reminders = []
    if due_date - today == timedelta(days=1) and not sent.get('reminded_before'):
        reminders.append('before')
    if today >= due_date and not sent.get('reminded_due'):
        reminders.append('due')
    return reminders


def build_reminder_notifications(docs, state: dict, today: date) -> tuple:
    """Run decide_reminders() over a batch of eBOK documents (EbokDocument or
    anything with .signature/.due_date/.paid). Returns (notifications, new_state)
    without mutating `state` — the caller persists new_state via save_payment_state."""
    new_state = {sig: dict(entry) for sig, entry in state.items()}
    notifications = []
    for doc in docs:
        for kind in decide_reminders(doc.signature, doc.due_date, doc.paid, today, state):
            new_state.setdefault(doc.signature, _new_entry())[f'reminded_{kind}'] = True
            notifications.append({'signature': doc.signature, 'kind': kind,
                                   'message': _reminder_message(doc, kind)})
    return notifications, new_state


def refresh_payment_state(docs, state: dict, today: date) -> tuple:
    """One daily check: reminders to send plus the new state — reminder flags from
    build_reminder_notifications() merged with the snapshot_documents() display fields.
    Pure; the caller persists new_state via save_payment_state."""
    notifications, new_state = build_reminder_notifications(docs, state, today)
    for signature, snap in snapshot_documents(docs).items():
        new_state.setdefault(signature, _new_entry()).update(snap)
    return notifications, new_state


_refresh_lock = threading.Lock()


def apply_refresh(docs, state_path: Path, today: date) -> list:
    """Load the persisted state, run refresh_payment_state() and save it back; returns the
    reminder notifications to send. Shared by the daily payment check and by ebok_sync, so
    the status bar reflects a fresh import without waiting for the next 07:00 run."""
    with _refresh_lock:            # zadanie 07:00 i synchronizacja nie mogą się nakładać
        notifications, new_state = refresh_payment_state(docs, load_payment_state(state_path), today)
        save_payment_state(state_path, new_state)
    return notifications


_TOLERANCE = 0.10
_CREDIT_TYPES = ('Wpłata', 'Odpisanie')


def reconcile_payments(docs, payments, invoice_records) -> dict:
    """Pure: match eBOK payments (EbokPayment) against documents and imported invoices.
    `settled` = Σ settlement per signature (Wpłata/Odpisanie minus Zwrot); `rescued` = unpaid
    documents fully settled by payments; `mismatches` = paid documents whose settled sum differs
    from the invoice's amount_due_pln (only signatures that appear in any payment, so invoices
    older than the payments window never look like mismatches); `last_payment` = newest Wpłata."""
    from .ebok_client import normalize_signature as norm

    settled_n: dict = {}
    display: dict = {}
    for pay in payments:
        sign = 1.0 if pay.type in _CREDIT_TYPES else (-1.0 if pay.type == 'Zwrot' else 0.0)
        if not sign:
            continue
        for sig, _gross, amount in pay.settlements:
            key = norm(sig)
            display.setdefault(key, sig)
            settled_n[key] = settled_n.get(key, 0.0) + sign * (amount or 0.0)

    rescued, paid_keys = [], set()
    for doc in docs:
        key = norm(doc.signature)
        if doc.paid:
            paid_keys.add(key)
        elif (key in settled_n and doc.amount_gross_pln is not None and doc.amount_gross_pln > 0
              and settled_n[key] >= doc.amount_gross_pln - _TOLERANCE):
            rescued.append(doc.signature)
            paid_keys.add(key)

    mismatches, seen = [], set()
    for rec in invoice_records:
        number = rec.get('invoice_number')
        due = rec.get('amount_due_pln')
        if not number or due is None:
            continue
        key = norm(number)
        if key in seen or key not in paid_keys or key not in settled_n:
            continue
        seen.add(key)
        settled = settled_n[key]
        if abs(settled - due) > _TOLERANCE:
            mismatches.append({'signature': display[key], 'settled': round(settled, 2),
                               'amount_due': due, 'diff': round(settled - due, 2)})

    last_payment = None
    credits = [p for p in payments if p.type == 'Wpłata' and p.date is not None]
    if credits:
        last = max(credits, key=lambda p: p.date)     # remis: późniejszy na liście wygrywa
        for p in credits:
            if p.date == last.date:
                last = p
        covered = {}
        for sig, gross, _amount in last.settlements:
            if gross is not None and gross - settled_n.get(norm(sig), 0.0) > 0.01:
                covered[sig] = round(gross - settled_n.get(norm(sig), 0.0), 2)
        last_payment = {'date': last.date.isoformat(), 'amount': last.amount,
                        'signatures': [s[0] for s in last.settlements], 'deposit_covered': covered}

    return {'last_payment': last_payment, 'mismatches': mismatches, 'rescued': rescued,
            'settled': {display[k]: round(v, 2) for k, v in settled_n.items()}}


def apply_reconciliation(state_path: Path, result: dict, today: date) -> list:
    """Persist reconcile_payments() output under state['_payments'] and return push messages
    for mismatches not yet notified (notified drops signatures whose mismatch is gone)."""
    with _refresh_lock:
        state = load_payment_state(state_path)
        already = set((state.get('_payments') or {}).get('notified') or [])
        current = [m['signature'] for m in result['mismatches']]
        messages = [
            'Wpłaty: faktura {} — rozliczono {} zł, na fakturze {} zł.'.format(
                m['signature'], _pl(m['settled']), _pl(m['amount_due']))
            for m in result['mismatches'] if m['signature'] not in already]
        state['_payments'] = {'last_payment': result['last_payment'], 'mismatches': result['mismatches'],
                              'checked_at': today.isoformat(), 'notified': current}
        for sig in result['rescued']:
            if isinstance(state.get(sig), dict):
                state[sig]['paid'] = True
        save_payment_state(state_path, state)
    return messages


def _pl(value) -> str:
    return f'{value:.2f}'.replace('.', ',')


def _new_entry() -> dict:
    return {'reminded_before': False, 'reminded_due': False}


def _reminder_message(doc, kind: str) -> str:
    due = doc.due_date.strftime('%d.%m.%Y')
    if kind == 'before':
        return f'Faktura {doc.signature}: termin płatności jutro ({due}), nadal niezapłacona.'
    return f'Faktura {doc.signature}: termin płatności dziś ({due}) lub już minął, nadal niezapłacona.'


def snapshot_documents(docs) -> dict:
    """Per-signature due_date/paid/amount snapshot for display (docs/ROADMAP_EBOK_PAYMENT_STATUS.md
    Etap 3) — docs without a due_date (notes) are skipped, same as decide_reminders."""
    return {
        doc.signature: {
            'due_date': doc.due_date.isoformat(),
            'paid': doc.paid,
            'amount_gross_pln': doc.amount_gross_pln,
        }
        for doc in docs if doc.due_date is not None
    }


def select_active_payment(state: dict, today: date) -> Optional[dict]:
    """Nearest unpaid invoice with a known due_date, for the status bar. None when
    nothing needs attention (nothing unpaid, or state has no due_date snapshots yet)."""
    candidates = []
    for signature, entry in state.items():
        if entry.get('paid') or not entry.get('due_date'):
            continue
        candidates.append((date.fromisoformat(entry['due_date']), signature, entry))
    if not candidates:
        return None
    due, signature, entry = min(candidates, key=lambda c: c[0])
    days_until_due = (due - today).days
    urgency = 'alarm' if days_until_due <= 0 else ('warning' if days_until_due == 1 else 'neutral')
    return {
        'signature': signature,
        'due_date': due.isoformat(),
        'amount_gross_pln': entry.get('amount_gross_pln'),
        'days_until_due': days_until_due,
        'urgency': urgency,
    }


def load_payment_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_payment_state(path: Path, state: dict) -> None:
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    os.replace(tmp, path)          # atomowo: przerwany zapis nie zostawia uciętego JSON-a
