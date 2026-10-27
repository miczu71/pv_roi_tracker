"""
Wariant C symulacji taryf: planowanie ładowania magazynu z sieci (arbitraż strefowy).

Każda doba lokalna to problem programowania dynamicznego (DP) po energii zmagazynowanej na
siatce `step_kwh`: start doby = SoC z końca poprzedniej doby (gdy kolejna kalendarzowo, inaczej
dolny próg), koniec dowolny z wartością końcową `end_value` (zł/kWh), jedna akcja na godzinę
(ładuj albo rozładuj). Limit cykli (energia ładowana z sieci na dobę) realizuje
relaksacja Lagrange'a — kara λ za kWh z sieci, dobierana bisekcją; `_TIE_BREAK` lekko różnicuje
karę po godzinie, by rozbić remisy identycznych cykli w bisekcji λ.
`charge_hours` to godziny ładowania Z SIECI (ładowanie z PV nie jest planem okien).
`simulate_variant_c`: w każdym miesiącu C = lepszy z (DP z przenoszeniem SoC, ciągła polityka B).
`days_total` w sugestiach = doby grupy (w ostatnich 12 miesiącach) mające choć jedną cenę.
`suggest_plans`: prosta reguła do JIT (okno ładowania z sieci + cel SoC + strefy rozładowania) na grupę
sezon × typ dnia, oceniona symulatorem godzinowym zgodnym z `battery_sim.simulate_s1` na ostatnich 12 miesiącach.

Czysta logika, bez I/O, bez zależności zewnętrznych (czysty Python).
"""
from __future__ import annotations

import dataclasses
import math
from datetime import date, timedelta
from statistics import mean, median

from .battery_sim import simulate_s1, throughput_cost_kwh
from .tariff_battery import make_price_fn
from .tariff_catalog import _is_day_off

SOC_FLOOR = 0.16   # rezerwa backupu (dolny SoC), zakres cykli 16→100%
GRID_TARIFFS = ('G12', 'G12w', 'G13', 'G13s')
MIN_WORTH_PLN_YEAR = 20.0   # poniżej tej oszczędności rocznej sugestia jest „niewarta zachodu”
TARGET_STEP_PCT = 10        # krok celu SoC sugestii (%)

_EPS = 1e-12
_MAX_BISECT = 20
_TIE_BREAK = 2e-3


def _zero_result() -> dict:
    return {'savings_pln': 0.0, 'wear_pln': 0.0, 'grid_charged_kwh': 0.0,
            'pv_charged_kwh': 0.0, 'discharged_kwh': 0.0,
            'charge_hours': [], 'discharge_hours': [], 'end_soc_kwh': 0.0}


def _run_dp(hours: list, n: int, lam: float, s0: int = 0, end_bonus=None):
    """Jedna DP dla kary `lam`. `hours` = lista (idx, price, kc, kd, cval, xgrid, xpv, dval).
    `s0` = stan startowy (kroki siatki), `end_bonus[i]` = wartość stanu końcowego i.
    Zwraca (ścieżka [(idx, delta_steps)], wartość z karą)."""
    neg = float('-inf')
    best = [neg] * (n + 1)
    best[s0] = 0.0
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
    s = max(range(n + 1), key=lambda i: (best[i] + (end_bonus[i] if end_bonus else 0.0), -i))
    total = best[s] + (end_bonus[s] if end_bonus else 0.0)
    path = []
    for h in range(len(hours) - 1, -1, -1):
        p = prevs[h][s]
        if p != s:
            path.append((h, s - p))
        s = p
    path.reverse()
    return path, total


