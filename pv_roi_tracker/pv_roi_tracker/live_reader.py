"""
Live HA data reader — reads current-month values via the HA Supervisor REST API.
The SUPERVISOR_TOKEN env var is injected automatically by HA into every add-on.
"""
from __future__ import annotations

import calendar
import json as _json
import logging
import os
from datetime import date, datetime as _dt, timedelta, timezone as _tz
from statistics import mean
from typing import Optional
from zoneinfo import ZoneInfo

import requests
import websocket as _ws

from .models import MonthlyRecord

logger = logging.getLogger(__name__)

_BASE     = 'http://supervisor/core/api/states'
_BASE_API = 'http://supervisor/core/api'
_TOKEN    = os.environ.get('SUPERVISOR_TOKEN', '')
_HEADERS  = {'Authorization': f'Bearer {_TOKEN}', 'Content-Type': 'application/json'}
_SYSTEM_KWP           = float(os.environ.get('SYSTEM_KWP', '6.72'))
_TARIFF_PEAK_PRICE    = float(os.environ.get('TARIFF_PEAK_PRICE', '1.23'))
_TARIFF_OFFPEAK_PRICE = float(os.environ.get('TARIFF_OFFPEAK_PRICE', '0.63'))
_BATTERY_RT_EFF       = float(os.environ.get('BATTERY_ROUNDTRIP_EFFICIENCY', '0.92'))
_TZ_NAME              = os.environ.get('TZ', 'Europe/Warsaw')

# Lifetime total_increasing meters — the canonical kWh basis since v0.35.0
# (see docs/pv_roi_energy_rebase plan). Unlike the monthly utility_meter
# helpers below, these never reset, so a long-term-statistics 'change' over a
# calendar month is an exact total.
#
# WHICH entity is canonical for each role is NOT hardcoded by name — it is
# read at runtime from HA's own Energy Dashboard configuration (the same
# .storage/energy prefs Settings -> Energy reads), via
# get_energy_dashboard_sources() below. An earlier version of this file
# guessed sensor.inverter_total_yield for production by name-matching; that
# guess disagreed with what the Energy Dashboard is actually configured to
# use (sensor.energy_pv) by ~6.5% for March 2026. _FALLBACK_ENERGY_SOURCES is
# used only if the Energy Dashboard isn't configured or the WS call fails.
_FALLBACK_ENERGY_SOURCES: dict = {
    'solar':             ['sensor.inverter_total_yield'],
    'grid_import':       ['sensor.power_meter_consumption'],
    'grid_export':       ['sensor.power_meter_exported'],
    'battery_charge':    ['sensor.battery_total_charge'],
    'battery_discharge': ['sensor.battery_total_discharge'],
}

# The OTHER production-tracking family, used only for the balance.py
# cross-family plausibility check (never to compute produced_kwh itself) —
# see balance.py module docstring for why this, not a same-record residual,
# is the one signal on this installation that carries real information.
_TEMPLATE_PRODUCED_METER = 'sensor.inverter_total_yield'

# Zone-tariff split for billing (peak/off-peak) — not modeled by HA's Energy
# Dashboard schema at all (that's a tariff-billing concept layered on top via
# utility_meter tariffs), so these stay directly named rather than discovered.
# Chosen to match what the Energy Dashboard's own grid entries reference
# (sensor.daily_energy_peak/offpeak), not the old sensor.monthly_energy_peak/
# offpeak guess, so the total import these split also lines up with the
# dashboard's own grid-import total.
_ZONE_PEAK_METER    = 'sensor.daily_energy_peak'
_ZONE_OFFPEAK_METER = 'sensor.daily_energy_offpeak'

# Minimalna produkcja miesięczna uznawana za „licznik po resecie".
# Nawet pochmurny czerwiec w Polsce produkuje >5 kWh — poniżej tej wartości
# zakładamy, że utility_meter właśnie się zresetował i odczyt jest fałszywy.
# Dotyczy tylko read_current_month() (sensor.inverter_yield_monthly, resetujący
# się co miesiąc, wciąż używany jako prowizoryczne zabezpieczenie) — lifetime
# liczniki użyte w read_month_from_statistics() nigdy się nie resetują, więc
# nie potrzebują tego zabezpieczenia.
_MIN_PRODUCED_KWH = 5.0

