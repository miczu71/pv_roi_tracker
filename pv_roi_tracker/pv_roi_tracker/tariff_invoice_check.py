"""
Kontrola faktur per strefa: stawki z faktury rozliczeniowej vs stawki modułu taryf (`legacy_rates`, dzień 15.).

Czysta logika — bez I/O poza `load_notified` / `save_notified`. Moduł taryf i faktury podaje wywołujący.
Okres cennika, którego wszystkie porównane miesiące zgadzają się z fakturą, dostaje `confidence='invoice'`.
"""
from __future__ import annotations

import calendar
import json
import logging
from datetime import date
from pathlib import Path

from . import tariff_catalog as tc
from .battery_store import _atomic_write
from .invoice_store import _period_range
from .tariff_zones import THREE_ZONE_TARIFFS

logger = logging.getLogger(__name__)

CHECK_FIELDS = ('energy_peak_net', 'energy_offpeak_net', 'dist_var_peak_net', 'dist_var_offpeak_net',
                'dist_jakosciowa_net', 'dist_oze_net', 'dist_kogeneracja_net', 'fixed_total_net',
                'peak_gross', 'offpeak_gross')
# G13: pole faktury (`*_morning` itd.) → strefa katalogu.
G13_ZONES = {'morning': 'morning_peak', 'evening': 'evening_peak', 'other': 'other'}
TOLERANCE_PCT = 2.0
CHECK_TARIFFS = ('G11', 'G12', 'G12w', 'G13')
_MAX_MSG_FIELDS = 4


def _compare(fields: dict, diverged: list, name: str, iv, mv: float, tolerance_pct: float) -> None:
    try:
        iv = float(iv)
    except (TypeError, ValueError):
        logger.warning('Faktura: pole %s ma złą wartość %r — pominięte', name, iv)
        return
    if mv == 0:
        diff, ok = (0.0 if iv == 0 else None), iv == 0
    else:
        diff = 100.0 * (iv - mv) / mv
        ok = abs(diff) <= tolerance_pct
    fields[name] = {'invoice': iv, 'module': mv, 'diff_pct': diff, 'ok': ok}
    if not ok:
        diverged.append(name)


def _check_g13(module, ym: str, inv: dict, tolerance_pct: float):
    """Kontrola faktury G13 (trzy strefy) wg okresu faktury, nie miesiąca; stałe opłaty proporcjonalnie do dni.
    None, gdy okres obejmuje zmianę taryfy/okresu cennika albo nie da się go wyznaczyć."""
    year, month = int(ym[:4]), int(ym[5:7])
    rng = _period_range(inv.get('billing_period_raw'))
    try:
        if rng:
            start, end = date(*rng[0]), date(*rng[1])
        else:
            start, end = date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
        tariff = module.tariff_on(start)
        if tariff != module.tariff_on(end) or tariff not in THREE_ZONE_TARIFFS:
            return None
        period, period_from = module._period_info(start)[:2]
        if module._period_info(end)[1] != period_from:
            return None
        excise = tc._scalar(period, 'excise_net')
        scalars = {'dist_jakosciowa_net': tc._scalar(period, 'quality_net'),
                   'dist_oze_net': tc._scalar(period, 'oze_net'),
                   'dist_kogeneracja_net': tc._scalar(period, 'cogeneration_net')}
        rates = {z: tc.rates_per_kwh(tariff, period, cz) for z, cz in G13_ZONES.items()}
        network = module.fixed(start, tariff)['network']
    except (ValueError, LookupError, KeyError):
        return None
    days = (end - start).days + 1
    fields: dict = {}
    diverged: list = []
    for z, r in rates.items():
        pairs = ((f'energy_{z}_net', r['energy'] + excise), (f'dist_var_{z}_net', r['dist']),
                 (f'{z}_gross', (r['energy'] + r['dist'] + r['fees']) * module.vat))
        for name, mv in pairs:
            if inv.get(name) is not None:
                _compare(fields, diverged, name, inv[name], mv, tolerance_pct)
    for name, mv in scalars.items():
        if inv.get(name) is not None:
            _compare(fields, diverged, name, inv[name], mv, tolerance_pct)
    if inv.get('fixed_stalysieciowy_net') is not None:
        prorated = network * days / calendar.monthrange(start.year, start.month)[1]
        _compare(fields, diverged, 'fixed_stalysieciowy_net', inv['fixed_stalysieciowy_net'], prorated, tolerance_pct)
    if not fields:
        return None
    return {'tariff': tariff, 'period_from': period_from, 'ok': not diverged, 'fields': fields, 'diverged': diverged}


