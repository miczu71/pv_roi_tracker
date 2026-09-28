"""Testy heatpump.py — koszt pompy ciepła got./ekon. atrybuowany do PV/baterii/sieci."""
from datetime import date

import pytest

from pv_roi_tracker.heatpump import (
    BatteryPool,
    attribute_heatpump,
    aggregate_months,
    aggregate_seasons,
    compute,
    grid_price_for_hour,
    hourly_flows,
    mode_split,
    run_hours,
    season_cutoff_date,
    season_key,
    season_label,
    season_start_year,
)


# ── hourly_flows ────────────────────────────────────────────────────────────────

def test_hourly_flows_pv_covers_load_no_battery():
    flows = hourly_flows({'produced': 5.0, 'exported': 1.0, 'imported': 0.5})
    # pv_net=4.0, no battery -> pv_to_load=4.0, grid_to_load=0.5
    assert flows['pv_to_load'] == pytest.approx(4.0)
    assert flows['grid_to_load'] == pytest.approx(0.5)
    assert flows['load'] == pytest.approx(4.5)
    assert flows['charge_from_pv'] == 0
    assert flows['charge_from_grid'] == 0


def test_hourly_flows_battery_charged_from_pv_first():
    # pv_net=6, battery charges 2 -> all from PV surplus, none from grid
    flows = hourly_flows({'produced': 8.0, 'exported': 2.0, 'imported': 0.0,
                          'batt_charge': 2.0})
    assert flows['charge_from_pv'] == pytest.approx(2.0)
    assert flows['charge_from_grid'] == pytest.approx(0.0)
    assert flows['pv_to_load'] == pytest.approx(4.0)  # 6 pv_net - 2 charged


def test_hourly_flows_battery_charge_exceeds_pv_surplus():
    # pv_net=1, battery charges 3 -> 1 from PV, 2 from grid
    flows = hourly_flows({'produced': 3.0, 'exported': 2.0, 'batt_charge': 3.0, 'imported': 2.0})
    assert flows['charge_from_pv'] == pytest.approx(1.0)
    assert flows['charge_from_grid'] == pytest.approx(2.0)
    assert flows['grid_to_load'] == pytest.approx(0.0)  # 2 imported - 2 charged from grid


def test_hourly_flows_discharge_covers_load():
    flows = hourly_flows({'imported': 0.0, 'batt_discharge': 1.5})
    assert flows['batt_to_load'] == pytest.approx(1.5)
    assert flows['load'] == pytest.approx(1.5)


# ── attribute_heatpump ──────────────────────────────────────────────────────────

def test_attribute_heatpump_proportional_split():
    flows = {'load': 10.0, 'pv_to_load': 6.0, 'batt_to_load': 2.0, 'grid_to_load': 2.0}
    attrib = attribute_heatpump(flows, hp_kwh=5.0)  # share = 0.5
    assert attrib['hp_pv'] == pytest.approx(3.0)
    assert attrib['hp_batt'] == pytest.approx(1.0)
    assert attrib['hp_grid'] == pytest.approx(1.0)
    assert attrib['anomaly'] is False
    assert attrib['hp_pv'] + attrib['hp_batt'] + attrib['hp_grid'] == pytest.approx(5.0)


def test_attribute_heatpump_takes_full_load():
    flows = {'load': 4.0, 'pv_to_load': 4.0, 'batt_to_load': 0.0, 'grid_to_load': 0.0}
    attrib = attribute_heatpump(flows, hp_kwh=4.0)
    assert attrib['hp_pv'] == pytest.approx(4.0)


def test_attribute_heatpump_overflow_goes_to_grid_and_flags_anomaly():
    # hp_kwh > load (meters on different measurement paths)
    flows = {'load': 2.0, 'pv_to_load': 1.0, 'batt_to_load': 0.0, 'grid_to_load': 1.0}
    attrib = attribute_heatpump(flows, hp_kwh=3.0)
    assert attrib['hp_pv'] + attrib['hp_batt'] + attrib['hp_grid'] == pytest.approx(3.0)
    assert attrib['hp_grid'] == pytest.approx(2.0)  # 1.0 attributed + 1.0 overflow
    assert attrib['anomaly'] is True


def test_attribute_heatpump_zero_load_zero_hp():
    attrib = attribute_heatpump({'load': 0.0, 'pv_to_load': 0.0, 'batt_to_load': 0.0,
                                 'grid_to_load': 0.0}, hp_kwh=0.0)
    assert attrib == {'hp_pv': 0.0, 'hp_batt': 0.0, 'hp_grid': 0.0, 'anomaly': False}


