"""
Symulacja kosztu tego samego poboru z sieci (import) w kilku taryfach (wariant A).

Taryfy: G11, G12, G12w, G13 (stałe strefy), dyn_a (RDN + marża Tauron, średnia ważona miesięczna z podłogą, dystrybucja G12w; od 2026-04)
i dyn_b (stała cena oferty „Prąd ze zmienną dystrybucją” + dystrybucja G14dynamic, strefy S1–S4 z Kompasu PSE).

Koszt godziny netto = kWh × (energia + dystrybucja zmienna + opłaty zmienne). Godzina z poborem,
dla której brakuje ceny RDN / strefy S, NIE jest liczona po cichu jako 0 — jej kWh trafiają do
`missing_kwh`, a `coverage_pct` pokazuje pokrycie (poniżej 95% taryfa dynamiczna jest
oznaczana `reliable: false`). Eksport (net-billing) jest taki sam w każdej taryfie i leży
poza symulacją.
"""
from __future__ import annotations

import calendar
import logging
from datetime import date, datetime, timedelta
from typing import Optional

from .tariff_catalog import (
    TariffSettings,
    fixed_monthly,
    period_for,
    rates_per_kwh,
    zone_for,
)

logger = logging.getLogger(__name__)

STATIC_TARIFFS = ('G11', 'G12', 'G12w', 'G13')
ALL_TARIFFS = STATIC_TARIFFS + ('dyn_a', 'dyn_b')
RELIABLE_MIN_COVERAGE_PCT = 95.0


def _parse_key(key: str) -> datetime:
    return datetime.strptime(key, '%Y-%m-%dT%H')


def _required(sales: dict, key: str) -> float:
    """Pole `dynamic_sales`; brak/null to błąd — nigdy cicho zero (reguła katalogu)."""
    v = sales.get(key)
    if v is None:
        raise ValueError(f'dyn_a: brak {key} w katalogu')
    return float(v)


def _dyn_b_price(catalog: dict, overrides: Optional[dict]) -> float:
    """Stała cena oferty dyn_b (netto zł/kWh, jednakowa w S1–S4); `overrides` ma pierwszeństwo."""
    ov = ((overrides or {}).get('dynamic_sales_b') or {}).get('fixed_price_net_per_kwh')
    if ov is not None:
        return float(ov)
    v = (catalog.get('dynamic_sales_b') or {}).get('fixed_price_net_per_kwh')
    if v is None:
        raise ValueError('dyn_b: brak fixed_price_net_per_kwh w katalogu')
    return float(v)