def solve_day(prices, export_kwh, import_kwh, rcem, cfg, *, soc_floor=SOC_FLOOR,
              max_cycles=2.0, step_kwh=0.2, soc0_kwh=0.0, end_value=0.0) -> dict:
    """prices/export_kwh/import_kwh: równe listy (po godzinach doby, brak ceny = None);
    zwraca {'savings_pln','wear_pln','grid_charged_kwh','pv_charged_kwh','discharged_kwh',
            'charge_hours':[idx...], 'discharge_hours':[idx...], 'end_soc_kwh'} (idx = pozycja w liście).
    `soc0_kwh` = energia w zakresie użytecznym na starcie doby, `end_value` = zł za 1 kWh
    zmagazynowanej na koniec doby."""
    usable = cfg.usable_kwh * (1.0 - soc_floor)
    if usable <= 0 or step_kwh <= 0 or cfg.power_kw <= 0:
        return _zero_result()
    n = max(1, int(round(usable / step_kwh)))
    step = usable / n
    eff = math.sqrt(max(cfg.roundtrip_eff, 1e-6))
    s0 = min(n, max(0, round(soc0_kwh / step)))
    end_bonus = [end_value * s * step * eff for s in range(n + 1)]

    def _idle() -> dict:
        r = _zero_result()
        r['end_soc_kwh'] = s0 * step
        return r
    P = cfg.power_kw
    wear = throughput_cost_kwh(cfg)

    priced = [i for i, p in enumerate(prices) if p is not None]
    if not priced:
        return _idle()
    pmax = max(prices[i] for i in priced)
    pmin = min(prices[i] for i in priced)
    any_export = any((export_kwh[i] or 0.0) > 0 for i in priced)
    any_import = any((import_kwh[i] or 0.0) > 0 for i in priced)
    if not any_import:
        return _idle()
    if not any_export and pmax * eff * eff - pmin - wear <= 0:
        return _idle()

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
        return _idle()

    limit = max_cycles * usable + 1e-9

    def grid_stored(path) -> float:
        return sum(hours[h][5][d] for h, d in path if d > 0) * eff

    path, _ = _run_dp(hours, n, 0.0, s0, end_bonus)
    if grid_stored(path) > limit:
        lo, hi = 0.0, pmax + wear + 1e-6
        best_path, _ = _run_dp(hours, n, hi, s0, end_bonus)
        for _ in range(_MAX_BISECT):
            if hi - lo < 1e-5:
                break
            mid = (lo + hi) / 2
            p2, _ = _run_dp(hours, n, mid, s0, end_bonus)
            if grid_stored(p2) <= limit:
                hi, best_path = mid, p2
            else:
                lo = mid
        path = best_path

    res = _zero_result()
    gain = 0.0
    end_steps = s0
    for h, d in path:
        idx, price, _kc, _kd, cval, xgrid, xpv, dval = hours[h]
        end_steps += d
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
    res['end_soc_kwh'] = end_steps * step
    return res


