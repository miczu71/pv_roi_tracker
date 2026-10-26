"""
Wariant C symulacji taryf: planowanie ładowania magazynu z sieci (arbitraż strefowy).

Każda doba lokalna to niezależny problem programowania dynamicznego (DP) po energii
zmagazynowanej na siatce `step_kwh`: start doby = dolny próg SoC, koniec dowolny, jedna akcja
na godzinę (ładuj albo rozładuj). Limit cykli (energia ładowana z sieci na dobę) realizuje
relaksacja Lagrange'a — kara λ za kWh z sieci, dobierana bisekcją; `_TIE_BREAK` lekko różnicuje
karę po godzinie, by rozbić remisy identycznych cykli w bisekcji λ.
`charge_hours` to godziny ładowania Z SIECI (ładowanie z PV nie jest planem okien).
`simulate_variant_c`: C = max(DP, polityka B) na dobę, więc C >= B z resetem SoC co dobę.
`days_total` w planach = doby grupy mające choć jedną cenę; `days` = doby z jakąkolwiek akcją.

Czysta logika, bez I/O, bez zależności zewnętrznych (czysty Python).
"""
from __future__ import annotations

import dataclasses
import math
from datetime import date
from statistics import mean

from .battery_sim import simulate_s1, throughput_cost_kwh
from .tariff_battery import make_price_fn
from .tariff_catalog import _is_day_off

SOC_FLOOR = 0.16   # rezerwa backupu (dolny SoC), zakres cykli 16→100%
GRID_TARIFFS = ('G12', 'G12w', 'G13', 'G13s')

_EPS = 1e-12
_MAX_BISECT = 20
_TIE_BREAK = 2e-3


def _zero_result() -> dict:
    return {'savings_pln': 0.0, 'wear_pln': 0.0, 'grid_charged_kwh': 0.0,
            'pv_charged_kwh': 0.0, 'discharged_kwh': 0.0,
            'charge_hours': [], 'discharge_hours': []}


def _run_dp(hours: list, n: int, lam: float):
    """Jedna DP dla kary `lam`. `hours` = lista (idx, price, kc, kd, cval, xgrid, xpv, dval).
    Zwraca (ścieżka [(idx, delta_steps)], wartość z karą)."""
    neg = float('-inf')
    best = [neg] * (n + 1)
    best[0] = 0.0
    prevs = []
    nh = len(hours)
    for pos, (_idx, _price, kc, kd, cval, xgrid, _xpv, dval) in enumerate(hours):
        # drobne różnicowanie kary po godzinie rozbija remisy identycznych cykli
        # (bez tego bisekcja λ przeskakuje z N cykli od razu do zera)
        lam_h = lam * (1.0 + _TIE_BREAK * pos / nh)
        new = best[:]
        prev = list(range(n + 1))
        for s in range(n + 1):
            b = best[s]
            if b == neg:
                continue
            for k in range(1, min(kc, n - s) + 1):
                v = b + cval[k] - lam_h * xgrid[k]
                s2 = s + k
                if v > new[s2] + _EPS:
                    new[s2] = v
                    prev[s2] = s
            for k in range(1, min(kd, s) + 1):
                v = b + dval[k]
                s2 = s - k
                if v > new[s2] + _EPS:
                    new[s2] = v
                    prev[s2] = s
        prevs.append(prev)
        best = new
    s = max(range(n + 1), key=lambda i: (best[i], -i))
    total = best[s]
    path = []
    for h in range(len(hours) - 1, -1, -1):
        p = prevs[h][s]
        if p != s:
            path.append((h, s - p))
        s = p
    path.reverse()
    return path, total


