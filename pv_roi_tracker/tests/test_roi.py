"""Tests for roi.py — known-answer and guard-clause tests."""
import pytest
from datetime import date
from pv_roi_tracker.models import MonthlyRecord
from pv_roi_tracker.roi import (calculate, degradation_analysis, bill_comparison,
                                underperformance_analysis,
                                GROSS_INVESTMENT, SUBSIDY, SYSTEM_KWP)

_GROSS = GROSS_INVESTMENT   # 51_900.00
_SUB   = SUBSIDY            # 28_714.00


def month(year, month_num, savings=500.0, feedin=100.0, produced=600.0, exported=200.0):
    return MonthlyRecord(
        year=year, month=month_num,
        self_consumed_savings_pln=savings,
        feedin_revenue_pln=feedin,
        produced_kwh=produced,
        exported_kwh=exported,
    )


# ── Savings streams ──────────────────────────────────────────────────────────────────────────────────

def test_total_savings_is_sum_of_both_streams():
    records = [month(2023, 5, savings=400.0, feedin=100.0)]
    r = calculate(records, subsidy=0.0)
    assert r.self_consumption_savings == pytest.approx(400.0)
    assert r.feedin_revenue == pytest.approx(100.0)
    assert r.total_savings == pytest.approx(500.0)


def test_null_feedin_treated_as_zero():
    """Months written before RCEm is known have feedin_revenue_pln=None — must not crash."""
    rec = MonthlyRecord(year=2026, month=5,
                        self_consumed_savings_pln=300.0, feedin_revenue_pln=None)
    r = calculate([rec], subsidy=0.0)
    assert r.feedin_revenue == pytest.approx(0.0)
    assert r.total_savings == pytest.approx(300.0)


# ── Seasonal payback percentiles ────────────────────────────────────────────────────────────────

