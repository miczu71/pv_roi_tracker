"""Wycena trzystrefowa (G13): zone_rates, buy_price ważony po strefach, pompa ciepła, symulacja magazynu,
atrybuty opłat stałych encji taryfy."""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import patch

import pytest

import pv_roi_tracker.live_reader as lr
from pv_roi_tracker import battery_sim, heatpump
from pv_roi_tracker import tariff_catalog as tc
from pv_roi_tracker.tariff_module import TariffModule, feed_payload

G13_PRICES = {'morning_peak': 0.9048, 'evening_peak': 1.4960, 'other': 0.6257}


@pytest.fixture(scope='module')
def catalog():
    return tc.load_catalog()


@pytest.fixture
def g13(catalog):
    return TariffModule(catalog, periods=[{'tariff': 'G12w', 'from': None},
                                          {'tariff': 'G13', 'from': '2026-11-03'}])


# ── TariffModule.zone_rates ──────────────────────────────────────────────────

def test_zone_rates_g13_rowne_price_at(g13):
    rates = g13.zone_rates(date(2026, 11, 10))
    assert set(rates) == {'morning_peak', 'evening_peak', 'other'}
    assert rates['morning_peak'] == pytest.approx(0.9048, abs=1e-4)
    assert rates['evening_peak'] == pytest.approx(1.4960, abs=1e-4)
    assert rates['other'] == pytest.approx(0.6257, abs=1e-4)
    # wtorek, zima: 08 = poranny, 18 = wieczorny, 14 = other
    for hour, zone in ((8, 'morning_peak'), (18, 'evening_peak'), (14, 'other')):
        p = g13.price_at(datetime(2026, 11, 10, hour))
        assert p.zone == zone and p.gross == pytest.approx(rates[zone])


def test_zone_rates_g12w_i_taryfa_jawna(g13):
    r = g13.zone_rates(date(2026, 11, 1))
    assert set(r) == {'peak', 'offpeak'}
    assert set(g13.zone_rates(date(2026, 11, 10), 'G12w')) == {'peak', 'offpeak'}


def test_zone_rates_uwzglednia_korekte(catalog):
    ovs = [{'from': '2026-11-01', 'to': None, 'patch': {'energy_net': {'G13': {'other': 0.5}}}}]
    base = TariffModule(catalog, periods=[{'tariff': 'G13', 'from': None}])
    cor = TariffModule(catalog, periods=[{'tariff': 'G13', 'from': None}], overrides=ovs)
    assert cor.zone_rates(date(2026, 11, 10))['other'] != base.zone_rates(date(2026, 11, 10))['other']
    assert cor.zone_rates(date(2026, 11, 10))['other'] == pytest.approx(
        cor.price_at(datetime(2026, 11, 10, 14)).gross)


# ── buy_price: _build_record(zone_kwh, zone_prices) ──────────────────────────

def _record(**kw):
    with patch.object(lr, '_get_state', return_value=None):
        return lr._build_record(year=2026, month=11, produced=100.0, exported=40.0, arb_kwh=None,
                                rcem_price=0.35, **kw)


def test_buy_price_g13_srednia_wazona_po_trzech_strefach():
    zone_kwh = {'morning_peak': 21.05, 'evening_peak': 30.0, 'other': 120.0}
    peak, offpeak = 21.05 + 30.0, 120.0
    rec = _record(peak=peak, offpeak=offpeak, peak_gross=1.496, offpeak_gross=0.6257,
                  imported=peak + offpeak, zone_kwh=zone_kwh, zone_prices=G13_PRICES)
    expected = (21.05 * 0.9048 + 30.0 * 1.4960 + 120.0 * 0.6257) / (21.05 + 30.0 + 120.0)
    assert rec.buy_price_pln_kwh == pytest.approx(expected, abs=1e-4)
    # stare liczenie (poranny po stawce wieczornej) dawałoby wyższą cenę
    old = (peak * 1.496 + offpeak * 0.6257) / (peak + offpeak)
    assert rec.buy_price_pln_kwh < old - 0.03
    assert rec.purchase_cost_pln == pytest.approx(rec.purchased_kwh * rec.buy_price_pln_kwh, abs=0.01)
    # podział do wyświetlania bez zmian
    assert rec.purchased_kwh_peak == pytest.approx(peak, abs=1e-3)


