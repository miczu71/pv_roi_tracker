"""
Przejrzystość modułu taryf (T6) — czysta logika odpowiedzi „skąd ta cena”: wiersze cennika, rozbicie
na składniki i porównania. Bez I/O; moduł taryf, pobór godzinowy i faktury podaje wywołujący.

Stawki i ceny pochodzą wyłącznie z `TariffModule` / `tariff_catalog` (nic nie jest tu zduplikowane).
Błędne parametry i braki danych to `ValueError` (po polsku) — web zamienia je na 400.
"""
from __future__ import annotations

import calendar
import dataclasses
import re
from datetime import date, datetime
from typing import Optional

from . import tariff_catalog as tc
from . import tariff_invoice_check as tic
from .tariff_module import CONFIDENCES

VARIABLE_KEYS = ('energy', 'dist', 'quality', 'oze', 'cogeneration', 'excise')
FIXED_KEYS = ('network', 'subscription_dist', 'capacity', 'transitional', 'sales_fee')
ALL_KEYS = VARIABLE_KEYS + FIXED_KEYS + ('vat',)
LABELS = {
    'energy': 'Energia czynna', 'dist': 'Dystrybucja zmienna', 'quality': 'Jakościowa', 'oze': 'OZE',
    'cogeneration': 'Kogeneracja', 'excise': 'Akcyza', 'network': 'Sieciowa',
    'subscription_dist': 'Abonament dystrybucyjny', 'capacity': 'Mocowa', 'transitional': 'Przejściowa',
    'sales_fee': 'Handlowa', 'vat': 'VAT',
}
UNIT_VAR, UNIT_FIX = 'zł/kWh', 'zł/mies.'
ROWS_LIMIT = 2000
_SCALAR_FIELDS = {'quality': 'quality_net', 'oze': 'oze_net',
                  'cogeneration': 'cogeneration_net', 'excise': 'excise_net'}
_ZONE_FIELDS = {'energy': 'energy_net', 'dist': 'dist_var_net'}
_YM = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')


# ── parsowanie wspólne ───────────────────────────────────────────────────────

def tariff_names(catalog: dict) -> list:
    return [t for t in catalog['zones'] if t != 'dynamic_distribution_zones']


def check_tariff(catalog: dict, tariff: str) -> str:
    if tariff not in tariff_names(catalog):
        raise ValueError(f'nieznana taryfa: {tariff}')
    return tariff


def parse_include(raw: Optional[str]) -> tuple:
    if raw is None or raw == '':
        return ALL_KEYS
    keys = tuple(k.strip() for k in raw.split(',') if k.strip())
    bad = [k for k in keys if k not in ALL_KEYS]
    if bad or not keys:
        raise ValueError(f'nieznany składnik w include: {", ".join(bad) or raw}')
    return keys


def parse_basis(raw: Optional[str]) -> str:
    basis = raw or 'gross'
    if basis not in ('net', 'gross'):
        raise ValueError('basis musi być net albo gross')
    return basis


def _day(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f'{label} musi mieć format RRRR-MM-DD') from None


def _hour(value: str) -> datetime:
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H')
    except (TypeError, ValueError):
        raise ValueError('at musi mieć format RRRR-MM-DDTGG') from None


def _month(value: str, label: str = 'month') -> tuple:
    if not value or not _YM.match(value):
        raise ValueError(f'{label} musi mieć format RRRR-MM')
    return int(value[:4]), int(value[5:7])


def _round(v):
    return None if v is None else round(v, 6)


def _weakest(confidences) -> str:
    return max(confidences, key=CONFIDENCES.index) if confidences else 'unconfirmed'


# ── /api/tariff/rates ────────────────────────────────────────────────────────

