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
  • Each field uses a *list* of candidate patterns (multi-pattern fallback):
    the first match wins, so future label changes only need a new pattern
    prepended to the list rather than a code rewrite.
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
from typing import Optional


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
    m = re.search(pattern, text)
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
    # "Okres rozliczeniowy 01.04.2026 - 30.04.2026"  (ł mangles to ø in pypdf)
    period_m = re.search(
        r'Okres rozliczeniowy\s+(\d{2})\.(\d{2})\.(\d{4})',
        text)
    if not period_m:
        raise InvoiceParseError('Billing period (Okres rozliczeniowy) not found — not a Tauron invoice')
    month = int(period_m.group(2))
    year = int(period_m.group(3))
    billing_period_raw = _first(
        r'Okres rozliczeniowy\s+(\d{2}\.\d{2}\.\d{4} - \d{2}\.\d{2}\.\d{4})', text)

    # ── Invoice number ────────────────────────────────────────────────────────
    invoice_number = _first_multi([
        r'Numer faktury\s+([\w/]+)',
        r'Nr faktury\s+([\w/]+)',
        r'Faktura\s+nr\s+([\w/]+)',
    ], text)

    # ── Import / export totals ────────────────────────────────────────────────
    imp_total = _first_float_multi([
        r'Pobrano z sieci\s+([\d,]+)',
        r'Pobrane z sieci\s+([\d,]+)',
        r'Pobran[ae] z sieci\s+([\d,]+)',
        r'Energia pobrana z sieci\s+([\d,]+)',
    ], text)
    if imp_total is None:
        raise InvoiceParseError('Imported kWh (Pobrano z sieci) not found')

    exp_total = _first_float_multi([
        r'Wprowadzono do sieci\s+([\d,]+)',
        r'Wprowadzone do sieci\s+([\d,]+)',
        r'Oddano do sieci\s+([\d,]+)',
        r'Energia wprowadzona do sieci\s+([\d,]+)',
    ], text)
    if exp_total is None:
        raise InvoiceParseError('Exported kWh (Wprowadzono do sieci) not found')

    # ── Peak / offpeak import from Sprzedaż section ───────────────────────────
    imp_peak = _first_float_multi([
        r'(?:Energia czynna\s+)?szczytowa\s+([\d,]+)\s+kWh\s+[\d,]+',
        r'(?:Energia czynna\s+)?szczyt\s+([\d,]+)\s+kWh\s+[\d,]+',
        r'strefa szczytow\w*\s+([\d,]+)\s+kWh',
    ], text)
    imp_offpeak = _first_float_multi([
        r'(?:Energia czynna\s+)?pozaszczytowa\s+([\d,]+)\s+kWh\s+[\d,]+',
        r'(?:Energia czynna\s+)?poza szczytem\s+([\d,]+)\s+kWh\s+[\d,]+',
        r'strefa poza\s*szczytow\w*\s+([\d,]+)\s+kWh',
    ], text)
    energy_peak_net = _first_float_multi([
        r'(?:Energia czynna\s+)?szczytowa\s+[\d,]+\s+kWh\s+([\d,]+)',
        r'(?:Energia czynna\s+)?szczyt\s+[\d,]+\s+kWh\s+([\d,]+)',
    ], text)
    energy_offpeak_net = _first_float_multi([
        r'(?:Energia czynna\s+)?pozaszczytowa\s+[\d,]+\s+kWh\s+([\d,]+)',
        r'(?:Energia czynna\s+)?poza szczytem\s+[\d,]+\s+kWh\s+([\d,]+)',
    ], text)

    if imp_peak is None:
        warnings.append('import szczytowy (kWh) nie znaleziony')
    if imp_offpeak is None:
        warnings.append('import pozaszczytowy (kWh) nie znaleziony')
    if energy_peak_net is None:
        warnings.append('cena energii szczytowej (net) nie znaleziona — szczyt gross nieobliczony')
    if energy_offpeak_net is None:
        warnings.append('cena energii pozaszczytowej (net) nie znaleziona — poza-szczyt gross nieobliczony')

    # ── Peak / offpeak export from meter reading section ──────────────────────
    # Layout (two lines after the (oddanie) counter line):
    #   szczyt 30.04.2026 (Zdalny) 230,0000
    #   pozaszczytowa 30.04.2026 (Zdalny) 238,0000
    _exp_m = re.search(r'\(oddanie\)-\d+\nszczyt\s+[\d.]+\s+\([^\)]+\)\s+([\d,]+)', text)
    exp_peak = _n(_exp_m.group(1)) if _exp_m else None
    _exp_m2 = re.search(r'\(oddanie\)-\d+\n[^\n]+\npozaszczytowa\s+[\d.]+\s+\([^\)]+\)\s+([\d,]+)', text)
    exp_offpeak = _n(_exp_m2.group(1)) if _exp_m2 else None

    # ── Distribution variable components ─────────────────────────────────────
    # Scope to the Dystrybucja section to avoid false positives.
    dist_section_m = re.search(r'Dystrybucja energii elektrycznej.*?Razem za dystrybucj', text, re.DOTALL)
    dist_text = dist_section_m.group(0) if dist_section_m else text

    def _dist_peak(section_patterns: list) -> Optional[float]:
        """Find heading matching any section_pattern, return price on next szczytow* line."""
        for sp in section_patterns:
            m = re.search(sp + r'\s+szczyt\w*\s+\d+\s+kWh\s+([\d,]+)', dist_text, re.IGNORECASE)
            if m:
                return _n(m.group(1))
        return None

    def _dist_offpeak(section_patterns: list) -> Optional[float]:
        for sp in section_patterns:
            # Simpler two-line approach: find heading, skip peak line, grab next kWh price
            m2 = re.search(
                sp + r'\s+\S+\s+\d+\s+kWh\s+[\d,]+[^\n]+\n\s*\S+\s+\d+\s+kWh\s+([\d,]+)',
                dist_text, re.IGNORECASE)
            if m2:
                return _n(m2.group(1))
        return None

    # Składnik zmienny stawki sieciowej (ł→ø in pypdf → match with .)
    _sksn_patterns = [r'Sk.adnik zmienny stawki sieciowej', r'Sk.adnik zmienny sieciow\w*']
    dist_var_peak_net    = _dist_peak(_sksn_patterns)
    dist_var_offpeak_net = _dist_offpeak(_sksn_patterns)

    # Stawka jakościowa
    dist_jakosciowa_net  = _dist_peak([r'Stawka jako.ciow\w*', r'Jako.ciow\w*'])

    # Opłata OZE
    dist_oze_net         = _dist_peak([r'Op.ata OZE', r'Stawka OZE'])

    # Opłata kogeneracyjna
    dist_kogeneracja_net = _dist_peak([r'Op.ata kogeneracyjna', r'Kogeneracyjna'])

    if dist_var_peak_net is None:
        warnings.append('składnik zmienny sieciowy szczytowy nie znaleziony — szczyt gross nieobliczony')
    if dist_var_offpeak_net is None:
        warnings.append('składnik zmienny sieciowy pozaszczytowy nie znaleziony — poza-szczyt gross nieobliczony')
    if dist_jakosciowa_net is None:
        warnings.append('stawka jakościowa nie znaleziona (użyto 0)')
    if dist_oze_net is None:
        warnings.append('opłata OZE nie znaleziona (użyto 0)')
    if dist_kogeneracja_net is None:
        warnings.append('opłata kogeneracyjna nie znaleziona (użyto 0)')

    # ── Fixed monthly charges (1 mc lines) ───────────────────────────────────
    fixed_mocowa_net      = _first_float_multi([
        r'Op.ata mocowa\s+\d+\s+mc\s+[\d,]+\s+([\d,]+)',
        r'Op.ata mocow\w*\s+\d+\s+mc\s+([\d,]+)',
    ], dist_text)
    fixed_abonament_net   = _first_float_multi([
        r'Stawka op.aty abonamentowej\s+\d+\s+mc\s+[\d,]+\s+([\d,]+)',
        r'Op.ata abonamentow\w*\s+\d+\s+mc\s+[\d,]+\s+([\d,]+)',
        r'Abonament\s+\d+\s+mc\s+[\d,]+\s+([\d,]+)',
    ], dist_text)
    fixed_stalysieciowy_net = _first_float_multi([
        r'Sk.adnik sta.y stawki sieciowej\s+\d+\s+mc\s+[\d,]+\s+([\d,]+)',
        r'Sk.adnik sta.y sieciow\w*\s+\d+\s+mc\s+[\d,]+\s+([\d,]+)',
    ], dist_text)

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
    deposit_current_pln  = _first_float_multi([
        r'Depozyt prosumencki w rozliczanym okresie[^\d]+([\d,]+)',
        r'Depozyt prosumencki w bie.*?okresie[^\d]+([\d,]+)',
        r'4\.\s*Depozyt prosumencki[^\d]+([\d,]+)',
    ], text)
    deposit_previous_pln = _first_float_multi([
        r'Depozyt prosumencki z okres.w poprzednich[^\d]+([\d,]+)',
        r'5\.\s*Depozyt prosumencki z poprz[^\d]+([\d,]+)',
    ], text)
    deposit_used_pln     = _first_float_multi([
        r'Rozliczenie depozytu \(\d\+\d\)\s+([\d,]+)',
        r'Rozliczenie depozytu\s+([\d,]+)',
        r'6\.\s*Rozliczenie depozytu[^\d]+([\d,]+)',
    ], text)

    if deposit_current_pln is None:
        warnings.append('depozyt prosumencki bieżący nie znaleziony')
    if deposit_previous_pln is None:
        warnings.append('depozyt prosumencki z poprzednich okresów nie znaleziony')
    if deposit_used_pln is None:
        warnings.append('rozliczenie depozytu nie znalezione')

    # ── Amount due ────────────────────────────────────────────────────────────
    amount_due_pln = _first_float_multi([
        r'Razem \(3-6\)\s+([\d,]+)',
        r'Razem do zap.aty\s+([\d,]+)',
        r'Do zap.aty\s+([\d,]+)',
    ], text)
    avg_price_pln_kwh = _first_float_multi([
        r'[Śś]rednia cena za 1 kWh to\s+([\d,]+)',
        r'rednia cena za 1 kWh to\s+([\d,]+)',
        r'[Śś]rednia cena 1 kWh\s+([\d,]+)',
        r'[Śś]r\.\s+cena\s+([\d,]+)',
    ], text)

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
    else:
        warnings.append('offpeak_gross nieobliczony — brakuje ceny energii lub składnika zmiennego')

    if (peak_gross is not None and offpeak_gross is not None
            and imp_peak is not None and imp_offpeak is not None):
        total_imp = imp_peak + imp_offpeak
        if total_imp > 0:
            blended_gross = round(
                (imp_peak * peak_gross + imp_offpeak * offpeak_gross) / total_imp, 4)
    elif peak_gross is not None and offpeak_gross is not None:
        # No split available — use arithmetic mean as approximation
        blended_gross = round((peak_gross + offpeak_gross) / 2, 4)
        warnings.append('blended_gross: podział szczyt/poza-szczyt niedostępny — użyto średniej arytmetycznej')

    # De-duplicate warnings generated in multiple places (e.g. peak_gross warned twice)
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

    # Append sanity-check results
    data.warnings.extend(_validate(data))
    return data