def test_buy_price_dane_listopadowe_w_przedziale():
    """Udziały zbliżone do listopada (21,05 kWh porannych, mało wieczornych) → 0,68–0,70."""
    zone_kwh = {'morning_peak': 21.05, 'evening_peak': 6.0, 'other': 150.0}
    rec = _record(peak=27.05, offpeak=150.0, peak_gross=1.496, offpeak_gross=0.6257,
                  imported=177.05, zone_kwh=zone_kwh, zone_prices=G13_PRICES)
    assert 0.68 <= rec.buy_price_pln_kwh <= 0.70


def test_buy_price_g12w_bez_zmian_gdy_brak_stref():
    rec = _record(peak=20.0, offpeak=80.0, peak_gross=1.23, offpeak_gross=0.63, imported=100.0)
    assert rec.buy_price_pln_kwh == pytest.approx((20 * 1.23 + 80 * 0.63) / 100, abs=1e-4)


def test_buy_price_g12w_ze_strefami_rowna_sciezce_dwustrefowej():
    prices = {'peak': 1.23, 'offpeak': 0.63}
    rec = _record(peak=20.0, offpeak=80.0, peak_gross=1.23, offpeak_gross=0.63, imported=100.0,
                  zone_kwh={'peak': 20.0, 'offpeak': 80.0}, zone_prices=prices)
    assert rec.buy_price_pln_kwh == pytest.approx((20 * 1.23 + 80 * 0.63) / 100, abs=1e-4)


def test_buy_price_miesiac_mieszany_g12w_g13():
    prices = {'peak': 1.23, 'offpeak': 0.63, **G13_PRICES}
    zone_kwh = {'peak': 5.0, 'offpeak': 10.0, 'morning_peak': 4.0, 'evening_peak': 6.0, 'other': 25.0}
    rec = _record(peak=15.0, offpeak=35.0, peak_gross=1.496, offpeak_gross=0.6257, imported=50.0,
                  zone_kwh=zone_kwh, zone_prices=prices)
    expected = (5 * 1.23 + 10 * 0.63 + 4 * 0.9048 + 6 * 1.496 + 25 * 0.6257) / 50
    assert rec.buy_price_pln_kwh == pytest.approx(expected, abs=1e-4)


def test_buy_price_niepelne_ceny_stref_wracaja_do_peak_offpeak():
    rec = _record(peak=20.0, offpeak=80.0, peak_gross=1.23, offpeak_gross=0.63, imported=100.0,
                  zone_kwh={'morning_peak': 20.0, 'other': 80.0}, zone_prices={'other': 0.6})
    assert rec.buy_price_pln_kwh == pytest.approx((20 * 1.23 + 80 * 0.63) / 100, abs=1e-4)


def test_zone_kwh_from_mapuje_liczniki_na_strefy():
    vals = {'sensor.daily_energy_peak': 1.0, 'sensor.g13_energia_morning_peak': 2.0,
            'sensor.g13_energia_other': 3.0, 'sensor.g13_energia_evening_peak': None}
    assert lr._zone_kwh_from(vals) == {'peak': 1.0, 'morning_peak': 2.0, 'other': 3.0}


# ── pompa ciepła i symulacja magazynu ────────────────────────────────────────

