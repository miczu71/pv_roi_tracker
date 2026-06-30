"""
Testy regresji dla poprawki strefy czasowej (v0.28.0).

Pokrywa:
1. _build_record() — spójność wyliczeń derived fields.
2. read_current_month() guard: produced <= _MIN_PRODUCED_KWH przy day=1 → None.
3. historic_store.replace_month() — nadpisywanie istniejącego rekordu zerowego.
4. historic_store.replace_month() — dopisywanie gdy brak rekordu.
5. historic_store.replace_month() — zachowanie tariff i rcem_status ze starego rekordu.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import date

import pytest

from pv_roi_tracker.models import MonthlyRecord
from pv_roi_tracker import historic_store
import pv_roi_tracker.live_reader as lr


# ── Helpers ───────────────────────────────────────────────────────────────────

def _zero_record(year, month) -> MonthlyRecord:
    """Symuluje zerowy rekord z błędnego month_close (licznik po resecie)."""
    return MonthlyRecord(year=year, month=month, produced_kwh=0.0, exported_kwh=0.0,
                         self_consumed_savings_pln=0.0, feedin_revenue_pln=0.0,
                         rcem_status='pending')


def _real_record(year, month) -> MonthlyRecord:
    return MonthlyRecord(year=year, month=month, produced_kwh=850.5, exported_kwh=320.0,
                         self_consumed_savings_pln=660.0, feedin_revenue_pln=None,
                         rcem_status='pending')


# ── 1. _build_record — derived fields ────────────────────────────────────────

def test_build_record_derived_fields():
    """_build_record oblicza self_consumed_kwh, purchase_cost, savings poprawnie."""
    # Patchujemy _get_state żeby nie wywoływać HA API
    with patch.object(lr, '_get_state', side_effect=lambda eid: None):
        rec = lr._build_record(
            year=2026, month=6,
            produced=850.0,
            exported=320.0,
            consumed=400.0,
            peak=210.0,
            offpeak=190.0,
            arb_kwh=60.0,
            rcem_price=0.35,
        )
    assert rec.produced_kwh == 850.0
    assert rec.exported_kwh == 320.0
    assert rec.self_consumed_kwh == pytest.approx(850.0 - 320.0, abs=0.01)
    assert rec.feedin_revenue_pln == pytest.approx(320.0 * 0.35, abs=0.01)
    # buy_price z taryfy G12W: (210×1.23 + 190×0.63) / 400
    expected_buy = (210 * 1.23 + 190 * 0.63) / (210 + 190)
    assert rec.buy_price_pln_kwh == pytest.approx(expected_buy, abs=0.001)
    expected_sc_savings = (850.0 - 320.0) * expected_buy
    assert rec.self_consumed_savings_pln == pytest.approx(expected_sc_savings, abs=0.1)
    assert rec.rcem_status == 'confirmed'


def test_build_record_no_rcem_pending():
    """Bez rcem_price rcem_status='pending' i feedin_revenue_pln=None."""
    with patch.object(lr, '_get_state', side_effect=lambda eid: None):
        rec = lr._build_record(
            year=2026, month=6,
            produced=400.0, exported=100.0, consumed=None,
            peak=None, offpeak=None, arb_kwh=None,
            rcem_price=None,
        )
    assert rec.rcem_status == 'pending'
    assert rec.feedin_revenue_pln is None


# ── 2. read_current_month guard: zerowe liczniki w day=1 → None ───────────────

def test_read_current_month_returns_none_when_reset():
    """Jeśli produced <= 5 kWh w day=1 → return None (licznik po resecie)."""
    with (
        patch.object(lr, '_get_state', return_value=0.0),
        patch('pv_roi_tracker.live_reader.date') as mock_date,
    ):
        mock_date.today.return_value = date(2026, 7, 1)
        result = lr.read_current_month(rcem_price=None)
    assert result is None, 'Zerowy odczyt w day=1 powinien zwrócić None'


def test_read_current_month_returns_none_when_low_produced_day1():
    """produced=2.5 kWh w day=1 to też reset — zwróć None."""
    states = {
        'sensor.inverter_yield_monthly': 2.5,
        'sensor.power_meter_exported_energy_monthly': 0.0,
        'sensor.house_consumption_energy_monthly': 0.0,
        'sensor.monthly_energy_peak': 0.0,
        'sensor.monthly_energy_offpeak': 0.0,
        'sensor.battery_grid_charge_off_peak_monthly': 0.0,
    }
    with (
        patch.object(lr, '_get_state', side_effect=lambda eid: states.get(eid, None)),
        patch('pv_roi_tracker.live_reader.date') as mock_date,
    ):
        mock_date.today.return_value = date(2026, 7, 1)
        result = lr.read_current_month(rcem_price=None)
    assert result is None


def test_read_current_month_ok_when_produced_above_threshold():
    """produced=200 kWh w day=1 — prawdziwy odczyt, nie reset."""
    states = {
        'sensor.inverter_yield_monthly': 200.0,
        'sensor.power_meter_exported_energy_monthly': 80.0,
        'sensor.house_consumption_energy_monthly': 250.0,
        'sensor.monthly_energy_peak': 120.0,
        'sensor.monthly_energy_offpeak': 100.0,
        'sensor.battery_grid_charge_off_peak_monthly': 30.0,
    }
    with (
        patch.object(lr, '_get_state', side_effect=lambda eid: states.get(eid, None)),
        patch.object(lr, '_solcast_month_projection', return_value=None),
        patch('pv_roi_tracker.live_reader.date') as mock_date,
    ):
        mock_date.today.return_value = date(2026, 7, 1)
        result = lr.read_current_month(rcem_price=None)
    assert result is not None
    assert result.produced_kwh == 200.0


def test_read_current_month_ok_after_day1():
    """Guard nie blokuje day=15 nawet gdy produced jest mały."""
    with (
        patch.object(lr, '_get_state', return_value=3.0),
        patch.object(lr, '_solcast_month_projection', return_value=None),
        patch('pv_roi_tracker.live_reader.date') as mock_date,
    ):
        mock_date.today.return_value = date(2026, 7, 15)
        result = lr.read_current_month(rcem_price=None)
    # produced=3.0 > 0 i day≠1, więc NIE powinno zwrócić None z powodu guardu
    # (może zwrócić None jeśli _build_record daje None z innych powodów, ale nie z guardu)
    # Sprawdzamy że guard nie zadziałał — produced wraca jako 3.0
    assert result is not None
    assert result.produced_kwh == 3.0


# ── 3. replace_month — nadpisywanie zerowego rekordu ─────────────────────────

@pytest.fixture
def store(tmp_path) -> Path:
    return tmp_path / 'historic.json'


def test_replace_month_overwrites_zero_record(store):
    """Zerowy rekord z month_close powinien być zastąpiony prawdziwymi danymi."""
    zero = _zero_record(2026, 6)
    historic_store.append_month(zero, store)

    real = _real_record(2026, 6)
    replaced = historic_store.replace_month(real, store)

    assert replaced is True  # rekord istniał
    records = historic_store.load(store)
    june = next(r for r in records if r.year == 2026 and r.month == 6)
    assert june.produced_kwh == pytest.approx(850.5)
    assert june.self_consumed_savings_pln == pytest.approx(660.0)


def test_replace_month_appends_when_absent(store):
    """Gdy brak rekordu replace_month dopisuje go i zwraca False."""
    real = _real_record(2026, 6)
    replaced = historic_store.replace_month(real, store)

    assert replaced is False  # rekord nie istniał
    records = historic_store.load(store)
    assert any(r.year == 2026 and r.month == 6 for r in records)


def test_replace_month_preserves_tariff_and_rcem_status(store):
    """replace_month zachowuje tariff i rcem_status gdy nowy rekord ich nie ma."""
    old = MonthlyRecord(year=2026, month=6, produced_kwh=0.0, tariff='G12W',
                        rcem_status='confirmed', feedin_price_pln_kwh=0.35)
    historic_store.append_month(old, store)

    # Nowy rekord z backfill — rcem_status='pending', tariff=None
    new = MonthlyRecord(year=2026, month=6, produced_kwh=850.0, rcem_status='pending',
                        tariff=None)
    historic_store.replace_month(new, store)

    records = historic_store.load(store)
    june = next(r for r in records if r.year == 2026 and r.month == 6)
    # tariff ze starego rekordu powinien zostać
    assert june.tariff == 'G12W'
    # rcem_status: nowy ma 'pending' (nie None) → nie zastępowany starym
    assert june.rcem_status == 'pending'


def test_replace_month_does_not_disturb_other_months(store):
    """replace_month nie modyfikuje innych miesięcy."""
    may = MonthlyRecord(year=2026, month=5, produced_kwh=700.0)
    zero_june = _zero_record(2026, 6)
    historic_store.append_month(may, store)
    historic_store.append_month(zero_june, store)

    historic_store.replace_month(_real_record(2026, 6), store)

    records = historic_store.load(store)
    may_rec = next(r for r in records if r.year == 2026 and r.month == 5)
    assert may_rec.produced_kwh == pytest.approx(700.0)
