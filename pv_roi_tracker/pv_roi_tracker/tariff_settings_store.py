"""
Ustawienia symulacji taryf (/data/tariff_settings.json): fazy, moc umowna,
opłata handlowa i próg rekomendacji. Zapis atomowy (tmp + rename).
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .battery_store import _atomic_write
from .tariff_catalog import TariffSettings

logger = logging.getLogger(__name__)

DEFAULTS = {'phases': 3, 'capacity_bracket': 'gt2800', 'sales_fee_net': 0.0,
            'recommend_threshold_pln': 100.0}
BRACKETS = ('lt500', '500_1200', '1200_2800', 'gt2800')


def validate(raw: dict) -> dict:
    """Zwraca pełny słownik ustawień; ValueError (po polsku) przy błędnych wartościach."""
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    if 'phases' in raw:
        try:
            phases = int(raw['phases'])
        except (TypeError, ValueError):
            raise ValueError('liczba faz musi być równa 1 lub 3')
        if phases not in (1, 3):
            raise ValueError('liczba faz musi być równa 1 lub 3')
        out['phases'] = phases
    if 'capacity_bracket' in raw:
        if raw['capacity_bracket'] not in BRACKETS:
            raise ValueError('nieznany próg mocy umownej')
        out['capacity_bracket'] = raw['capacity_bracket']
    for key, lo, hi, label in (
        ('sales_fee_net', 0.0, 200.0, 'opłata handlowa netto musi być w zakresie 0–200 zł/mies.'),
        ('recommend_threshold_pln', 0.0, 5000.0, 'próg rekomendacji musi być w zakresie 0–5000 zł/rok'),
    ):
        if key in raw:
            try:
                val = float(raw[key])
            except (TypeError, ValueError):
                raise ValueError(label)
            if not (lo <= val <= hi):
                raise ValueError(label)
            out[key] = val
    return out


def load(path) -> dict:
    try:
        with open(path, encoding='utf-8') as f:
            return validate(json.load(f))
    except FileNotFoundError:
        return dict(DEFAULTS)
    except Exception:
        logger.warning('tariff_settings_store: nie można wczytać %s — używam domyślnych',
                       path, exc_info=True)
        return dict(DEFAULTS)


def save(settings: dict, path) -> None:
    _atomic_write(Path(path), settings)
    logger.info('tariff_settings_store: zapisano ustawienia do %s', path)


def to_catalog_settings(d: dict) -> TariffSettings:
    return TariffSettings(phases=int(d['phases']), capacity_bracket=d['capacity_bracket'],
                          sales_fee_net=float(d['sales_fee_net']))
