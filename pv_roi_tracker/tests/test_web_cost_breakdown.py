"""Tests for web._build_cost_breakdown() — the Faktury tab's per-component
cost aggregation (table + stacked chart data source).

_build_cost_breakdown() takes an already stub-filtered invoices dict (loaded
once per /api/data request by api_data() — see invoice_store.load_real());
it no longer loads invoices.json itself."""
import pytest

from pv_roi_tracker import web, invoice_store
from pv_roi_tracker.invoice_parser import _parse_text
from tests.test_invoice_parser import _make_old_format_text, _make_g11_format_text


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / 'invoices.json'


def _real(store_path):
    return invoice_store.load_real(store_path)


def test_no_invoices_returns_none(store_path):
    assert web._build_cost_breakdown(_real(store_path)) is None


def test_stub_only_returns_none(store_path):
    invoice_store.upsert_stub('bad.pdf', 'raw', 'parse failed', path=store_path)
    assert web._build_cost_breakdown(_real(store_path)) is None


def test_single_invoice_amounts_used_directly(store_path):
    data = _parse_text(_make_old_format_text())  # 2025-10, full amount fields present
    invoice_store.upsert(data, filename='a.pdf', path=store_path)

    bd = web._build_cost_breakdown(_real(store_path))
    assert bd is not None
    assert bd['per_month']['labels'] == ['2025-10']
    assert bd['any_reconstructed'] is False

    by_key = {c['key']: c for c in bd['components']}
    assert by_key['energia']['total_net'] == pytest.approx(150.71, abs=0.01)
    assert by_key['oze']['total_net'] == pytest.approx(1.05, abs=0.01)
    assert by_key['mocowa']['total_net'] == pytest.approx(16.01, abs=0.01)
    # Optional fees never appear on this invoice — must be omitted, not zeroed
    assert 'przejsciowa' not in by_key
    assert 'akcyza' not in by_key

    total_share = sum(c['share_pct'] for c in bd['components'])
    assert total_share == pytest.approx(100.0, abs=0.5)


def test_missing_amount_fields_trigger_fallback_reconstruction(store_path):
    """Invoices parsed before the amount fields existed (or where the value
    column wasn't captured) must still produce a breakdown via rate × kWh."""
    data = _parse_text(_make_g11_format_text())  # 2024-02
    data.energy_amount_net = None
    data.dist_var_amount_net = None
    data.dist_jakosciowa_amount_net = None
    data.dist_oze_amount_net = None
    data.dist_kogeneracja_amount_net = None
    invoice_store.upsert(data, filename='b.pdf', path=store_path)

    bd = web._build_cost_breakdown(_real(store_path))
    assert bd['any_reconstructed'] is True
    by_key = {c['key']: c for c in bd['components']}
    # G11: single zone, energy_peak_net=0.67270, imported_kwh_peak=imported_kwh=1226
    assert by_key['energia']['total_net'] == pytest.approx(0.67270 * 1226, abs=0.5)
    # fixed charges are already monetary — no reconstruction needed, unaffected
    assert by_key['mocowa']['total_net'] == pytest.approx(14.90, abs=0.01)


def test_multi_month_sums_across_invoices(store_path):
    d1 = _parse_text(_make_old_format_text())   # 2025-10
    d2 = _parse_text(_make_g11_format_text())    # 2024-02
    invoice_store.upsert(d1, filename='a.pdf', path=store_path)
    invoice_store.upsert(d2, filename='b.pdf', path=store_path)

    bd = web._build_cost_breakdown(_real(store_path))
    assert bd['per_month']['labels'] == ['2024-02', '2025-10']
    by_key = {c['key']: c for c in bd['components']}
    assert by_key['mocowa']['total_net'] == pytest.approx(14.90 + 16.01, abs=0.01)
    assert bd['grand_total_net'] == pytest.approx(
        sum(c['total_net'] for c in bd['components']), abs=0.01)


def test_unparsed_stub_excluded_from_aggregation(store_path):
    data = _parse_text(_make_old_format_text())
    invoice_store.upsert(data, filename='a.pdf', path=store_path)
    invoice_store.upsert_stub('bad.pdf', 'raw', 'parse failed', path=store_path)

    bd = web._build_cost_breakdown(_real(store_path))
    assert bd['per_month']['labels'] == ['2025-10']  # stub's synthetic key excluded


def test_implausibly_small_stored_amount_reconstructed_not_kept(store_path):
    """Regression for the real 2023-12 invoice, caught during 0.35.4's own
    post-release verification (docs/AUDIT_2026_08_10.md): energy_amount_net
    was stored as ~1.0 PLN net on a 1703 kWh month whose own energy_peak_net
    rate (0.698 PLN/kWh) implies ~1189 PLN — a parser artifact, not a
    genuine near-zero charge. Because the field was non-None, the original
    'val is None -> reconstruct' fallback never fired and kept the bad
    value. _pick_amount() must override a stored figure that's far below
    what the rate implies, the same way it already does for a truly missing
    (None) one."""
    data = _parse_text(_make_g11_format_text())   # 2024-02: 1226 kWh, energy_peak_net=0.67270
    data.energy_amount_net = 1.0                  # parser artifact — nowhere near 0.67270*1226
    invoice_store.upsert(data, filename='dec.pdf', path=store_path)

    bd = web._build_cost_breakdown(_real(store_path))
    by_key = {c['key']: c for c in bd['components']}
    assert by_key['energia']['total_net'] == pytest.approx(0.67270 * 1226, abs=0.5)
    assert bd['any_reconstructed'] is True


def test_plausible_small_stored_amount_kept_not_overridden(store_path):
    """A genuinely small amount (e.g. a low-import month, or a component
    whose rate is legitimately near-zero) must NOT be second-guessed just
    for being small relative to a much larger reconstruction target on a
    high-volume invoice — only reconstruct when there's an available rate
    to compare against and the stored figure is far below it."""
    data = _parse_text(_make_g11_format_text())   # 2024-02
    data.dist_oze_net = 0.00001                   # near-zero but real rate
    data.dist_oze_amount_net = 0.01                # tiny but genuine amount
    invoice_store.upsert(data, filename='dec.pdf', path=store_path)

    bd = web._build_cost_breakdown(_real(store_path))
    by_key = {c['key']: c for c in bd['components']}
    assert by_key['oze']['total_net'] == pytest.approx(0.01, abs=0.001)