def simulate_variant_c(hours, cfg, catalog, rdn, kompas, rcem_by_ym, from_ym, to_ym,
                       overrides=None, *, soc_floor=SOC_FLOOR, max_cycles=2.0,
                       step_kwh=0.2, tariffs=GRID_TARIFFS, dist_tariff='G12w') -> dict:
    """-> monthly = {ym: {taryfa: {savings_pln, wear_pln, grid_charged_kwh,
    pv_charged_kwh, discharged_kwh}}}.
    Ceny: tariff_battery.make_price_fn(t, catalog, rdn, kompas, overrides); rcem: rcem_by_ym.get(ym)
    lub średnia wartości (jak battery_sim.simulate_s1; 0.35 gdy pusto)."""
    cfg_b = dataclasses.replace(cfg, arbitrage_enabled=False,
                                usable_kwh=cfg.usable_kwh * (1 - soc_floor))
    rcem_fallback = round(mean(rcem_by_ym.values()), 4) if rcem_by_ym else 0.35
    days: dict = {}
    for key in sorted(k for k in hours if from_ym <= k[:7] <= to_ym):
        days.setdefault(key[:10], []).append(key)

    all_keys = sorted(k for k in hours if from_ym <= k[:7] <= to_ym)
    eff = math.sqrt(max(cfg.roundtrip_eff, 1e-6))
    day_list = list(days)

    monthly: dict = {}
    for t in tariffs:
        price_fn = make_price_fn(t, catalog, rdn, kompas, overrides, dist_tariff=dist_tariff)
        day_prices: dict = {}
        for day, keys in days.items():
            prices = []
            for k in keys:
                p = price_fn(k)
                prices.append(None if p is None else p[0])
            day_prices[day] = prices
        dp_monthly: dict = {}
        soc = 0.0
        prev_date = None
        for di, day in enumerate(day_list):
            keys = days[day]
            prices = day_prices[day]
            if all(p is None for p in prices):
                soc = 0.0
                prev_date = None
                continue
            ym = day[:7]
            d = date(int(day[:4]), int(day[5:7]), int(day[8:10]))
            if prev_date is None or (d - prev_date).days != 1:
                soc = 0.0   # luka w danych = reset
            end_value = 0.0
            if di + 1 < len(day_list):
                nd = day_list[di + 1]
                nd_date = date(int(nd[:4]), int(nd[5:7]), int(nd[8:10]))
                nprices = sorted((p for p in day_prices[nd] if p is not None), reverse=True)
                if (nd_date - d).days == 1 and nprices:
                    top = nprices[:3]
                    end_value = sum(top) / len(top) * eff
            rcem = rcem_by_ym.get(ym, rcem_fallback)
            res = solve_day(prices, [hours[k][0] for k in keys], [hours[k][1] for k in keys],
                            rcem, cfg, soc_floor=soc_floor, max_cycles=max_cycles,
                            step_kwh=step_kwh, soc0_kwh=soc, end_value=end_value)
            soc = res['end_soc_kwh']
            prev_date = d
            row = dp_monthly.setdefault(ym, {
                'savings_pln': 0.0, 'wear_pln': 0.0, 'grid_charged_kwh': 0.0,
                'pv_charged_kwh': 0.0, 'discharged_kwh': 0.0})
            for f in row:
                row[f] += res[f]
        # ciągłe B (SoC przenoszony przez całą serię): w miesiącu bierzemy lepszy wynik
        b_rows = {r['ym']: r for r in simulate_s1(hours, cfg_b, {}, rcem_by_ym, keys=all_keys,
                                                  price_fn=price_fn)}
        for ym, row in dp_monthly.items():
            b = b_rows.get(ym)
            if b is not None and b['savings_pln'] >= row['savings_pln']:
                row = {'savings_pln': b['savings_pln'], 'wear_pln': 0.0, 'grid_charged_kwh': 0.0,
                       'pv_charged_kwh': b['charged_kwh'], 'discharged_kwh': b['discharged_kwh']}
            monthly.setdefault(ym, {})[t] = row
    return monthly


# ── Sugestie: prosta reguła (okno + cel SoC + strefy rozładowania) ────────────

def _group_key(day: date) -> str:
    return f"{'summer' if 4 <= day.month <= 9 else 'winter'}_{'dayoff' if _is_day_off(day) else 'workday'}"


def _shift_ym(ym: str, delta: int) -> str:
    n = int(ym[:4]) * 12 + int(ym[5:7]) - 1 + delta
    return f"{n // 12:04d}-{n % 12 + 1:02d}"


def _year_day_counts(to_ym: str) -> dict:
    """Liczba dni każdej grupy w 12 miesiącach kończących się na `to_ym` (kalendarz)."""
    d = date(int(_shift_ym(to_ym, -11)[:4]), int(_shift_ym(to_ym, -11)[5:7]), 1)
    end = date(int(to_ym[:4]), int(to_ym[5:7]), 1)
    end = date(end.year + (end.month == 12), end.month % 12 + 1, 1)
    out: dict = {}
    while d < end:
        gk = _group_key(d)
        out[gk] = out.get(gk, 0) + 1
        d += timedelta(days=1)
    return out