def test_attribute_heatpump_zero_load_nonzero_hp_is_pure_anomaly():
    attrib = attribute_heatpump({'load': 0.0, 'pv_to_load': 0.0, 'batt_to_load': 0.0,
                                 'grid_to_load': 0.0}, hp_kwh=2.0)
    assert attrib['hp_grid'] == pytest.approx(2.0)
    assert attrib['anomaly'] is True


# ── BatteryPool ─────────────────────────────────────────────────────────────────

def test_battery_pool_pv_only_charge_is_free_cash():
    pool = BatteryPool()
    pool.charge(from_pv=2.0, from_grid=0.0, grid_price=1.2, pv_price=0.30, efficiency=1.0)
    assert pool.unit_cash == pytest.approx(0.0)
    assert pool.unit_econ == pytest.approx(0.30)  # opportunity cost of lost RCEm sale
    assert pool.energy_kwh == pytest.approx(2.0)


def test_battery_pool_grid_only_charge_costs_grid_price_both_ways():
    pool = BatteryPool()
    pool.charge(from_pv=0.0, from_grid=2.0, grid_price=0.63, pv_price=0.30, efficiency=1.0)
    assert pool.unit_cash == pytest.approx(0.63)
    assert pool.unit_econ == pytest.approx(0.63)


def test_battery_pool_efficiency_loss_raises_unit_price():
    pool = BatteryPool()
    pool.charge(from_pv=0.0, from_grid=1.0, grid_price=1.0, pv_price=0.0, efficiency=0.5)
    # paid 1.0 zł for 1 kWh in, only 0.5 kWh usable stored -> 2.0 zł/kWh
    assert pool.energy_kwh == pytest.approx(0.5)
    assert pool.unit_cash == pytest.approx(2.0)


def test_battery_pool_weighted_average_across_two_charges():
    pool = BatteryPool()
    pool.charge(from_pv=0.0, from_grid=1.0, grid_price=1.0, pv_price=0.0, efficiency=1.0)
    pool.charge(from_pv=1.0, from_grid=0.0, grid_price=1.0, pv_price=0.0, efficiency=1.0)
    # 1 kWh @1.0 zł + 1 kWh @0.0 zł -> avg 0.5 zł/kWh
    assert pool.unit_cash == pytest.approx(0.5)


def test_battery_pool_discharge_does_not_change_unit_price():
    pool = BatteryPool()
    pool.charge(from_pv=0.0, from_grid=2.0, grid_price=0.8, pv_price=0.0, efficiency=1.0)
    taken = pool.drain(1.0)
    assert taken == pytest.approx(1.0)
    assert pool.unit_cash == pytest.approx(0.8)
    assert pool.energy_kwh == pytest.approx(1.0)


def test_battery_pool_drain_caps_at_available_energy():
    pool = BatteryPool()
    pool.charge(from_pv=1.0, from_grid=0.0, grid_price=1.0, pv_price=0.3, efficiency=1.0)
    taken = pool.drain(5.0)
    assert taken == pytest.approx(1.0)
    assert pool.energy_kwh == pytest.approx(0.0)


# ── grid_price_for_hour ──────────────────────────────────────────────────────────

def test_grid_price_g11_flat_ignores_hour():
    rates = (1.0, None)
    assert grid_price_for_hour(date(2026, 1, 5), 7, rates) == 1.0
    assert grid_price_for_hour(date(2026, 1, 5), 23, rates) == 1.0


def test_grid_price_g12w_weekday_peak_and_offpeak():
    rates = (1.2, 0.6)
    mon = date(2026, 6, 1)  # poniedziałek
    assert grid_price_for_hour(mon, 7, rates) == 1.2   # szczyt 06-13
    assert grid_price_for_hour(mon, 14, rates) == 0.6  # dolina 13-15
    assert grid_price_for_hour(mon, 20, rates) == 1.2  # szczyt 15-22
    assert grid_price_for_hour(mon, 23, rates) == 0.6  # dolina


def test_grid_price_g12w_weekend_always_offpeak():
    rates = (1.2, 0.6)
    sat = date(2026, 6, 6)
    assert grid_price_for_hour(sat, 8, rates) == 0.6


def test_grid_price_none_rates_returns_none():
    assert grid_price_for_hour(date(2026, 1, 1), 8, None) is None


# ── mode_split ────────────────────────────────────────────────────────────────

def test_mode_split_proportional_to_hours():
    result = mode_split(hp_kwh=3.0, heat_h=0.75, dhw_h=0.25)
    assert result['heating'] == pytest.approx(2.25)
    assert result['dhw'] == pytest.approx(0.75)
    assert result['other'] == 0.0


