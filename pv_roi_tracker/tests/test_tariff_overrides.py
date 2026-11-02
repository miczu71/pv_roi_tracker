"""Testy tariff_overrides.py — ręczne korekty katalogu taryf (zastępują tariff_config)."""
from __future__ import annotations

import json
from datetime import date

import pytest

from pv_roi_tracker import tariff_catalog as tc
from pv_roi_tracker import tariff_overrides as to
from pv_roi_tracker.tariff_module import TariffModule, build

DAY = date(2026, 6, 15)


@pytest.fixture
def module():
    return TariffModule(tc.load_catalog())


def _with(module, entry):
    return TariffModule(module.catalog, module.periods, [entry], module.default_tariff, module.settings)


# ── load / save ──────────────────────────────────────────────────────────────

def test_load_missing_file_is_empty(tmp_path):
    assert to.load(tmp_path / 'nope.json') == []


def test_save_load_roundtrip(tmp_path):
    p = tmp_path / 'sub' / 'o.json'
    entry = {'from': '2027-01-01', 'to': None, 'note': 'x', 'patch': {'quality_net': 0.04}}
    to.save([entry], p)
    assert to.load(p) == [entry]
    assert not list(p.parent.glob('.tariff_overrides_*.tmp'))


def test_load_corrupt_file_quarantined(tmp_path, caplog):
    p = tmp_path / 'o.json'
    p.write_text('{nie json', encoding='utf-8')
    with caplog.at_level('ERROR'):
        assert to.load(p) == []
    assert (tmp_path / 'o.json.corrupt').exists()
    assert any(r.levelname == 'ERROR' for r in caplog.records)


def test_load_invalid_schema_quarantined(tmp_path):
    p = tmp_path / 'o.json'
    p.write_text(json.dumps([{'from': 'wczoraj', 'patch': {}}]), encoding='utf-8')
    assert to.load(p) == []
    assert (tmp_path / 'o.json.corrupt').exists()


# ── validate / upsert / remove ───────────────────────────────────────────────

def test_validate_rejects_bad_entries():
    with pytest.raises(ValueError):
        to.validate({'from': '2026-01-01'})                                   # nie lista
    with pytest.raises(ValueError):
        to.validate([{'from': '2026-13-01', 'patch': {}}])                    # zła data
    with pytest.raises(ValueError):
        to.validate([{'from': '2026-05-01', 'to': '2026-04-01', 'patch': {}}])  # to < from
    with pytest.raises(ValueError):
        to.validate([{'from': '2026-05-01', 'patch': {'nieznane': 1}}])       # obcy klucz
    with pytest.raises(ValueError):
        to.validate([{'from': '2026-05-01', 'patch': {'quality_net': -1}}])   # ujemna


def test_upsert_replaces_same_from_and_sorts():
    a = {'from': '2027-01-01', 'to': None, 'note': 'a', 'patch': {'quality_net': 0.1}}
    b = {'from': '2026-03-01', 'to': None, 'note': 'b', 'patch': {'quality_net': 0.2}}
    a2 = {'from': '2027-01-01', 'to': None, 'note': 'a2', 'patch': {'quality_net': 0.3}}
    out = to.upsert(to.upsert([a], b), a2)
    assert [e['from'] for e in out] == ['2026-03-01', '2027-01-01']
    assert out[1]['note'] == 'a2'


def test_remove_by_from():
    a = {'from': '2027-01-01', 'to': None, 'note': '', 'patch': {}}
    assert to.remove([a], '2027-01-01') == []
    assert to.remove([a], '2020-01-01') == [a]


# ── entry_to_patch ───────────────────────────────────────────────────────────

