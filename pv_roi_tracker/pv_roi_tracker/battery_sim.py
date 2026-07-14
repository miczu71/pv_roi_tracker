"""
Symulacja rozbudowy magazynu o drugi moduł (np. LUNA2000-5-E0, +5 kWh / +2,5 kW).

Analiza MARGINALNA: wartość drugiego modułu pochodzi wyłącznie z energii,
której istniejący magazyn nie zdołał obsłużyć. Kluczowa obserwacja: skoro
energia fizycznie wyszła do sieci (eksport), istniejący moduł nie mógł jej
przyjąć (pełny albo limit mocy) — analogicznie import oznacza energię,
której magazyn nie pokrył. Symulacja potrzebuje więc tylko godzinowego
eksportu i importu z liczników (LTS), bez SoC istniejącej baterii.

Wirtualny moduł = bufor usable_kwh / power_kw symulowany godzina po godzinie:
  • ładowanie z eksportu (utracona okazja magazynowania nadwyżki PV),
  • rozładowanie przeciw importowi (niepokryte zapotrzebowanie domu),
  • straty sprawności √η na wejściu i √η na wyjściu (łącznie η).

Wycena (net-billing): rozładowany kWh unika zakupu wg strefy G12w tej godziny,
ale zmagazynowany kWh to utracona sprzedaż po RCEm — marża jest różnicą.

Scenariusze:
  S1  G12w + net-billing (stan faktyczny); opcjonalny arbitraż (dokupowanie
      energii z sieci w dolinie i oddawanie w szczycie — wariant „maksymalny").
  S2  Taryfa dynamiczna + rozliczenie RCEh: te same przepływy energii, wycena
      godzinową ceną RCE (zakup = RCE + dystrybucja, sprzedaż = RCE, ujemne → 0).
  S3  Sprzedaż z magazynu (informacyjny): próg RCE, od którego eksport
      z magazynu przebija RCEm + koszt przerobu.

Moduł czysty — bez I/O; dane godzinowe, cenniki i RCEm podaje wywołujący.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from datetime import date, datetime
from statistics import mean
from typing import Optional

from .rce_hourly import _vat_factor
from .roi import _npv, _irr, _residual_cv, _seasonal_factors, _walk_payback
# Domyślne stawki G12w (brutto, PLN/kWh) — nadpisywane cennikiem z tariff_config.
from .tariff_analysis import _DEFAULT_OFFPEAK_GROSS, _DEFAULT_PEAK_GROSS


@dataclass
class BatteryConfig:
    """Parametry wirtualnego (dokupywanego) modułu — edytowalne w UI."""
    module_price_pln: float = 7599.0
    usable_kwh: float = 5.0
    power_kw: float = 2.5
    roundtrip_eff: float = 0.92
    lifetime_cycles: float = 4500.0
    start_month: str = '2023-06'      # pierwszy symulowany miesiąc (YYYY-MM)
    arbitrage_enabled: bool = False   # S1: dokupowanie z sieci w dolinie
    dynamic_dist_gross: float = 0.49  # S2: dystrybucja+opłaty przy taryfie dynamicznej (PLN/kWh brutto)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'BatteryConfig':
        known = set(cls.__dataclass_fields__)
        clean: dict = {}
        for k, v in (d or {}).items():
            if k not in known:
                continue
            if k == 'arbitrage_enabled':
                clean[k] = bool(v)
            elif k == 'start_month':
                clean[k] = str(v)
            else:
                try:
                    clean[k] = float(v)
                except (TypeError, ValueError):
                    continue
        return cls(**clean)


# ── Kalendarz stref G12w ──────────────────────────────────────────────────────

def _easter(year: int) -> date:
    """Niedziela wielkanocna (algorytm anonimowy gregoriański)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def polish_holidays(year: int) -> set[date]:
    """Dni ustawowo wolne od pracy (w G12w traktowane jak weekend — cała dolina)."""
    from datetime import timedelta
    e = _easter(year)
    return {
        date(year, 1, 1), date(year, 1, 6),
        e + timedelta(days=1),            # Poniedziałek Wielkanocny
        date(year, 5, 1), date(year, 5, 3),
        e + timedelta(days=49),           # Zielone Świątki
        e + timedelta(days=60),           # Boże Ciało
        date(year, 8, 15), date(year, 11, 1), date(year, 11, 11),
        date(year, 12, 24), date(year, 12, 25), date(year, 12, 26),
    }