# Ostatni znany stan dostępności Solcast — None dopóki nie próbowano odczytu.
_solcast_available: Optional[bool] = None

# Cache dla get_energy_dashboard_sources() — preferencje Energy Dashboard
# rzadko się zmieniają; odświeżane raz na start + raz dziennie (patrz main.py).
_energy_prefs_cache: Optional[dict] = None


def solcast_available() -> Optional[bool]:
    return _solcast_available


def _get_state(entity_id: str) -> Optional[float]:
    """Return an HA entity state as float, or None if unavailable/unknown."""
    try:
        resp = requests.get(f'{_BASE}/{entity_id}', headers=_HEADERS, timeout=10)
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
    try:
        resp = requests.get(f'{_BASE}/{entity_id}', headers=_HEADERS, timeout=10)
        resp.raise_for_status()
        state = resp.json().get('state', '')
        return None if state in ('unavailable', 'unknown', '') else state
    except (requests.RequestException, ValueError) as exc:
        logger.warning('Cannot read raw %s: %s', entity_id, exc)
        return None


def _ws_connect_authed(timeout: int = 30):
    """Open an authenticated HA WebSocket connection (auth handshake done).
    Caller owns the connection and must close it. Raises on any failure."""
    ws = _ws.create_connection('ws://supervisor/core/websocket', timeout=timeout)

    def _recv() -> dict:
        return _json.loads(ws.recv())

    msg = _recv()  # auth_required
    if msg.get('type') != 'auth_required':
        ws.close()
        raise RuntimeError(f'Unexpected handshake message: {msg.get("type")}')

    ws.send(_json.dumps({'type': 'auth', 'access_token': _TOKEN}))
    auth_reply = _recv()
    if auth_reply.get('type') != 'auth_ok':
        ws.close()
        raise RuntimeError(f'WebSocket auth failed: {auth_reply.get("message")}')
    return ws


def _ws_statistics(
    statistic_ids: list,
    start_iso: str,
    period: str,
    end_iso: Optional[str] = None,
    timeout: int = 30,
) -> dict:
    """
    recorder/statistics_during_period via HA WebSocket (auth handshake included).
    Returns the raw result {statistic_id: [rows]}; raises on any failure.

    HA 2026.x removed the REST /api/recorder/statistics_during_period endpoint;
    statistics are available only through the WebSocket API.
    """
    ws = None
    try:
        ws = _ws_connect_authed(timeout=timeout)
        req: dict = {
            'id': 1,
            'type': 'recorder/statistics_during_period',
            'start_time': start_iso,
            'period': period,
            'statistic_ids': statistic_ids,
            'types': ['change'],
        }
        if end_iso:
            req['end_time'] = end_iso
        ws.send(_json.dumps(req))
        stats_reply = _json.loads(ws.recv())
        if not stats_reply.get('success'):
            raise RuntimeError(f'statistics_during_period failed: {stats_reply}')
        return stats_reply.get('result', {})
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def get_energy_dashboard_sources(force_refresh: bool = False) -> dict:
    """Read HA's actual Energy Dashboard configuration (.storage/energy — the
    same prefs Settings -> Energy / the energy/get_prefs WebSocket command
    reads) so pv_roi_tracker always tracks whatever the user has configured
    there, instead of guessing an entity by name. An earlier version of this
    module picked sensor.inverter_total_yield for solar production by
    name-matching; that guess disagreed with what the Energy Dashboard is
    actually configured to use (sensor.energy_pv) by ~6.5% for March 2026.

    Returns {'solar': [...], 'grid_import': [...], 'grid_export': [...],
    'battery_charge': [...], 'battery_discharge': [...]} — lists because HA
    allows multiple entries per role (this installation has 3 grid entries:
    one dead/legacy 'from' entity plus two live peak/off-peak entities —
    summed for the role total, same as the Energy Dashboard itself does).

    Cached in-process after the first successful fetch (prefs rarely change);
    pass force_refresh=True to bypass. Falls back to _FALLBACK_ENERGY_SOURCES
    per-role if the WS call fails or a role has nothing configured, so the
    add-on still works on an install without an Energy Dashboard set up.
    """
    global _energy_prefs_cache
    if _energy_prefs_cache is not None and not force_refresh:
        return _energy_prefs_cache

    result = {role: [] for role in _FALLBACK_ENERGY_SOURCES}
    ws = None
    try:
        ws = _ws_connect_authed(timeout=15)
        ws.send(_json.dumps({'id': 1, 'type': 'energy/get_prefs'}))
        reply = _json.loads(ws.recv())
        if not reply.get('success'):
            raise RuntimeError(f'energy/get_prefs failed: {reply}')

        for src in reply.get('result', {}).get('energy_sources', []):
            stype = src.get('type')
            if stype == 'solar' and src.get('stat_energy_from'):
                result['solar'].append(src['stat_energy_from'])
            elif stype == 'grid':
                if src.get('stat_energy_from'):
                    result['grid_import'].append(src['stat_energy_from'])
                if src.get('stat_energy_to'):
                    result['grid_export'].append(src['stat_energy_to'])
            elif stype == 'battery':
                if src.get('stat_energy_from'):
                    result['battery_discharge'].append(src['stat_energy_from'])
                if src.get('stat_energy_to'):
                    result['battery_charge'].append(src['stat_energy_to'])
    except Exception as exc:
        logger.warning('get_energy_dashboard_sources: energy/get_prefs failed (%s) — '
                       'using fallback defaults for every role', exc)
        result = {role: [] for role in _FALLBACK_ENERGY_SOURCES}
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    for role, fallback in _FALLBACK_ENERGY_SOURCES.items():
        if not result[role]:
            logger.warning('Energy Dashboard: brak skonfigurowanego źródła dla %s — fallback na %s',
                          role, fallback)
            result[role] = fallback

    _energy_prefs_cache = result
    logger.info('Energy Dashboard sources resolved: %s', result)
    return result


