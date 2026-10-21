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

from . import tariff_battery
from .battery_store import _atomic_write
from .tariff_catalog import TariffSettings
from .tariff_sim import ALL_TARIFFS as TARIFFS, simulate_range

logger = logging.getLogger(__name__)

RECONCILE_TOLERANCE_PCT = 3.0
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


def _sum12_b(months: dict, tariff: str, yms: list) -> Optional[float]:
    total = 0.0
    for ym in yms:
        entry = (months.get(ym) or {}).get(tariff)
        b = entry.get('b') if _usable(entry) else None
        if not b:
            return None
        total += float(b['gross'])
    return total


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
            hours, battery_cfg, catalog, rdn, kompas, rcem_by_ym or {}, from_ym, to_ym))

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
    if with_b:
        gross_b = {t: _sum12_b(months, t, yms) for t in TARIFFS}
        ranked_b = {t: g for t, g in gross_b.items() if g is not None}
        cheapest_b = min(ranked_b, key=ranked_b.get) if ranked_b else None
        saving_b = None
        if cheapest_b is not None and gross_b.get('G12w') is not None:
            saving_b = max(0.0, gross_b['G12w'] - ranked_b[cheapest_b])
        l12_extra = {'gross_b': gross_b, 'cheapest_b': cheapest_b,
                     'saving_vs_g12w_b': saving_b}
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
        'settings': {'phases': settings.phases, 'capacity_bracket': settings.capacity_bracket,
                     'sales_fee_net': settings.sales_fee_net},
    }
    if with_b:
        payload['current_month']['gross_b'] = current_b
        payload['variant_b_note'] = ('Wariant B: gross_b = gross − oszczędność modułu magazynu '
                                     '(bez ceny modułu; zwrot w zakładce Magazyn)')
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


def recommend(payload: Optional[dict], threshold_pln: float) -> dict:
    """Rekomendacja dla wariantów A i B z `last12` (czysta funkcja); brak danych → None."""
    l12 = (payload or {}).get('last12') or {}
    return {'A': _recommend_one(l12.get('gross'), threshold_pln),
            'B': _recommend_one(l12.get('gross_b'), threshold_pln)}


def load_payload(path) -> Optional[dict]:
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_payload(payload: dict, path) -> None:
    _atomic_write(Path(path), payload)