def _sources(catalog: dict, period: dict) -> tuple:
    """(klucze, [{key,url}]) źródeł okresu (`sources` lista albo `source` tekst)."""
    raw = period.get('sources') if period.get('sources') is not None else period.get('source')
    keys = [raw] if isinstance(raw, str) else [str(k) for k in (raw or [])]
    urls = (catalog.get('_meta') or {}).get('sources') or {}
    return keys, [{'key': k, 'url': urls.get(k)} for k in keys]


def _catalog_confidence(module, period: dict, tariff: str) -> str:
    if period.get('confirmed') and tariff not in (period.get('unconfirmed_tariffs') or []):
        return 'document'
    if period['valid_from'] in module.invoice_ok:
        return 'invoice'
    return 'unconfirmed'


def _fixed_values(period: dict, settings, tariff: str) -> dict:
    try:
        return tc.fixed_monthly(period, settings, tariff)
    except ValueError:
        try:
            out = tc.fixed_monthly(period, dataclasses.replace(settings, sales_fee_g13s_net=0.0), tariff)
            out['sales_fee'] = None
            return out
        except (ValueError, TypeError, KeyError):
            pass
    except (TypeError, KeyError):
        pass
    return {k: None for k in FIXED_KEYS}


def _period_values(period: dict, tariff: str, settings) -> list:
    """[(klucz, strefa|None, net|None)] dla taryfy w rozwiniętym okresie katalogu."""
    out = []
    for key in ('energy', 'dist'):
        for zone, v in (period.get(_ZONE_FIELDS[key]) or {}).get(tariff, {}).items():
            out.append((key, zone, v))
    for key, field in _SCALAR_FIELDS.items():
        out.append((key, None, period.get(field)))
    fixed = _fixed_values(period, settings, tariff)
    for key in FIXED_KEYS:
        out.append((key, None, fixed[key]))
    return out


def _patch_values(patch: dict, tariff: str, settings) -> list:
    out = []
    for key in ('energy', 'dist'):
        for zone, v in ((patch.get(_ZONE_FIELDS[key]) or {}).get(tariff) or {}).items():
            out.append((key, zone, v))
    for key, field in _SCALAR_FIELDS.items():
        if field in patch:
            out.append((key, None, patch[field]))
    fx = patch.get('fixed_net') or {}
    net_key = 'network_3ph' if settings.phases == 3 else 'network_1ph'
    simple = {'network': fx.get(net_key), 'subscription_dist': fx.get('subscription_dist'),
              'capacity': (fx.get('capacity_by_annual_kwh') or {}).get(settings.capacity_bracket),
              'transitional': (fx.get('transitional_by_annual_kwh') or {}).get(
                  tc._transitional_key(settings.capacity_bracket)),
              'sales_fee': (fx.get('sales_fee_by_tariff') or {}).get(tariff)}
    for key in FIXED_KEYS:
        if simple[key] is not None:
            out.append((key, None, simple[key]))
    return out


def _invoice_numbers(invoices: dict, p_from: str, p_to: Optional[str]) -> Optional[str]:
    """Numery faktur rozliczeniowych z miesięcy okresu (tylko odczytane, bez zgadywania)."""
    nums = set()
    for key, rec in (invoices or {}).items():
        ym = key[:7]
        if '~' in key or not _YM.match(ym) or not isinstance(rec, dict):
            continue
        if ym < p_from[:7] or (p_to is not None and ym > p_to[:7]):
            continue
        num = rec.get('invoice_number')
        if isinstance(num, str) and num.strip():
            nums.add(num.strip())
    return ', '.join(sorted(nums)) or None