_HOLIDAY_CACHE: dict[int, set[date]] = {}


def _is_peak_hour(d: date, hour: int) -> bool:
    """Strefa szczytowa G12w: dni robocze 06–13 i 15–22 czasu lokalnego."""
    if d.weekday() >= 5:
        return False
    hol = _HOLIDAY_CACHE.get(d.year)
    if hol is None:
        hol = _HOLIDAY_CACHE[d.year] = polish_holidays(d.year)
    if d in hol:
        return False
    return 6 <= hour < 13 or 15 <= hour < 22


def is_peak(dt: datetime) -> bool:
    return _is_peak_hour(dt.date(), dt.hour)


# ── Symulacja godzinowa ───────────────────────────────────────────────────────

def _new_month_row(ym: str) -> dict:
    return {
        'ym': ym,
        'charged_kwh': 0.0,          # energia pobrana z eksportu (przed stratami)
        'discharged_kwh': 0.0,       # energia oddana do domu (po stratach)
        'avoided_buy_pln': 0.0,      # uniknięty zakup
        'foregone_rcem_pln': 0.0,    # utracona sprzedaż zmagazynowanej nadwyżki
        'savings_pln': 0.0,          # marża = avoided − foregone (+ arbitraż)
        'hours': 0,                  # godziny z danymi (pokrycie)
    }


def _new_s1_row(ym: str) -> dict:
    """Wiersz S1 = wspólna baza + strefy G12w i arbitraż."""
    row = _new_month_row(ym)
    row.update({
        'disch_peak_kwh': 0.0,
        'disch_offpeak_kwh': 0.0,
        'arb_charged_kwh': 0.0,      # arbitraż: dokupione z sieci w dolinie
        'arb_discharged_kwh': 0.0,   # arbitraż: oddane w szczycie
        'arb_savings_pln': 0.0,
        'rcem_estimated': False,
    })
    return row


def simulate_s1(
    hours: dict[str, tuple],
    cfg: BatteryConfig,
    zone_prices: dict[str, tuple],
    rcem_by_ym: dict[str, float],
    keys: Optional[list[str]] = None,
) -> list[dict]:
    """
    Scenariusz S1 (G12w + net-billing): wirtualny moduł ładowany nadwyżką PV
    (= zmierzonym eksportem), rozładowywany przeciw importowi.

    hours:       {'YYYY-MM-DDTHH': (export_kwh, import_kwh)} — czas lokalny
    zone_prices: {'YYYY-MM': (peak_gross, offpeak_gross)} PLN/kWh
    rcem_by_ym:  {'YYYY-MM': PLN/kWh} — efektywna cena sprzedaży RCEm
    keys:        posortowane klucze hours (opcjonalnie, oszczędza powtórny sort)

    Zwraca listę miesięcznych wierszy (posortowaną po ym).
    """
    eff_leg = math.sqrt(max(cfg.roundtrip_eff, 1e-6))   # strata na wejściu i wyjściu
    cap_h = cfg.power_kw                                 # kWh na godzinę
    rcem_fallback = round(mean(rcem_by_ym.values()), 4) if rcem_by_ym else 0.35

    soc_pv = 0.0      # kWh w module pochodzące z PV (po stratach wejścia)
    soc_grid = 0.0    # kWh dokupione z sieci w dolinie (arbitraż)
    months: dict[str, dict] = {}

    for key in (keys if keys is not None else sorted(hours)):
        exp, imp = hours[key]
        try:
            # klucz ma stałą szerokość 'YYYY-MM-DDTHH' — parsowanie wycinkami
            # jest ~10× szybsze od strptime (pętla po ~26k godzin)
            day = date(int(key[:4]), int(key[5:7]), int(key[8:10]))
            hour = int(key[11:13])
        except ValueError:
            continue
        ym = key[:7]
        row = months.get(ym)
        if row is None:
            row = months[ym] = _new_s1_row(ym)
            row['rcem_estimated'] = ym not in rcem_by_ym
        peak_px, offpeak_px = zone_prices.get(
            ym, (_DEFAULT_PEAK_GROSS, _DEFAULT_OFFPEAK_GROSS))
        rcem = rcem_by_ym.get(ym, rcem_fallback)
        peak = _is_peak_hour(day, hour)
        row['hours'] += 1
        budget = cap_h  # wspólny limit mocy godziny (ładowanie + rozładowanie)

        # 1. Ładowanie z eksportu — istniejący moduł go nie przyjął.
        free = cfg.usable_kwh - soc_pv - soc_grid
        charge = min(max(exp, 0.0), budget, max(free, 0.0) / eff_leg)
        if charge > 0:
            soc_pv += charge * eff_leg
            budget -= charge
            row['charged_kwh'] += charge
            row['foregone_rcem_pln'] += charge * rcem

        # 2. Rozładowanie przeciw importowi: najpierw energia PV (tańsza),
        #    energia arbitrażowa tylko w szczycie (w dolinie to czysta strata).
        need = min(max(imp, 0.0), budget)
        if need > 0 and soc_pv > 0:
            deliver = min(need, soc_pv * eff_leg)
            soc_pv -= deliver / eff_leg
            need -= deliver
            budget -= deliver
            px = peak_px if peak else offpeak_px
            row['discharged_kwh'] += deliver
            row['disch_peak_kwh' if peak else 'disch_offpeak_kwh'] += deliver
            row['avoided_buy_pln'] += deliver * px
        if need > 0 and soc_grid > 0 and peak:
            deliver = min(need, soc_grid * eff_leg)
            soc_grid -= deliver / eff_leg
            budget -= deliver
            row['arb_discharged_kwh'] += deliver
            row['arb_savings_pln'] += deliver * peak_px

        # 3. Arbitraż (opcjonalny, wariant maksymalny): w dolinie dokup
        #    z sieci do pełna w ramach pozostałego limitu mocy.
        if cfg.arbitrage_enabled and not peak and budget > 0:
            free = cfg.usable_kwh - soc_pv - soc_grid
            buy = min(budget, max(free, 0.0) / eff_leg)
            if buy > 1e-9:
                soc_grid += buy * eff_leg
                row['arb_charged_kwh'] += buy
                row['arb_savings_pln'] -= buy * offpeak_px

    for row in months.values():
        row['savings_pln'] = (row['avoided_buy_pln'] - row['foregone_rcem_pln']
                              + row['arb_savings_pln'])
    return [months[ym] for ym in sorted(months)]


