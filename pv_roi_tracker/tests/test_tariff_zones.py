"""Testy tariff_zones.py — szczyt wg taryfy obowiązującej w dniu (okresy z tariff_history)."""
from datetime import datetime

import pytest

from pv_roi_tracker import battery_sim, tariff_zones
from pv_roi_tracker.tariff_catalog import load_catalog


@pytest.fixture(scope="module")
def cat():
    return load_catalog()


def _dt(day, hour):
    return datetime(2026, 6, day, hour)   # 1.06.2026 = poniedziałek, 6.06 = sobota


def test_pusta_historia_to_g12w(cat):
    fn = tariff_zones.peak_fn([], cat)
    for d in (1, 6):
        for h in range(24):
            assert fn(_dt(d, h)) == battery_sim.is_peak(_dt(d, h))


def test_zmiana_na_g13_dzien_przed_i_po(cat):
    periods = [{'from': None, 'tariff': 'G12w'}, {'from': '2026-06-03', 'tariff': 'G13'}]
    fn = tariff_zones.peak_fn(periods, cat)
    # 2.06 (wtorek) jeszcze G12w: 14:00 to dolina, 07:00 szczyt
    assert fn(_dt(2, 14)) is False
    assert fn(_dt(2, 20)) is True
    # 3.06 (środa) już G13: lato — szczyty z katalogu, reszta poza szczytem
    g13 = cat['zones']['G13']['summer']
    m_a, m_b = g13['morning_peak']
    e_a, e_b = g13['evening_peak']
    assert fn(_dt(3, m_a)) is True
    assert fn(_dt(3, m_b)) is False
    assert fn(_dt(3, e_a)) is True
    assert fn(_dt(3, e_b)) is False
    assert fn(_dt(3, 15)) is False      # other, w G12w byłby szczyt


def test_weekend_g12w_i_g13_poza_szczytem(cat):
    for t in ('G12w', 'G13'):
        fn = tariff_zones.peak_fn([{'from': None, 'tariff': t}], cat)
        assert all(fn(_dt(6, h)) is False for h in range(24))


def test_g12_szczyt_z_katalogu(cat):
    fn = tariff_zones.peak_fn([{'from': None, 'tariff': 'G12'}], cat)
    assert fn(_dt(1, 7)) is True and fn(_dt(1, 14)) is False and fn(_dt(1, 23)) is False
    assert fn(_dt(6, 7)) is True        # G12 liczy strefę dzienną także w weekend


def test_g11_zawsze_szczyt(cat):
    fn = tariff_zones.peak_fn([{'from': None, 'tariff': 'G11'}], cat)
    assert all(fn(_dt(d, h)) for d in (1, 6) for h in (0, 12, 23))


def test_blad_katalogu_nie_jest_polykany():
    fn = tariff_zones.peak_fn([{'from': None, 'tariff': 'G13'}], {'zones': {'G13': {}}})
    with pytest.raises((ValueError, KeyError)):
        fn(_dt(1, 8))