def rates_rows(module, invoices: Optional[dict], *, tariff=None, zone=None, component=None,
               date_from=None, date_to=None, source=None, confidence=None, q=None,
               limit: int = ROWS_LIMIT) -> dict:
    """Wiersze cennika: okresy katalogu (rozwinięte) + korekty ręczne, filtry łączone AND."""
    cat = module.catalog
    vat = module.vat
    tariffs = [check_tariff(cat, tariff)] if tariff else tariff_names(cat)
    if component and component not in VARIABLE_KEYS + FIXED_KEYS:
        raise ValueError(f'nieznany składnik: {component}')
    if confidence and confidence not in CONFIDENCES:
        raise ValueError(f'nieznana pewność: {confidence}')
    d_from = _day(date_from, 'from').isoformat() if date_from else None
    d_to = _day(date_to, 'to').isoformat() if date_to else None
    if d_from and d_to and d_to < d_from:
        raise ValueError('to nie może być przed from')
    needle = (q or '').strip().lower()

    rows: list = []

    def add(p_from, p_to, t, z, key, net, conf, srcs, note, inv_no):
        if component and key != component:
            return
        if zone and z != zone:
            return
        if confidence and conf != confidence:
            return
        keys = [s['key'] for s in srcs]
        if source and source not in keys:
            return
        if d_from and p_to is not None and p_to < d_from:
            return
        if d_to and p_from > d_to:
            return
        label = LABELS[key]
        if needle and needle not in ' '.join(str(x or '') for x in (label, t, z, note)).lower():
            return
        first = srcs[0] if srcs else None
        rows.append({
            'period_from': p_from, 'period_to': p_to, 'tariff': t, 'zone': z, 'component': key,
            'label': label, 'unit': UNIT_VAR if key in VARIABLE_KEYS else UNIT_FIX,
            'net': _round(net), 'gross': _round(None if net is None else net * vat),
            'confidence': conf, 'source_key': ', '.join(keys) or None,
            'source_url': first['url'] if first else None,
            'doc_label': ('Źródło ' + ', '.join(keys)) if keys else None,
            'sources': srcs, 'invoice_no': inv_no, 'note': note or None})

    for entry in sorted(cat['periods'], key=lambda p: p['valid_from']):
        period = tc._expand(cat, entry)
        keys, srcs = _sources(cat, period)
        for t in tariffs:
            conf = _catalog_confidence(module, period, t)
            inv_no = _invoice_numbers(invoices, period['valid_from'], period['valid_to']) \
                if conf == 'invoice' else None
            for key, z, v in _period_values(period, t, module.settings):
                add(period['valid_from'], period['valid_to'], t, z, key, v, conf, srcs,
                    period.get('note'), inv_no)
    for ov in module.overrides:
        for t in tariffs:
            for key, z, v in _patch_values(ov.get('patch') or {}, t, module.settings):
                add(ov['from'], ov.get('to'), t, z, key, v, 'manual', [], ov.get('note'), None)

    order = {k: i for i, k in enumerate(VARIABLE_KEYS + FIXED_KEYS)}
    rows.sort(key=lambda r: (r['period_from'], r['tariff'], order[r['component']], r['zone'] or '',
                             r['confidence'] == 'manual'))
    truncated = len(rows) > limit
    rows = rows[:limit]
    return {'ok': True, 'count': len(rows), 'truncated': truncated, 'rows': rows}


# ── rozbicie ─────────────────────────────────────────────────────────────────

