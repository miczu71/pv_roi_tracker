"""
Unit tests for invoice_parser.py.

Uses the real Tauron PDF (April 2026, invoice T/K1/BN567872/0011/26) when
available at the fixed path below; skips gracefully if the file is absent
(CI environments).  All expected values come directly from the invoice text.
"""
from __future__ import annotations

import os
import pytest

SAMPLE_PDF = (
    '/data/home/.claude/uploads/'
    'e051e8b1-45e6-4c3b-aed4-cd54d75b6cb4/'
    'e3651fae-FV_TAURON_TPE_60567872_TK1BN567872001126_a12932344f7f4b76ff1b4d67b5f5247a.pdf'
)

pytestmark = pytest.mark.skipif(
    not os.path.exists(SAMPLE_PDF),
    reason='Sample invoice PDF not present',
)


@pytest.fixture(scope='module')
def inv():
    from pv_roi_tracker.invoice_parser import parse_invoice
    return parse_invoice(open(SAMPLE_PDF, 'rb').read())


# ── Billing identity ──────────────────────────────────────────────────────────

def test_year_month(inv):
    assert inv.year == 2026
    assert inv.month == 4


def test_invoice_number(inv):
    assert inv.invoice_number == 'T/K1/BN567872/0011/26'


# ── kWh totals ────────────────────────────────────────────────────────────────

def test_imported_kwh(inv):
    assert inv.imported_kwh == pytest.approx(215.0)


def test_exported_kwh(inv):
    assert inv.exported_kwh == pytest.approx(468.0)


def test_imported_peak_offpeak(inv):
    assert inv.imported_kwh_peak    == pytest.approx(14.0)
    assert inv.imported_kwh_offpeak == pytest.approx(201.0)


def test_exported_peak_offpeak(inv):
    assert inv.exported_kwh_peak    == pytest.approx(230.0)
    assert inv.exported_kwh_offpeak == pytest.approx(238.0)


# ── Tariff rates ──────────────────────────────────────────────────────────────

def test_energy_net_prices(inv):
    assert inv.energy_peak_net    == pytest.approx(0.627)
    assert inv.energy_offpeak_net == pytest.approx(0.418)


def test_dist_variable_rates(inv):
    assert inv.dist_var_peak_net    == pytest.approx(0.3298)
    assert inv.dist_var_offpeak_net == pytest.approx(0.0512)


def test_dist_jakosciowa(inv):
    assert inv.dist_jakosciowa_net == pytest.approx(0.0332)


def test_dist_oze_kogeneracja(inv):
    assert inv.dist_oze_net         == pytest.approx(0.0073)
    assert inv.dist_kogeneracja_net == pytest.approx(0.003)


def test_peak_gross_rate(inv):
    """Computed peak gross should reproduce Tauron's effective per-kWh cost ~1.218 zł/kWh."""
    # (0.627 + 0.3298 + 0.0332 + 0.0073 + 0.003) × 1.23 = 1.2304
    assert inv.peak_gross == pytest.approx(1.2304, abs=0.001)


def test_offpeak_gross_rate(inv):
    """Offpeak gross ~0.631 zł/kWh."""
    assert inv.offpeak_gross == pytest.approx(0.6306, abs=0.001)


# ── Fixed monthly charges ─────────────────────────────────────────────────────

def test_fixed_charges(inv):
    assert inv.fixed_mocowa_net        == pytest.approx(24.05)
    assert inv.fixed_abonament_net     == pytest.approx(4.56)
    assert inv.fixed_stalysieciowy_net == pytest.approx(10.86)
    assert inv.fixed_total_net         == pytest.approx(39.47, abs=0.01)


# ── Prosument deposit ─────────────────────────────────────────────────────────

def test_deposit(inv):
    assert inv.deposit_current_pln  == pytest.approx(0.00)
    assert inv.deposit_previous_pln == pytest.approx(72.48)
    assert inv.deposit_used_pln     == pytest.approx(72.48)


# ── Financial summary ─────────────────────────────────────────────────────────

def test_amount_due(inv):
    assert inv.amount_due_pln == pytest.approx(120.04)


def test_avg_price(inv):
    assert inv.avg_price_pln_kwh == pytest.approx(0.90)


# ── InvoiceParseError on garbage input ───────────────────────────────────────

def test_invalid_pdf_raises():
    from pv_roi_tracker.invoice_parser import parse_invoice, InvoiceParseError
    with pytest.raises(InvoiceParseError):
        parse_invoice(b'not a pdf')