def get_ha_tariff_stats(
    entity_ids: list,
    start: str,
    period: str = 'day',
    end: Optional[str] = None,
) -> dict:
    """
    Fetch HA long-term statistics via WebSocket for the given entity_ids.

    Returns {entity_id: {key: float}} where key format depends on period:
      'month' → 'YYYY-MM'
      'day'   → 'YYYY-MM-DD'
      'hour'  → 'YYYY-MM-DDTHH'

    Uses 'change' stat type (monthly/daily variable cost for utility_meters).
    'start'/'end' are 'YYYY-MM-DD' — converted to local midnight for bucketing
    (container TZ = Europe/Warsaw).

    end: pass this whenever the caller only needs a narrow window (notably
    _fetch_lifetime_month_stats, one target month). Leaving it unset queries
    from 'start' all the way to *now* — confirmed to reliably exceed HA's
    default statistics_during_period response time (>30s, every time) once
    'start' is more than roughly a year in the past and several entities are
    requested at once (e.g. every month rebase.simulate() touches for a
    3-year history). Callers that intentionally want the whole open range —
    main.py's tariff-stats fetch, which reads a dict of many months at once
    from a fixed early start — must keep end=None.
    """
    result = {eid: {} for eid in entity_ids}
    try:
        # 'start' must be LOCAL midnight, not UTC midnight: HA computes the
        # first returned bucket's 'change' relative to the cumulative sum at
        # start_time, so `f'{start}T00:00:00+00:00'` (02:00 CEST / 01:00 CET
        # local) silently drops the month's first 1-2 local hours from every
        # caller that queries a target month as its own start (notably
        # read_month_from_statistics) — squarely inside the G12W off-peak
        # window (22:00-06:00) and night battery charging. Callers that pass
        # a fixed early start_month (e.g. '2024-12-01') and read a later
        # month_key from the result are unaffected except for that first
        # historical bucket.
        y, m, d = (int(p) for p in start.split('-'))
        local_midnight = _dt(y, m, d, tzinfo=ZoneInfo(_TZ_NAME))
        end_iso = None
        if end is not None:
            ey, em, ed = (int(p) for p in end.split('-'))
            end_iso = _dt(ey, em, ed, tzinfo=ZoneInfo(_TZ_NAME)).isoformat()
        data = _ws_statistics(entity_ids, local_midnight.isoformat(), period, end_iso=end_iso)
        for eid in entity_ids:
            for entry in data.get(eid, []):
                start_ms = entry.get('start')
                change = entry.get('change')
                if start_ms is None or change is None:
                    continue
                try:
                    # epoch ms in UTC → local TZ (Europe/Warsaw in container)
                    dt = _dt.fromtimestamp(start_ms / 1000)
                    if period == 'month':
                        key = f'{dt.year}-{dt.month:02d}'
                    elif period == 'hour':
                        key = dt.strftime('%Y-%m-%dT%H')
                    else:  # day
                        key = dt.strftime('%Y-%m-%d')
                    result[eid][key] = round(float(change), 2)
                except Exception:
                    pass

        logger.info('HA tariff stats fetched (WebSocket, period=%s): %s',
                    period, {e: len(v) for e, v in result.items()})
        return result

    except Exception as exc:
        logger.warning('get_ha_tariff_stats failed (period=%s): %s', period, exc)
        return {eid: {} for eid in entity_ids}


