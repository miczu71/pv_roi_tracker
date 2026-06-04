"""
Live HA data reader — reads current-month values via the HA Supervisor REST API.
The SUPERVISOR_TOKEN env var is injected automatically by HA into every add-on.
"""
from __future__ import annotations

import calendar
import logging
import os
from datetime import date
from statistics import mean
from typing import Optional

import requests

from .models import MonthlyRecord

logger = logging.getLogger(__name__)

_BASE     = 'http://supervisor/core/api/states'
_BASE_API = 'http://supervisor/core/api'
_TOKEN    = os.environ.get('SUPERVISOR_TOKEN', '')
_SYSTEM_KWP           = float(os.environ.get('SYSTEM_KWP', '6.72'))
_TARIFF_PEAK_PRICE    = float(os.environ.get('TARIFF_PEAK_PRICE', '1.23'))
_TARIFF_OFFPEAK_PRICE = float(os.environ.get('TARIFF_OFFPEAK_PRICE', '0.63'))


def _get_state(entity_id: str) -> Optional[float]:
    """Return an HA entity state as float, or None if unavailable/unknown."""
    headers = {'Authorization': f'Bearer {_TOKEN}', 'Content-Type': 'application/json'}
    try:
        resp = requests.get(f'{_BASE}/{entity_id}', headers=headers, timeout=10)
        resp.raise_for_status()
        state = resp.json().get('state', '')
        if state in ('unavailable', 'unknown', ''):
            return None
        return float(state)
    except (requests.RequestException, ValueError) as exc:
        logger.warning('Cannot read %s: %s', entity_id, exc)
        return None


def _get_state_raw(entity_id: str) -> Optional[str]:
    """Return raw HA entity state as string, or None if unavailable/unknown."""
    headers = {'Authorization': f'Bearer {_TOKEN}', 'Content-Type': 'application/json'}
    try:
        resp = requests.get(f'{_BASE}/{entity_id}', headers=headers, timeout=10)
        resp.raise_for_status()
        state = resp.json().get('state', '')
        return None if state in ('unavailable', 'unknown', '') else state
    except (requests.RequestException, ValueError) as exc:
        logger.warning('Cannot read raw %s: %s', entity_id, exc)
        return None


