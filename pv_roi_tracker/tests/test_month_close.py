"""close_month: nie dopisuje snapshotu bez podziału peak/offpeak (brak LTS stref)."""
from unittest.mock import patch

from pv_roi_tracker import month_close, live_reader, historic_store, rcem_scraper
from pv_roi_tracker.models import MonthlyRecord


def _record(peak, offpeak):
    return MonthlyRecord(year=2026, month=11, purchased_kwh_peak=peak, purchased_kwh_offpeak=offpeak)


def _run(record, tmp_path):
    with (
        patch.object(rcem_scraper, 'get_current_month_rcem', return_value=None),
        patch.object(live_reader, 'read_current_month', return_value=record),
        patch.object(historic_store, 'append_month', return_value=True) as append,
    ):
        result = month_close.close_month(historic_path=tmp_path / 'h.json', rcem_history_path=tmp_path / 'r.json')
    return result, append


def test_close_month_skips_record_without_peak_offpeak(tmp_path):
    result, append = _run(_record(None, None), tmp_path)
    assert result is False
    append.assert_not_called()


def test_close_month_skips_when_only_offpeak_missing(tmp_path):
    result, append = _run(_record(10.0, None), tmp_path)
    assert result is False
    append.assert_not_called()


def test_close_month_appends_complete_record(tmp_path):
    result, append = _run(_record(10.0, 5.0), tmp_path)
    assert result is True
    append.assert_called_once()
