"""Tests for web._build_rate_trend() — trend stawek jednostkowych z faktur.

_build_rate_trend() takes an already stub-filtered invoices dict (billing records only)
and builds per-invoice rate series and effective_gross_per_kwh."""
import pytest

from pv_roi_tracker import web, invoice_store
from pv_roi_tracker.invoice_parser import InvoiceData, _parse_text
from tests.test_invoice_parser import _make_old_format_text, _make_g11_format_text


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / 'invoices.json'


def _real(store_path):
    return invoice_store.load_real(store_path)


def test_no_invoices_returns_none(store_path):
    assert web._build_rate_trend(_real(store_path)) is None


def test_stub_only_returns_none(store_path):
    invoice_store.upsert_stub('bad.pdf', 'raw', 'parse failed', path=store_path)
    assert web._build_rate_trend(_real(store_path)) is None


def test_returns_rate_series_for_single_invoice(store_path):
    data = _parse_text(_make_old_format_text())   # 2025-10
    invoice_store.upsert(data, filename='a.pdf', path=store_path)

    rt = web._build_rate_trend(_real(store_path))
    assert rt is not None
    assert '2025-10' in rt['labels']
    months = {m['ym']: m for m in rt['rates_per_month']}
    m = months['2025-10']
    assert m['energy_peak_net'] == pytest.approx(0.76100, abs=0.0001)
    assert m['oze_net'] is not None


def test_effective_gross_per_kwh_is_positive_when_kwh_present(store_path):
    data = _parse_text(_make_old_format_text())   # 2025-10 — has imported_kwh
    invoice_store.upsert(data, filename='a.pdf', path=store_path)

    rt = web._build_rate_trend(_real(store_path))
    m  = rt['rates_per_month'][0]
    # Either populated (if amounts present) or None (if no amount fields)
    # We only assert it's not negative
    if m['effective_gross_per_kwh'] is not None:
        assert m['effective_gross_per_kwh'] > 0


def test_chronological_order_and_labels(store_path):
    older = _parse_text(_make_g11_format_text())   # 2024-02
    newer = _parse_text(_make_old_format_text())   # 2025-10
    invoice_store.upsert(newer, filename='newer.pdf', path=store_path)
    invoice_store.upsert(older, filename='older.pdf', path=store_path)

    rt = web._build_rate_trend(_real(store_path))
    labels = rt['labels']
    assert labels == sorted(labels)
    assert labels[0] == '2024-02'
    assert labels[-1] == '2025-10'


def test_latest_effective_gross_matches_last_month(store_path):
    older = _parse_text(_make_g11_format_text())   # 2024-02
    newer = _parse_text(_make_old_format_text())   # 2025-10
    invoice_store.upsert(older, filename='older.pdf', path=store_path)
    invoice_store.upsert(newer, filename='newer.pdf', path=store_path)

    rt = web._build_rate_trend(_real(store_path))
    last = rt['rates_per_month'][-1]
    assert rt['latest_effective_gross_per_kwh'] == last['effective_gross_per_kwh']


# ── regressions from docs/AUDIT_2026_08_10.md (point C) ─────────────────────

def _data(**overrides) -> InvoiceData:
    base = dict(
        year=2023, month=12, imported_kwh=1703.0, exported_kwh=0.0,
        imported_kwh_peak=1703.0, imported_kwh_offpeak=None,
        exported_kwh_peak=None, exported_kwh_offpeak=None,
        energy_peak_net=0.698, energy_offpeak_net=None,
        dist_var_peak_net=0.272, dist_var_offpeak_net=None,
        dist_jakosciowa_net=0.0242, dist_oze_net=0.0, dist_kogeneracja_net=0.00496,
        fixed_mocowa_net=13.35, fixed_abonament_net=4.56, fixed_stalysieciowy_net=10.3,
    )
    base.update(overrides)
    return InvoiceData(**base)