def get_ha_monthly_stats(entity_ids: list, start_month: str = '2024-12-01') -> dict:
    """
    Fetch monthly statistics from HA Recorder for the given entity_ids.
    Returns {entity_id: {YYYY-MM: float}} using 'change' statistic type.
    For state_class:total utility_meters, 'change' = monthly variable cost.
    """
    headers = {'Authorization': f'Bearer {_TOKEN}', 'Content-Type': 'application/json'}
    try:
        resp = requests.post(
            f'{_BASE_API}/recorder/statistics_during_period',
            headers=headers,
            json={
                'start_time': f'{start_month}T00:00:00+00:00',
                'period': 'month',
                'statistic_ids': entity_ids,
                'types': ['change'],
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        result = {eid: {} for eid in entity_ids}
        for eid in entity_ids:
            for entry in data.get(eid, []):
                start_ts = entry.get('start')
                change = entry.get('change')
                if start_ts is None or change is None:
                    continue
                try:
                    # start_ts may be int (epoch) or ISO string
                    if isinstance(start_ts, (int, float)):
                        from datetime import timezone as _tz
                        dt = date.fromtimestamp(start_ts)
                    else:
                        from datetime import datetime as _dt
                        dt = _dt.fromisoformat(str(start_ts).replace('Z', '+00:00'))
                    ym = f'{dt.year}-{dt.month:02d}'
                    result[eid][ym] = round(float(change), 2)
                except Exception:
                    pass
        logger.info('HA monthly stats fetched: %s', {e: len(v) for e, v in result.items()})
        return result
    except Exception as exc:
        logger.warning('get_ha_monthly_stats failed: %s', exc)
        return {eid: {} for eid in entity_ids}


def get_ha_history_7d(entity_ids: list) -> dict:
    """
    Fetch 7-day state history from HA Recorder for the given entity_ids.
    Returns {entity_id: [{t: iso_str, v: float}]} filtered to numeric states.
    """
    from datetime import datetime as _dt, timedelta, timezone as _tz
    headers = {'Authorization': f'Bearer {_TOKEN}', 'Content-Type': 'application/json'}
    start = (_dt.now(_tz.utc) - timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%S+00:00')
    ids_param = ','.join(entity_ids)
    try:
        resp = requests.get(
            f'{_BASE_API}/history/period/{start}',
            headers=headers,
            params={
                'filter_entity_id': ids_param,
                'minimal_response': 'true',
                'no_attributes': 'true',
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()  # list of lists of state dicts
        result = {eid: [] for eid in entity_ids}
        for entity_history in data:
            if not entity_history:
                continue
            eid = entity_history[0].get('entity_id', '')
            if eid not in result:
                continue
            for sd in entity_history:
                state = sd.get('state', '')
                ts = sd.get('last_changed') or sd.get('lu', '')
                try:
                    v = float(state)
                    result[eid].append({'t': ts, 'v': v})
                except (ValueError, TypeError):
                    pass
        return result
    except Exception as exc:
        logger.warning('get_ha_history_7d failed: %s', exc)
        return {eid: [] for eid in entity_ids}


def read_tariff_live() -> dict:
    """Fetch live tariff comparison sensor values from HA."""
    return {
        'dynamic_diff_monthly':  _get_state('sensor.roznica_miesieczna_g12w_vs_dynamiczna'),
        'dynamic_annual_proj':   _get_state('sensor.prognozowana_oszczednosc_roczna_dynamiczna'),
        'dynamic_pct_cheaper':   _get_state('sensor.dynamiczna_tansza_procent_miesiac'),
        'dyn_price_now':         _get_state('sensor.calkowity_koszt_1_kwh_dynamiczna'),
        'g12w_price_now':        _get_state('sensor.power_tauron_g12w_current_price'),
        'dyn_cheaper_now':       _get_state_raw('binary_sensor.dynamiczna_tansza_niz_g12w_teraz'),
        'dyn_spike_now':         _get_state_raw('binary_sensor.dynamiczna_drozsza_niz_g12w_szczyt'),
        'diff_kwh_now':          _get_state('sensor.dynamiczna_vs_g12w_roznica_kwh'),
    }


def _solcast_month_projection(today: date, produced_so_far: float) -> Optional[float]:
    """Estimate full-month production using Solcast 7-day forecast."""
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_remaining_incl_today = days_in_month - today.day + 1

    solcast_days_raw = [
        _get_state('sensor.solcast_pv_forecast_forecast_remaining_today'),
        _get_state('sensor.solcast_pv_forecast_forecast_tomorrow'),
        _get_state('sensor.solcast_pv_forecast_forecast_day_3'),
        _get_state('sensor.solcast_pv_forecast_forecast_day_4'),
        _get_state('sensor.solcast_pv_forecast_forecast_day_5'),
        _get_state('sensor.solcast_pv_forecast_forecast_day_6'),
        _get_state('sensor.solcast_pv_forecast_forecast_day_7'),
    ]
    # Trim to actual remaining days in month and drop None entries
    solcast_vals = [v for v in solcast_days_raw[:days_remaining_incl_today] if v is not None]
    if not solcast_vals:
        return None

    solcast_sum = sum(solcast_vals)
    avg_daily = solcast_sum / len(solcast_vals)
    days_beyond = max(0, days_remaining_incl_today - len(solcast_vals))
    projected_remaining = solcast_sum + avg_daily * days_beyond
    return round(produced_so_far + projected_remaining, 1)


def _savings_per_kwh(historic_records: list[MonthlyRecord], calendar_month: int) -> Optional[float]:
    """Historical mean zł/kWh savings ratio for the given calendar month (or lifetime if no match)."""
    def _msav(r: MonthlyRecord) -> float:
        return (r.self_consumed_savings_pln or 0.0) + (r.feedin_revenue_pln or 0.0) + (r.battery_arbitrage_savings_pln or 0.0)

    same_mo = [r for r in historic_records if r.month == calendar_month and (r.produced_kwh or 0.0) > 0]
    if same_mo:
        return mean(_msav(r) / r.produced_kwh for r in same_mo)  # type: ignore[arg-type]
    all_prod = [r for r in historic_records if (r.produced_kwh or 0.0) > 0]
    if all_prod:
        total_s = sum(_msav(r) for r in all_prod)
        total_p = sum(r.produced_kwh for r in all_prod)  # type: ignore[misc]
        return total_s / total_p if total_p > 0 else None
    return None


def read_current_month(
    rcem_price: Optional[float] = None,
    historic_records: Optional[list] = None,
) -> Optional[MonthlyRecord]:
    """
    Build a MonthlyRecord for the current calendar month from live HA values.
    Returns None if the primary production sensor is unavailable.
    """
    today = date.today()
    year, month = today.year, today.month

    produced  = _get_state('sensor.inverter_yield_monthly')
    exported  = _get_state('sensor.power_meter_exported_energy_monthly')
    consumed  = _get_state('sensor.house_consumption_energy_monthly')
    peak      = _get_state('sensor.monthly_energy_peak')
    offpeak   = _get_state('sensor.monthly_energy_offpeak')
    arbitrage = _get_state('sensor.battery_arbitrage_savings_monthly')

    # Buy price: blend config tariff rates from peak/offpeak split when available;
    # fall back to the HA average-price template sensor.
    if peak is not None and offpeak is not None and (peak + offpeak) > 0:
        buy_price: Optional[float] = round(
            (peak * _TARIFF_PEAK_PRICE + offpeak * _TARIFF_OFFPEAK_PRICE)
            / (peak + offpeak), 4)
    else:
        buy_price = _get_state('sensor.srednia_cena_energii_w_miesiacu')

    if produced is None:
        logger.warning('sensor.inverter_yield_monthly unavailable — skipping current-month record')
        return None

    purchased = (peak or 0.0) + (offpeak or 0.0) if (peak is not None or offpeak is not None) else None

    self_consumed_kwh         = max(0.0, produced - (exported or 0.0)) if exported is not None else None
    self_consumed_savings_pln = round(self_consumed_kwh * buy_price, 2) if (self_consumed_kwh is not None and buy_price is not None) else None
    purchase_cost_pln         = round(purchased * buy_price, 2)         if (purchased      is not None and buy_price is not None) else None
    feedin_revenue_pln        = round((exported or 0.0) * rcem_price, 2) if (exported is not None and rcem_price is not None) else None
    specific_yield            = round(produced / _SYSTEM_KWP, 1)         if _SYSTEM_KWP else None
    projected_month_kwh       = _solcast_month_projection(today, produced)

    projected_month_savings_pln = None
    if projected_month_kwh is not None and historic_records:
        spk = _savings_per_kwh(historic_records, month)
        if spk is not None:
            projected_month_savings_pln = round(projected_month_kwh * spk, 2)

    return MonthlyRecord(
        year=year, month=month,
        produced_kwh=produced,
        consumed_kwh=consumed,
        purchased_kwh=purchased,
        purchased_kwh_peak=peak,
        purchased_kwh_offpeak=offpeak,
        exported_kwh=exported,
        self_consumed_kwh=self_consumed_kwh,
        buy_price_pln_kwh=buy_price,
        feedin_price_pln_kwh=rcem_price,
        self_consumed_savings_pln=self_consumed_savings_pln,
        purchase_cost_pln=purchase_cost_pln,
        feedin_revenue_pln=feedin_revenue_pln,
        specific_yield=specific_yield,
        battery_arbitrage_savings_pln=round(arbitrage, 2) if arbitrage is not None else None,
        rcem_status='confirmed' if rcem_price is not None else 'pending',
        projected_month_kwh=projected_month_kwh,
        projected_month_savings_pln=projected_month_savings_pln,
    )
