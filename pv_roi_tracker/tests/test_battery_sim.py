"""Testy battery_sim — symulacja wirtualnego drugiego modułu magazynu."""
import math
from datetime import date, datetime

import pytest

from pv_roi_tracker.battery_sim import (
    BatteryConfig,
    is_peak,
    polish_holidays,
    simulate_s1,
    simulate_s2,
    simulate_all,
    sell_threshold_analysis,
    summarize,
    throughput_cost_kwh,
)

CFG = BatteryConfig(module_price_pln=7599.0, usable_kwh=5.0, power_kw=2.5,
                    roundtrip_eff=0.92, lifetime_cycles=4500.0,
                    start_month='2023-06')

ZONE = {'2026-06': (1.20, 0.60)}
RCEM = {'2026-06': 0.30}

# 2026-06-01 to poniedziałek (dzień roboczy)
MON = '2026-06-01'
SAT = '2026-06-06'


def _h(day: str, hour: int) -> str:
    return f'{day}T{hour:02d}'


# ── Strefy G12w ───────────────────────────────────────────────────────────────

def test_peak_weekday_hours():
    assert is_peak(datetime(2026, 6, 1, 7))      # pon 07 — szczyt
    assert is_peak(datetime(2026, 6, 1, 21))     # pon 21 — szczyt
    assert not is_peak(datetime(2026, 6, 1, 13)) # dolina południowa 13–15
    assert not is_peak(datetime(2026, 6, 1, 14))
    assert not is_peak(datetime(2026, 6, 1, 5))  # noc
    assert not is_peak(datetime(2026, 6, 1, 22))


def test_peak_weekend_and_holiday():
    assert not is_peak(datetime(2026, 6, 6, 10))   # sobota
    assert not is_peak(datetime(2026, 5, 1, 10))   # 1 maja (piątek) — święto
    assert not is_peak(datetime(2026, 6, 4, 10))   # Boże Ciało 2026-06-04 (czwartek)


def test_polish_holidays_moveable():
    hol = polish_holidays(2026)
    assert date(2026, 4, 6) in hol    # Poniedziałek Wielkanocny (Wielkanoc 2026-04-05)
    assert date(2026, 6, 4) in hol    # Boże Ciało
    assert date(2026, 5, 24) in hol   # Zielone Świątki


# ── S1: przechwytywanie eksportu / rozładowanie ──────────────────────────────

def test_s1_charge_from_export_then_discharge_peak():
    # 1 kWh eksportu w nocy, 5 kWh importu w szczycie następnego dnia.
    hours = {
        _h(MON, 2): (1.0, 0.0),
        _h(MON, 7): (0.0, 5.0),
    }
    rows = simulate_s1(hours, CFG, ZONE, RCEM)
    assert len(rows) == 1
    r = rows[0]
    assert r['charged_kwh'] == pytest.approx(1.0)
    # dostarczone = 1 kWh × η
    assert r['discharged_kwh'] == pytest.approx(0.92, abs=1e-6)
    assert r['disch_peak_kwh'] == pytest.approx(0.92, abs=1e-6)
    # marża = 0.92×1.20 − 1.0×0.30
    assert r['savings_pln'] == pytest.approx(0.92 * 1.20 - 0.30, abs=1e-6)


def test_s1_power_cap_limits_hourly_flow():
    hours = {
        _h(MON, 2): (10.0, 0.0),   # eksport 10 kWh w 1 h — cap 2.5
        _h(MON, 7): (0.0, 10.0),
    }
    rows = simulate_s1(hours, CFG, ZONE, RCEM)
    r = rows[0]
    assert r['charged_kwh'] == pytest.approx(2.5)
    assert r['discharged_kwh'] == pytest.approx(2.5 * 0.92, abs=1e-6)


def test_s1_capacity_cap():
    # 3 godziny × 2.5 kWh eksportu = 7.5, ale mieści się tylko 5/√η ≈ 5.21
    hours = {_h(MON, h): (2.5, 0.0) for h in (1, 2, 3)}
    hours[_h(MON, 18)] = (0.0, 10.0)
    hours[_h(MON, 19)] = (0.0, 10.0)
    hours[_h(MON, 20)] = (0.0, 10.0)
    rows = simulate_s1(hours, CFG, ZONE, RCEM)
    r = rows[0]
    eff_leg = math.sqrt(0.92)
    assert r['charged_kwh'] == pytest.approx(5.0 / eff_leg, abs=1e-3)
    assert r['discharged_kwh'] == pytest.approx(5.0 * eff_leg, abs=1e-3)


def test_s1_discharge_split_by_zone():
    hours = {
        _h(MON, 2): (2.0, 0.0),
        _h(MON, 7): (0.0, 0.5),    # szczyt
        _h(MON, 13): (0.0, 0.5),   # dolina południowa
    }
    rows = simulate_s1(hours, CFG, ZONE, RCEM)
    r = rows[0]
    assert r['disch_peak_kwh'] == pytest.approx(0.5)
    assert r['disch_offpeak_kwh'] == pytest.approx(0.5)
    assert r['avoided_buy_pln'] == pytest.approx(0.5 * 1.20 + 0.5 * 0.60)


def test_s1_no_discharge_without_charge():
    hours = {_h(MON, 7): (0.0, 5.0)}
    rows = simulate_s1(hours, CFG, ZONE, RCEM)
    assert rows[0]['discharged_kwh'] == 0.0
    assert rows[0]['savings_pln'] == 0.0


