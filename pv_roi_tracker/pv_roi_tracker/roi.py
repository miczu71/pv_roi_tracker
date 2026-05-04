"""Pure ROI calculation functions — no I/O, no side effects."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import ceil
from typing import Optional

from dateutil.relativedelta import relativedelta

from .models import MonthlyRecord

GROSS_INVESTMENT = 51_900.00  # zł — total project cost before any subsidy
SUBSIDY = 28_714.00           # zł — one-time government grant (Mój Prąd / Czyste Powietrze)
NET_INVESTMENT = GROSS_INVESTMENT - SUBSIDY  # 23_186.00 zł — kept for reference
SYSTEM_KWP = 6.72


@dataclass
class RoiResult:
    self_consumption_savings: float   # Σ autokonsumpcja oszczędności
    feedin_revenue: float             # Σ suma sprzedaży
    total_savings: float              # == spreadsheet oszczędności (savings streams only)
    total_return: float               # subsidy + total_savings  == spreadsheet "status"
    roi_pct: float                    # total_return / gross_investment × 100
    months_with_data: int
    monthly_avg_savings: Optional[float]
    monthly_avg_window: int           # how many complete months the avg is based on (max 12)
    remaining_to_recover: float       # gross_investment − total_return (floored at 0)
    months_to_payback: Optional[float]
    years_to_payback: Optional[float]
    payback_date: Optional[date]
    total_produced_kwh: float
    total_exported_kwh: float
    specific_yield_lifetime: float
    gross_investment: float
    subsidy: float


def calculate(
    records: list[MonthlyRecord],
    today: Optional[date] = None,
    gross_investment: float = GROSS_INVESTMENT,
    subsidy: float = SUBSIDY,
    system_kwp: float = SYSTEM_KWP,
) -> RoiResult:
    if today is None:
        today = date.today()

    self_consumption_savings = sum(r.self_consumed_savings_pln or 0.0 for r in records)
    feedin_revenue = sum(r.feedin_revenue_pln or 0.0 for r in records)
    total_savings = self_consumption_savings + feedin_revenue

    # Spreadsheet formula: status = dofinansowanie + oszczędności; % = status / inwestycja
    total_return = subsidy + total_savings
    roi_pct = (total_return / gross_investment * 100.0) if gross_investment else 0.0

    months_with_data = sum(
        1 for r in records
        if (r.self_consumed_savings_pln or 0.0) > 0 or (r.feedin_revenue_pln or 0.0) > 0
    )

    # Monthly avg from last 12 complete months — current month excluded (still in progress)
    current_ym = (today.year, today.month)
    complete = sorted(
        [r for r in records if (r.year, r.month) != current_ym],
        key=lambda r: (r.year, r.month),
    )
    window = [
        r for r in complete
        if (r.self_consumed_savings_pln or 0.0) > 0 or (r.feedin_revenue_pln or 0.0) > 0
    ][-12:]
    if window:
        monthly_avg_savings: Optional[float] = sum(
            (r.self_consumed_savings_pln or 0.0) + (r.feedin_revenue_pln or 0.0)
            for r in window
        ) / len(window)
        monthly_avg_window = len(window)
    else:
        monthly_avg_savings = None
        monthly_avg_window = 0

    remaining_to_recover = max(0.0, gross_investment - total_return)

    if monthly_avg_savings and monthly_avg_savings > 0:
        months_to_payback = remaining_to_recover / monthly_avg_savings
        years_to_payback = round(months_to_payback / 12, 2)
        payback_date = today + relativedelta(months=ceil(months_to_payback))
    else:
        months_to_payback = None
        years_to_payback = None
        payback_date = None

    total_produced_kwh = sum(r.produced_kwh or 0.0 for r in records)
    total_exported_kwh = sum(r.exported_kwh or 0.0 for r in records)
    specific_yield_lifetime = (total_produced_kwh / system_kwp) if system_kwp else 0.0

    return RoiResult(
        self_consumption_savings=round(self_consumption_savings, 2),
        feedin_revenue=round(feedin_revenue, 2),
        total_savings=round(total_savings, 2),
        total_return=round(total_return, 2),
        roi_pct=round(roi_pct, 2),
        months_with_data=months_with_data,
        monthly_avg_savings=round(monthly_avg_savings, 2) if monthly_avg_savings is not None else None,
        monthly_avg_window=monthly_avg_window,
        remaining_to_recover=round(remaining_to_recover, 2),
        months_to_payback=round(months_to_payback, 1) if months_to_payback is not None else None,
        years_to_payback=years_to_payback,
        payback_date=payback_date,
        total_produced_kwh=round(total_produced_kwh, 1),
        total_exported_kwh=round(total_exported_kwh, 1),
        specific_yield_lifetime=round(specific_yield_lifetime, 1),
        gross_investment=gross_investment,
        subsidy=subsidy,
    )