def test_payback_percentiles_ordered_p10_before_p90():
    """P10 (optimistic) must fall on or before P50 (seasonal), which must fall
    on or before P90 (pessimistic). Regression guard for the swapped z-signs bug."""
    today = date(2026, 7, 15)
    # 24 months of varied savings so residual_cv > 0 and the fan has width;
    # gross large enough that payback is still in the future (dates not None).
    records = []
    for i in range(24):
        y = 2024 + (i // 12)
        m = (i % 12) + 1
        sav = 400.0 + (i % 5) * 60.0   # variation → non-zero residual CV
        records.append(month(y, m, savings=sav, feedin=80.0))
    r = calculate(records, gross_investment=60_000.0, subsidy=0.0, today=today)
    assert r.payback_date_p10 is not None
    assert r.payback_date_seasonal is not None
    assert r.payback_date_p90 is not None
    assert r.payback_date_p10 <= r.payback_date_seasonal <= r.payback_date_p90


# ── ROI % ───────────────────────────────────────────────────────────────────────────────────────

def test_roi_pct_known_answer():
    # 3 months × (500 + 100) = 1800 total savings; subsidy=0 → roi = savings/gross
    records = [month(2023, m) for m in (5, 6, 7)]
    r = calculate(records, gross_investment=_GROSS, subsidy=0.0)
    assert r.total_savings == pytest.approx(1800.0)
    assert r.roi_pct == pytest.approx(1800.0 / _GROSS * 100, abs=0.01)


def test_roi_pct_includes_subsidy():
    """Spreadsheet formula: ROI = (subsidy + savings) / gross_investment × 100."""
    records = [month(2023, m) for m in (5, 6, 7)]
    r = calculate(records, gross_investment=_GROSS, subsidy=_SUB)
    expected = (_SUB + 1800.0) / _GROSS * 100
    assert r.total_return == pytest.approx(_SUB + 1800.0)
    assert r.roi_pct == pytest.approx(expected, abs=0.01)


def test_roi_pct_subsidy_only_when_no_savings():
    """With no data, ROI reflects subsidy contribution alone."""
    r = calculate([], gross_investment=_GROSS, subsidy=_SUB)
    assert r.roi_pct == pytest.approx(_SUB / _GROSS * 100, abs=0.01)
    assert r.total_return == pytest.approx(_SUB)


# ── Monthly average ──────────────────────────────────────────────────────────────────────────────────

def test_monthly_avg_savings():
    records = [month(2023, m, savings=400.0, feedin=200.0) for m in range(1, 13)]
    r = calculate(records)
    assert r.months_with_data == 12
    assert r.monthly_avg_savings == pytest.approx(600.0)


def test_monthly_avg_none_when_no_data():
    r = calculate([])
    assert r.monthly_avg_savings is None


# ── Payback ────────────────────────────────────────────────────────────────────────────────────────────

def test_payback_date_in_future():
    today = date(2026, 5, 1)
    # total_savings = 5 × 600 = 3000
    # remaining = 51900 − (28714 + 3000) = 20186; avg = 600
    records = [month(2023, m, savings=500.0, feedin=100.0) for m in range(1, 6)]
    r = calculate(records, today=today, gross_investment=_GROSS, subsidy=_SUB)
    assert r.remaining_to_recover == pytest.approx(20186.0)
    assert r.payback_date is not None
    assert r.payback_date > today


def test_already_paid_back():
    records = [month(2023, m, savings=5000.0, feedin=0.0) for m in range(1, 10)]
    # 9 × 5000 = 45000; total_return = 28714 + 45000 = 73714 > 51900
    r = calculate(records, today=date(2026, 5, 1), gross_investment=_GROSS, subsidy=_SUB)
    assert r.remaining_to_recover == pytest.approx(0.0)
    assert r.months_to_payback == pytest.approx(0.0)


def test_payback_none_when_no_avg():
    r = calculate([])
    assert r.months_to_payback is None
    assert r.years_to_payback is None
    assert r.payback_date is None


# ── Energy totals ─────────────────────────────────────────────────────────────────────────────────

def test_total_produced_kwh():
    records = [month(2023, m, produced=500.0) for m in range(1, 5)]
    r = calculate(records)
    assert r.total_produced_kwh == pytest.approx(2000.0)


def test_total_exported_kwh():
    records = [month(2023, m, exported=150.0) for m in range(1, 4)]
    r = calculate(records)
    assert r.total_exported_kwh == pytest.approx(450.0)


def test_specific_yield():
    records = [month(2023, 5, produced=672.0)]
    r = calculate(records, system_kwp=6.72)
    assert r.specific_yield_lifetime == pytest.approx(100.0)


# ── Autokonsumpcja / autarkia / CO2 ──────────────────────────────────────────

def test_self_consumption_rate_and_autarky():
    rec = MonthlyRecord(year=2025, month=5, produced_kwh=600.0,
                        consumed_kwh=400.0, self_consumed_kwh=240.0)
    r = calculate([rec])
    assert r.self_consumption_rate_pct == pytest.approx(40.0)   # 240/600
    assert r.autarky_pct == pytest.approx(60.0)                 # 240/400


def test_ratios_none_without_data():
    r = calculate([])
    assert r.self_consumption_rate_pct is None
    assert r.autarky_pct is None


def test_co2_avoided_uses_factor():
    records = [month(2023, 5, produced=1000.0)]
    r = calculate(records, co2_factor=0.6)
    assert r.co2_avoided_kg == pytest.approx(600.0)


# ── Degradacja ────────────────────────────────────────────────────────────────

def _prod(y, m, kwh):
    return MonthlyRecord(year=y, month=m, produced_kwh=kwh)


def test_degradation_yoy_paired_months():
    # 2025: 100/mies., 2026 (sty–maj): 95/mies. → r/r −5%
    records = ([_prod(2025, m, 100.0) for m in range(1, 13)]
               + [_prod(2026, m, 95.0) for m in range(1, 6)])
    out = degradation_analysis(records, system_kwp=1.0, today=date(2026, 6, 15))
    assert out['pairs_used'] == 5
    assert out['yoy_delta_pct'] == pytest.approx(-5.0)


def test_degradation_yoy_none_with_few_pairs():
    records = [_prod(2026, m, 100.0) for m in range(1, 6)]
    out = degradation_analysis(records, system_kwp=1.0, today=date(2026, 6, 15))
    assert out['yoy_delta_pct'] is None


def test_degradation_rolling_and_trend():
    # 24 mies. liniowego spadku 120→97 kWh/mies. → ujemny trend
    records = []
    y, m = 2024, 7
    for i in range(24):
        records.append(_prod(y, m, 120.0 - i))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    out = degradation_analysis(records, system_kwp=1.0, today=date(2026, 7, 15))
    assert len(out['rolling']) == 13   # okna 12-mies. od mies. 12 do 24
    assert out['trend_pct_per_year'] is not None
    assert out['trend_pct_per_year'] < -5.0


def test_degradation_yearly_complete_flag():
    records = [_prod(2025, m, 100.0) for m in range(1, 13)] + [_prod(2026, 1, 100.0)]
    out = degradation_analysis(records, system_kwp=2.0, today=date(2026, 3, 1))
    yearly = {r['year']: r for r in out['yearly']}
    assert yearly[2025]['complete'] is True
    assert yearly[2025]['yield_kwh_kwp'] == pytest.approx(600.0)
    assert yearly[2026]['complete'] is False


# ── bill_comparison ───────────────────────────────────────────────────────────


def _bill_month(year, month_num, consumed=400.0, purchased=100.0,
                buy_price=1.0, feedin_rev=50.0):
    return MonthlyRecord(
        year=year, month=month_num,
        consumed_kwh=consumed,
        purchased_kwh=purchased,
        buy_price_pln_kwh=buy_price,
        feedin_revenue_pln=feedin_rev,
    )


def test_bill_comparison_basic():
    # consumed=400, buy=1.0 → without_pv=400
    # purchased=100, buy=1.0, feedin=50 → with_pv=100−50=50
    # saved = 400−50 = 350
    r = _bill_month(2025, 1, consumed=400.0, purchased=100.0, buy_price=1.0, feedin_rev=50.0)
    out = bill_comparison([r])
    assert len(out['months']) == 1
    m = out['months'][0]
    assert m['bill_without_pv'] == pytest.approx(400.0)
    assert m['bill_with_pv']    == pytest.approx(50.0)
    assert m['saved']           == pytest.approx(350.0)
    assert m['savings_pct']     == pytest.approx(87.5)
    assert out['total_saved']   == pytest.approx(350.0)


def test_bill_comparison_skips_missing_data():
    # Month without consumed_kwh or buy_price should be skipped
    no_consumed = MonthlyRecord(year=2025, month=2, purchased_kwh=100.0, buy_price_pln_kwh=1.0)
    no_price    = MonthlyRecord(year=2025, month=3, consumed_kwh=400.0)
    out = bill_comparison([no_consumed, no_price])
    assert out['months'] == []
    assert out['total_saved'] == pytest.approx(0.0)
    assert out['avg_savings_pct'] is None


def test_bill_comparison_multiple_months():
    records = [
        _bill_month(2025, m, consumed=400.0, purchased=100.0, buy_price=1.0, feedin_rev=50.0)
        for m in range(1, 4)
    ]
    out = bill_comparison(records)
    assert len(out['months']) == 3
    assert out['total_without_pv'] == pytest.approx(1200.0)
    assert out['total_with_pv']    == pytest.approx(150.0)
    assert out['total_saved']      == pytest.approx(1050.0)
    assert out['avg_savings_pct']  == pytest.approx(87.5)


def test_bill_comparison_sorted_chronologically():
    # Records in reverse order should come out sorted ym ascending
    records = [
        _bill_month(2025, 3),
        _bill_month(2025, 1),
        _bill_month(2025, 2),
    ]
    out = bill_comparison(records)
    yms = [m['ym'] for m in out['months']]
    assert yms == sorted(yms)


# ── underperformance_analysis ─────────────────────────────────────────────────


def _prod_month(year, month_num, kwh):
    return MonthlyRecord(year=year, month=month_num, produced_kwh=kwh)


def test_underperformance_ok_when_normal():
    # 3 years of June at 600 kWh, last June also 600 → ok
    records = ([_prod_month(y, 6, 600.0) for y in range(2023, 2026)]
               + [_prod_month(2026, 6, 600.0)])
    out = underperformance_analysis(records, today=date(2026, 7, 1))
    assert out['flag'] == 'ok'
    assert out['deviation_pct'] == pytest.approx(0.0)
    assert out['n_prior_years'] == 3


def test_underperformance_uwaga_when_below_threshold():
    # Prior years: 600 kWh in June; last year: 510 kWh → -15% → uwaga
    records = ([_prod_month(y, 6, 600.0) for y in range(2023, 2026)]
               + [_prod_month(2026, 6, 510.0)])
    out = underperformance_analysis(records, today=date(2026, 7, 1))
    assert out['flag'] == 'uwaga'
    assert out['deviation_pct'] == pytest.approx(-15.0)


def test_underperformance_ok_when_above_threshold():
    # -5% → below 10% threshold → ok
    records = ([_prod_month(y, 6, 600.0) for y in range(2023, 2026)]
               + [_prod_month(2026, 6, 570.0)])
    out = underperformance_analysis(records, today=date(2026, 7, 1))
    assert out['flag'] == 'ok'
    assert out['deviation_pct'] == pytest.approx(-5.0)


def test_underperformance_none_with_no_prior_year():
    # Only one June — no prior year → deviation_pct=None, flag='ok'
    records = [_prod_month(2026, 6, 500.0)]
    out = underperformance_analysis(records, today=date(2026, 7, 1))
    assert out['flag'] == 'ok'
    assert out['deviation_pct'] is None
    assert out['n_prior_years'] == 0


def test_underperformance_none_with_empty_records():
    out = underperformance_analysis([], today=date(2026, 7, 1))
    assert out['flag'] == 'ok'
    assert out['last_closed_ym'] is None


def test_underperformance_custom_threshold():
    # -8% below 15% threshold → ok
    records = ([_prod_month(y, 6, 600.0) for y in range(2024, 2026)]
               + [_prod_month(2026, 6, 552.0)])
    out = underperformance_analysis(records, today=date(2026, 7, 1), alert_threshold_pct=-15.0)
    assert out['flag'] == 'ok'
