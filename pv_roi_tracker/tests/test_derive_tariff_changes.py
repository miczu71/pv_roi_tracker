"""Tests for web._derive_tariff_changes() — invoice-derived tariff history."""
import pytest

from pv_roi_tracker.web import _derive_tariff_changes


def _inv(**kwargs) -> dict:
    """Build a minimal invoice record dict."""
    return kwargs


def test_empty_real_returns_empty():
    assert _derive_tariff_changes({}) == []


def test_single_invoice_is_base_row_with_no_changed():
    real = {'2025-01': _inv(peak_gross=1.23, offpeak_gross=0.63)}
    result = _derive_tariff_changes(real)
    assert len(result) == 1
    assert result[0]['effective_from'] == '2025-01'
    assert result[0]['changed'] == []  # base row, not a delta
    assert result[0]['rates']['peak_gross'] == pytest.approx(1.23, abs=1e-5)


def test_detects_gross_change_between_invoices():
    real = {
        '2025-01': _inv(peak_gross=1.00, offpeak_gross=0.50),
        '2025-02': _inv(peak_gross=1.23, offpeak_gross=0.63),
    }
    result = _derive_tariff_changes(real)
    assert len(result) == 2
    base, change = result[0], result[1]
    assert base['changed'] == []
    assert base['effective_from'] == '2025-01'
    assert change['effective_from'] == '2025-02'
    fields_changed = {d['field'] for d in change['changed']}
    assert 'peak_gross' in fields_changed
    assert 'offpeak_gross' in fields_changed


def test_no_false_change_below_epsilon():
    """Difference of 2e-5 in stawka (< 1e-4 epsilon) → no change row."""
    real = {
        '2025-01': _inv(peak_gross=1.2300),
        '2025-02': _inv(peak_gross=1.2300 + 2e-5),
    }
    result = _derive_tariff_changes(real)
    assert len(result) == 1  # only the base row


def test_detects_fixed_change_with_01_epsilon():
    """Fixed opłaty use epsilon=0.01 PLN; change of 0.02 must be detected."""
    real = {
        '2025-01': _inv(fixed_abonament_net=4.56),
        '2025-02': _inv(fixed_abonament_net=4.58),  # +0.02
    }
    result = _derive_tariff_changes(real)
    assert len(result) == 2
    changed_fields = {d['field'] for d in result[1]['changed']}
    assert 'fixed_abonament_net' in changed_fields


def test_snapshot_includes_all_non_none_fields():
    real = {
        '2025-01': _inv(peak_gross=1.23, offpeak_gross=0.63,
                        dist_oze_net=0.0073, fixed_mocowa_net=24.05),
        '2025-02': _inv(peak_gross=1.30, offpeak_gross=0.65,
                        dist_oze_net=0.0073, fixed_mocowa_net=24.05),
    }
    result = _derive_tariff_changes(real)
    # change row snapshot should include all fields from that invoice
    snap = result[1]['rates']
    assert 'peak_gross' in snap
    assert 'dist_oze_net' in snap
    assert 'fixed_mocowa_net' in snap


def test_sorted_by_billing_key():
    """Insertion order doesn't matter — changes are emitted in YYYY-MM order."""
    real = {
        '2026-01': _inv(peak_gross=1.25),
        '2024-01': _inv(peak_gross=1.00),
        '2025-01': _inv(peak_gross=1.20),
    }
    result = _derive_tariff_changes(real)
    dates = [r['effective_from'] for r in result]
    assert dates == sorted(dates)


def test_invoices_without_rate_fields_skipped():
    """Invoice without any _RATE_FIELDS present is silently skipped."""
    real = {
        '2025-01': _inv(imported_kwh=100),  # no rate fields
        '2025-02': _inv(peak_gross=1.23),
    }
    result = _derive_tariff_changes(real)
    assert len(result) == 1
    assert result[0]['effective_from'] == '2025-02'


def test_source_invoice_key():
    real = {'2025-06': _inv(peak_gross=1.23, offpeak_gross=0.63)}
    result = _derive_tariff_changes(real)
    assert result[0]['source_invoice'] == '2025-06'
