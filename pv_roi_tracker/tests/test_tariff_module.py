from __future__ import annotations

from datetime import date, datetime

import pytest

from pv_roi_tracker import tariff_catalog as tc
from pv_roi_tracker.tariff_module import PricePoint, TariffModule


@pytest.fixture(scope='module')
def catalog():
    return tc.load_catalog()


@pytest.fixture
def g12w(catalog):
    return TariffModule(catalog)


@pytest.fixture
def g13_from_nov(catalog):
    return TariffModule(catalog, periods=[{'tariff': 'G12w', 'from': None},
                                          {'tariff': 'G13', 'from': '2026-11-01'}])


def _at(y, m, d, h):
    return datetime(y, m, d, h)


# ── taryfa na dzień ──────────────────────────────────────────────────────────

def test_default_tariff_is_g12w_without_history(g12w):
    assert g12w.tariff_on(date(2026, 10, 29)) == 'G12w'


def test_tariff_switches_on_period_date(g13_from_nov):
    assert g13_from_nov.tariff_on(date(2026, 10, 31)) == 'G12w'
    assert g13_from_nov.tariff_on(date(2026, 11, 1)) == 'G13'


# ── cena G12w ────────────────────────────────────────────────────────────────

def test_g12w_prices_match_ha_sensor(g12w):
    peak = g12w.price_at(_at(2026, 10, 29, 12))          # czwartek
    off = g12w.price_at(_at(2026, 10, 29, 14))
    assert (peak.zone, round(peak.gross, 4)) == ('peak', 1.2304)
    assert (off.zone, round(off.gross, 4)) == ('offpeak', 0.6306)
    assert peak.is_peak and not off.is_peak
    assert (peak.level, off.level) == ('expensive', 'cheap')


def test_weekend_is_offpeak(g12w):
    p = g12w.price_at(_at(2026, 10, 31, 10))              # sobota
    assert (p.zone, p.is_peak, p.level) == ('offpeak', False, 'cheap')


def test_components_sum_to_net_and_gross_uses_vat(g12w, catalog):
    p = g12w.price_at(_at(2026, 10, 29, 12))
    assert sum(p.components.values()) == pytest.approx(p.net)
    assert p.gross == pytest.approx(p.net * float(catalog['_meta']['vat']))
    assert set(p.components) == {'energy', 'dist', 'quality', 'oze', 'cogeneration', 'excise'}


# ── G13 ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('dt,zone,is_peak', [
    (_at(2026, 11, 10, 8), 'morning_peak', True),          # zima, wtorek
    (_at(2026, 11, 10, 17), 'evening_peak', True),         # zima 16–21
    (_at(2026, 11, 10, 22), 'other', False),
    (_at(2026, 7, 14, 17), 'other', False),                # lato: wieczorny szczyt dopiero od 19
    (_at(2026, 7, 14, 19), 'evening_peak', True),
    (_at(2026, 11, 11, 8), 'other', False),                # 11.11 święto
    (_at(2026, 11, 14, 8), 'other', False),                # sobota
])
def test_g13_zones(catalog, dt, zone, is_peak):
    m = TariffModule(catalog, periods=[{'tariff': 'G13', 'from': None}])
    p = m.price_at(dt)
    assert (p.tariff, p.zone, p.is_peak) == ('G13', zone, is_peak)


def test_g13_levels_rank_by_price(catalog):
    m = TariffModule(catalog, periods=[{'tariff': 'G13', 'from': None}])
    assert m.price_at(_at(2026, 11, 10, 17)).level == 'expensive'    # wieczorny najdroższy
    assert m.price_at(_at(2026, 11, 10, 8)).level == 'mid'
    assert m.price_at(_at(2026, 11, 10, 23)).level == 'cheap'
    assert m.price_at(_at(2026, 11, 10, 17)).gross == pytest.approx(1.496049, abs=1e-5)


def test_g11_is_flat(catalog):
    m = TariffModule(catalog, periods=[{'tariff': 'G11', 'from': None}])
    p = m.price_at(_at(2026, 10, 29, 12))
    assert (p.zone, p.level) == ('all', 'flat')