def test_entry_to_patch_components(module):
    rates = {'energy_peak_net': 0.7, 'energy_offpeak_net': 0.4, 'dist_var_peak_net': 0.3,
             'dist_var_offpeak_net': 0.05, 'dist_jakosciowa_net': 0.04, 'dist_oze_net': 0.008,
             'dist_kogeneracja_net': 0.0031, 'fixed_mocowa_net': 20.0, 'fixed_abonament_net': 5.0,
             'fixed_stalysieciowy_net': 11.0, 'fixed_total_net': 999.0,
             'peak_gross': 9.9, 'offpeak_gross': 9.9}
    p = to.entry_to_patch(rates, module, DAY)
    assert p['energy_net'] == {'G12w': {'peak': 0.7, 'offpeak': 0.4}}
    assert p['dist_var_net'] == {'G12w': {'peak': 0.3, 'offpeak': 0.05}}
    assert (p['quality_net'], p['oze_net'], p['cogeneration_net']) == (0.04, 0.008, 0.0031)
    assert p['fixed_net']['capacity_by_annual_kwh'] == {'gt2800': 20.0}
    assert p['fixed_net']['subscription_dist'] == 5.0
    assert p['fixed_net']['network_3ph'] == 11.0
    assert 'network_1ph' not in p['fixed_net']
    # składniki wygrywają z gross, fixed_total_net ignorowane
    assert 'fixed_total_net' not in json.dumps(p)
    mod = _with(module, {'from': '2026-01-01', 'to': None, 'note': '', 'patch': p})
    r = mod.legacy_rates(DAY)
    assert r['energy_peak_net'] == pytest.approx(0.7)
    assert r['fixed_total_net'] == pytest.approx(36.0)


def test_entry_to_patch_single_phase(module):
    module.settings = tc.TariffSettings(phases=1)
    p = to.entry_to_patch({'fixed_stalysieciowy_net': 8.0}, module, DAY)
    assert p == {'fixed_net': {'network_1ph': 8.0}}


def test_entry_to_patch_gross_only(module):
    p = to.entry_to_patch({'peak_gross': 1.31, 'offpeak_gross': 0.67}, module, DAY)
    mod = _with(module, {'from': '2026-01-01', 'to': None, 'note': '', 'patch': p})
    r = mod.legacy_rates(DAY)
    assert r['peak_gross'] == pytest.approx(1.31, abs=1e-5)
    assert r['offpeak_gross'] == pytest.approx(0.67, abs=1e-5)
    # dist/fees z katalogu bez zmian
    base = module.legacy_rates(DAY)
    assert r['dist_var_peak_net'] == pytest.approx(base['dist_var_peak_net'])
    assert r['dist_oze_net'] == pytest.approx(base['dist_oze_net'])


def test_entry_to_patch_gross_uses_given_dist(module):
    p = to.entry_to_patch({'peak_gross': 1.23, 'dist_var_peak_net': 0.5}, module, DAY)
    mod = _with(module, {'from': '2026-01-01', 'to': None, 'note': '', 'patch': p})
    r = mod.legacy_rates(DAY)
    assert r['dist_var_peak_net'] == pytest.approx(0.5)
    assert r['peak_gross'] == pytest.approx(1.23, abs=1e-5)


def test_entry_to_patch_gross_too_low_raises(module):
    with pytest.raises(ValueError, match='za niskie'):
        to.entry_to_patch({'peak_gross': 0.10}, module, DAY)


# ── patch_to_rates ───────────────────────────────────────────────────────────

def test_patch_to_rates_only_changed_fields(module):
    p = to.entry_to_patch({'dist_oze_net': 0.0099}, module, DAY)
    mod = _with(module, {'from': '2026-01-01', 'to': None, 'note': '', 'patch': p})
    out = to.patch_to_rates(mod, module, DAY)
    assert out['dist_oze_net'] == pytest.approx(0.0099)
    assert 'peak_gross' in out and 'offpeak_gross' in out      # oze wchodzi do brutto
    assert 'fixed_total_net' not in out and 'energy_peak_net' not in out


def test_patch_to_rates_no_change_is_empty(module):
    assert to.patch_to_rates(module, module, DAY) == {}


# ── migrate_legacy ───────────────────────────────────────────────────────────

SEED = {'tariffs': [{'effective_from': '2026-02', 'note': 'Taryfa TD (seed przy instalacji)', 'rates': {
    'peak_gross': 1.23, 'offpeak_gross': 0.63, 'dist_jakosciowa_net': 0.0332, 'dist_oze_net': 0.0073,
    'dist_kogeneracja_net': 0.003, 'fixed_abonament_net': 4.56, 'fixed_stalysieciowy_net': 10.86,
    'fixed_mocowa_net': 24.05, 'fixed_total_net': 39.47}}]}