def solve_day(prices, export_kwh, import_kwh, rcem, cfg, *, soc_floor=SOC_FLOOR,
              max_cycles=2.0, step_kwh=0.2) -> dict:
    """prices/export_kwh/import_kwh: równe listy (po godzinach doby, brak ceny = None);
    zwraca {'savings_pln','wear_pln','grid_charged_kwh','pv_charged_kwh','discharged_kwh',
            'charge_hours':[idx...], 'discharge_hours':[idx...]} (idx = pozycja w liście)."""
    usable = cfg.usable_kwh * (1.0 - soc_floor)
    if usable <= 0 or step_kwh <= 0 or cfg.power_kw <= 0:
        return _zero_result()
    n = max(1, int(round(usable / step_kwh)))
    step = usable / n
    eff = math.sqrt(max(cfg.roundtrip_eff, 1e-6))
    P = cfg.power_kw
    wear = throughput_cost_kwh(cfg)

    priced = [i for i, p in enumerate(prices) if p is not None]
    if not priced:
        return _zero_result()
    pmax = max(prices[i] for i in priced)
    pmin = min(prices[i] for i in priced)
    any_export = any((export_kwh[i] or 0.0) > 0 for i in priced)
    any_import = any((import_kwh[i] or 0.0) > 0 for i in priced)
    if not any_import:
        return _zero_result()
    if not any_export and pmax * eff * eff - pmin - wear <= 0:
        return _zero_result()

    kc_max = int(P * eff / step + 1e-9)
    hours = []
    for i in priced:
        price = prices[i]
        exp = max(export_kwh[i] or 0.0, 0.0)
        imp = max(import_kwh[i] or 0.0, 0.0)
        kd = int(min(imp, P) / eff / step + 1e-9)
        kc = kc_max
        pv_first = rcem <= price + wear
        cval = [0.0] * (kc + 1)
        xgrid = [0.0] * (kc + 1)
        xpv = [0.0] * (kc + 1)
        for k in range(1, kc + 1):
            x = k * step / eff
            xp = min(x, exp) if pv_first else 0.0
            xg = x - xp
            xpv[k] = xp
            xgrid[k] = xg
            cval[k] = -(xg * (price + wear) + xp * rcem)
        dval = [k * step * eff * price for k in range(kd + 1)]
        if kd == 0 and kc == 0:
            continue
        hours.append((i, price, kc, kd, cval, xgrid, xpv, dval))
    if not hours:
        return _zero_result()

    limit = max_cycles * usable + 1e-9

    def grid_stored(path) -> float:
        return sum(hours[h][5][d] for h, d in path if d > 0) * eff

    path, _ = _run_dp(hours, n, 0.0)
    if grid_stored(path) > limit:
        lo, hi = 0.0, pmax + wear + 1e-6
        best_path, _ = _run_dp(hours, n, hi)
        for _ in range(_MAX_BISECT):
            if hi - lo < 1e-5:
                break
            mid = (lo + hi) / 2
            p2, _ = _run_dp(hours, n, mid)
            if grid_stored(p2) <= limit:
                hi, best_path = mid, p2
            else:
                lo = mid
        path = best_path

    res = _zero_result()
    gain = 0.0
    for h, d in path:
        idx, price, _kc, _kd, cval, xgrid, xpv, dval = hours[h]
        if d > 0:
            if xgrid[d] > 0:
                res['charge_hours'].append(idx)
            res['grid_charged_kwh'] += xgrid[d]
            res['pv_charged_kwh'] += xpv[d]
            gain += cval[d]
        else:
            res['discharge_hours'].append(idx)
            res['discharged_kwh'] += -d * step * eff
            gain += dval[-d]
    res['wear_pln'] = wear * res['grid_charged_kwh']
    res['savings_pln'] = gain
    return res


def _season(ym: str) -> str:
    return 'summer' if 4 <= int(ym[5:7]) <= 9 else 'winter'


