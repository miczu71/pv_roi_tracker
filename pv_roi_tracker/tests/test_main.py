"""Tests for main.py's pure, module-level helpers.

main() itself wires together apscheduler/MQTT/Flask/HA and isn't unit-tested
directly (no test_main.py existed before this file) — but the decision logic
for the v0.31.0 missed-month-close catch-up (previous_month / month_present)
is deliberately kept as plain module-level functions so it can be tested in
isolation, without booting the rest of the add-on.
"""
from datetime import date

from pv_roi_tracker.main import (
    previous_month, month_present, month_has_data,
    _months_since_commissioning, _heal_month_if_needed,
)
from pv_roi_tracker.models import MonthlyRecord


# ── previous_month ────────────────────────────────────────────────────────────

def test_previous_month_normal():
    assert previous_month(date(2026, 7, 25)) == (2026, 6)


def test_previous_month_january_wraps_to_prior_december():
    assert previous_month(date(2026, 1, 15)) == (2025, 12)


def test_previous_month_february():
    assert previous_month(date(2026, 2, 1)) == (2026, 1)


# ── month_present ─────────────────────────────────────────────────────────────

def _rec(year, month):
    return MonthlyRecord(year=year, month=month, produced_kwh=500.0)


def test_month_present_true_when_found():
    records = [_rec(2026, 5), _rec(2026, 6)]
    assert month_present(records, 2026, 6) is True


def test_month_present_false_when_missing():
    """Reproduces the B3 scenario: month-close never fired for June, so the
    month is entirely absent from historic.json (not even a zero row)."""
    records = [_rec(2026, 5)]
    assert month_present(records, 2026, 6) is False


def test_month_present_false_on_empty_records():
    assert month_present([], 2026, 6) is False


def test_month_present_distinguishes_year():
    """A same-numbered month in a different year must not count as present —
    guards against a year/month field mix-up in the caller."""
    records = [_rec(2025, 6)]
    assert month_present(records, 2026, 6) is False


# ── month_has_data ────────────────────────────────────────────────────────────

def _placeholder(year, month):
    """A data-less row — same shape as the ones a CSV import pre-seeds for
    future calendar months. See historic_store.has_energy_data."""
    return MonthlyRecord(year=year, month=month, produced_kwh=None)


def test_month_has_data_true_for_real_record():
    records = [_rec(2026, 7)]
    assert month_has_data(records, 2026, 7) is True


def test_month_has_data_false_for_placeholder():
    """Reproduces the 2026-08-01 incident: month_present() would say True here
    (the row exists), but month_has_data() must say False — this is exactly
    the distinction that should have triggered the startup catch-up backfill
    for July 2026 instead of silently accepting the empty row."""
    records = [_placeholder(2026, 7)]
    assert month_present(records, 2026, 7) is True
    assert month_has_data(records, 2026, 7) is False


def test_month_has_data_false_when_missing_entirely():
    assert month_has_data([], 2026, 7) is False


def test_month_has_data_false_for_zero_production():
    records = [MonthlyRecord(year=2026, month=7, produced_kwh=0.0)]
    assert month_has_data(records, 2026, 7) is False


# ── _months_since_commissioning (v0.35.0 multi-month healer scan) ────────────

def test_months_since_commissioning_spans_full_range():
    records = [_rec(2026, 3), _rec(2026, 4), _rec(2026, 5)]
    months = _months_since_commissioning(records, date(2026, 7, 15))
    assert months == [(2026, 3), (2026, 4), (2026, 5), (2026, 6)]


def test_months_since_commissioning_wraps_year_boundary():
    records = [_rec(2025, 11)]
    months = _months_since_commissioning(records, date(2026, 2, 1))
    assert months == [(2025, 11), (2025, 12), (2026, 1)]


def test_months_since_commissioning_ignores_placeholders():
    """A future data-less placeholder must not push the scan window forward."""
    records = [_rec(2026, 5), _placeholder(2026, 12)]
    months = _months_since_commissioning(records, date(2026, 7, 1))
    assert months == [(2026, 5), (2026, 6)]


def test_months_since_commissioning_empty_when_no_data():
    assert _months_since_commissioning([_placeholder(2026, 1)], date(2026, 7, 1)) == []


# ── _heal_month_if_needed ─────────────────────────────────────────────────────

def _balanced_rec(year, month):
    """March 2026 figures — dashboard-family produced within normal
    cross-family drift of the template-family figure (see balance.py)."""
    return MonthlyRecord(year=year, month=month, produced_kwh=643.98, exported_kwh=307.0,
                         self_consumed_kwh=336.98, consumed_kwh=670.88, purchased_kwh=35.0,
                         cross_family_produced_kwh=685.53)


def test_heal_month_if_needed_none_for_missing_key():
    assert _heal_month_if_needed({}, 2026, 7) == 'brak danych'


def test_heal_month_if_needed_none_for_placeholder():
    by_key = {(2026, 7): _placeholder(2026, 7)}
    assert _heal_month_if_needed(by_key, 2026, 7) == 'brak danych'


def test_heal_month_if_needed_none_when_balanced():
    by_key = {(2026, 7): _balanced_rec(2026, 7)}
    assert _heal_month_if_needed(by_key, 2026, 7) is None


def test_heal_month_if_needed_flags_balance_breach():
    """A month whose two production-tracking families diverge well beyond
    the normal ~0.6-6.5% drift must be flagged for repair even though the
    row isn't a data-less placeholder."""
    broken = MonthlyRecord(year=2026, month=7, produced_kwh=500.0, exported_kwh=430.00,
                           self_consumed_kwh=70.0, consumed_kwh=154.32, purchased_kwh=84.32,
                           cross_family_produced_kwh=650.0)
    by_key = {(2026, 7): broken}
    reason = _heal_month_if_needed(by_key, 2026, 7)
    assert reason is not None
    assert 'bilans' in reason
