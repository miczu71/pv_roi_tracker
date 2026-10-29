"""Tests for web.latest_invoice_rates(_today=PRE_CATALOG) and the _latest_real_invoice() helper
it shares with _build_tariff_drift() — the "single source of truth" rate
provider for MQTT sensors / energy_simulation.yaml / Analiza taryf.

latest_invoice_rates() incorporates the tariff module (catalog + manual overrides) as
layer 1. Tests that isolate invoice-only behaviour pass a date before the catalog
(PRE_CATALOG), where the module layer is empty."""
from datetime import date

import pytest

from pv_roi_tracker import web, invoice_store, tariff_overrides
from pv_roi_tracker.invoice_parser import _parse_text
from tests.test_invoice_parser import _make_old_format_text, _make_g11_format_text

PRE_CATALOG = date(2020, 1, 1)   # katalog zaczyna się 2025-01 → warstwa modułu pusta


@pytest.fixture(autouse=True)
def _reset_paths(tmp_path):
    """Reset both invoice_path and tariff_overrides_path after each test."""
    tc_path = tmp_path / 'tariff_overrides.json'
    # empty overrides — module layer = catalog only, no manual override
    tariff_overrides.save([], tc_path)
    web.set_tariff_overrides_path(tc_path)
    yield
    web.set_invoice_path(None)
    web.set_tariff_overrides_path(None)


@pytest.fixture
def store_path(tmp_path):
    path = tmp_path / 'invoices.json'
    web.set_invoice_path(path)
    return path


def test_no_invoice_path_returns_empty_dict(tmp_path):
    web.set_invoice_path(None)
    # Date outside the catalog → no module layer → should return {}
    assert web.latest_invoice_rates(_today=PRE_CATALOG) == {}


def test_no_invoices_returns_empty_dict(store_path):
    # Date outside the catalog → no module layer → {}
    assert web.latest_invoice_rates(_today=PRE_CATALOG) == {}


def test_stub_only_returns_empty_dict(store_path):
    invoice_store.upsert_stub('bad.pdf', 'raw', 'parse failed', path=store_path)
    assert web.latest_invoice_rates(_today=PRE_CATALOG) == {}


def test_returns_rates_from_only_invoice(store_path):
    data = _parse_text(_make_old_format_text())  # 2025-10
    invoice_store.upsert(data, filename='a.pdf', path=store_path)

    rates = web.latest_invoice_rates(_today=PRE_CATALOG)
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
    rates_after_newer = web.latest_invoice_rates(_today=PRE_CATALOG)
    assert rates_after_newer['energy_peak_net'] == pytest.approx(0.76100, abs=0.0001)

    # Upload the older invoice afterward — must NOT change "latest"
    invoice_store.upsert(older, filename='older.pdf', path=store_path)
    rates_after_older = web.latest_invoice_rates(_today=PRE_CATALOG)
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

    rates = web.latest_invoice_rates(_today=PRE_CATALOG)
    assert rates.get('energy_peak_net') == pytest.approx(0.76100, abs=0.0001)


def test_only_non_none_fields_included(store_path):
    data = _parse_text(_make_g11_format_text())  # G11: no offpeak fields
    invoice_store.upsert(data, filename='a.pdf', path=store_path)
    rates = web.latest_invoice_rates(_today=PRE_CATALOG)
    assert 'energy_offpeak_net' not in rates  # None on a single-zone invoice


def test_manual_override_inherits_catalog_fields(tmp_path):
    """Korekta 2027-01 z samym gross_peak/offpeak zmienia tylko ceny brutto; opłaty stałe
    zostają z katalogu (ostatni okres katalogu dla dni po jego końcu). Brak faktur."""
    module = web._tariff_module()
    day = date(2027, 1, 1)
    patch = tariff_overrides.entry_to_patch({'peak_gross': 1.31, 'offpeak_gross': 0.67}, module, day)
    ov_path = tmp_path / 'tariff_overrides_inh.json'
    tariff_overrides.save([{'from': '2027-01-01', 'to': None, 'note': 'nowe gross', 'patch': patch}],
                          ov_path)
    web.set_tariff_overrides_path(ov_path)

    rates = web.latest_invoice_rates(_today=date(2027, 6, 1))
    assert rates.get('peak_gross') == pytest.approx(1.31, abs=1e-4)       # z korekty 2027-01
    assert rates.get('offpeak_gross') == pytest.approx(0.67, abs=1e-4)    # z korekty 2027-01
    assert rates.get('fixed_abonament_net') == pytest.approx(4.56, abs=1e-3)  # z katalogu
    assert rates.get('fixed_stalysieciowy_net') == pytest.approx(10.86, abs=1e-3)  # z katalogu


