"""Testy heatpump_store.py — cache godzinowego bilansu pompy ciepła."""
import json

import pytest

from pv_roi_tracker import heatpump_store


def test_load_hours_missing_file_returns_empty(tmp_path):
    assert heatpump_store.load_hours(tmp_path / 'missing.json') == {}


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / 'heatpump_hours.json'
    hours = {
        '2026-01-10T02': {'produced': 0.0, 'exported': 0.0, 'imported': 2.0,
                          'batt_charge': 0.0, 'batt_discharge': 0.0,
                          'hp_kwh': 2.0, 'heat_h': 1.0, 'dhw_h': 0.0, 't_out': -3.2},
    }
    heatpump_store.save_hours(hours, path)
    loaded = heatpump_store.load_hours(path)
    assert loaded == hours


def test_save_hours_rounds_and_preserves_none_temperature(tmp_path):
    path = tmp_path / 'heatpump_hours.json'
    hours = {'2026-01-10T02': {'produced': 1.23456, 'exported': 0.0, 'imported': 0.0,
                               'batt_charge': 0.0, 'batt_discharge': 0.0,
                               'hp_kwh': 0.0, 'heat_h': 0.0, 'dhw_h': 0.0, 't_out': None}}
    heatpump_store.save_hours(hours, path)
    loaded = heatpump_store.load_hours(path)
    assert loaded['2026-01-10T02']['produced'] == pytest.approx(1.2346)
    assert loaded['2026-01-10T02']['t_out'] is None


def test_load_hours_rejects_stale_cache_version(tmp_path):
    path = tmp_path / 'heatpump_hours.json'
    path.write_text(json.dumps({'v': 0, 'hours': {'x': {}}}))
    assert heatpump_store.load_hours(path) == {}


def test_load_hours_corrupt_json_returns_empty(tmp_path):
    path = tmp_path / 'heatpump_hours.json'
    path.write_text('{not json')
    assert heatpump_store.load_hours(path) == {}


def test_save_hours_atomic_no_partial_file_on_crash(tmp_path):
    path = tmp_path / 'sub' / 'heatpump_hours.json'
    heatpump_store.save_hours({'2026-01-01T00': {'produced': 1.0, 'exported': 0.0,
                                                  'imported': 0.0, 'batt_charge': 0.0,
                                                  'batt_discharge': 0.0, 'hp_kwh': 0.0,
                                                  'heat_h': 0.0, 'dhw_h': 0.0, 't_out': None}}, path)
    assert path.exists()
    assert not any(p.suffix == '.tmp' for p in path.parent.iterdir())
