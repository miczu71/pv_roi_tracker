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


def test_save_does_not_delete_target_before_atomic_replace(store, monkeypatch):
    """Regression guard: a crash mid-save must never leave `store` missing.

    The old _save_document did `path.rename(.bak)` BEFORE writing the new
    file — a crash between those two steps left neither file readable, and
    the *next* load() silently returned an empty document (production
    incident: 46 months of PV history at risk). The fix backs up via copy
    (leaves the original intact) and only ever touches `store` itself via
    the atomic os.rename inside _atomic_write."""
    historic_store.save([_rec(2023, 5)], store)

    original_rename = Path.rename
    seen_missing = []

    def _spy_rename(self, target):
        # Called once, for the final tmp → store atomic replace. At the
        # instant just before this call, `store` must still exist —
        # otherwise there was a window where it didn't.
        seen_missing.append(store.exists())
        return original_rename(self, target)

    monkeypatch.setattr(Path, 'rename', _spy_rename)
    historic_store.save([_rec(2023, 6)], store)

    assert seen_missing == [True], (
        '`store` must exist right up until the atomic rename replaces it')
    assert store.with_suffix('.json.bak').exists()
    assert len(historic_store.load(store)) == 1
    assert historic_store.load(store)[0].month == 6


def test_load_missing_with_bak_present_recovers_from_bak(store):
    """Reproduces the pre-fix crash window directly: `store` doesn't exist
    but `.bak` holds a valid document — load() must recover from .bak
    instead of silently returning an empty document."""
    historic_store.save([_rec(2023, 5)], store)
    bak = store.with_suffix('.json.bak')
    bak.write_text(store.read_text())
    store.unlink()   # simulate a crash that left only the .bak behind

    loaded = historic_store.load(store)
    assert len(loaded) == 1
    assert loaded[0].month == 5


def test_load_missing_with_no_bak_returns_empty(store):
    """No file and no .bak — genuinely a fresh install, not a crash. Must
    still return an empty document (unchanged pre-fix behavior)."""
    assert historic_store.load(store) == []


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


# ── has_energy_data ────────────────────────────────────────────────────────────────────────────────────

def test_has_energy_data_true_for_positive_production():
    assert historic_store.has_energy_data(862.02) is True


def test_has_energy_data_false_for_none():
    assert historic_store.has_energy_data(None) is False


def test_has_energy_data_false_for_zero():
    assert historic_store.has_energy_data(0.0) is False


# ── append_month: overwrite-empty placeholder (incident 2026-08-01) ──────────────────────────────────────

def _placeholder(year, month):
    """A data-less row shaped like the ones a Google Sheets pivot CSV import
    seeds for months that haven't happened yet — matches the real production
    of the 2026-08-01 incident: every energy field None except a few derived
    ones the parser reads as an explicit 0.0."""
    return MonthlyRecord(
        year=year, month=month,
        self_consumed_kwh=0.0, self_consumed_savings_pln=0.0,
        purchase_cost_pln=0.0, feedin_revenue_pln=0.0,
        rcem_status='confirmed',
    )


def test_append_month_overwrites_empty_placeholder(store):
    """Reproduces the 2026-08-01 incident directly: a placeholder row for the
    month already exists (from an earlier CSV import), and month-close's
    append_month must overwrite it with the real snapshot instead of skipping."""
    historic_store.save([_placeholder(2026, 7)], store)

    appended = historic_store.append_month(_rec(2026, 7, produced=862.02, exported=430.0), store)

    assert appended is True
    loaded = historic_store.load(store)
    assert len(loaded) == 1
    assert loaded[0].produced_kwh == pytest.approx(862.02)


def test_append_month_does_not_overwrite_real_data(store):
    """A month that already has a real snapshot must stay untouched (idempotent) —
    overwrite-empty must never clobber a month that genuinely has data."""
    historic_store.save([_rec(2026, 6, produced=888.35)], store)

    appended = historic_store.append_month(_rec(2026, 6, produced=1.0), store)

    assert appended is False
    assert historic_store.load(store)[0].produced_kwh == pytest.approx(888.35)