def _core(module, import_by_hour: dict, tariff: str, scope: str, target) -> dict:
    """Sumy netto składników za zakres. `target`: datetime (hour), date (day), (rok, mies.) (month/invoice),
    rok (year). Brak godzin poboru → `kwh` 0, składniki stałe nadal liczone."""
    if scope == 'hour':
        pp = module.price_at(target, tariff)
        return {'kwh': 1.0, 'confidence': pp.confidence, 'var': dict(pp.components), 'fixed': {},
                'rates': dict(pp.components), 'note': None}
    if scope == 'day':
        prefix, ref = target.isoformat(), target
        dim = calendar.monthrange(target.year, target.month)[1]
        fixed_days = [(target, 1.0 / dim)]
    elif scope in ('month', 'invoice'):
        y, m = target
        prefix, ref = f'{y:04d}-{m:02d}', date(y, m, 1)
        fixed_days = [(date(y, m, 15), 1.0)]
    else:  # year
        prefix, ref = f'{target:04d}', date(target, 1, 1)
        fixed_days = [(date(target, m, 15), 1.0) for m in range(1, 13)]
    var = {k: 0.0 for k in VARIABLE_KEYS}
    kwh = 0.0
    confs = []
    for key in sorted(import_by_hour):
        v = float(import_by_hour[key])
        if not key.startswith(prefix) or v <= 0:
            continue
        pp = module.price_at(datetime.strptime(key, '%Y-%m-%dT%H'), tariff)
        for c in VARIABLE_KEYS:
            var[c] += v * pp.components[c]
        kwh += v
        confs.append(pp.confidence)
    note = None
    if not confs:
        note = 'brak danych poboru dla zakresu'
        mid = date(ref.year, ref.month, 15) if scope != 'day' else ref
        confs.append(module.price_at(datetime(mid.year, mid.month, mid.day, 12), tariff).confidence)
    fixed = {k: 0.0 for k in FIXED_KEYS}
    for d, w in fixed_days:
        for k, v in module.fixed(d, tariff).items():
            fixed[k] += v * w
    weight = sum(w for _, w in fixed_days)
    rates = {k: (var[k] / kwh if kwh else None) for k in VARIABLE_KEYS}
    rates.update({k: fixed[k] / weight for k in FIXED_KEYS})
    return {'kwh': kwh, 'confidence': _weakest(confs), 'var': var, 'fixed': fixed, 'rates': rates,
            'note': note}


def _assemble(core: dict, include: tuple, basis: str, vat: float) -> dict:
    comps = []
    total_net = 0.0
    vat_on = basis == 'gross' and 'vat' in include
    factor = vat if (basis == 'net' or vat_on) else 1.0     # wyłączony VAT = brutto bez VAT
    for key in VARIABLE_KEYS + (FIXED_KEYS if core['fixed'] else ()):
        fixed = key in FIXED_KEYS
        net = core['fixed' if fixed else 'var'][key]
        inc = key in include
        total_net += net if inc else 0.0
        comps.append({'key': key, 'label': LABELS[key], 'kind': 'fixed' if fixed else 'variable',
                      'unit': UNIT_FIX if fixed else UNIT_VAR, 'rate_net': _round(core['rates'][key]),
                      'net': _round(net), 'gross': _round(net * factor), 'included': inc})
    total_gross = total_net * factor
    if basis == 'gross':
        comps.append({'key': 'vat', 'label': LABELS['vat'], 'kind': 'vat', 'unit': 'zł', 'rate_net': None,
                      'net': 0.0, 'gross': _round(total_gross - total_net), 'included': vat_on})
    kwh = core['kwh']
    return {'kwh': _round(kwh), 'confidence': core['confidence'], 'components': comps,
            'total_net': _round(total_net), 'total_gross': _round(total_gross),
            'per_kwh_gross': _round(total_gross / kwh) if kwh else None}


def breakdown(module, import_by_hour: dict, invoices: Optional[dict], *, tariff=None, at=None, day=None,
              month=None, invoice=None, include=None, basis=None) -> dict:
    given = [(n, v) for n, v in (('at', at), ('day', day), ('month', month), ('invoice', invoice)) if v]
    if len(given) != 1:
        raise ValueError('podaj dokładnie jeden z parametrów: at, day, month, invoice')
    name, value = given[0]
    inc, basis = parse_include(include), parse_basis(basis)
    if name == 'at':
        scope, target = 'hour', _hour(value)
        label, ref = value, target.date()
    elif name == 'day':
        scope, target = 'day', _day(value, 'day')
        label, ref = value, target
    elif name == 'month':
        scope, target = 'month', _month(value)
        label, ref = value, date(target[0], target[1], 1)
    else:
        rec = (invoices or {}).get(value)
        if not isinstance(rec, dict) or not _YM.match(value[:7]):
            raise ValueError(f'nieznana faktura: {value}')
        scope, target = 'invoice', _month(value[:7], 'invoice')
        num = rec.get('invoice_number')
        label = f'faktura {num}' if num else f'faktura {value}'
        ref = date(target[0], target[1], 1)
    t = check_tariff(module.catalog, tariff) if tariff else module.tariff_on(ref)
    try:
        core = _core(module, import_by_hour or {}, t, scope, target)
    except LookupError as exc:
        raise ValueError(str(exc)) from None
    out = {'ok': True, 'scope': scope, 'label': label, 'tariff': t, 'basis': basis, 'vat': module.vat}
    out.update(_assemble(core, inc, basis, module.vat))
    if core['note']:
        out['note'] = core['note']
    return out


