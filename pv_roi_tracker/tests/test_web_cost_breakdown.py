"""Tests for web._build_cost_breakdown() — the Faktury tab's per-component
cost aggregation (table + stacked chart data source)."""
import pytest

from pv_roi_tracker import web, invoice_store
from pv_roi_tracker.invoice_parser import _parse_text
from tests.test_invoice_parser import _make_old_format_text, _make_g11_format_text


@pytest.fixture(autouse=True)
def _reset_invoice_path():
    """Each test gets its own invoice_path; restore the module-level global after."""
    yield
    web.set_invoice_path(None)


@pytest.fixture
def store_path(tmp_path):
    path = tmp_path / 'invoices.json'
    web.set_invoice_path(path)
    return path


def test_no_invoice_path_returns_none():
    web.set_invoice_path(None)
    assert web._build_cost_breakdown() is None


def test_no_invoices_returns_none(store_path):
    assert web._build_cost_breakdown() is None


def test_stub_only_returns_none(store_path):
    invoice_store.upsert_stub('bad.pdf', 'raw', 'parse failed', path=store_path)
    assert web._build_cost_breakdown() is None


def test_single_invoice_amounts_used_directly(store_path):
    data = _parse_text(_make_old_format_text())  # 2025-10, full amount fields present
    invoice_store.upsert(data, filename='a.pdf', path=store_path)

    bd = web._build_cost_breakdown()
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

    bd = web._build_cost_breakdown()
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

    bd = web._build_cost_breakdown()
    assert bd['per_month']['labels'] == ['2024-02', '2025-10']
    by_key = {c['key']: c for c in bd['components']}
    assert by_key['mocowa']['total_net'] == pytest.approx(14.90 + 16.01, abs=0.01)
    assert bd['grand_total_net'] == pytest.approx(
        sum(c['total_net'] for c in bd['components']), abs=0.01)


def test_unparsed_stub_excluded_from_aggregation(store_path):
    data = _parse_text(_make_old_format_text())
    invoice_store.upsert(data, filename='a.pdf', path=store_path)
    invoice_store.upsert_stub('bad.pdf', 'raw', 'parse failed', path=store_path)

    bd = web._build_cost_breakdown()
    assert bd['per_month']['labels'] == ['2025-10']  # stub's synthetic key excluded
