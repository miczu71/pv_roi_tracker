"""
Katalog taryf Tauron (tariffs/tauron.json) — okresy cennika, strefy czasowe, stawki.

Czysta logika bez I/O poza wczytaniem JSON. Stawki netto (zł/kWh, zł/mies.), VAT osobno
(`catalog["_meta"]["vat"]`). Wartość `null` w katalogu oznacza brak danych i NIGDY nie
jest cicho zamieniana na zero — `rates_per_kwh` rzuca ValueError.
"""
from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .battery_sim import polish_holidays

_holidays = lru_cache(maxsize=None)(polish_holidays)

logger = logging.getLogger(__name__)

DEFAULT_CATALOG_PATH = Path(__file__).parent / 'tariffs' / 'tauron.json'

TARIFFS = ('G11', 'G12', 'G12w', 'G13', 'G13s')


@dataclass
class TariffSettings:
    """Ustawienia użytkownika wpływające na opłaty stałe (kalibracja z fakturami w E2)."""
    phases: int = 3
    capacity_bracket: str = 'gt2800'      # lt500 | 500_1200 | 1200_2800 | gt2800
    sales_fee_net: float = 0.0            # opłata handlowa netto zł/mies.
    sales_fee_g13s_net: Optional[float] = None  # opłata handlowa G13s; None = z okresu cennika


def load_catalog(path: Optional[Path] = None) -> dict:
    p = Path(path) if path else DEFAULT_CATALOG_PATH
    with open(p, encoding='utf-8') as f:
        return json.load(f)


# ── okresy ───────────────────────────────────────────────────────────────────

def _deep_merge(base: dict, extra: dict) -> dict:
    """Scala `extra` w `base` rekurencyjnie (zwraca nowy słownik)."""
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _expand(catalog: dict, entry: dict) -> dict:
    """Rozwija wpis z `inherits`/`override` do pełnego okresu."""
    inherits = entry.get('inherits')
    if not inherits:
        return copy.deepcopy(entry)
    base = next((p for p in catalog['periods'] if p['valid_from'] == inherits), None)
    if base is None:
        raise LookupError(f'okres bazowy {inherits} (inherits) nie istnieje')
    full = _deep_merge(_expand(catalog, base), entry.get('override', {}))
    for k in ('valid_from', 'valid_to', 'confirmed', 'note'):
        if k in entry:
            full[k] = entry[k]
    full.pop('inherits', None)
    full.pop('override', None)
    return full


def period_for(catalog: dict, d: date, overrides: Optional[dict] = None) -> dict:
    """Okres cennika obowiązujący w dniu `d` (kopia, z polem `confirmed`)."""
    iso = d.isoformat()
    for entry in catalog['periods']:
        if entry['valid_from'] <= iso <= entry['valid_to']:
            period = _expand(catalog, entry)
            period['confirmed'] = bool(period.get('confirmed', False))
            if overrides:
                period = _deep_merge(period, overrides)
            return period
    raise LookupError(f'brak okresu cennika dla {iso}')


# ── strefy ───────────────────────────────────────────────────────────────────

def _in_hours(hour: int, start: int, end: int) -> bool:
    """Przedział [start, end); gdy start > end przechodzi przez północ."""
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _is_day_off(d: date) -> bool:
    return d.weekday() >= 5 or d in _holidays(d.year)


def _g13_season(zones: dict, month: int) -> dict:
    for name in ('summer', 'winter'):
        if month in zones[name]['months']:
            return zones[name]
    raise ValueError(f'G13: brak sezonu dla miesiąca {month}')


def _g13s_season(zones: dict, month: int) -> tuple:
    for name in ('summer', 'winter'):
        if month in zones[name]['months']:
            return name, zones[name]
    raise ValueError(f'G13s: brak sezonu dla miesiąca {month}')