_EXPORT_METER = 'sensor.power_meter_exported'
_IMPORT_METER = 'sensor.power_meter_consumption'


def get_hourly_energy(start_iso: str, end_iso: Optional[str] = None) -> dict:
    """
    Godzinowy eksport/import z liczników (LTS 'change', period=hour) dla
    symulacji rozbudowy magazynu (battery_sim).

    Zwraca {'YYYY-MM-DDTHH': (export_kwh, import_kwh)} w czasie lokalnym;
    pusty dict przy błędzie. start_iso/end_iso: 'YYYY-MM-DDTHH:MM:SS+00:00'.
    """
    try:
        data = _ws_statistics([_EXPORT_METER, _IMPORT_METER], start_iso, 'hour',
                              end_iso=end_iso, timeout=60)
        hours: dict = {}
        for eid, idx in ((_EXPORT_METER, 0), (_IMPORT_METER, 1)):
            for entry in data.get(eid, []):
                start_ms = entry.get('start')
                change = entry.get('change')
                if start_ms is None or change is None:
                    continue
                change_f = float(change)
                if change_f < 0:
                    logger.warning(
                        'get_hourly_energy: change ujemny dla %s @ %s: %.3f — '
                        'pomijam (prawdopodobnie źle wykryty reset licznika)',
                        eid, start_ms, change_f)
                    continue
                # Local hour string can collide on the autumn DST fall-back day
                # (two distinct UTC hours both map to local 02:00) — accumulate
                # rather than overwrite, or the second entry silently discards
                # the first (one real hour of export/import lost every autumn).
                key = _dt.fromtimestamp(start_ms / 1000).strftime('%Y-%m-%dT%H')
                cur = hours.setdefault(key, [0.0, 0.0])
                cur[idx] += change_f
        logger.info('get_hourly_energy: %d godzin od %s', len(hours), start_iso)
        return {k: (v[0], v[1]) for k, v in hours.items()}
    except Exception as exc:
        logger.warning('get_hourly_energy failed: %s', exc)
        return {}


def get_ha_monthly_stats(entity_ids: list, start_month: str = '2024-12-01',
                         end_month: Optional[str] = None) -> dict:
    """
    Thin wrapper around get_ha_tariff_stats for monthly statistics.
    Returns {entity_id: {YYYY-MM: float}}.

    end_month: see get_ha_tariff_stats' end= — pass this for a single-month
    lookup (_fetch_lifetime_month_stats); leave unset for the intentionally
    open-ended "every month since X" queries elsewhere in this codebase.
    """
    return get_ha_tariff_stats(entity_ids, start=start_month, period='month', end=end_month)


