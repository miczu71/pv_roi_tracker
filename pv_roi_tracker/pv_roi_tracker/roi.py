"""Pure ROI calculation functions — no I/O, no side effects."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from statistics import mean, stdev
from typing import Optional

from dateutil.relativedelta import relativedelta

from .models import MonthlyRecord
from . import cpi_fetcher as _cpi


# ── Rachunek „bez PV vs z PV" ────────────────────────────────────────────────

def bill_comparison(records: list[MonthlyRecord]) -> dict:
    """Rachunek hipotetyczny 'bez PV' vs rzeczywisty 'z PV', per miesiąc.

    bill_without_pv = consumed_kwh × buy_price          (cały dom z sieci)
    bill_with_pv    = purchased_kwh × buy_price − feedin_revenue  (rzeczywisty)
    saved           = bill_without_pv − bill_with_pv     ≈ autokonsumpcja + przychód

    Zwraca dict:
      months:          [{ym, bill_without_pv, bill_with_pv, saved, savings_pct}]
      total_without_pv, total_with_pv, total_saved,
      avg_savings_pct: średni % niższego rachunku (miesiące z danymi)
    """
    months = []
    for r in sorted(records, key=lambda x: (x.year, x.month)):
        consumed = r.consumed_kwh or 0.0
        buy_px   = r.buy_price_pln_kwh or 0.0
        if consumed <= 0 or buy_px <= 0:
            continue
        bill_without = round(consumed * buy_px, 2)
        purchased    = r.purchased_kwh or 0.0
        feedin_rev   = r.feedin_revenue_pln or 0.0
        bill_with    = round(purchased * buy_px - feedin_rev, 2)
        saved        = round(bill_without - bill_with, 2)
        savings_pct  = round(saved / bill_without * 100, 1) if bill_without > 0 else 0.0
        months.append({
            'ym':             f'{r.year}-{r.month:02d}',
            'bill_without_pv': bill_without,
            'bill_with_pv':    bill_with,
            'saved':           saved,
            'savings_pct':     savings_pct,
        })

    total_without = round(sum(m['bill_without_pv'] for m in months), 2)
    total_with    = round(sum(m['bill_with_pv']    for m in months), 2)
    total_saved   = round(total_without - total_with, 2)
    pcts          = [m['savings_pct'] for m in months if m['bill_without_pv'] > 0]
    avg_pct       = round(sum(pcts) / len(pcts), 1) if pcts else None

    return {
        'months':          months,
        'total_without_pv': total_without,
        'total_with_pv':    total_with,
        'total_saved':      total_saved,
        'avg_savings_pct':  avg_pct,
    }


# ── Alert „poniżej oczekiwań" ────────────────────────────────────────────────

def _seasonal_prod_factors(complete: list[MonthlyRecord]) -> dict[int, float]:
    """Per-calendar-month production index (kWh) relative to overall mean.

    Analogous to _seasonal_factors() but computed on produced_kwh, not savings.
    Returns {1..12: float}; returns all-1.0 when fewer than 6 distinct months have data.
    """
    by_month: dict[int, list[float]] = {m: [] for m in range(1, 13)}
    for r in complete:
        p = r.produced_kwh or 0.0
        if p > 0:
            by_month[r.month].append(p)
    months_present = [m for m, vals in by_month.items() if vals]
    if len(months_present) < 6:
        return {m: 1.0 for m in range(1, 13)}
    month_means = {m: mean(vals) for m, vals in by_month.items() if vals}
    overall_mean = mean(month_means.values())
    if overall_mean <= 0:
        return {m: 1.0 for m in range(1, 13)}
    return {m: (month_means[m] / overall_mean if m in month_means else 1.0)
            for m in range(1, 13)}


def underperformance_analysis(
    records: list[MonthlyRecord],
    today: Optional[date] = None,
    alert_threshold_pct: float = -10.0,
) -> dict:
    """Analiza odchylenia produkcji ostatniego zamkniętego miesiąca od oczekiwania sezonowego.

    Oczekiwanie = średnia produkcja tego samego miesiąca kalendarzowego
    z poprzednich lat (minimum 1 poprzedni rok + łącznie ≥ 2 obserwacje).

    Zwraca dict:
      last_closed_ym:   str 'YYYY-MM'
      actual_kwh:       rzeczywista produkcja
      expected_kwh:     oczekiwana na podstawie historii
      deviation_pct:    (actual−expected)/expected × 100
      flag:             'uwaga' gdy deviation_pct ≤ alert_threshold_pct, else 'ok'
      n_prior_years:    liczba lat użytych do obliczenia oczekiwania
    Gdy brak wystarczających danych — zwraca flag='ok', pozostałe pola None.
    """
    if today is None:
        today = date.today()
    current_ym = (today.year, today.month)

    complete = sorted(
        [r for r in records
         if (r.year, r.month) < current_ym and (r.produced_kwh or 0.0) > 0],
        key=lambda r: (r.year, r.month),
    )
    if len(complete) < 2:
        return {'flag': 'ok', 'last_closed_ym': None, 'actual_kwh': None,
                'expected_kwh': None, 'deviation_pct': None, 'n_prior_years': 0}

    last = complete[-1]

    # Need at least 1 prior year of the same calendar month
    prior_same = [r for r in complete
                  if r.month == last.month and r.year < last.year
                  and (r.produced_kwh or 0.0) > 0]
    if not prior_same:
        return {'flag': 'ok', 'last_closed_ym': f'{last.year}-{last.month:02d}',
                'actual_kwh': round(last.produced_kwh, 1), 'expected_kwh': None,
                'deviation_pct': None, 'n_prior_years': 0}

    expected = round(mean(r.produced_kwh for r in prior_same), 1)
    actual   = round(last.produced_kwh, 1)
    deviation_pct = round((actual - expected) / expected * 100, 1) if expected > 0 else None
    flag = ('uwaga' if deviation_pct is not None and deviation_pct <= alert_threshold_pct
            else 'ok')

    return {
        'flag':           flag,
        'last_closed_ym': f'{last.year}-{last.month:02d}',
        'actual_kwh':     actual,
        'expected_kwh':   expected,
        'deviation_pct':  deviation_pct,
        'n_prior_years':  len(prior_same),
    }

GROSS_INVESTMENT = 51_900.00  # zł — total project cost before any subsidy
SUBSIDY = 28_714.00           # zł — one-time government grant (Mój Prąd / Czyste Powietrze)
NET_INVESTMENT = GROSS_INVESTMENT - SUBSIDY  # 23_186.00 zł — kept for reference
SYSTEM_KWP = 6.72

# Wskaźnik emisyjności CO2 dla odbiorców końcowych energii elektrycznej (KOBiZE,
# dane za 2023, publikacja 2024) — nadpisywalny opcją co2_factor_kg_kwh.
CO2_FACTOR_KG_KWH = 0.597

# Horyzont NPV/IRR — pełny cykl życia instalacji, nie tylko do nominalnej
# spłaty. Truncating cashflows at payback makes total inflows equal the
# outflow by construction (inflows = remaining_to_recover + total_savings =
# gross_investment − subsidy = outflow), which pins NPV/IRR near zero
# regardless of how well the system performs — nadpisywalne opcjami
# asset_lifetime_years / panel_degradation_pct_year.
ASSET_LIFETIME_YEARS = 25.0
PANEL_DEGRADATION_PCT_YEAR = 0.5   # roczny spadek mocy paneli (typowa gwarancja)


def _msav(r: MonthlyRecord) -> float:
    return (r.self_consumed_savings_pln or 0.0) + (r.feedin_revenue_pln or 0.0) + (r.battery_arbitrage_savings_pln or 0.0)


# ── Seasonal forecast helpers ────────────────────────────────────────────────

def _seasonal_factors(pairs: list[tuple[int, float]]) -> dict[int, float]:
    """Per-calendar-month savings index relative to overall mean.
    Takes (calendar_month, savings) pairs. Returns {1..12: float}."""
    by_month: dict[int, list[float]] = {m: [] for m in range(1, 13)}
    for month, s in pairs:
        if s > 0:
            by_month[month].append(s)
    months_present = [m for m, vals in by_month.items() if vals]
    if len(months_present) < 2:
        return {m: 1.0 for m in range(1, 13)}
    month_means = {m: mean(vals) for m, vals in by_month.items() if vals}
    overall_mean = mean(month_means.values())
    if overall_mean <= 0:
        return {m: 1.0 for m in range(1, 13)}
    return {m: (month_means[m] / overall_mean if m in month_means else 1.0) for m in range(1, 13)}


def _residual_cv(pairs: list[tuple[int, float]], monthly_avg: float, factors: dict[int, float]) -> float:
    """Coefficient of variation of actual vs seasonal-expected savings, capped at 0.5.
    Takes (calendar_month, savings) pairs."""
    if monthly_avg <= 0:
        return 0.0
    ratios = [
        s / (monthly_avg * factors.get(month, 1.0))
        for month, s in pairs
        if s > 0 and monthly_avg * factors.get(month, 1.0) > 0
    ]
    if len(ratios) < 2:
        return 0.0
    m = mean(ratios)
    return min(stdev(ratios) / m, 0.5) if m > 0 else 0.0


def _walk_payback(
    remaining: float,
    monthly_avg: float,
    factors: dict[int, float],
    start_date: date,
    z: float = 0.0,
    cv: float = 0.0,
) -> Optional[date]:
    """Walk forward accumulating seasonal savings until cumulative >= remaining. Cap 240 months."""
    if monthly_avg <= 0:
        return None
    if remaining <= 0:
        return start_date
    cursor = start_date + relativedelta(months=1)
    cumulative = 0.0
    for _ in range(240):
        m_sav = max(monthly_avg * factors.get(cursor.month, 1.0) * (1.0 + z * cv), 0.01)
        cumulative += m_sav
        if cumulative >= remaining:
            return cursor
        cursor += relativedelta(months=1)
    return None


# ── Degradacja / wydajność znormalizowana ────────────────────────────────────

def degradation_analysis(
    records: list[MonthlyRecord],
    system_kwp: float = SYSTEM_KWP,
    today: Optional[date] = None,
) -> dict:
    """Znormalizowana analiza wydajności (kWh/kWp) do wykrywania degradacji/zabrudzenia.

    Zwraca:
      yearly:            [{year, produced_kwh, yield_kwh_kwp, complete}]
      rolling:           [{ym, yield_12m}] — krocząca suma 12 mies. / kWp
      yoy_delta_pct:     produkcja r/r po sparowanych miesiącach (ost. 12 zamkn. mies.
                         vs te same miesiące rok wcześniej); None gdy <3 par
      trend_pct_per_year: nachylenie regresji liniowej serii kroczącej, %/rok; None gdy <6 punktów
    """
    if today is None:
        today = date.today()
    current_ym = (today.year, today.month)
    complete = sorted(
        [r for r in records if (r.year, r.month) < current_ym and (r.produced_kwh or 0.0) > 0],
        key=lambda r: (r.year, r.month),
    )

    # Suma roczna (rok kompletny = 12 miesięcy z produkcją)
    by_year: dict[int, list[MonthlyRecord]] = {}
    for r in complete:
        by_year.setdefault(r.year, []).append(r)
    yearly = [{
        'year': y,
        'produced_kwh': round(sum(r.produced_kwh for r in recs), 1),
        'yield_kwh_kwp': round(sum(r.produced_kwh for r in recs) / system_kwp, 1) if system_kwp else None,
        'complete': len(recs) == 12,
    } for y, recs in sorted(by_year.items())]

    # Krocząca suma 12 miesięcy
    rolling = []
    for i in range(11, len(complete)):
        window = complete[i - 11:i + 1]
        first, last = window[0], window[-1]
        # okno musi być ciągłe (12 kolejnych miesięcy)
        span = (last.year - first.year) * 12 + (last.month - first.month)
        if span != 11:
            continue
        rolling.append({
            'ym': f'{last.year}-{last.month:02d}',
            'yield_12m': round(sum(r.produced_kwh for r in window) / system_kwp, 1) if system_kwp else None,
        })

    # r/r po sparowanych miesiącach (działa już przy <24 mies. historii)
    by_ym = {(r.year, r.month): r.produced_kwh for r in complete}
    recent_sum = prior_sum = 0.0
    pairs = 0
    for r in complete[-12:]:
        prior = by_ym.get((r.year - 1, r.month))
        if prior:
            recent_sum += r.produced_kwh
            prior_sum += prior
            pairs += 1
    yoy_delta_pct = (round((recent_sum / prior_sum - 1.0) * 100.0, 2)
                     if pairs >= 3 and prior_sum > 0 else None)

    # Trend liniowy serii kroczącej (najmniejsze kwadraty), %/rok względem średniej
    trend_pct_per_year = None
    pts = [p['yield_12m'] for p in rolling if p['yield_12m'] is not None]
    if len(pts) >= 6:
        n = len(pts)
        mean_x = (n - 1) / 2.0
        mean_y = sum(pts) / n
        num = sum((i - mean_x) * (y - mean_y) for i, y in enumerate(pts))
        den = sum((i - mean_x) ** 2 for i in range(n))
        if den > 0 and mean_y > 0:
            slope_per_month = num / den
            trend_pct_per_year = round(slope_per_month * 12 / mean_y * 100.0, 2)

    return {
        'yearly': yearly,
        'rolling': rolling,
        'yoy_delta_pct': yoy_delta_pct,
        'trend_pct_per_year': trend_pct_per_year,
        'pairs_used': pairs,
    }


# ── NPV / IRR helpers ────────────────────────────────────────────────────────

def _npv(cashflows: list[float], monthly_rate: float) -> float:
    return sum(cf / (1.0 + monthly_rate) ** i for i, cf in enumerate(cashflows))


def _irr(cashflows: list[float]) -> Optional[float]:
    """Newton's method monthly IRR. Returns monthly rate or None if not converged."""
    if not cashflows or cashflows[0] >= 0:
        return None
    if not any(cf > 0 for cf in cashflows[1:]):
        return None
    r = 0.01  # initial guess: 1% monthly (~12% annual)
    for _ in range(100):
        npv_v = sum(cf / (1.0 + r) ** i for i, cf in enumerate(cashflows))
        dnpv  = sum(-i * cf / (1.0 + r) ** (i + 1) for i, cf in enumerate(cashflows))
        if abs(dnpv) < 1e-12:
            return None
        r_new = r - npv_v / dnpv
        if abs(r_new - r) < 1e-8:
            return r_new if r_new > -1.0 else None
        r = r_new
    return None


