"""Testy jit_schedule.py — mosty (tanie okno przed szczytem) i opłacalność ładowania z sieci."""
import copy
import dataclasses
import json
from datetime import date, datetime, timedelta

import pytest

from pv_roi_tracker import battery_sim, jit_schedule as js
from pv_roi_tracker.battery_sim import BatteryConfig
from pv_roi_tracker.tariff_battery import make_price_fn
from pv_roi_tracker.tariff_catalog import load_catalog, zone_for
from pv_roi_tracker.tariff_zones import PEAK_ZONES

CFG = BatteryConfig()


@pytest.fixture(scope="module")
def cat():
    return load_catalog()


def dt(y, m, d, h=0):
    return datetime(y, m, d, h)


def _expected_peaks(tariff, day, cat):
    """Zakresy godzin szczytu liczone ze `zone_for` (niezależnie od make_price_fn)."""
    out, cur = [], None
    for h in range(24):
        peak = zone_for(tariff, dt(day.year, day.month, day.day, h), cat) in PEAK_ZONES
        if peak and cur is None:
            cur = h
        if not peak and cur is not None:
            out.append((cur, h))
            cur = None
    if cur is not None:
        out.append((cur, 24))
    return out


def _peak_hours(res, day):
    return [((p['start'] - dt(day.year, day.month, day.day)) // timedelta(hours=1),
             (p['end'] - dt(day.year, day.month, day.day)) // timedelta(hours=1)) for p in res['peaks']]


def _avg(tariff, cat, start, end):
    f = make_price_fn(tariff, cat, None, None)
    n = int((end - start) / timedelta(hours=1))
    prices = [f((start + timedelta(hours=i)).strftime('%Y-%m-%dT%H'))[0] for i in range(n)]
    return sum(prices) / n, min(prices)


def _margin(peak, cheap, cfg=CFG):
    return peak - (cheap / cfg.roundtrip_eff + battery_sim.throughput_cost_kwh(cfg))


# ── G13 ──────────────────────────────────────────────────────────────────────

def test_g13_zima_dzien_roboczy(cat):
    day = date(2026, 12, 2)   # środa
    res = js.bridges('G13', day, cat, CFG)
    assert _peak_hours(res, day) == [(7, 13), (16, 21)] == _expected_peaks('G13', day, cat)
    b1, b2 = res['bridges']
    assert b1['charge_start'] == dt(2026, 12, 1, 21) and b1['charge_end'] == dt(2026, 12, 2, 7)
    assert b1['gap_end'] == dt(2026, 12, 2, 13)
    assert b2['charge_start'] == dt(2026, 12, 2, 13) and b2['charge_end'] == dt(2026, 12, 2, 16)
    assert b2['gap_end'] == dt(2026, 12, 2, 21)
    peak1, _ = _avg('G13', cat, dt(2026, 12, 2, 7), dt(2026, 12, 2, 13))
    peak2, _ = _avg('G13', cat, dt(2026, 12, 2, 16), dt(2026, 12, 2, 21))
    _, cheap1 = _avg('G13', cat, dt(2026, 12, 1, 21), dt(2026, 12, 2, 7))
    _, cheap2 = _avg('G13', cat, dt(2026, 12, 2, 13), dt(2026, 12, 2, 16))
    assert b1['peak_pln_kwh'] == pytest.approx(peak1) and b1['cheap_pln_kwh'] == pytest.approx(cheap1)
    assert b1['margin_pln_kwh'] == pytest.approx(_margin(peak1, cheap1))
    assert b2['margin_pln_kwh'] == pytest.approx(_margin(peak2, cheap2))
    assert [b['profitable'] for b in res['bridges']] == [False, True]


def test_g13_lato_dzien_roboczy(cat):
    day = date(2026, 6, 10)   # środa
    res = js.bridges('G13', day, cat, CFG)
    assert _peak_hours(res, day) == [(7, 13), (19, 22)] == _expected_peaks('G13', day, cat)
    b2 = res['bridges'][1]
    assert b2['charge_start'] == dt(2026, 6, 10, 13) and b2['charge_end'] == dt(2026, 6, 10, 19)
    assert b2['gap_end'] == dt(2026, 6, 10, 22)


# ── G12w ─────────────────────────────────────────────────────────────────────

def test_g12w_dzien_roboczy(cat):
    day = date(2026, 12, 2)
    res = js.bridges('G12w', day, cat, CFG)
    assert _peak_hours(res, day) == [(6, 13), (15, 22)] == _expected_peaks('G12w', day, cat)
    b1, b2 = res['bridges']
    assert (b1['charge_start'], b1['charge_end'], b1['gap_end']) == (
        dt(2026, 12, 1, 22), dt(2026, 12, 2, 6), dt(2026, 12, 2, 13))
    assert (b2['charge_start'], b2['charge_end'], b2['gap_end']) == (
        dt(2026, 12, 2, 13), dt(2026, 12, 2, 15), dt(2026, 12, 2, 22))
    assert [b['profitable'] for b in res['bridges']] == [True, True]


# ── G13s ─────────────────────────────────────────────────────────────────────

def test_g13s_sezonowe_okna_z_katalogu(cat):
    day = date(2026, 12, 2)   # zima: peak 7-10, 15-21, offpeak 10-15, night 21-7
    res = js.bridges('G13s', day, cat, CFG)
    assert _peak_hours(res, day) == [(7, 10), (15, 21)] == _expected_peaks('G13s', day, cat)
    b1, b2 = res['bridges']
    assert b1['charge_start'] == dt(2026, 12, 1, 21) and b1['charge_end'] == dt(2026, 12, 2, 7)
    assert b2['charge_start'] == dt(2026, 12, 2, 10) and b2['charge_end'] == dt(2026, 12, 2, 15)
    summer = js.bridges('G13s', date(2026, 6, 10), cat, CFG)
    assert _peak_hours(summer, date(2026, 6, 10)) == [(7, 9), (17, 21)]
    assert summer['bridges'][1]['charge_start'] == dt(2026, 6, 10, 9)


def test_g13s_minimum_po_roznych_strefach(cat):
    # sobota: okno = noc pt (workday_night) -> noc sb (dayoff_night), różne strefy w jednym oknie
    day = date(2026, 12, 5)
    b1 = js.bridges('G13s', day, cat, CFG)['bridges'][0]
    zones = {zone_for('G13s', b1['charge_start'] + timedelta(hours=i), cat)
             for i in range(int((b1['charge_end'] - b1['charge_start']) / timedelta(hours=1)))}
    assert len(zones) > 1
    _, cheap = _avg('G13s', cat, b1['charge_start'], b1['charge_end'])
    assert b1['cheap_pln_kwh'] == pytest.approx(cheap)


# ── dni wolne ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('tariff', ('G12w', 'G13'))
@pytest.mark.parametrize('day', [date(2026, 12, 5), date(2026, 12, 6), date(2026, 11, 11)])
def test_weekend_i_swieto_bez_szczytow(cat, tariff, day):
    assert js.bridges(tariff, day, cat, CFG) == {'peaks': [], 'bridges': []}


@pytest.mark.parametrize('day', [date(2026, 12, 5), date(2026, 12, 6), date(2026, 11, 11)])
def test_g13s_dni_wolne_maja_szczyty_z_katalogu_ale_nieoplacalne(cat, day):
    # katalog G13s ma strefy *_dayoff_peak (niższa cena, ale nadal szczyt) — wg zone_for, nie pusto
    res = js.bridges('G13s', day, cat, CFG)
    assert _peak_hours(res, day) == _expected_peaks('G13s', day, cat) != []
    assert res['bridges'] and not any(b['profitable'] for b in res['bridges'])


def test_poniedzialek_po_weekendzie_limit_96h(cat):
    day = date(2026, 12, 7)
    b1 = js.bridges('G13', day, cat, CFG)['bridges'][0]
    assert b1['charge_start'] == dt(2026, 12, 4, 21)   # piątek wieczór
    assert (b1['charge_end'] - b1['charge_start']) == timedelta(hours=58)
    assert (b1['charge_end'] - b1['charge_start']) <= timedelta(hours=js.MAX_LOOKBACK_H)


def test_lookback_obciety_do_96h(cat):
    # długi weekend: święto pn + wt przed szczytem w środę nie wydłuży okna ponad limit
    day = date(2026, 6, 10)
    b = js.bridges('G13', day, cat, CFG)['bridges'][0]
    assert b['charge_end'] - b['charge_start'] <= timedelta(hours=js.MAX_LOOKBACK_H)


# ── DST ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('first', [date(2026, 3, 27), date(2026, 10, 23)])
@pytest.mark.parametrize('tariff', js.TARIFFS)
def test_przelom_dst_bez_wyjatku_i_zgodny_ze_zone_for(cat, tariff, first):
    for i in range(6):   # obejmuje niedzielę 29.03 (23 h) i 25.10 (25 h) oraz poniedziałek po niej
        day = first + timedelta(days=i)
        res = js.bridges(tariff, day, cat, CFG)
        assert _peak_hours(res, day) == _expected_peaks(tariff, day, cat)
        assert len(res['bridges']) == len(res['peaks'])


# ── brak ceny ────────────────────────────────────────────────────────────────

def test_g13s_przed_oferta_bez_mostow(cat):
    assert js.bridges('G13s', date(2025, 12, 10), cat, CFG) == {'peaks': [], 'bridges': []}


def test_brak_ceny_w_oknie_pomija_most_ale_zostawia_szczyt(cat):
    c = copy.deepcopy(cat)
    c['available_from']['G13s'] = '2026-01-02'
    res = js.bridges('G13s', date(2026, 1, 2), c, CFG)   # okno wchodzi w 01.01 (brak ceny)
    assert _peak_hours(res, date(2026, 1, 2)) == [(7, 10), (15, 21)]
    assert [b['charge_start'] for b in res['bridges']] == [dt(2026, 1, 2, 10)]


# ── opłacalność ──────────────────────────────────────────────────────────────

def test_profitable_zalezy_od_sprawnosci_i_cykli(cat):
    day = date(2026, 12, 2)

    def morning(cfg):
        return js.bridges('G13', day, cat, cfg)['bridges'][0]

    assert morning(CFG)['profitable'] is False
    lossless = dataclasses.replace(CFG, roundtrip_eff=1.0, lifetime_cycles=1e9)
    assert morning(lossless)['profitable'] is True
    only_cycles = dataclasses.replace(CFG, lifetime_cycles=1e9)
    assert morning(only_cycles)['profitable'] is True
    only_eff = dataclasses.replace(CFG, roundtrip_eff=1.0)
    assert morning(only_eff)['margin_pln_kwh'] > morning(CFG)['margin_pln_kwh']
    evening_bad = dataclasses.replace(CFG, roundtrip_eff=0.3)
    assert js.bridges('G13', day, cat, evening_bad)['bridges'][1]['profitable'] is False
    assert js.bridges('G13', day, cat, CFG)['bridges'][1]['profitable'] is True


# ── build_schedule ───────────────────────────────────────────────────────────

def test_build_schedule_ksztalt_i_iso_z_tz(cat):
    out = js.build_schedule(date(2026, 12, 2), cat, CFG)
    assert out['roundtrip_eff'] == CFG.roundtrip_eff
    assert out['throughput_cost_pln_kwh'] == pytest.approx(battery_sim.throughput_cost_kwh(CFG))
    assert out['generated_at'].endswith(('+01:00', '+02:00'))
    assert set(out['schedules']) == set(js.TARIFFS)
    assert set(out['schedules']['G13']) == {'2026-12-02', '2026-12-03'}
    g13 = out['schedules']['G13']['2026-12-02']
    assert [b['profitable'] for b in g13['bridges']] == [False, True]
    assert g13['bridges'][0]['charge_start'] == '2026-12-01T21:00:00+01:00'
    assert g13['peaks'][0]['start'] == '2026-12-02T07:00:00+01:00'
    assert len(json.dumps(out, ensure_ascii=False)) < 16000


def test_build_schedule_lato_offset_plus_2(cat):
    out = js.build_schedule(date(2026, 6, 10), cat, CFG)
    assert out['schedules']['G13']['2026-06-10']['peaks'][0]['start'] == '2026-06-10T07:00:00+02:00'