def simulate_month(ym: str, import_by_hour: dict, catalog: dict,
                   settings: Optional[TariffSettings] = None,
                   rdn: Optional[dict] = None, kompas: Optional[dict] = None,
                   overrides: Optional[dict] = None) -> dict:
    """Koszt poboru `import_by_hour` ({YYYY-MM-DDTHH: kWh}) w miesiącu `ym` w każdej taryfie."""
    settings = settings or TariffSettings()
    rdn = rdn or {}
    kompas = kompas or {}
    vat = float(catalog['_meta']['vat'])
    sales = catalog.get('dynamic_sales', {})
    a_from = sales.get('valid_from')
    a_available = not (a_from and ym < a_from[:7])
    margin = _required(sales, 'margin_net_per_kwh') if a_available else 0.0
    min_avg = _required(sales, 'min_avg_price_net_per_kwh') if a_available else 0.0
    a_sum_price_kwh = 0.0      # Σ (cena_h × kWh_h), cena = RDN/1000 + marża
    a_covered = 0.0
    a_fallback = 0.0

    b_from = (catalog.get('dynamic_sales_b') or {}).get('valid_from')
    b_available = not (b_from and ym < b_from[:7])
    names = ALL_TARIFFS
    acc = {n: {'kwh': 0.0, 'energy': 0.0, 'dist': 0.0, 'fees': 0.0, 'missing': 0.0} for n in names}
    confirmed = {n: True for n in names}
    periods: dict = {}

    def period_of(d: date) -> dict:
        p = periods.get(d)
        if p is None:
            p = periods[d] = period_for(catalog, d, overrides)
        return p

    for key in sorted(k for k in import_by_hour if k.startswith(ym)):
        kwh = float(import_by_hour[key])
        if kwh <= 0:
            continue
        dt = _parse_key(key)
        p = period_of(dt.date())
        for n in names:
            acc[n]['kwh'] += kwh
            if not p['confirmed']:
                confirmed[n] = False

        for n in STATIC_TARIFFS:
            r = rates_per_kwh(n, p, zone_for(n, dt, catalog))
            for f in ('energy', 'dist', 'fees'):
                acc[n][f] += kwh * r[f]

        g12w = rates_per_kwh('G12w', p, zone_for('G12w', dt, catalog))

        if a_available:
            price = rdn.get(key)
            if price is None:
                # cennik: brak notowania → notowanie z analogicznej godziny poprzedniego tygodnia
                price = rdn.get((dt - timedelta(days=7)).strftime('%Y-%m-%dT%H'))
                if price is not None:
                    a_fallback += kwh
            if price is None:
                acc['dyn_a']['missing'] += kwh
            else:
                a_sum_price_kwh += kwh * (price / 1000.0 + margin)
                a_covered += kwh
                acc['dyn_a']['dist'] += kwh * g12w['dist']
                acc['dyn_a']['fees'] += kwh * g12w['fees']

        zone = kompas.get(key)
        s_rate = (p.get('dynamic_dist_var_net') or {}).get(zone) if zone else None
        if not b_available:
            pass
        elif s_rate is None:
            acc['dyn_b']['missing'] += kwh
        else:
            acc['dyn_b']['energy'] += kwh * _dyn_b_price(catalog, overrides)
            acc['dyn_b']['dist'] += kwh * float(s_rate)
            acc['dyn_b']['fees'] += kwh * g12w['fees']

    if a_covered > 0:
        # średnia ważona cena netto za miesiąc (5 miejsc); podłoga dotyczy średniej, nie godzin
        avg = round(a_sum_price_kwh / a_covered, 5)
        acc['dyn_a']['energy'] = a_covered * max(avg, min_avg)

    first = period_of(date(int(ym[:4]), int(ym[5:7]), 1))
    fixed = sum(fixed_monthly(first, settings).values())
    confirmed_first = bool(first['confirmed'])

    out: dict = {}
    for n in names:
        if n == 'dyn_b' and not b_available:
            out[n] = {'available': False, 'reason': f'oferta od {b_from}'}
            continue
        if n == 'dyn_a' and not a_available:
            out[n] = {'available': False,
                      'reason': 'cennik dynamiczny od 2026-04-01; wcześniejsze wersje nieustalone'}
            continue
        a = acc[n]
        net = a['energy'] + a['dist'] + a['fees'] + fixed
        gross = net * vat
        coverage = 100.0 * (a['kwh'] - a['missing']) / a['kwh'] if a['kwh'] > 0 else 100.0
        res = {
            'kwh': a['kwh'], 'energy': a['energy'], 'dist': a['dist'], 'fees': a['fees'],
            'fixed': fixed, 'net': net, 'vat': gross - net, 'gross': gross,
            'confirmed': confirmed[n] and confirmed_first,
            'coverage_pct': coverage, 'missing_kwh': a['missing'],
        }
        if n == 'dyn_b':
            res['confirmed'] = res['confirmed'] and bool(
                (catalog.get('dynamic_sales_b') or {}).get('confirmed', False))
        if n == 'dyn_a':
            res['confirmed'] = res['confirmed'] and bool(sales.get('confirmed', False))
            res['price_fallback_kwh'] = a_fallback
        if n in ('dyn_a', 'dyn_b'):
            res['reliable'] = coverage >= RELIABLE_MIN_COVERAGE_PCT
        out[n] = res
    return out


def _month_range(from_ym: str, to_ym: str):
    y, m = int(from_ym[:4]), int(from_ym[5:7])
    ty, tm = int(to_ym[:4]), int(to_ym[5:7])
    while (y, m) <= (ty, tm):
        yield f'{y:04d}-{m:02d}'
        m += 1
        if m > 12:
            y, m = y + 1, 1


