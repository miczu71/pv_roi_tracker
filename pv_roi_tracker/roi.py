"""Pure ROI calculation functions — no I/O, no side effects."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from statistics import mean, stdev
from typing import Optional

from dateutil.relativedelta import relativedelta

from .models import MonthlyRecord
from . import cpi_fetcher as _cpi

GROSS_INVESTMENT = 51_900.00  # zł — total project cost before any subsidy
SUBSIDY = 28_714.00           # zł — one-time government grant (Mój Prąd / Czyste Powietrze)
NET_INVESTMENT = GROSS_INVESTMENT - SUBSIDY  # 23_186.00 zł — kept for reference
SYSTEM_KWP = 6.72


def _msav(r: MonthlyRecord) -> float:
    return (r.self_consumed_savings_pln or 0.0) + (r.feedin_revenue_pln or 0.0) + (r.battery_arbitrage_savings_pln or 0.0)


# ── Seasonal forecast helpers ────────────────────────────────────────────────

def _seasonal_factors(complete_records: list[MonthlyRecord]) -> dict[int, float]:
    """Per-calendar-month savings index relative to overall mean. Returns {1..12: float}."""
    by_month: dict[int, list[float]] = {m: [] for m in range(1, 13)}
    for r in complete_records:
        s = _msav(r)
        if s > 0:
            by_month[r.month].append(s)
    months_present = [m for m, vals in by_month.items() if vals]
    if len(months_present) < 2:
        return {m: 1.0 for m in range(1, 13)}
    month_means = {m: mean(vals) for m, vals in by_month.items() if vals}
    overall_mean = mean(month_means.values())
    if overall_mean <= 0:
        return {m: 1.0 for m in range(1, 13)}
    return {m: (month_means[m] / overall_mean if m in month_means else 1.0) for m in range(1, 13)}


def _residual_cv(complete_records: list[MonthlyRecord], monthly_avg: float, factors: dict[int, float]) -> float:
    """Coefficient of variation of actual vs seasonal-expected savings, capped at 0.5."""
    if monthly_avg <= 0:
        return 0.0
    ratios = [
        _msav(r) / (monthly_avg * factors.get(r.month, 1.0))
        for r in complete_records
        if _msav(r) > 0 and monthly_avg * factors.get(r.month, 1.0) > 0
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
    payback_date_p10: Optional[date]    # optimistic  (z = −1.28)
    payback_date_p90: Optional[date]    # pessimistic (z = +1.28)
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
    # Self-consumption vs export opportunity metrics
    self_consume_premium: Optional[float] = field(default=None)
    export_self_consume_potential: Optional[float] = field(default=None)


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
) -> RoiResult:
    if today is None:
        today = date.today()

    sc_sav  = sum(r.self_consumed_savings_pln  or 0.0 for r in records)
    fi_rev  = sum(r.feedin_revenue_pln          or 0.0 for r in records)
    bat_sav = sum(r.battery_arbitrage_savings_pln or 0.0 for r in records)

    # Opportunity metrics: value of self-consumption vs export, per-month price spread
    sc_premium_total = 0.0
    exp_potential_total = 0.0
    opp_months = 0
    for r in records:
        bp = r.buy_price_pln_kwh or 0.0
        fp = r.feedin_price_pln_kwh or 0.0
        if bp > 0 and fp > 0:
            spread = bp - fp
            sc_premium_total    += (r.self_consumed_kwh or 0.0) * spread
            exp_potential_total += (r.exported_kwh      or 0.0) * spread
            opp_months += 1
    self_consume_premium    = round(sc_premium_total,    2) if opp_months else None
    export_self_consume_pot = round(exp_potential_total, 2) if opp_months else None
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
    factors = _seasonal_factors(complete)
    cv = _residual_cv(complete, monthly_avg_savings or 0.0, factors)

    if monthly_avg_savings and monthly_avg_savings > 0:
        payback_date_seasonal = _walk_payback(remaining_to_recover, monthly_avg_savings, factors, today)
        payback_date_p10 = _walk_payback(remaining_to_recover, monthly_avg_savings, factors, today, z=-1.28, cv=cv)
        payback_date_p90 = _walk_payback(remaining_to_recover, monthly_avg_savings, factors, today, z=1.28,  cv=cv)
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

        # Build monthly cashflow vector from commissioning
        net_inv = gross_investment - subsidy
        hist_by_ym = {(r.year, r.month): _msav(r) for r in complete}
        n_hist = (today.year - commissioning_date.year) * 12 + (today.month - commissioning_date.month)
        cfs: list[float] = [-net_inv]
        for i in range(1, n_hist + 1):
            d = commissioning_date + relativedelta(months=i)
            cfs.append(hist_by_ym.get((d.year, d.month), 0.0))
        # Append projected forward months until P50 payback (max 120)
        fwd = today + relativedelta(months=1)
        fwd_rem = remaining_to_recover
        for _ in range(120):
            if fwd_rem <= 0:
                break
            m_sav = max((monthly_avg_savings or 0.0) * factors.get(fwd.month, 1.0), 0.0)
            cfs.append(m_sav)
            fwd_rem -= m_sav
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
        self_consume_premium=self_consume_premium,
        export_self_consume_potential=export_self_consume_pot,
    )
