"""
Unit tests for invoice_parser.py

Tests that use the real April 2026 PDF are skipped automatically when the
file is not present on this machine (it lives outside the repo).

All other tests use synthetic text strings to exercise warnings, validation,
multi-pattern fallback, and error conditions without requiring a real PDF.
"""
from __future__ import annotations

import pytest
from pathlib import Path

from pv_roi_tracker.invoice_parser import (
    InvoiceData,
    InvoiceParseError,
    _parse_text,
    _validate,
    parse_invoice,
    parse_invoice_debug,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

_SAMPLE_PDF_PATH = Path(
    '/data/home/.claude/uploads/e3651fae-FV_T_K1_BN567872_0011_26.pdf'
)


def _make_minimal_text(
    period='Okres rozliczeniowy 01.04.2026 - 30.04.2026',
    imported='Pobrano z sieci 215',
    exported='Wprowadzono do sieci 468',
    extra='',
) -> str:
    return f'{period}\n{imported}\n{exported}\n{extra}'


# ── Basic parsing of synthetic minimal invoice ────────────────────────────────

class TestMinimalParse:
    def test_required_fields_extracted(self):
        text = _make_minimal_text()
        data = _parse_text(text)
        assert data.year == 2026
        assert data.month == 4
        assert data.imported_kwh == 215.0
        assert data.exported_kwh == 468.0

    def test_missing_period_raises(self):
        text = 'Pobrano z sieci 215\nWprowadzono do sieci 468'
        with pytest.raises(InvoiceParseError, match='Okres rozliczeniowy'):
            _parse_text(text)

    def test_missing_imported_raises(self):
        text = 'Okres rozliczeniowy 01.04.2026 - 30.04.2026\nWprowadzono do sieci 468'
        with pytest.raises(InvoiceParseError, match='Pobrano z sieci'):
            _parse_text(text)

    def test_missing_exported_raises(self):
        text = 'Okres rozliczeniowy 01.04.2026 - 30.04.2026\nPobrano z sieci 215'
        with pytest.raises(InvoiceParseError, match='Wprowadzono do sieci'):
            _parse_text(text)

    def test_optional_fields_none_when_missing(self):
        data = _parse_text(_make_minimal_text())
        assert data.peak_gross is None
        assert data.offpeak_gross is None
        assert data.blended_gross is None
        assert data.fixed_total_net is None
        assert data.amount_due_pln is None

    def test_warnings_generated_for_missing_optional_fields(self):
        data = _parse_text(_make_minimal_text())
        assert len(data.warnings) > 0
        # Key warnings that should always appear on a minimal text
        warning_text = ' '.join(data.warnings)
        assert 'peak_gross' in warning_text or 'szczyt' in warning_text

    def test_decimal_comma_parsing(self):
        text = _make_minimal_text(imported='Pobrano z sieci 215,5',
                                   exported='Wprowadzono do sieci 468,3')
        data = _parse_text(text)
        assert data.imported_kwh == 215.5
        assert data.exported_kwh == 468.3


# ── Multi-pattern fallback ────────────────────────────────────────────────────

class TestMultiPatternFallback:
    """Verify alternate label wordings are matched via the fallback list."""

    def test_pobrane_fallback(self):
        """'Pobrane z sieci' (alternate form) should parse as imported."""
        text = ('Okres rozliczeniowy 01.04.2026 - 30.04.2026\n'
                'Pobrane z sieci 100\nWprowadzono do sieci 50')
        data = _parse_text(text)
        assert data.imported_kwh == 100.0

    def test_oddano_fallback(self):
        """'Oddano do sieci' (alternate form) should parse as exported."""
        text = ('Okres rozliczeniowy 01.04.2026 - 30.04.2026\n'
                'Pobrano z sieci 100\nOddano do sieci 50')
        data = _parse_text(text)
        assert data.exported_kwh == 50.0

    def test_invoice_number_alternate_label(self):
        text = _make_minimal_text(extra='Nr faktury T/K1/TEST/0001/26')
        data = _parse_text(text)
        assert data.invoice_number == 'T/K1/TEST/0001/26'

    def test_amount_due_alternate_label(self):
        text = _make_minimal_text(extra='Razem do zapłaty 150,00')
        data = _parse_text(text)
        assert data.amount_due_pln == 150.0


# ── Warning generation ────────────────────────────────────────────────────────

class TestWarnings:
    def test_missing_distribution_generates_warning(self):
        """A text with no distribution section → warnings about variable components."""
        data = _parse_text(_make_minimal_text())
        warning_text = ' '.join(data.warnings).lower()
        assert 'sieciowy' in warning_text or 'zmienny' in warning_text

    def test_missing_fixed_charges_generates_warning(self):
        data = _parse_text(_make_minimal_text())
        warning_text = ' '.join(data.warnings).lower()
        assert 'stała' in warning_text or 'fixed_total' in warning_text or 'opłat' in warning_text

    def test_warnings_are_unique(self):
        """Duplicate warning strings should be deduplicated."""
        data = _parse_text(_make_minimal_text())
        assert len(data.warnings) == len(set(data.warnings))

    def test_no_extra_gross_warning_when_gross_already_warned(self):
        """When peak_gross can't be computed, the exact warning appears exactly once."""
        data = _parse_text(_make_minimal_text())
        # Use startswith so 'offpeak_gross' warnings don't match
        peak_gross_warnings = [w for w in data.warnings if w.startswith('peak_gross')]
        assert len(peak_gross_warnings) <= 1


# ── Validation sanity-checks ──────────────────────────────────────────────────

class TestValidation:
    def _data_with_split_mismatch(self) -> InvoiceData:
        """A parsed object where peak+offpeak doesn't add up to the total."""
        text = _make_minimal_text()
        data = _parse_text(text)
        # Manually inject inconsistent values
        data.imported_kwh = 215.0
        data.imported_kwh_peak = 14.0
        data.imported_kwh_offpeak = 180.0   # 14+180=194 ≠ 215
        # Re-run validation
        data.warnings = [w for w in data.warnings
                         if 'walidacja' not in w]  # remove old validation warnings
        data.warnings.extend(_validate(data))
        return data

    def test_import_split_mismatch_warning(self):
        data = self._data_with_split_mismatch()
        val_warns = [w for w in data.warnings if 'walidacja' in w and 'pobrano' in w]
        assert len(val_warns) >= 1

    def test_deposit_arithmetic_mismatch_warning(self):
        text = _make_minimal_text()
        data = _parse_text(text)
        data.deposit_current_pln = 50.0
        data.deposit_previous_pln = 30.0
        data.deposit_used_pln = 100.0   # 50+30=80 ≠ 100
        data.warnings.extend(_validate(data))
        val_warns = [w for w in data.warnings if 'depozyt' in w and 'walidacja' in w]
        assert len(val_warns) >= 1

    def test_implausible_avg_price_warning(self):
        text = _make_minimal_text()
        data = _parse_text(text)
        data.avg_price_pln_kwh = 50.0  # clearly wrong
        data.warnings.extend(_validate(data))
        val_warns = [w for w in data.warnings if 'średnia cena' in w and 'walidacja' in w]
        assert len(val_warns) >= 1

    def test_clean_data_no_validation_warnings(self):
        """Data matching the April 2026 values should produce no validation warnings."""
        text = _make_minimal_text()
        data = _parse_text(text)
        data.imported_kwh = 215.0
        data.imported_kwh_peak = 14.0
        data.imported_kwh_offpeak = 201.0
        data.exported_kwh = 468.0
        data.exported_kwh_peak = 230.0
        data.exported_kwh_offpeak = 238.0
        data.deposit_current_pln = 44.14
        data.deposit_previous_pln = 28.34
        data.deposit_used_pln = 72.48
        data.avg_price_pln_kwh = 0.90
        val_warns = _validate(data)
        assert val_warns == []


# ── parse_invoice_debug ───────────────────────────────────────────────────────

class TestDebug:
    def test_debug_returns_text_and_fields_on_valid_parse(self):
        """Monkey-patch _extract_text to avoid needing a real PDF."""
        import pv_roi_tracker.invoice_parser as _mod
        minimal = _make_minimal_text()
        orig = _mod._extract_text
        try:
            _mod._extract_text = lambda _: minimal
            result = parse_invoice_debug(b'fake-pdf')
            assert result['ok'] is True
            assert 'text' in result
            assert result['text']   # non-empty
            assert 'fields' in result
            assert result['fields']['year'] == 2026
            assert result['fields']['month'] == 4
            assert 'warnings' in result
        finally:
            _mod._extract_text = orig

    def test_debug_returns_ok_false_on_bad_pdf(self):
        result = parse_invoice_debug(b'not-a-pdf')
        assert result['ok'] is False
        assert 'error' in result


# ── Old invoice format (stary wzór, pre-2026) ─────────────────────────────────

def _make_old_format_text() -> str:
    """Synthetic text mimicking the old Tauron invoice layout (e.g. T/K1/0951283/25)."""
    return (
        'Podsumowanie faktury VAT\n'
        'T/K1/0951283/25\n'
        'Za okres\n'
        'Od 01/10/2025 do 31/10/2025\n'
        'Pobrano z sieci\n'
        '300 kWh\n'
        'Wprowadzono do sieci\n'
        '101 kWh\n'
        'Do zapøaty\n'
        '92,78 zø\n'
        'FAKTURA VAT NR T/K1/0951283/25 - wystawiona w formie elektronicznej\n'
        'za okres od 01/10/2025 do 31/10/2025\n'
        '5. Depozyt prosumencki w rozliczanym okresie (z.) 0,00\n'
        '6. Depozyt prosumencki z okres.w poprzednich (z.) 185,37\n'
        '7. Rozliczenie depozytu ( 4 + 5 + 6 ) 185,37\n'
        '8. Do zap.aty (z.) ( 3 - 7 ) 92,78\n'
        'Licznik energii elektrycznej (oddanie)\n'
        'nr 312186091817\n'
        'szczyt 31/10/2025 (Z) 52,0000\n'
        'pozaszczytowa 31/10/2025 (Z) 49,0000\n'
        'Rozliczenie sprzeda.y energii elektrycznej za okres od 01/10/2025 do 31/10/2025\n'
        'Energia czynna\n'
        'szczytowa kWh 62 0,76100 47,18 23 10,85 58,03\n'
        'pozaszczytowa kWh 238 0,43500 103,53 23 23,81 127,34\n'
        'Sk.adnik sta.y stawki sieciowej mc 1 10,34000 10,34 23 2,38 12,72\n'
        'Stawka jako.ciowa\n'
        'szczytowa kWh 62 0,03210 1,99 23 0,46 2,45\n'
        'pozaszczytowa kWh 238 0,03210 7,64 23 1,76 9,40\n'
        'Sk.adnik zmienny stawki sieciowej\n'
        'szczytowa kWh 62 0,32710 20,28 23 4,66 24,94\n'
        'pozaszczytowa kWh 238 0,05180 12,33 23 2,84 15,17\n'
        'Op.ata OZE\n'
        'szczytowa kWh 62 1 0,00350 0,22 23 0,05 0,27\n'
        'pozaszczytowa kWh 238 1 0,00350 0,83 23 0,19 1,02\n'
        'Op.ata kogeneracyjna\n'
        'szczyt kWh 62 1 0,00300 0,19 23 0,04 0,23\n'
        'pozaszczyt kWh 238 1 0,00300 0,71 23 0,16 0,87\n'
        'Stawka op.aty abonamentowej z./mc 1 4,56000 4,56 23 1,05 5,61\n'
        'Op.ata mocowa z./mc 1 16,01000 16,01 23 3,68 19,69\n'
        '3. rednia cena brutto 1 kWh 0,93 z./kWh\n'
    )


class TestOldFormat:
    """Parser handles the pre-2026 Tauron invoice layout (stary wzór)."""

    @pytest.fixture(scope='class')
    def parsed(self):
        return _parse_text(_make_old_format_text())

    def test_billing_period(self, parsed):
        assert parsed.year == 2025
        assert parsed.month == 10

    def test_billing_period_no_warning(self, parsed):
        period_warns = [w for w in parsed.warnings if 'okres rozliczeniowy' in w.lower() or 'wydedukowano' in w]
        assert period_warns == [], f'Unexpected period warnings: {period_warns}'

    def test_billing_period_raw(self, parsed):
        assert parsed.billing_period_raw is not None
        assert '10/2025' in parsed.billing_period_raw

    def test_invoice_number(self, parsed):
        assert parsed.invoice_number == 'T/K1/0951283/25'

    def test_imported_kwh(self, parsed):
        assert parsed.imported_kwh == pytest.approx(300.0)

    def test_exported_kwh(self, parsed):
        assert parsed.exported_kwh == pytest.approx(101.0)

    def test_imp_peak(self, parsed):
        assert parsed.imported_kwh_peak == pytest.approx(62.0)

    def test_imp_offpeak(self, parsed):
        assert parsed.imported_kwh_offpeak == pytest.approx(238.0)

    def test_exp_peak(self, parsed):
        assert parsed.exported_kwh_peak == pytest.approx(52.0)

    def test_exp_offpeak(self, parsed):
        assert parsed.exported_kwh_offpeak == pytest.approx(49.0)

    def test_energy_peak_net(self, parsed):
        assert parsed.energy_peak_net == pytest.approx(0.761, abs=0.001)

    def test_energy_offpeak_net(self, parsed):
        assert parsed.energy_offpeak_net == pytest.approx(0.435, abs=0.001)

    def test_dist_var_peak(self, parsed):
        assert parsed.dist_var_peak_net == pytest.approx(0.32710, abs=0.0001)

    def test_dist_var_offpeak(self, parsed):
        assert parsed.dist_var_offpeak_net == pytest.approx(0.05180, abs=0.0001)

    def test_dist_jakosciowa(self, parsed):
        assert parsed.dist_jakosciowa_net == pytest.approx(0.03210, abs=0.0001)

    def test_dist_oze(self, parsed):
        assert parsed.dist_oze_net == pytest.approx(0.00350, abs=0.0001)

    def test_dist_kogeneracja(self, parsed):
        assert parsed.dist_kogeneracja_net == pytest.approx(0.00300, abs=0.0001)

    def test_fixed_mocowa(self, parsed):
        assert parsed.fixed_mocowa_net == pytest.approx(16.01, abs=0.01)

    def test_fixed_abonament(self, parsed):
        assert parsed.fixed_abonament_net == pytest.approx(4.56, abs=0.01)

    def test_fixed_stalysieciowy(self, parsed):
        assert parsed.fixed_stalysieciowy_net == pytest.approx(10.34, abs=0.01)

    def test_fixed_total_net(self, parsed):
        assert parsed.fixed_total_net == pytest.approx(30.91, abs=0.01)

    def test_deposit_current(self, parsed):
        assert parsed.deposit_current_pln == pytest.approx(0.0, abs=0.01)

    def test_deposit_previous(self, parsed):
        assert parsed.deposit_previous_pln == pytest.approx(185.37, abs=0.01)

    def test_deposit_used(self, parsed):
        assert parsed.deposit_used_pln == pytest.approx(185.37, abs=0.01)

    def test_amount_due(self, parsed):
        assert parsed.amount_due_pln == pytest.approx(92.78, abs=0.01)

    def test_avg_price(self, parsed):
        assert parsed.avg_price_pln_kwh == pytest.approx(0.93, abs=0.01)

    def test_peak_gross_computed(self, parsed):
        # (0,761 + 0,32710 + 0,03210 + 0,00350 + 0,00300) * 1.23
        expected = (0.76100 + 0.32710 + 0.03210 + 0.00350 + 0.00300) * 1.23
        assert parsed.peak_gross == pytest.approx(expected, abs=0.001)

    def test_offpeak_gross_computed(self, parsed):
        expected = (0.43500 + 0.05180 + 0.03210 + 0.00350 + 0.00300) * 1.23
        assert parsed.offpeak_gross == pytest.approx(expected, abs=0.001)

    def test_no_field_warnings(self, parsed):
        """All fields extractable — only validation warnings (if any) are acceptable."""
        field_warns = [w for w in parsed.warnings if 'nie znalezion' in w or 'nieobliczony' in w or 'nieznalezion' in w]
        assert field_warns == [], f'Unexpected field warnings: {field_warns}'


# ── Real PDF tests (skipped if PDF absent) ────────────────────────────────────

@pytest.mark.skipif(not _SAMPLE_PDF_PATH.exists(),
                    reason='April 2026 sample PDF not available')
class TestRealPdf:
    @pytest.fixture(scope='class')
    def parsed(self):
        return parse_invoice(_SAMPLE_PDF_PATH.read_bytes())

    def test_billing_period(self, parsed):
        assert parsed.year == 2026
        assert parsed.month == 4

    def test_invoice_number(self, parsed):
        assert parsed.invoice_number is not None
        assert '567872' in parsed.invoice_number

    def test_imported_kwh(self, parsed):
        assert parsed.imported_kwh == pytest.approx(215, abs=2)

    def test_exported_kwh(self, parsed):
        assert parsed.exported_kwh == pytest.approx(468, abs=2)

    def test_peak_import_kwh(self, parsed):
        assert parsed.imported_kwh_peak == pytest.approx(14, abs=2)

    def test_offpeak_import_kwh(self, parsed):
        assert parsed.imported_kwh_offpeak == pytest.approx(201, abs=2)

    def test_peak_gross_rate(self, parsed):
        assert parsed.peak_gross == pytest.approx(1.2304, abs=0.01)

    def test_offpeak_gross_rate(self, parsed):
        assert parsed.offpeak_gross == pytest.approx(0.6306, abs=0.01)

    def test_deposit_used(self, parsed):
        assert parsed.deposit_used_pln == pytest.approx(72.48, abs=0.50)

    def test_amount_due(self, parsed):
        assert parsed.amount_due_pln == pytest.approx(120.04, abs=1.0)

    def test_fixed_total_net(self, parsed):
        assert parsed.fixed_total_net == pytest.approx(39.47, abs=1.0)

    def test_zero_warnings_on_clean_pdf(self, parsed):
        """A correct Tauron invoice should parse with no warnings."""
        assert parsed.warnings == [], f'Unexpected warnings: {parsed.warnings}'

    def test_debug_parses_same_as_parse(self, parsed):
        result = parse_invoice_debug(_SAMPLE_PDF_PATH.read_bytes())
        assert result['ok'] is True
        assert result['fields']['year'] == parsed.year
        assert result['fields']['imported_kwh'] == parsed.imported_kwh
        assert result['warnings'] == []