def test_pompa_ciepla_o_8_w_dzien_roboczy_ma_stawke_porannego(g13):
    fn = lambda dt: g13.price_at(dt).gross
    price = heatpump.grid_price_for_hour(date(2026, 11, 10), 8, (1.496, 0.6257), hour_price_fn=fn)
    assert price == pytest.approx(0.9048, abs=1e-4)
    # bez funkcji: dotychczasowa para (poranny szczyt = peak_gross)
    assert heatpump.grid_price_for_hour(date(2026, 11, 10), 8, (1.496, 0.6257)) == 1.496
    # funkcja zwraca None -> para
    assert heatpump.grid_price_for_hour(date(2026, 11, 10), 8, (1.496, 0.6257),
                                        hour_price_fn=lambda dt: None) == 1.496


def test_simulate_s1_hour_price_fn_wycenia_godzine(g13):
    cfg = battery_sim.BatteryConfig()
    # ładowanie z eksportu o 12, rozładowanie przeciw importowi o 08 następnego dnia (poranny szczyt)
    hours = {'2026-11-09T12': (2.0, 0.0), '2026-11-10T08': (0.0, 1.0)}
    zone = {'2026-11': (1.496, 0.6257)}
    base = battery_sim.simulate_s1(hours, cfg, zone, {'2026-11': 0.3})[0]
    fn = lambda dt: g13.price_at(dt).gross
    cor = battery_sim.simulate_s1(hours, cfg, zone, {'2026-11': 0.3}, hour_price_fn=fn)[0]
    assert base['discharged_kwh'] == pytest.approx(cor['discharged_kwh'])
    assert cor['avoided_buy_pln'] == pytest.approx(cor['discharged_kwh'] * 0.9048, abs=1e-3)
    assert base['avoided_buy_pln'] == pytest.approx(base['discharged_kwh'] * 1.496, abs=1e-3)


# ── feed_payload: opłaty stałe ───────────────────────────────────────────────

def test_feed_payload_oplaty_stale_g13_2026(g13):
    attrs = feed_payload(g13, datetime(2026, 11, 17, 10, 15))['attributes']
    assert attrs['fixed_monthly_net'] == pytest.approx(34.91, abs=0.005)
    assert attrs['fixed_monthly_gross'] == pytest.approx(34.91 * 1.23, abs=0.01)


# ── TariffModule.zone_prices_for_month ───────────────────────────────────────

def test_zone_prices_miesiac_przejsciowy_laczy_g12w_i_g13(g13):
    prices = g13.zone_prices_for_month(date(2026, 11, 10))
    assert {'peak', 'offpeak'} <= set(prices)               # liczniki G12w z 1–2.11 wyceniane stawkami G12w
    for zone, gross in G13_PRICES.items():
        assert prices[zone] == pytest.approx(gross, abs=1e-4)
    legacy = g13.zone_rates(date(2026, 11, 1))
    assert prices['peak'] == pytest.approx(legacy['peak'])


def test_zone_prices_miesiac_bez_przejscia_tylko_taryfa_dnia(g13):
    prices = g13.zone_prices_for_month(date(2026, 12, 10))
    assert set(prices) == set(G13_PRICES)


def test_zone_prices_przed_katalogiem_puste(g13):
    assert g13.zone_prices_for_month(date(2024, 6, 10)) == {}


def test_feed_payload_bez_oplat_stalych_nie_blokuje_strefy_i_ceny(g13):
    with patch.object(TariffModule, 'fixed', side_effect=KeyError('capacity')):
        out = feed_payload(g13, datetime(2026, 11, 17, 8, 15))
    assert out['state'] and out['price'] > 0
    assert out['attributes']['fixed_monthly_net'] is None
    assert out['attributes']['fixed_monthly_gross'] is None


def test_pompa_ciepla_bez_stawek_z_faktury_wycena_z_cennika(g13):
    """Miesiąc trzystrefowy bez pary z faktury/rekordu (rates None): cenę daje hour_price_fn."""
    fn = lambda dt: g13.price_at(dt).gross
    assert heatpump.grid_price_for_hour(date(2026, 11, 10), 8, None, hour_price_fn=fn) \
        == pytest.approx(0.9048, abs=1e-4)
