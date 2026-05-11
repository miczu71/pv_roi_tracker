"""
Live HA data reader — reads current-month values via the HA Supervisor REST API.
The SUPERVISOR_TOKEN env var is injected automatically by HA into every add-on.
"""
from __future__ import annotations

import calendar
import logging
import os
from datetime import date
from typing import Optional

import requests

from .models import MonthlyRecord

logger = logging.getLogger(__name__)

_BASE = 'http://supervisor/core/api/states'
_TOKEN = os.environ.get('SUPERVISOR_TOKEN', '')
_SYSTEM_KWP = float(os.environ.get('SYSTEM_KWP', '6.72'))


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


def read_current_month(rcem_price: Optional[float] = None) -> Optional[MonthlyRecord]:
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
    buy_price = _get_state('sensor.srednia_cena_energii_w_miesiacu')
    arbitrage = _get_state('sensor.battery_arbitrage_savings_monthly')

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
    )
