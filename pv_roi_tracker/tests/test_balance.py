"""Tests for balance.py — cross-family production plausibility check (v0.35.0,
revised design).

An earlier design computed R1/R2 residuals from produced/exported/
self_consumed/consumed/purchased on a single record and treated them as an
energy-balance proof. That was wrong for this installation: there is no
independent whole-house meter here, so self_consumed_kwh (= produced -
exported) and consumed_kwh (= self_consumed + imported) are algebraically
derived from the same primitives that feed produced_kwh/exported_kwh/
purchased_kwh — any such residual is tautologically zero by construction,
never real evidence of anything. See balance.py's module docstring.

The check that DOES carry information: this system has two independently-
sourced solar-production tracking chains (the Energy-Dashboard-configured
sensor.energy_pv, and the Huawei integration's own sensor.inverter_total_yield)
that genuinely disagree by a few percent most months. compute_balance()
compares produced_kwh (dashboard family) against cross_family_produced_kwh
(template family) on the same record.
"""
from __future__ import annotations

import pytest

from pv_roi_tracker import balance
from pv_roi_tracker.models import MonthlyRecord


def _rec(produced_kwh=None, cross_family_produced_kwh=None, **kwargs):
    defaults = dict(year=2026, month=7)
    defaults.update(kwargs)
    return MonthlyRecord(produced_kwh=produced_kwh,
                         cross_family_produced_kwh=cross_family_produced_kwh,
                         **defaults)


# ── compute_balance ───────────────────────────────────────────────────────────

def test_compute_balance_incomplete_when_cross_family_missing():
    """Most records (pre-0.35.0, or a failed extra LTS fetch) won't have
    cross_family_produced_kwh — must not be treated as a breach."""
    rec = _rec(produced_kwh=862.02, cross_family_produced_kwh=None)
    b = balance.compute_balance(rec)
    assert b['reason'] == 'incomplete'
    assert b['ok'] is True
    assert b['diff_kwh'] is None
    assert b['diff_pct'] is None


def test_compute_balance_within_normal_drift_is_ok():
    """Reproduces the confirmed March 2026 measurement: energy_pv=643.98 vs
    inverter_total_yield=685.53 — a real ~6.5% difference between the two
    tracking families, within the normal observed range (0.6-6.5%), not a
    breach."""
    rec = _rec(produced_kwh=643.98, cross_family_produced_kwh=685.53)
    b = balance.compute_balance(rec)
    assert b['reason'] == 'ok'
    assert b['ok'] is True
    assert b['diff_pct'] == pytest.approx(6.45, abs=0.1)


def test_compute_balance_large_divergence_breaches():
    """A divergence well beyond normal dashboard-vs-template drift is worth
    surfacing — not proof either family is wrong, but worth a look."""
    rec = _rec(produced_kwh=500.0, cross_family_produced_kwh=650.0)  # 30% off
    b = balance.compute_balance(rec)
    assert b['reason'] == 'breach'
    assert b['ok'] is False
    assert b['diff_pct'] == pytest.approx(30.0, abs=0.1)


def test_compute_balance_just_under_tolerance_is_ok():
    rec = _rec(produced_kwh=1000.0, cross_family_produced_kwh=1099.0)  # 9.9%
    b = balance.compute_balance(rec)
    assert b['ok'] is True


def test_compute_balance_just_over_tolerance_breaches():
    rec = _rec(produced_kwh=1000.0, cross_family_produced_kwh=1101.0)  # 10.1%
    b = balance.compute_balance(rec)
    assert b['ok'] is False
    assert b['reason'] == 'breach'


def test_compute_balance_zero_produced_no_crash():
    rec = _rec(produced_kwh=0.0, cross_family_produced_kwh=0.0)
    b = balance.compute_balance(rec)
    assert b['ok'] is True
    assert b['diff_pct'] == 0.0


# ── residual_kwh ──────────────────────────────────────────────────────────────

def test_residual_kwh_is_absolute_diff():
    rec = _rec(produced_kwh=643.98, cross_family_produced_kwh=685.53)
    assert balance.residual_kwh(rec) == pytest.approx(41.55, abs=0.01)


def test_residual_kwh_none_when_incomplete():
    rec = _rec(produced_kwh=643.98, cross_family_produced_kwh=None)
    assert balance.residual_kwh(rec) is None


# ── check_all ─────────────────────────────────────────────────────────────────

def test_check_all_flags_only_breaching_months():
    ok_month = _rec(year=2026, month=6, produced_kwh=643.98, cross_family_produced_kwh=685.53)
    bad_month = _rec(year=2026, month=7, produced_kwh=500.0, cross_family_produced_kwh=650.0)
    result = balance.check_all([ok_month, bad_month])
    assert result['ok'] is False
    assert len(result['breaches']) == 1
    assert result['breaches'][0]['ym'] == '2026-07'


def test_check_all_empty_breaches_when_all_ok():
    ok_month = _rec(produced_kwh=643.98, cross_family_produced_kwh=685.53)
    result = balance.check_all([ok_month])
    assert result['ok'] is True
    assert result['breaches'] == []


def test_check_all_ignores_incomplete_records():
    """A record without a computed cross-family figure must not be reported
    as a breach."""
    incomplete = _rec(produced_kwh=643.98, cross_family_produced_kwh=None)
    result = balance.check_all([incomplete])
    assert result['ok'] is True
    assert result['breaches'] == []