def test_append_month_overwrite_empty_false_preserves_old_behavior(store):
    """overwrite_empty=False restores the pre-fix skip-if-exists behavior."""
    historic_store.save([_placeholder(2026, 7)], store)

    appended = historic_store.append_month(
        _rec(2026, 7, produced=862.02), store, overwrite_empty=False)

    assert appended is False
    assert historic_store.load(store)[0].produced_kwh is None


def test_append_month_overwrite_preserves_tariff_and_rcem_status(store):
    """Mirrors replace_month's field-preservation contract: if the new record
    doesn't carry tariff/rcem_status, keep whatever the placeholder already had."""
    placeholder = _placeholder(2026, 7)
    placeholder.tariff = 'G12W'
    historic_store.save([placeholder], store)

    new_rec = MonthlyRecord(year=2026, month=7, produced_kwh=862.02, rcem_status='pending')
    historic_store.append_month(new_rec, store)

    loaded = historic_store.load(store)[0]
    assert loaded.tariff == 'G12W'
    assert loaded.rcem_status == 'pending'


# ── prune_future_months ───────────────────────────────────────────────────────────────────────────────

def test_prune_future_months_removes_empty_placeholders(store):
    from datetime import date
    historic_store.save([_rec(2026, 6), _placeholder(2026, 7), _placeholder(2026, 8)], store)

    removed = historic_store.prune_future_months(date(2026, 6, 15), store)

    assert removed == 2
    loaded = historic_store.load(store)
    assert [(r.year, r.month) for r in loaded] == [(2026, 6)]


def test_prune_future_months_keeps_past_and_current(store):
    from datetime import date
    historic_store.save([_rec(2026, 5), _rec(2026, 6)], store)

    removed = historic_store.prune_future_months(date(2026, 6, 15), store)

    assert removed == 0
    assert len(historic_store.load(store)) == 2


def test_prune_future_months_keeps_future_rows_with_data(store):
    """A future row with real data (e.g. a pre-entered projection) must survive —
    pruning only targets data-less placeholders."""
    from datetime import date
    historic_store.save([_rec(2026, 6), _rec(2026, 7, produced=500.0)], store)

    removed = historic_store.prune_future_months(date(2026, 6, 15), store)

    assert removed == 0
    assert len(historic_store.load(store)) == 2


def test_prune_future_months_idempotent_when_nothing_to_remove(store):
    from datetime import date
    historic_store.save([_rec(2026, 6)], store)

    assert historic_store.prune_future_months(date(2026, 6, 15), store) == 0
    # second call: still nothing to do, no error, no spurious .bak thrash
    assert historic_store.prune_future_months(date(2026, 6, 15), store) == 0


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


# ── reconcile_pending_invoices: diverged re-reconciliation ─────────────────────────────────────────

def _invoice(year=2026, month=3, imported=343.0, exported=307.0) -> 'InvoiceData':
    from pv_roi_tracker.invoice_parser import InvoiceData
    return InvoiceData(
        year=year, month=month,
        imported_kwh=imported, exported_kwh=exported,
        imported_kwh_peak=None, imported_kwh_offpeak=None,
        exported_kwh_peak=None, exported_kwh_offpeak=None,
        energy_peak_net=None, energy_offpeak_net=None,
        dist_var_peak_net=None, dist_var_offpeak_net=None,
        dist_jakosciowa_net=None, dist_oze_net=None, dist_kogeneracja_net=None,
        fixed_mocowa_net=None, fixed_abonament_net=None, fixed_stalysieciowy_net=None,
    )


@pytest.fixture
def invoices(tmp_path) -> Path:
    return tmp_path / 'invoices.json'


def test_diverged_reconciled_invoice_is_reapplied(store, invoices):
    """reconciled=True, ale historic ma inne kWh (utracona rekonsyliacja) → re-apply."""
    from pv_roi_tracker import invoice_store
    r = _rec(2026, 3, produced=643.98, exported=0.0)  # zepsuty rekord (przypadek 2026-03)
    historic_store.save([r], store)
    invoice_store.upsert(_invoice(), reconciled=True, path=invoices)

    count = historic_store.reconcile_pending_invoices(invoices, store)

    assert count == 1
    loaded = historic_store.load(store)[0]
    assert loaded.exported_kwh == pytest.approx(307.0)
    assert loaded.purchased_kwh == pytest.approx(343.0)
    assert loaded.feedin_revenue_pln == pytest.approx(307.0 * 0.40)  # rcem z _rec


