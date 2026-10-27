"""Testy tariff_history.py — historia okresów taryfy (czyste funkcje + zapis/odczyt JSON)."""
import json
import logging
from datetime import date

import pytest

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


# ── validate_periods (E3) ────────────────────────────────────────────────────

def test_validate_periods_ok_i_kanoniczna_nazwa():
    raw = [{'tariff': 'g12w', 'from': None}, {'tariff': 'G13', 'from': '2026-03-01'}]
    assert th.validate_periods(raw) == [{'tariff': 'G12w', 'from': None},
                                        {'tariff': 'G13', 'from': '2026-03-01'}]


def test_validate_periods_jeden_wpis_z_data():
    assert th.validate_periods([{'tariff': 'G11', 'from': '2025-01-01'}]) == [
        {'tariff': 'G11', 'from': '2025-01-01'}]


@pytest.mark.parametrize('raw', [
    [], None, 'x', {}, [1],
    [{'tariff': 'XX', 'from': None}],
    [{'from': None}],
    [{'tariff': 'G12w'}],
    [{'tariff': 'G12w', 'from': None}, {'tariff': 'G13', 'from': None}],
    [{'tariff': 'G12w', 'from': None}, {'tariff': 'G13', 'from': '2026-13-01'}],
    [{'tariff': 'G12w', 'from': None}, {'tariff': 'G13', 'from': '2026-03-01'},
     {'tariff': 'G11', 'from': '2026-03-01'}],
    [{'tariff': 'G12w', 'from': '2026-05-01'}, {'tariff': 'G13', 'from': '2026-03-01'}],
])
def test_validate_periods_zle_wejscia(raw):
    with pytest.raises(ValueError):
        th.validate_periods(raw)


def test_load_status_corrupt_json_flags_and_copies_original(tmp_path):
    p = tmp_path / 'tariff_periods.json'
    p.write_text('{nie json', encoding='utf-8')
    assert th.load_status(p) == ([], True)
    corrupt = tmp_path / 'tariff_periods.json.corrupt'
    assert corrupt.read_text(encoding='utf-8') == '{nie json'


def test_load_status_second_corrupt_read_keeps_first_copy(tmp_path):
    p = tmp_path / 'tariff_periods.json'
    p.write_text('{pierwsza', encoding='utf-8')
    th.load_status(p)
    p.write_text('{druga', encoding='utf-8')
    assert th.load_status(p) == ([], True)
    corrupt = tmp_path / 'tariff_periods.json.corrupt'
    assert corrupt.read_text(encoding='utf-8') == '{pierwsza'


def test_load_status_invalid_entry_is_corrupt(tmp_path):
    p = tmp_path / 'tariff_periods.json'
    p.write_text(json.dumps([{'tariff': 'G14', 'from': None}]), encoding='utf-8')
    assert th.load_status(p) == ([], True)
    assert (tmp_path / 'tariff_periods.json.corrupt').exists()


def test_load_status_missing_file_is_not_corrupt(tmp_path):
    assert th.load_status(tmp_path / 'brak.json') == ([], False)
    assert not (tmp_path / 'brak.json.corrupt').exists()


def test_load_status_valid_file_is_not_corrupt(tmp_path):
    p = tmp_path / 'tariff_periods.json'
    periods = [{'tariff': 'G12w', 'from': None}]
    th.save(periods, p)
    assert th.load_status(p) == (periods, False)
    assert not (tmp_path / 'tariff_periods.json.corrupt').exists()


def test_load_status_copy_failure_is_logged_not_raised(tmp_path, monkeypatch, caplog):
    p = tmp_path / 'tariff_periods.json'
    p.write_text('{nie json', encoding='utf-8')

    def boom(*a, **k):
        raise OSError('brak miejsca')
    monkeypatch.setattr(th.shutil, 'copy2', boom)
    with caplog.at_level(logging.ERROR):
        assert th.load_status(p) == ([], True)
    assert 'nie można skopiować' in caplog.text


def test_with_override_change_appends_dated_period():
    base = [{'tariff': 'G12w', 'from': None}]
    out = th.with_override(base, 'G13', date(2026, 10, 27))
    assert out == [{'tariff': 'G12w', 'from': None}, {'tariff': 'G13', 'from': '2026-10-27'}]
    assert base == [{'tariff': 'G12w', 'from': None}]


def test_with_override_empty_history_uses_g12w_base():
    out = th.with_override([], 'G13', date(2026, 10, 27))
    assert out == [{'tariff': 'G12w', 'from': None}, {'tariff': 'G13', 'from': '2026-10-27'}]


def test_with_override_same_as_last_returns_periods_unchanged():
    base = [{'tariff': 'G12w', 'from': None}]
    assert th.with_override(base, 'g12W', date(2026, 10, 27)) == base


def test_with_override_invalid_returns_periods_unchanged():
    base = [{'tariff': 'G12w', 'from': None}]
    assert th.with_override(base, 'G14', date(2026, 10, 27)) == base
    assert th.with_override(base, None, date(2026, 10, 27)) == base


def test_with_override_empty_without_override_stays_empty():
    assert th.with_override([], None, date(2026, 10, 27)) == []
    assert th.with_override([], 'G14', date(2026, 10, 27)) == []