def _write(p, cfg):
    p.write_text(json.dumps(cfg), encoding='utf-8')


def test_migrate_skips_seed_and_is_idempotent(tmp_path, module):
    cfg_p, ov_p = tmp_path / 'tariff_config.json', tmp_path / 'tariff_overrides.json'
    _write(cfg_p, SEED)
    to.migrate_legacy(cfg_p, ov_p, module)
    assert json.loads(ov_p.read_text()) == []
    assert (tmp_path / 'tariff_config.json.pre-tariff-module').read_text() == cfg_p.read_text()
    # po migracji nic się nie zmienia, nawet gdy config dostanie nowy wpis
    cfg2 = json.loads(json.dumps(SEED))
    cfg2['tariffs'].append({'effective_from': '2027-01', 'note': 'n', 'rates': {'peak_gross': 1.5}})
    _write(cfg_p, cfg2)
    to.migrate_legacy(cfg_p, ov_p, module)
    assert json.loads(ov_p.read_text()) == []


def test_migrate_keeps_entry_with_different_price(tmp_path, module):
    cfg_p, ov_p = tmp_path / 'tariff_config.json', tmp_path / 'tariff_overrides.json'
    cfg = json.loads(json.dumps(SEED))
    cfg['tariffs'].append({'effective_from': '2027-01', 'note': 'TD 2027',
                           'rates': {'peak_gross': 1.31, 'offpeak_gross': 0.67}})
    _write(cfg_p, cfg)
    to.migrate_legacy(cfg_p, ov_p, module)
    out = json.loads(ov_p.read_text())
    assert len(out) == 1
    assert out[0]['from'] == '2027-01-01' and out[0]['to'] is None and out[0]['note'] == 'TD 2027'
    mod = _with(module, out[0])
    assert mod.legacy_rates(date(2027, 3, 1))['peak_gross'] == pytest.approx(1.31, abs=1e-5)
    assert (tmp_path / 'tariff_config.json.pre-tariff-module').exists()


def test_migrate_without_config_does_nothing(tmp_path, module):
    to.migrate_legacy(tmp_path / 'a.json', tmp_path / 'b.json', module)
    assert not (tmp_path / 'b.json').exists()


def test_migrate_does_not_overwrite_existing_overrides(tmp_path, module):
    cfg_p, ov_p = tmp_path / 'tariff_config.json', tmp_path / 'tariff_overrides.json'
    _write(cfg_p, SEED)
    ov_p.write_text('[{"keep": 1}]', encoding='utf-8')
    to.migrate_legacy(cfg_p, ov_p, module)
    assert ov_p.read_text() == '[{"keep": 1}]'
    assert not (tmp_path / 'tariff_config.json.pre-tariff-module').exists()


# ── build ────────────────────────────────────────────────────────────────────

def test_build_reads_overrides_and_override_tariff(tmp_path):
    ov_p = tmp_path / 'o.json'
    to.save([{'from': '2026-01-01', 'to': None, 'note': 'k', 'patch': {'quality_net': 0.05}}], ov_p)
    m = build(tmp_path / 'brak_okresow.json', '', ov_p)
    assert m.legacy_rates(DAY)['dist_jakosciowa_net'] == 0.05
    m2 = build(None, 'G11', None)
    assert m2.tariff_on(date.today()) == 'G11'


# ── E3: kształt patcha, load_status, kwarantanna ─────────────────────────────

@pytest.mark.parametrize('patch', [
    {'energy_net': 0.5},
    {'energy_net': {'G12w': 0.5}},
    {'energy_net': {'G12w': {'peak': -1}}},
    {'dist_var_net': {'G12w': {'peak': 'x'}}},
    {'dynamic_dist_var_net': {'S1': {'peak': 0.1}}},
    {'dynamic_dist_var_net': 0.1},
    {'fixed_net': 1.0},
    {'fixed_net': {'network_1ph': -1}},
    {'fixed_net': {'capacity_by_annual_kwh': {'lt500': {'x': 1}}}},
    {'quality_net': {'G12w': 0.1}},
    {'oze_net': {}},
    {'cogeneration_net': [0.1]},
    {'excise_net': True},
])
def test_validate_patch_shape_rejected(patch):
    with pytest.raises(ValueError, match='korekta 1'):
        to.validate([{'from': '2026-05-01', 'patch': patch}])