def test_mode_split_no_mode_hours_is_other():
    result = mode_split(hp_kwh=1.5, heat_h=0.0, dhw_h=0.0)
    assert result == {'heating': 0.0, 'dhw': 0.0, 'other': 1.5}


def test_mode_split_zero_kwh():
    assert mode_split(0.0, 0.5, 0.5) == {'heating': 0.0, 'dhw': 0.0, 'other': 0.0}


def test_mode_split_clips_noisy_hours_above_one():
    # sensor delta noise could exceed 1h in a clock hour
    result = mode_split(hp_kwh=2.0, heat_h=1.5, dhw_h=0.0)
    assert result['heating'] == pytest.approx(2.0)  # clipped to 1.0, still all of hp_kwh (only mode)


# ── season helpers ────────────────────────────────────────────────────────────

def test_season_start_year_september_starts_new_season():
    assert season_start_year(date(2026, 9, 1)) == 2026
    assert season_start_year(date(2026, 8, 31)) == 2025
    assert season_start_year(date(2026, 1, 15)) == 2025


def test_season_key_and_label():
    assert season_key(2025) == '2025-2026'
    assert season_label(2025) == '2025/26'


def test_season_cutoff_date_matches_month_day_in_season():
    # today = 28 września -> falls in the Sep-Dec half of the season
    assert season_cutoff_date(2024, date(2026, 9, 28)) == date(2024, 9, 28)
    # today = 15 lutego -> falls in the Jan-Aug half (next calendar year)
    assert season_cutoff_date(2024, date(2026, 2, 15)) == date(2025, 2, 15)


# ── integration: run_hours / aggregate ───────────────────────────────────────

def _hours_two_days():
    """1 dzień w lipcu (lato, tylko PV, brak grzania) + 1 dzień w styczniu
    (zima, tylko sieć w dolinie, grzanie)."""
    hours = {}
    # 2023-07-10: południe, nadwyżka PV, pompa CWU z PV
    hours['2023-07-10T12'] = {'produced': 5.0, 'exported': 3.0, 'imported': 0.0,
                              'batt_charge': 0.0, 'batt_discharge': 0.0,
                              'hp_kwh': 1.0, 'heat_h': 0.0, 'dhw_h': 1.0, 't_out': 25.0}
    # 2024-01-10: noc, brak PV, pompa grzeje z sieci (dolina)
    hours['2024-01-10T02'] = {'produced': 0.0, 'exported': 0.0, 'imported': 2.0,
                              'batt_charge': 0.0, 'batt_discharge': 0.0,
                              'hp_kwh': 2.0, 'heat_h': 1.0, 'dhw_h': 0.0, 't_out': -3.0}
    return hours


def test_run_hours_source_balance_equals_hp_kwh():
    hours = _hours_two_days()
    rates = {'2023-07': (1.2, 0.6), '2024-01': (1.2, 0.6)}
    rcem = {'2023-07': 0.35, '2024-01': 0.35}
    rows = run_hours(hours, rates, rcem, battery_efficiency=0.92)
    for r in rows:
        assert r['hp_pv'] + r['hp_batt'] + r['hp_grid'] == pytest.approx(r['hp_kwh'], abs=1e-9)


def test_run_hours_summer_hour_is_pv_and_dhw():
    hours = _hours_two_days()
    rates = {'2023-07': (1.2, 0.6)}
    rcem = {'2023-07': 0.35}
    rows = run_hours({'2023-07-10T12': hours['2023-07-10T12']}, rates, rcem)
    r = rows[0]
    assert r['hp_pv'] == pytest.approx(1.0)
    assert r['dhw'] == pytest.approx(1.0)
    assert r['heating'] == 0.0
    assert r['cash_pln'] == pytest.approx(0.0)  # PV covers it, cash cost = 0


def test_run_hours_winter_night_hour_is_grid_offpeak_and_heating():
    hours = _hours_two_days()
    rates = {'2024-01': (1.2, 0.6)}
    rcem = {'2024-01': 0.35}
    rows = run_hours({'2024-01-10T02': hours['2024-01-10T02']}, rates, rcem)
    r = rows[0]
    assert r['hp_grid'] == pytest.approx(2.0)
    assert r['hp_grid_offpeak'] == pytest.approx(2.0)
    assert r['hp_grid_peak'] == 0.0
    assert r['heating'] == pytest.approx(2.0)
    assert r['cash_pln'] == pytest.approx(2.0 * 0.6)
    assert r['econ_pln'] == pytest.approx(2.0 * 0.6)  # all from grid, cash==econ


