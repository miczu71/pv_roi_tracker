"""Tests for rebase.py — dry-run simulation + apply of the v0.35.0 kWh rebase.

Uses a fake fetch_month() (no network / no HA dependency) that returns
canned MonthlyRecords, so these tests exercise the merge/diff/apply logic
in isolation.
"""
from __future__ import annotations

import json

import pytest

from pv_roi_tracker import historic_store, rebase
from pv_roi_tracker.models import MonthlyRecord


# July 2026 lifetime-meter figures, confirmed against HA (see plan doc).
_JULY_OLD = MonthlyRecord(
    year=2026, month=7, produced_kwh=862.02, exported_kwh=430.00,
    consumed_kwh=945.94, purchased_kwh=84.32, purchased_kwh_peak=4.53,
    purchased_kwh_offpeak=79.79, self_consumed_kwh=432.02,
    self_consumed_savings_pln=350.0, feedin_revenue_pln=150.0,
    buy_price_pln_kwh=0.81, tariff='G12W', rcem_status='confirmed',
    feedin_price_pln_kwh=0.35,
)


def _july_new() -> MonthlyRecord:
    return MonthlyRecord(
        year=2026, month=7, produced_kwh=867.11, exported_kwh=431.56,
        consumed_kwh=520.20, purchased_kwh=84.65, purchased_kwh_peak=4.53,
        purchased_kwh_offpeak=80.12, self_consumed_kwh=435.55,
        self_consumed_savings_pln=352.8, feedin_revenue_pln=151.0,
        buy_price_pln_kwh=0.81, self_consumed_source='measured',
        battery_charge_kwh=157.2, battery_discharge_kwh=155.57,
        balance_residual_kwh=0.02, source='lts',
    )


def _fetch_stub(reachable: dict):
    def _fetch(year, month):
        return reachable.get((year, month))
    return _fetch


# ── simulate ──────────────────────────────────────────────────────────────────

def test_simulate_diffs_consumed_kwh_correctly():
    """Reproduces the confirmed 82% consumed_kwh error for July 2026."""
    fetch = _fetch_stub({(2026, 7): _july_new()})
    report = rebase.simulate([_JULY_OLD], fetch_month=fetch)
    assert len(report['months']) == 1
    m = report['months'][0]
    assert m['ym'] == '2026-07'
    assert m['before']['consumed_kwh'] == pytest.approx(945.94)
    assert m['after']['consumed_kwh'] == pytest.approx(520.20)
    assert m['delta']['consumed_kwh'] == pytest.approx(520.20 - 945.94, abs=0.01)


def test_simulate_marks_still_broken_when_balance_does_not_close():
    """A month whose two production-tracking families diverge well beyond
    the normal drift must be flagged for investigation rather than silently
    accepted."""
    broken_new = MonthlyRecord(
        year=2026, month=7, produced_kwh=867.11, exported_kwh=431.56,
        purchased_kwh=84.65, self_consumed_kwh=435.55,
        self_consumed_source='derived', source='lts',
        cross_family_produced_kwh=1200.0,  # wildly divergent on purpose
    )
    fetch = _fetch_stub({(2026, 7): broken_new})
    report = rebase.simulate([_JULY_OLD], fetch_month=fetch)
    assert '2026-07' in report['still_broken']


def test_simulate_lists_unavailable_months():
    """No LTS data for a given month — leave it untouched and report it,
    don't silently drop it from the record set."""
    fetch = _fetch_stub({})  # nothing reachable
    report = rebase.simulate([_JULY_OLD], fetch_month=fetch)
    assert report['unavailable'] == ['2026-07']
    assert report['months'] == []


def test_simulate_skips_placeholder_rows():
    placeholder = MonthlyRecord(year=2026, month=12, produced_kwh=None)
    fetch = _fetch_stub({(2026, 7): _july_new()})
    report = rebase.simulate([_JULY_OLD, placeholder], fetch_month=fetch)
    assert len(report['months']) == 1  # only July touched
    assert report['unavailable'] == []


