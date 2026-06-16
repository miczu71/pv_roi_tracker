"""Tests for publisher.py — focused on the Phase 2 invoice-rate sensors and
their fallback-to-'unknown' behavior when no invoice has been parsed yet."""
import dataclasses

import pytest

from pv_roi_tracker.publisher import _SENSORS, _INVOICE_RATE_SENSORS, _render_value
from pv_roi_tracker.roi import RoiResult


@pytest.fixture
def result() -> RoiResult:
    # RoiResult has many required fields; zero them all out, we only care
    # about the invoice-rate sensor branch in _render_value here.
    fields = {f.name: 0.0 for f in dataclasses.fields(RoiResult)}
    return RoiResult(**fields)


def _sensor(slug):
    return next(s for s in _SENSORS if s.slug == slug)


def test_all_invoice_rate_sensors_are_declared():
    slugs = {s.slug for s in _SENSORS}
    for slug in _INVOICE_RATE_SENSORS:
        assert slug in slugs, f'{slug} missing from _SENSORS'


def test_invoice_rate_sensors_are_monetary_or_per_kwh():
    for slug in _INVOICE_RATE_SENSORS:
        s = _sensor(slug)
        assert s.device_class == 'monetary'
        assert s.unit in ('PLN', 'PLN/kWh')


def test_render_value_uses_invoice_rates_dict(result):
    rates = {'dist_oze_net': 0.0035, 'fixed_mocowa_net': 16.01, 'peak_gross': 1.3}
    assert _render_value(_sensor('rate_oze_net'), result, invoice_rates=rates) == '0.0035'
    assert _render_value(_sensor('fixed_mocowa_net'), result, invoice_rates=rates) == '16.01'
    assert _render_value(_sensor('rate_peak_gross'), result, invoice_rates=rates) == '1.3'


def test_render_value_unknown_when_rate_missing(result):
    assert _render_value(_sensor('rate_energy_peak_net'), result, invoice_rates={}) == 'unknown'
    assert _render_value(_sensor('rate_energy_peak_net'), result, invoice_rates=None) == 'unknown'


def test_render_value_unaffected_for_non_rate_sensors(result):
    # Sanity: passing invoice_rates must not disturb the existing RoiResult-backed sensors.
    rates = {'dist_oze_net': 0.0035}
    assert _render_value(_sensor('roi_pct'), result, invoice_rates=rates) == '0.0'