def simulate_s2(
    hours: dict[str, tuple],
    cfg: BatteryConfig,
    rce_hourly: dict[str, float],
    keys: Optional[list[str]] = None,
) -> list[dict]:
    """
    Scenariusz S2 (taryfa dynamiczna + rozliczenie RCEh): te same przepływy
    energii, ale wycena godzinowa — uniknięty zakup po (RCE + dystrybucja),
    utracona sprzedaż po RCE (ujemne ceny → 0, art. 4b ustawy o OZE).

    rce_hourly: {'YYYY-MM-DDTHH': PLN/MWh netto} (format cache rce_hourly.json).
    Miesiące bez cen RCE są pomijane (dane PSE od 2024-07).
    """
    eff_leg = math.sqrt(max(cfg.roundtrip_eff, 1e-6))
    cap_h = cfg.power_kw
    soc = 0.0
    months: dict[str, dict] = {}

    for key in (keys if keys is not None else sorted(hours)):
        exp, imp = hours[key]
        rce = rce_hourly.get(key)
        if rce is None:
            continue
        ym = key[:7]
        row = months.get(ym)
        if row is None:
            row = months[ym] = _new_month_row(ym)
        vat = _vat_factor(ym)
        sell_px = max(rce, 0.0) / 1000.0 * vat                  # RCEh sprzedaż
        buy_px = max(rce, 0.0) / 1000.0 * 1.23 + cfg.dynamic_dist_gross
        row['hours'] += 1
        budget = cap_h

        free = cfg.usable_kwh - soc
        charge = min(max(exp, 0.0), budget, max(free, 0.0) / eff_leg)
        if charge > 0:
            soc += charge * eff_leg
            budget -= charge
            row['charged_kwh'] += charge
            row['foregone_rcem_pln'] += charge * sell_px

        need = min(max(imp, 0.0), budget)
        if need > 0 and soc > 0:
            deliver = min(need, soc * eff_leg)
            soc -= deliver / eff_leg
            row['discharged_kwh'] += deliver
            row['avoided_buy_pln'] += deliver * buy_px

    for row in months.values():
        row['savings_pln'] = row['avoided_buy_pln'] - row['foregone_rcem_pln']
    return [months[ym] for ym in sorted(months)]


