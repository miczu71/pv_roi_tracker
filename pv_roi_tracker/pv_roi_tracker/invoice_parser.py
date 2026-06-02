"""
Tauron invoice PDF parser.

parse_invoice(pdf_bytes) -> InvoiceData
parse_invoice_debug(pdf_bytes) -> dict  (returns parsed fields + raw text, no side-effects)

Extracts billing kWh, tariff rates, prosument deposit and amount-due from
the Tauron G12W net-billing (prosument) PDF layout confirmed against
invoice T/K1/BN567872/0011/26 (April 2026).

pypdf mangles Polish diacritics in certain PDFs (ł→ø, ą→a, etc.) so all
regex patterns match on diacritic-neutral substrings or ASCII-safe text.

Resilience design:
  • Each field has a *list* of built-in candidate patterns (multi-pattern fallback):
    the first match wins, so future label changes only need a new pattern
    prepended to the list rather than a code rewrite.
  • An injectable layouts-provider (set_layouts_provider) appends *learned*
    patterns after the built-ins. Built-ins always take priority; learned
    patterns only activate when built-ins produce None for a field.
  • Missing *optional* fields produce a human-readable warning in
    InvoiceData.warnings rather than silently returning None.
  • Post-extraction sanity checks (_validate) catch wrong matches:
    peak+offpeak ≈ totals, deposit arithmetic, plausibility ranges.
  • InvoiceParseError is only raised when the billing month or import/export
    TOTALS cannot be found (genuinely not a parseable Tauron invoice).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

# ── Layouts provider (injectable, default = no learned patterns) ──────────────

_layouts_provider: Callable[[str], list] = lambda field_key: []


def set_layouts_provider(fn: Callable[[str], list]) -> None:
    """
    Inject a function that returns learned regex patterns for a field key.
    Called at add-on startup by main.py so the parser picks up saved layouts.
    """
    global _layouts_provider
    _layouts_provider = fn


# ── Built-in candidate patterns (ordered: most specific / current first) ─────
# Keys match invoice_layouts.LEARNABLE_FIELDS.

_BUILTIN_PATTERNS: dict[str, list] = {
    'imp_total': [
        r'Pobrano z sieci\s+([\d,]+)',
        r'Pobrane z sieci\s+([\d,]+)',
        r'Pobran[ae] z sieci\s+([\d,]+)',
        r'Energia pobrana z sieci\s+([\d,]+)',
        # Oldest format (2025): no "Pobrano z sieci" summary — extract from invoice table row 1
        r'Sprzeda.y energii elektrycznej\s+[\d,]+\s+\d+\s+[\d,]+\s+[\d,]+\s+([\d,]+)',
        # Oldest format fallback: "Ogółem: net vat brutto import export" summary row
        r'Og..em:\s+[\d,]+\s+[\d,]+\s+[\d,]+\s+([\d,]+)\s+[\d,]+',
    ],
    'exp_total': [
        r'Wprowadzono do sieci\s+([\d,]+)',
        r'Wprowadzone do sieci\s+([\d,]+)',
        r'Oddano do sieci\s+([\d,]+)',
        r'Energia wprowadzona do sieci\s+([\d,]+)',
        # Oldest format: last number in "Ogółem: net vat brutto import export" row
        r'Og..em:\s+[\d,]+\s+[\d,]+\s+[\d,]+\s+[\d,]+\s+([\d,]+)',
    ],
    'imp_peak': [
        # New format: zone qty kWh price
        r'(?:Energia czynna\s+)?szczytowa\s+([\d,]+)\s+kWh\s+[\d,]+',
        r'(?:Energia czynna\s+)?szczyt\s+([\d,]+)\s+kWh\s+[\d,]+',
        r'strefa szczytow\w*\s+([\d,]+)\s+kWh',
        # Old format (stary wzór): Energia czynna header / zone kWh qty price
        r'Energia czynna\s+szczytowa\s+kWh\s+([\d,]+)\s+[\d,]+',
        # G11 single-zone: całodobowa maps to "peak" slot
        r'Energia czynna\s+ca.odobowa\s+kWh\s+([\d,]+)\s+[\d,]+',
    ],
    'imp_offpeak': [
        # New format: zone qty kWh price
        r'(?:Energia czynna\s+)?pozaszczytowa\s+([\d,]+)\s+kWh\s+[\d,]+',
        r'(?:Energia czynna\s+)?poza szczytem\s+([\d,]+)\s+kWh\s+[\d,]+',
        r'strefa poza\s*szczytow\w*\s+([\d,]+)\s+kWh',
        # Old format: offpeak is the second row after Energia czynna / szczytowa
        r'Energia czynna\s+szczytowa\s+kWh\s+[\d,]+\s+[\d,]+[^\n]+\npozaszczytowa\s+kWh\s+([\d,]+)',
        # G11: no offpeak zone — intentionally no pattern; None is suppressed by _is_single_zone
    ],
    'energy_peak_net': [
        # New format: zone qty kWh price
        r'(?:Energia czynna\s+)?szczytowa\s+[\d,]+\s+kWh\s+([\d,]+)',
        r'(?:Energia czynna\s+)?szczyt\s+[\d,]+\s+kWh\s+([\d,]+)',
        # Old format: zone kWh qty price
        r'Energia czynna\s+szczytowa\s+kWh\s+[\d,]+\s+([\d,]+)',
        # G11 single-zone: całodobowa price → peak slot
        r'Energia czynna\s+ca.odobowa\s+kWh\s+[\d,]+\s+([\d,]+)',
    ],
    'energy_offpeak_net': [
        # New format: zone qty kWh price
        r'(?:Energia czynna\s+)?pozaszczytowa\s+[\d,]+\s+kWh\s+([\d,]+)',
        r'(?:Energia czynna\s+)?poza szczytem\s+[\d,]+\s+kWh\s+([\d,]+)',
        # Old format: offpeak price is second value after Energia czynna header
        r'Energia czynna\s+szczytowa\s+kWh\s+[\d,]+\s+[\d,]+[^\n]+\npozaszczytowa\s+kWh\s+[\d,]+\s+([\d,]+)',
        # G11: no offpeak — intentionally no pattern
    ],
    'invoice_number': [
        # Old format: "FAKTURA VAT NR T/K1/…" header in ZAŁĄCZNIK
        r'FAKTURA VAT NR\s+([\w/]+)',
        r'Podsumowanie faktury VAT\s+([\w/]+)',
        r'Numer faktury\s+([\w/]+)',
        r'Nr faktury\s+([\w/]+)',
        r'Faktura\s+nr\s+([\w/]+)',
    ],
    'amount_due': [
        r'Razem \(3-6\)\s+([\d,]+)',
        r'Razem do zap.aty\s+([\d,]+)',
        r'Do zap.aty\s+([\d,]+)',
        # Oldest format: "Do zapłaty: 83,63 zł" (colon between label and amount)
        r'Do zap.aty:\s*([\d,]+)',
    ],
    'avg_price': [
        r'[Śś]rednia cena za 1 kWh to\s+([\d,]+)',
        r'rednia cena za 1 kWh to\s+([\d,]+)',
        r'[Śś]rednia cena 1 kWh\s+([\d,]+)',
        r'[Śś]r\.\s+cena\s+([\d,]+)',
        # Old format: "Średnia cena brutto 1 kWh 0,93 zł/kWh"
        r'rednia cena brutto\s+\d+\s+kWh\s+([\d,]+)',
    ],
    'deposit_current': [
        r'Depozyt prosumencki w rozliczanym okresie[^\d]+([\d,]+)',
        r'Depozyt prosumencki w bie.*?okresie[^\d]+([\d,]+)',
        r'4\.\s*Depozyt prosumencki[^\d]+([\d,]+)',
    ],
    'deposit_previous': [
        r'Depozyt prosumencki z okres.w poprzednich[^\d]+([\d,]+)',
        r'5\.\s*Depozyt prosumencki z poprz[^\d]+([\d,]+)',
    ],
    'deposit_used': [
        r'Rozliczenie depozytu \(\d\+\d\)\s+([\d,]+)',       # old: (4+5)
        r'Rozliczenie depozytu[^)]*\)\s*([\d,]+)',            # new: ( 4 + 5 + 6 ) and any variant
        r'Rozliczenie depozytu\s+([\d,]+)',
        r'[67]\.\s*Rozliczenie depozytu[^\d]+([\d,]+)',       # numbered (6. or 7.)
    ],
    'fixed_mocowa': [
        # New format: label qty unit price_per_unit value_net
        r'Op.ata mocowa\s+\d+\s+[^\s]*mc\s+[\d,]+\s+([\d,]+)',
        r'Op.ata mocow\w*\s+\d+\s+[^\s]*mc\s+([\d,]+)',
        # Old format: label unit qty price_per_unit value_net  (unit comes before qty)
        r'Op.ata mocow\w*\s+(?!\d)\S+\s+\d+\s+[\d,]+\s+([\d,]+)',
    ],
    'fixed_abonament': [
        # New format (label on one line)
        r'Stawka op.aty abonamentowej\s+\d+\s+[^\s]*mc\s+[\d,]+\s+([\d,]+)',
        r'Op.ata abonamentow\w*\s+\d+\s+[^\s]*mc\s+[\d,]+\s+([\d,]+)',
        r'Abonament\s+\d+\s+[^\s]*mc\s+[\d,]+\s+([\d,]+)',
        # Old format: unit before qty (label on one line)
        r'Stawka op.aty abonamentowej\s+(?!\d)\S+\s+\d+\s+[\d,]+\s+([\d,]+)',
        # Oldest format: pypdf splits "Stawka opłaty\nabonamentowej"
        r'Stawka op.aty\s+abonamentowej\s+(?!\d)\S+\s+\d+\s+[\d,]+\s+([\d,]+)',
        r'Stawka op.aty\s+abonamentowej\s+\d+\s+[^\s]*mc\s+[\d,]+\s+([\d,]+)',
    ],
    'fixed_stalysieciowy': [
        # New format (label on one line)
        r'Sk.adnik sta.y stawki sieciowej\s+\d+\s+[^\s]*mc\s+[\d,]+\s+([\d,]+)',
        r'Sk.adnik sta.y sieciow\w*\s+\d+\s+[^\s]*mc\s+[\d,]+\s+([\d,]+)',
        # Old format: unit before qty (label on one line)
        r'Sk.adnik sta.y stawki sieciowej\s+(?!\d)\S+\s+\d+\s+[\d,]+\s+([\d,]+)',
        # Oldest format: pypdf splits "Składnik stały stawki\nsieciowej"
        r'Sk.adnik sta.y stawki\s+sieciowej\s+(?!\d)\S+\s+\d+\s+[\d,]+\s+([\d,]+)',
        r'Sk.adnik sta.y stawki\s+sieciowej\s+\d+\s+[^\s]*mc\s+[\d,]+\s+([\d,]+)',
    ],
}


def _patterns_for(field_key: str) -> list:
    """Built-in patterns first, then any learned patterns for this field."""
    builtins = _BUILTIN_PATTERNS.get(field_key, [])
    learned = _layouts_provider(field_key)
    return builtins + [p for p in learned if p not in builtins]


def find_field_spans(text: str, parsed_fields: dict) -> dict:
    """
    For each field key backed by _BUILTIN_PATTERNS, find the text span of the
    first regex match, provided the field has a non-None value in parsed_fields.

    Returns {field_key: {'start': int, 'end': int, 'text': str}}.
    Fields with no match are absent from the result (not None).

    Also checks for the billing period (special-cased in _parse_text).
    """
    spans: dict = {}

    # Fields backed by _patterns_for
    for field_key in _BUILTIN_PATTERNS:
        if parsed_fields.get(field_key) is None:
            continue
        for pattern in _patterns_for(field_key):
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                # Use the widest match: from the start of the full match to its end
                spans[field_key] = {
                    'start': m.start(),
                    'end': m.end(),
                    'text': text[m.start():m.end()],
                }
                break

    # Billing period — use the same multi-pattern list as _parse_text
    year = parsed_fields.get('year')
    month = parsed_fields.get('month')
    if year is not None and month is not None:
        _bp_span_patterns = [
            r'Okres rozliczeniowy\s+\d{2}[./]\d{2}[./]\d{4}[^\n]*',
            r'Okre.{0,3}rozliczeniowy\s+\d{2}[./]\d{2}[./]\d{4}[^\n]*',
            r'Okres rozliczeniowy\s*\n[^\n]*\d{2}[./]\d{2}[./]\d{4}',
            r'rozliczeniow\w*[^./\d]*\d{2}[./]\d{2}[./]\d{4}[^\n]*',
            rf'\b01[./]{month:02d}[./]{year}[^\n]*',
        ]
        for _bp_pat in _bp_span_patterns:
            bp_m = re.search(_bp_pat, text, re.IGNORECASE)
            if bp_m:
                spans['billing_period'] = {
                    'start': bp_m.start(),
                    'end': bp_m.end(),
                    'text': text[bp_m.start():bp_m.end()],
                }
                break

    return spans


# ── InvoiceParseError ─────────────────────────────────────────────────────────

class InvoiceParseError(ValueError):
    """Raised when the PDF is not a recognisable Tauron invoice."""


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class InvoiceData:
    # Billing period
    year: int
    month: int                              # 1-12, derived from period start date

    # kWh after bilansowanie międzyfazowe
    imported_kwh: float                     # Pobrano z sieci
    exported_kwh: float                     # Wprowadzono do sieci
    imported_kwh_peak: Optional[float]      # Energia czynna szczytowa
    imported_kwh_offpeak: Optional[float]   # Energia czynna pozaszczytowa
    exported_kwh_peak: Optional[float]      # Licznik oddanie szczyt
    exported_kwh_offpeak: Optional[float]   # Licznik oddanie pozaszczyt

    # Tariff rates (net, from the Sprzedaż section)
    energy_peak_net: Optional[float]        # e.g. 0.62700 zł/kWh
    energy_offpeak_net: Optional[float]     # e.g. 0.41800 zł/kWh

    # Distribution variable components (net)
    dist_var_peak_net: Optional[float]      # składnik zmienny sieciowy szczytowy
    dist_var_offpeak_net: Optional[float]   # składnik zmienny sieciowy pozaszczytowy
    dist_jakosciowa_net: Optional[float]    # stawka jakościowa (same both zones)
    dist_oze_net: Optional[float]           # opłata OZE
    dist_kogeneracja_net: Optional[float]   # opłata kogeneracyjna

    # Fixed monthly charges (net)
    fixed_mocowa_net: Optional[float]       # opłata mocowa
    fixed_abonament_net: Optional[float]    # stawka opłaty abonamentowej
    fixed_stalysieciowy_net: Optional[float]  # składnik stały stawki sieciowej

    # Computed gross marginal rates (incl. all variable distribution, +23% VAT)
    peak_gross: Optional[float] = field(default=None)
    offpeak_gross: Optional[float] = field(default=None)
    blended_gross: Optional[float] = field(default=None)  # weighted by import split

    # Financial summary
    deposit_current_pln: Optional[float] = field(default=None)   # depozyt w bieżącym miesiącu
    deposit_previous_pln: Optional[float] = field(default=None)  # depozyt z poprzednich
    deposit_used_pln: Optional[float] = field(default=None)      # rozliczenie depozytu
    amount_due_pln: Optional[float] = field(default=None)        # Razem do zapłaty
    avg_price_pln_kwh: Optional[float] = field(default=None)     # Średnia cena 1 kWh

    # Fixed monthly net sum (for comparison with energy_simulation.yaml)
    fixed_total_net: Optional[float] = field(default=None)

    # Invoice identity
    invoice_number: Optional[str] = field(default=None)
    billing_period_raw: Optional[str] = field(default=None)

    # Parse quality: empty list = clean parse; non-empty = missing fields or
    # failed sanity checks — not fatal but worth surfacing in the UI.
    warnings: list = field(default_factory=list)


# ── Number / string parsing helpers ──────────────────────────────────────────

def _n(s: str) -> float:
    """Parse Polish decimal notation (comma as separator) → float."""
    return float(s.replace(',', '.'))


def _first(pattern: str, text: str, group: int = 1) -> Optional[str]:
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(group) if m else None


def _first_float(pattern: str, text: str, group: int = 1) -> Optional[float]:
    raw = _first(pattern, text, group)
    try:
        return _n(raw) if raw is not None else None
    except (ValueError, AttributeError):
        return None


def _first_multi(patterns: list, text: str, group: int = 1) -> Optional[str]:
    """Try an ordered list of regex patterns; return the first match."""
    for p in patterns:
        v = _first(p, text, group)
        if v is not None:
            return v
    return None


def _first_float_multi(patterns: list, text: str, group: int = 1) -> Optional[float]:
    """Try an ordered list of regex patterns; return the first numeric match."""
    for p in patterns:
        v = _first_float(p, text, group)
        if v is not None:
            return v
    return None


# ── Validation sanity-checks ──────────────────────────────────────────────────

def _validate(data: InvoiceData) -> list:
    """
    Post-extraction consistency checks.
    Returns a list of human-readable warning strings for any failed check.
    Does not raise — failing a check means possible wrong match, not fatal error.
    """
    warnings: list = []

    def _approx(a: float, b: float, tol_abs: float = 2.0, tol_rel: float = 0.05) -> bool:
        return abs(a - b) <= max(tol_abs, abs(a) * tol_rel)

    # Imported total ≈ peak + offpeak
    if (data.imported_kwh_peak is not None and data.imported_kwh_offpeak is not None):
        split_sum = data.imported_kwh_peak + data.imported_kwh_offpeak
        if not _approx(data.imported_kwh, split_sum):
            warnings.append(
                f'walidacja: pobrano {data.imported_kwh:.0f} ≠ szczyt+poza '
                f'{data.imported_kwh_peak:.0f}+{data.imported_kwh_offpeak:.0f}'
                f'={split_sum:.0f}'
            )

    # Exported total ≈ peak + offpeak
    if (data.exported_kwh_peak is not None and data.exported_kwh_offpeak is not None):
        split_sum = data.exported_kwh_peak + data.exported_kwh_offpeak
        if not _approx(data.exported_kwh, split_sum):
            warnings.append(
                f'walidacja: oddano {data.exported_kwh:.0f} ≠ szczyt+poza '
                f'{data.exported_kwh_peak:.0f}+{data.exported_kwh_offpeak:.0f}'
                f'={split_sum:.0f}'
            )

    # Deposit arithmetic: used ≈ current + previous
    if (data.deposit_current_pln is not None
            and data.deposit_previous_pln is not None
            and data.deposit_used_pln is not None):
        expected = data.deposit_current_pln + data.deposit_previous_pln
        if abs(data.deposit_used_pln - expected) > 0.10:
            warnings.append(
                f'walidacja: depozyt wykorzystany {data.deposit_used_pln:.2f} ≠ '
                f'bieżący+poprzednie {data.deposit_current_pln:.2f}+'
                f'{data.deposit_previous_pln:.2f}={expected:.2f}'
            )

    # Plausibility ranges
    if data.avg_price_pln_kwh is not None:
        if not (0.10 <= data.avg_price_pln_kwh <= 3.0):
            warnings.append(
                f'walidacja: średnia cena {data.avg_price_pln_kwh:.4f} zł/kWh poza '
                f'rozsądnym zakresem [0.10, 3.00]'
            )

    if data.blended_gross is not None:
        if not (0.20 <= data.blended_gross <= 3.0):
            warnings.append(
                f'walidacja: blended_gross {data.blended_gross:.4f} zł/kWh poza '
                f'rozsądnym zakresem [0.20, 3.00]'
            )

    if data.amount_due_pln is not None and data.amount_due_pln < 0:
        warnings.append(
            f'walidacja: kwota do zapłaty {data.amount_due_pln:.2f} jest ujemna'
        )

    return warnings


# ── Main parser ───────────────────────────────────────────────────────────────

def _extract_text(pdf_bytes: bytes) -> str:
    """Extract and normalise text from a Tauron PDF. Raises InvoiceParseError on failure."""
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = [p.extract_text() or '' for p in reader.pages]
        text = '\n'.join(pages)
    except Exception as exc:
        raise InvoiceParseError(f'PDF extraction failed: {exc}') from exc
    # Normalise: collapse runs of whitespace (pypdf sometimes breaks numbers)
    return re.sub(r'[ \t]+', ' ', text)


def parse_invoice(pdf_bytes: bytes) -> InvoiceData:
    """
    Parse a Tauron PDF invoice and return an InvoiceData instance.

    Raises InvoiceParseError if the billing month or import/export totals
    cannot be found.
    """
    text = _extract_text(pdf_bytes)
    return _parse_text(text)


def parse_invoice_debug(pdf_bytes: bytes) -> dict:
    """
    Parse without saving — returns the raw extracted text alongside the
    parsed fields and warnings. Used by the /api/invoice/debug endpoint
    so layout issues can be diagnosed without re-uploading.
    """
    text = ''
    try:
        text = _extract_text(pdf_bytes)
        data = _parse_text(text)
        return {
            'ok': True,
            'text': text,
            'fields': asdict(data),
            'warnings': data.warnings,
        }
    except InvoiceParseError as exc:
        return {
            'ok': False,
            'text': text,
            'error': str(exc),
            'fields': None,
            'warnings': [],
        }


def _parse_text(text: str) -> InvoiceData:
    """Core parser: operates on already-extracted, whitespace-normalised text."""
    warnings: list = []

    # ── Billing period ────────────────────────────────────────────────────────
    # "Okres rozliczeniowy 01.04.2026 - 30.04.2026"
    # pypdf can mangle ś→ missing, add newlines inside phrases, or use variants.
    # Try many patterns before falling back to raw date-range heuristic.
    # Each pattern variant is listed twice: dot-separator and slash-separator.
    # Tauron's "nowy wzór faktury" (new format, introduced 2026) uses DD/MM/YYYY;
    # the classic format uses DD.MM.YYYY.
    _BILLING_PERIOD_PATTERNS = [
        # Standard label — dot format
        r'Okres rozliczeniowy\s+(\d{2})\.(\d{2})\.(\d{4})',
        # Standard label — slash format (new invoice template)
        r'Okres rozliczeniowy\s+(\d{2})/(\d{2})/(\d{4})',
        # Label on own line, date on next line — dot
        r'Okres rozliczeniowy\s*\n\s*(\d{2})\.(\d{2})\.(\d{4})',
        # Label on own line, date on next line — slash
        r'Okres rozliczeniowy\s*\n\s*(\d{2})/(\d{2})/(\d{4})',
        # Diacritic-drop variant — dot
        r'Okre.{0,3}rozliczeniowy\s+(\d{2})\.(\d{2})\.(\d{4})',
        # Diacritic-drop variant — slash
        r'Okre.{0,3}rozliczeniowy\s+(\d{2})/(\d{2})/(\d{4})',
        # Diacritic-drop + newline — dot
        r'Okre.{0,3}rozliczeniowy\s*\n\s*(\d{2})\.(\d{2})\.(\d{4})',
        # Diacritic-drop + newline — slash
        r'Okre.{0,3}rozliczeniowy\s*\n\s*(\d{2})/(\d{2})/(\d{4})',
        # Label with colon — dot
        r'Okres rozliczeniowy\s*:\s*(\d{2})\.(\d{2})\.(\d{4})',
        # Label with colon — slash
        r'Okres rozliczeniowy\s*:\s*(\d{2})/(\d{2})/(\d{4})',
        # Capitalised variants
        r'OKRES ROZLICZENIOWY\s+(\d{2})\.(\d{2})\.(\d{4})',
        r'OKRES ROZLICZENIOWY\s+(\d{2})/(\d{2})/(\d{4})',
        # Just the concept word near any date
        r'rozliczeniow\w*[^./\d]*(\d{2})\.(\d{2})\.(\d{4})',
        r'rozliczeniow\w*[^./\d]*(\d{2})/(\d{2})/(\d{4})',
        # Old format (stary wzór): "Za okres\nOd 01/10/2025 do 31/10/2025"
        r'Za okres\s+Od\s+(\d{2})/(\d{2})/(\d{4})',
        # Old format body (both "Za okres od" and "za okres od" — IGNORECASE in loop)
        r'za okres od\s+(\d{2})/(\d{2})/(\d{4})',
        # Old format annex with colon: "Rozliczenie za okres: 01/10/2025"
        r'Rozliczenie za okres:\s*(\d{2})/(\d{2})/(\d{4})',
        # Oldest format annex without colon: "Rozliczenie za okres 01/04/2025"
        r'Rozliczenie za okres\s+(\d{2})/(\d{2})/(\d{4})',
    ]
    period_m = None
    for _bp in _BILLING_PERIOD_PATTERNS:
        period_m = re.search(_bp, text, re.IGNORECASE)
        if period_m:
            break

    # Last resort: find any start-of-month date (01.MM.YYYY or 01/MM/YYYY) —
    # Tauron billing periods always start on the 1st.
    if not period_m:
        _heuristic_m = re.search(r'\b01[./](\d{2})[./](\d{4})\b', text)
        if _heuristic_m:
            _raw_month, _raw_year = _heuristic_m.group(1), _heuristic_m.group(2)
            warnings.append(
                f'okres rozliczeniowy: etykieta nieznaleziona — wydedukowano z daty '
                f'01/{_raw_month}/{_raw_year}; zweryfikuj miesiąc'
            )
            month = int(_raw_month)
            year  = int(_raw_year)
            billing_period_raw = None
            period_m = None  # suppress standard extraction below
        else:
            raise InvoiceParseError(
                'Billing period (Okres rozliczeniowy) not found — not a Tauron invoice')

    if period_m is not None:
        month = int(period_m.group(2))
        year  = int(period_m.group(3))

    billing_period_raw = (
        _first(r'Okres rozliczeniowy\s+(\d{2}\.\d{2}\.\d{4} - \d{2}\.\d{2}\.\d{4})', text)
        or _first(r'Okres rozliczeniowy\s*\n?\s*(\d{2}/\d{2}/\d{4} - \d{2}/\d{2}/\d{4})', text)
        # Old format: "Od 01/10/2025 do 31/10/2025"
        or _first(r'[Oo]d\s+(\d{2}/\d{2}/\d{4} do \d{2}/\d{2}/\d{4})', text)
        # Old format annex (with or without colon): "Rozliczenie za okres[: ] DD/MM/YYYY - DD/MM/YYYY"
        or _first(r'Rozliczenie za okres:?\s*(\d{2}/\d{2}/\d{4} - \d{2}/\d{2}/\d{4})', text)
    )

    # ── Invoice number ────────────────────────────────────────────────────────
    invoice_number = _first_multi(_patterns_for('invoice_number'), text)

    # ── Single-zone tariff detection (G11) ───────────────────────────────────
    # G11 uses "całodobowa" instead of "szczytowa/pozaszczytowa"; offpeak doesn't exist.
    _is_single_zone = bool(
        re.search(r'ca.odobowa\s+kWh', text, re.IGNORECASE)
        and not re.search(r'(?:szczytowa|pozaszczytowa)\s+kWh', text, re.IGNORECASE)
    )

    # ── Import / export totals ────────────────────────────────────────────────
    imp_total = _first_float_multi(_patterns_for('imp_total'), text)
    if imp_total is None:
        raise InvoiceParseError('Imported kWh (Pobrano z sieci) not found')

    exp_total = _first_float_multi(_patterns_for('exp_total'), text)
    if exp_total is None:
        raise InvoiceParseError('Exported kWh (Wprowadzono do sieci) not found')

    # ── Peak / offpeak import from Sprzedaż section ───────────────────────────
    imp_peak          = _first_float_multi(_patterns_for('imp_peak'), text)
    imp_offpeak       = _first_float_multi(_patterns_for('imp_offpeak'), text)
    energy_peak_net   = _first_float_multi(_patterns_for('energy_peak_net'), text)
    energy_offpeak_net = _first_float_multi(_patterns_for('energy_offpeak_net'), text)

    if imp_peak is None:
        warnings.append('import szczytowy (kWh) nie znaleziony')
    if imp_offpeak is None and not _is_single_zone:
        warnings.append('import pozaszczytowy (kWh) nie znaleziony')
    if energy_peak_net is None:
        warnings.append('cena energii szczytowej (net) nie znaleziona — szczyt gross nieobliczony')
    if energy_offpeak_net is None and not _is_single_zone:
        warnings.append('cena energii pozaszczytowej (net) nie znaleziona — poza-szczyt gross nieobliczony')

    # ── Peak / offpeak export from meter reading section ──────────────────────
    # New format: "(oddanie)-<serial>\nszczyt <date> (Z) <kwh>"
    # Old format: "(oddanie)\nnr <serial>\nszczyt <date> (Z) <kwh>"
    # G11:        "(oddanie)\nnr <serial>\ncałodobowa <date> (Z) <kwh>"
    _exp_m = (
        re.search(r'\(oddanie\)-\d+\nszczyt\s+[\d./]+\s+\([^\)]+\)\s+([\d,]+)', text)
        or re.search(r'\(oddanie\)\s*\n\s*\w+\s+\d+\s*\nszczyt\s+[\d./]+\s+\([^\)]+\)\s+([\d,]+)', text)
        or re.search(r'\(oddanie\)\s*\n\s*\w+\s+\d+\s*\nca.odobowa\s+[\d./]+\s+\([^\)]+\)\s+([\d,]+)', text)
    )
    exp_peak = _n(_exp_m.group(1)) if _exp_m else None
    _exp_m2 = (
        re.search(r'\(oddanie\)-\d+\n[^\n]+\npozaszczytowa\s+[\d./]+\s+\([^\)]+\)\s+([\d,]+)', text)
        or re.search(r'\(oddanie\)\s*\n\s*\w+\s+\d+\s*\n[^\n]+\npozaszczytowa\s+[\d./]+\s+\([^\)]+\)\s+([\d,]+)', text)
    )
    exp_offpeak = _n(_exp_m2.group(1)) if _exp_m2 else None

    # ── Distribution variable components ─────────────────────────────────────
    # Scope to the Dystrybucja section to avoid false positives.
    dist_section_m = re.search(r'Dystrybucja energii elektrycznej.*?Razem za dystrybucj', text, re.DOTALL)
    dist_text = dist_section_m.group(0) if dist_section_m else text

    def _dist_peak(section_patterns: list) -> Optional[float]:
        # Match both two-zone (szczyt) and single-zone (całodobowa / G11) tariffs
        for sp in section_patterns:
            for _zone in (r'szczyt\w*', r'ca.odobowa'):
                # New format: label zone qty kWh price_net
                m = re.search(sp + r'\s+' + _zone + r'\s+\d+\s+kWh\s+([\d,]+)', dist_text, re.IGNORECASE)
                if m:
                    return _n(m.group(1))
                # Old/oldest format: label\nzone kWh qty [optional-coeff] price_net
                m = re.search(sp + r'\s+' + _zone + r'\s+kWh\s+\d+(?:\s+\d+)?\s+([\d,]+)', dist_text, re.IGNORECASE)
                if m:
                    return _n(m.group(1))
        return None

    def _dist_offpeak(section_patterns: list) -> Optional[float]:
        for sp in section_patterns:
            # New format: label zone qty kWh value ... \n zone qty kWh value
            m2 = re.search(
                sp + r'\s+\S+\s+\d+\s+kWh\s+[\d,]+[^\n]+\n\s*\S+\s+\d+\s+kWh\s+([\d,]+)',
                dist_text, re.IGNORECASE)
            if m2:
                return _n(m2.group(1))
            # Old format: label\nzone kWh qty [coeff] price ... \n zone kWh qty [coeff] price
            m2 = re.search(
                sp + r'\s+\S+\s+kWh\s+\d+(?:\s+\d+)?\s+[\d,]+[^\n]+\n\s*\S+\s+kWh\s+\d+(?:\s+\d+)?\s+([\d,]+)',
                dist_text, re.IGNORECASE)
            if m2:
                return _n(m2.group(1))
        return None

    # Use \s+ between "stawki" and "sieciowej": pypdf sometimes splits across lines
    _sksn_patterns = [r'Sk.adnik zmienny stawki\s+sieciowej', r'Sk.adnik zmienny sieciow\w*']
    dist_var_peak_net    = _dist_peak(_sksn_patterns)
    dist_var_offpeak_net = _dist_offpeak(_sksn_patterns)
    dist_jakosciowa_net  = _dist_peak([r'Stawka jako.ciow\w*', r'Jako.ciow\w*'])
    dist_oze_net         = _dist_peak([r'Op.ata OZE', r'Stawka OZE'])
    dist_kogeneracja_net = _dist_peak([r'Op.ata kogeneracyjna', r'Kogeneracyjna'])

    if dist_var_peak_net is None:
        warnings.append('składnik zmienny sieciowy szczytowy nie znaleziony — szczyt gross nieobliczony')
    if dist_var_offpeak_net is None and not _is_single_zone:
        warnings.append('składnik zmienny sieciowy pozaszczytowy nie znaleziony — poza-szczyt gross nieobliczony')
    if dist_jakosciowa_net is None:
        warnings.append('stawka jakościowa nie znaleziona (użyto 0)')
    if dist_oze_net is None:
        warnings.append('opłata OZE nie znaleziona (użyto 0)')
    if dist_kogeneracja_net is None:
        warnings.append('opłata kogeneracyjna nie znaleziona (użyto 0)')

    # ── Fixed monthly charges ─────────────────────────────────────────────────
    fixed_mocowa_net        = _first_float_multi(_patterns_for('fixed_mocowa'), dist_text)
    fixed_abonament_net     = _first_float_multi(_patterns_for('fixed_abonament'), dist_text)
    fixed_stalysieciowy_net = _first_float_multi(_patterns_for('fixed_stalysieciowy'), dist_text)

    if fixed_mocowa_net is None:
        warnings.append('opłata mocowa nie znaleziona')
    if fixed_abonament_net is None:
        warnings.append('abonament nie znaleziony')
    if fixed_stalysieciowy_net is None:
        warnings.append('składnik stały sieciowy nie znaleziony')

    fixed_total_net: Optional[float] = None
    if all(v is not None for v in (fixed_mocowa_net, fixed_abonament_net, fixed_stalysieciowy_net)):
        fixed_total_net = round(fixed_mocowa_net + fixed_abonament_net + fixed_stalysieciowy_net, 4)  # type: ignore[operator]
    else:
        warnings.append('fixed_total_net nieobliczony — brakuje co najmniej jednej opłaty stałej')

    # ── Prosument deposit ─────────────────────────────────────────────────────
    deposit_current_pln  = _first_float_multi(_patterns_for('deposit_current'), text)
    deposit_previous_pln = _first_float_multi(_patterns_for('deposit_previous'), text)
    deposit_used_pln     = _first_float_multi(_patterns_for('deposit_used'), text)

    if deposit_current_pln is None:
        warnings.append('depozyt prosumencki bieżący nie znaleziony')
    if deposit_previous_pln is None:
        warnings.append('depozyt prosumencki z poprzednich okresów nie znaleziony')
    if deposit_used_pln is None:
        warnings.append('rozliczenie depozytu nie znalezione')

    # ── Amount due ────────────────────────────────────────────────────────────
    amount_due_pln    = _first_float_multi(_patterns_for('amount_due'), text)
    avg_price_pln_kwh = _first_float_multi(_patterns_for('avg_price'), text)

    if amount_due_pln is None:
        warnings.append('kwota do zapłaty (Razem) nie znaleziona')
    if avg_price_pln_kwh is None:
        warnings.append('średnia cena za 1 kWh nie znaleziona')

    # ── Compute gross marginal rates ──────────────────────────────────────────
    VAT = 1.23
    peak_gross: Optional[float] = None
    offpeak_gross: Optional[float] = None
    blended_gross: Optional[float] = None

    if energy_peak_net is not None and dist_var_peak_net is not None:
        peak_var_net = (energy_peak_net
                        + dist_var_peak_net
                        + (dist_jakosciowa_net or 0.0)
                        + (dist_oze_net or 0.0)
                        + (dist_kogeneracja_net or 0.0))
        peak_gross = round(peak_var_net * VAT, 4)
    else:
        warnings.append('peak_gross nieobliczony — brakuje ceny energii lub składnika zmiennego')

    if energy_offpeak_net is not None and dist_var_offpeak_net is not None:
        offpeak_var_net = (energy_offpeak_net
                           + dist_var_offpeak_net
                           + (dist_jakosciowa_net or 0.0)
                           + (dist_oze_net or 0.0)
                           + (dist_kogeneracja_net or 0.0))
        offpeak_gross = round(offpeak_var_net * VAT, 4)
    elif not _is_single_zone:
        warnings.append('offpeak_gross nieobliczony — brakuje ceny energii lub składnika zmiennego')

    if (peak_gross is not None and offpeak_gross is not None
            and imp_peak is not None and imp_offpeak is not None):
        total_imp = imp_peak + imp_offpeak
        if total_imp > 0:
            blended_gross = round(
                (imp_peak * peak_gross + imp_offpeak * offpeak_gross) / total_imp, 4)
    elif peak_gross is not None and offpeak_gross is not None:
        blended_gross = round((peak_gross + offpeak_gross) / 2, 4)
        warnings.append('blended_gross: podział szczyt/poza-szczyt niedostępny — użyto średniej arytmetycznej')
    elif _is_single_zone and peak_gross is not None:
        blended_gross = peak_gross  # G11: single zone — blended equals the single gross rate

    # De-duplicate warnings
    seen: set = set()
    unique_warnings: list = []
    for w in warnings:
        if w not in seen:
            seen.add(w)
            unique_warnings.append(w)

    data = InvoiceData(
        year=year,
        month=month,
        imported_kwh=imp_total,
        exported_kwh=exp_total,
        imported_kwh_peak=imp_peak,
        imported_kwh_offpeak=imp_offpeak,
        exported_kwh_peak=exp_peak,
        exported_kwh_offpeak=exp_offpeak,
        energy_peak_net=energy_peak_net,
        energy_offpeak_net=energy_offpeak_net,
        dist_var_peak_net=dist_var_peak_net,
        dist_var_offpeak_net=dist_var_offpeak_net,
        dist_jakosciowa_net=dist_jakosciowa_net,
        dist_oze_net=dist_oze_net,
        dist_kogeneracja_net=dist_kogeneracja_net,
        fixed_mocowa_net=fixed_mocowa_net,
        fixed_abonament_net=fixed_abonament_net,
        fixed_stalysieciowy_net=fixed_stalysieciowy_net,
        peak_gross=peak_gross,
        offpeak_gross=offpeak_gross,
        blended_gross=blended_gross,
        deposit_current_pln=deposit_current_pln,
        deposit_previous_pln=deposit_previous_pln,
        deposit_used_pln=deposit_used_pln,
        amount_due_pln=amount_due_pln,
        avg_price_pln_kwh=avg_price_pln_kwh,
        fixed_total_net=fixed_total_net,
        invoice_number=invoice_number,
        billing_period_raw=billing_period_raw,
        warnings=unique_warnings,
    )

    data.warnings.extend(_validate(data))
    return data
