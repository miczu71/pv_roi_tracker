"""Tests for main.py's pure, module-level helpers.

main() itself wires together apscheduler/MQTT/Flask/HA and isn't unit-tested
directly (no test_main.py existed before this file) — but the decision logic
for the v0.31.0 missed-month-close catch-up (previous_month / month_present)
is deliberately kept as plain module-level functions so it can be tested in
isolation, without booting the rest of the add-on.
"""
from datetime import date

from pv_roi_tracker.main import previous_month, month_present
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