def simulate_variant_c(hours, cfg, catalog, rdn, kompas, rcem_by_ym, from_ym, to_ym,
                       overrides=None, *, soc_floor=SOC_FLOOR, max_cycles=2.0,
                       step_kwh=0.2, tariffs=GRID_TARIFFS) -> tuple[dict, dict]:
    """-> (monthly, plans); monthly = {ym: {taryfa: {savings_pln, wear_pln, grid_charged_kwh,
    pv_charged_kwh, discharged_kwh}}}; plans = {taryfa: {'summer_workday': {...}, ...}}.
    Ceny: tariff_battery.make_price_fn(t, catalog, rdn, kompas, overrides); rcem: rcem_by_ym.get(ym)
    lub średnia wartości (jak battery_sim.simulate_s1; 0.35 gdy pusto)."""
    cfg_b = dataclasses.replace(cfg, arbitrage_enabled=False,
                                usable_kwh=cfg.usable_kwh * (1 - soc_floor))
    rcem_fallback = round(mean(rcem_by_ym.values()), 4) if rcem_by_ym else 0.35
    days: dict = {}
    for key in sorted(k for k in hours if from_ym <= k[:7] <= to_ym):
        days.setdefault(key[:10], []).append(key)

    monthly: dict = {}
    plans: dict = {}
    for t in tariffs:
        price_fn = make_price_fn(t, catalog, rdn, kompas, overrides)
        groups: dict = {}
        for day, keys in days.items():
            prices = []
            for k in keys:
                p = price_fn(k)
                prices.append(None if p is None else p[0])
            if all(p is None for p in prices):
                continue
            ym = day[:7]
            rcem = rcem_by_ym.get(ym, rcem_fallback)
            res = solve_day(prices, [hours[k][0] for k in keys], [hours[k][1] for k in keys],
                            rcem, cfg, soc_floor=soc_floor, max_cycles=max_cycles,
                            step_kwh=step_kwh)
            row = monthly.setdefault(ym, {}).setdefault(t, {
                'savings_pln': 0.0, 'wear_pln': 0.0, 'grid_charged_kwh': 0.0,
                'pv_charged_kwh': 0.0, 'discharged_kwh': 0.0})
            # C = max(DP, polityka B) na dobę, więc C >= B z resetem SoC co dobę
            b_rows = simulate_s1(hours, cfg_b, {}, {ym: rcem}, keys=keys,
                                 price_fn=price_fn)
            if b_rows and b_rows[0]['savings_pln'] > res['savings_pln']:
                b = b_rows[0]
                res = {'savings_pln': b['savings_pln'], 'wear_pln': 0.0, 'grid_charged_kwh': 0.0,
                       'pv_charged_kwh': b['charged_kwh'], 'discharged_kwh': b['discharged_kwh'],
                       'charge_hours': [], 'discharge_hours': []}
            for f in row:
                row[f] += res[f]
            d = date(int(day[:4]), int(day[5:7]), int(day[8:10]))
            gk = f"{_season(ym)}_{'dayoff' if _is_day_off(d) else 'workday'}"
            g = groups.setdefault(gk, {'total': 0, 'acted': 0, 'saving': 0.0, 'grid': 0.0,
                                       'ch': {}, 'dis': {}})
            g['total'] += 1
            g['saving'] += res['savings_pln']
            g['grid'] += res['grid_charged_kwh']
            if res['charge_hours'] or res['discharge_hours']:
                g['acted'] += 1
            for i in set(res['charge_hours']):
                hr = int(keys[i][11:13])
                g['ch'][hr] = g['ch'].get(hr, 0) + 1
            for i in set(res['discharge_hours']):
                hr = int(keys[i][11:13])
                g['dis'][hr] = g['dis'].get(hr, 0) + 1
        plans[t] = {
            gk: {
                'charge_hours': sorted(h for h, c in g['ch'].items() if c >= 0.5 * g['total']),
                'discharge_hours': sorted(h for h, c in g['dis'].items() if c >= 0.5 * g['total']),
                'days': g['acted'],
                'days_total': g['total'],
                'avg_saving_pln': g['saving'] / g['total'],
                'avg_grid_kwh': g['grid'] / g['total'],
            } for gk, g in groups.items()
        }
    return monthly, plans
