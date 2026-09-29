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
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False))
