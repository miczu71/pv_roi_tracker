"""Tests for yoy.py — MTD/YTD rok-do-roku, rozbicie przyczyn, projekcja, wyścig."""
import pytest
from datetime import date

from pv_roi_tracker.models import MonthlyRecord
from pv_roi_tracker import yoy


def rec(year, month_num, produced, exported, imported, buy, feedin, arb=0.0):
    """Zbuduj zamknięty MonthlyRecord ze spójnymi polami pochodnymi (jak
    live_reader._build_record), do testowania yoy.py bez I/O."""
    self_consumed = produced - exported
    consumed = self_consumed + imported
    return MonthlyRecord(
        year=year, month=month_num,
        produced_kwh=produced, exported_kwh=exported, self_consumed_kwh=self_consumed,
        purchased_kwh=imported, consumed_kwh=consumed,
        buy_price_pln_kwh=buy, feedin_price_pln_kwh=feedin,
        self_consumed_savings_pln=round(self_consumed * buy, 2),
        feedin_revenue_pln=round(exported * feedin, 2),
        battery_arbitrage_savings_pln=arb,
    )


# ── build_period_metrics ──────────────────────────────────────────────────────

def test_build_period_metrics_basic():
    m = yoy.build_period_metrics(produced_kwh=300.0, exported_kwh=100.0, imported_kwh=50.0,
                                 buy_price=0.9, feedin_price=0.3, arbitrage_pln=5.0, system_kwp=6.72)
    assert m.self_consumed_kwh == pytest.approx(200.0)
    assert m.consumed_kwh == pytest.approx(250.0)
    assert m.self_savings_pln == pytest.approx(180.0)
    assert m.feedin_revenue_pln == pytest.approx(30.0)
    assert m.savings_pln == pytest.approx(215.0)  # 180 + 30 + 5
    assert m.self_consumption_rate_pct == pytest.approx(200 / 300 * 100, abs=0.1)
    assert m.autarky_pct == pytest.approx(200 / 250 * 100, abs=0.1)
    assert m.specific_yield == pytest.approx(300 / 6.72, abs=0.1)


def test_build_period_metrics_missing_price_leaves_savings_none_for_that_stream():
    m = yoy.build_period_metrics(produced_kwh=300.0, exported_kwh=100.0, imported_kwh=50.0,
                                 buy_price=None, feedin_price=0.3)
    assert m.self_savings_pln is None
    assert m.feedin_revenue_pln == pytest.approx(30.0)
    assert m.savings_pln == pytest.approx(30.0)  # brak self_savings liczony jako 0, nie None


# ── decompose_effects: tożsamość sumy ─────────────────────────────────────────

def test_decompose_effects_sums_exactly_to_delta_savings():
    prev = yoy.build_period_metrics(500.0, 150.0, 80.0, buy_price=0.85, feedin_price=0.25, arbitrage_pln=10.0)
    cur = yoy.build_period_metrics(560.0, 120.0, 90.0, buy_price=0.95, feedin_price=0.35, arbitrage_pln=15.0)
    effects = yoy.decompose_effects(prev, cur)
    assert effects is not None
    total = sum(effects.values())
    delta = cur.savings_pln - prev.savings_pln
    assert total == pytest.approx(delta, abs=0.01)


def test_decompose_effects_sums_exactly_even_with_production_drop():
    """Ten sam test co wyżej, ale z SPADKIEM produkcji r/r — upewnia się, że
    tożsamość trzyma się także gdy (P1-P0) jest ujemne."""
    prev = yoy.build_period_metrics(600.0, 200.0, 60.0, buy_price=0.9, feedin_price=0.3)
    cur = yoy.build_period_metrics(450.0, 130.0, 100.0, buy_price=1.1, feedin_price=0.2)
    effects = yoy.decompose_effects(prev, cur)
    total = sum(effects.values())
    delta = cur.savings_pln - prev.savings_pln
    assert total == pytest.approx(delta, abs=0.01)


def test_decompose_effects_none_when_price_missing():
    prev = yoy.build_period_metrics(500.0, 150.0, 80.0, buy_price=0.85, feedin_price=None)
    cur = yoy.build_period_metrics(560.0, 120.0, 90.0, buy_price=0.95, feedin_price=0.35)
    assert yoy.decompose_effects(prev, cur) is None


# ── pair_closed_months ─────────────────────────────────────────────────────────

