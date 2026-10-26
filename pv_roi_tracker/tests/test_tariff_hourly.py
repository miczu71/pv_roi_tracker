"""Testy hourly_series / zone_map / cost_by_zone / cumulative_diff (tariff_sim, E1)."""
from datetime import date

import pytest

from pv_roi_tracker.tariff_catalog import TariffSettings, load_catalog
from pv_roi_tracker.tariff_sim import (
    ALL_TARIFFS, cost_by_zone, cumulative_diff, hourly_series, simulate_month, zone_map)


@pytest.fixture(scope="module")
def cat():
    return load_catalog()


def _zones(series, tariff, day):
    return {r['key'][-2:]: r['zone'] for r in series[tariff]['hours'] if r['key'].startswith(day)}


def test_kazda_godzina_doby_nawet_bez_poboru(cat):
    s = hourly_series(date(2026, 3, 10), date(2026, 3, 11), {}, cat)
    assert set(s) == set(ALL_TARIFFS)
    for n in ('G11', 'G12', 'G12w', 'G13'):
        hrs = s[n]['hours']
        assert s[n]['available'] is True and s[n]['reason'] is None
        assert len(hrs) == 48
        assert hrs[0]['key'] == '2026-03-10T00' and hrs[-1]['key'] == '2026-03-11T23'
        assert all(r['kwh'] == 0 and r['cost_net'] == 0 for r in hrs)


def test_g12_granice_6_13_15_22(cat):
    z = _zones(hourly_series(date(2026, 3, 10), date(2026, 3, 10), {}, cat, tariffs=('G12',)),
               'G12', '2026-03-10')
    assert z['05'] == 'night' and z['06'] == 'day' and z['12'] == 'day'
    assert z['13'] == 'night' and z['14'] == 'night' and z['15'] == 'day'
    assert z['21'] == 'day' and z['22'] == 'night'


def test_g12w_robocze_weekend_swieto(cat):
    s = hourly_series(date(2026, 11, 9), date(2026, 11, 15), {}, cat, tariffs=('G12w',))
    assert _zones(s, 'G12w', '2026-11-09')['10'] == 'peak'      # poniedziałek
    assert _zones(s, 'G12w', '2026-11-10')['14'] == 'offpeak'
    assert _zones(s, 'G12w', '2026-11-11')['10'] == 'offpeak'   # święto
    assert _zones(s, 'G12w', '2026-11-14')['10'] == 'offpeak'   # sobota


def test_g13_przelom_sezonow(cat):
    s = hourly_series(date(2026, 3, 31), date(2026, 4, 1), {}, cat, tariffs=('G13',))
    assert _zones(s, 'G13', '2026-03-31')['16'] == 'evening_peak'   # zima 16-21
    assert _zones(s, 'G13', '2026-04-01')['16'] == 'other'          # lato 19-22
    assert _zones(s, 'G13', '2026-04-01')['19'] == 'evening_peak'
    s = hourly_series(date(2026, 9, 30), date(2026, 10, 1), {}, cat, tariffs=('G13',))
    assert _zones(s, 'G13', '2026-09-30')['19'] == 'evening_peak'
    assert _zones(s, 'G13', '2026-09-30')['16'] == 'other'
    assert _zones(s, 'G13', '2026-10-01')['16'] == 'evening_peak'
    assert _zones(s, 'G13', '2026-10-01')['21'] == 'other'


def test_ceny_wiersza_statycznego(cat):
    r = hourly_series(date(2026, 3, 10), date(2026, 3, 10), {'2026-03-10T10': 2.0}, cat,
                      tariffs=('G12w',))['G12w']['hours'][10]
    net = 0.6220 + 0.3298 + 0.0332 + 0.0073 + 0.0030 + 0.005
    assert r['net_per_kwh'] == pytest.approx(net)
    assert r['gross_per_kwh'] == pytest.approx(net * 1.23)
    assert r['cost_net'] == pytest.approx(2 * net)
    assert r['cost_gross'] == pytest.approx(2 * net * 1.23)
    assert r['fallback'] is False


def test_dyn_a_cena_rdn_i_fallback_tydzien_wstecz(cat):
    key = '2026-05-13T10'
    rdn = {'2026-05-06T10': 300.0}       # tylko tydzień wcześniej
    r = hourly_series(date(2026, 5, 13), date(2026, 5, 13), {key: 1.0}, cat,
                      tariffs=('dyn_a',), rdn=rdn)['dyn_a']['hours'][10]
    assert r['fallback'] is True
    net = 0.3 + 0.0892 + 0.3298 + 0.0332 + 0.0073 + 0.0030 + 0.005 
    assert r['net_per_kwh'] == pytest.approx(net, abs=1e-6)
    assert r['zone'] == 'peak'
    r2 = hourly_series(date(2026, 5, 13), date(2026, 5, 13), {key: 1.0}, cat,
                       tariffs=('dyn_a',), rdn={key: 400.0})['dyn_a']['hours'][10]
    assert r2['fallback'] is False and r2['net_per_kwh'] > r['net_per_kwh']


def test_dyn_a_brak_rdn_none(cat):
    s = hourly_series(date(2026, 5, 13), date(2026, 5, 13), {'2026-05-13T10': 3.0}, cat,
                      tariffs=('dyn_a',))['dyn_a']
    r = s['hours'][10]
    assert s['available'] is True
    assert r['net_per_kwh'] is None and r['gross_per_kwh'] is None
    assert r['cost_net'] is None and r['cost_gross'] is None
    assert r['zone'] == 'peak' and r['kwh'] == 3.0


