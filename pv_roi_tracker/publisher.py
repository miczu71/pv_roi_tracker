"""
MQTT discovery publisher for PV ROI sensors.

Publishes 12 sensors under a single 'PV ROI Tracker' device using HA MQTT discovery.
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
]

# Sensors removed in previous versions — clear their retained discovery messages on connect.
_TOMBSTONED_SLUGS: list[str] = ['rcem_current_month']


def _state_topic(slug: str) -> str:
    return f'{_STATE_PREFIX}/{slug}/state'


def _disc_topic(slug: str) -> str:
    return f'{_DISC_PREFIX}/sensor/{_DEVICE_ID}/{slug}/config'


def _render_value(sensor: _Sensor, result: RoiResult,
                  current_month_savings: Optional[float] = None,
                  rcem_scrape_status: Optional[str] = None,
                  projected_month_kwh: Optional[float] = None,
                  projected_month_savings: Optional[float] = None) -> str:
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
        for slug in _TOMBSTONED_SLUGS:
            self._client.publish(_disc_topic(slug), '', retain=True)
        logger.info('MQTT discovery published for %d sensors', len(_SENSORS))

    # ── State publishing ──────────────────────────────────────────────────────

    def publish_roi(self, result: RoiResult,
                    current_month_savings: Optional[float] = None,
                    rcem_scrape_status: Optional[str] = None,
                    projected_month_kwh: Optional[float] = None,
                    projected_month_savings: Optional[float] = None) -> None:
        if not self._connected:
            logger.debug('MQTT not connected — skipping publish')
            return
        for s in _SENSORS:
            payload = _render_value(s, result, current_month_savings, rcem_scrape_status,
                                    projected_month_kwh, projected_month_savings)
            self._client.publish(_state_topic(s.slug), payload, retain=True)
        logger.debug('Published ROI state to MQTT (roi_pct=%.2f%%)', result.roi_pct)
