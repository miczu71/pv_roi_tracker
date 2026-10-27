"""
Harmonogram mostów JIT (E6a): gdzie w taryfie strefowej są szczyty, jakie tanie okno je
poprzedza (most) i czy ładowanie z sieci w tym oknie się opłaca (marża na kWh).

Czysta logika — bez I/O. Ceny godzin z `tariff_battery.make_price_fn` (klucz `YYYY-MM-DDTHH`,
wynik `(cena_brutto_kwh, is_peak)` albo None). Ile ładować, liczy JIT w HA — tu tylko okna i opłacalność.
Oś czasu godzinowa, naiwna lokalna (bez DST); konwersja na ISO z TZ w `build_schedule`.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from . import battery_sim
from .battery_sim import BatteryConfig
from .tariff_battery import make_price_fn

TARIFFS = ('G12w', 'G13', 'G13s')
MIN_MARGIN_PLN_KWH = 0.0
MAX_LOOKBACK_H = 96
_MAX_PEAK_H = 48   # bezpiecznik pętli szczytu
_TZ = ZoneInfo('Europe/Warsaw')
_HOUR = timedelta(hours=1)


def _key(dt: datetime) -> str:
    return dt.strftime('%Y-%m-%dT%H')


def bridges(tariff: str, day: date, catalog: dict, cfg: BatteryConfig,
            overrides: Optional[dict] = None) -> dict:
    """{'peaks': [{'start','end','avg_pln_kwh'}], 'bridges': [{'charge_start','charge_end','gap_end',
    'cheap_pln_kwh','peak_pln_kwh','margin_pln_kwh','profitable'}]} dla szczytów zaczynających się w `day`.
    Czasy: naiwny lokalny `datetime` (konwersja na ISO z TZ robi build_schedule)."""
    price_fn = make_price_fn(tariff, catalog, None, None, overrides)
    midnight = datetime(day.year, day.month, day.day)
    peaks: list = []
    out: list = []
    per_kwh_cost = battery_sim.throughput_cost_kwh(cfg)
    for h in range(24):
        start = midnight + timedelta(hours=h)
        cur = price_fn(_key(start))
        if cur is None or not cur[1]:
            continue
        prev = price_fn(_key(start - _HOUR))
        if prev is not None and prev[1]:
            continue   # szczyt zaczął się wcześniej (nie w `day`)
        prices: list = []
        end = start
        complete = True
        while len(prices) < _MAX_PEAK_H:
            p = price_fn(_key(end))
            if p is None:
                complete = False
                break
            if not p[1]:
                break
            prices.append(p[0])
            end += _HOUR
        if not complete or not prices:
            continue
        peak_avg = sum(prices) / len(prices)
        peaks.append({'start': start, 'end': end, 'avg_pln_kwh': peak_avg})

        window: list = []
        t = start - _HOUR
        ok = True
        while len(window) < MAX_LOOKBACK_H:
            p = price_fn(_key(t))
            if p is None:
                ok = False
                break
            if p[1]:
                break
            window.append(p[0])
            t -= _HOUR
        if not ok or not window:
            continue
        charge_start = start - _HOUR * len(window)
        cheap = min(window)
        margin = peak_avg - (cheap / cfg.roundtrip_eff + per_kwh_cost)
        out.append({'charge_start': charge_start, 'charge_end': start, 'gap_end': end,
                    'cheap_pln_kwh': cheap, 'peak_pln_kwh': peak_avg,
                    'margin_pln_kwh': margin, 'profitable': margin > MIN_MARGIN_PLN_KWH})
    return {'peaks': peaks, 'bridges': out}


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=_TZ).isoformat()


def _iso_day(res: dict) -> dict:
    return {
        'peaks': [{**p, 'start': _iso(p['start']), 'end': _iso(p['end'])} for p in res['peaks']],
        'bridges': [{**b, 'charge_start': _iso(b['charge_start']), 'charge_end': _iso(b['charge_end']),
                     'gap_end': _iso(b['gap_end'])} for b in res['bridges']],
    }


def build_schedule(today: date, catalog: dict, cfg: BatteryConfig, overrides=None) -> dict:
    """{'generated_at', 'roundtrip_eff', 'throughput_cost_pln_kwh',
        'schedules': {taryfa: {'YYYY-MM-DD': <wynik bridges>}}}  (dni: today, today+1)"""
    days = (today, today + timedelta(days=1))
    return {
        'generated_at': datetime.now(_TZ).isoformat(timespec='seconds'),
        'roundtrip_eff': cfg.roundtrip_eff,
        'throughput_cost_pln_kwh': battery_sim.throughput_cost_kwh(cfg),
        'schedules': {t: {d.isoformat(): _iso_day(bridges(t, d, catalog, cfg, overrides)) for d in days}
                      for t in TARIFFS},
    }