def test_pair_closed_months_pairs_and_flags_unpaired():
    records = [
        rec(2025, 1, 400, 100, 50, 0.8, 0.3),
        rec(2026, 1, 420, 110, 55, 0.85, 0.32),
        rec(2026, 2, 300, 80, 40, 0.85, 0.32),   # brak pary (2025-02 nie istnieje)
    ]
    today = date(2026, 3, 15)
    pairs, unpaired = yoy.pair_closed_months(records, today)
    assert len(pairs) == 1
    assert pairs[0][0].month == 1 and pairs[0][0].year == 2026
    assert pairs[0][1].month == 1 and pairs[0][1].year == 2025
    assert unpaired == ['2026-02']


# ── metrics_from_record / aggregate_metrics ───────────────────────────────────

def test_metrics_from_record_matches_build_period_metrics():
    r = rec(2026, 6, 500.0, 150.0, 80.0, 0.9, 0.3, arb=12.0)
    m = yoy.metrics_from_record(r)
    expected = yoy.build_period_metrics(500.0, 150.0, 80.0, 0.9, 0.3, arbitrage_pln=12.0)
    assert m.savings_pln == pytest.approx(expected.savings_pln)
    assert m.net_grid_cost_pln == pytest.approx(expected.net_grid_cost_pln)


def test_aggregate_metrics_uses_kwh_weighted_prices_not_averaged_averages():
    # Miesiąc A: dużo kWh po niskiej cenie; miesiąc B: mało kWh po wysokiej cenie.
    a = yoy.build_period_metrics(1000.0, 0.0, 900.0, buy_price=0.5, feedin_price=0.2)
    b = yoy.build_period_metrics(100.0, 0.0, 100.0, buy_price=2.0, feedin_price=0.2)
    agg = yoy.aggregate_metrics([a, b])
    # prosta średnia (0.5+2.0)/2=1.25 byłaby błędna — ważona kWh musi być bliżej 0.5
    assert agg.buy_price < 0.8
    assert agg.purchased_kwh == pytest.approx(1000.0)


# ── build_ytd_period: efekty sumują się z miesięcy ────────────────────────────

def test_ytd_effects_equal_sum_of_monthly_effects():
    pairs = [
        (rec(2026, 1, 400, 100, 60, 0.85, 0.3), rec(2025, 1, 380, 90, 55, 0.80, 0.28)),
        (rec(2026, 2, 300, 70, 90, 0.90, 0.25), rec(2025, 2, 320, 85, 80, 0.82, 0.30)),
    ]
    ytd = yoy.build_ytd_period(pairs, mtd_period=None, system_kwp=None, label='test')
    expected = {k: 0.0 for k in ('production', 'self_consumption', 'buy_price', 'feedin_price', 'arbitrage')}
    for c, p in pairs:
        eff = yoy.decompose_effects(yoy.metrics_from_record(p), yoy.metrics_from_record(c))
        for k in expected:
            expected[k] += eff[k]
    for k in expected:
        assert ytd['effects'][k] == pytest.approx(round(expected[k], 2), abs=0.01)
    # I delta całościowa == suma efektów
    assert sum(ytd['effects'].values()) == pytest.approx(ytd['delta_savings_pln'], abs=0.05)


def test_ytd_period_includes_mtd_window_in_totals():
    pairs = [(rec(2026, 1, 400, 100, 60, 0.85, 0.3), rec(2025, 1, 380, 90, 55, 0.80, 0.28))]
    mtd_prev = yoy.build_period_metrics(50.0, 10.0, 8.0, buy_price=0.8, feedin_price=0.28)
    mtd_cur = yoy.build_period_metrics(60.0, 12.0, 9.0, buy_price=0.9, feedin_price=0.32)
    mtd_period = yoy.build_period('MTD', mtd_prev, mtd_cur)
    ytd = yoy.build_ytd_period(pairs, mtd_period, system_kwp=None, label='test')
    assert ytd['cur']['produced_kwh'] == pytest.approx(400.0 + 60.0)
    assert ytd['prev']['produced_kwh'] == pytest.approx(380.0 + 50.0)


# ── build_projection ───────────────────────────────────────────────────────────

def test_projection_scales_prev_full_year_by_pace():
    proj = yoy.build_projection(
        ytd_cur_savings=1100.0, ytd_prev_savings=1000.0,
        ytd_cur_produced=None, ytd_prev_produced=None,
        prev_full_year_savings=6000.0, prev_full_year_produced=None,
    )
    assert proj['savings_pln'] == pytest.approx(6600.0)  # 6000 * 1100/1000
    assert proj['gap_savings_pln'] == pytest.approx(600.0)
    assert proj['pace_pct'] == pytest.approx(110.0)


def test_projection_none_without_prior_ytd():
    assert yoy.build_projection(100.0, 0.0, None, None, 5000.0, None) is None
    assert yoy.build_projection(100.0, 100.0, None, None, None, None) is None