# ── Result ───────────────────────────────────────────────────────────────────

@dataclass
class RoiResult:
    # Core savings
    self_consumption_savings: float
    feedin_revenue: float
    battery_arbitrage_savings: float
    total_savings: float
    total_return: float
    roi_pct: float
    months_with_data: int
    monthly_avg_savings: Optional[float]
    monthly_avg_window: int
    remaining_to_recover: float
    months_to_payback: Optional[float]
    years_to_payback: Optional[float]
    payback_date: Optional[date]        # = payback_date_seasonal (P50)
    total_produced_kwh: float
    total_exported_kwh: float
    specific_yield_lifetime: float
    gross_investment: float
    subsidy: float
    net_profit: float
    # Seasonal forecast
    seasonal_factors: dict              # {1..12: float}
    residual_cv: float
    payback_date_seasonal: Optional[date]
    payback_date_p10: Optional[date]    # optimistic  (z = +1.28 → higher savings → earlier)
    payback_date_p90: Optional[date]    # pessimistic (z = −1.28 → lower savings → later)
    # Real / NPV / IRR / bond
    commissioning_date: Optional[date]
    real_total_savings: Optional[float]
    real_total_return: Optional[float]
    real_roi_pct: Optional[float]
    npv: Optional[float]
    irr_pct: Optional[float]
    counterfactual_bond_value: Optional[float]
    counterfactual_delta: Optional[float]
    # CPI provenance (default values allow older callers without breaking)
    inflation_source: str = field(default="flat fallback")
    cumulative_inflation_pct: float = field(default=0.0)
    # Wskaźniki energetyczne (defaults keep older callers working)
    self_consumption_rate_pct: Optional[float] = field(default=None)  # Σ autokonsumpcja / Σ produkcja
    autarky_pct: Optional[float] = field(default=None)                # Σ autokonsumpcja / Σ zużycie
    co2_avoided_kg: float = field(default=0.0)                        # Σ produkcja × wskaźnik KOBiZE
    yoy_yield_delta_pct: Optional[float] = field(default=None)        # produkcja r/r (pary miesięcy)
    # Alert „poniżej oczekiwań" — odchylenie produkcji ostatniego miesiąca (v0.27.0)
    underperformance_pct: Optional[float] = field(default=None)       # (actual−expected)/expected × 100
    underperformance_flag: str = field(default='ok')                   # 'ok' | 'uwaga'


