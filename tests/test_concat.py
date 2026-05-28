"""Tests for concat.py."""
import pytest
from pv_roi_tracker.models import MonthlyRecord
from pv_roi_tracker.concat import concat


def rec(year, month, savings=100.0, feedin=20.0):
    return MonthlyRecord(year=year, month=month,
                         self_consumed_savings_pln=savings, feedin_revenue_pln=feedin)


def test_no_current_month():
    historic = [rec(2023, 5), rec(2023, 6)]
    result = concat(historic, None)
    assert len(result) == 2


def test_current_month_appended():
    historic = [rec(2023, 5)]
    result = concat(historic, rec(2026, 5))
    assert len(result) == 2
    assert result[-1].year == 2026


def test_current_month_overrides_same_key():
    historic = [rec(2026, 5, savings=100.0)]
    current = rec(2026, 5, savings=999.0)
    result = concat(historic, current)
    assert len(result) == 1
    assert result[0].self_consumed_savings_pln == pytest.approx(999.0)


def test_result_is_sorted():
    historic = [rec(2024, 3), rec(2023, 12), rec(2024, 1)]
    result = concat(historic, None)
    keys = [(r.year, r.month) for r in result]
    assert keys == sorted(keys)


def test_original_historic_not_mutated():
    historic = [rec(2023, 5)]
    original_len = len(historic)
    concat(historic, rec(2026, 1))
    assert len(historic) == original_len
