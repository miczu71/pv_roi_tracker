"""
Zadanie symulacji taryf: składa wynik `tariff_sim` w jeden payload dla UI/MQTT.

Czysta logika — bez I/O poza `load_payload` / `save_payload`. Pobór godzinowy,
katalog, ceny RDN/Kompas i faktury podaje wywołujący (main.py).
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from . import tariff_battery, tariff_grid_charge
from .battery_store import _atomic_write
from .tariff_catalog import TariffSettings
from .tariff_sim import ALL_TARIFFS as TARIFFS, simulate_range

logger = logging.getLogger(__name__)

RECONCILE_TOLERANCE_PCT = 3.0
RECOMMEND_VARIANTS = (('A', 'gross'), ('B', 'gross_b'), ('C', 'gross_c'))
BASELINE_TARIFF = 'G12w'
_INVOICE_NET_FIELDS = (
    'energy_amount_net', 'dist_var_amount_net', 'dist_jakosciowa_amount_net',
    'dist_oze_amount_net', 'dist_kogeneracja_amount_net', 'fixed_total_net',
)


def import_series(hours: dict) -> dict:
    """`{klucz: (export, import)}` (cache battery_sim) → `{klucz: import_kWh}`."""
    out: dict = {}
    for key, val in hours.items():
        try:
            kwh = val[1]
        except (TypeError, IndexError, KeyError):
            continue
        if kwh is not None:
            out[key] = float(kwh)
    return out


def _ym(d: date) -> str:
    return f'{d.year:04d}-{d.month:02d}'


def _shift(ym: str, months: int) -> str:
    idx = int(ym[:4]) * 12 + int(ym[5:7]) - 1 + months
    return f'{idx // 12:04d}-{idx % 12 + 1:02d}'


def _usable(entry: Optional[dict], need_reliable: bool = True) -> bool:
    """Taryfa dostępna (i wiarygodna); `need_reliable=False` dla bieżącego, częściowego miesiąca."""
    return bool(entry) and entry.get('available', True) is not False \
        and (not need_reliable or entry.get('reliable', True) is not False)


def _sum12(months: dict, tariff: str, yms: list, field: str) -> Optional[float]:
    total = 0.0
    for ym in yms:
        entry = (months.get(ym) or {}).get(tariff)
        if not _usable(entry) or entry.get(field) is None:
            return None
        total += float(entry[field])
    return total


def _sum12_variant(months: dict, tariff: str, yms: list, key: str) -> Optional[float]:
    """Suma 12 mies. `gross` wariantu (`entry['b']`/`entry['c']`); brak miesiąca → None."""
    total = 0.0
    for ym in yms:
        entry = (months.get(ym) or {}).get(tariff)
        v = entry.get(key) if _usable(entry) else None
        if not v:
            return None
        total += float(v['gross'])
    return total


def _attach_variant_c(months: dict, c_monthly: dict) -> None:
    """Dokłada `entry['c']` (gross = gross − oszczędność planu C) lub None (nie-GRID, brak wyniku)."""
    for ym, tmap in months.items():
        for t, entry in tmap.items():
            res = (c_monthly.get(ym) or {}).get(t) if t in tariff_grid_charge.GRID_TARIFFS else None
            if not _usable(entry, False) or not res or entry.get('gross') is None:
                entry['c'] = None
                continue
            entry['c'] = {**res, 'gross': entry['gross'] - res['savings_pln']}


def _best_year(months: dict, yms: list, gross: dict, gross_b: Optional[dict],
               gross_c: Optional[dict]) -> Optional[dict]:
    """Ranking taryfa × wariant w skali 12 mies. względem G12w wariant A; brak baseline → None."""
    base = gross.get(BASELINE_TARIFF)
    if base is None:
        return None

    def confirmed(t: str) -> bool:
        return all(((months.get(ym) or {}).get(t) or {}).get('confirmed', True) is not False
                   for ym in yms)

    rows = []
    for variant, values in (('A', gross), ('B', gross_b), ('C', gross_c)):
        for t, g in (values or {}).items():
            if g is not None:
                rows.append({'tariff': t, 'variant': variant, 'gross': g,
                             'saving_pln': base - g, 'confirmed': confirmed(t)})
    rows.sort(key=lambda r: r['gross'])
    best = rows[0] if rows else None
    warnings = []
    unconfirmed = sorted({r['tariff'] for r in rows if not r['confirmed']})
    if unconfirmed:
        warnings.append(f"Cennik niepotwierdzony: {', '.join(unconfirmed)} — wynik orientacyjny")
    if best and best['tariff'] != BASELINE_TARIFF:
        warnings.append('Zmiana grupy taryfowej jest możliwa raz na 12 miesięcy')
    return {'baseline': {'tariff': BASELINE_TARIFF, 'variant': 'A', 'gross': base},
            'rows': rows, 'best': best, 'warnings': warnings}


def _attach_variant_b(months: dict, b_res: dict) -> None:
    """Dokłada `entry['b']` (gross_b = gross − oszczędność modułu) lub None."""
    for ym, tmap in months.items():
        for t, entry in tmap.items():
            res = (b_res.get(ym) or {}).get(t)
            if not _usable(entry, False) or not res or entry.get('gross') is None:
                entry['b'] = None
                continue
            entry['b'] = {
                'savings_pln': res['savings_pln'],
                'gross': entry['gross'] - res['savings_pln'],
                'charged_kwh': res['charged_kwh'],
                'discharged_kwh': res['discharged_kwh'],
            }


def _invoice_net(inv: dict) -> Optional[float]:
    vals = [inv.get(f) for f in _INVOICE_NET_FIELDS]
    if any(v is None for v in vals):
        return None
    return float(sum(vals))


def _reconcile(months: dict, invoices: dict) -> dict:
    out: dict = {}
    for key in sorted(invoices):
        ym = key[:7]
        inv = invoices[key]
        sim = (months.get(ym) or {}).get('G12w')
        if not sim or sim.get('available', True) is False or not isinstance(inv, dict):
            continue
        inv_net = _invoice_net(inv)
        if inv_net is None or inv_net == 0:
            continue
        diff = 100.0 * (sim['net'] - inv_net) / inv_net
        out[ym] = {
            'sim_net': sim['net'], 'invoice_net': inv_net,
            'diff_pct': diff, 'ok': abs(diff) <= RECONCILE_TOLERANCE_PCT,
            'kwh_sim': sim.get('kwh'), 'kwh_invoice': inv.get('imported_kwh'),
            'confirmed': bool(sim.get('confirmed', True)),
        }
    return out


def compute_payload(import_by_hour: dict, catalog: dict, settings: Optional[TariffSettings],
                    rdn: Optional[dict], kompas: Optional[dict], invoices: Optional[dict],
                    today: date, from_ym: str = '2024-12', hours: Optional[dict] = None,
                    battery_cfg=None, rcem_by_ym: Optional[dict] = None) -> dict:
    settings = settings or TariffSettings()
    to_ym = _ym(today)
    # po ostatnim okresie cennika nie zgadujemy stawek: liczymy do jego końca i oznaczamy
    cat_to = max((e['valid_to'] for e in catalog['periods'] if e.get('valid_to')), default=None)
    catalog_ends = None
    if cat_to and to_ym > cat_to[:7]:
        to_ym = catalog_ends = cat_to[:7]
    # katalog nie sięga przed pierwszy okres cennika — wcześniejsze miesiące pomijamy
    cat_from = min(e['valid_from'] for e in catalog['periods'])[:7]
    from_ym = max(from_ym, cat_from)
    months = simulate_range(from_ym, to_ym, import_by_hour, catalog, settings, rdn, kompas)

    with_b = bool(hours) and battery_cfg is not None
    if with_b:
        _attach_variant_b(months, tariff_battery.simulate_variant_b(
            hours, battery_cfg, catalog, rdn, kompas, rcem_by_ym or {}, from_ym, to_ym, soc_floor=tariff_grid_charge.SOC_FLOOR))
        _attach_variant_c(months, tariff_grid_charge.simulate_variant_c(
            hours, battery_cfg, catalog, rdn, kompas, rcem_by_ym or {}, from_ym, to_ym))
        plans = tariff_grid_charge.suggest_plans(
            hours, battery_cfg, catalog, rdn, kompas, rcem_by_ym or {}, from_ym, to_ym)

    cur = months.get(to_ym) or {}
    current = {t: (cur[t]['gross'] if _usable(cur.get(t), False) else None) for t in TARIFFS}

    l12_to = _shift(to_ym, -1)
    l12_from = _shift(to_ym, -12)
    yms = [_shift(l12_from, i) for i in range(12)]
    gross = {t: _sum12(months, t, yms, 'gross') for t in TARIFFS}
    net = {t: _sum12(months, t, yms, 'net') for t in TARIFFS}
    ranked = {t: g for t, g in gross.items() if g is not None}
    cheapest = min(ranked, key=ranked.get) if ranked else None
    saving = None
    if cheapest is not None and gross.get('G12w') is not None:
        saving = max(0.0, gross['G12w'] - ranked[cheapest])

    l12_extra: dict = {}
    gross_b = gross_c = None
    if with_b:
        gross_b = {t: _sum12_variant(months, t, yms, 'b') for t in TARIFFS}
        ranked_b = {t: g for t, g in gross_b.items() if g is not None}
        cheapest_b = min(ranked_b, key=ranked_b.get) if ranked_b else None
        saving_b = None
        if cheapest_b is not None and gross_b.get('G12w') is not None:
            saving_b = max(0.0, gross_b['G12w'] - ranked_b[cheapest_b])
        l12_extra = {'gross_b': gross_b, 'cheapest_b': cheapest_b,
                     'saving_vs_g12w_b': saving_b}
        gross_c = {t: _sum12_variant(months, t, yms, 'c') for t in TARIFFS}
        ranked_c = {t: g for t, g in gross_c.items() if g is not None}
        cheapest_c = min(ranked_c, key=ranked_c.get) if ranked_c else None
        saving_c = None
        if cheapest_c is not None and gross_c.get('G12w') is not None:
            saving_c = max(0.0, gross_c['G12w'] - ranked_c[cheapest_c])
        l12_extra.update({'gross_c': gross_c, 'cheapest_c': cheapest_c,
                          'saving_vs_g12w_c': saving_c})
        current_c = {t: (cur[t]['c']['gross'] if _usable(cur.get(t), False) and cur[t].get('c')
                         else None) for t in TARIFFS}
        current_b = {t: (cur[t]['b']['gross'] if _usable(cur.get(t), False) and cur[t].get('b')
                         else None) for t in TARIFFS}

    recon = _reconcile(months, invoices or {})
    confirmed = [r for r in recon.values() if r['confirmed']]
    diffs = [abs(r['diff_pct']) for r in confirmed]
    payload = {
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'from': from_ym, 'to': to_ym, 'catalog_ends': catalog_ends,
        'tariffs': list(TARIFFS),
        'months': months,
        'current_month': {'ym': to_ym, 'gross': current},
        'last12': {'from': l12_from, 'to': l12_to, 'gross': gross, 'net': net,
                   'cheapest': cheapest, 'saving_vs_g12w': saving, **l12_extra},
        'reconciliation': recon,
        'reconciliation_summary': {
            'months': len(confirmed),
            'ok_months': sum(1 for r in confirmed if r['ok']),
            'unconfirmed_months': len(recon) - len(confirmed),
            'max_abs_diff_pct': max(diffs) if diffs else None,
        },
        'best_year': _best_year(months, yms, gross, gross_b, gross_c),
        'settings': {'phases': settings.phases, 'capacity_bracket': settings.capacity_bracket,
                     'sales_fee_net': settings.sales_fee_net,
                     'sales_fee_g13s_net': settings.sales_fee_g13s_net},
    }
    if with_b:
        payload['current_month']['gross_b'] = current_b
        payload['current_month']['gross_c'] = current_c
        payload['plans'] = plans
        payload['variant_b_note'] = ('Wariant B: gross_b = gross − oszczędność modułu magazynu '
                                     '(bez ceny modułu; zwrot w zakładce Magazyn; SoC ≥ 16%)')
        payload['variant_c_note'] = ('Wariant C to pułap: zna dobę z góry i planuje ładowanie z sieci. '
                                     'SoC dolny 16%, zakres 16→100%; do 2 cykli na dobę; '
                                     'SoC przenoszony między dobami; w miesiącu liczony lepszy z planowania '
                                     'i polityki B; ceny G13s są niepotwierdzone')
    return payload


def _recommend_one(gross: Optional[dict], threshold_pln: float) -> Optional[dict]:
    if not gross or gross.get('G12w') is None:
        return None
    ranked = {t: g for t, g in gross.items() if g is not None}
    best = min(ranked, key=ranked.get)
    saving = gross['G12w'] - ranked[best]
    switch = best != 'G12w' and saving >= threshold_pln
    if best == 'G12w':
        reason = 'G12w jest najtańsza'
    elif switch:
        reason = f'zmiana opłaca się: {saving:.0f} zł/rok'
    else:
        reason = f'oszczędność {saving:.0f} zł poniżej progu {threshold_pln:.0f} zł'
    return {'best': best, 'g12w': gross['G12w'], 'saving_pln': saving,
            'switch': switch, 'reason': reason}


def _recommend_best_year(best_year: Optional[dict], threshold_pln: float) -> Optional[dict]:
    best = (best_year or {}).get('best')
    if not best:
        return None
    saving = best['saving_pln']
    change = best['tariff'] != BASELINE_TARIFF
    above = saving >= threshold_pln
    label = f"{best['tariff']} {best['variant']}"
    if saving <= 0:
        reason = f'{label} jest najtańsza — bez zmian'
    elif above:
        reason = f'{label} oszczędza {saving:.0f} zł/rok względem G12w A'
    else:
        reason = f'{label}: oszczędność {saving:.0f} zł poniżej progu {threshold_pln:.0f} zł'
    return {'best': best, 'saving_pln': saving, 'tariff_change': change,
            'above_threshold': above, 'reason': reason}


def recommend(payload: Optional[dict], threshold_pln: float) -> dict:
    """Rekomendacja dla wariantów A/B/C z `last12` i `best_year` (czysta funkcja); brak danych → None."""
    p = payload or {}
    l12 = p.get('last12') or {}
    out = {v: _recommend_one(l12.get(f), threshold_pln) for v, f in RECOMMEND_VARIANTS}
    out['best_year'] = _recommend_best_year(p.get('best_year'), threshold_pln)
    return out


def load_payload(path) -> Optional[dict]:
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_payload(payload: dict, path) -> None:
    _atomic_write(Path(path), payload)