def simulate_range(from_ym: str, to_ym: str, import_by_hour: dict, catalog: dict,
                   settings: Optional[TariffSettings] = None,
                   rdn: Optional[dict] = None, kompas: Optional[dict] = None,
                   overrides: Optional[dict] = None) -> dict:
    """`{ym: simulate_month(...)}` dla miesięcy od `from_ym` do `to_ym` włącznie."""
    return {ym: simulate_month(ym, import_by_hour, catalog, settings, rdn, kompas, overrides)
            for ym in _month_range(from_ym, to_ym)}


# ── seria godzinowa (E1: widok „Strefy i ceny”) ──────────────────────────────

def hourly_series(from_date: date, to_date: date, import_by_hour: dict, catalog: dict,
                  tariffs=ALL_TARIFFS, settings: Optional[TariffSettings] = None,
                  rdn: Optional[dict] = None, kompas: Optional[dict] = None,
                  overrides: Optional[dict] = None) -> dict:
    """Wiersz na KAŻDĄ godzinę doby od `from_date` do `to_date` (także bez poboru, kWh = 0).

    `{taryfa: {'available', 'reason', 'hours': [wiersz, ...]}}`, wiersz:
    `{key, zone, net_per_kwh, gross_per_kwh, kwh, cost_net, cost_gross, fallback}`.
    DST jak w `simulate_month`: klucz `YYYY-MM-DDTHH`, jeden wiersz na klucz.

    Różnice względem `simulate_month`: dyn_a liczy cenę każdej godziny osobno (RDN/1000 + marża
    + dystrybucja G12w + opłaty) — BEZ miesięcznej podłogi średniej; brak RDN → ta sama godzina
    tydzień wcześniej (`fallback: True`), nadal brak → ceny i koszty `None` (zone podana).
    Taryfa dynamiczna jest niedostępna (`available: False`, `hours: []`), gdy cały zakres leży
    przed jej `valid_from`; dni przed `valid_from` w szerszym zakresie mają ceny `None`.
    Brak wartości w katalogu → ValueError, nigdy cichy zero.
    """
    rdn = rdn or {}
    kompas = kompas or {}
    vat = float(catalog['_meta']['vat'])
    sales = catalog.get('dynamic_sales', {})
    a_from = sales.get('valid_from')
    b_from = (catalog.get('dynamic_sales_b') or {}).get('valid_from')
    out: dict = {}
    for n in tariffs:
        if n == 'dyn_a' and a_from and to_date.isoformat() < a_from:
            out[n] = {'available': False, 'reason': 'cennik dynamiczny od 2026-04-01; wcześniejsze wersje nieustalone',
                      'hours': []}
            continue
        if n == 'dyn_b' and b_from and to_date.isoformat() < b_from:
            out[n] = {'available': False, 'reason': f'oferta od {b_from}', 'hours': []}
            continue
        out[n] = {'available': True, 'reason': None, 'hours': []}

    margin = _required(sales, 'margin_net_per_kwh') if out.get('dyn_a', {}).get('available') else 0.0
    d = from_date
    while d <= to_date:
        iso = d.isoformat()
        p = period_for(catalog, d, overrides)
        for h in range(24):
            key = f'{iso}T{h:02d}'
            dt = datetime(d.year, d.month, d.day, h)
            kwh = float(import_by_hour.get(key) or 0.0)
            for n in tariffs:
                if not out[n]['available']:
                    continue
                fallback = False
                net: Optional[float]
                if n in STATIC_TARIFFS:
                    zone = zone_for(n, dt, catalog)
                    r = rates_per_kwh(n, p, zone)
                    net = r['energy'] + r['dist'] + r['fees']
                elif n == 'dyn_a':
                    zone = zone_for('G12w', dt, catalog)
                    net = None
                    if not (a_from and iso < a_from):
                        price = rdn.get(key)
                        if price is None:
                            price = rdn.get((dt - timedelta(days=7)).strftime('%Y-%m-%dT%H'))
                            fallback = price is not None
                        if price is not None:
                            g12w = rates_per_kwh('G12w', p, zone)
                            net = price / 1000.0 + margin + g12w['dist'] + g12w['fees']
                else:   # dyn_b
                    zone = kompas.get(key)
                    net = None
                    s_rate = (p.get('dynamic_dist_var_net') or {}).get(zone) if zone else None
                    if s_rate is not None and not (b_from and iso < b_from):
                        fees = rates_per_kwh('G12w', p, zone_for('G12w', dt, catalog))['fees']
                        net = _dyn_b_price(catalog, overrides) + float(s_rate) + fees
                gross = net * vat if net is not None else None
                out[n]['hours'].append({
                    'key': key, 'zone': zone, 'net_per_kwh': net, 'gross_per_kwh': gross,
                    'kwh': kwh,
                    'cost_net': kwh * net if net is not None else None,
                    'cost_gross': kwh * gross if gross is not None else None,
                    'fallback': fallback,
                })
        d += timedelta(days=1)
    return out