def test_validate_patch_shape_accepts_catalog_shapes():
    patch = {
        'energy_net': {'G12w': {'peak': 0.5, 'offpeak': 0.3}},
        'dist_var_net': {'G13': {'morning_peak': 0.2}},
        'dynamic_dist_var_net': {'S1': 0.1},
        'fixed_net': {'network_1ph': 7.0, 'capacity_by_annual_kwh': {'lt500': 2.9},
                      'sales_fee_by_tariff': {'G13s': 19.1}},
        'quality_net': 0.01, 'oze_net': 0.0, 'cogeneration_net': 0.002, 'excise_net': 0.005,
    }
    assert to.validate([{'from': '2026-05-01', 'patch': patch}])


def test_entry_to_patch_output_passes_validate(module):
    rates = {'energy_peak_net': 0.6, 'dist_var_offpeak_net': 0.1, 'dist_oze_net': 0.001,
             'fixed_mocowa_net': 5.0, 'fixed_abonament_net': 4.0, 'fixed_stalysieciowy_net': 8.0}
    patch = to.entry_to_patch(rates, module, DAY, 'G12w')
    assert to.validate([{'from': '2026-06-01', 'patch': patch}])


def test_load_status_missing_file(tmp_path):
    assert to.load_status(tmp_path / 'brak.json') == ([], False)
    assert not (tmp_path / 'brak.json.corrupt').exists()


def test_load_status_valid_file(tmp_path):
    p = tmp_path / 'ov.json'
    entry = {'from': '2026-05-01', 'to': None, 'note': '', 'patch': {'oze_net': 0.1}}
    p.write_text(json.dumps([entry]), encoding='utf-8')
    assert to.load_status(p) == ([entry], False)


@pytest.mark.parametrize('text', ['{nie json', '{"a": 1}', '[{"from": "2026-05-01", "patch": {"energy_net": 0.5}}]'])
def test_load_status_corrupt_flags_and_quarantines(tmp_path, caplog, text):
    p = tmp_path / 'ov.json'
    p.write_text(text, encoding='utf-8')
    with caplog.at_level('ERROR'):
        assert to.load_status(p) == ([], True)
    assert to.load(p) == []
    assert (tmp_path / 'ov.json.corrupt').read_text(encoding='utf-8') == text
    assert any(r.levelname == 'ERROR' for r in caplog.records)


def test_quarantine_twice_keeps_both_copies(tmp_path):
    p = tmp_path / 'ov.json'
    p.write_text('pierwsza', encoding='utf-8')
    to._quarantine(p)
    p.write_text('druga', encoding='utf-8')
    to._quarantine(p)
    assert (tmp_path / 'ov.json.corrupt').read_text(encoding='utf-8') == 'pierwsza'
    extra = [f for f in tmp_path.iterdir() if f.name.startswith('ov.json.corrupt.')]
    assert len(extra) == 1 and extra[0].read_text(encoding='utf-8') == 'druga'
    p.write_text('trzecia', encoding='utf-8')
    to._quarantine(p)
    extra = sorted(f.read_text(encoding='utf-8') for f in tmp_path.iterdir() if f.name.startswith('ov.json.corrupt.'))
    assert extra == ['druga', 'trzecia']


def test_quarantine_same_content_not_copied_again(tmp_path):
    p = tmp_path / 'ov.json'
    p.write_text('x', encoding='utf-8')
    for _ in range(3):
        to._quarantine(p)
    assert sorted(f.name for f in tmp_path.iterdir()) == ['ov.json', 'ov.json.corrupt']


def test_catalog_periods_pass_patch_shape_validation():
    import json as _json
    from pathlib import Path
    cat = _json.loads((Path(to.__file__).parent / 'tariffs' / 'tauron.json').read_text(encoding='utf-8'))
    n = 0
    for period in cat['periods']:
        patch = {k: v for k, v in period.items() if k in to._PATCH_KEYS}
        if patch:
            n += 1
            assert to.validate([{'from': '2026-05-01', 'patch': patch}])
    assert n >= 2
