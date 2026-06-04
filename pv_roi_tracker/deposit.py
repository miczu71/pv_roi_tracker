"""Pure deposit ledger — compute from inverter data and compare with invoice values.

No I/O, no side effects (mirrors roi.py style).

The Polish prosument deposit ("depozyt prosumencki") is credited as:
    accrued = exported_kWh × RCEm (monthly market price)

This module computes the ledger independently from inverter data (MonthlyRecord),
then cross-checks against the invoice-reported deposit fields.

Note on settlement timing: Tauron issues monthly forecast invoices and credits the
deposit with a 1-2 month lag (RCEm is published ~11th of the following month).
The per-month comparison with invoice dep_previous will therefore differ in timing;
cumulative totals converge over time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import mean
from typing import Optional

from dateutil.relativedelta import relativedelta

from .models import MonthlyRecord


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class DepositMonthRow:
    """Per-month deposit figures for the ledger."""
    year: int
    month: int
    # Inverter-computed (source of truth)
    accrued: float              # exported_kwh × RCEm = feedin_revenue_pln
    cumulative_accrued: float   # running total since commissioning
    import_cost: float          # purchased_kwh × buy_price (gross)
    # Simple running-balance model (FIFO, no settlement timing lag)
    consumed: float             # min(balance_before + accrued, import_cost)
    balance: float              # running balance at end of month
    # Invoice cross-check (None if no invoice for this month)
    invoice_balance: Optional[float]   # dep_previous_pln from Tauron invoice
    invoice_consumed: Optional[float]  # dep_used_pln from Tauron invoice


@dataclass
class DepositForecastMonth:
    """One projected month in the deposit forecast."""
    year: int
    month: int
    projected_accrued: float
    projected_import_cost: float
    projected_balance: float


@dataclass
class DepositResult:
    """Complete deposit ledger result."""
    # Totals
    total_inverter_accrued: float   # Σ accrued (inverter-computed, = feedin_revenue total)
    total_invoice_consumed: float   # Σ dep_used from all Tauron invoices
    total_model_consumed: float     # Σ consumed by simple FIFO model
    # Current balance
    current_balance_model: float    # Simple FIFO model's running balance (incl. current month)
    invoice_latest_balance: Optional[float]   # Latest dep_previous from Tauron invoice
    invoice_latest_month: Optional[str]       # YYYY-MM key of that invoice
    # Best estimate: invoice anchor + inverter delta for months after last invoice
    current_balance_estimate: Optional[float]
    balance_divergence: Optional[float]  # model − invoice_latest (informational)
    # Averages (last 12 complete months)
    avg_monthly_accrual: float
    avg_monthly_import_cost: float
    # Monthly detail (chronological, complete months only)
    months: list
    # Forecast
    forecast: list
    projected_balance_3m: Optional[float]
    projected_balance_6m: Optional[float]
    projected_balance_12m: Optional[float]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _seasonal_factors(
    rows: list[DepositMonthRow],
    value_fn,
    today_ym: tuple[int, int],
) -> dict[int, float]:
    """Per-calendar-month seasonal index for any row field. Returns {1..12: float}."""
    by_month: dict[int, list[float]] = {m: [] for m in range(1, 13)}
    for r in rows:
        if (r.year, r.month) >= today_ym:
            continue
        v = value_fn(r)
        if v > 0:
            by_month[r.month].append(v)
    present = [m for m, vals in by_month.items() if vals]
    if len(present) < 2:
        return {m: 1.0 for m in range(1, 13)}
    month_means = {m: mean(vals) for m, vals in by_month.items() if vals}
    overall = mean(month_means.values())
    if overall <= 0:
        return {m: 1.0 for m in range(1, 13)}
    return {m: (month_means[m] / overall if m in month_means else 1.0) for m in range(1, 13)}


# ── Main calculation ──────────────────────────────────────────────────────────

def calculate(
    records: list[MonthlyRecord],
    invoice_store_data: dict,  # from invoice_store.load() — {YYYY-MM: record_dict, ...}
    today: Optional[date] = None,
    forecast_months: int = 12,
) -> DepositResult:
    """Compute deposit ledger from inverter MonthlyRecords and cross-check with invoices.

    Args:
        records:            all MonthlyRecords from historic_store.load()
        invoice_store_data: dict from invoice_store.load() — keyed YYYY-MM or unparsed-...
        today:              override today (default: date.today())
        forecast_months:    how many months to project forward (default 12)
    """
    if today is None:
        today = date.today()
    today_ym = (today.year, today.month)

    # Sort all records chronologically
    sorted_recs = sorted(records, key=lambda r: (r.year, r.month))
    complete = [r for r in sorted_recs if (r.year, r.month) < today_ym]

    # ── Build per-month rows using simple FIFO balance model ─────────────────
    balance = 0.0
    cumulative_accrued = 0.0
    total_model_consumed = 0.0
    month_rows: list[DepositMonthRow] = []

    for r in complete:
        mk = f'{r.year}-{r.month:02d}'
        accrued = r.feedin_revenue_pln or 0.0
        import_cost = round((r.purchased_kwh or 0.0) * (r.buy_price_pln_kwh or 0.0), 2)

        available = balance + accrued
        consumed = round(min(available, import_cost), 2)
        balance = round(max(0.0, available - consumed), 2)

        cumulative_accrued = round(cumulative_accrued + accrued, 2)
        total_model_consumed = round(total_model_consumed + consumed, 2)

        inv = invoice_store_data.get(mk) or {}
        inv_balance = inv.get('deposit_previous_pln')
        inv_consumed = inv.get('deposit_used_pln')

        month_rows.append(DepositMonthRow(
            year=r.year,
            month=r.month,
            accrued=round(accrued, 2),
            cumulative_accrued=cumulative_accrued,
            import_cost=import_cost,
            consumed=consumed,
            balance=balance,
            invoice_balance=inv_balance,
            invoice_consumed=inv_consumed,
        ))

    # ── Include current month (partial) ──────────────────────────────────────
    current_rec = next((r for r in records if (r.year, r.month) == today_ym), None)
    current_accrued = 0.0
    current_balance = balance   # will be updated below
    if current_rec:
        current_accrued = current_rec.feedin_revenue_pln or 0.0
        current_import = round((current_rec.purchased_kwh or 0.0) * (current_rec.buy_price_pln_kwh or 0.0), 2)
        avail = balance + current_accrued
        curr_consumed = round(min(avail, current_import), 2)
        current_balance = round(max(0.0, avail - curr_consumed), 2)

    # ── Total invoice-reported consumed ──────────────────────────────────────
    total_invoice_consumed = round(sum(
        (row.invoice_consumed or 0.0) for row in month_rows
    ), 2)

    # ── Latest invoice balance ────────────────────────────────────────────────
    invoice_latest_balance: Optional[float] = None
    invoice_latest_month: Optional[str] = None
    for row in reversed(month_rows):
        if row.invoice_balance is not None:
            invoice_latest_balance = row.invoice_balance
            invoice_latest_month = f'{row.year}-{row.month:02d}'
            break

    # ── Invoice-anchored estimate ─────────────────────────────────────────────
    # Start from the latest invoice's dep_previous (what Tauron actually recorded),
    # then add inverter-derived accruals and subtract estimated consumption
    # for months after that invoice, up to today.
    current_balance_estimate: Optional[float] = None
    if invoice_latest_balance is not None and invoice_latest_month is not None:
        latest_inv_ym = (int(invoice_latest_month[:4]), int(invoice_latest_month[5:]))
        est = invoice_latest_balance
        for row in month_rows:
            if (row.year, row.month) <= latest_inv_ym:
                continue
            avail = est + row.accrued
            est = round(max(0.0, avail - min(avail, row.import_cost)), 2)
        if current_rec:
            avail = est + current_accrued
            curr_imp = round((current_rec.purchased_kwh or 0.0) * (current_rec.buy_price_pln_kwh or 0.0), 2)
            est = round(max(0.0, avail - min(avail, curr_imp)), 2)
        current_balance_estimate = est

    # ── Divergence ────────────────────────────────────────────────────────────
    balance_divergence: Optional[float] = None
    if invoice_latest_balance is not None:
        balance_divergence = round(current_balance - invoice_latest_balance, 2)

    # ── Averages (last 12 complete months with accrual > 0) ──────────────────
    window = [r for r in month_rows if r.accrued > 0][-12:]
    if window:
        avg_accrual = round(mean(r.accrued for r in window), 2)
        avg_import  = round(mean(r.import_cost for r in window), 2)
    else:
        avg_accrual = 0.0
        avg_import  = 0.0

    # ── Seasonal factors for forecast ────────────────────────────────────────
    sf_acc = _seasonal_factors(month_rows, lambda r: r.accrued,      today_ym)
    sf_imp = _seasonal_factors(month_rows, lambda r: r.import_cost,  today_ym)

    # ── Forecast ─────────────────────────────────────────────────────────────
    forecast_bal = current_balance
    forecast_list: list[DepositForecastMonth] = []
    cursor = today + relativedelta(months=1)
    for _ in range(forecast_months):
        proj_acc = round(avg_accrual * sf_acc.get(cursor.month, 1.0), 2)
        proj_imp = round(avg_import  * sf_imp.get(cursor.month, 1.0), 2)
        avail = forecast_bal + proj_acc
        proj_cons = round(min(avail, proj_imp), 2)
        forecast_bal = round(max(0.0, avail - proj_cons), 2)
        forecast_list.append(DepositForecastMonth(
            year=cursor.year, month=cursor.month,
            projected_accrued=proj_acc,
            projected_import_cost=proj_imp,
            projected_balance=forecast_bal,
        ))
        cursor = cursor + relativedelta(months=1)

    p3  = forecast_list[2].projected_balance  if len(forecast_list) >= 3  else None
    p6  = forecast_list[5].projected_balance  if len(forecast_list) >= 6  else None
    p12 = forecast_list[11].projected_balance if len(forecast_list) >= 12 else None

    return DepositResult(
        total_inverter_accrued=round(cumulative_accrued, 2),
        total_invoice_consumed=total_invoice_consumed,
        total_model_consumed=total_model_consumed,
        current_balance_model=round(current_balance, 2),
        invoice_latest_balance=invoice_latest_balance,
        invoice_latest_month=invoice_latest_month,
        current_balance_estimate=current_balance_estimate,
        balance_divergence=balance_divergence,
        avg_monthly_accrual=avg_accrual,
        avg_monthly_import_cost=avg_import,
        months=month_rows,
        forecast=forecast_list,
        projected_balance_3m=p3,
        projected_balance_6m=p6,
        projected_balance_12m=p12,
    )
