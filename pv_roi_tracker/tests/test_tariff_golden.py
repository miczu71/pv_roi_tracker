"""
Test złoty modułu taryf (T0). Zamraża wyniki sprzed scalenia źródeł taryf:

* warstwa katalogu: strefa i stawki netto każdej godziny 2025-01…2026-12 dla wszystkich taryf
  (odcisk SHA-1 per taryfa i miesiąc + czytelna tabela stawek per okres i strefa),
* warstwa pieniędzy: `effective_baseline` / `override_rates` dla konfiguracji z żywego add-onu
  (jeden wpis seed 2026-02: 1,23 / 0,63 zł) w każdym miesiącu 2025-01…2026-12.

Każda późniejsza implementacja musi dać ten sam zrzut. Świadome różnice wolno wpisać wyłącznie
do `ALLOWED_DIFFS` razem z uzasadnieniem. Odświeżenie wzorca: `GOLDEN_UPDATE=1 pytest <ten plik>`.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from pv_roi_tracker import tariff_catalog as tc
from pv_roi_tracker import tariff_overrides as tovr
from pv_roi_tracker import web
from pv_roi_tracker.tariff_module import TariffModule

FIXTURE = Path(__file__).parent / 'fixtures' / 'tariff_golden.json'
START, END = date(2025, 1, 1), date(2026, 12, 31)

# Konfiguracja z żywego add-onu (odczyt 2026-10-29): jedyny wpis to seed instalacji.
LIVE_CONFIG = {'tariffs': [{
    'effective_from': '2026-02',
    'note': 'Taryfa TD (seed przy instalacji)',
    'rates': {
        'dist_jakosciowa_net': 0.0332, 'dist_kogeneracja_net': 0.003, 'dist_oze_net': 0.0073,
        'fixed_abonament_net': 4.56, 'fixed_mocowa_net': 24.05, 'fixed_stalysieciowy_net': 10.86,
        'fixed_total_net': 39.47, 'offpeak_gross': 0.63, 'peak_gross': 1.23,
    },
}]}

# klucz "<warstwa>.<ścieżka>" → powód (pusty słownik = żadnych świadomych różnic)
ALLOWED_DIFFS: dict[str, str] = {}


def _months():
    d = START.replace(day=1)
    while d <= END:
        yield d
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)


def catalog_snapshot(catalog: dict | None = None) -> dict:
    cat = catalog or tc.load_catalog()
    vat = float(cat['_meta']['vat'])
    fingerprints: dict = {}
    for tariff in tc.TARIFFS:
        for m in _months():
            h = hashlib.sha1()
            d = m
            while d.month == m.month and d <= END:
                period = tc.period_for(cat, d)
                for hour in range(24):
                    dt = datetime(d.year, d.month, d.day, hour)
                    zone = tc.zone_for(tariff, dt, cat)
                    try:
                        r = tc.rates_per_kwh(tariff, period, zone)
                        net = f'{r["energy"] + r["dist"] + r["fees"]:.6f}'
                    except ValueError as exc:         # null/brak stawki też jest wynikiem do zamrożenia
                        net = f'ERR:{exc}'
                    h.update(f'{d.isoformat()}T{hour:02d}|{zone}|{net}\n'.encode())
                d += timedelta(days=1)
            fingerprints[f'{tariff}|{m.strftime("%Y-%m")}'] = h.hexdigest()
    rates: dict = {}
    for entry in cat['periods']:
        period = tc.period_for(cat, date.fromisoformat(entry['valid_from']))
        for tariff in tc.TARIFFS:
            for zone in _zones_of(cat, tariff):
                try:
                    r = tc.rates_per_kwh(tariff, period, zone)
                    rates[f'{entry["valid_from"]}|{tariff}|{zone}'] = {
                        'net': round(r['energy'] + r['dist'] + r['fees'], 6),
                        'gross': round((r['energy'] + r['dist'] + r['fees']) * vat, 6),
                    }
                except ValueError as exc:
                    rates[f'{entry["valid_from"]}|{tariff}|{zone}'] = f'ERR:{exc}'
    return {'fingerprints': fingerprints, 'rates': rates}


def _zones_of(cat: dict, tariff: str) -> list:
    """Wszystkie nazwy stref, jakie zwraca zone_for dla taryfy (z próbkowania całego roku)."""
    seen: list = []
    d = date(2026, 1, 1)
    while d.year == 2026:
        for hour in (0, 3, 7, 9, 11, 15, 17, 19, 21):
            z = tc.zone_for(tariff, datetime(d.year, d.month, d.day, hour), cat)
            if z not in seen:
                seen.append(z)
        d += timedelta(days=1)
    return sorted(seen)


def _migrated_module(cfg: dict, tmp_dir: Path) -> TariffModule:
    """Moduł z katalogiem i korektami zmigrowanymi ze starej konfiguracji (jak przy starcie add-onu)."""
    cfg_p, ov_p = tmp_dir / 'tariff_config.json', tmp_dir / 'tariff_overrides.json'
    cfg_p.write_text(json.dumps(cfg), encoding='utf-8')
    catalog = tc.load_catalog()
    tovr.migrate_legacy(cfg_p, ov_p, TariffModule(catalog))
    return TariffModule(catalog, overrides=tovr.load(ov_p))


def money_snapshot(cfg: dict | None = None) -> dict:
    import tempfile
    cfg = LIVE_CONFIG if cfg is None else cfg
    with tempfile.TemporaryDirectory() as tmp:
        module = _migrated_module(cfg, Path(tmp))
        out: dict = {}
        for m in _months():
            day = m.replace(day=28)
            base = web._catalog_layer_rates(module, day)
            out[m.strftime('%Y-%m')] = {
                'baseline': {k: base[k] for k in sorted(base)},
                'override_no_invoice': web._override_layer_rates(module, {}, day),
                'override_invoice_2026_09': web._override_layer_rates(module, {'2026-09': {}}, day),
            }
    return out


def snapshot() -> dict:
    return {'catalog': catalog_snapshot(), 'money': money_snapshot()}


def _flatten(prefix: str, value) -> dict:
    if isinstance(value, dict):
        out: dict = {}
        for k, v in value.items():
            out.update(_flatten(f'{prefix}.{k}' if prefix else str(k), v))
        return out
    return {prefix: value}


def diff(expected: dict, actual: dict) -> dict:
    e, a = _flatten('', expected), _flatten('', actual)
    return {k: (e.get(k), a.get(k)) for k in sorted(set(e) | set(a)) if e.get(k) != a.get(k)}


def test_golden_fixture_matches():
    actual = snapshot()
    if os.environ.get('GOLDEN_UPDATE'):
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(actual, indent=1, sort_keys=True, ensure_ascii=False) + '\n',
                           encoding='utf-8')
    expected = json.loads(FIXTURE.read_text(encoding='utf-8'))
    unexpected = {k: v for k, v in diff(expected, actual).items() if k not in ALLOWED_DIFFS}
    assert not unexpected, f'{len(unexpected)} różnic poza ALLOWED_DIFFS, np.: {list(unexpected.items())[:5]}'


def test_live_g12w_price_matches_ha_sensor():
    """Ceny G12w z katalogu = to, co dziś mają sensor HA i stałe Heiko (1,2304 / 0,6306)."""
    cat = tc.load_catalog()
    p = tc.period_for(cat, date(2026, 10, 29))
    vat = float(cat['_meta']['vat'])
    for zone, gross in (('peak', 1.2304), ('offpeak', 0.6306)):
        r = tc.rates_per_kwh('G12w', p, zone)
        assert round((r['energy'] + r['dist'] + r['fees']) * vat, 4) == gross


def test_tariff_module_agrees_with_catalog_hour_by_hour():
    """Moduł (T1) = katalog: ta sama strefa i stawka netto w każdej godzinie 2025–2026 (co 3. dzień)."""
    from pv_roi_tracker.tariff_module import TariffModule
    cat = tc.load_catalog()
    module = TariffModule(cat)
    d, n = START, 0
    while d <= END:
        if d.toordinal() % 3 == 0:
            period = tc.period_for(cat, d)
            for tariff in tc.TARIFFS:
                for hour in range(24):
                    dt = datetime(d.year, d.month, d.day, hour)
                    zone = tc.zone_for(tariff, dt, cat)
                    try:
                        r = tc.rates_per_kwh(tariff, period, zone)
                    except ValueError:                       # luka w katalogu → moduł też ma zgłosić
                        with pytest.raises(ValueError):
                            module.price_at(dt, tariff)
                        continue
                    p = module.price_at(dt, tariff)
                    assert p.zone == zone, (tariff, dt)
                    assert abs(p.net - (r['energy'] + r['dist'] + r['fees'])) < 1e-9, (tariff, dt)
                    n += 1
        d += timedelta(days=1)
    assert n > 20000
