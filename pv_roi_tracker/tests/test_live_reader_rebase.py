"""
Tests for the v0.35.0 kWh-rebase in live_reader.py (revised design):

  - get_energy_dashboard_sources() parses HA's energy/get_prefs WS reply into
    role-lists, with per-role fallback if a role has nothing configured or
    the whole call fails.
  - _build_record always derives self_consumed_kwh/consumed_kwh (no more
    "measured self-use" concept — this installation has no independent
    whole-house meter, see balance.py) and ratio-splits an authoritative
    import total across zones.
  - read_month_from_statistics resolves entities dynamically instead of
    reading hardcoded names, summing multi-entity roles.
  - read_current_month corrects produced/exported/imported against the
    dashboard-resolved lifetime meters (with graceful fallback to the old
    live REST reads if that extra call fails).
  - a negative LTS 'change' is rejected, not silently coerced.

See docs/pv_roi_energy_rebase (plan) and test_timezone_fix.py (older
_build_record / read_current_month coverage, preserved with the `consumed`
parameter removed to match the new signature).
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

import pv_roi_tracker.live_reader as lr


# ── get_energy_dashboard_sources ─────────────────────────────────────────────

def _fake_ws_reply(energy_sources):
    """Build the two messages _ws_connect_authed + energy/get_prefs expect,
    consumed via a fake connection object."""
    class _FakeWS:
        def __init__(self):
            self._sent = []
            self.closed = False

        def send(self, payload):
            self._sent.append(payload)

        def recv(self):
            import json
            return json.dumps({'success': True, 'result': {'energy_sources': energy_sources}})

        def close(self):
            self.closed = True

    return _FakeWS()


def test_get_energy_dashboard_sources_parses_real_config_shape():
    """Reproduces this installation's actual ha_manage_energy_prefs shape:
    one solar entry, one battery entry, three grid entries (one dead 'from'
    with a live 'to', two live 'from'-only peak/offpeak entries)."""
    energy_sources = [
        {'type': 'solar', 'stat_energy_from': 'sensor.energy_pv'},
        {'type': 'battery', 'stat_energy_from': 'sensor.battery_total_discharge',
         'stat_energy_to': 'sensor.battery_total_charge'},
        {'type': 'grid', 'stat_energy_from': 'sensor.energy_history_archive',
         'stat_energy_to': 'sensor.power_meter_exported'},
        {'type': 'grid', 'stat_energy_from': 'sensor.daily_energy_peak', 'stat_energy_to': None},
        {'type': 'grid', 'stat_energy_from': 'sensor.daily_energy_offpeak', 'stat_energy_to': None},
    ]
    lr._energy_prefs_cache = None
    with patch.object(lr, '_ws_connect_authed', return_value=_fake_ws_reply(energy_sources)):
        result = lr.get_energy_dashboard_sources()
    assert result['solar'] == ['sensor.energy_pv']
    assert result['grid_export'] == ['sensor.power_meter_exported']
    assert set(result['grid_import']) == {
        'sensor.energy_history_archive', 'sensor.daily_energy_peak', 'sensor.daily_energy_offpeak'}
    assert result['battery_charge'] == ['sensor.battery_total_charge']
    assert result['battery_discharge'] == ['sensor.battery_total_discharge']
    lr._energy_prefs_cache = None  # don't leak into other tests


def test_get_energy_dashboard_sources_falls_back_on_ws_failure():
    lr._energy_prefs_cache = None
    with patch.object(lr, '_ws_connect_authed', side_effect=RuntimeError('no route to host')):
        result = lr.get_energy_dashboard_sources()
    assert result == lr._FALLBACK_ENERGY_SOURCES
    lr._energy_prefs_cache = None


def test_get_energy_dashboard_sources_falls_back_per_role_when_unconfigured():
    """Only solar configured — grid/battery roles fall back individually,
    not the whole result."""
    energy_sources = [{'type': 'solar', 'stat_energy_from': 'sensor.energy_pv'}]
    lr._energy_prefs_cache = None
    with patch.object(lr, '_ws_connect_authed', return_value=_fake_ws_reply(energy_sources)):
        result = lr.get_energy_dashboard_sources()
    assert result['solar'] == ['sensor.energy_pv']
    assert result['grid_import'] == lr._FALLBACK_ENERGY_SOURCES['grid_import']
    assert result['battery_charge'] == lr._FALLBACK_ENERGY_SOURCES['battery_charge']
    lr._energy_prefs_cache = None


def test_get_energy_dashboard_sources_caches_result():
    energy_sources = [{'type': 'solar', 'stat_energy_from': 'sensor.energy_pv'}]
    lr._energy_prefs_cache = None
    with patch.object(lr, '_ws_connect_authed', return_value=_fake_ws_reply(energy_sources)) as mock_ws:
        lr.get_energy_dashboard_sources()
        lr.get_energy_dashboard_sources()  # second call must not reconnect
    assert mock_ws.call_count == 1
    lr._energy_prefs_cache = None


def test_get_energy_dashboard_sources_force_refresh_bypasses_cache():
    energy_sources = [{'type': 'solar', 'stat_energy_from': 'sensor.energy_pv'}]
    lr._energy_prefs_cache = None
    with patch.object(lr, '_ws_connect_authed', return_value=_fake_ws_reply(energy_sources)) as mock_ws:
        lr.get_energy_dashboard_sources()
        lr.get_energy_dashboard_sources(force_refresh=True)
    assert mock_ws.call_count == 2
    lr._energy_prefs_cache = None


# ── _sum_role_month ───────────────────────────────────────────────────────────

def test_sum_role_month_sums_available_entities():
    vals = {'sensor.a': 10.0, 'sensor.b': 20.0}
    assert lr._sum_role_month(vals, ['sensor.a', 'sensor.b']) == pytest.approx(30.0)


def test_sum_role_month_partial_read_still_sums():
    """One entity missing (None) — sum what's available, don't fail the whole
    role, matching how the Energy Dashboard itself behaves."""
    vals = {'sensor.a': 10.0, 'sensor.b': None}
    assert lr._sum_role_month(vals, ['sensor.a', 'sensor.b']) == pytest.approx(10.0)


def test_sum_role_month_none_when_all_missing():
    vals = {'sensor.a': None}
    assert lr._sum_role_month(vals, ['sensor.a']) is None


def test_sum_role_month_none_for_empty_entity_list():
    assert lr._sum_role_month({}, []) is None


# ── _build_record: always-derived self_consumed/consumed ────────────────────

def test_build_record_derives_self_consumed_and_consumed():
    with patch.object(lr, '_get_state', side_effect=lambda eid: None):
        rec = lr._build_record(
            year=2026, month=7, produced=867.11, exported=431.56,
            peak=4.53, offpeak=79.79, arb_kwh=None, rcem_price=0.35,
            imported=84.65,
        )
    assert rec.self_consumed_kwh == pytest.approx(867.11 - 431.56)
    assert rec.self_consumed_source == 'derived'
    assert rec.consumed_kwh == pytest.approx((867.11 - 431.56) + 84.65)


def test_build_record_consumed_none_without_imported():
    """No independent consumed sensor exists anymore — without `imported`,
    consumed_kwh must be None, not silently wrong."""
    with patch.object(lr, '_get_state', side_effect=lambda eid: None):
        rec = lr._build_record(
            year=2026, month=7, produced=867.11, exported=431.56,
            peak=4.53, offpeak=79.79, arb_kwh=None, rcem_price=0.35,
        )
    assert rec.consumed_kwh is None


def test_build_record_stores_cross_family_produced():
    with patch.object(lr, '_get_state', side_effect=lambda eid: None):
        rec = lr._build_record(
            year=2026, month=7, produced=643.98, exported=307.0,
            peak=4.53, offpeak=79.79, arb_kwh=None, rcem_price=0.35,
            cross_family_produced_kwh=685.53,
        )
    assert rec.cross_family_produced_kwh == pytest.approx(685.53)
    assert rec.balance_residual_kwh == pytest.approx(abs(643.98 - 685.53), abs=0.01)


# ── _build_record: authoritative import ratio-split (unchanged from before) ─

def test_build_record_ratio_splits_authoritative_import():
    with patch.object(lr, '_get_state', side_effect=lambda eid: None):
        rec = lr._build_record(
            year=2026, month=7, produced=867.11, exported=431.56,
            peak=4.53, offpeak=79.79, arb_kwh=None, rcem_price=0.35,
            imported=84.65,
        )
    assert rec.purchased_kwh == pytest.approx(84.65)
    assert rec.purchased_kwh_peak + rec.purchased_kwh_offpeak == pytest.approx(84.65, abs=0.01)


def test_build_record_without_imported_keeps_old_half_sum_behavior():
    with patch.object(lr, '_get_state', side_effect=lambda eid: None):
        rec = lr._build_record(
            year=2026, month=7, produced=867.11, exported=431.56,
            peak=4.53, offpeak=None, arb_kwh=None, rcem_price=0.35,
        )
    assert rec.purchased_kwh == pytest.approx(4.53)
    assert rec.purchased_kwh_offpeak is None


# ── _fetch_lifetime_month_stats: negative change rejected, zero preserved ───

def test_fetch_lifetime_month_stats_rejects_negative_change():
    with patch.object(lr, 'get_ha_monthly_stats',
                      return_value={'sensor.x': {'2026-07': -148.06}}):
        result = lr._fetch_lifetime_month_stats(2026, 7, ['sensor.x'])
    assert result['sensor.x'] is None


def test_fetch_lifetime_month_stats_preserves_genuine_zero():
    with patch.object(lr, 'get_ha_monthly_stats',
                      return_value={'sensor.x': {'2026-07': 0.0}}):
        result = lr._fetch_lifetime_month_stats(2026, 7, ['sensor.x'])
    assert result['sensor.x'] == 0.0


# ── read_month_from_statistics: dynamic entity resolution ───────────────────

def _fixed_sources():
    return {
        'solar': ['sensor.energy_pv'],
        'grid_export': ['sensor.power_meter_exported'],
        'grid_import': ['sensor.daily_energy_peak', 'sensor.daily_energy_offpeak'],
        'battery_charge': ['sensor.battery_total_charge'],
        'battery_discharge': ['sensor.battery_total_discharge'],
    }


def test_read_month_from_statistics_uses_dashboard_resolved_entities():
    """Reproduces the confirmed measurement: dashboard-family produced
    (energy_pv, 643.98) differs from template-family (inverter_total_yield,
    685.53) for March 2026 — both must be captured on the record."""
    lts_values = {
        'sensor.energy_pv':                  {'2026-03': 643.98},
        'sensor.power_meter_exported':        {'2026-03': 307.0},
        'sensor.daily_energy_peak':           {'2026-03': 20.0},
        'sensor.daily_energy_offpeak':        {'2026-03': 15.0},
        'sensor.battery_total_charge':        {'2026-03': 100.0},
        'sensor.battery_total_discharge':     {'2026-03': 95.0},
        lr._ZONE_PEAK_METER:                  {'2026-03': 20.0},
        lr._ZONE_OFFPEAK_METER:               {'2026-03': 15.0},
        lr._TEMPLATE_PRODUCED_METER:          {'2026-03': 685.53},
    }
    with (
        patch.object(lr, 'get_energy_dashboard_sources', return_value=_fixed_sources()),
        patch.object(lr, 'get_ha_monthly_stats',
                    side_effect=lambda entity_ids, start_month=None:
                        {eid: lts_values.get(eid, {}) for eid in entity_ids}),
    ):
        rec = lr.read_month_from_statistics(2026, 3, rcem_price=0.35)
    assert rec is not None
    assert rec.produced_kwh == pytest.approx(643.98)
    assert rec.exported_kwh == pytest.approx(307.0)
    assert rec.purchased_kwh == pytest.approx(35.0)  # 20+15
    assert rec.battery_charge_kwh == pytest.approx(100.0)
    assert rec.battery_discharge_kwh == pytest.approx(95.0)
    assert rec.cross_family_produced_kwh == pytest.approx(685.53)
    assert rec.source == 'lts'
    # Reproduces the confirmed ~6.45% cross-family divergence for March 2026.
    assert rec.balance_residual_kwh == pytest.approx(41.55, abs=0.01)


def test_read_month_from_statistics_none_when_solar_missing():
    with (
        patch.object(lr, 'get_energy_dashboard_sources', return_value=_fixed_sources()),
        patch.object(lr, 'get_ha_monthly_stats',
                    side_effect=lambda entity_ids, start_month=None: {eid: {} for eid in entity_ids}),
    ):
        rec = lr.read_month_from_statistics(2026, 3, rcem_price=0.35)
    assert rec is None


# ── read_current_month: dashboard correction with graceful fallback ────────

def test_read_current_month_corrects_produced_exported_from_dashboard_sources():
    states = {
        'sensor.inverter_yield_monthly': 200.0,               # provisional, will be overridden
        'sensor.power_meter_exported_energy_monthly': 80.0,   # provisional, will be overridden
        'sensor.monthly_energy_peak': 999.0,                  # provisional, will be overridden
        'sensor.monthly_energy_offpeak': 999.0,               # provisional, will be overridden
        'sensor.battery_grid_charge_off_peak_monthly': 5.0,
    }
    lts_values = {
        'sensor.energy_pv': 210.0,
        'sensor.power_meter_exported': 82.0,
        'sensor.daily_energy_peak': 20.0,
        'sensor.daily_energy_offpeak': 15.0,
        'sensor.battery_total_charge': 30.0,
        'sensor.battery_total_discharge': 28.0,
        lr._TEMPLATE_PRODUCED_METER: 225.0,
    }
    with (
        patch.object(lr, '_get_state', side_effect=lambda eid: states.get(eid, None)),
        patch.object(lr, '_solcast_month_projection', return_value=None),
        patch.object(lr, 'get_energy_dashboard_sources', return_value=_fixed_sources()),
        patch.object(lr, '_fetch_lifetime_month_stats', return_value=lts_values),
        patch('pv_roi_tracker.live_reader.date') as mock_date,
    ):
        mock_date.today.return_value = date(2026, 7, 15)
        result = lr.read_current_month(rcem_price=None)
    assert result is not None
    assert result.produced_kwh == pytest.approx(210.0)   # dashboard-corrected, not the provisional 200.0
    assert result.exported_kwh == pytest.approx(82.0)     # dashboard-corrected, not the provisional 80.0
    # Regression: peak/offpeak must come from the LTS (month-integrated)
    # values, not a raw REST read of the daily-cycle zone sensors — those
    # reset every day and would silently understate the month-to-date split.
    assert result.purchased_kwh_peak == pytest.approx(20.0)
    assert result.purchased_kwh_offpeak == pytest.approx(15.0)
    assert result.cross_family_produced_kwh == pytest.approx(225.0)
    assert result.source == 'live'


def test_read_current_month_falls_back_when_dashboard_correction_unavailable():
    """If the supplementary LTS call fails (empty dict, e.g. WS/recorder
    hiccup), the old provisional REST-read produced/exported/peak/offpeak
    must still be used — the poll must not fail just because the correction
    couldn't run. The REST fallback for peak/offpeak reads the old monthly-
    cycle sensors (which carry a real month-to-date total), not the
    daily-cycle ones used for the LTS path."""
    states = {
        'sensor.inverter_yield_monthly': 200.0,
        'sensor.power_meter_exported_energy_monthly': 80.0,
        'sensor.monthly_energy_peak': 120.0,
        'sensor.monthly_energy_offpeak': 100.0,
        'sensor.battery_grid_charge_off_peak_monthly': 5.0,
    }
    with (
        patch.object(lr, '_get_state', side_effect=lambda eid: states.get(eid, None)),
        patch.object(lr, '_solcast_month_projection', return_value=None),
        patch.object(lr, 'get_energy_dashboard_sources', return_value=_fixed_sources()),
        patch.object(lr, '_fetch_lifetime_month_stats', return_value={}),
        patch('pv_roi_tracker.live_reader.date') as mock_date,
    ):
        mock_date.today.return_value = date(2026, 7, 15)
        result = lr.read_current_month(rcem_price=None)
    assert result is not None
    assert result.produced_kwh == pytest.approx(200.0)   # old provisional value, unchanged
    assert result.exported_kwh == pytest.approx(80.0)    # old provisional value, unchanged
    assert result.purchased_kwh_peak == pytest.approx(120.0)
    assert result.purchased_kwh_offpeak == pytest.approx(100.0)
    assert result.cross_family_produced_kwh is None
