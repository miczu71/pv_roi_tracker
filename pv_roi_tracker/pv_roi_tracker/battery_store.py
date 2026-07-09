"""
Persystencja symulacji rozbudowy magazynu:
  /data/battery_config.json — parametry wirtualnego modułu (edytowalne w UI)
  /data/battery_sim.json    — cache godzinowych danych eksport/import z LTS

Cache pozwala nie pobierać całej historii statystyk HA przy każdym przebiegu —
dociągamy tylko brakującą końcówkę (z zakładką na opóźnienie LTS).
Zapis atomiczny (tmp + rename), wzorzec jak historic_store.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from .battery_sim import BatteryConfig

logger = logging.getLogger(__name__)

CACHE_VERSION = 1


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


# ── Konfiguracja ──────────────────────────────────────────────────────────────

def load_config(path) -> BatteryConfig:
    try:
        with open(path) as f:
            return BatteryConfig.from_dict(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return BatteryConfig()
    except Exception:
        logger.exception('battery_store: nie można wczytać %s — używam domyślnych', path)
        return BatteryConfig()


def save_config(cfg: BatteryConfig, path) -> None:
    _atomic_write(Path(path), cfg.to_dict())
    logger.info('battery_store: zapisano konfigurację do %s', path)


# ── Cache godzinowy ───────────────────────────────────────────────────────────

def load_hours(path) -> dict:
    """Zwraca {'YYYY-MM-DDTHH': (export_kwh, import_kwh)}."""
    try:
        with open(path) as f:
            doc = json.load(f)
        if doc.get('v') != CACHE_VERSION:
            logger.info('battery_store: cache v%s ≠ v%d — odbudowa od zera',
                        doc.get('v'), CACHE_VERSION)
            return {}
        return {k: (float(v[0]), float(v[1])) for k, v in doc.get('hours', {}).items()}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    except Exception:
        logger.exception('battery_store: nie można wczytać %s — odbudowa od zera', path)
        return {}


def save_hours(hours: dict, path) -> None:
    doc = {
        'v': CACHE_VERSION,
        'hours': {k: [round(v[0], 3), round(v[1], 3)] for k, v in hours.items()},
    }
    _atomic_write(Path(path), doc)
    logger.debug('battery_store: zapisano %d godzin do %s', len(hours), path)
