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
