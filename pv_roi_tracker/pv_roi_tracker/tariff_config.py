"""
Persistent tariff configuration — lista datowanych wpisów ręcznej taryfy.

Struktura /data/tariff_config.json:
{
  "tariffs": [
    { "effective_from": "2026-02", "note": "TD 2026", "rates": { ... } },
    { "effective_from": "2027-01", "note": "TD 2027", "rates": { ... } }
  ]
}

Logika priorytetu w latest_invoice_rates():
  baseline (current_entry.rates) < faktura < override (gdy current_entry nowsze niż faktura)

Nowy wpis = nowa zmiana taryfy; stare wpisy zostają (historia). Wpisy z przyszłą
effective_from czekają bezczynnie — przydatne do wpisania zmian z wyprzedzeniem.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Dozwolone klucze w rates (musi być zgodne z web._RATE_FIELDS)
_RATE_KEYS = frozenset({
    'energy_peak_net', 'energy_offpeak_net',
    'dist_var_peak_net', 'dist_var_offpeak_net',
    'dist_jakosciowa_net', 'dist_oze_net', 'dist_kogeneracja_net',
    'fixed_mocowa_net', 'fixed_abonament_net', 'fixed_stalysieciowy_net',
    'peak_gross', 'offpeak_gross', 'fixed_total_net',
})

# Wartości seed — używane gdy nie ma żadnego pliku konfiguracyjnego
_SEED_RATES: dict = {
    'peak_gross': 1.23,
    'offpeak_gross': 0.63,
    'dist_jakosciowa_net': 0.0332,
    'dist_oze_net': 0.0073,
    'dist_kogeneracja_net': 0.003,
    'fixed_abonament_net': 4.56,
    'fixed_stalysieciowy_net': 10.86,
    'fixed_mocowa_net': 24.05,
    'fixed_total_net': 39.47,
}


def _empty() -> dict:
    return {'tariffs': []}


def load(path) -> dict:
    """Wczytaj tariff_config.json; zwróć pustą strukturę przy braku/błędzie."""
    try:
        with open(path) as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get('tariffs'), list):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return _empty()


def save(cfg: dict, path) -> None:
    """Atomiczny zapis tariff_config.json (rename z pliku tmp)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f'.tariff_config_{os.getpid()}.tmp'
    try:
        with open(tmp, 'w') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        tmp.rename(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def seed_if_missing(path, peak: float = 1.23, offpeak: float = 0.63) -> None:
    """Utwórz tariff_config.json z jednym wpisem seed, jeśli plik nie istnieje."""
    path = Path(path)
    if path.exists():
        return
    rates = dict(_SEED_RATES)
    rates['peak_gross'] = round(peak, 4)
    rates['offpeak_gross'] = round(offpeak, 4)
    cfg = {
        'tariffs': [
            {
                'effective_from': '2026-02',
                'note': 'Taryfa TD (seed przy instalacji)',
                'rates': rates,
            }
        ]
    }
    save(cfg, path)
    logger.info('tariff_config: plik seed utworzony w %s (peak=%.4f, offpeak=%.4f)',
                path, peak, offpeak)


def validate_entry(entry: dict) -> None:
    """Zgłoś ValueError gdy wpis ma błędny format."""
    ef = entry.get('effective_from', '')
    if not (isinstance(ef, str) and re.match(r'^\d{4}-\d{2}$', ef)):
        raise ValueError(f'effective_from musi być YYYY-MM, otrzymano {ef!r}')
    rates = entry.get('rates', {})
    if not isinstance(rates, dict):
        raise ValueError('rates musi być słownikiem')
    bad_keys = set(rates) - _RATE_KEYS
    if bad_keys:
        raise ValueError(f'Nieznane klucze stawek: {bad_keys}')
    for k, v in rates.items():
        if not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f'{k} musi być liczbą >= 0, otrzymano {v!r}')


def upsert_entry(cfg: dict, entry: dict) -> dict:
    """Dodaj lub zastąp wpis po effective_from. Zwraca nowy cfg (nie mutuje)."""
    validate_entry(entry)
    ef = entry['effective_from']
    tariffs = [t for t in cfg.get('tariffs', []) if t.get('effective_from') != ef]
    tariffs.append(dict(entry))
    tariffs.sort(key=lambda t: t.get('effective_from', ''))
    return {**cfg, 'tariffs': tariffs}


def remove_entry(cfg: dict, effective_from: str) -> dict:
    """Usuń wpis po effective_from. Zwraca nowy cfg (nie mutuje)."""
    tariffs = [t for t in cfg.get('tariffs', []) if t.get('effective_from') != effective_from]
    return {**cfg, 'tariffs': tariffs}


def current_entry(cfg: dict, today: date) -> Optional[dict]:
    """Zwróć najnowszy wpis z effective_from <= dziś (YYYY-MM).
    None, gdy takich wpisów brak (np. wszystkie są z przyszłością)."""
    today_ym = today.strftime('%Y-%m')
    past = [t for t in cfg.get('tariffs', [])
            if isinstance(t.get('effective_from'), str)
            and t['effective_from'] <= today_ym]
    if not past:
        return None
    return max(past, key=lambda t: t.get('effective_from', ''))


def effective_baseline(cfg: dict, today: date) -> dict:
    """Kumulatywny baseline — scalenie rates wszystkich wpisów ≤ dziś rosnąco.

    Późniejszy wpis nadpisuje wcześniejszy tylko dla kluczy, które sam zawiera.
    Skutek: wpis 2027-01 z samym peak_gross/offpeak_gross dziedziczy fixed_*/dist_*
    z wpisu 2026-02 — wystarczy wpisać tylko zmienione pola.

    Zwraca {} gdy brak wpisów (lub wszystkie przyszłe).
    """
    today_ym = today.strftime('%Y-%m')
    past = sorted(
        [t for t in cfg.get('tariffs', [])
         if isinstance(t.get('effective_from'), str)
         and t['effective_from'] <= today_ym],
        key=lambda t: t.get('effective_from', ''),
    )
    merged: dict = {}
    for entry in past:
        merged.update(entry.get('rates', {}))
    return merged


def override_rates(cfg: dict, real: dict, today: date) -> dict:
    """Zwróć stawki z current_entry GDY jest NOWSZY niż najnowsza faktura
    (= ogłoszona taryfa wypełnia lukę, zanim faktura nadejdzie).
    Zwraca {} gdy faktura już nadgoniła lub brak current_entry.

    `real` to pre-filtered dict billing invoice keys (filter_billing output).
    """
    cur = current_entry(cfg, today)
    if cur is None:
        return {}
    ef = cur.get('effective_from', '')
    if not real:
        # Brak faktur → każdy aktualny wpis traktuj jako override (= baseline)
        return dict(cur.get('rates', {}))
    max_inv = max(real)  # YYYY-MM najnowszej faktury rozliczeniowej
    if ef > max_inv:
        # Ogłoszona taryfa jest nowsza niż faktura → aktywny override
        return dict(cur.get('rates', {}))
    # Faktura nadgoniła → prymat faktury, override wygasa
    return {}
