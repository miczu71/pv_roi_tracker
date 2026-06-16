"""Tests for compute_tariff_tab()'s effective-rate parameters — the Phase 2
override of FIXED_GROSS_PLN / 1.23 / 0.63 by the latest parsed invoice, with
the original constants as fallback when no invoice is available."""
import pytest

from pv_roi_tracker.tariff_analysis import (
    compute_tariff_tab, FIXED_GROSS_PLN, _DEFAULT_PEAK_GROSS, _DEFAULT_OFFPEAK_GROSS,
)


def _call(**overrides):
    return compute_tariff_tab(
        records=[],
        dynamic_monthly_stats={},
        g12w_monthly_stats={},
        current_roi=None,
        current_month_live={},
        tariff_history_7d={},
        **overrides,
    )


def test_defaults_used_when_no_invoice_rates_passed():
    tc = _call()
    s = tc['summary']
    assert s['fixed_gross_pln'] == FIXED_GROSS_PLN
    assert s['effective_peak_gross'] == _DEFAULT_PEAK_GROSS
    assert s['effective_offpeak_gross'] == _DEFAULT_OFFPEAK_GROSS


def test_invoice_rates_override_defaults():
    tc = _call(fixed_gross_pln=55.0, peak_gross=1.40, offpeak_gross=0.70)
    s = tc['summary']
    assert s['fixed_gross_pln'] == 55.0
    assert s['effective_peak_gross'] == 1.40
    assert s['effective_offpeak_gross'] == 0.70


def test_partial_override_falls_back_for_missing_fields():
    """Only peak_gross supplied — offpeak and fixed must still fall back."""
    tc = _call(peak_gross=1.50)
    s = tc['summary']
    assert s['effective_peak_gross'] == 1.50
    assert s['effective_offpeak_gross'] == _DEFAULT_OFFPEAK_GROSS
    assert s['fixed_gross_pln'] == FIXED_GROSS_PLN