# ── build_race_series ──────────────────────────────────────────────────────────

def test_race_series_cumulative_and_today_point():
    records = [
        rec(2025, m, 300 + m, 80, 40, 0.8, 0.3) for m in range(1, 13)
    ] + [
        rec(2026, 1, 320, 90, 45, 0.85, 0.32),
        rec(2026, 2, 340, 95, 50, 0.85, 0.32),
    ]
    today = date(2026, 3, 10)
    mtd_prev = yoy.build_period_metrics(50.0, 10.0, 5.0, buy_price=0.85, feedin_price=0.30)
    mtd_cur = yoy.build_period_metrics(60.0, 12.0, 6.0, buy_price=0.9, feedin_price=0.35)
    mtd_by_method = {'estimate': yoy.build_period('MTD', mtd_prev, mtd_cur), 'prior_year': None}

    race = yoy.build_race_series(records, today, mtd_by_method)
    assert len(race['prev']['savings_pln']) == 12
    assert race['prev']['savings_pln'][-1] == pytest.approx(
        sum((yoy.metrics_from_record(rec(2025, m, 300 + m, 80, 40, 0.8, 0.3)).savings_pln) for m in range(1, 13)),
        abs=0.05)
    # cur_closed ma tylko styczeń+luty (przed marcem)
    assert len(race['cur_closed']['savings_pln']) == 2
    closed_total = race['cur_closed']['savings_pln'][-1]
    assert race['cur_today']['estimate']['savings_pln'] == pytest.approx(closed_total + mtd_cur.savings_pln, abs=0.05)
    assert race['cur_today']['prior_year'] is None


# ── build_yoy_payload: integracja ──────────────────────────────────────────────

def _full_year(year, base=300):
    return [rec(year, m, base + m * 5, 80 + m, 40, 0.85, 0.30) for m in range(1, 13)]


def test_build_yoy_payload_none_on_first_of_month():
    records = _full_year(2025) + _full_year(2026)[:8]
    assert yoy.build_yoy_payload(records, date(2026, 9, 1), days_elapsed=0,
                                 mtd_prev_metrics=None, mtd_cur_metrics_by_method={},
                                 mtd_flags_by_method={}) is None


def test_build_yoy_payload_full_structure():
    records = _full_year(2025) + [
        rec(2026, m, 300 + m * 6, 80 + m, 42, 0.9, 0.32) for m in range(1, 9)
    ]
    today = date(2026, 9, 27)
    days_elapsed = 26
    mtd_prev = yoy.build_period_metrics(60.0, 15.0, 8.0, buy_price=0.85, feedin_price=0.30)
    mtd_cur_by_method = {
        'estimate': yoy.build_period_metrics(70.0, 18.0, 9.0, buy_price=0.95, feedin_price=0.40),
        'prior_year': yoy.build_period_metrics(70.0, 18.0, 9.0, buy_price=0.95, feedin_price=0.30),
    }
    flags = {'estimate': {'rcem_estimated': True}, 'prior_year': {'rcem_estimated': True}}

    payload = yoy.build_yoy_payload(records, today, days_elapsed, mtd_prev, mtd_cur_by_method, flags,
                                     system_kwp=6.72)

    assert payload['method_default'] == 'estimate'
    assert set(payload['methods']) == {'estimate', 'prior_year'}
    for method in ('estimate', 'prior_year'):
        assert method in payload['mtd']
        assert method in payload['ytd']
        assert method in payload['projection']
        # tożsamość: suma efektów MTD == delta MTD
        mtd = payload['mtd'][method]
        assert sum(mtd['effects'].values()) == pytest.approx(mtd['delta_savings_pln'], abs=0.05)
        # tożsamość: suma efektów YTD == delta YTD
        ytd = payload['ytd'][method]
        assert sum(ytd['effects'].values()) == pytest.approx(ytd['delta_savings_pln'], abs=0.05)
        # projekcja istnieje (pełny 2025 == 12 miesięcy)
        assert payload['projection'][method] is not None

    # dwie metody różnią się tylko przez cenę RCEm (feedin_price cur) — MTD prev identyczny
    assert payload['mtd']['estimate']['prev'] == payload['mtd']['prior_year']['prev']
    assert (payload['mtd']['estimate']['cur']['feedin_revenue_pln']
            != payload['mtd']['prior_year']['cur']['feedin_revenue_pln'])

    race = payload['race']
    assert race['cur_year'] == 2026 and race['prev_year'] == 2025
    assert len(race['cur_closed']['savings_pln']) == 8  # sty..sie zamknięte
