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


def test_invoice_rate_sensors_device_class_matches_unit():
    """HA's 'monetary' device class requires a currency unit. Per-kWh rates must
    therefore NOT be monetary (PLN/kWh is invalid → warnings + no long-term stats);
    only whole-PLN/month fixed charges may be monetary."""
    for slug in _INVOICE_RATE_SENSORS:
        s = _sensor(slug)
        assert s.unit in ('PLN', 'PLN/kWh')
        if s.unit == 'PLN/kWh':
            assert s.device_class is None
        else:  # 'PLN'
            assert s.device_class == 'monetary'


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


# ── symulacja taryf (tariffsim_*) ────────────────────────────────────────────

_SIM = {
    'current_month': {'ym': '2026-10', 'gross': {'G12w': 55.5, 'dyn_a': None}},
    'last12': {'gross': {'G12w': 1234.5678, 'dyn_a': None}, 'cheapest': 'G12w',
               'saving_vs_g12w': 0.0,
               'gross_c': {'G12w': 1000.0, 'G13s': 900.5, 'G11': None}},
    'best_year': {'best': {'tariff': 'G13s', 'variant': 'C', 'gross': 900.5,
                           'saving_pln': 334.0678, 'confirmed': False}},
}


def test_tariffsim_ma_23_sensory_monetary_bez_state_class():
    sims = [s for s in _SENSORS if s.slug.startswith('tariffsim_')]
    assert len(sims) == 23
    for s in sims:
        assert s.state_class is None
        if s.slug not in ('tariffsim_cheapest', 'tariffsim_best_variant'):
            assert s.unit == 'PLN' and s.device_class == 'monetary'
    assert _sensor('tariffsim_cheapest').unit is None
    assert _sensor('tariffsim_best_variant').unit is None


def test_render_tariffsim(result):
    assert _render_value(_sensor('tariffsim_12m_g13s'), result, tariff_sim={'last12': {'gross': {'G13s': 77.5}}}) == '77.5'
    assert _render_value(_sensor('tariffsim_12m_g12w'), result, tariff_sim=_SIM) == '1234.5678'
    assert _render_value(_sensor('tariffsim_month_g12w'), result, tariff_sim=_SIM) == '55.5'
    assert _render_value(_sensor('tariffsim_cheapest'), result, tariff_sim=_SIM) == 'G12w'
    assert _render_value(_sensor('tariffsim_saving_vs_g12w'), result, tariff_sim=_SIM) == '0.0'


def test_render_tariffsim_c_i_best(result):
    assert _render_value(_sensor('tariffsim_12m_c_g13s'), result, tariff_sim=_SIM) == '900.5'
    assert _render_value(_sensor('tariffsim_12m_c_g12w'), result, tariff_sim=_SIM) == '1000.0'
    assert _render_value(_sensor('tariffsim_best_variant'), result, tariff_sim=_SIM) == 'G13s C'
    assert _render_value(_sensor('tariffsim_best_cost'), result, tariff_sim=_SIM) == '900.5'
    assert _render_value(_sensor('tariffsim_best_saving'), result, tariff_sim=_SIM) == '334.0678'


def test_render_tariffsim_c_i_best_unknown(result):
    assert _render_value(_sensor('tariffsim_12m_c_g13'), result, tariff_sim=_SIM) == 'unknown'
    assert _render_value(_sensor('tariffsim_12m_c_g12'), result) == 'unknown'
    for slug in ('tariffsim_best_variant', 'tariffsim_best_cost', 'tariffsim_best_saving'):
        assert _render_value(_sensor(slug), result, tariff_sim={'best_year': None}) == 'unknown'
        assert _render_value(_sensor(slug), result) == 'unknown'


def test_render_tariffsim_unknown(result):
    assert _render_value(_sensor('tariffsim_12m_g12w'), result) == 'unknown'
    assert _render_value(_sensor('tariffsim_12m_dyn_a'), result, tariff_sim=_SIM) == 'unknown'
    assert _render_value(_sensor('tariffsim_12m_g11'), result, tariff_sim=_SIM) == 'unknown'
    assert _render_value(_sensor('tariffsim_cheapest'), result, tariff_sim={}) == 'unknown'


def test_publish_roi_publikuje_23_tematy_tariffsim(result):
    from unittest.mock import MagicMock
    from pv_roi_tracker.publisher import MQTTPublisher
    pub = MQTTPublisher('localhost', 1883, '', '')
    pub._client = MagicMock()
    pub._connected = True
    result = dataclasses.replace(result, roi_pct=1.0)
    pub.publish_roi(result, tariff_sim=_SIM)
    topics = [c.args[0] for c in pub._client.publish.call_args_list]
    assert sum('/tariffsim_' in t for t in topics) == 23


# ── bieżąca taryfa (current_tariff) ──────────────────────────────────────────

def test_current_tariff_sensor_is_plain_text():
    s = _sensor('current_tariff')
    assert s.name == 'PV Current Tariff'
    assert s.unit is None and s.device_class is None and s.state_class is None


def test_render_current_tariff(result):
    assert _render_value(_sensor('current_tariff'), result, current_tariff='G13') == 'G13'
    assert _render_value(_sensor('current_tariff'), result) == 'unknown'


def test_publish_roi_publishes_current_tariff(result):
    from unittest.mock import MagicMock
    from pv_roi_tracker.publisher import MQTTPublisher
    pub = MQTTPublisher('localhost', 1883, '', '')
    pub._client = MagicMock()
    pub._connected = True
    pub.publish_roi(result, current_tariff='G12w')
    published = {c.args[0]: c.args[1] for c in pub._client.publish.call_args_list}
    topic = next(t for t in published if t.endswith('/current_tariff/state'))
    assert published[topic] == 'G12w'
