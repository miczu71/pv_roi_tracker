"""
Live HA data reader — reads current-month values via the HA Supervisor REST API.
The SUPERVISOR_TOKEN env var is injected automatically by HA into every add-on.
"""
from __future__ import annotations

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

    if produced is None:
        logger.warning('sensor.inverter_yield_monthly unavailable — skipping current-month record')
        return None

    purchased = (peak or 0.0) + (offpeak or 0.0) if (peak is not None or offpeak is not None) else None

    self_consumed_kwh         = max(0.0, produced - (exported or 0.0)) if exported is not None else None
    self_consumed_savings_pln = round(self_consumed_kwh * buy_price, 2) if (self_consumed_kwh is not None and buy_price is not None) else None
    purchase_cost_pln         = round(purchased * buy_price, 2)         if (purchased      is not None and buy_price is not None) else None
    feedin_revenue_pln        = round((exported or 0.0) * rcem_price, 2) if (exported is not None and rcem_price is not None) else None
    specific_yield            = round(produced / _SYSTEM_KWP, 1)         if _SYSTEM_KWP else None

    return MonthlyRecord(
        year=year, month=month,
        produced_kwh=produced,
        consumed_kwh=consumed,
        purchased_kwh=purchased,
        exported_kwh=exported,
        self_consumed_kwh=self_consumed_kwh,
        buy_price_pln_kwh=buy_price,
        feedin_price_pln_kwh=rcem_price,
        self_consumed_savings_pln=self_consumed_savings_pln,
        purchase_cost_pln=purchase_cost_pln,
        feedin_revenue_pln=feedin_revenue_pln,
        specific_yield=specific_yield,
        rcem_status='confirmed' if rcem_price is not None else 'pending',
    )
