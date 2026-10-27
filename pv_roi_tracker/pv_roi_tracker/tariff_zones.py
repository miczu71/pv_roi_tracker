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
