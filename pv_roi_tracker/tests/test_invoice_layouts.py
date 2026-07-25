"""
Unit tests for invoice_layouts.py — layout derivation and storage.
"""
from __future__ import annotations

import json
import pytest
import tempfile
from pathlib import Path

from pv_roi_tracker.invoice_layouts import (
    derive_pattern,
    derive_and_store_patterns,
    add_patterns,
    learned_for,
    clear,
    summary,
    load,
)


# ── derive_pattern ────────────────────────────────────────────────────────────

class TestDerivePattern:
    def test_derives_pattern_for_float_in_context(self):
        text = 'Pobrane z sieci 215\nInne dane 123'
        pat = derive_pattern(text, 215.0, 'imp_total')
        assert pat is not None
        # Pattern must re-extract the value
        import re
        m = re.search(pat, text, re.IGNORECASE)
        assert m is not None
        assert abs(float(m.group(1).replace(',', '.')) - 215.0) < 0.01

    def test_returns_none_when_value_not_in_text(self):
        text = 'Pobrano z sieci 100\nRazem 200'
        pat = derive_pattern(text, 999.0, 'imp_total')
        assert pat is None

    def test_handles_polish_decimal(self):
        text = 'Energia szczytowa 14 kWh 0,627\nDane'
        pat = derive_pattern(text, 0.627, 'energy_peak_net')
        assert pat is not None

    def test_tolerates_alternate_label_wording(self):
        text = 'Oddano do sieci 468\nInne'
        pat = derive_pattern(text, 468.0, 'exp_total')
        assert pat is not None
        import re
        m = re.search(pat, text, re.IGNORECASE)
        assert m is not None

    def test_integer_value_as_string(self):
        """Integer value 215 should appear without decimal in text."""
        text = 'Pobrane z sieci 215 kWh'
        pat = derive_pattern(text, 215.0, 'imp_total')
        assert pat is not None


# ── add_patterns / learned_for / clear ───────────────────────────────────────

class TestStore:
    def test_add_and_read(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            path = Path(f.name)
        path.unlink(missing_ok=True)
        try:
            add_patterns('imp_total', [r'Pobrane z sieci\s+([\d,]+)'], path)
            learned = learned_for('imp_total', path)
            assert r'Pobrane z sieci\s+([\d,]+)' in learned
        finally:
            path.unlink(missing_ok=True)

    def test_deduplication(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            path = Path(f.name)
        path.unlink(missing_ok=True)
        try:
            add_patterns('imp_total', [r'PatternA'], path)
            add_patterns('imp_total', [r'PatternA', r'PatternB'], path)
            learned = learned_for('imp_total', path)
            assert learned.count(r'PatternA') == 1
            assert r'PatternB' in learned
        finally:
            path.unlink(missing_ok=True)

    def test_clear_empties_fields(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            path = Path(f.name)
        path.unlink(missing_ok=True)
        try:
            add_patterns('imp_total', [r'SomePattern'], path)
            assert learned_for('imp_total', path)
            clear(path)
            assert learned_for('imp_total', path) == []
        finally:
            path.unlink(missing_ok=True)

    def test_summary_counts(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as f:
            path = Path(f.name)
        path.unlink(missing_ok=True)
        try:
            add_patterns('imp_total', [r'P1', r'P2'], path)
            add_patterns('exp_total', [r'P3'], path)
            s = summary(path)
            assert s['field_counts']['imp_total'] == 2
            assert s['field_counts']['exp_total'] == 1
        finally:
            path.unlink(missing_ok=True)


# ── Parser integration: learned patterns used when built-ins fail ─────────────

class TestParserIntegration:
    def test_learned_pattern_fills_gap(self):
        """
        When a text uses 'Pobrane z sieci' (not in built-ins), a learned pattern
        should allow the parser to extract imported_kwh.
        """
        import pv_roi_tracker.invoice_parser as _mod
        text = (
            'Okres rozliczeniowy 01.07.2025 - 31.07.2025\n'
            'Pobrane z sieci 150\n'
            'Wprowadzono do sieci 300\n'
        )
        # Without learned pattern: should still parse via built-in fallback 'Pobran[ae] z sieci'
        data = _mod._parse_text(text)
        assert data.imported_kwh == 150.0

    def test_builtin_takes_priority_over_learned(self):
        """
        Built-in patterns win; a learned wrong pattern should not override them.
        """
        import pv_roi_tracker.invoice_parser as _mod
        original_provider = _mod._layouts_provider
        try:
            # Inject a "learned" pattern that would match a wrong value
            _mod.set_layouts_provider(lambda fk: [r'Wprowadzono do sieci\s+([\d,]+)'] if fk == 'imp_total' else [])
            text = (
                'Okres rozliczeniowy 01.07.2025 - 31.07.2025\n'
                'Pobrano z sieci 150\n'
                'Wprowadzono do sieci 300\n'
            )
            data = _mod._parse_text(text)
            # Built-in 'Pobrano z sieci' should win → 150, not 300
            assert data.imported_kwh == 150.0
        finally:
            _mod.set_layouts_provider(original_provider)

    def test_learned_pattern_activates_when_builtin_fails(self):
        """
        When a text layout defeats all built-ins, an injected learned pattern activates.
        """
        import pv_roi_tracker.invoice_parser as _mod
        original_provider = _mod._layouts_provider
        try:
            _mod.set_layouts_provider(
                lambda fk: [r'Pobrana energia z sieci:\s+([\d,]+)'] if fk == 'imp_total' else []
            )
            text = (
                'Okres rozliczeniowy 01.07.2025 - 31.07.2025\n'
                'Pobrana energia z sieci: 175\n'
                'Wprowadzono do sieci 200\n'
            )
            data = _mod._parse_text(text)
            assert data.imported_kwh == 175.0
        finally:
            _mod.set_layouts_provider(original_provider)


# ── Safe-write regression (same fix as historic_store — see its test file) ──

class TestSafeWrite:
    def test_load_missing_with_bak_present_recovers_from_bak(self, tmp_path):
        path = tmp_path / 'invoice_layouts.json'
        add_patterns('imp_total', [r'SomePattern'], path)
        bak = path.with_suffix('.json.bak')
        bak.write_text(path.read_text())
        path.unlink()   # simulate a crash that left only the .bak behind

        assert learned_for('imp_total', path) == [r'SomePattern']

    def test_save_does_not_delete_target_before_atomic_replace(self, tmp_path, monkeypatch):
        path = tmp_path / 'invoice_layouts.json'
        add_patterns('imp_total', [r'PatternA'], path)

        original_rename = Path.rename
        seen_missing = []

        def _spy_rename(self, target):
            seen_missing.append(path.exists())
            return original_rename(self, target)

        monkeypatch.setattr(Path, 'rename', _spy_rename)
        add_patterns('imp_total', [r'PatternB'], path)

        assert seen_missing == [True]
        assert path.with_suffix('.json.bak').exists()