def _fetch_lifetime_month_stats(year: int, month: int, entity_ids: list) -> dict:
    """Fetch one calendar month's kWh 'change' for lifetime total_increasing
    meters via HA long-term statistics. Works for a closed month AND for the
    current in-progress month — HA's statistics compiler keeps a running
    'change' for the current incomplete period, updated on its normal compile
    cadence (a few minutes' lag at worst, fine at a 30-minute poll interval).

    Bounds the query to [this month, next month) — confirmed that an
    unbounded query (this month to *now*, HA's default when no end_time is
    given) reliably exceeds 30s once the target month is more than roughly a
    year old and several entities are requested at once, e.g. every month
    rebase.simulate() touches across a 3-year history.

    Returns {entity_id: float | None}. None means missing OR a negative
    'change' — some monthly-resetting helper sensors (confirmed on
    sensor.inverter_yield_self_use_monthly: -148.06 for a real month) produce
    garbage negative deltas that must never be silently coerced into 0 or
    accepted at face value; a genuine reading of exactly 0.0 is preserved
    as 0.0, not treated as missing.
    """
    start = f'{year}-{month:02d}-01'
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    end = f'{next_year}-{next_month:02d}-01'
    stats = get_ha_monthly_stats(entity_ids, start_month=start, end_month=end)
    month_key = f'{year}-{month:02d}'
    out: dict = {}
    for eid in entity_ids:
        v = stats.get(eid, {}).get(month_key)
        if v is None:
            out[eid] = None
        elif v < 0:
            logger.warning(
                'LTS change ujemny dla %s %s: %.2f — traktuję jako brak danych '
                '(prawdopodobnie źle wykryty reset licznika)', eid, month_key, v)
            out[eid] = None
        else:
            out[eid] = v
    return out


def _sum_role_month(vals: dict, entities: list) -> Optional[float]:
    """Sum the already-fetched per-entity LTS 'change' values (from
    _fetch_lifetime_month_stats) for one Energy Dashboard role (e.g. multiple
    grid-import meters). Returns None only if EVERY entity for this role is
    missing/negative; a partial read (some entities present) still sums what
    is available — the Energy Dashboard itself likewise just adds up
    whatever statistics exist for each of its configured sources.
    """
    if not entities:
        return None
    available = [vals[e] for e in entities if vals.get(e) is not None]
    if not available:
        return None
    return sum(available)