# ── Main calculation ─────────────────────────────────────────────────────────

def calculate(
    records: list[MonthlyRecord],
    today: Optional[date] = None,
    gross_investment: float = GROSS_INVESTMENT,
    subsidy: float = SUBSIDY,
    system_kwp: float = SYSTEM_KWP,
    discount_rate: float = 0.04,
    inflation: float = 0.05,
    comparison_yield: float = 0.055,
    co2_factor: float = CO2_FACTOR_KG_KWH,
    asset_lifetime_years: float = ASSET_LIFETIME_YEARS,
    panel_degradation_pct_year: float = PANEL_DEGRADATION_PCT_YEAR,
) -> RoiResult:
    if today is None:
        today = date.today()

    sc_sav  = sum(r.self_consumed_savings_pln  or 0.0 for r in records)
    fi_rev  = sum(r.feedin_revenue_pln          or 0.0 for r in records)
    bat_sav = sum(r.battery_arbitrage_savings_pln or 0.0 for r in records)
    total_savings = sc_sav + fi_rev + bat_sav
    total_return  = subsidy + total_savings
    roi_pct = (total_return / gross_investment * 100.0) if gross_investment else 0.0

    months_with_data = sum(
        1 for r in records
        if (r.self_consumed_savings_pln or 0.0) > 0 or (r.feedin_revenue_pln or 0.0) > 0
    )

    current_ym = (today.year, today.month)
    complete = sorted([r for r in records if (r.year, r.month) != current_ym],
                      key=lambda r: (r.year, r.month))
    window = [r for r in complete if _msav(r) > 0][-12:]

    if window:
        monthly_avg_savings: Optional[float] = sum(_msav(r) for r in window) / len(window)
        monthly_avg_window = len(window)
    else:
        monthly_avg_savings = None
        monthly_avg_window = 0

    remaining_to_recover = max(0.0, gross_investment - total_return)

    # ── Seasonal forecast ────────────────────────────────────────────────────
    savings_pairs = [(r.month, _msav(r)) for r in complete]
    factors = _seasonal_factors(savings_pairs)
    cv = _residual_cv(savings_pairs, monthly_avg_savings or 0.0, factors)

    if monthly_avg_savings and monthly_avg_savings > 0:
        payback_date_seasonal = _walk_payback(remaining_to_recover, monthly_avg_savings, factors, today)
        # P10 = optimistic/earlier → higher monthly savings (z > 0);
        # P90 = pessimistic/later → lower monthly savings (z < 0).
        payback_date_p10 = _walk_payback(remaining_to_recover, monthly_avg_savings, factors, today, z=1.28,  cv=cv)
        payback_date_p90 = _walk_payback(remaining_to_recover, monthly_avg_savings, factors, today, z=-1.28, cv=cv)
        months_to_payback: Optional[float] = remaining_to_recover / monthly_avg_savings
        years_to_payback:  Optional[float] = round(months_to_payback / 12, 2)
    else:
        payback_date_seasonal = payback_date_p10 = payback_date_p90 = None
        months_to_payback = years_to_payback = None

    payback_date = payback_date_seasonal

    # ── Totals ───────────────────────────────────────────────────────────────
    total_produced = sum(r.produced_kwh or 0.0 for r in records)
    total_exported = sum(r.exported_kwh or 0.0 for r in records)
    specific_yield = (total_produced / system_kwp) if system_kwp else 0.0

    # ── Autokonsumpcja / autarkia / CO2 / degradacja ─────────────────────────
    total_self_consumed = sum(r.self_consumed_kwh or 0.0 for r in records)
    total_consumed = sum(r.consumed_kwh or 0.0 for r in records)
    self_consumption_rate = (round(total_self_consumed / total_produced * 100.0, 1)
                             if total_produced > 0 else None)
    autarky = (round(total_self_consumed / total_consumed * 100.0, 1)
               if total_consumed > 0 else None)
    co2_avoided = round(total_produced * co2_factor, 1)
    yoy_delta = degradation_analysis(records, system_kwp, today)['yoy_delta_pct']

    # ── Alert „poniżej oczekiwań" ─────────────────────────────────────────────
    _ua = underperformance_analysis(records, today)
    underperf_pct  = _ua['deviation_pct']
    underperf_flag = _ua['flag']

    # ── Commissioning date ───────────────────────────────────────────────────
    commissioning_date: Optional[date] = None
    for r in sorted(records, key=lambda r: (r.year, r.month)):
        if (r.produced_kwh or 0.0) > 0:
            commissioning_date = date(r.year, r.month, 1)
            break

    # ── Real savings / NPV / IRR / bond ──────────────────────────────────────
    real_total_savings = real_total_return_v = real_roi_pct_v = None
    npv_v = irr_pct_v = counterfactual_bond = counterfactual_delta = None
    cum_infl_pct = 0.0

    inflation_src = _cpi.coverage_desc()
    today_ym = (today.year, today.month)

    if commissioning_date is not None:
        years_elapsed = (today - commissioning_date).days / 365.25
        comm_ym = (commissioning_date.year, commissioning_date.month)

        # Cumulative inflation since commissioning (for display)
        cum_defl = _cpi.get_deflator(comm_ym, today_ym, inflation)
        cum_infl_pct = round((cum_defl - 1.0) * 100.0, 2)

        # Deflate each historical month's savings to today's PLN using real CPI
        real_sav = sum(
            _msav(r) / _cpi.get_deflator((r.year, r.month), today_ym, inflation)
            for r in complete if _msav(r) > 0
        )
        real_total_savings = round(real_sav, 2)
        real_subsidy = subsidy / _cpi.get_deflator(comm_ym, today_ym, inflation)
        real_total_return_v = round(real_sav + real_subsidy, 2)
        real_roi_pct_v = round(real_total_return_v / gross_investment * 100.0, 2) if gross_investment else None

        # Build monthly cashflow vector from commissioning through the asset's
        # full rated lifetime (not just until nominal payback — see the note
        # on ASSET_LIFETIME_YEARS above). Mirrors battery_sim.summarize().
        net_inv = gross_investment - subsidy
        hist_by_ym = {(r.year, r.month): _msav(r) for r in complete}
        n_hist = (today.year - commissioning_date.year) * 12 + (today.month - commissioning_date.month)
        cfs: list[float] = [-net_inv]
        for i in range(1, n_hist + 1):
            d = commissioning_date + relativedelta(months=i)
            cfs.append(hist_by_ym.get((d.year, d.month), 0.0))

        # Forward months out to the rated lifetime, degrading the trailing-
        # 12-month average savings rate year over year (panel output decay).
        horizon_months = round(asset_lifetime_years * 12)
        fwd_months_needed = max(0, horizon_months - n_hist)
        fwd = today + relativedelta(months=1)
        for i in range(1, fwd_months_needed + 1):
            years_out = i / 12.0
            degr = (1.0 - panel_degradation_pct_year / 100.0) ** years_out
            m_sav = max((monthly_avg_savings or 0.0) * factors.get(fwd.month, 1.0) * degr, 0.0)
            cfs.append(m_sav)
            fwd = fwd + relativedelta(months=1)

        monthly_disc = discount_rate / 12.0
        npv_v = round(_npv(cfs, monthly_disc), 2)

        irr_m = _irr(cfs)
        if irr_m is not None:
            irr_pct_v = round(((1.0 + irr_m) ** 12 - 1.0) * 100.0, 2)

        counterfactual_bond = round(net_inv * (1.0 + comparison_yield) ** years_elapsed, 2)
        counterfactual_delta = round(total_return - counterfactual_bond, 2)

    return RoiResult(
        self_consumption_savings=round(sc_sav,  2),
        feedin_revenue=round(fi_rev,  2),
        battery_arbitrage_savings=round(bat_sav, 2),
        total_savings=round(total_savings, 2),
        total_return=round(total_return,  2),
        roi_pct=round(roi_pct, 2),
        months_with_data=months_with_data,
        monthly_avg_savings=round(monthly_avg_savings, 2) if monthly_avg_savings is not None else None,
        monthly_avg_window=monthly_avg_window,
        remaining_to_recover=round(remaining_to_recover, 2),
        months_to_payback=round(months_to_payback, 1) if months_to_payback is not None else None,
        years_to_payback=years_to_payback,
        payback_date=payback_date,
        total_produced_kwh=round(total_produced, 1),
        total_exported_kwh=round(total_exported, 1),
        specific_yield_lifetime=round(specific_yield, 1),
        gross_investment=gross_investment,
        subsidy=subsidy,
        net_profit=round(max(0.0, total_return - gross_investment), 2),
        seasonal_factors={m: round(f, 4) for m, f in factors.items()},
        residual_cv=round(cv, 4),
        payback_date_seasonal=payback_date_seasonal,
        payback_date_p10=payback_date_p10,
        payback_date_p90=payback_date_p90,
        commissioning_date=commissioning_date,
        real_total_savings=real_total_savings,
        real_total_return=real_total_return_v,
        real_roi_pct=real_roi_pct_v,
        npv=npv_v,
        irr_pct=irr_pct_v,
        counterfactual_bond_value=counterfactual_bond,
        counterfactual_delta=counterfactual_delta,
        inflation_source=inflation_src,
        cumulative_inflation_pct=cum_infl_pct,
        self_consumption_rate_pct=self_consumption_rate,
        autarky_pct=autarky,
        co2_avoided_kg=co2_avoided,
        yoy_yield_delta_pct=yoy_delta,
        underperformance_pct=underperf_pct,
        underperformance_flag=underperf_flag,
    )