def test_matching_reconciled_invoice_is_left_alone(store, invoices):
    from pv_roi_tracker import invoice_store
    r = _rec(2026, 3, produced=643.98, exported=307.0)
    r.purchased_kwh = 343.0
    historic_store.save([r], store)
    invoice_store.upsert(_invoice(), reconciled=True, path=invoices)

    count = historic_store.reconcile_pending_invoices(invoices, store)

    assert count == 0


def test_diverged_check_ignores_none_invoice_kwh(store, invoices):
    """Faktura bez kWh (None) nie jest traktowana jako rozjazd."""
    from pv_roi_tracker import invoice_store
    r = _rec(2026, 3, produced=643.98, exported=0.0)
    historic_store.save([r], store)
    invoice_store.upsert(_invoice(imported=None, exported=None), reconciled=True, path=invoices)

    count = historic_store.reconcile_pending_invoices(invoices, store)

    assert count == 0
    assert historic_store.load(store)[0].exported_kwh == pytest.approx(0.0)


def test_pending_invoice_still_reconciled(store, invoices):
    """Dotychczasowa ścieżka pending (reconciled=False) działa bez zmian."""
    from pv_roi_tracker import invoice_store
    historic_store.save([_rec(2026, 3, produced=643.98, exported=0.0)], store)
    invoice_store.upsert(_invoice(), reconciled=False, path=invoices)

    count = historic_store.reconcile_pending_invoices(invoices, store)

    assert count == 1
    assert historic_store.load(store)[0].exported_kwh == pytest.approx(307.0)
    assert invoice_store.get('2026-03', invoices)['reconciled'] is True


def test_reconcile_pending_idempotent_after_fix(store, invoices):
    """Po naprawie kolejne wywołanie nic nie zmienia (brak pętli zapisu przy każdym starcie)."""
    from pv_roi_tracker import invoice_store
    historic_store.save([_rec(2026, 3, produced=643.98, exported=0.0)], store)
    invoice_store.upsert(_invoice(), reconciled=True, path=invoices)

    assert historic_store.reconcile_pending_invoices(invoices, store) == 1
    assert historic_store.reconcile_pending_invoices(invoices, store) == 0


def test_nota_never_reconciles_historic(store, invoices):
    """Nota (reconciled=False na stałe, kWh=0) nie może zerować rekordu miesiąca."""
    import dataclasses
    from pv_roi_tracker import invoice_store
    r = _rec(2026, 3, produced=643.98, exported=307.0)
    r.purchased_kwh = 343.0
    historic_store.save([r], store)
    nota = dataclasses.replace(_invoice(imported=0.0, exported=0.0),
                               doc_type='nota', invoice_number='K1NBN567872/025')
    invoice_store.upsert(nota, reconciled=False, path=invoices)

    count = historic_store.reconcile_pending_invoices(invoices, store)

    assert count == 0
    loaded = historic_store.load(store)[0]
    assert loaded.exported_kwh == pytest.approx(307.0)
    assert loaded.purchased_kwh == pytest.approx(343.0)


def test_diverged_billing_heals_despite_pending_nota(store, invoices):
    """Scenariusz incydentu 2026-03: rekord wyzerowany przez notę, billing reconciled=True.
    Start ma naprawić rekord z faktury rozliczeniowej, a noty nie tknąć."""
    import dataclasses
    from pv_roi_tracker import invoice_store
    historic_store.save([_rec(2026, 3, produced=643.98, exported=0.0)], store)
    invoice_store.upsert(_invoice(), reconciled=True, path=invoices)
    nota = dataclasses.replace(_invoice(imported=0.0, exported=0.0),
                               doc_type='nota', invoice_number='K1NBN567872/025')
    invoice_store.upsert(nota, reconciled=False, path=invoices)

    count = historic_store.reconcile_pending_invoices(invoices, store)

    assert count == 1
    loaded = historic_store.load(store)[0]
    assert loaded.exported_kwh == pytest.approx(307.0)
    # i idempotentnie — nota nie wraca przy kolejnym starcie
    assert historic_store.reconcile_pending_invoices(invoices, store) == 0
    assert historic_store.load(store)[0].exported_kwh == pytest.approx(307.0)