def test_aggregate_months_groups_by_ym_and_computes_hdd():
    hours = _hours_two_days()
    rates = {'2023-07': (1.2, 0.6), '2024-01': (1.2, 0.6)}
    rcem = {'2023-07': 0.35, '2024-01': 0.35}
    rows = run_hours(hours, rates, rcem)
    months = aggregate_months(rows, hdd_base=15.0)
    by_ym = {m['ym']: m for m in months}
    assert set(by_ym) == {'2023-07', '2024-01'}
    # July hour at 25°C > base -> 0 HDD contribution
    assert by_ym['2023-07']['hdd'] == 0.0
    # January hour at -3°C -> (15-(-3))/24 = 0.75, rounded to 1 decimal (0.8)
    assert by_ym['2024-01']['hdd'] == pytest.approx(0.8)
    assert by_ym['2023-07']['pv_battery_coverage_pct'] == pytest.approx(100.0)
    assert by_ym['2024-01']['pv_battery_coverage_pct'] == pytest.approx(0.0)


def test_aggregate_seasons_assigns_july_and_january_to_different_seasons():
    hours = _hours_two_days()
    rates = {'2023-07': (1.2, 0.6), '2024-01': (1.2, 0.6)}
    rcem = {'2023-07': 0.35, '2024-01': 0.35}
    rows = run_hours(hours, rates, rcem)
    seasons = aggregate_seasons(rows, today=date(2026, 9, 28))
    by_key = {s['season']: s for s in seasons}
    assert '2022-2023' in by_key  # lipiec 2023 -> sezon 2022/23
    assert '2023-2024' in by_key  # styczeń 2024 -> sezon 2023/24


def test_aggregate_seasons_to_date_respects_cutoff():
    hours = _hours_two_days()
    rates = {'2023-07': (1.2, 0.6), '2024-01': (1.2, 0.6)}
    rcem = {'2023-07': 0.35, '2024-01': 0.35}
    rows = run_hours(hours, rates, rcem)
    # 'today' = 15 sierpnia -> cutoff dla każdego sezonu = 15.08 w części
    # styczeń..sierpień, więc oba przykładowe godziny (lipiec, styczeń)
    # mieszczą się przed odcięciem swoich sezonów.
    seasons = aggregate_seasons(rows, today=date(2024, 8, 15))
    by_key = {s['season']: s for s in seasons}
    assert by_key['2022-2023']['to_date']['kwh_total'] == pytest.approx(1.0)
    assert by_key['2023-2024']['to_date']['kwh_total'] == pytest.approx(2.0)


def test_aggregate_seasons_to_date_excludes_hours_after_cutoff():
    hours = _hours_two_days()
    rates = {'2023-07': (1.2, 0.6), '2024-01': (1.2, 0.6)}
    rcem = {'2023-07': 0.35, '2024-01': 0.35}
    rows = run_hours(hours, rates, rcem)
    # 'today' = 28 września -> cutoff wczesny w sezonie (dzień 28), godziny
    # z lipca/stycznia (dużo dalej w sezonie) jeszcze nie nadeszły.
    seasons = aggregate_seasons(rows, today=date(2026, 9, 28))
    by_key = {s['season']: s for s in seasons}
    assert by_key['2022-2023']['to_date']['kwh_total'] == pytest.approx(0.0)
    assert by_key['2023-2024']['to_date']['kwh_total'] == pytest.approx(0.0)


def test_compute_returns_none_when_no_hours():
    assert compute({}, {}, {}) is None


def test_compute_full_payload_shape():
    hours = _hours_two_days()
    rates = {'2023-07': (1.2, 0.6), '2024-01': (1.2, 0.6)}
    rcem = {'2023-07': 0.35, '2024-01': 0.35}
    payload = compute(hours, rates, rcem, today=date(2026, 9, 28))
    assert payload['hours_total'] == 2
    assert payload['anomaly_hours_total'] == 0
    assert len(payload['months']) == 2
    assert payload['current_season_to_date'] is None or isinstance(payload['current_season_to_date'], dict)


def test_compute_flags_anomaly_hours_total():
    hours = {'2024-01-10T02': {'produced': 0.0, 'exported': 0.0, 'imported': 0.0,
                               'batt_charge': 0.0, 'batt_discharge': 0.0,
                               'hp_kwh': 1.0, 'heat_h': 1.0, 'dhw_h': 0.0, 't_out': -3.0}}
    payload = compute(hours, {'2024-01': (1.2, 0.6)}, {'2024-01': 0.35}, today=date(2024, 1, 15))
    assert payload['anomaly_hours_total'] == 1