def check_invoices(module, invoices: dict, tolerance_pct: float = TOLERANCE_PCT) -> dict:
    """`{YYYY-MM: wynik}` dla faktur dwustrefowych (miesiące bez zmiany taryfy) i G13 (okres faktury);
    pozostałe pomijane."""
    out: dict = {}
    for key in sorted(invoices or {}):
        inv = invoices[key]
        if not isinstance(inv, dict):
            continue
        ym = key[:7]
        if inv.get('energy_morning_net') is not None:
            try:
                res = _check_g13(module, ym, inv, tolerance_pct)
            except ValueError:
                res = None
            if res:
                out[ym] = res
            continue
        try:
            year, month = int(ym[:4]), int(ym[5:7])
            first = date(year, month, 1)
            last = date(year, month, calendar.monthrange(year, month)[1])
            mid = date(year, month, 15)
            tariff = module.tariff_on(first)
            if tariff != module.tariff_on(last) or tariff not in CHECK_TARIFFS:
                continue
            mod = module.legacy_rates(mid)
        except (ValueError, LookupError):
            continue
        fields: dict = {}
        diverged: list = []
        for f in CHECK_FIELDS:
            iv = inv.get(f)
            if iv is None or f not in mod:
                continue
            try:
                iv = float(iv)
            except (TypeError, ValueError):
                logger.warning('Faktura %s: pole %s ma złą wartość %r — pominięte', ym, f, iv)
                continue
            mv = float(mod[f])
            if mv == 0:
                diff = 0.0 if iv == 0 else None
                ok = iv == 0
            else:
                diff = 100.0 * (iv - mv) / mv
                ok = abs(diff) <= tolerance_pct
            fields[f] = {'invoice': iv, 'module': mv, 'diff_pct': diff, 'ok': ok}
            if not ok:
                diverged.append(f)
        if not fields:
            continue
        out[ym] = {'tariff': tariff, 'period_from': module._period_info(mid)[1],
                   'ok': not diverged, 'fields': fields, 'diverged': diverged}
    return out


def invoice_confirmed_periods(check: dict) -> list:
    """Okresy cennika (`period_from`), w których wszystkie porównane miesiące są zgodne z fakturą."""
    ok = {m['period_from'] for m in (check or {}).values() if m['ok']}
    bad = {m['period_from'] for m in (check or {}).values() if not m['ok']}
    return sorted(ok - bad)


def pending_notifications(check: dict, notified: dict) -> tuple:
    """Komunikaty dla nowych rozbieżności (sygnatura = pola rozbieżne; bez powtórek). `(msgs, nowy_notified)`."""
    msgs: list = []
    new = {ym: sig for ym, sig in (notified or {}).items() if ym in (check or {})}
    for ym in sorted(check or {}):
        m = check[ym]
        if m['ok']:
            new.pop(ym, None)
            continue
        sig = ','.join(sorted(m['diverged']))
        new[ym] = sig
        if (notified or {}).get(ym) == sig:
            continue
        parts = []
        for f in m['diverged'][:_MAX_MSG_FIELDS]:
            x = m['fields'][f]
            parts.append(f"{f} {x['invoice']:.4f} zł vs cennik {x['module']:.4f} zł")
        if len(m['diverged']) > _MAX_MSG_FIELDS:
            parts.append('…')
        msgs.append(f"Faktura {ym}: stawki ({m['tariff']}) rozjeżdżają się z cennikiem o więcej niż "
                    f"{TOLERANCE_PCT:g}%: " + '; '.join(parts))
    return msgs, new


def load_notified(path) -> dict:
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning('Plik powiadomień o fakturach uszkodzony: %s', path)
        return {}
    if not isinstance(data, dict):
        logger.warning('Plik powiadomień o fakturach uszkodzony: %s', path)
        return {}
    return data


def save_notified(notified: dict, path) -> None:
    _atomic_write(Path(path), notified)