def test_s1_rcem_fallback_flag():
    hours = {_h('2026-07-06', 2): (1.0, 0.0)}   # brak RCEm dla 2026-07
    rows = simulate_s1(hours, CFG, ZONE, RCEM)
    assert rows[0]['rcem_estimated'] is True
    # fallback = średnia znanych RCEm (0.30)
    assert rows[0]['foregone_rcem_pln'] == pytest.approx(0.30)


def test_s1_arbitrage_charges_offpeak_discharges_peak_only():
    cfg = BatteryConfig(**{**CFG.to_dict(), 'arbitrage_enabled': True})
    hours = {
        _h(MON, 2): (0.0, 0.0),    # dolina: dokup do pełna
        _h(MON, 7): (0.0, 3.0),    # szczyt: oddaj
        _h(MON, 13): (0.0, 3.0),   # dolina: energia arbitrażowa NIE może być oddana
    }
    rows = simulate_s1(hours, cfg, ZONE, RCEM)
    r = rows[0]
    # chciwy wariant: dokup 2.5 w nocy + 2.5 w dolinie 13–15 (cap mocy per godzina)
    assert r['arb_charged_kwh'] == pytest.approx(5.0)
    assert r['arb_discharged_kwh'] <= 2.5 * 0.92 + 1e-9
    assert r['arb_discharged_kwh'] > 0
    # dolina 13:00 nie zwiększa arb_discharged ponad to, co oddano w szczycie
    assert r['disch_offpeak_kwh'] == 0.0
    # marża arbitrażu = oddane×peak − dokupione×offpeak
    expected = r['arb_discharged_kwh'] * 1.20 - r['arb_charged_kwh'] * 0.60
    assert r['arb_savings_pln'] == pytest.approx(expected, abs=1e-6)


# ── S2: taryfa dynamiczna ────────────────────────────────────────────────────

def test_s2_prices_hourly_and_negative_clamp():
    cfg = CFG
    hours = {
        _h(MON, 2): (1.0, 0.0),
        _h(MON, 19): (0.0, 1.0),
    }
    rce = {_h(MON, 2): -50.0, _h(MON, 19): 800.0}   # PLN/MWh
    rows = simulate_s2(hours, cfg, rce)
    r = rows[0]
    # ujemna cena → utracona sprzedaż = 0
    assert r['foregone_rcem_pln'] == 0.0
    buy = 0.8 * 1.23 + cfg.dynamic_dist_gross
    assert r['avoided_buy_pln'] == pytest.approx(0.92 * buy, abs=1e-6)


def test_s2_skips_hours_without_rce():
    hours = {_h(MON, 2): (1.0, 0.0), _h(MON, 19): (0.0, 1.0)}
    rows = simulate_s2(hours, CFG, {})
    assert rows == []


# ── S3 / finanse ─────────────────────────────────────────────────────────────

def test_throughput_cost():
    assert throughput_cost_kwh(CFG) == pytest.approx(7599.0 / (4500 * 5.0))


def test_sell_threshold_breakeven():
    rce = {f'2026-06-{d:02d}T12': 900.0 for d in range(1, 31)}
    out = sell_threshold_analysis(rce, CFG, {'2026-06': 0.30})
    tc = throughput_cost_kwh(CFG)
    assert out['breakeven_sell_price_gross'] == pytest.approx((0.30 + tc) / 0.92, abs=1e-4)
    assert out['months_analyzed'] == 1


def test_summarize_payback_and_cycles():
    # 24 zamknięte miesiące po 150 zł → payback ~51 mies. od dziś
    rows = []
    for i in range(24):
        y, m = 2024 + (i // 12), (i % 12) + 1
        rows.append({'ym': f'{y}-{m:02d}', 'savings_pln': 150.0,
                     'charged_kwh': 100.0, 'discharged_kwh': 92.0,
                     'arb_charged_kwh': 0.0, 'arb_discharged_kwh': 0.0})
    s = summarize(rows, CFG, today=date(2026, 6, 15))
    assert s['monthly_avg_savings'] == pytest.approx(150.0)
    assert s['payback_date'] is not None
    assert s['payback_years'] == pytest.approx(7599.0 / (150.0 * 12), abs=0.1)
    assert s['cycles_total'] == pytest.approx(24 * 100.0 / 5.0)
    assert s['margin_per_kwh'] == pytest.approx(150.0 / 92.0, abs=1e-4)
    assert s['npv'] is not None and s['irr_pct'] is not None


def test_simulate_all_start_month_filter_and_payload():
    hours = {
        '2023-05-15T02': (2.0, 0.0),   # przed start_month — odfiltrowane
        _h(MON, 2): (1.0, 0.0),
        _h(MON, 7): (0.0, 1.0),
    }
    out = simulate_all(hours, CFG, ZONE, RCEM, rce_hourly={},
                       today=date(2026, 7, 9))
    assert [r['ym'] for r in out['s1_months']] == ['2026-06']
    assert out['config']['module_price_pln'] == 7599.0
    assert 'summary' in out and 's3' in out


def test_config_from_dict_ignores_junk():
    cfg = BatteryConfig.from_dict({'module_price_pln': '8000', 'nope': 1,
                                   'arbitrage_enabled': 1, 'start_month': '2024-01'})
    assert cfg.module_price_pln == 8000.0
    assert cfg.arbitrage_enabled is True
    assert cfg.start_month == '2024-01'
    assert cfg.usable_kwh == 5.0