def test_latest_invoice_rates_priority(tmp_path):
    """Moduł (katalog) < faktura < korekta ręczna w oknie luki."""
    from dataclasses import replace
    from pv_roi_tracker.invoice_parser import InvoiceData

    inv_p = tmp_path / 'invoices.json'
    web.set_invoice_path(inv_p)
    today = date(2027, 1, 15)

    # Brak faktur → stawki katalogu (G12w 2026: 1.2304)
    assert web.latest_invoice_rates(_today=date(2026, 10, 29)).get('peak_gross') == pytest.approx(1.2304, abs=1e-4)

    # Faktura 2026-12 z peak_gross=1.2999 nadpisuje katalog
    base = InvoiceData(year=2026, month=12, imported_kwh=300, exported_kwh=100,
                       imported_kwh_peak=None, imported_kwh_offpeak=None,
                       exported_kwh_peak=None, exported_kwh_offpeak=None,
                       energy_peak_net=None, energy_offpeak_net=None,
                       dist_var_peak_net=None, dist_var_offpeak_net=None,
                       dist_jakosciowa_net=None, dist_oze_net=None, dist_kogeneracja_net=None,
                       fixed_mocowa_net=None, fixed_abonament_net=None,
                       fixed_stalysieciowy_net=None)
    invoice_store.upsert(replace(base, peak_gross=1.2999, offpeak_gross=0.6306), path=inv_p)
    assert web.latest_invoice_rates(_today=today).get('peak_gross') == pytest.approx(1.2999, abs=1e-4)

    # Korekta od 2027-01 (peak 1.31) wygrywa, gdy brak faktury za 2027-01
    module = web._tariff_module()
    patch = tariff_overrides.entry_to_patch({'peak_gross': 1.31, 'offpeak_gross': 0.67},
                                            module, date(2027, 1, 1))
    ov_p = tmp_path / 'ov_prio.json'
    tariff_overrides.save([{'from': '2027-01-01', 'to': None, 'note': 'TD 2027', 'patch': patch}], ov_p)
    web.set_tariff_overrides_path(ov_p)
    assert web.latest_invoice_rates(_today=today).get('peak_gross') == pytest.approx(1.31, abs=1e-4)

    # Faktura za 2027-01 nadgania korektę → wraca prymat faktury
    invoice_store.upsert(replace(base, year=2027, month=1, peak_gross=1.3333, offpeak_gross=0.6806),
                         path=inv_p)
    assert web.latest_invoice_rates(_today=today).get('peak_gross') == pytest.approx(1.3333, abs=1e-4)


def test_tariff_drift_unaffected_by_stub_key(store_path, tmp_path):
    """_build_tariff_drift() shares the same _latest_real_invoice() selection
    — a stub must not suppress a real drift detection."""
    data = _parse_text(_make_old_format_text())
    invoice_store.upsert(data, filename='a.pdf', path=store_path)
    invoice_store.upsert_stub('bad.pdf', 'raw', 'boom', path=store_path)

    # Set a manual override to extreme values to force a drift vs the invoice
    module = web._tariff_module()
    patch = tariff_overrides.entry_to_patch(
        {'peak_gross': 0.80, 'offpeak_gross': 0.35, 'fixed_abonament_net': 0.01}, module, date.today())
    tc_path = tmp_path / 'tariff_overrides_drift.json'
    tariff_overrides.save([{'from': '2025-01-01', 'to': None, 'note': 'test', 'patch': patch}], tc_path)
    web.set_tariff_overrides_path(tc_path)

    drift = web._build_tariff_drift(invoice_store.load_real(store_path))
    assert drift is not None
    assert 'peak' in drift