def _window_runs(hours_set) -> list:
    """Maksymalne cykliczne ciągi godzin doby -> [(start, end)] (end wyłączny, mod 24)."""
    hs = set(hours_set)
    if len(hs) == 24:
        return [(0, 24)]
    runs = []
    for h in sorted(hs):
        if (h - 1) % 24 in hs:
            continue
        e = h
        while e % 24 in hs:
            e += 1
        runs.append((h, e % 24))
    return runs


def _run_hours(start: int, end: int) -> frozenset:
    n = (end - start) % 24 or 24
    return frozenset((start + i) % 24 for i in range(n))


def _simulate_schedule(recs, cfg_u, U, cand, active_group) -> float:
    """Suma oszczędności serii `recs` = [(hour, exp, imp, price, rcem, group)] (chronologiczna,
    bez godzin bez ceny). `cfg_u` = konfiguracja z pełną pojemnością (moc, sprawność, koszt przerobu),
    `U` = zakres użyteczny magazynu (kWh). `cand` = None albo (okno: frozenset godzin,
    target_kwh, strefy rozładowania: frozenset); zakup w oknie tylko gdy group == active_group.
    Konwencje jak `battery_sim.simulate_s1`."""
    eff = math.sqrt(max(cfg_u.roundtrip_eff, 1e-6))
    P = cfg_u.power_kw
    wear = throughput_cost_kwh(cfg_u)
    if cand is None:
        window = dz = frozenset()
        target = 0.0
    else:
        window, target, dz = cand
        target = min(target, U)
    soc_pv = soc_grid = 0.0
    total = 0.0
    for hour, exp, imp, price, rcem, group in recs:
        budget = P
        free = U - soc_pv - soc_grid
        charge = min(exp if exp > 0 else 0.0, budget, (free if free > 0 else 0.0) / eff)
        if charge > 0:
            soc_pv += charge * eff
            budget -= charge
            total -= charge * rcem
        need = min(imp if imp > 0 else 0.0, budget)
        if need > 0 and soc_pv > 0:
            deliver = min(need, soc_pv * eff)
            soc_pv -= deliver / eff
            need -= deliver
            budget -= deliver
            total += deliver * price
        if need > 0 and soc_grid > 0 and hour in dz:
            deliver = min(need, soc_grid * eff)
            soc_grid -= deliver / eff
            budget -= deliver
            total += deliver * price
        if window and budget > 0 and group == active_group and hour in window:
            room = target - soc_pv - soc_grid
            buy = min(budget, (room if room > 0 else 0.0) / eff)
            if buy > 1e-9:
                soc_grid += buy * eff
                total -= buy * (price + wear)
    return total


def _empty_suggestion(days_total: int, saving: float) -> dict:
    return {'charge_window': None, 'target_soc_pct': None, 'discharge_zones': [],
            'saving_vs_b_pln_year': saving, 'worth_it': False, 'days_total': days_total}


def _suggest_group(recs, cfg, U, gk, base, scale, soc_floor) -> tuple:
    """-> (saving_pln_year, (start, end) | None, target_pct | None, dz_set)."""
    profile: dict = {}
    for hour, _e, _i, price, _r, g in recs:
        if g == gk:
            profile.setdefault(hour, []).append(price)
    prof = {h: median(v) for h, v in profile.items()}
    if not prof:
        return 0.0, None, None, frozenset()
    levels = sorted({round(p, 4) for p in prof.values()})
    windows: dict = {}
    for L in levels[:-1]:
        for s, e in _window_runs(h for h, p in prof.items() if round(p, 4) <= L):
            windows.setdefault(_run_hours(s, e), (s, e))

    def target_kwh(pct):
        return (pct / 100.0 - soc_floor) / (1.0 - soc_floor) * U

    def evaluate(window, thr, pct):
        dz = frozenset(h for h, p in prof.items() if round(p, 4) >= thr)
        return (_simulate_schedule(recs, cfg, U, (window, target_kwh(pct), dz), gk) - base) * scale

    best = None   # remis: krótsze okno, potem wcześniejszy start i niższy próg (deterministycznie)
    best_key = None
    for window, (s, e) in windows.items():
        wmax = max(round(prof[h], 4) for h in window if h in prof)
        for thr in (lv for lv in levels if lv > wmax):
            sv = evaluate(window, thr, 100)
            key = (round(sv, 9), -len(window), -s, -thr)
            if best_key is None or key > best_key:
                best_key, best = key, (sv, window, (s, e), thr)
    if best is None:
        return 0.0, None, None, frozenset()
    best_sv, window, se, thr = best
    best_pct = 100
    cands = [p for p in range(30, 101, TARGET_STEP_PCT) if p > soc_floor * 100]
    for pct in cands:
        sv = evaluate(window, thr, pct)
        if round(sv, 9) > round(best_sv, 9) or (round(sv, 9) == round(best_sv, 9) and pct < best_pct):
            best_sv, best_pct = sv, pct
    dz = frozenset(h for h, p in prof.items() if round(p, 4) >= thr)
    if best_sv <= 0:
        return 0.0, None, None, frozenset()
    return best_sv, se, best_pct, dz