def test_dyn_a_przed_2026_04_niedostepna(cat):
    s = hourly_series(date(2026, 3, 10), date(2026, 3, 12), {}, cat, tariffs=('dyn_a',))['dyn_a']
    assert s['available'] is False and 'od 2026-04-01' in s['reason'] and s['hours'] == []


def test_dyn_b_kompas_i_brak_strefy(cat):
    key = '2026-05-13T10'
    s = hourly_series(date(2026, 5, 13), date(2026, 5, 13), {key: 1.0}, cat,
                      tariffs=('dyn_b',), kompas={key: 'S2'})['dyn_b']
    r = s['hours'][10]
    assert r['zone'] == 'S2' and r['net_per_kwh'] > 0.502
    other = s['hours'][11]
    assert other['zone'] is None and other['net_per_kwh'] is None


def test_dyn_b_przed_oferta_niedostepna(cat):
    s = hourly_series(date(2025, 12, 1), date(2025, 12, 2), {}, cat, tariffs=('dyn_b',))['dyn_b']
    assert s['available'] is False and s['hours'] == []


def test_katalog_null_rzuca_valueerror(cat):
    import copy
    c = copy.deepcopy(cat)
    c['dynamic_sales']['margin_net_per_kwh'] = None
    with pytest.raises(ValueError):
        hourly_series(date(2026, 5, 13), date(2026, 5, 13), {}, c, tariffs=('dyn_a',), rdn={'2026-05-13T10': 1.0})


def test_suma_cost_by_zone_rowna_simulate_month(cat):
    import random
    rnd = random.Random(7)
    imp = {f'2026-03-{d:02d}T{h:02d}': round(rnd.random(), 3)
           for d in range(1, 32) for h in range(24) if rnd.random() < 0.6}
    st = TariffSettings()
    s = hourly_series(date(2026, 3, 1), date(2026, 3, 31), imp, cat, settings=st)
    cz = cost_by_zone(s, cat, settings=st)
    sim = simulate_month('2026-03', imp, cat, st)
    for n in ('G11', 'G12', 'G12w', 'G13'):
        assert cz[n]['total_gross'] == pytest.approx(sim[n]['gross'], abs=0.01)
        assert cz[n]['missing_kwh'] == 0
        assert sum(z['kwh'] for z in cz[n]['zones'].values()) == pytest.approx(sim[n]['kwh'])
        assert cz[n]['fixed_gross'] == pytest.approx(sim[n]['fixed'] * 1.23, abs=0.01)


def test_cost_by_zone_braki_poza_strefami(cat):
    key = '2026-05-13T10'
    s = hourly_series(date(2026, 5, 13), date(2026, 5, 13), {key: 3.0}, cat, tariffs=('dyn_a',))
    cz = cost_by_zone(s, cat)['dyn_a']
    assert cz['missing_kwh'] == 3.0 and cz['zones'] == {}


def test_cumulative_diff_sumuje_narastajaco_i_pomija_braki(cat):
    imp = {'2026-03-10T10': 1.0, '2026-03-11T10': 2.0, '2026-03-12T10': 1.0}
    s = hourly_series(date(2026, 3, 10), date(2026, 3, 12), imp, cat, tariffs=('G11', 'G12w'))
    d = cumulative_diff(s, 'G11', 'G12w')
    assert d['days'] == ['2026-03-10', '2026-03-11', '2026-03-12']
    per = [s['G11']['hours'][i * 24 + 10]['cost_gross'] - s['G12w']['hours'][i * 24 + 10]['cost_gross']
           for i in range(3)]
    assert d['diff_gross'][0] == pytest.approx(per[0])
    assert d['diff_gross'][1] == pytest.approx(per[0] + per[1])
    assert d['diff_gross'][2] == pytest.approx(sum(per))
    assert d['skipped_kwh'] == 0
    s2 = hourly_series(date(2026, 5, 13), date(2026, 5, 13), {'2026-05-13T10': 2.0}, cat,
                       tariffs=('G11', 'dyn_a'))
    d2 = cumulative_diff(s2, 'G11', 'dyn_a')
    assert d2['skipped_kwh'] == 2.0 and d2['diff_gross'] == [0.0]


def test_zone_map_24_komorki_na_dzien(cat):
    s = hourly_series(date(2026, 3, 10), date(2026, 3, 11), {}, cat)
    zm = zone_map(s)
    assert zm['G12']['days'] == ['2026-03-10', '2026-03-11']
    cells = zm['G12']['cells']['2026-03-10']
    assert len(cells) == 24
    assert cells[6]['zone'] == 'day' and cells[5]['zone'] == 'night'
    assert set(cells[0]) == {'zone', 'net_per_kwh', 'gross_per_kwh'}
    assert zm['dyn_a']['days'] == []     # niedostępna przed 2026-04


def test_dst_jeden_wiersz_na_klucz(cat):
    s = hourly_series(date(2026, 10, 25), date(2026, 10, 25), {'2026-10-25T02': 1.5}, cat,
                      tariffs=('G11',))['G11']['hours']
    assert len(s) == 24 and [r['key'] for r in s].count('2026-10-25T02') == 1
    assert zone_map({'G11': {'available': True, 'reason': None, 'hours': s}})['G11']['cells'][
        '2026-10-25'][2]['zone'] == 'all'
