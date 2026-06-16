"""Tests for historic_store.py."""
import json
import pytest
from pathlib import Path
from pv_roi_tracker.models import MonthlyRecord
from pv_roi_tracker import historic_store


def _rec(year, month, produced=100.0, exported=50.0, savings=80.0, feedin=20.0,
         status='confirmed'):
    return MonthlyRecord(
        year=year, month=month,
        produced_kwh=produced,
        exported_kwh=exported,
        self_consumed_savings_pln=savings,
        feedin_revenue_pln=feedin,
        feedin_price_pln_kwh=0.40,
        rcem_status=status,
    )


@pytest.fixture
def store(tmp_path) -> Path:
    return tmp_path / 'historic.json'


# ── Round-trip ────────────────────────────────────────────────────────────────────────────────────

def test_save_and_load(store):
    records = [_rec(2023, 5), _rec(2023, 6)]
    historic_store.save(records, store)
    loaded = historic_store.load(store)
    assert len(loaded) == 2
    assert loaded[0].year == 2023 and loaded[0].month == 5
    assert loaded[1].month == 6


def test_load_missing_file_returns_empty(store):
    assert historic_store.load(store) == []


def test_schema_version_written(store):
    historic_store.save([_rec(2023, 5)], store)
    doc = json.loads(store.read_text())
    assert doc['schema_version'] == 1


# ── Atomic write + backup ──────────────────────────────────────────────────────────────────────────────────

def test_second_save_creates_bak(store):
    historic_store.save([_rec(2023, 5)], store)
    historic_store.save([_rec(2023, 6)], store)
    assert store.with_suffix('.json.bak').exists()


def test_corrupt_file_falls_back_to_bak(store):
    historic_store.save([_rec(2023, 5)], store)
    bak = store.with_suffix('.json.bak')
    bak.write_text(store.read_text())  # valid bak
    store.write_text('{broken json{{')  # corrupt main
    loaded = historic_store.load(store)
    assert len(loaded) == 1
    assert loaded[0].month == 5


# ── append_month ───────────────────────────────────────────────────────────────────────────────────────

def test_append_month_adds_new(store):
    historic_store.save([_rec(2023, 5)], store)
    added = historic_store.append_month(_rec(2023, 6), store)
    assert added is True
    assert len(historic_store.load(store)) == 2


def test_append_month_idempotent(store):
    historic_store.save([_rec(2023, 5)], store)
    added = historic_store.append_month(_rec(2023, 5), store)
    assert added is False
    assert len(historic_store.load(store)) == 1


def test_append_month_sorted(store):
    historic_store.save([_rec(2023, 12)], store)
    historic_store.append_month(_rec(2023, 1), store)
    loaded = historic_store.load(store)
    keys = [(r.year, r.month) for r in loaded]
    assert keys == sorted(keys)


# ── backfill_rcem ──────────────────────────────────────────────────────────────────────────────────────

def test_backfill_rcem_fills_revenue(store):
    r = MonthlyRecord(year=2026, month=5, exported_kwh=200.0,
                      feedin_price_pln_kwh=None, feedin_revenue_pln=None,
                      rcem_status='pending')
    historic_store.save([r], store)
    ok = historic_store.backfill_rcem(2026, 5, 0.40, store)
    assert ok is True
    loaded = historic_store.load(store)
    assert loaded[0].feedin_price_pln_kwh == pytest.approx(0.40)
    assert loaded[0].feedin_revenue_pln == pytest.approx(80.0)  # 200 × 0.40
    assert loaded[0].rcem_status == 'confirmed'


def test_backfill_rcem_leaves_other_fields_unchanged(store):
    r = _rec(2026, 5, produced=500.0, exported=150.0, savings=300.0, feedin=None)
    r.feedin_price_pln_kwh = None
    r.feedin_revenue_pln = None
    r.rcem_status = 'pending'
    historic_store.save([r], store)
    historic_store.backfill_rcem(2026, 5, 0.42, store)
    loaded = historic_store.load(store)
    assert loaded[0].produced_kwh == pytest.approx(500.0)
    assert loaded[0].self_consumed_savings_pln == pytest.approx(300.0)
    assert loaded[0].feedin_revenue_pln == pytest.approx(63.0)  # 150 × 0.42


def test_backfill_rcem_missing_month_returns_false(store):
    historic_store.save([_rec(2023, 5)], store)
    ok = historic_store.backfill_rcem(2099, 1, 0.40, store)
    assert ok is False


# ── backfill_tariff ────────────────────────────────────────────────────────────────────────────────────

def test_backfill_tariff_tags_peak_only_month_as_g11(store):
    r = _rec(2024, 5)
    r.purchased_kwh_peak = 676.0
    r.purchased_kwh_offpeak = None
    historic_store.save([r], store)
    changed = historic_store.backfill_tariff(store)
    assert changed == 1
    assert historic_store.load(store)[0].tariff == 'G11'


def test_backfill_tariff_tags_peak_and_offpeak_month_as_g12w(store):
    r = _rec(2025, 5)
    r.purchased_kwh_peak = 505.0
    r.purchased_kwh_offpeak = 29.0
    historic_store.save([r], store)
    changed = historic_store.backfill_tariff(store)
    assert changed == 1
    assert historic_store.load(store)[0].tariff == 'G12W'


def test_backfill_tariff_leaves_already_tagged_month_unchanged(store):
    r = _rec(2024, 5)
    r.purchased_kwh_peak = 676.0
    r.purchased_kwh_offpeak = None
    r.tariff = 'G12W'  # already explicitly set — must not be overwritten
    historic_store.save([r], store)
    changed = historic_store.backfill_tariff(store)
    assert changed == 0
    assert historic_store.load(store)[0].tariff == 'G12W'


def test_backfill_tariff_skips_month_without_peak(store):
    r = _rec(2026, 1)
    r.purchased_kwh_peak = None
    historic_store.save([r], store)
    changed = historic_store.backfill_tariff(store)
    assert changed == 0
    assert historic_store.load(store)[0].tariff is None


def test_backfill_tariff_idempotent(store):
    r = _rec(2024, 5)
    r.purchased_kwh_peak = 676.0
    r.purchased_kwh_offpeak = None
    historic_store.save([r], store)
    historic_store.backfill_tariff(store)
    changed_again = historic_store.backfill_tariff(store)
    assert changed_again == 0