def zone_for(tariff: str, dt: datetime, catalog: dict) -> str:
    """Nazwa strefy taryfy `tariff` dla godziny `dt` (czas lokalny)."""
    if tariff not in TARIFFS:
        raise ValueError(f'nieznana taryfa: {tariff}')
    zones = catalog['zones'][tariff]
    h = dt.hour
    d = dt.date()
    if tariff == 'G11':
        return 'all'
    if tariff == 'G12':
        for name, spec in zones.items():
            if any(_in_hours(h, a, b) for a, b in spec['hours']):
                return name
        raise ValueError(f'G12: brak strefy dla godziny {h}')
    if tariff == 'G13s':
        daytype = 'dayoff' if _is_day_off(d) else 'workday'
        season_name, season = _g13s_season(zones, dt.month)
        for band in ('offpeak', 'peak', 'night'):
            if any(_in_hours(h, a, b) for a, b in season[band]):
                return f'{season_name}_{daytype}_{band}'
        raise ValueError(f'G13s: brak pasma dla godziny {h}')
    if tariff == 'G12w':
        if _is_day_off(d):
            return 'offpeak'
        for name, spec in zones.items():
            if any(_in_hours(h, a, b) for a, b in spec['hours']):
                return name
        raise ValueError(f'G12w: brak strefy dla godziny {h}')
    # G13
    if _is_day_off(d):
        return 'other'
    season = _g13_season(zones, dt.month)
    if _in_hours(h, *season['morning_peak']):
        return 'morning_peak'
    if _in_hours(h, *season['evening_peak']):
        return 'evening_peak'
    return 'other'


# ── stawki ───────────────────────────────────────────────────────────────────

def _zone_value(table: dict, tariff: str, zone: str, label: str) -> float:
    try:
        v = table[tariff][zone]
    except KeyError as e:
        raise ValueError(f'{label}: brak stawki {tariff}/{zone}') from e
    if v is None:
        raise ValueError(f'{label}: stawka {tariff}/{zone} ma wartość null (brak danych)')
    return float(v)


def _scalar(period: dict, key: str) -> float:
    v = period.get(key)
    if v is None:
        raise ValueError(f'{key}: brak wartości (null)')
    return float(v)


def rates_per_kwh(tariff: str, period: dict, zone: str) -> dict:
    """Stawki zmienne netto zł/kWh: energy, dist, fees (jakościowa + OZE + kogeneracja + akcyza)."""
    return {
        'energy': _zone_value(period['energy_net'], tariff, zone, 'energy_net'),
        'dist': _zone_value(period['dist_var_net'], tariff, zone, 'dist_var_net'),
        'fees': (_scalar(period, 'quality_net') + _scalar(period, 'oze_net')
                 + _scalar(period, 'cogeneration_net') + _scalar(period, 'excise_net')),
    }


def _transitional_key(bracket: str) -> str:
    if bracket in ('lt500', '500_1200'):
        return bracket
    return 'gt1200'


def _g13s_sales_fee(period: dict, s: TariffSettings) -> float:
    if s.sales_fee_g13s_net is not None:
        return float(s.sales_fee_g13s_net)
    v = (period['fixed_net'].get('sales_fee_by_tariff') or {}).get('G13s')
    if v is None:
        raise ValueError('G13s: brak opłaty handlowej w okresie cennika (sales_fee_by_tariff)')
    return float(v)


def fixed_monthly(period: dict, settings: Optional[TariffSettings] = None,
                  tariff: Optional[str] = None) -> dict:
    """Opłaty stałe netto zł/mies. Brak pola `transitional_by_annual_kwh` w okresie → 0.

    `tariff` wpływa tylko na opłatę handlową: G13s ma własną (zastępuje globalną `sales_fee_net`).
    """
    s = settings or TariffSettings()
    fx = period['fixed_net']
    network = fx['network_3ph'] if s.phases == 3 else fx['network_1ph']
    transitional = 0.0
    tbl = fx.get('transitional_by_annual_kwh')
    if tbl:
        transitional = float(tbl[_transitional_key(s.capacity_bracket)])
    return {
        'network': float(network),
        'subscription_dist': float(fx['subscription_dist']),
        'capacity': float(fx['capacity_by_annual_kwh'][s.capacity_bracket]),
        'transitional': transitional,
        'sales_fee': (_g13s_sales_fee(period, s) if tariff == 'G13s'
                      else float(s.sales_fee_net)),
    }