def suggest_plans(hours, cfg, catalog, rdn, kompas, rcem_by_ym, from_ym, to_ym,
                  overrides=None, *, soc_floor=SOC_FLOOR, tariffs=GRID_TARIFFS,
                  dist_tariff='G12w') -> dict:
    """-> {taryfa: {grupa: {'charge_window': {'start','end'}|None, 'target_soc_pct': int|None,
    'discharge_zones': [{'start','end'}], 'saving_vs_b_pln_year': float (>=0), 'worth_it': bool,
    'days_total': int}}}. Ocena na ostatnich 12 miesiącach kończących się na `to_ym` (nie przed
    `from_ym`); oszczędność względem B (PV rozładowywane zawsze, bez zakupu z sieci), przeskalowana
    do roku (dni grupy w roku / doby grupy w zakresie)."""
    U = cfg.usable_kwh * (1.0 - soc_floor)
    result: dict = {t: {} for t in tariffs}
    if U <= 0 or cfg.power_kw <= 0:
        return result
    start_ym = max(from_ym, _shift_ym(to_ym, -11))
    keys = sorted(k for k in hours if start_ym <= k[:7] <= to_ym)
    if not keys:
        return result
    rcem_fallback = round(mean(rcem_by_ym.values()), 4) if rcem_by_ym else 0.35
    year_days = _year_day_counts(to_ym)
    day_groups: dict = {}
    for t in tariffs:
        price_fn = make_price_fn(t, catalog, rdn, kompas, overrides, dist_tariff=dist_tariff)
        recs = []
        days_seen: dict = {}
        for k in keys:
            p = price_fn(k)
            if p is None:
                continue
            day = k[:10]
            gk = day_groups.get(day)
            if gk is None:
                gk = day_groups[day] = _group_key(date(int(k[:4]), int(k[5:7]), int(k[8:10])))
            days_seen.setdefault(gk, set()).add(day)
            exp, imp = hours[k]
            recs.append((int(k[11:13]), exp, imp, p[0], rcem_by_ym.get(k[:7], rcem_fallback), gk))
        if not recs:
            continue
        base = _simulate_schedule(recs, cfg, U, None, None)
        for gk in sorted(days_seen):
            n_range = len(days_seen[gk])
            scale = year_days.get(gk, 0) / n_range
            sv, se, pct, dz = _suggest_group(recs, cfg, U, gk, base, scale, soc_floor)
            sv = max(sv, 0.0)
            worth = se is not None and sv >= MIN_WORTH_PLN_YEAR
            if not worth:
                result[t][gk] = _empty_suggestion(n_range, sv)
                continue
            result[t][gk] = {
                'charge_window': {'start': se[0], 'end': se[1]},
                'target_soc_pct': int(pct),
                'discharge_zones': [{'start': a, 'end': b} for a, b in _window_runs(dz)],
                'saving_vs_b_pln_year': sv, 'worth_it': True, 'days_total': n_range,
            }
    return result
