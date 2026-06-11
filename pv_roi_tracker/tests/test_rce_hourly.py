"""Tests for rce_hourly.py — pure computation parts (no I/O)."""
from datetime import date
from pathlib import Path

import pytest

from pv_roi_tracker.models import MonthlyRecord
from pv_roi_tracker.rce_hourly import (
    build_summary,
    compare_month,
    rows_to_hourly,
    update_and_compare,
)


# ── rows_to_hourly ────────────────────────────────────────────────────────────

def test_rows_to_hourly_buckets_quarters_into_hour_start():
    # dtime = KONIEC okresu: 00:15..01:00 → godzina 00
    rows = [
        {'dtime': '2026-05-01 00:15:00', 'rce_pln': 100.0},
        {'dtime': '2026-05-01 00:30:00', 'rce_pln': 200.0},
        {'dtime': '2026-05-01 00:45:00', 'rce_pln': 300.0},
        {'dtime': '2026-05-01 01:00:00', 'rce_pln': 400.0},
        {'dtime': '2026-05-01 01:15:00', 'rce_pln': 500.0},
    ]
    out = rows_to_hourly(rows)
    assert out['2026-05-01T00'] == 250.0
    assert out['2026-05-01T01'] == 500.0


def test_rows_to_hourly_ignores_malformed():
    rows = [
        {'dtime': 'garbage', 'rce_pln': 100.0},
        {'dtime': '2026-05-01 00:15:00', 'rce_pln': None},
        {'rce_pln': 100.0},
    ]
    assert rows_to_hourly(rows) == {}


# ── compare_month ─────────────────────────────────────────────────────────────

def _export():
    # 10 kWh o 10:00 i 5 kWh o 12:00
    return {'2026-05-10T10': 10.0, '2026-05-10T12': 5.0}


def _prices():
    return {'2026-05-10T10': 500.0, '2026-05-10T12': 300.0}  # PLN/MWh netto


def test_compare_month_revenue_and_diff_with_vat():
    # 2026-05 ≥ 2025-02 → factor 1.23
    row = compare_month('2026-05', _export(), _prices(),
                        rcem_price_gross=0.50, exported_total_kwh=15.0)
    # RCE: (10×0.5 + 5×0.3) × 1.23 = 6.5 × 1.23 = 7.995
    assert row['revenue_rce_pln'] == pytest.approx(8.0, abs=0.01)
    # RCEm na tej samej bazie kWh: 15 × 0.50 = 7.50
    assert row['revenue_rcem_pln'] == pytest.approx(7.5)
    assert row['diff_pln'] == pytest.approx(0.5, abs=0.01)
    assert row['rce_better'] is True
    assert row['coverage_pct'] == 100.0


def test_compare_month_no_vat_before_2025_02():
    export = {'2024-08-10T10': 10.0}
    prices = {'2024-08-10T10': 500.0}
    row = compare_month('2024-08', export, prices,
                        rcem_price_gross=0.40, exported_total_kwh=10.0)
    assert row['revenue_rce_pln'] == pytest.approx(5.0)   # bez ×1.23
    assert row['rce_weighted_price_pln_kwh'] == pytest.approx(0.5)


def test_compare_month_partial_coverage():
    export = {'2026-05-10T10': 10.0, '2026-05-10T12': 10.0}
    prices = {'2026-05-10T10': 500.0}  # brak ceny dla 12:00
    row = compare_month('2026-05', export, prices,
                        rcem_price_gross=0.50, exported_total_kwh=20.0)
    assert row['coverage_pct'] == 50.0
    assert row['matched_kwh'] == 10.0


def test_compare_month_none_without_export():
    assert compare_month('2026-05', {}, _prices(), 0.5, 0.0) is None


def test_compare_month_unknown_rcem():
    row = compare_month('2026-05', _export(), _prices(),
                        rcem_price_gross=None, exported_total_kwh=15.0)
    assert row['revenue_rcem_pln'] is None
    assert row['diff_pln'] is None
    assert row['rce_better'] is None


# ── build_summary ─────────────────────────────────────────────────────────────

def _row(ym, diff, coverage=100.0, estimated=False):
    return {'ym': ym, 'diff_pln': diff, 'coverage_pct': coverage,
            'rcem_estimated': estimated}


def test_build_summary_recommends_rce_when_clearly_better():
    rows = [_row('2025-01', 20.0), _row('2025-02', 30.0), _row('2025-03', 25.0)]
    s = build_summary(rows)
    assert s['n_months'] == 3
    assert s['recommendation'] == 'ROZWAŻ RCE'
    assert s['avg_monthly_diff_pln'] == 25.0
    assert s['pct_rce_better'] == 100.0


def test_build_summary_recommends_rcem_when_worse():
    rows = [_row('2025-01', -20.0), _row('2025-02', -30.0), _row('2025-03', -25.0)]
    assert build_summary(rows)['recommendation'] == 'ZOSTAŃ PRZY RCEm'


def test_build_summary_excludes_estimated_and_low_coverage():
    rows = [
        _row('2025-01', 20.0),
        _row('2025-02', 30.0, coverage=50.0),     # za niskie pokrycie
        _row('2025-03', 25.0, estimated=True),    # RCEm szacowana
    ]
    s = build_summary(rows)
    assert s['n_months'] == 1
    assert s['recommendation'] == 'BRAK DANYCH'


# ── update_and_compare (z wstrzykniętym fetchem statystyk, bez sieci) ────────

def test_update_and_compare_freezes_settled_months(tmp_path, monkeypatch):
    import pv_roi_tracker.rce_hourly as rh
    cache = tmp_path / 'rce_hourly.json'

    # ceny już w cache → _ensure_prices nie woła sieci
    monkeypatch.setattr(rh, '_ensure_prices', lambda c, m, t: c['prices'].update({
        '2026-04-15T10': 500.0, '2026-05-15T10': 400.0,
    }))

    calls = []

    def fake_stats(entities, start, period):
        calls.append(start)
        return {rh.EXPORT_ENTITY: {'2026-04-15T10': 100.0, '2026-05-15T10': 50.0}}

    records = [MonthlyRecord(year=2026, month=4, exported_kwh=100.0),
               MonthlyRecord(year=2026, month=5, exported_kwh=50.0)]
    rcem = {'2026-04': 0.45}  # maj jeszcze nierozliczony

    out = update_and_compare(records, rcem, cache_path=cache,
                             get_stats_fn=fake_stats, today=date(2026, 5, 20))
    assert [m['ym'] for m in out['months']] == ['2026-04', '2026-05']
    apr = out['months'][0]
    assert apr['diff_pln'] is not None
    may = out['months'][1]
    assert may['rcem_estimated'] is True  # szacunek wg 2026-04

    # drugi przebieg: kwiecień zamrożony → stats od maja
    out2 = update_and_compare(records, rcem, cache_path=cache,
                              get_stats_fn=fake_stats, today=date(2026, 5, 20))
    assert calls[1] == '2026-05-01'
    assert [m['ym'] for m in out2['months']] == ['2026-04', '2026-05']
