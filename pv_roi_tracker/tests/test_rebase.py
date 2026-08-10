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


def _cross_family_stub(values: dict = None):
    values = values or {}
    def _fetch(year, month):
        return values.get((year, month))
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


# ── invoice-reconciled months frozen wholesale ───────────────────────────────

def test_simulate_freezes_reconciled_month_entirely():
    """An invoice-reconciled month is final for every BILLED field — produced,
    exported, purchased, self_consumed, prices — even though the fresh LTS
    rebuild disagrees with the old row on all of them. The fetch_month() full
    rebuild is never even called for this month; only two derived fields are
    refreshed: the cheap cross-family diagnostic, and consumed_kwh (recomputed
    as self_consumed_kwh + purchased_kwh — not billed by any invoice, so
    freezing it would only preserve whatever a buggy live sensor wrote at the
    time; see docs/AUDIT_2026_08_10.md)."""
    fetch = _fetch_stub({(2026, 7): _july_new()})
    cross_family = _cross_family_stub({(2026, 7): 900.0})
    report = rebase.simulate([_JULY_OLD], reconciled_months={(2026, 7)},
                             fetch_month=fetch, fetch_cross_family=cross_family)
    m = report['months'][0]
    assert m['frozen'] is True
    assert m['after']['exported_kwh'] == pytest.approx(430.00)
    assert m['after']['purchased_kwh'] == pytest.approx(84.32)
    assert m['after']['self_consumed_kwh'] == pytest.approx(432.02)
    # Recomputed from the frozen self_consumed_kwh + purchased_kwh above
    # (432.02 + 84.32), NOT the old row's 945.94 nor the LTS rebuild's 520.20.
    assert m['after']['consumed_kwh'] == pytest.approx(516.34)
    assert m['after']['produced_kwh'] == pytest.approx(862.02)  # untouched, NOT the rebuilt 867.11
    assert m['after']['cross_family_produced_kwh'] == pytest.approx(900.0)  # diagnostic only, refreshed


def test_simulate_unreconciled_month_takes_full_rebuild():
    fetch = _fetch_stub({(2026, 7): _july_new()})
    report = rebase.simulate([_JULY_OLD], reconciled_months=set(), fetch_month=fetch)
    m = report['months'][0]
    assert m['frozen'] is False
    assert m['after']['exported_kwh'] == pytest.approx(431.56)
    assert m['after']['self_consumed_kwh'] == pytest.approx(435.55)
    assert m['after']['produced_kwh'] == pytest.approx(867.11)


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


def test_apply_freezes_reconciled_month_on_disk(store):
    historic_store.save([_JULY_OLD], store)
    fetch = _fetch_stub({(2026, 7): _july_new()})
    cross_family = _cross_family_stub({(2026, 7): 900.0})

    rebase.apply(path=store, reconciled_months={(2026, 7)}, fetch_month=fetch,
                fetch_cross_family=cross_family)

    records = historic_store.load(store)
    july = next(r for r in records if r.month == 7)
    assert july.exported_kwh == pytest.approx(430.00)   # billed, untouched
    assert july.produced_kwh == pytest.approx(862.02)   # untouched, NOT the rebuilt 867.11
    assert july.cross_family_produced_kwh == pytest.approx(900.0)  # diagnostic refreshed
    assert july.consumed_kwh == pytest.approx(516.34)   # recomputed: self_consumed + purchased


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


# ── regression: 2026-05/06 consumed_kwh corruption (docs/AUDIT_2026_08_10.md) ─

_MAY_CORRUPTED = MonthlyRecord(
    year=2026, month=5, produced_kwh=892.8, exported_kwh=379.0,
    consumed_kwh=1020.8,  # bug: this is produced+imported (892.8+129.0-1.0
    purchased_kwh=129.0, purchased_kwh_peak=10.0, purchased_kwh_offpeak=119.0,
    self_consumed_kwh=513.8, buy_price_pln_kwh=0.6771, tariff='G12W',
    rcem_status='confirmed',
)


def test_apply_repairs_corrupted_consumed_kwh_on_reconciled_month(store):
    """Reproduces the exact production incident: a reconciled month whose
    consumed_kwh was written as produced+imported (1020.8) instead of
    self_consumed+imported (642.8) by a buggy live sensor, then frozen there
    by the pre-fix _freeze_month(). rebase.apply() must repair it even though
    the month is invoice-reconciled, because consumed_kwh is not a billed
    field — only self_consumed_kwh/purchased_kwh (both untouched here) are."""
    historic_store.save([_MAY_CORRUPTED], store)
    fetch = _fetch_stub({})  # full rebuild must never be called — reconciled

    report = rebase.apply(path=store, reconciled_months={(2026, 5)}, fetch_month=fetch)

    assert report['unavailable'] == []  # reconciled path skips fetch_month entirely
    records = historic_store.load(store)
    may = next(r for r in records if r.month == 5)
    assert may.self_consumed_kwh == pytest.approx(513.8)   # billed, untouched
    assert may.purchased_kwh == pytest.approx(129.0)       # billed, untouched
    assert may.consumed_kwh == pytest.approx(642.8)        # repaired: 513.8 + 129.0
