"""Tests for tariff_config.py — persistent tariff override / baseline."""
import json
from datetime import date
from pathlib import Path

import pytest

from pv_roi_tracker import tariff_config as tc


@pytest.fixture
def cfg_path(tmp_path) -> Path:
    return tmp_path / 'tariff_config.json'


def _entry(ef: str, peak: float = 1.23, offpeak: float = 0.63, **rates) -> dict:
    r = {'peak_gross': peak, 'offpeak_gross': offpeak}
    r.update(rates)
    return {'effective_from': ef, 'note': f'TD {ef}', 'rates': r}


# ── load / save / seed ──────────────────────────────────────────────────────

def test_load_missing_returns_empty(cfg_path):
    assert tc.load(cfg_path) == {'tariffs': []}


def test_save_and_load_roundtrip(cfg_path):
    cfg = {'tariffs': [_entry('2026-02')]}
    tc.save(cfg, cfg_path)
    loaded = tc.load(cfg_path)
    assert loaded['tariffs'][0]['effective_from'] == '2026-02'


def test_seed_creates_file(cfg_path):
    tc.seed_if_missing(cfg_path, peak=1.30, offpeak=0.65)
    assert cfg_path.exists()
    data = json.loads(cfg_path.read_text())
    assert data['tariffs'][0]['rates']['peak_gross'] == pytest.approx(1.30, abs=1e-4)


def test_seed_does_not_overwrite(cfg_path):
    tc.seed_if_missing(cfg_path, peak=1.23)
    tc.seed_if_missing(cfg_path, peak=9.99)  # must NOT overwrite
    data = json.loads(cfg_path.read_text())
    assert data['tariffs'][0]['rates']['peak_gross'] == pytest.approx(1.23, abs=1e-4)


# ── upsert / remove / validate ──────────────────────────────────────────────

def test_upsert_adds_entry(cfg_path):
    cfg = tc._empty()
    cfg = tc.upsert_entry(cfg, _entry('2027-01', peak=1.31))
    assert len(cfg['tariffs']) == 1
    assert cfg['tariffs'][0]['effective_from'] == '2027-01'


def test_upsert_deduplicates(cfg_path):
    cfg = tc._empty()
    cfg = tc.upsert_entry(cfg, _entry('2027-01', peak=1.31))
    cfg = tc.upsert_entry(cfg, _entry('2027-01', peak=1.35))
    assert len(cfg['tariffs']) == 1
    assert cfg['tariffs'][0]['rates']['peak_gross'] == pytest.approx(1.35, abs=1e-4)


def test_upsert_sorts_by_effective_from():
    cfg = tc._empty()
    cfg = tc.upsert_entry(cfg, _entry('2028-01'))
    cfg = tc.upsert_entry(cfg, _entry('2026-02'))
    cfg = tc.upsert_entry(cfg, _entry('2027-01'))
    assert [t['effective_from'] for t in cfg['tariffs']] == ['2026-02', '2027-01', '2028-01']


def test_remove_entry():
    cfg = tc._empty()
    cfg = tc.upsert_entry(cfg, _entry('2026-02'))
    cfg = tc.upsert_entry(cfg, _entry('2027-01'))
    cfg = tc.remove_entry(cfg, '2026-02')
    assert len(cfg['tariffs']) == 1
    assert cfg['tariffs'][0]['effective_from'] == '2027-01'


def test_validate_bad_effective_from():
    with pytest.raises(ValueError, match='effective_from'):
        tc.validate_entry({'effective_from': '2027', 'rates': {}})


def test_validate_unknown_rate_key():
    with pytest.raises(ValueError, match='Nieznane'):
        tc.validate_entry({'effective_from': '2027-01', 'rates': {'unknown_key': 1.0}})


def test_validate_negative_rate():
    with pytest.raises(ValueError, match='>='):
        tc.validate_entry({'effective_from': '2027-01', 'rates': {'peak_gross': -1.0}})


# ── current_entry ────────────────────────────────────────────────────────────

def test_current_entry_picks_latest_past(cfg_path):
    cfg = tc._empty()
    cfg = tc.upsert_entry(cfg, _entry('2026-02', peak=1.23))
    cfg = tc.upsert_entry(cfg, _entry('2027-01', peak=1.31))
    cur = tc.current_entry(cfg, date(2027, 3, 15))
    assert cur['effective_from'] == '2027-01'
    assert cur['rates']['peak_gross'] == pytest.approx(1.31, abs=1e-4)


def test_current_entry_ignores_future():
    cfg = tc._empty()
    cfg = tc.upsert_entry(cfg, _entry('2026-02', peak=1.23))
    cfg = tc.upsert_entry(cfg, _entry('2028-01', peak=1.40))  # future
    cur = tc.current_entry(cfg, date(2027, 6, 1))
    assert cur['effective_from'] == '2026-02'