def get_ha_history_7d(entity_ids: list) -> dict:
    """
    Fetch 7-day state history from HA Recorder for the given entity_ids.
    Returns {entity_id: [{t: iso_str, v: float}]} filtered to numeric states.
    """
    now   = _dt.now(_tz.utc)
    start = (now - timedelta(days=7)).strftime('%Y-%m-%dT%H:%M:%S+00:00')
    end   = now.strftime('%Y-%m-%dT%H:%M:%S+00:00')
    ids_param = ','.join(entity_ids)
    try:
        resp = requests.get(
            f'{_BASE_API}/history/period/{start}',
            headers=_HEADERS,
            params={
                'filter_entity_id': ids_param,
                'end_time': end,
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
    global _solcast_available
    _solcast_available = any(v is not None for v in solcast_days_raw)
    if not _solcast_available:
        logger.warning('Solcast: wszystkie sensory prognozy niedostępne — brak projekcji miesiąca')
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


def _build_record(
    year: int,
    month: int,
    produced: float,
    exported: Optional[float],
    peak: Optional[float],
    offpeak: Optional[float],
    arb_kwh: Optional[float],
    rcem_price: Optional[float],
    projected_month_kwh: Optional[float] = None,
    projected_month_savings_pln: Optional[float] = None,
    peak_gross: Optional[float] = None,
    offpeak_gross: Optional[float] = None,
    imported: Optional[float] = None,
    battery_charge_kwh: Optional[float] = None,
    battery_discharge_kwh: Optional[float] = None,
    source: Optional[str] = None,
    cross_family_produced_kwh: Optional[float] = None,
) -> MonthlyRecord:
    """
    Build a MonthlyRecord from raw kWh readings.

    Called both from read_current_month() (live sensors) and
    read_month_from_statistics() (backfill from HA long-term stats).
    ``produced`` must be > 0 before calling this function.

    peak_gross/offpeak_gross: bieżące stawki taryfy (PLN/kWh, brutto),
    zwykle z tariff_config.effective_baseline() — ta sama ścieżka co
    battery_job i rekonsyliacja faktur. None → fallback na zaszyte env
    TARIFF_PEAK_PRICE/TARIFF_OFFPEAK_PRICE poniżej (opcje usunięte
    z config.yaml w 0.22.0 — to tylko siatka bezpieczeństwa, np. dla cli.py
    czy zanim tariff_config.json zostanie zasiany).

    v0.35.0 kWh-rebase notes:
      consumed_kwh is NOT a parameter here anymore — this installation has no
      independent whole-house meter (see balance.py module docstring), so it
      is always computed as self_consumed_kwh + imported, never read from a
      separate sensor.
      imported:   authoritative total import, dynamically resolved from HA's
                  Energy Dashboard config (get_energy_dashboard_sources()),
                  via LTS. None → purchased_kwh falls back to peak + offpeak
                  (may half-sum if only one zone sensor answered).
      battery_charge_kwh/battery_discharge_kwh: real battery throughput,
                  dynamically resolved the same way — stored for display,
                  not yet priced.
      source:     provenance tag stored on the record ('live' | 'lts' | None).
      cross_family_produced_kwh: the OTHER production-tracking family's own
                  figure for this month (see balance.py) — purely diagnostic,
                  never used to compute produced_kwh itself.
    """
    peak_px    = peak_gross    if peak_gross    is not None else _TARIFF_PEAK_PRICE
    offpeak_px = offpeak_gross if offpeak_gross is not None else _TARIFF_OFFPEAK_PRICE

    # Arbitraż baterii: kWh z sieci w dolinie × (peak_rate × eff − offpeak_rate)
    if arb_kwh is not None:
        arb_rate = peak_px * _BATTERY_RT_EFF - offpeak_px
        arbitrage: Optional[float] = arb_kwh * arb_rate
    else:
        arbitrage = _get_state('sensor.battery_arbitrage_savings_monthly')

    # Cena zakupu: ważona z taryf szczyt/dolina (surowe odczyty stref —
    # niezależnie od tego, czy purchased_kwh_peak/offpeak poniżej są
    # przeskalowane do sumy `imported`, ta sama ważona średnia wychodzi,
    # bo skalowanie zachowuje proporcję peak:offpeak).
    if peak is not None and offpeak is not None and (peak + offpeak) > 0:
        buy_price: Optional[float] = round(
            (peak * peak_px + offpeak * offpeak_px)
            / (peak + offpeak), 4)
    else:
        buy_price = _get_state('sensor.srednia_cena_energii_w_miesiacu')

    # purchased_kwh: authoritative import total when available (imported=
    # resolved Energy-Dashboard grid-import role via LTS), else the pre-0.35.0
    # fallback of summing the two zone sensors — which silently half-sums when
    # only one zone answered. Ratio-split the authoritative total across zones
    # so purchased_kwh_peak + purchased_kwh_offpeak always == purchased_kwh.
    if imported is not None:
        purchased: Optional[float] = imported
        if peak is not None and offpeak is not None and (peak + offpeak) > 0:
            split = peak / (peak + offpeak)
            purchased_kwh_peak: Optional[float] = round(purchased * split, 3)
            purchased_kwh_offpeak: Optional[float] = round(purchased - purchased_kwh_peak, 3)
        else:
            purchased_kwh_peak = peak
            purchased_kwh_offpeak = offpeak
    else:
        purchased = (peak or 0.0) + (offpeak or 0.0) if (peak is not None or offpeak is not None) else None
        purchased_kwh_peak = peak
        purchased_kwh_offpeak = offpeak

    # self_consumed_kwh / consumed_kwh: always derived. No independent
    # whole-house meter exists on this installation (see balance.py) — every
    # "self-use" or "house consumption" sensor HA exposes is itself just this
    # same subtraction/addition over produced/exported/imported, so reading
    # one of those instead would add nothing but a different name.
    if exported is not None:
        self_consumed_kwh: Optional[float] = max(0.0, produced - exported)
        self_consumed_source: Optional[str] = 'derived'
    else:
        self_consumed_kwh = None
        self_consumed_source = None

    consumed = (self_consumed_kwh + imported
               if (self_consumed_kwh is not None and imported is not None) else None)

    self_consumed_savings_pln = round(self_consumed_kwh * buy_price, 2) if (self_consumed_kwh is not None and buy_price is not None) else None
    purchase_cost_pln         = round(purchased * buy_price, 2)         if (purchased is not None and buy_price is not None) else None
    feedin_revenue_pln        = round((exported or 0.0) * rcem_price, 2) if (exported is not None and rcem_price is not None) else None
    specific_yield            = round(produced / _SYSTEM_KWP, 1)         if _SYSTEM_KWP else None

    record = MonthlyRecord(
        year=year, month=month,
        produced_kwh=produced,
        consumed_kwh=consumed,
        purchased_kwh=purchased,
        purchased_kwh_peak=purchased_kwh_peak,
        purchased_kwh_offpeak=purchased_kwh_offpeak,
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
        self_consumed_source=self_consumed_source,
        battery_charge_kwh=battery_charge_kwh,
        battery_discharge_kwh=battery_discharge_kwh,
        source=source,
        cross_family_produced_kwh=cross_family_produced_kwh,
    )
    from . import balance as _balance
    record.balance_residual_kwh = _balance.residual_kwh(record)
    return record


def _all_role_entities(sources: dict) -> list:
    """Flat, de-duplicated list of every entity_id across all Energy
    Dashboard roles, for a single batched LTS fetch."""
    seen: list = []
    for entities in sources.values():
        for e in entities:
            if e not in seen:
                seen.append(e)
    return seen


def read_month_from_statistics(
    year: int,
    month: int,
    rcem_price: Optional[float] = None,
    peak_gross: Optional[float] = None,
    offpeak_gross: Optional[float] = None,
) -> Optional[MonthlyRecord]:
    """
    Zbuduj MonthlyRecord na podstawie długoterminowych statystyk HA (WebSocket
    recorder/statistics_during_period), z lifetime (never-resetting) liczników
    jako kanoniczną podstawą kWh — dynamicznie odczytanych z konfiguracji HA
    Energy Dashboard (get_energy_dashboard_sources()), a nie z resetujących
    się co miesiąc utility_meter ani z odgadniętej po nazwie encji (patrz
    docstring modułu / plan docs/pv_roi_energy_rebase).

    Zwraca None, jeśli statystyki produkcji są niedostępne.
    """
    sources = get_energy_dashboard_sources()
    entity_ids = (_all_role_entities(sources)
                 + [_ZONE_PEAK_METER, _ZONE_OFFPEAK_METER,
                    'sensor.battery_grid_charge_off_peak_monthly', _TEMPLATE_PRODUCED_METER])
    vals = _fetch_lifetime_month_stats(year, month, entity_ids)
    month_key = f'{year}-{month:02d}'

    produced = _sum_role_month(vals, sources['solar'])
    if produced is None:
        logger.warning(
            'read_month_from_statistics: brak danych produkcji dla %s w statystykach HA', month_key)
        return None

    exported = _sum_role_month(vals, sources['grid_export'])
    imported = _sum_role_month(vals, sources['grid_import'])
    logger.info(
        'read_month_from_statistics %s: produced=%.1f exported=%s importowano=%s',
        month_key, produced, exported, imported,
    )
    return _build_record(
        year, month, produced, exported,
        vals.get(_ZONE_PEAK_METER), vals.get(_ZONE_OFFPEAK_METER),
        vals.get('sensor.battery_grid_charge_off_peak_monthly'), rcem_price,
        peak_gross=peak_gross, offpeak_gross=offpeak_gross,
        imported=imported,
        battery_charge_kwh=_sum_role_month(vals, sources['battery_charge']),
        battery_discharge_kwh=_sum_role_month(vals, sources['battery_discharge']),
        source='lts',
        cross_family_produced_kwh=vals.get(_TEMPLATE_PRODUCED_METER),
    )


def fetch_cross_family_produced(year: int, month: int) -> Optional[float]:
    """Single-entity LTS lookup of the cross-family production meter only.

    Used by rebase.py for invoice-reconciled months: those records are
    otherwise left 100% untouched (the invoice is final), but the balance
    check's diagnostic 'other family' figure is still worth refreshing, and
    a 1-entity query is cheap enough to do every simulate/apply run without
    the full multi-entity read_month_from_statistics() fetch.
    """
    vals = _fetch_lifetime_month_stats(year, month, [_TEMPLATE_PRODUCED_METER])
    return vals.get(_TEMPLATE_PRODUCED_METER)


def read_current_month(
    rcem_price: Optional[float] = None,
    historic_records: Optional[list] = None,
    peak_gross: Optional[float] = None,
    offpeak_gross: Optional[float] = None,
) -> Optional[MonthlyRecord]:
    """
    Build a MonthlyRecord for the current (in-progress) calendar month.

    This record is provisional by design — it is superseded once the month
    closes and gets read via read_month_from_statistics() (lifetime meters,
    exact). The day-1 reset guard below still keys on the old monthly-
    resetting sensor.inverter_yield_monthly (fast REST read, always
    available) purely as a trip-wire; the actual produced/exported/imported/
    peak/offpeak figures reported are then corrected against the Energy-
    Dashboard-resolved lifetime meters via one supplementary long-term-
    statistics call — if that extra call fails (WS/recorder hiccup), this
    degrades gracefully to the old provisional REST readings rather than
    aborting the poll.

    peak/offpeak specifically must come from the LTS call, not a raw REST
    read of _ZONE_PEAK_METER/_ZONE_OFFPEAK_METER (sensor.daily_energy_peak/
    offpeak): those reset every day, so _get_state() on them only ever
    returns today's import, not the month's — confirmed live 2 days into
    August 2026 (monthly_energy_offpeak=7.58 kWh vs daily_energy_offpeak=
    2.49 kWh, already diverged). The REST fallback below reads the old
    monthly-cycle sensors instead, which do carry a month-to-date total.

    Returns None if the primary production sensor is unavailable OR if the
    reading looks like a freshly-reset utility_meter (produced <= 5 kWh on
    the first day of the month, i.e. meters have just rolled over).
    """
    today = date.today()
    year, month = today.year, today.month

    produced_live = _get_state('sensor.inverter_yield_monthly')
    exported_live = _get_state('sensor.power_meter_exported_energy_monthly')
    peak_live     = _get_state('sensor.monthly_energy_peak')
    offpeak_live  = _get_state('sensor.monthly_energy_offpeak')
    arb_kwh       = _get_state('sensor.battery_grid_charge_off_peak_monthly')

    if produced_live is None:
        logger.warning('sensor.inverter_yield_monthly unavailable — skipping current-month record')
        return None

    # Zabezpieczenie: jeśli produced ≤ _MIN_PRODUCED_KWH w pierwszym dniu miesiąca,
    # liczniki właśnie się zresetowały (month_close strzelił po północy lokalnej).
    # Nie twórz fałszywego zerowego rekordu.
    if produced_live <= _MIN_PRODUCED_KWH and today.day == 1:
        logger.warning(
            'read_current_month: produced=%.2f kWh ≤ %.1f kWh i today.day=1 — '
            'utility_meter prawdopodobnie po resecie; pomijam snapshot %d-%02d.',
            produced_live, _MIN_PRODUCED_KWH, year, month,
        )
        return None

    sources = get_energy_dashboard_sources()
    entity_ids = (_all_role_entities(sources)
                 + [_TEMPLATE_PRODUCED_METER, _ZONE_PEAK_METER, _ZONE_OFFPEAK_METER])
    vals = _fetch_lifetime_month_stats(year, month, entity_ids)

    produced = _sum_role_month(vals, sources['solar'])
    if produced is None:
        produced = produced_live
    exported = _sum_role_month(vals, sources['grid_export'])
    if exported is None:
        exported = exported_live
    peak = vals.get(_ZONE_PEAK_METER)
    if peak is None:
        peak = peak_live
    offpeak = vals.get(_ZONE_OFFPEAK_METER)
    if offpeak is None:
        offpeak = offpeak_live
    imported = _sum_role_month(vals, sources['grid_import'])
    battery_charge_kwh = _sum_role_month(vals, sources['battery_charge'])
    battery_discharge_kwh = _sum_role_month(vals, sources['battery_discharge'])

    projected_month_kwh = _solcast_month_projection(today, produced)

    projected_month_savings_pln = None
    if projected_month_kwh is not None and historic_records:
        spk = _savings_per_kwh(historic_records, month)
        if spk is not None:
            projected_month_savings_pln = round(projected_month_kwh * spk, 2)

    return _build_record(year, month, produced, exported, peak, offpeak,
                         arb_kwh, rcem_price, projected_month_kwh, projected_month_savings_pln,
                         peak_gross=peak_gross, offpeak_gross=offpeak_gross,
                         imported=imported,
                         battery_charge_kwh=battery_charge_kwh,
                         battery_discharge_kwh=battery_discharge_kwh,
                         source='live',
                         cross_family_produced_kwh=vals.get(_TEMPLATE_PRODUCED_METER))
