"""
Historia taryf (/data/tariff_periods.json): okresy obowiązywania taryf wykryte z portalu
moj.tauron.pl (EbokClient.read_contract) oraz ręczny override z opcji add-onu.
Zapis atomowy (tmp + rename), jak tariff_settings_store.
"""
from __future__ import annotations

import json
import logging
import shutil
from datetime import date
from pathlib import Path
from typing import Optional

from .battery_store import _atomic_write
from .tariff_catalog import TARIFFS

logger = logging.getLogger(__name__)

_BY_FOLD = {t.casefold(): t for t in TARIFFS}


def normalize(name) -> Optional[str]:
    """Nazwa taryfy ze stałej TARIFFS (bez względu na wielkość liter) albo None."""
    if not isinstance(name, str):
        return None
    return _BY_FOLD.get(name.strip().casefold())


def _valid(item) -> Optional[dict]:
    if not isinstance(item, dict):
        return None
    tariff = normalize(item.get('tariff'))
    start = item.get('from')
    if tariff is None:
        return None
    if start is not None:
        try:
            date.fromisoformat(start)
        except (TypeError, ValueError):
            return None
    return {'tariff': tariff, 'from': start}


def validate_periods(raw) -> list:
    """Walidacja okresów z UI: niepusta lista `{tariff, from}`; taryfa → zapis kanoniczny;
    `from` null tylko w pierwszym wpisie, reszta ISO `YYYY-MM-DD` ściśle rosnąco."""
    if not isinstance(raw, list) or not raw:
        raise ValueError('oczekiwano niepustej listy okresów')
    out: list = []
    prev = None
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f'okres {i + 1}: oczekiwano obiektu {{tariff, from}}')
        tariff = normalize(item.get('tariff'))
        if tariff is None:
            raise ValueError(f'okres {i + 1}: nieznana taryfa')
        if 'from' not in item:
            raise ValueError(f'okres {i + 1}: brak pola from')
        start = item['from']
        if start is None:
            if i > 0:
                raise ValueError(f'okres {i + 1}: data początku jest wymagana')
        else:
            try:
                day = date.fromisoformat(start)
            except (TypeError, ValueError):
                raise ValueError(f'okres {i + 1}: data musi mieć format RRRR-MM-DD') from None
            if prev is not None and day <= prev:
                raise ValueError(f'okres {i + 1}: daty muszą rosnąć')
            prev = day
        out.append({'tariff': tariff, 'from': start})
    return out


def _quarantine(path) -> None:
    """Kopia uszkodzonego pliku do `<plik>.corrupt` — tylko gdy kopii jeszcze nie ma (nie nadpisuje)."""
    corrupt = Path(str(path) + '.corrupt')
    if corrupt.exists():
        return
    try:
        shutil.copy2(path, corrupt)
    except Exception:
        logger.error('tariff_history: nie można skopiować uszkodzonego %s do %s', path, corrupt, exc_info=True)


def load_status(path) -> tuple:
    """(okresy, uszkodzony). Brak pliku → ([], False). Każda inna wada (nie JSON, nie lista,
    niepoprawny wpis) → ([], True) i kopia pliku do `.corrupt`."""
    try:
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
    except FileNotFoundError:
        return [], False
    except Exception:
        logger.error('tariff_history: nie można wczytać %s — historia pusta', path, exc_info=True)
        _quarantine(path)
        return [], True
    if not isinstance(doc, list):
        logger.error('tariff_history: %s nie jest listą — historia pusta', path)
        _quarantine(path)
        return [], True
    periods = [_valid(item) for item in doc]
    if any(p is None for p in periods):
        logger.error('tariff_history: niepoprawny wpis w %s — historia pusta', path)
        _quarantine(path)
        return [], True
    return periods, False


def load(path) -> list:
    """Okresy z pliku; brak pliku albo uszkodzona zawartość → []."""
    return load_status(path)[0]


def save(periods: list, path) -> None:
    _atomic_write(Path(path), periods)
    logger.info('tariff_history: zapisano %d okres(y) do %s', len(periods), path)


def current(periods: list, override=None) -> Optional[str]:
    """Override (jeśli poprawna nazwa) wygrywa; inaczej taryfa ostatniego okresu; inaczej None."""
    forced = normalize(override)
    if forced:
        return forced
    if periods:
        return normalize(periods[-1].get('tariff'))
    return None


def tariff_on(periods: list, day: date) -> Optional[str]:
    """Taryfa obowiązująca w dniu: ostatni okres z `from` <= day (`from` null = od zawsze)."""
    found = None
    for p in periods:
        start = p.get('from')
        if start is None or date.fromisoformat(start) <= day:
            found = p.get('tariff')
    return found


def record_detection(periods: list, detected, today: date) -> tuple:
    """Dopisuje wykrytą taryfę, gdy różni się od ostatniej. Zwraca (historia, zmiana),
    zmiana = (poprzednia, nowa) albo None. Nieznana/None wykryta taryfa — bez zmian."""
    name = normalize(detected)
    if name is None:
        return periods, None
    if not periods:
        return [{'tariff': name, 'from': None}], None
    last = normalize(periods[-1].get('tariff'))
    if name == last:
        return periods, None
    new = [*periods, {'tariff': name, 'from': today.isoformat()}]
    return new, (last, name)


def with_override(periods: list, override, today: date) -> list:
    """Historia z ręcznym override z opcji add-onu. Poprawny override różny od taryfy ostatniego
    okresu → dopisuje okres od `today` (baza: `periods`, a pusta historia → [G12w od zawsze]).
    Brak/niepoprawny override albo ten sam co ostatni okres → `periods` bez zmian."""
    forced = normalize(override)
    if forced is None:
        return periods
    last = normalize(periods[-1].get('tariff')) if periods else None
    if forced == last:
        return periods
    base = list(periods) if periods else [{'tariff': 'G12w', 'from': None}]
    return [*base, {'tariff': forced, 'from': today.isoformat()}]