# ── porównania ───────────────────────────────────────────────────────────────

def _scope_target(scope: str, at: str):
    if scope == 'hour':
        dt = _hour(at)
        return dt, dt.date()
    if scope == 'day':
        d = _day(at, 'at')
        return d, d
    if scope == 'month':
        y, m = _month(at, 'at')
        return (y, m), date(y, m, 15)
    if scope == 'year':
        if not at or not re.match(r'^\d{4}$', at):
            raise ValueError('at musi mieć format RRRR')
        return int(at), date(int(at), 6, 15)
    raise ValueError('scope musi być hour, day, month albo year')


def zone_rates(module, tariff: str, day: date, include: tuple, basis: str) -> list:
    """Stawki zmienne per strefa taryfy w dniu `day` (sumy włączonych składników; brak danych → gross None)."""
    period = module._period_info(day)[0]
    out = []
    for zone in (period.get('energy_net') or {}).get(tariff, {}):
        comps = {}
        try:
            comps['energy'] = tc._zone_value(period['energy_net'], tariff, zone, 'energy_net')
            comps['dist'] = tc._zone_value(period['dist_var_net'], tariff, zone, 'dist_var_net')
            for k, f in _SCALAR_FIELDS.items():
                comps[k] = tc._scalar(period, f)
        except ValueError:
            out.append({'zone': zone, 'level': None, 'gross': None, 'components': {}})
            continue
        net = sum(v for k, v in comps.items() if k in include)
        factor = module.vat if (basis == 'net' or 'vat' in include) else 1.0
        out.append({'zone': zone, 'level': module._level(tariff, period, zone), 'gross': _round(net * factor),
                    'components': {k: _round(v) for k, v in comps.items() if k in include}})
    return out


def compare_tariff(module, import_by_hour: dict, *, a, b, scope, at, include=None, basis=None) -> dict:
    if not a or not b:
        raise ValueError('podaj taryfy a i b')
    check_tariff(module.catalog, a)
    check_tariff(module.catalog, b)
    inc, basis = parse_include(include), parse_basis(basis)
    target, ref = _scope_target(scope, at)
    sides, zones = {}, {}
    try:
        for name, t in (('a', a), ('b', b)):
            core = _core(module, import_by_hour or {}, t, scope, target)
            side = _assemble(core, inc, basis, module.vat)
            side['tariff'] = t
            side['total'] = side['total_net'] if basis == 'net' else side['total_gross']
            sides[name] = {k: side[k] for k in ('tariff', 'kwh', 'components', 'total')}
            sides[name]['confidence'] = side['confidence']
            zones[name] = zone_rates(module, t, ref, inc, basis)
    except LookupError as exc:
        raise ValueError(str(exc)) from None
    diff = sides['a']['total'] - sides['b']['total']
    pct = 100.0 * diff / sides['b']['total'] if sides['b']['total'] else None
    return {'ok': True, 'mode': 'tariff', 'scope': scope, 'at': at, 'include': list(inc), 'basis': basis,
            'a': sides['a'], 'b': sides['b'], 'diff': _round(diff), 'diff_pct': _round(pct), 'zones': zones}


