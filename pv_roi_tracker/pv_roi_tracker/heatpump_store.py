"""
Persystencja danych godzinowych pompy ciepła dla zakładki „Pompa ciepła":
  /data/heatpump_hours.json — cache godzinowego bilansu (patrz heatpump.py)

Wzorzec identyczny jak battery_store.py: cache pozwala nie pobierać całej
historii statystyk HA przy każdym przebiegu — dociągamy tylko brakującą
końcówkę. Zapis atomiczny (tmp + rename).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_VERSION = 1

_KEYS = ('produced', 'exported', 'imported', 'batt_charge', 'batt_discharge',
         'hp_kwh', 'heat_h', 'dhw_h', 't_out')


def _atomic_write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_hours(path) -> dict:
    """Zwraca {'YYYY-MM-DDTHH': {produced,exported,imported,batt_charge,
    batt_discharge,hp_kwh,heat_h,dhw_h,t_out}} (t_out może być None)."""
    try:
        with open(path) as f:
            doc = json.load(f)
        if doc.get('v') != CACHE_VERSION:
            logger.info('heatpump_store: cache v%s ≠ v%d — odbudowa od zera',
                        doc.get('v'), CACHE_VERSION)
            return {}
        return {k: dict(v) for k, v in doc.get('hours', {}).items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception:
        logger.exception('heatpump_store: nie można wczytać %s — odbudowa od zera', path)
        return {}


def save_hours(hours: dict, path) -> None:
    rounded = {}
    for k, v in hours.items():
        row = {}
        for key in _KEYS:
            val = v.get(key)
            row[key] = round(val, 4) if isinstance(val, (int, float)) else val
        rounded[k] = row
    doc = {'v': CACHE_VERSION, 'hours': rounded}
    _atomic_write(Path(path), doc)
    logger.debug('heatpump_store: zapisano %d godzin do %s', len(hours), path)