def test_switch_changes_price_on_same_clock(g13_from_nov):
    before = g13_from_nov.price_at(_at(2026, 10, 29, 12))            # G12w szczyt
    after = g13_from_nov.price_at(_at(2026, 11, 3, 12))              # G13: 12:00 mieści się w 7–13
    assert before.tariff == 'G12w' and after.tariff == 'G13'
    assert after.zone == 'morning_peak'


# ── pewność i okresy ─────────────────────────────────────────────────────────

def test_confidence_document_for_confirmed_period(g12w):
    p = g12w.price_at(_at(2026, 10, 29, 12))
    assert (p.confidence, p.period_from) == ('document', '2026-02-01')


def test_confidence_unconfirmed_for_2025(g12w):
    assert g12w.price_at(_at(2025, 8, 12, 12)).confidence == 'unconfirmed'


def test_period_after_catalog_end_is_assumed_not_error(g12w):
    p = g12w.price_at(_at(2027, 3, 10, 12))
    assert p.confidence == 'assumed' and p.period_from == '2026-02-01'
    assert round(p.gross, 4) == 1.2304


def test_before_catalog_start_raises(g12w):
    with pytest.raises(LookupError):
        g12w.price_at(_at(2024, 12, 31, 12))


def test_manual_override_wins_and_is_marked(catalog):
    m = TariffModule(catalog, overrides=[{
        'from': '2026-11-01', 'to': None, 'note': 'umowa',
        'patch': {'energy_net': {'G12w': {'peak': 0.700}}},
    }])
    old = m.price_at(_at(2026, 10, 29, 12))
    new = m.price_at(_at(2026, 11, 3, 12))
    assert old.confidence == 'document'
    assert new.confidence == 'manual'
    assert new.components['energy'] == pytest.approx(0.700)
    assert new.source == 'umowa'


# ── strefy wersjonowane w czasie ─────────────────────────────────────────────

def test_zones_history_applies_by_date(catalog):
    import copy
    cat = copy.deepcopy(catalog)
    new_zones = copy.deepcopy(cat['zones'])
    new_zones['G12w']['peak']['hours'] = [[7, 13]]
    new_zones['G12w']['offpeak']['hours'] = [[13, 7]]
    cat['zones_history'] = [{'valid_from': '2026-11-01', 'zones': new_zones}]
    m = TariffModule(cat)
    assert m.zone_at(_at(2026, 10, 29, 6)) == 'peak'
    assert m.zone_at(_at(2026, 11, 3, 6)) == 'offpeak'
    assert m.zone_at(_at(2026, 11, 3, 8)) == 'peak'


# ── harmonogram i widok dla starszych odbiorców ─────────────────────────────

def test_schedule_hourly_points(g12w):
    s = g12w.schedule(_at(2026, 10, 29, 12), 48)
    assert len(s) == 48 and all(isinstance(p, PricePoint) for p in s)
    assert s[0].hour == '2026-10-29T12' and s[-1].hour == '2026-10-31T11'
    assert s[0].zone == 'peak' and s[1].zone == 'offpeak'            # szczyt kończy się o 13:00


def test_next_change(g12w):
    nxt = g12w.next_change(_at(2026, 10, 29, 12))
    assert nxt == {'at': '2026-10-29T13', 'zone': 'offpeak'}


def test_legacy_rates_g12w_has_the_thirteen_fields(g12w):
    r = g12w.legacy_rates(date(2026, 10, 29))
    assert round(r['peak_gross'], 4) == 1.2304 and round(r['offpeak_gross'], 4) == 0.6306
    assert r['fixed_total_net'] == pytest.approx(4.56 + 10.86 + 24.05)
    assert r['dist_jakosciowa_net'] == pytest.approx(0.0332)
    assert set(r) == {'energy_peak_net', 'energy_offpeak_net', 'dist_var_peak_net', 'dist_var_offpeak_net',
                      'dist_jakosciowa_net', 'dist_oze_net', 'dist_kogeneracja_net',
                      'fixed_mocowa_net', 'fixed_abonament_net', 'fixed_stalysieciowy_net',
                      'peak_gross', 'offpeak_gross', 'fixed_total_net'}
