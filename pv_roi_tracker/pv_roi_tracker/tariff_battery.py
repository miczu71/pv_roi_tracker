"""
Wariant B symulacji taryf: ten sam wirtualny moduł magazynu (BatteryConfig z zakładki
Magazyn), ale uniknięty zakup wyceniany ceną danej taryfy w danej godzinie.

Czysta logika — bez I/O. Ceny godziny z tych samych funkcji katalogu co wariant A
(`period_for`, `zone_for`, `rates_per_kwh`). Arbitraż jest wyłączony: taryfy porównujemy
jednym, prostym scenariuszem. Godzina bez ceny (brak RDN / strefy Kompas / stawki w katalogu)
jest pomijana — moduł jej nie obsługuje, więc daje zero oszczędności.
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from . import battery_sim
from .tariff_catalog import period_for, rates_per_kwh, zone_for
from .tariff_sim import ALL_TARIFFS as TARIFFS, STATIC_TARIFFS, _dyn_b_price, _g13s_from, _required

_PEAK_ZONES = frozenset({'peak', 'morning_peak', 'evening_peak',
                         'summer_workday_peak', 'summer_dayoff_peak',
                         'winter_workday_peak', 'winter_dayoff_peak'})

PriceFn = Callable[[str], Optional[tuple]]


def _parse(key: str) -> datetime:
    return datetime(int(key[:4]), int(key[5:7]), int(key[8:10]), int(key[11:13]))


def make_price_fn(tariff: str, catalog: dict, rdn: Optional[dict], kompas: Optional[dict],
                  overrides: Optional[dict] = None) -> PriceFn:
    """`key -> (cena_brutto_kwh, is_peak)` lub None (godzina pominięta)."""
    rdn = rdn or {}
    kompas = kompas or {}
    vat = float(catalog['_meta']['vat'])
    sales = catalog.get('dynamic_sales') or {}
    margin = _required(sales, 'margin_net_per_kwh') if tariff == 'dyn_a' else 0.0
    fixed_b = _dyn_b_price(catalog, overrides) if tariff == 'dyn_b' else 0.0
    g13s_from = _g13s_from(catalog)
    periods: dict = {}

    def period_of(d: date) -> dict:
        p = periods.get(d)
        if p is None:
            p = periods[d] = period_for(catalog, d, overrides)
        return p

    def price_fn(key: str) -> Optional[tuple]:
        dt = _parse(key)
        if tariff == 'G13s' and g13s_from and dt.date().isoformat() < g13s_from:
            return None
        p = period_of(dt.date())
        if tariff in STATIC_TARIFFS:
            zone = zone_for(tariff, dt, catalog)
            r = rates_per_kwh(tariff, p, zone)
            return (r['energy'] + r['dist'] + r['fees']) * vat, zone in _PEAK_ZONES
        g12w = rates_per_kwh('G12w', p, zone_for('G12w', dt, catalog))
        if tariff == 'dyn_a':
            price = rdn.get(key)
            if price is None:
                price = rdn.get((dt - timedelta(days=7)).strftime('%Y-%m-%dT%H'))
            if price is None:
                return None
            return (price / 1000.0 + margin + g12w['dist'] + g12w['fees']) * vat, False
        if tariff == 'dyn_b':
            zone = kompas.get(key)
            s_rate = (p.get('dynamic_dist_var_net') or {}).get(zone) if zone else None
            if s_rate is None:
                return None
            return (fixed_b + float(s_rate) + g12w['fees']) * vat, False
        raise ValueError(f'nieznana taryfa: {tariff}')

    return price_fn


def simulate_variant_b(hours: dict, cfg, catalog: dict, rdn: Optional[dict],
                       kompas: Optional[dict], rcem_by_ym: dict, from_ym: str, to_ym: str,
                       overrides: Optional[dict] = None, soc_floor: float = 0.0) -> dict:
    """`{ym: {taryfa: {savings_pln, charged_kwh, discharged_kwh}}}` dla wszystkich taryf.
    `soc_floor` = ułamek pojemności zarezerwowany (dolny próg SoC), zmniejsza użyteczną pojemność."""
    cfg_b = dataclasses.replace(cfg, arbitrage_enabled=False,
                                usable_kwh=cfg.usable_kwh * (1 - soc_floor))
    keys = sorted(k for k in hours if from_ym <= k[:7] <= to_ym)
    out: dict = {}
    for t in TARIFFS:
        rows = battery_sim.simulate_s1(
            hours, cfg_b, {}, rcem_by_ym, keys=keys,
            price_fn=make_price_fn(t, catalog, rdn, kompas, overrides))
        for r in rows:
            out.setdefault(r['ym'], {})[t] = {
                'savings_pln': r['savings_pln'],
                'charged_kwh': r['charged_kwh'],
                'discharged_kwh': r['discharged_kwh'],
            }
    return out
