"""
Tauron invoice PDF parser.

parse_invoice(pdf_bytes) -> InvoiceData

Extracts billing kWh, tariff rates, prosument deposit and amount-due from
the Tauron G12W net-billing (prosument) PDF layout confirmed against
invoice T/K1/BN567872/0011/26 (April 2026).

pypdf mangles Polish diacritics in certain PDFs (ł→ø, ą→a, etc.) so all
regex patterns match on diacritic-neutral substrings or ASCII-safe text.
Defensive: missing individual fields become None; raises InvoiceParseError
only if the billing month or import/export totals cannot be extracted.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
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


# ── Number parsing helpers ────────────────────────────────────────────────────

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


# ── Main parser ───────────────────────────────────────────────────────────────

def parse_invoice(pdf_bytes: bytes) -> InvoiceData:
    """
    Parse a Tauron PDF invoice and return an InvoiceData instance.

    Raises InvoiceParseError if the billing month or import/export totals
    cannot be found.
    """
    try:
        from pypdf import PdfReader
        import io
        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages = [p.extract_text() or '' for p in reader.pages]
        text = '\n'.join(pages)
    except Exception as exc:
        raise InvoiceParseError(f'PDF extraction failed: {exc}') from exc

    # Normalise: collapse runs of whitespace (pypdf sometimes breaks numbers)
    text = re.sub(r'[ \t]+', ' ', text)

    # ── Billing period ────────────────────────────────────────────────────────
    # "Okres rozliczeniowy 01.04.2026 - 30.04.2026"  (ł mangles to ø in pypdf)
    period_m = re.search(
        r'Okres rozliczeniowy\s+(\d{2})\.(\d{2})\.(\d{4})',
        text)
    if not period_m:
        raise InvoiceParseError('Billing period (Okres rozliczeniowy) not found — not a Tauron invoice')
    day_s, mon_s, year_s = period_m.group(1), period_m.group(2), period_m.group(3)
    year = int(year_s)
    month = int(mon_s)
    billing_period_raw = _first(
        r'Okres rozliczeniowy\s+(\d{2}\.\d{2}\.\d{4} - \d{2}\.\d{2}\.\d{4})', text)

    # ── Invoice number ────────────────────────────────────────────────────────
    invoice_number = _first(r'Numer faktury\s+([\w/]+)', text)

    # ── Import / export totals ────────────────────────────────────────────────
    # "Pobrano z sieci 215" / "Wprowadzono do sieci 468"
    imp_total = _first_float(r'Pobrano z sieci\s+([\d,]+)', text)
    if imp_total is None:
        # Fallback: might appear as "Pobrane z sieci"
        imp_total = _first_float(r'Pobran[ae] z sieci\s+([\d,]+)', text)
    if imp_total is None:
        raise InvoiceParseError('Imported kWh (Pobrano z sieci) not found')

    exp_total = _first_float(r'Wprowadzono do sieci\s+([\d,]+)', text)
    if exp_total is None:
        raise InvoiceParseError('Exported kWh (Wprowadzono do sieci) not found')

    # ── Peak / offpeak import from Sprzedaż section ───────────────────────────
    # "szczytowa 14 kWh 0,62700 …" / "pozaszczytowa 201 kWh 0,41800 …"
    # Also handles "szczyt" abbreviation used in some layouts
    imp_peak     = _first_float(r'(?:Energia czynna\s+)?szczytowa\s+([\d,]+)\s+kWh\s+([\d,]+)', text, 1)
    imp_offpeak  = _first_float(r'(?:Energia czynna\s+)?pozaszczytowa\s+([\d,]+)\s+kWh\s+([\d,]+)', text, 1)
    energy_peak_net    = _first_float(r'(?:Energia czynna\s+)?szczytowa\s+[\d,]+\s+kWh\s+([\d,]+)', text)
    energy_offpeak_net = _first_float(r'(?:Energia czynna\s+)?pozaszczytowa\s+[\d,]+\s+kWh\s+([\d,]+)', text)

    # ── Peak / offpeak export from meter reading section ──────────────────────
    # Layout (two lines after the (oddanie) counter line):
    #   szczyt 30.04.2026 (Zdalny) 230,0000
    #   pozaszczytowa 30.04.2026 (Zdalny) 238,0000
    # The date/status separates the label from the value, so skip non-digit chars safely.
    _exp_m = re.search(r'\(oddanie\)-\d+\nszczyt\s+[\d.]+\s+\([^\)]+\)\s+([\d,]+)', text)
    if _exp_m:
        exp_peak = _n(_exp_m.group(1))
    else:
        exp_peak = None
    _exp_m2 = re.search(r'\(oddanie\)-\d+\n[^\n]+\npozaszczytowa\s+[\d.]+\s+\([^\)]+\)\s+([\d,]+)', text)
    if _exp_m2:
        exp_offpeak = _n(_exp_m2.group(1))
    else:
        exp_offpeak = None

    # ── Distribution variable components ─────────────────────────────────────
    # The Tauron distribution table has heading lines followed by the zone rows:
    #   Skøadnik zmienny stawki sieciowej
    #   szczytowa 14 kWh 0,32980 ...
    #   pozaszczytowa 201 kWh 0,05120 ...
    #   Stawka jakościowa
    #   szczytowa 14 kWh 0,03320 ...
    # We scope to the distribution section and match heading + next szczytowa line.

    dist_section_m = re.search(r'Dystrybucja energii elektrycznej.*?Razem za dystrybucj', text, re.DOTALL)
    dist_text = dist_section_m.group(0) if dist_section_m else text

    def _dist_peak(section_pattern: str) -> Optional[float]:
        """Find heading matching section_pattern, return price on next szczytow* line."""
        m = re.search(section_pattern + r'\s+szczyt\w*\s+\d+\s+kWh\s+([\d,]+)', dist_text, re.IGNORECASE)
        if m:
            return _n(m.group(1))
        return None

    def _dist_offpeak(section_pattern: str) -> Optional[float]:
        m = re.search(section_pattern + r'\s+\w*szczyt\w*\s+\d+\s+kWh\s+([\d,]+)\s+\d+\s+\d+\s+\d+,\d+\s+\d+,\d+\s+\w+\w*\s+(\d+)\s+kWh\s+([\d,]+)', dist_text, re.IGNORECASE)
        if m:
            return _n(m.group(3))
        # Simpler: find section heading, skip the peak line, grab the next kWh price
        m2 = re.search(section_pattern + r'\s+\S+\s+\d+\s+kWh\s+[\d,]+[^\n]+\n\s*\S+\s+\d+\s+kWh\s+([\d,]+)', dist_text, re.IGNORECASE)
        if m2:
            return _n(m2.group(1))
        return None

    # Składnik zmienny stawki sieciowej (ł→ø in pypdf → match with .)
    dist_var_peak_net    = _dist_peak(r'Sk.adnik zmienny stawki sieciowej')
    dist_var_offpeak_net = _dist_offpeak(r'Sk.adnik zmienny stawki sieciowej')

    # Stawka jakościowa — ś→regular in some versions; . covers both
    dist_jakosciowa_net  = _dist_peak(r'Stawka jako.ciow\w*')

    # Opłata OZE (ł→ø or preserved)
    dist_oze_net         = _dist_peak(r'Op.ata OZE')

    # Opłata kogeneracyjna
    dist_kogeneracja_net = _dist_peak(r'Op.ata kogeneracyjna')

    # ── Fixed monthly charges (1 mc lines) ───────────────────────────────────
    # "Opøata mocowa 1 mc 24,05000 24,05 …"
    # Match: [label] 1 mc [full_price] [net] …  — we want the second decimal (net)
    fixed_mocowa_net      = _first_float(r'Op.ata mocowa\s+\d+\s+mc\s+[\d,]+\s+([\d,]+)', dist_text)
    fixed_abonament_net   = _first_float(r'Stawka op.aty abonamentowej\s+\d+\s+mc\s+[\d,]+\s+([\d,]+)', dist_text)
    fixed_stalysieciowy_net = _first_float(r'Sk.adnik sta.y stawki sieciowej\s+\d+\s+mc\s+[\d,]+\s+([\d,]+)', dist_text)

    fixed_total_net: Optional[float] = None
    if all(v is not None for v in (fixed_mocowa_net, fixed_abonament_net, fixed_stalysieciowy_net)):
        fixed_total_net = round(fixed_mocowa_net + fixed_abonament_net + fixed_stalysieciowy_net, 4)  # type: ignore[operator]

    # ── Prosument deposit ─────────────────────────────────────────────────────
    # Line 4: "Depozyt prosumencki w rozliczanym okresie"
    # Line 5: "Depozyt prosumencki z okresów poprzednich"
    # Line 6: "Rozliczenie depozytu (4+5)"
    deposit_current_pln  = _first_float(r'Depozyt prosumencki w rozliczanym okresie[^\d]+([\d,]+)', text)
    deposit_previous_pln = _first_float(r'Depozyt prosumencki z okres.w poprzednich[^\d]+([\d,]+)', text)
    # "6. Rozliczenie depozytu (4+5) 72,48" — [^\d]+ stops at '4', need explicit pattern
    deposit_used_pln     = _first_float(r'Rozliczenie depozytu \(\d\+\d\)\s+([\d,]+)', text)

    # ── Amount due ────────────────────────────────────────────────────────────
    # "Razem (3-6) 120,04"
    amount_due_pln = _first_float(r'Razem \(3-6\)\s+([\d,]+)', text)
    # "Średnia cena za 1 kWh to 0,90 zł." — Ś may be preserved or → S
    avg_price_pln_kwh = _first_float(r'[Śś]rednia cena za 1 kWh to\s+([\d,]+)', text)
    if avg_price_pln_kwh is None:
        avg_price_pln_kwh = _first_float(r'rednia cena za 1 kWh to\s+([\d,]+)', text)

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

    if energy_offpeak_net is not None and dist_var_offpeak_net is not None:
        offpeak_var_net = (energy_offpeak_net
                           + dist_var_offpeak_net
                           + (dist_jakosciowa_net or 0.0)
                           + (dist_oze_net or 0.0)
                           + (dist_kogeneracja_net or 0.0))
        offpeak_gross = round(offpeak_var_net * VAT, 4)

    if (peak_gross is not None and offpeak_gross is not None
            and imp_peak is not None and imp_offpeak is not None):
        total_imp = imp_peak + imp_offpeak
        if total_imp > 0:
            blended_gross = round(
                (imp_peak * peak_gross + imp_offpeak * offpeak_gross) / total_imp, 4)

    return InvoiceData(
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
    )