def sell_threshold_analysis(
    rce_hourly: dict[str, float],
    cfg: BatteryConfig,
    rcem_by_ym: dict[str, float],
    last_n_months: int = 12,
) -> dict:
    """
    Scenariusz S3 (informacyjny): sprzedaż z magazynu przy wysokim RCE.

    Cykl „kup okazję PV → sprzedaj w godzinie max RCE" opłaca się, gdy
    max_RCE × η > RCEm + koszt przerobu. Zwraca próg opłacalności i statystykę
    dziennych maksimów RCE z ostatnich miesięcy.
    """
    throughput_cost = throughput_cost_kwh(cfg)
    daily_max: dict[str, float] = {}
    for key, p in rce_hourly.items():
        day = key[:10]
        if p is not None:
            daily_max[day] = max(daily_max.get(day, float('-inf')), p)
    months: dict[str, list[float]] = {}
    for day, p in daily_max.items():
        months.setdefault(day[:7], []).append(p)

    rows = []
    for ym in sorted(months)[-last_n_months:]:
        vals = months[ym]
        rcem = rcem_by_ym.get(ym)
        avg_max_gross = mean(vals) / 1000.0 * _vat_factor(ym)
        margin = (avg_max_gross * cfg.roundtrip_eff
                  - (rcem if rcem is not None else 0.0) - throughput_cost)
        rows.append({
            'ym': ym,
            'avg_daily_max_rce_gross': round(avg_max_gross, 4),
            'rcem': rcem,
            'margin_per_kwh': round(margin, 4) if rcem is not None else None,
        })
    with_margin = [r['margin_per_kwh'] for r in rows if r['margin_per_kwh'] is not None]
    rcem_recent = [r['rcem'] for r in rows if r['rcem'] is not None]
    breakeven = (round((mean(rcem_recent) + throughput_cost) / cfg.roundtrip_eff, 4)
                 if rcem_recent else None)
    return {
        'months': rows,
        'throughput_cost_kwh': round(throughput_cost, 4),
        'breakeven_sell_price_gross': breakeven,   # PLN/kWh, od tej ceny sprzedaż > RCEm
        'avg_margin_per_kwh': round(mean(with_margin), 4) if with_margin else None,
        'profitable_months': sum(1 for m in with_margin if m > 0),
        'months_analyzed': len(with_margin),
    }


# ── Agregacja / finanse ──────────────────────────────────────────────────────

def throughput_cost_kwh(cfg: BatteryConfig) -> float:
    """Koszt przerobu 1 kWh (degradacja): cena ÷ (cykle życiowe × pojemność)."""
    denom = cfg.lifetime_cycles * cfg.usable_kwh
    return cfg.module_price_pln / denom if denom > 0 else 0.0


