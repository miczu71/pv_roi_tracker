"""
Strefy szczytu według taryfy obowiązującej w danym dniu (okresy z `tariff_history`).

Czysta logika. `peak_fn(periods, catalog)` zwraca `datetime -> bool` (czy godzina jest w szczycie)
dla symulacji godzinowych (bateria, pompa ciepła). Brak okresów = dotychczasowe zachowanie G12w.
Błędy katalogu (ValueError z `zone_for`) nie są połykane.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable

from . import battery_sim, tariff_history
from .tariff_catalog import zone_for

PEAK_ZONES = frozenset({'peak', 'morning_peak', 'evening_peak',
                        'summer_workday_peak', 'summer_dayoff_peak',
                        'winter_workday_peak', 'winter_dayoff_peak'})
_G12_PEAK = 'day'

_ZONE_TARIFFS = ('G12', 'G13', 'G13s')
THREE_ZONE_TARIFFS = ('G13', 'G13s')


def three_zone_tariff_over(tariff_on: Callable, start, end):
    """Jedna definicja „okres trzystrefowy”: taryfa końca okresu, gdy początek I koniec są w G13/G13s
    (okres z częścią G12w — np. 01–18.11 — nie jest trzystrefowy), inaczej None.
    `tariff_on: date -> taryfa|None`. Pytanie „czy miesiąc ma jakieś dni G13” (LUB początek/koniec)
    to osobna decyzja — `TariffModule.has_three_zone_days`."""
    t_end = tariff_on(end)
    if t_end in THREE_ZONE_TARIFFS and tariff_on(start) in THREE_ZONE_TARIFFS:
        return t_end
    return None


def is_peak_zone(tariff: str, zone: str) -> bool:
    """Jedno źródło prawdy: strefa jest szczytem (G12: strefa dzienna `day` też jest szczytem)."""
    return zone in PEAK_ZONES or (tariff == 'G12' and zone == _G12_PEAK)


def peak_fn(periods: list, catalog: dict, default: str = 'G12w') -> Callable[[datetime], bool]:
    """`dt -> czy szczyt` wg taryfy z `periods` w dniu `dt` (brak okresu → `default`)."""
    def is_peak(dt: datetime) -> bool:
        t = (tariff_history.tariff_on(periods, dt.date()) if periods else None) or default
        if t == 'G11':
            return True
        if t in _ZONE_TARIFFS:
            zone = zone_for(t, dt, catalog)
            return is_peak_zone(t, zone)
        return battery_sim.is_peak(dt)
    return is_peak
