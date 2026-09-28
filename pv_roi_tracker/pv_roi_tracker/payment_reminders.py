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
            entry = new_state.setdefault(doc.signature, {'reminded_before': False, 'reminded_due': False})
            entry[f'reminded_{kind}'] = True
            notifications.append({'signature': doc.signature, 'kind': kind,
                                   'message': _reminder_message(doc, kind)})
    return notifications, new_state


def _reminder_message(doc, kind: str) -> str:
    due = doc.due_date.strftime('%d.%m.%Y')
    if kind == 'before':
        return f'Faktura {doc.signature}: termin płatności jutro ({due}), nadal niezapłacona.'
    return f'Faktura {doc.signature}: termin płatności dziś ({due}) lub już minął, nadal niezapłacona.'


def load_payment_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_payment_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False))