def summarize(
    s1_rows: list[dict],
    cfg: BatteryConfig,
    today: Optional[date] = None,
    discount_rate: float = 0.04,
    horizon_months: int = 120,
) -> dict:
    """
    Podsumowanie finansowe S1: retro („gdybym miał moduł od początku danych")
    i prognoza (payback sezonowy P10/P50/P90, NPV i IRR w horyzoncie gwarancji).
    """
    if today is None:
        today = date.today()
    current_ym = f'{today.year}-{today.month:02d}'
    closed = [r for r in s1_rows if r['ym'] != current_ym]

    total_savings = sum(r['savings_pln'] for r in closed)
    total_charged = sum(r['charged_kwh'] for r in closed) + sum(r['arb_charged_kwh'] for r in closed)
    total_discharged = sum(r['discharged_kwh'] for r in closed) + sum(r['arb_discharged_kwh'] for r in closed)
    cycles_total = total_charged / cfg.usable_kwh if cfg.usable_kwh > 0 else 0.0
    n_closed = len(closed)

    window = [r for r in closed if r['savings_pln'] > 0][-12:]
    monthly_avg = (sum(r['savings_pln'] for r in window) / len(window)) if window else None

    savings_pairs = [(int(r['ym'][5:7]), r['savings_pln'] or 0.0) for r in closed]
    factors = _seasonal_factors(savings_pairs)
    cv = _residual_cv(savings_pairs, monthly_avg or 0.0, factors)

    payback_p50 = payback_p10 = payback_p90 = None
    npv_v = irr_pct = None
    if monthly_avg and monthly_avg > 0:
        price = cfg.module_price_pln
        payback_p50 = _walk_payback(price, monthly_avg, factors, today)
        payback_p10 = _walk_payback(price, monthly_avg, factors, today, z=1.28, cv=cv)
        payback_p90 = _walk_payback(price, monthly_avg, factors, today, z=-1.28, cv=cv)
        # NPV/IRR zakupu dziś, przepływy prognozowane w horyzoncie gwarancji.
        from dateutil.relativedelta import relativedelta
        cfs = [-price]
        cursor = today + relativedelta(months=1)
        for _ in range(horizon_months):
            cfs.append(monthly_avg * factors.get(cursor.month, 1.0))
            cursor += relativedelta(months=1)
        npv_v = round(_npv(cfs, discount_rate / 12.0), 2)
        try:
            irr_m = _irr(cfs)
        except (OverflowError, ZeroDivisionError):
            irr_m = None   # Newton rozbieżny przy bardzo długim/nieosiągalnym zwrocie
        if irr_m is not None:
            irr_pct = round(((1.0 + irr_m) ** 12 - 1.0) * 100.0, 2)

    # Retro: skumulowane oszczędności vs cena — kiedy moduł by się spłacił.
    cum = 0.0
    retro_paid_ym = None
    for r in closed:
        cum += r['savings_pln']
        if retro_paid_ym is None and cum >= cfg.module_price_pln:
            retro_paid_ym = r['ym']

    tc = throughput_cost_kwh(cfg)
    margin_per_kwh = (total_savings / total_discharged) if total_discharged > 0 else None

    return {
        'monthly_avg_savings': round(monthly_avg, 2) if monthly_avg is not None else None,
        'avg_window': len(window),
        'months_simulated': n_closed,
        'payback_date': payback_p50.isoformat() if payback_p50 else None,
        'payback_date_p10': payback_p10.isoformat() if payback_p10 else None,
        'payback_date_p90': payback_p90.isoformat() if payback_p90 else None,
        'payback_years': (round(cfg.module_price_pln / (monthly_avg * 12), 1)
                          if monthly_avg and monthly_avg > 0 else None),
        'npv': npv_v,
        'irr_pct': irr_pct,
        'retro_total_savings': round(total_savings, 2),
        'retro_paid_back_ym': retro_paid_ym,
        'retro_first_ym': closed[0]['ym'] if closed else None,
        'cycles_total': round(cycles_total, 1),
        'cycles_per_month': round(cycles_total / n_closed, 2) if n_closed else None,
        'throughput_cost_kwh': round(tc, 4),
        'margin_per_kwh': round(margin_per_kwh, 4) if margin_per_kwh is not None else None,
        'margin_above_throughput': (round(margin_per_kwh - tc, 4)
                                    if margin_per_kwh is not None else None),
        'total_discharged_kwh': round(total_discharged, 1),
        'total_charged_kwh': round(total_charged, 1),
    }


def simulate_all(
    hours: dict[str, tuple],
    cfg: BatteryConfig,
    zone_prices: dict[str, tuple],
    rcem_by_ym: dict[str, float],
    rce_hourly: Optional[dict[str, float]] = None,
    today: Optional[date] = None,
) -> dict:
    """Pełny payload zakładki: S1 (+podsumowanie), S2, S3, echo konfiguracji."""
    hours = {k: v for k, v in hours.items() if k[:7] >= cfg.start_month}
    keys = sorted(hours)
    s1 = simulate_s1(hours, cfg, zone_prices, rcem_by_ym, keys=keys)
    s2 = simulate_s2(hours, cfg, rce_hourly or {}, keys=keys)
    s3 = sell_threshold_analysis(rce_hourly or {}, cfg, rcem_by_ym)
    summary = summarize(s1, cfg, today=today)

    def _round_rows(rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            rr = dict(r)
            for k, v in rr.items():
                if isinstance(v, float):
                    rr[k] = round(v, 3)
            out.append(rr)
        return out

    if today is None:
        today = date.today()
    current_ym = f'{today.year}-{today.month:02d}'
    for rows in (s1, s2):
        for r in rows:
            r['is_current'] = r['ym'] == current_ym

    s2_closed = [r for r in s2 if not r['is_current']]
    s2_avg = (round(mean(r['savings_pln'] for r in s2_closed[-12:]), 2)
              if s2_closed else None)

    return {
        'config': cfg.to_dict(),
        's1_months': _round_rows(s1),
        's2_months': _round_rows(s2),
        's2_avg_monthly': s2_avg,
        's3': s3,
        'summary': summary,
    }
