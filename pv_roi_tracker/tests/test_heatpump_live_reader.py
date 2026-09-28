"""Testy live_reader._daily_counter_hourly_deltas — zamiana godzinowego 'max'
dziennie-resetującego się licznika (sensor.pompa_heating/hot_water) na deltę
przyrostu w danej godzinie."""
from datetime import datetime

import pytest

from pv_roi_tracker.live_reader import _daily_counter_hourly_deltas


def _row(dt: datetime, val: float) -> dict:
    return {'start': int(dt.timestamp() * 1000), 'max': val}


def test_first_hour_of_day_is_the_raw_counter_value():
    rows = [_row(datetime(2026, 1, 10, 0), 0.3)]
    out = _daily_counter_hourly_deltas(rows)
    assert out['2026-01-10T00'] == pytest.approx(0.3)


def test_increasing_counter_within_day_gives_hourly_delta():
    rows = [_row(datetime(2026, 1, 10, 0), 0.5),
            _row(datetime(2026, 1, 10, 1), 1.5),
            _row(datetime(2026, 1, 10, 2), 1.5)]
    out = _daily_counter_hourly_deltas(rows)
    assert abs(out['2026-01-10T00'] - 0.5) < 1e-9
    assert abs(out['2026-01-10T01'] - 1.0) < 1e-9
    assert abs(out['2026-01-10T02'] - 0.0) < 1e-9


def test_midnight_reset_gives_fresh_delta_not_negative():
    rows = [_row(datetime(2026, 1, 10, 23), 20.0),
            _row(datetime(2026, 1, 11, 0), 0.4)]
    out = _daily_counter_hourly_deltas(rows)
    assert abs(out['2026-01-11T00'] - 0.4) < 1e-9  # not -19.6


def test_unexpected_intraday_dip_treated_as_fresh_value_not_negative():
    rows = [_row(datetime(2026, 1, 10, 5), 3.0),
            _row(datetime(2026, 1, 10, 6), 1.0)]  # dip within same day
    out = _daily_counter_hourly_deltas(rows)
    assert out['2026-01-10T06'] >= 0
    assert abs(out['2026-01-10T06'] - 1.0) < 1e-9


def test_missing_start_or_max_rows_are_skipped():
    rows = [{'start': None, 'max': 1.0}, {'start': 1, 'max': None}]
    assert _daily_counter_hourly_deltas(rows) == {}
