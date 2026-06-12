"""Tests for deposit.py — FIFO ledger, 12-month expiry, refund cap."""
from datetime import date

import pytest

from pv_roi_tracker.deposit import calculate, REFUND_CAP_DEFAULT
from pv_roi_tracker.models import MonthlyRecord


def _rec(y, m, feedin=0.0, purchased=0.0, buy_price=0.0):
    return MonthlyRecord(year=y, month=m, feedin_revenue_pln=feedin,
                         purchased_kwh=purchased, buy_price_pln_kwh=buy_price)


def test_fifo_consumption_drains_oldest_first():
    records = [
        _rec(2025, 1, feedin=100.0),
        _rec(2025, 2, feedin=100.0),
        _rec(2025, 3, purchased=100.0, buy_price=1.5),  # konsumpcja 150
    ]
    out = calculate(records, {}, today=date(2025, 4, 1))
    assert out.balance_model == pytest.approx(50.0)
    # partia ze stycznia w całości zjedzona, z lutego zostaje 50
    assert len(out.lots) == 1
    assert out.lots[0]['ym'] == '2025-02'
    assert out.lots[0]['remaining'] == pytest.approx(50.0)


def test_expiry_after_12_months_with_refund_cap():
    # zasilenie 100 w 2025-01, zero konsumpcji → przedawnia się na początku 2026-01
    records = [_rec(2025, 1, feedin=100.0)] + [
        _rec(2025 + (m - 1) // 12, (m - 1) % 12 + 1) for m in range(2, 14)
    ]
    out = calculate(records, {}, today=date(2026, 2, 1))
    # zwrot = 20% × 100, umorzenie = 80
    assert out.expired_refund_total == pytest.approx(20.0)
    assert out.expired_forfeit_total == pytest.approx(80.0)
    assert out.balance_model == 0.0


def test_refund_cap_30pct_for_hourly_rce():
    records = [_rec(2025, 1, feedin=100.0)] + [
        _rec(2025 + (m - 1) // 12, (m - 1) % 12 + 1) for m in range(2, 14)
    ]
    out = calculate(records, {}, today=date(2026, 2, 1), refund_cap=0.30)
    assert out.expired_refund_total == pytest.approx(30.0)
    assert out.expired_forfeit_total == pytest.approx(70.0)
    assert out.refund_cap_pct == 30


def test_partial_consumption_limits_refund_base_to_original():
    # 100 zasilenia, 85 skonsumowane → zostaje 15; cap 20% × 100 = 20 > 15 → cały zwrot, zero umorzenia
    records = [_rec(2025, 1, feedin=100.0),
               _rec(2025, 2, purchased=85.0, buy_price=1.0)] + [
        _rec(2025 + (m - 1) // 12, (m - 1) % 12 + 1) for m in range(3, 14)
    ]
    out = calculate(records, {}, today=date(2026, 2, 1))
    assert out.expired_refund_total == pytest.approx(15.0)
    assert out.expired_forfeit_total == pytest.approx(0.0)


def test_invoice_consumption_overrides_inverter_estimate():
    records = [_rec(2025, 1, feedin=100.0),
               _rec(2025, 2, purchased=100.0, buy_price=1.0)]  # szacunek: 100
    invoices = {'2025-02': {'deposit_used_pln': 40.0, 'deposit_previous_pln': 60.0}}
    out = calculate(records, invoices, today=date(2025, 3, 1))
    assert out.balance_model == pytest.approx(60.0)
    assert out.months[-1]['consumed'] == pytest.approx(40.0)
    assert out.months[-1]['consumed_source'] == 'faktura'
    assert out.invoice_latest_balance == pytest.approx(60.0)
    assert out.invoice_latest_month == '2025-02'


def test_balance_estimate_anchored_to_latest_invoice():
    records = [
        _rec(2025, 1, feedin=100.0),
        _rec(2025, 2, feedin=50.0),                       # po fakturze: +50
        _rec(2025, 3, purchased=10.0, buy_price=1.0),      # po fakturze: −10
    ]
    invoices = {'2025-01': {'deposit_used_pln': 0.0, 'deposit_previous_pln': 200.0}}
    out = calculate(records, invoices, today=date(2025, 4, 1))
    # kotwica 200 + 50 − 10 = 240 (model widzi tylko 140)
    assert out.balance_estimate == pytest.approx(240.0)
    assert out.balance_model == pytest.approx(140.0)
    # struktura wiekowa przeskalowana do szacunku
    assert sum(l['remaining'] for l in out.lots) == pytest.approx(240.0, abs=0.1)


def test_forecast_reports_upcoming_expiry():
    # zasilenie w 2025-06 → przedawni się 2026-06; today 2026-05 → expiring_1m
    records = [_rec(2025, 6, feedin=120.0)] + [
        _rec(2025 + (m - 1) // 12, (m - 1) % 12 + 1) for m in range(7, 17)
    ]
    out = calculate(records, {}, today=date(2026, 5, 15))
    assert out.expiring_1m == pytest.approx(120.0)
    assert out.expiring_3m == pytest.approx(120.0)
    # 20% zwrotu, 80% umorzenia w prognozie 12m
    assert out.projected_refund_12m == pytest.approx(24.0)
    assert out.projected_forfeit_12m == pytest.approx(96.0)


def test_empty_records():
    out = calculate([], {}, today=date(2026, 1, 1))
    assert out.balance_model == 0.0
    assert out.balance_estimate is None
    assert out.expiring_1m == 0.0