def zone_map(series: dict) -> dict:
    """`{taryfa: {'days': [...], 'cells': {dzień: [24 × {zone, net_per_kwh, gross_per_kwh}]}}}`."""
    out: dict = {}
    for n, s in series.items():
        cells: dict = {}
        for r in s.get('hours', []):
            day, h = r['key'][:10], int(r['key'][11:13])
            row = cells.setdefault(day, [None] * 24)
            row[h] = {'zone': r['zone'], 'net_per_kwh': r['net_per_kwh'],
                      'gross_per_kwh': r['gross_per_kwh']}     # DST: ostatni zapis klucza wygrywa
        out[n] = {'days': sorted(cells), 'cells': cells}
    return out


def cost_by_zone(series: dict, catalog: dict, settings: Optional[TariffSettings] = None,
                 overrides: Optional[dict] = None) -> dict:
    """Koszt per strefa + opłaty stałe (proporcjonalnie do dni) + `missing_kwh` (godziny bez ceny)."""
    settings = settings or TariffSettings()
    vat = float(catalog['_meta']['vat'])
    out: dict = {}
    for n, s in series.items():
        if not s.get('available'):
            continue
        zones: dict = {}
        missing = 0.0
        days = set()
        for r in s['hours']:
            days.add(r['key'][:10])
            if r['cost_net'] is None:
                missing += r['kwh']
                continue
            z = zones.setdefault(r['zone'], {'kwh': 0.0, 'cost_net': 0.0, 'cost_gross': 0.0})
            z['kwh'] += r['kwh']
            z['cost_net'] += r['cost_net']
            z['cost_gross'] += r['cost_gross']
        fixed_net = 0.0
        for iso in days:
            d = date.fromisoformat(iso)
            monthly = sum(fixed_monthly(period_for(catalog, d, overrides), settings).values())
            fixed_net += monthly / calendar.monthrange(d.year, d.month)[1]
        fixed_gross = fixed_net * vat
        out[n] = {'zones': zones, 'fixed_net': fixed_net, 'fixed_gross': fixed_gross,
                  'missing_kwh': missing,
                  'total_gross': sum(z['cost_gross'] for z in zones.values()) + fixed_gross}
    return out


def cumulative_diff(series: dict, a: str, b: str) -> dict:
    """Narastająca suma dziennych `cost_gross[a] − cost_gross[b]` (bez opłat stałych).

    Godziny, w których cena którejś taryfy jest `None`, są pomijane i liczone w `skipped_kwh`.
    """
    sa, sb = series.get(a), series.get(b)
    if not sa or not sb or not sa.get('available') or not sb.get('available'):
        return {'days': [], 'diff_gross': [], 'skipped_kwh': 0.0}
    by_b = {r['key']: r for r in sb['hours']}
    daily: dict = {}
    skipped = 0.0
    for r in sa['hours']:
        day = r['key'][:10]
        daily.setdefault(day, 0.0)
        rb = by_b.get(r['key'])
        if rb is None or r['cost_gross'] is None or rb['cost_gross'] is None:
            skipped += r['kwh']
            continue
        daily[day] += r['cost_gross'] - rb['cost_gross']
    days = sorted(daily)
    acc = 0.0
    cum = []
    for day in days:
        acc += daily[day]
        cum.append(acc)
    return {'days': days, 'diff_gross': cum, 'skipped_kwh': skipped}