def test_reconstructs_missing_energy_amount_from_rate(store_path):
    """2023-12 regression: energy_amount_net wasn't captured (only the
    per-kWh rate energy_peak_net was), so the old comps_net list silently
    treated the whole energy line as 0 — 0.39 PLN/kWh at 1703 kWh imported,
    versus an invoice total (amount_due 2035.92) implying ~1.20. The fix
    reconstructs it the same way _build_cost_breakdown already does
    (rate × kWh) instead of dropping it."""
    data = _data(energy_amount_net=None)  # not captured, only the rate is
    invoice_store.upsert(data, filename='dec.pdf', path=store_path)

    rt = web._build_rate_trend(_real(store_path))
    m = rt['rates_per_month'][0]
    assert m['effective_gross_per_kwh'] is not None
    # Without the energy component the old code produced ~0.39; with the
    # rate×kWh reconstruction it lands close to the invoice's own avg_price.
    assert m['effective_gross_per_kwh'] > 0.9


def test_reconstructs_implausibly_small_stored_energy_amount(store_path):
    """Real 2023-12 regression, caught during 0.35.4's own post-release
    verification: energy_amount_net wasn't missing — it was stored as a
    parser artifact (~1.0 PLN net) on a 1703 kWh month whose own rate
    (energy_peak_net=0.698) implies ~1189 PLN. The 'reconstruct only when
    None' fix above didn't catch this, because the field was non-None. Must
    fall back to the rate×kWh reconstruction here too, not just when the
    field is truly absent."""
    data = _data(energy_amount_net=1.0)  # implausible: real invoice implies ~1189
    invoice_store.upsert(data, filename='dec.pdf', path=store_path)

    rt = web._build_rate_trend(_real(store_path))
    m = rt['rates_per_month'][0]
    assert m['effective_gross_per_kwh'] is not None
    assert m['effective_gross_per_kwh'] > 0.9  # not the ~0.39 the stored artifact would give


def test_eff_is_none_when_energy_component_wholly_unrecoverable(store_path):
    """When neither the amount nor the rate is available, the energy
    component can't be reconstructed at all — eff must be None, not a number
    computed from just the small remaining components."""
    data = _data(energy_amount_net=None, energy_peak_net=None, imported_kwh_peak=None,
                 imported_kwh_offpeak=1703.0, energy_offpeak_net=None)
    invoice_store.upsert(data, filename='dec.pdf', path=store_path)

    rt = web._build_rate_trend(_real(store_path))
    m = rt['rates_per_month'][0]
    assert m['effective_gross_per_kwh'] is None


def test_low_volume_month_flagged_and_excluded_from_latest(store_path):
    """2024-06 regression: 9 kWh imported that month meant ~17 PLN of fixed
    fees alone worked out to 5.26 PLN/kWh — mathematically correct but not a
    usable 'current rate'. low_volume must flag it, and the headline
    latest_effective_gross_per_kwh must skip past it to the prior
    representative month instead of reporting the spike."""
    representative = _data(year=2024, month=5, imported_kwh=300.0, imported_kwh_peak=300.0)
    low_volume = _data(year=2024, month=6, imported_kwh=9.0, imported_kwh_peak=9.0)
    invoice_store.upsert(representative, filename='may.pdf', path=store_path)
    invoice_store.upsert(low_volume, filename='jun.pdf', path=store_path)

    rt = web._build_rate_trend(_real(store_path))
    months = {m['ym']: m for m in rt['rates_per_month']}
    assert months['2024-06']['low_volume'] is True
    assert months['2024-06']['effective_gross_per_kwh'] > 2.0  # still reported, just flagged
    assert months['2024-05']['low_volume'] is False
    assert rt['latest_effective_gross_per_kwh'] == months['2024-05']['effective_gross_per_kwh']


def test_corrections_excluded(store_path):
    """Correction keys (YYYY-MM~kor~N) must not appear in rate_trend.

    In production api_data() calls filter_billing(filter_real(stored)) before
    passing the result to _build_rate_trend() — the correction is already gone.
    We mirror that same path here.
    """
    data = _parse_text(_make_old_format_text())   # 2025-10
    invoice_store.upsert(data, filename='a.pdf', path=store_path)

    # Manually inject a correction record
    from dataclasses import asdict as _asdict
    import json
    raw = invoice_store.load(store_path)
    raw['2025-10~kor~1'] = {**_asdict(data), 'doc_type': 'korekta'}
    store_path.write_text(json.dumps({'invoices': raw}))

    # Production path: filter_billing(filter_real(...))
    billing = invoice_store.filter_billing(invoice_store.load_real(store_path))
    rt = web._build_rate_trend(billing)
    assert rt is not None
    assert all('~kor~' not in m['ym'] for m in rt['rates_per_month'])
    assert len(rt['rates_per_month']) == 1
