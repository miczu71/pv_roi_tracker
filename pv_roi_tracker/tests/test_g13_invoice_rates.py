"""Faktura G13 jako źródło bieżących stawek stref: latest_invoice_rates, dryf, czujniki, pompa ciepła."""
from datetime import date, datetime

import pytest

from pv_roi_tracker import main, web
from pv_roi_tracker import tariff_catalog as tc
from pv_roi_tracker.publisher import _INVOICE_RATE_SENSORS, _SENSORS, _render_value
from pv_roi_tracker.tariff_module import TariffModule

CATALOG = {'morning': 0.9048, 'evening': 1.4960, 'other': 0.6257}
ZONES = ('morning', 'evening', 'other')
G13_PERIODS = [{'tariff': 'G12w', 'from': None}, {'tariff': 'G13', 'from': '2026-11-03'}]
G12W_PERIODS = [{'tariff': 'G12w', 'from': None}]
G13S_PERIODS = [{'tariff': 'G12w', 'from': None}, {'tariff': 'G13s', 'from': '2026-11-03'}]
G13_DAY = date(2026, 11, 20)


def _module(periods):
    return TariffModule(tc.load_catalog(), periods=periods)


def _g13_invoice(**gross):
    inv = {'tariff': 'G13', 'peak_gross': None, 'offpeak_gross': None,
           **{f'{z}_gross': CATALOG[z] for z in ZONES}}
    inv.update(gross)
    return inv


@pytest.fixture
def use_module(monkeypatch):
    def _use(periods):
        monkeypatch.setattr(web, '_tariff_module', lambda: _module(periods))
    return _use


def test_g13_z_faktura_strefy_z_faktury(use_module):
    use_module(G13_PERIODS)
    real = {'2026-11': _g13_invoice(morning_gross=0.95, evening_gross=1.55, other_gross=0.65)}
    rates = web.latest_invoice_rates(real, _today=G13_DAY)
    assert rates['morning_gross'] == 0.95
    assert rates['evening_gross'] == 1.55
    assert rates['other_gross'] == 0.65


def test_g13_bez_faktury_strefy_z_katalogu(use_module):
    use_module(G13_PERIODS)
    rates = web.latest_invoice_rates({}, _today=G13_DAY)
    for z in ZONES:
        assert rates[f'{z}_gross'] == pytest.approx(CATALOG[z], abs=1e-4)


def test_g12w_po_starej_fakturze_g13_bez_stref(use_module):
    use_module(G12W_PERIODS)
    real = {'2026-11': _g13_invoice()}
    rates = web.latest_invoice_rates(real, _today=date(2026, 10, 20))
    for z in ZONES:
        assert f'{z}_gross' not in rates


def test_g12w_bez_regresji(use_module):
    use_module(G12W_PERIODS)
    real = {'2026-10': {'tariff': 'G12w', 'peak_gross': 1.31, 'offpeak_gross': 0.66}}
    rates = web.latest_invoice_rates(real, _today=date(2026, 10, 20))
    assert rates['peak_gross'] == 1.31
    assert rates['offpeak_gross'] == 0.66
    assert not any(f'{z}_gross' in rates for z in ZONES)


def test_dryf_g13_rozjazd_ponad_prog(use_module):
    use_module(G13_PERIODS)
    real = {'2026-11': _g13_invoice(morning_gross=1.0048)}   # +0.10 vs katalog
    drift = web._build_tariff_drift(real, _today=G13_DAY)
    assert drift is not None
    assert drift['morning']['invoice'] == 1.0048
    assert drift['morning']['configured'] == pytest.approx(0.9048, abs=1e-4)
    assert 'evening' not in drift and 'other' not in drift


def test_dryf_g13_zgodny_z_katalogiem_brak_wpisu(use_module):
    use_module(G13_PERIODS)
    assert web._build_tariff_drift({'2026-11': _g13_invoice()}, _today=G13_DAY) is None


def test_publisher_trzy_czujniki_stref():
    rates = {'morning_gross': 0.9048, 'evening_gross': 1.496, 'other_gross': 0.6257}
    for slug, field in (('rate_morning_gross', 'morning_gross'), ('rate_evening_gross', 'evening_gross'),
                        ('rate_other_gross', 'other_gross')):
        sensor = next(s for s in _SENSORS if s.slug == slug)
        assert _INVOICE_RATE_SENSORS[slug] == field
        assert sensor.unit == 'PLN/kWh'
        assert _render_value(sensor, None, invoice_rates=rates) == str(rates[field])
        assert _render_value(sensor, None, invoice_rates={}) == 'unknown'


# ── pompa ciepła: godziny G13 po stawkach faktury, G12W po starej ścieżce ──

