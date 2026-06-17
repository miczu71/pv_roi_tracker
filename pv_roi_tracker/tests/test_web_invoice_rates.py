"""Tests for web.latest_invoice_rates() and the _latest_real_invoice() helper
it shares with _build_tariff_drift() — the "single source of truth" rate
provider for MQTT sensors / energy_simulation.yaml / Analiza taryf.

Since v0.22.0, latest_invoice_rates() incorporates tariff_config baseline + override,
so tests must provide a tariff_config_path pointing to an empty / minimal config to
isolate invoice-only behaviour."""
import pytest

from pv_roi_tracker import web, invoice_store, tariff_config
from pv_roi_tracker.invoice_parser import _parse_text
from tests.test_invoice_parser import _make_old_format_text, _make_g11_format_text


@pytest.fixture(autouse=True)
def _reset_paths(tmp_path):
    """Reset both invoice_path and tariff_config_path after each test."""
    tc_path = tmp_path / 'tariff_config.json'
    # empty config — no tariffs, so baseline = {} and override = {}
    tariff_config.save({'tariffs': []}, tc_path)
    web.set_tariff_config_path(tc_path)
    yield
    web.set_invoice_path(None)
    web.set_tariff_config_path(None)


@pytest.fixture
def store_path(tmp_path):
    path = tmp_path / 'invoices.json'
    web.set_invoice_path(path)
    return path


def test_no_invoice_path_returns_empty_dict(tmp_path):
    web.set_invoice_path(None)
    # Empty tariff_config → no baseline → should return {}
    assert web.latest_invoice_rates() == {}


def test_no_invoices_returns_empty_dict(store_path):
    # Empty tariff_config (from autouse fixture) → no baseline → {}
    assert web.latest_invoice_rates() == {}


def test_stub_only_returns_empty_dict(store_path):
    invoice_store.upsert_stub('bad.pdf', 'raw', 'parse failed', path=store_path)
    assert web.latest_invoice_rates() == {}


def test_returns_rates_from_only_invoice(store_path):
    data = _parse_text(_make_old_format_text())  # 2025-10
    invoice_store.upsert(data, filename='a.pdf', path=store_path)

    rates = web.latest_invoice_rates()
    assert rates['energy_peak_net'] == pytest.approx(0.76100, abs=0.0001)
    assert rates['dist_oze_net'] == pytest.approx(0.00350, abs=0.0001)
    assert rates['fixed_mocowa_net'] == pytest.approx(16.01, abs=0.01)
    assert rates['peak_gross'] is not None


def test_picks_chronologically_latest_regardless_of_upload_order(store_path):
    """Uploading an older invoice AFTER a newer one must not override the
    newer one's rates — selection is by billing-period key, not insertion
    order (this is the exact guarantee the user asked about)."""
    newer = _parse_text(_make_old_format_text())   # 2025-10
    older = _parse_text(_make_g11_format_text())    # 2024-02

    invoice_store.upsert(newer, filename='newer.pdf', path=store_path)
    rates_after_newer = web.latest_invoice_rates()
    assert rates_after_newer['energy_peak_net'] == pytest.approx(0.76100, abs=0.0001)

    # Upload the older invoice afterward — must NOT change "latest"
    invoice_store.upsert(older, filename='older.pdf', path=store_path)
    rates_after_older = web.latest_invoice_rates()
    assert rates_after_older['energy_peak_net'] == pytest.approx(0.76100, abs=0.0001)
    assert rates_after_older == rates_after_newer


def test_stub_key_does_not_shadow_real_latest_invoice(store_path):
    """Regression: 'unparsed-<epoch>-...' stub keys sort after every real
    'YYYY-MM' key under plain max(), which previously made the (still
    present) bug in _build_tariff_drift silently pick the stub instead of
    the real latest invoice."""
    data = _parse_text(_make_old_format_text())  # 2025-10
    invoice_store.upsert(data, filename='a.pdf', path=store_path)
    invoice_store.upsert_stub('bad.pdf', 'raw', 'boom', path=store_path)

    rates = web.latest_invoice_rates()
    assert rates.get('energy_peak_net') == pytest.approx(0.76100, abs=0.0001)


def test_only_non_none_fields_included(store_path):
    data = _parse_text(_make_g11_format_text())  # G11: no offpeak fields
    invoice_store.upsert(data, filename='a.pdf', path=store_path)
    rates = web.latest_invoice_rates()
    assert 'energy_offpeak_net' not in rates  # None on a single-zone invoice


def test_tariff_drift_unaffected_by_stub_key(store_path, tmp_path):
    """_build_tariff_drift() shares the same _latest_real_invoice() selection
    — a stub must not suppress a real drift detection."""
    data = _parse_text(_make_old_format_text())
    invoice_store.upsert(data, filename='a.pdf', path=store_path)
    invoice_store.upsert_stub('bad.pdf', 'raw', 'boom', path=store_path)

    # Set tariff_config baseline to extreme values to force a drift vs the invoice
    tc_path = tmp_path / 'tariff_config_drift.json'
    tariff_config.save({'tariffs': [{'effective_from': '2020-01', 'note': 'test',
                                     'rates': {'peak_gross': 0.10, 'offpeak_gross': 0.05,
                                               'fixed_total_net': 0.01}}]}, tc_path)
    web.set_tariff_config_path(tc_path)

    drift = web._build_tariff_drift(invoice_store.load_real(store_path))
    assert drift is not None
    assert 'peak' in drift
