"""Testy tariff_history.py — historia okresów taryfy (czyste funkcje + zapis/odczyt JSON)."""
import json
import logging
from datetime import date

from pv_roi_tracker import tariff_history as th


def test_normalize_is_case_insensitive_and_rejects_unknown():
    assert th.normalize('g12w') == 'G12w'
    assert th.normalize(' G13S ') == 'G13s'
    assert th.normalize('G12') == 'G12'
    assert th.normalize('G14') is None
    assert th.normalize(None) is None
    assert th.normalize('') is None


def test_load_missing_file_returns_empty(tmp_path):
    assert th.load(tmp_path / 'brak.json') == []


def test_load_corrupt_file_returns_empty(tmp_path, caplog):
    p = tmp_path / 'tariff_periods.json'
    p.write_text('{nie json', encoding='utf-8')
    with caplog.at_level(logging.WARNING):
        assert th.load(p) == []


def test_load_invalid_entry_marks_file_corrupt(tmp_path):
    p = tmp_path / 'tariff_periods.json'
    p.write_text(json.dumps([{'tariff': 'G14', 'from': None}]), encoding='utf-8')
    assert th.load(p) == []


def test_save_then_load_roundtrip(tmp_path):
    p = tmp_path / 'sub' / 'tariff_periods.json'
    periods = [{'tariff': 'G12w', 'from': None}, {'tariff': 'G13', 'from': '2026-10-01'}]
    th.save(periods, p)
    assert json.loads(p.read_text(encoding='utf-8')) == periods
    assert th.load(p) == periods


def test_record_detection_empty_history_starts_without_change():
    periods, change = th.record_detection([], 'G12w', date(2026, 10, 27))
    assert periods == [{'tariff': 'G12w', 'from': None}]
    assert change is None


def test_record_detection_same_tariff_is_no_change():
    before = [{'tariff': 'G12w', 'from': None}]
    periods, change = th.record_detection(before, 'g12W', date(2026, 10, 27))
    assert periods == before
    assert change is None


def test_record_detection_change_appends_dated_period():
    before = [{'tariff': 'G12w', 'from': None}]
    periods, change = th.record_detection(before, 'G13', date(2026, 10, 27))
    assert periods == [{'tariff': 'G12w', 'from': None},
                       {'tariff': 'G13', 'from': '2026-10-27'}]
    assert change == ('G12w', 'G13')
    assert before == [{'tariff': 'G12w', 'from': None}]  # wejście nietknięte


def test_record_detection_unknown_or_none_changes_nothing():
    before = [{'tariff': 'G12w', 'from': None}]
    assert th.record_detection(before, None, date(2026, 10, 27)) == (before, None)
    assert th.record_detection(before, 'G14', date(2026, 10, 27)) == (before, None)


def test_current_override_wins_then_last_period_then_none():
    periods = [{'tariff': 'G12w', 'from': None}, {'tariff': 'G13', 'from': '2026-10-01'}]
    assert th.current(periods) == 'G13'
    assert th.current(periods, override='g11') == 'G11'
    assert th.current(periods, override='') == 'G13'
    assert th.current(periods, override='XYZ') == 'G13'
    assert th.current([], override=None) is None
    assert th.current([], override='G12w') == 'G12w'


def test_tariff_on_before_and_after_change():
    periods = [{'tariff': 'G12w', 'from': None}, {'tariff': 'G13', 'from': '2026-10-01'}]
    assert th.tariff_on(periods, date(2025, 1, 1)) == 'G12w'
    assert th.tariff_on(periods, date(2026, 9, 30)) == 'G12w'
    assert th.tariff_on(periods, date(2026, 10, 1)) == 'G13'
    assert th.tariff_on(periods, date(2027, 5, 5)) == 'G13'


def test_tariff_on_empty_history_is_none():
    assert th.tariff_on([], date(2026, 10, 27)) is None
    dated_only = [{'tariff': 'G11', 'from': '2026-10-01'}]
    assert th.tariff_on(dated_only, date(2026, 9, 1)) is None