def test_current_entry_none_when_all_future():
    cfg = tc._empty()
    cfg = tc.upsert_entry(cfg, _entry('2099-01'))
    assert tc.current_entry(cfg, date(2027, 1, 1)) is None


def test_current_entry_none_when_empty():
    assert tc.current_entry(tc._empty(), date.today()) is None


# ── override_rates ───────────────────────────────────────────────────────────

def test_override_active_when_newer_than_invoice():
    cfg = tc._empty()
    cfg = tc.upsert_entry(cfg, _entry('2027-01', peak=1.31))
    real = {'2026-12': {}}  # najnowsza faktura grudzień 2026
    ov = tc.override_rates(cfg, real, date(2027, 1, 15))
    assert ov.get('peak_gross') == pytest.approx(1.31, abs=1e-4)


def test_override_inactive_when_invoice_caught_up():
    cfg = tc._empty()
    cfg = tc.upsert_entry(cfg, _entry('2027-01', peak=1.31))
    real = {'2027-01': {}}  # faktura za styczeń 2027 już jest
    ov = tc.override_rates(cfg, real, date(2027, 2, 10))
    assert ov == {}


def test_override_active_when_no_invoices():
    cfg = tc._empty()
    cfg = tc.upsert_entry(cfg, _entry('2026-02', peak=1.23))
    ov = tc.override_rates(cfg, {}, date(2026, 6, 1))
    assert ov.get('peak_gross') == pytest.approx(1.23, abs=1e-4)


def test_override_inactive_when_no_current_entry():
    cfg = tc._empty()
    cfg = tc.upsert_entry(cfg, _entry('2099-01', peak=9.99))  # future
    ov = tc.override_rates(cfg, {}, date(2027, 1, 1))
    assert ov == {}


def test_override_merges_only_provided_fields():
    cfg = tc._empty()
    # Only peak_gross and offpeak_gross set, no fixed_abonament_net
    cfg = tc.upsert_entry(cfg, {'effective_from': '2027-01', 'note': '', 'rates': {
        'peak_gross': 1.35, 'offpeak_gross': 0.68
    }})
    ov = tc.override_rates(cfg, {'2026-12': {}}, date(2027, 1, 15))
    assert set(ov.keys()) == {'peak_gross', 'offpeak_gross'}


# ── latest_invoice_rates priority chain ─────────────────────────────────────

def test_latest_invoice_rates_priority(tmp_path):
    """Baseline < faktura < override."""
    from pv_roi_tracker import web as _web, invoice_store
    from pv_roi_tracker.invoice_parser import InvoiceData
    from dataclasses import replace

    cfg_p = tmp_path / 'tariff_config.json'
    inv_p = tmp_path / 'invoices.json'

    # Seed baseline: peak_gross=1.23
    tc.seed_if_missing(cfg_p, peak=1.23, offpeak=0.63)
    _web.set_tariff_config_path(cfg_p)
    _web.set_invoice_path(inv_p)

    # Brak faktur → baseline
    rates = _web.latest_invoice_rates()
    assert rates.get('peak_gross') == pytest.approx(1.23, abs=1e-4)

    # Wgraj fakturę z peak_gross=1.2304 (grudzień 2026)
    base = InvoiceData(year=2026, month=12, imported_kwh=300, exported_kwh=100,
                       imported_kwh_peak=None, imported_kwh_offpeak=None,
                       exported_kwh_peak=None, exported_kwh_offpeak=None,
                       energy_peak_net=None, energy_offpeak_net=None,
                       dist_var_peak_net=None, dist_var_offpeak_net=None,
                       dist_jakosciowa_net=None, dist_oze_net=None, dist_kogeneracja_net=None,
                       fixed_mocowa_net=None, fixed_abonament_net=None,
                       fixed_stalysieciowy_net=None)
    base = replace(base, peak_gross=1.2304, offpeak_gross=0.6306)
    invoice_store.upsert(base, path=inv_p)

    rates = _web.latest_invoice_rates()
    assert rates.get('peak_gross') == pytest.approx(1.2304, abs=1e-4)

    # Override styczeń 2027 (peak=1.31) → powinien wygrać gdy brak faktury za 2027-01
    cfg = tc.load(cfg_p)
    cfg = tc.upsert_entry(cfg, {'effective_from': '2027-01', 'note': 'TD 2027',
                                 'rates': {'peak_gross': 1.31, 'offpeak_gross': 0.67}})
    tc.save(cfg, cfg_p)

    # Symuluj: dziś = 2027-01-15, najnowsza faktura = 2026-12 → override aktywny
    rates = _web.latest_invoice_rates(_today=date(2027, 1, 15))
    assert rates.get('peak_gross') == pytest.approx(1.31, abs=1e-4)
