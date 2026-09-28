"""
Unit tests for payment_reminders.py — pure decision logic (no I/O), and the
state load/save helpers (mirroring _load_ebok_state/_save_ebok_state in
main.py, see docs/ROADMAP_EBOK_PAYMENT_STATUS.md Etap 1).
"""
from __future__ import annotations

from datetime import date

from pv_roi_tracker.payment_reminders import (
    build_reminder_notifications,
    decide_reminders,
    load_payment_state,
    save_payment_state,
    select_active_payment,
    snapshot_documents,
)


class _Doc:
    """Minimal stand-in for EbokDocument (only the attrs decide_reminders needs)."""
    def __init__(self, signature, due_date, paid, amount_gross_pln=None):
        self.signature = signature
        self.due_date = due_date
        self.paid = paid
        self.amount_gross_pln = amount_gross_pln


def test_paid_invoice_gets_no_reminders():
    reminders = decide_reminders('SIG/1', due_date=date(2026, 10, 5), paid=True,
                                  today=date(2026, 10, 4), state={})
    assert reminders == []


def test_due_tomorrow_unpaid_gets_before_reminder():
    reminders = decide_reminders('SIG/1', due_date=date(2026, 10, 5), paid=False,
                                  today=date(2026, 10, 4), state={})
    assert reminders == ['before']


def test_due_today_unpaid_gets_due_reminder():
    reminders = decide_reminders('SIG/1', due_date=date(2026, 10, 5), paid=False,
                                  today=date(2026, 10, 5), state={})
    assert reminders == ['due']


def test_due_in_a_week_unpaid_gets_no_reminder_yet():
    reminders = decide_reminders('SIG/1', due_date=date(2026, 10, 12), paid=False,
                                  today=date(2026, 10, 4), state={})
    assert reminders == []


def test_overdue_unpaid_still_gets_due_reminder():
    reminders = decide_reminders('SIG/1', due_date=date(2026, 10, 1), paid=False,
                                  today=date(2026, 10, 5), state={})
    assert reminders == ['due']


def test_before_reminder_not_repeated_once_sent():
    state = {'SIG/1': {'reminded_before': True, 'reminded_due': False}}
    reminders = decide_reminders('SIG/1', due_date=date(2026, 10, 5), paid=False,
                                  today=date(2026, 10, 4), state=state)
    assert reminders == []


def test_due_reminder_not_repeated_once_sent():
    state = {'SIG/1': {'reminded_before': True, 'reminded_due': True}}
    reminders = decide_reminders('SIG/1', due_date=date(2026, 10, 1), paid=False,
                                  today=date(2026, 10, 5), state=state)
    assert reminders == []


def test_paid_in_the_meantime_after_before_reminder_sent_gets_no_due_reminder():
    state = {'SIG/1': {'reminded_before': True, 'reminded_due': False}}
    reminders = decide_reminders('SIG/1', due_date=date(2026, 10, 5), paid=True,
                                  today=date(2026, 10, 5), state=state)
    assert reminders == []


def test_missing_due_date_gets_no_reminders():
    reminders = decide_reminders('SIG/1', due_date=None, paid=False,
                                  today=date(2026, 10, 5), state={})
    assert reminders == []


def test_load_payment_state_missing_file_returns_empty_dict(tmp_path):
    missing = tmp_path / 'ebok_payment_state.json'
    assert load_payment_state(missing) == {}


def test_save_then_load_payment_state_roundtrips(tmp_path):
    path = tmp_path / 'ebok_payment_state.json'
    state = {'SIG/1': {'reminded_before': True, 'reminded_due': False}}
    save_payment_state(path, state)
    assert load_payment_state(path) == state


def test_load_payment_state_corrupt_file_returns_empty_dict(tmp_path):
    path = tmp_path / 'ebok_payment_state.json'
    path.write_text('not json')
    assert load_payment_state(path) == {}


# ── build_reminder_notifications() ──────────────────────────────────────────

def test_build_reminder_notifications_empty_docs_returns_nothing():
    notifications, new_state = build_reminder_notifications([], {}, date(2026, 10, 4))
    assert notifications == []
    assert new_state == {}


def test_build_reminder_notifications_due_tomorrow_sends_before_and_marks_state():
    docs = [_Doc('SIG/1', date(2026, 10, 5), paid=False)]
    notifications, new_state = build_reminder_notifications(docs, {}, date(2026, 10, 4))
    assert len(notifications) == 1
    assert notifications[0]['signature'] == 'SIG/1'
    assert notifications[0]['kind'] == 'before'
    assert 'SIG/1' in notifications[0]['message']
    assert new_state == {'SIG/1': {'reminded_before': True, 'reminded_due': False}}


def test_build_reminder_notifications_paid_invoice_produces_nothing():
    docs = [_Doc('SIG/1', date(2026, 10, 5), paid=True)]
    notifications, new_state = build_reminder_notifications(docs, {}, date(2026, 10, 4))
    assert notifications == []
    assert new_state == {}


