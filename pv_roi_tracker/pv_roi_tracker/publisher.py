"""
MQTT discovery publisher for PV ROI sensors.

Publishes the _SENSORS list (+ health) under a single 'PV ROI Tracker' device
using HA MQTT discovery.
Uses paho-mqtt with loop_start() so publishing runs from the main thread
while paho reconnects in the background automatically.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, NamedTuple, Optional

import paho.mqtt.client as mqtt

from .roi import RoiResult

logger = logging.getLogger(__name__)

_DEVICE_ID     = 'pv_roi_tracker'
_AVAIL_TOPIC   = 'pv_roi/availability'
_STATE_PREFIX  = 'pv_roi/sensors'
_DISC_PREFIX   = 'homeassistant'


class _Sensor(NamedTuple):
    slug: str
    name: str
    attr: Optional[str]        # RoiResult attribute, or None for special handling
    unit: Optional[str]
    device_class: Optional[str]
    state_class: Optional[str]
    icon: Optional[str]


_SENSORS: list[_Sensor] = [
    _Sensor('roi_pct',                  'PV ROI',                     'roi_pct',                  '%',       None,       'measurement',      'mdi:percent'),
    _Sensor('payback_years',            'PV Payback Remaining',        'years_to_payback',          'years',   None,       'measurement',      'mdi:calendar-clock'),
    _Sensor('payback_date',             'PV Payback Date',             'payback_date',              None,      'date',     None,               'mdi:calendar-check'),
    _Sensor('total_savings',            'PV Total Savings',            'total_savings',             'PLN',     'monetary', 'total_increasing', 'mdi:cash-plus'),
    _Sensor('self_consumption_savings', 'PV Self-Consumption Savings', 'self_consumption_savings',  'PLN',     'monetary', 'total_increasing', 'mdi:home-lightning-bolt'),
    _Sensor('feedin_revenue',           'PV Feed-in Revenue',          'feedin_revenue',            'PLN',     'monetary', 'total_increasing', 'mdi:transmission-tower-export'),
    _Sensor('net_investment',           'PV Net Investment',           None,                        'PLN',     'monetary', 'measurement',      'mdi:bank'),
    _Sensor('monthly_avg_savings',      'PV Monthly Avg Savings',      'monthly_avg_savings',       'PLN',     'monetary', 'measurement',      'mdi:chart-line'),
    _Sensor('total_produced_kwh',       'PV Total Produced',           'total_produced_kwh',        'kWh',     'energy',   'total_increasing', 'mdi:solar-power'),
    _Sensor('total_exported_kwh',       'PV Total Exported',           'total_exported_kwh',        'kWh',     'energy',   'total_increasing', 'mdi:transmission-tower'),
    _Sensor('specific_yield',           'PV Specific Yield',           'specific_yield_lifetime',   'kWh/kWp', None,       'total_increasing', 'mdi:chart-bar'),
    _Sensor('battery_arbitrage_savings', 'PV Battery Arbitrage Savings', 'battery_arbitrage_savings', 'PLN',    'monetary', 'total_increasing', 'mdi:battery-charging'),
    _Sensor('net_profit',              'PV Net Profit',               'net_profit',                'PLN',     'monetary', 'total_increasing', 'mdi:cash-multiple'),
    _Sensor('current_month_savings',      'PV Savings This Month',          None,                          'PLN',  'monetary', 'measurement',      'mdi:calendar-today'),
    _Sensor('rcem_scrape_status',         'RCEm Scrape Status',             None,                          None,   None,       None,               'mdi:cloud-sync'),
    # Solcast projection
    _Sensor('projected_month_kwh',        'PV Projected Month kWh',         None,                          'kWh',  'energy',   'measurement',      'mdi:solar-power-variant'),
    _Sensor('projected_month_savings',    'PV Projected Month Savings',     None,                          'PLN',  'monetary', 'measurement',      'mdi:chart-timeline-variant'),
    # Financial analysis
    _Sensor('real_total_savings',         'PV Real Total Savings',          'real_total_savings',          'PLN',  'monetary', 'total_increasing', 'mdi:cash-check'),
    _Sensor('real_roi_pct',               'PV Real ROI',                    'real_roi_pct',                '%',    None,       'measurement',      'mdi:percent-outline'),
    _Sensor('npv',                        'PV NPV',                         'npv',                         'PLN',  'monetary', 'measurement',      'mdi:bank-outline'),
    _Sensor('irr_pct',                    'PV IRR',                         'irr_pct',                     '%',    None,       'measurement',      'mdi:trending-up'),
    _Sensor('vs_bond_delta',              'PV vs Bond Delta',               'counterfactual_delta',        'PLN',  'monetary', 'measurement',      'mdi:scale-balance'),
    _Sensor('cumulative_inflation',       'PV Cumulative Inflation',        'cumulative_inflation_pct',    '%',    None,       'measurement',      'mdi:trending-up'),
    # Wskaźniki energetyczne (v0.17.0)
    _Sensor('self_consumption_rate',      'PV Self-Consumption Rate',       'self_consumption_rate_pct',   '%',    None,       'measurement',      'mdi:home-percent'),
    _Sensor('autarky',                    'PV Autarky',                     'autarky_pct',                 '%',    None,       'measurement',      'mdi:home-battery'),
    _Sensor('co2_avoided',                'PV CO2 Avoided',                 'co2_avoided_kg',              'kg',   None,       'total_increasing', 'mdi:molecule-co2'),
    _Sensor('yoy_yield_delta',            'PV YoY Yield Delta',             'yoy_yield_delta_pct',         '%',    None,       'measurement',      'mdi:sun-clock'),
    # Alert „poniżej oczekiwań" (v0.27.0)
    _Sensor('underperformance_pct',       'PV Underperformance',            'underperformance_pct',        '%',    None,       'measurement',      'mdi:solar-power-variant-outline'),
    _Sensor('underperformance_flag',      'PV Underperformance Flag',       'underperformance_flag',       None,   None,       None,               'mdi:alert-circle-outline'),
    # Depozyt prosumencki (v0.17.0)
    _Sensor('deposit_balance_est',        'PV Deposit Balance Est',         None,                          'PLN',  'monetary', 'measurement',      'mdi:piggy-bank'),
    _Sensor('deposit_expiring_30d',       'PV Deposit Expiring 30d',        None,                          'PLN',  'monetary', 'measurement',      'mdi:timer-sand'),
    # Latest-invoice rates (v0.20.0) — single source of truth for energy_simulation.yaml
    # and Analiza taryf; values come from invoice_rates (see _render_value), not RoiResult.
    # NOTE: per-kWh rates are NOT device_class 'monetary' — HA's monetary class requires a
    # currency unit (PLN), and 'PLN/kWh' triggers warnings + blocks long-term stats.
    # Only the whole-PLN/month fixed_* charges keep device_class 'monetary'.
    _Sensor('rate_energy_peak_net',       'PV Rate Energy Peak',            None,                          'PLN/kWh', None,       'measurement', 'mdi:flash'),
    _Sensor('rate_energy_offpeak_net',    'PV Rate Energy Offpeak',         None,                          'PLN/kWh', None,       'measurement', 'mdi:flash-outline'),
    _Sensor('rate_dist_var_peak_net',     'PV Rate Dist Var Peak',          None,                          'PLN/kWh', None,       'measurement', 'mdi:transmission-tower'),
    _Sensor('rate_dist_var_offpeak_net',  'PV Rate Dist Var Offpeak',       None,                          'PLN/kWh', None,       'measurement', 'mdi:transmission-tower'),
    _Sensor('rate_jakosciowa_net',        'PV Rate Jakościowa',             None,                          'PLN/kWh', None,       'measurement', 'mdi:certificate'),
    _Sensor('rate_oze_net',               'PV Rate OZE',                    None,                          'PLN/kWh', None,       'measurement', 'mdi:leaf'),
    _Sensor('rate_kogeneracja_net',       'PV Rate Kogeneracja',            None,                          'PLN/kWh', None,       'measurement', 'mdi:factory'),
    _Sensor('fixed_mocowa_net',           'PV Fixed Mocowa',                None,                          'PLN',     'monetary', 'measurement', 'mdi:gauge'),
    _Sensor('fixed_abonament_net',        'PV Fixed Abonament',             None,                          'PLN',     'monetary', 'measurement', 'mdi:receipt'),
    _Sensor('fixed_stalysieciowy_net',    'PV Fixed Stały Sieciowy',        None,                          'PLN',     'monetary', 'measurement', 'mdi:transmission-tower'),
    _Sensor('fixed_total_net',            'PV Fixed Total Net',             None,                          'PLN',     'monetary', 'measurement', 'mdi:sigma'),
    _Sensor('rate_peak_gross',            'PV Rate Peak Gross',             None,                          'PLN/kWh', None,       'measurement', 'mdi:cash'),
    _Sensor('rate_offpeak_gross',         'PV Rate Offpeak Gross',          None,                          'PLN/kWh', None,       'measurement', 'mdi:cash-outline'),
]

# slug → key in the invoice_rates dict (web.latest_invoice_rates()) for the
# sensors above. Kept separate from _SENSORS so the table stays scannable.
_INVOICE_RATE_SENSORS: dict[str, str] = {
    'rate_energy_peak_net':      'energy_peak_net',
    'rate_energy_offpeak_net':   'energy_offpeak_net',
    'rate_dist_var_peak_net':    'dist_var_peak_net',
    'rate_dist_var_offpeak_net': 'dist_var_offpeak_net',
    'rate_jakosciowa_net':       'dist_jakosciowa_net',
    'rate_oze_net':              'dist_oze_net',
    'rate_kogeneracja_net':      'dist_kogeneracja_net',
    'fixed_mocowa_net':          'fixed_mocowa_net',
    'fixed_abonament_net':       'fixed_abonament_net',
    'fixed_stalysieciowy_net':   'fixed_stalysieciowy_net',
    'fixed_total_net':           'fixed_total_net',
    'rate_peak_gross':           'peak_gross',
    'rate_offpeak_gross':        'offpeak_gross',
}

# Sensors removed in previous versions — clear their retained discovery messages on connect.
_TOMBSTONED_SLUGS: list[str] = ['rcem_current_month']

# Health sensor — publikowany osobno (stan + atrybuty JSON z detalami zadań).
_HEALTH_SLUG = 'health'


def _state_topic(slug: str) -> str:
    return f'{_STATE_PREFIX}/{slug}/state'


def _disc_topic(slug: str) -> str:
    return f'{_DISC_PREFIX}/sensor/{_DEVICE_ID}/{slug}/config'


def _render_value(sensor: _Sensor, result: RoiResult,
                  current_month_savings: Optional[float] = None,
                  rcem_scrape_status: Optional[str] = None,
                  projected_month_kwh: Optional[float] = None,
                  projected_month_savings: Optional[float] = None,
                  deposit_balance: Optional[float] = None,
                  deposit_expiring_30d: Optional[float] = None,
                  invoice_rates: Optional[dict] = None) -> str:
    if sensor.slug == 'net_investment':
        v: Any = round(result.gross_investment - result.subsidy, 2)
    elif sensor.slug == 'current_month_savings':
        v = current_month_savings
    elif sensor.slug == 'rcem_scrape_status':
        v = rcem_scrape_status
    elif sensor.slug == 'projected_month_kwh':
        v = projected_month_kwh
    elif sensor.slug == 'projected_month_savings':
        v = projected_month_savings
    elif sensor.slug == 'deposit_balance_est':
        v = deposit_balance
    elif sensor.slug == 'deposit_expiring_30d':
        v = deposit_expiring_30d
    elif sensor.slug in _INVOICE_RATE_SENSORS:
        v = (invoice_rates or {}).get(_INVOICE_RATE_SENSORS[sensor.slug])
    else:
        v = getattr(result, sensor.attr) if sensor.attr else None

    if v is None:
        return 'unknown'
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        return str(round(v, 4))
    return str(v)


class MQTTPublisher:
    def __init__(self, host: str, port: int, user: str, password: str, version: str = '0.2.0') -> None:
        self._host = host
        self._port = port
        self._version = version
        self._connected = False

        self._client = mqtt.Client(client_id=_DEVICE_ID, clean_session=True)
        if user:
            self._client.username_pw_set(user, password)
        self._client.will_set(_AVAIL_TOPIC, 'offline', retain=True)
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    # ── Connection lifecycle ──────────────────────────────────────────────────

    def connect(self) -> None:
        self._client.connect_async(self._host, self._port, keepalive=60)
        self._client.loop_start()

    def disconnect(self) -> None:
        self._client.publish(_AVAIL_TOPIC, 'offline', retain=True)
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc == 0:
            self._connected = True
            logger.info('MQTT connected to %s:%d', self._host, self._port)
            self._publish_discovery()
            client.publish(_AVAIL_TOPIC, 'online', retain=True)
        else:
            logger.error('MQTT connect failed (rc=%d)', rc)

    def _on_disconnect(self, client, userdata, rc) -> None:
        self._connected = False
        if rc != 0:
            logger.warning('MQTT unexpectedly disconnected (rc=%d) — paho will reconnect', rc)

    # ── Discovery ─────────────────────────────────────────────────────────────

    def _publish_discovery(self) -> None:
        device = {
            'identifiers': [_DEVICE_ID],
            'name': 'PV ROI Tracker',
            'manufacturer': 'Custom',
            'model': 'pv_roi_tracker',
            'sw_version': self._version,
        }
        for s in _SENSORS:
            payload: dict = {
                'name':               s.name,
                'unique_id':          f'{_DEVICE_ID}_{s.slug}',
                'state_topic':        _state_topic(s.slug),
                'availability_topic': _AVAIL_TOPIC,
                'device':             device,
            }
            if s.unit:         payload['unit_of_measurement'] = s.unit
            if s.device_class: payload['device_class']        = s.device_class
            if s.state_class:  payload['state_class']         = s.state_class
            if s.icon:         payload['icon']                 = s.icon
            self._client.publish(_disc_topic(s.slug), json.dumps(payload), retain=True)
        health_payload = {
            'name':                  'PV ROI Tracker Health',
            'unique_id':             f'{_DEVICE_ID}_{_HEALTH_SLUG}',
            'state_topic':           _state_topic(_HEALTH_SLUG),
            'json_attributes_topic': f'{_STATE_PREFIX}/{_HEALTH_SLUG}/attributes',
            'availability_topic':    _AVAIL_TOPIC,
            'device':                device,
            'icon':                  'mdi:heart-pulse',
        }
        self._client.publish(_disc_topic(_HEALTH_SLUG), json.dumps(health_payload), retain=True)
        for slug in _TOMBSTONED_SLUGS:
            self._client.publish(_disc_topic(slug), '', retain=True)
        logger.info('MQTT discovery published for %d sensors', len(_SENSORS) + 1)

    # ── State publishing ──────────────────────────────────────────────────────

    def publish_roi(self, result: RoiResult,
                    current_month_savings: Optional[float] = None,
                    rcem_scrape_status: Optional[str] = None,
                    projected_month_kwh: Optional[float] = None,
                    projected_month_savings: Optional[float] = None,
                    deposit_balance: Optional[float] = None,
                    deposit_expiring_30d: Optional[float] = None,
                    invoice_rates: Optional[dict] = None) -> None:
        if not self._connected:
            logger.debug('MQTT not connected — skipping publish')
            return
        for s in _SENSORS:
            payload = _render_value(s, result, current_month_savings, rcem_scrape_status,
                                    projected_month_kwh, projected_month_savings,
                                    deposit_balance, deposit_expiring_30d, invoice_rates)
            self._client.publish(_state_topic(s.slug), payload, retain=True)
        logger.debug('Published ROI state to MQTT (roi_pct=%.2f%%)', result.roi_pct)

    def publish_health(self, state: str, attributes: dict) -> None:
        """Publikuje stan zdrowia add-onu: 'ok' | 'degraded' | 'error' + atrybuty zadań."""
        if not self._connected:
            return
        self._client.publish(_state_topic(_HEALTH_SLUG), state, retain=True)
        self._client.publish(f'{_STATE_PREFIX}/{_HEALTH_SLUG}/attributes',
                             json.dumps(attributes, ensure_ascii=False), retain=True)