def test_simulate_reports_roi_before_and_after():
    fetch = _fetch_stub({(2026, 7): _july_new()})
    report = rebase.simulate([_JULY_OLD], fetch_month=fetch)
    assert 'total_produced_kwh' in report['roi_before']
    assert 'total_produced_kwh' in report['roi_after']
    # Production goes up after rebase (867.11 > 862.02).
    assert report['roi_after']['total_produced_kwh'] > report['roi_before']['total_produced_kwh']


# ── invoice-reconciled months protected ──────────────────────────────────────

def test_simulate_protects_reconciled_fields():
    """A billed month's exported/purchased/self_consumed/savings/revenue must
    survive the rebase untouched — the invoice outranks any sensor.
    produced_kwh (never billed) takes the rebuilt value. consumed_kwh is also
    never billed, but it's recomputed as self_consumed_kwh + purchased_kwh
    using the just-restored billed values (not left at the freshly-rebuilt
    LTS figure) — otherwise it would silently disagree with the very fields
    sitting next to it on the same record."""
    fetch = _fetch_stub({(2026, 7): _july_new()})
    report = rebase.simulate([_JULY_OLD], reconciled_months={(2026, 7)}, fetch_month=fetch)
    m = report['months'][0]
    assert m['after']['exported_kwh'] == pytest.approx(430.00)   # billed value preserved
    assert m['after']['purchased_kwh'] == pytest.approx(84.32)   # billed value preserved
    assert m['after']['self_consumed_kwh'] == pytest.approx(432.02)  # billed value preserved
    assert m['after']['produced_kwh'] == pytest.approx(867.11)   # rebuilt (never billed)
    assert m['after']['consumed_kwh'] == pytest.approx(432.02 + 84.32)  # recomputed from restored fields


def test_simulate_unreconciled_month_takes_full_rebuild():
    fetch = _fetch_stub({(2026, 7): _july_new()})
    report = rebase.simulate([_JULY_OLD], reconciled_months=set(), fetch_month=fetch)
    m = report['months'][0]
    assert m['after']['exported_kwh'] == pytest.approx(431.56)
    assert m['after']['self_consumed_kwh'] == pytest.approx(435.55)


# ── apply ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    return tmp_path / 'historic.json'


def test_apply_writes_rebased_records_and_snapshots_first(store):
    historic_store.save([_JULY_OLD], store)
    fetch = _fetch_stub({(2026, 7): _july_new()})

    report = rebase.apply(path=store, fetch_month=fetch)

    assert report['snapshot_path'] is not None
    snapshot = json.loads(open(report['snapshot_path']).read())
    # Snapshot holds the PRE-rebase value.
    july_snap = next(m for m in snapshot['months'] if m['month'] == 7)
    assert july_snap['consumed_kwh'] == pytest.approx(945.94)

    records = historic_store.load(store)
    july = next(r for r in records if r.month == 7)
    assert july.consumed_kwh == pytest.approx(520.20)
    assert july.produced_kwh == pytest.approx(867.11)


def test_apply_preserves_reconciled_fields_on_disk(store):
    historic_store.save([_JULY_OLD], store)
    fetch = _fetch_stub({(2026, 7): _july_new()})

    rebase.apply(path=store, reconciled_months={(2026, 7)}, fetch_month=fetch)

    records = historic_store.load(store)
    july = next(r for r in records if r.month == 7)
    assert july.exported_kwh == pytest.approx(430.00)  # billed, untouched
    assert july.produced_kwh == pytest.approx(867.11)  # rebuilt


def test_apply_leaves_unavailable_months_untouched(store):
    historic_store.save([_JULY_OLD], store)
    fetch = _fetch_stub({})  # nothing reachable

    report = rebase.apply(path=store, fetch_month=fetch)

    assert report['unavailable'] == ['2026-07']
    records = historic_store.load(store)
    july = next(r for r in records if r.month == 7)
    assert july.consumed_kwh == pytest.approx(945.94)  # unchanged


def test_apply_no_snapshot_when_no_prior_file(tmp_path):
    store = tmp_path / 'historic.json'  # never written
    fetch = _fetch_stub({})
    report = rebase.apply(path=store, fetch_month=fetch)
    assert report['snapshot_path'] is None