def test_build_reminder_notifications_does_not_mutate_input_state():
    original_state = {'SIG/1': {'reminded_before': True, 'reminded_due': False}}
    docs = [_Doc('SIG/1', date(2026, 10, 5), paid=False)]
    build_reminder_notifications(docs, original_state, date(2026, 10, 5))
    assert original_state == {'SIG/1': {'reminded_before': True, 'reminded_due': False}}


def test_build_reminder_notifications_overdue_after_before_already_sent_adds_due():
    state = {'SIG/1': {'reminded_before': True, 'reminded_due': False}}
    docs = [_Doc('SIG/1', date(2026, 10, 1), paid=False)]
    notifications, new_state = build_reminder_notifications(docs, state, date(2026, 10, 5))
    assert [n['kind'] for n in notifications] == ['due']
    assert new_state == {'SIG/1': {'reminded_before': True, 'reminded_due': True}}


def test_build_reminder_notifications_preserves_unrelated_existing_state_entries():
    state = {'OTHER/SIG': {'reminded_before': True, 'reminded_due': True}}
    docs = [_Doc('SIG/1', date(2026, 10, 5), paid=False)]
    _, new_state = build_reminder_notifications(docs, state, date(2026, 10, 4))
    assert new_state['OTHER/SIG'] == {'reminded_before': True, 'reminded_due': True}
    assert new_state['SIG/1'] == {'reminded_before': True, 'reminded_due': False}


# ── snapshot_documents() ────────────────────────────────────────────────────

def test_snapshot_documents_includes_docs_with_due_date():
    docs = [_Doc('SIG/1', date(2026, 10, 5), paid=False, amount_gross_pln=63.3)]
    snap = snapshot_documents(docs)
    assert snap == {'SIG/1': {'due_date': '2026-10-05', 'paid': False, 'amount_gross_pln': 63.3}}


def test_snapshot_documents_skips_docs_without_due_date():
    docs = [_Doc('NOTA/1', None, paid=False, amount_gross_pln=50.0)]
    snap = snapshot_documents(docs)
    assert snap == {}


def test_snapshot_documents_multiple_docs():
    docs = [
        _Doc('SIG/1', date(2026, 10, 5), paid=False, amount_gross_pln=63.3),
        _Doc('SIG/2', date(2026, 11, 1), paid=True, amount_gross_pln=80.0),
    ]
    snap = snapshot_documents(docs)
    assert set(snap.keys()) == {'SIG/1', 'SIG/2'}
    assert snap['SIG/2']['paid'] is True


# ── select_active_payment() ─────────────────────────────────────────────────

def test_select_active_payment_no_state_returns_none():
    assert select_active_payment({}, date(2026, 10, 4)) is None


def test_select_active_payment_all_paid_returns_none():
    state = {'SIG/1': {'due_date': '2026-10-05', 'paid': True, 'amount_gross_pln': 63.3}}
    assert select_active_payment(state, date(2026, 10, 4)) is None


def test_select_active_payment_picks_soonest_unpaid():
    state = {
        'SIG/LATER': {'due_date': '2026-11-01', 'paid': False, 'amount_gross_pln': 80.0},
        'SIG/SOON': {'due_date': '2026-10-05', 'paid': False, 'amount_gross_pln': 63.3},
    }
    active = select_active_payment(state, date(2026, 10, 4))
    assert active['signature'] == 'SIG/SOON'
    assert active['amount_gross_pln'] == 63.3
    assert active['days_until_due'] == 1
    assert active['urgency'] == 'warning'


def test_select_active_payment_urgency_neutral_when_due_later():
    state = {'SIG/1': {'due_date': '2026-10-12', 'paid': False, 'amount_gross_pln': 63.3}}
    active = select_active_payment(state, date(2026, 10, 4))
    assert active['urgency'] == 'neutral'
    assert active['days_until_due'] == 8


def test_select_active_payment_urgency_alarm_when_due_today():
    state = {'SIG/1': {'due_date': '2026-10-04', 'paid': False, 'amount_gross_pln': 63.3}}
    active = select_active_payment(state, date(2026, 10, 4))
    assert active['urgency'] == 'alarm'
    assert active['days_until_due'] == 0


def test_select_active_payment_urgency_alarm_when_overdue():
    state = {'SIG/1': {'due_date': '2026-10-01', 'paid': False, 'amount_gross_pln': 63.3}}
    active = select_active_payment(state, date(2026, 10, 4))
    assert active['urgency'] == 'alarm'
    assert active['days_until_due'] == -3


def test_select_active_payment_ignores_entries_without_due_date():
    state = {'SIG/1': {'reminded_before': True, 'reminded_due': False}}
    assert select_active_payment(state, date(2026, 10, 4)) is None