INV_ZONES = {'morning_peak': 1.01, 'evening_peak': 1.61, 'other': 0.71}   # != katalog; klucze = strefy katalogu


def test_dryf_g12w_bez_regresji_i_bez_stref(use_module):
    use_module(G12W_PERIODS)
    real = {'2026-10': {'tariff': 'G12w', 'peak_gross': 1.60, 'offpeak_gross': 0.63}}
    drift = web._build_tariff_drift(real, _today=date(2026, 10, 20))
    assert drift['peak']['invoice'] == 1.60
    assert not any(z in drift for z in ZONES)


def test_dryf_g13_prog_graniczny(use_module):
    use_module(G13_PERIODS)
    assert web._build_tariff_drift({'2026-11': _g13_invoice(morning_gross=0.9048 + 0.019)}, _today=G13_DAY) is None
    assert 'morning' in web._build_tariff_drift({'2026-11': _g13_invoice(morning_gross=0.9048 + 0.03)}, _today=G13_DAY)


def test_faktura_g13_w_dniu_g12w_bez_dryfu_stref(use_module):
    use_module(G12W_PERIODS)
    assert web._build_tariff_drift({'2026-11': _g13_invoice(morning_gross=2.0)}, _today=date(2026, 10, 20)) is None


def test_pompa_ciepla_miesiac_mieszany_stawki_z_faktury():
    tmod = _module(G13_PERIODS)
    g13_invoice = {'2026-11': INV_ZONES}
    # 2026-11-10 (wt, G13): 08 -> poranny, 19 -> wieczorny, 02 -> pozostałe
    assert main._hour_price(tmod, datetime(2026, 11, 10, 8), (), g13_invoice) == 1.01
    assert main._hour_price(tmod, datetime(2026, 11, 10, 19), (), g13_invoice) == 1.61
    assert main._hour_price(tmod, datetime(2026, 11, 10, 2), (), g13_invoice) == 0.71
    # niedziela 2026-11-15 — cały dzień „pozostałe”
    assert main._hour_price(tmod, datetime(2026, 11, 15, 8), (), g13_invoice) == 0.71
    # godziny G12W (przed 03.11) — stara ścieżka (None → para peak/offpeak)
    assert main._hour_price(tmod, datetime(2026, 11, 1, 8), (), g13_invoice) is None
    # bez faktury — katalog
    assert main._hour_price(tmod, datetime(2026, 11, 10, 8), (), {}) == pytest.approx(0.9048, abs=1e-4)
    # miesiąc spoza mapy faktur — katalog
    assert main._hour_price(tmod, datetime(2026, 12, 10, 8), (), g13_invoice) == pytest.approx(
        tmod.price_at(datetime(2026, 12, 10, 8)).gross)


@pytest.fixture(autouse=True)
def _reset_warned():
    main._HOUR_PRICE_WARNED.clear()


def test_hour_price_g13s_ignoruje_stawki_faktury_g13():
    tmod = _module(G13S_PERIODS)          # G13s ma inne strefy — wycena z katalogu, bez ostrzeżenia
    dt = datetime(2026, 11, 10, 2)
    assert main._hour_price(tmod, dt, (), {'2026-11': INV_ZONES}) == pytest.approx(tmod.price_at(dt).gross)
    assert not main._HOUR_PRICE_WARNED


def test_hour_price_skip_months_wygrywa_z_faktura_g13():
    tmod = _module(G13_PERIODS)
    assert main._hour_price(tmod, datetime(2026, 11, 10, 8), ('2026-11',), {'2026-11': INV_ZONES}) is None


def test_hour_price_strefa_bez_stawki_w_fakturze_idzie_do_katalogu():
    tmod = _module(G13_PERIODS)
    partial = {'2026-11': {'evening_peak': 1.61, 'other': 0.71}}      # brak morning_peak
    assert main._hour_price(tmod, datetime(2026, 11, 10, 8), (), partial) == pytest.approx(0.9048, abs=1e-4)


def test_g13_invoice_rates_wiring():
    full = _g13_invoice(morning_gross=1.01, evening_gross=1.61, other_gross=0.71)
    partial = _g13_invoice(other_gross=None)
    two_zone = {'tariff': 'G12w', 'peak_gross': 1.3, 'offpeak_gross': 0.6}
    stored = {'2026-11': full, '2026-12': partial, '2026-10': two_zone, '2027-01': full}
    out = main._g13_invoice_rates(stored, {'2026-11', '2026-12', '2026-10'})
    assert out == {'2026-11': INV_ZONES}         # częściowa, dwustrefowa i miesiąc spoza trójstrefowych pominięte