def compare_period(module, *, tariff, zone=None, date_from=None, date_to=None,
                   include=None, basis=None) -> dict:
    cat = module.catalog
    t = check_tariff(cat, tariff or module.default_tariff)
    inc, basis = parse_include(include), parse_basis(basis)
    d_from = _day(date_from, 'from').isoformat() if date_from else None
    d_to = _day(date_to, 'to').isoformat() if date_to else None
    if d_from and d_to and d_to < d_from:
        raise ValueError('to nie może być przed from')
    periods = []
    for entry in sorted(cat['periods'], key=lambda p: p['valid_from']):
        p = tc._expand(cat, entry)
        if (d_to and p['valid_from'] > d_to) or (d_from and p['valid_to'] < d_from):
            continue
        zones = list((p.get('energy_net') or {}).get(t, {}))
        if zone is None and zones:
            zone = zones[0]
        if zone is not None and zones and zone not in zones:
            raise ValueError(f'nieznana strefa {zone} w taryfie {t}')
        comps = {}
        for key, z, v in _period_values(p, t, module.settings):
            if key in inc and (z is None or z == zone):
                comps[key] = {'net': _round(v), 'gross': _round(None if v is None else v * module.vat)}
        periods.append({'from': p['valid_from'], 'to': p['valid_to'],
                        'confidence': _catalog_confidence(module, p, t), 'components': comps})
    return {'ok': True, 'mode': 'period', 'tariff': t, 'zone': zone, 'basis': basis, 'periods': periods}


_INVOICE_ROWS = (
    ('energy_peak_net', 'energy', 'peak', 'net'), ('energy_offpeak_net', 'energy', 'offpeak', 'net'),
    ('dist_var_peak_net', 'dist', 'peak', 'net'), ('dist_var_offpeak_net', 'dist', 'offpeak', 'net'),
    ('dist_jakosciowa_net', 'quality', None, 'net'), ('dist_oze_net', 'oze', None, 'net'),
    ('dist_kogeneracja_net', 'cogeneration', None, 'net'), ('fixed_total_net', 'fixed_total', None, 'net'),
    ('peak_gross', 'price_gross', 'peak', 'gross'), ('offpeak_gross', 'price_gross', 'offpeak', 'gross'),
)
_EXTRA_LABELS = {'fixed_total': 'Opłaty stałe razem', 'price_gross': 'Cena łączna brutto'}


def compare_invoice(module, invoices: Optional[dict], *, month) -> dict:
    """Dokument (cennik modułu) ↔ faktura składnik po składniku; ta sama logika co `check_invoices` (T3)."""
    y, m = _month(month)
    rec = (invoices or {}).get(month)
    base = {'ok': True, 'mode': 'invoice', 'month': month, 'tolerance_pct': tic.TOLERANCE_PCT,
            'invoice_no': None, 'status': 'no_invoice', 'rows': []}
    if not isinstance(rec, dict):
        return base
    base['invoice_no'] = rec.get('invoice_number') or None
    check = tic.check_invoices(module, {month: rec}).get(month)
    if check is None:
        base['note'] = 'faktura nieporównywalna z cennikiem (zmiana taryfy w miesiącu albo brak pól)'
        return base
    try:
        legacy = module.legacy_rates(date(y, m, 15))
    except (ValueError, LookupError):
        legacy = {}
    rows = []
    for field, key, zone, kind in _INVOICE_ROWS:
        f = check['fields'].get(field)
        doc = f['module'] if f else legacy.get(field)
        row = {'key': key, 'label': LABELS.get(key) or _EXTRA_LABELS[key], 'zone': zone, 'basis': kind,
               'doc_net': _round(doc), 'invoice_net': None, 'diff_pct': None, 'status': 'missing'}
        if f:
            row.update(invoice_net=_round(f['invoice']), diff_pct=_round(f['diff_pct']),
                       status='ok' if f['ok'] else 'diff')
        rows.append(row)
    base.update(rows=rows, status='match' if check['ok'] else 'diff', tariff=check['tariff'])
    return base
