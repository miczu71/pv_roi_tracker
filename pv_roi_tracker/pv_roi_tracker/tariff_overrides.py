"""
Ręczne korekty katalogu taryf (/data/tariff_overrides.json) — zastępują dawny `tariff_config`.

Plik to lista wpisów
    {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"|null, "note": "...", "patch": {fragment okresu katalogu}}
nakładanych przez `TariffModule` na okres katalogu w dniach `from`..`to`. Stawki bazowe daje
katalog (`tariffs/tauron.json`); tu żyją tylko różnice (wpisy z umowy/faktury).

Zewnętrznie (API, `latest_invoice_rates`) korekty nadal wyglądają jak dawne 13-polowe wpisy
(`entry_to_patch` / `patch_to_rates`). `migrate_legacy` jednorazowo przenosi stary
`tariff_config.json`.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import date
from pathlib import Path

from .tariff_history import _quarantine

logger = logging.getLogger(__name__)

# 13 pól dawnego wpisu (zgodne z web._RATE_FIELDS)
_RATE_ORDER = (
    'energy_peak_net', 'energy_offpeak_net', 'dist_var_peak_net', 'dist_var_offpeak_net',
    'dist_jakosciowa_net', 'dist_oze_net', 'dist_kogeneracja_net',
    'fixed_mocowa_net', 'fixed_abonament_net', 'fixed_stalysieciowy_net',
    'peak_gross', 'offpeak_gross', 'fixed_total_net',
)
RATE_KEYS = frozenset(_RATE_ORDER)
_PATCH_KEYS = frozenset({
    'energy_net', 'dist_var_net', 'quality_net', 'oze_net', 'cogeneration_net',
    'excise_net', 'fixed_net', 'dynamic_dist_var_net',
})
_FIXED_FIELDS = ('fixed_mocowa_net', 'fixed_abonament_net', 'fixed_stalysieciowy_net', 'fixed_total_net')
_EPS = 1e-9


# ── walidacja ────────────────────────────────────────────────────────────────

def _check_number_tree(value, label: str) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _check_number_tree(v, f'{label}.{k}')
    elif isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f'{label} musi być liczbą >= 0, otrzymano {value!r}')


def _iso(value, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f'{label} musi mieć format RRRR-MM-DD, otrzymano {value!r}') from None


def validate(overrides) -> list:
    """Zgłoś ValueError (po polsku) gdy lista korekt ma błędny schemat; zwraca ją bez zmian."""
    if not isinstance(overrides, list):
        raise ValueError('oczekiwano listy korekt')
    for i, e in enumerate(overrides, 1):
        if not isinstance(e, dict):
            raise ValueError(f'korekta {i}: oczekiwano obiektu')
        start = _iso(e.get('from'), f'korekta {i}: from')
        end = e.get('to')
        if end is not None and _iso(end, f'korekta {i}: to') < start:
            raise ValueError(f'korekta {i}: to nie może być przed from')
        note = e.get('note', '')
        if note is not None and not isinstance(note, str):
            raise ValueError(f'korekta {i}: note musi być tekstem')
        patch = e.get('patch')
        if not isinstance(patch, dict):
            raise ValueError(f'korekta {i}: patch musi być obiektem')
        bad = set(patch) - _PATCH_KEYS
        if bad:
            raise ValueError(f'korekta {i}: nieznane klucze patch: {sorted(bad)}')
        _check_number_tree(patch, f'korekta {i}: patch')
    return overrides


def validate_rates_entry(entry: dict) -> None:
    """Walidacja wpisu w starym kształcie `{effective_from: YYYY-MM, note?, rates{}}` (POST /api/tariff_config)."""
    ef = entry.get('effective_from', '')
    if not (isinstance(ef, str) and len(ef) == 7 and ef[4] == '-'
            and ef[:4].isdigit() and ef[5:].isdigit() and 1 <= int(ef[5:]) <= 12):
        raise ValueError(f'effective_from musi być YYYY-MM, otrzymano {ef!r}')
    rates = entry.get('rates', {})
    if not isinstance(rates, dict):
        raise ValueError('rates musi być słownikiem')
    bad_keys = set(rates) - RATE_KEYS
    if bad_keys:
        raise ValueError(f'Nieznane klucze stawek: {bad_keys}')
    for k, v in rates.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f'{k} musi być liczbą >= 0, otrzymano {v!r}')


# ── odczyt / zapis ───────────────────────────────────────────────────────────

def load(path) -> list:
    """Korekty z pliku. Brak pliku → []. Uszkodzony plik/schemat → [] + log ERROR + kopia `.corrupt`."""
    try:
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
        return validate(doc)
    except FileNotFoundError:
        return []
    except Exception:
        logger.error('tariff_overrides: nie można wczytać %s — korekty puste', path, exc_info=True)
        _quarantine(path)
        return []


def save(overrides: list, path) -> None:
    """Atomowy zapis (tmp + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f'.tariff_overrides_{os.getpid()}.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(overrides, f, indent=2, ensure_ascii=False)
        tmp.rename(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def upsert(overrides: list, entry: dict) -> list:
    """Dodaj albo zastąp korektę po `from`. Zwraca nową listę posortowaną po `from` (nie mutuje)."""
    validate([entry])
    out = [e for e in overrides if e.get('from') != entry['from']]
    out.append(dict(entry))
    out.sort(key=lambda e: e['from'])
    return out


def remove(overrides: list, from_: str) -> list:
    """Usuń korektę po `from`. Zwraca nową listę."""
    return [e for e in overrides if e.get('from') != from_]


# ── konwersja: stary wpis 13-polowy <-> fragment okresu ──────────────────────

def _view(module, tariff: str) -> tuple:
    from .tariff_module import _TWO_ZONE_VIEW
    if tariff not in _TWO_ZONE_VIEW:
        raise ValueError(f'{tariff}: brak widoku dwustrefowego (peak/offpeak)')
    return _TWO_ZONE_VIEW[tariff]


def _catalog_only(module):
    """Ten sam moduł, ale bez korekt — źródło składników katalogu."""
    from .tariff_module import TariffModule
    return TariffModule(module.catalog, module.periods, [], module.default_tariff, module.settings)


def entry_to_patch(rates: dict, module, day: date, tariff: str = 'G12w') -> dict:
    """Zamienia 13 pól dawnego wpisu na fragment okresu katalogu.

    Składniki netto wygrywają z `peak_gross`/`offpeak_gross`; samo gross daje energię
    `gross/vat − dist − fees` (dist i fees z okresu katalogu lub z tych samych pól wpisu).
    `fixed_total_net` jest pochodne i jest ignorowane.
    """
    zones = _view(module, tariff)
    settings = module.settings
    patch: dict = {}

    def put(path: tuple, value) -> None:
        node = patch
        for k in path[:-1]:
            node = node.setdefault(k, {})
        node[path[-1]] = value

    base = _catalog_only(module)._period_info(day)[0]
    fees = {}
    for field, key in (('dist_jakosciowa_net', 'quality_net'), ('dist_oze_net', 'oze_net'),
                       ('dist_kogeneracja_net', 'cogeneration_net')):
        if rates.get(field) is not None:
            put((key,), float(rates[field]))
            fees[key] = float(rates[field])
    fee_sum = (fees.get('quality_net', base['quality_net']) + fees.get('oze_net', base['oze_net'])
               + fees.get('cogeneration_net', base['cogeneration_net']) + base['excise_net'])

    for label, zone in (('peak', zones[0]), ('offpeak', zones[1])):
        energy, dist = rates.get(f'energy_{label}_net'), rates.get(f'dist_var_{label}_net')
        if dist is not None:
            put(('dist_var_net', tariff, zone), float(dist))
        gross = rates.get(f'{label}_gross')
        if energy is not None:
            put(('energy_net', tariff, zone), float(energy))
        elif gross is not None:
            dist_v = float(dist) if dist is not None else float(base['dist_var_net'][tariff][zone])
            energy_v = round(float(gross) / module.vat - dist_v - fee_sum, 6)
            if energy_v < 0:
                raise ValueError(f'{label}_gross {gross} jest za niskie: sama dystrybucja i opłaty '
                                 f'to {(dist_v + fee_sum) * module.vat:.4f} zł brutto')
            put(('energy_net', tariff, zone), energy_v)

    if rates.get('fixed_mocowa_net') is not None:
        put(('fixed_net', 'capacity_by_annual_kwh', settings.capacity_bracket),
            float(rates['fixed_mocowa_net']))
    if rates.get('fixed_abonament_net') is not None:
        put(('fixed_net', 'subscription_dist'), float(rates['fixed_abonament_net']))
    if rates.get('fixed_stalysieciowy_net') is not None:
        put(('fixed_net', 'network_3ph' if settings.phases == 3 else 'network_1ph'),
            float(rates['fixed_stalysieciowy_net']))
    return patch


def patch_to_rates(patch_module, base_module, day: date) -> dict:
    """Pola 13-polowe, które różnią się między modułem z korektą a bez niej (|Δ| > 1e-9)."""
    with_p, without = patch_module.legacy_rates(day), base_module.legacy_rates(day)
    return {k: with_p[k] for k in _RATE_ORDER if abs(with_p[k] - without[k]) > _EPS}


# ── migracja ze starego tariff_config.json ───────────────────────────────────

def _close_to_catalog(with_p: dict, without: dict) -> bool:
    for k in ('peak_gross', 'offpeak_gross'):
        if abs(with_p[k] - without[k]) > 0.005:
            return False
    return all(abs(with_p[k] - without[k]) <= 0.01 for k in _FIXED_FIELDS)


def migrate_legacy(config_path, overrides_path, module) -> None:
    """Jednorazowo: `tariff_config.json` → `tariff_overrides.json`.

    Działa tylko gdy `overrides_path` nie istnieje, a `config_path` istnieje. Kopia starego pliku
    w `<config_path>.pre-tariff-module`; wpisy zbliżone do katalogu (seed instalacji) są pomijane.
    Zapisuje `overrides_path` także gdy wynik jest pusty (migracja nie powtarza się).
    """
    from .tariff_module import TariffModule, _TWO_ZONE_VIEW
    overrides_path, config_path = Path(overrides_path), Path(config_path)
    if overrides_path.exists() or not config_path.exists():
        return
    backup = Path(str(config_path) + '.pre-tariff-module')
    shutil.copy2(config_path, backup)
    try:
        with open(config_path, encoding='utf-8') as f:
            legacy = json.load(f)
        entries = legacy.get('tariffs', []) if isinstance(legacy, dict) else []
    except Exception:
        logger.error('tariff_overrides: nie można wczytać %s do migracji — korekty puste (kopia: %s)',
                     config_path, backup, exc_info=True)
        entries = []
    base = _catalog_only(module)
    out: list = []
    for entry in sorted((e for e in entries if isinstance(e, dict)),
                        key=lambda e: str(e.get('effective_from', ''))):
        try:
            ef = entry['effective_from']
            day = date(int(ef[:4]), int(ef[5:7]), 1)
            rates = {k: v for k, v in (entry.get('rates') or {}).items() if k in RATE_KEYS}
            tariff = base.tariff_on(day)
            tariff = tariff if tariff in _TWO_ZONE_VIEW else 'G12w'
            patch = entry_to_patch(rates, module, day, tariff)
            cand = {'from': day.isoformat(), 'to': None, 'note': str(entry.get('note') or ''),
                    'patch': patch}
            probe = TariffModule(module.catalog, module.periods, [cand], module.default_tariff,
                                 module.settings)
            if _close_to_catalog(probe.legacy_rates(day, tariff), base.legacy_rates(day, tariff)):
                continue
            out.append(cand)
        except Exception:
            logger.warning('tariff_overrides: pominięto wpis %r przy migracji (kopia: %s)',
                           entry.get('effective_from'), backup, exc_info=True)
    save(out, overrides_path)
    logger.info('tariff_overrides: zmigrowano %d z %d wpisów do %s', len(out), len(entries), overrides_path)
