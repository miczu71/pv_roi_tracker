"""Depozyt prosumencki — czysty ledger FIFO z 12-mies. przedawnieniem i limitem zwrotu.

Bez I/O i efektów ubocznych (styl jak roi.py).

Reguły (ustawa o OZE, art. 4 ust. 11):
  • środki przypisane do depozytu w miesiącu M są rozliczane przez 12 miesięcy
    od przypisania — konsumpcja zdejmuje najstarsze partie (FIFO);
  • po upływie 12 miesięcy niewykorzystana część partii staje się nadpłatą:
    sprzedawca zwraca maks. refund_cap × wartość energii wprowadzonej w miesiącu,
    którego dotyczy zwrot (20% standardowo; 30% przy rozliczeniu godzinowym RCE
    po nowelizacji z 27.11.2024), reszta jest umarzana.

Zasilenie miesięczne = feedin_revenue_pln (eksport × RCEm, ×1,23 od 2025-02).
Konsumpcja: faktyczna z faktur (deposit_used), a dla miesięcy bez faktury —
szacunek purchased_kwh × buy_price.

Uwaga na lag Taurona: depozyt jest dopisywany 1–2 mies. po miesiącu eksportu,
więc saldo modelowe rozjeżdża się z fakturowym miesiąc-po-miesiącu; sumy
skumulowane są zbieżne. Najlepszy szacunek bieżącego salda kotwiczymy na
ostatniej fakturze (dep_previous) + delty z falownika, a strukturę wiekową
partii skalujemy proporcjonalnie do tego szacunku.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from statistics import mean
from typing import Optional

from dateutil.relativedelta import relativedelta

from .models import MonthlyRecord

REFUND_CAP_DEFAULT = 0.20   # RCEm; 0.30 przy rozliczeniu godzinowym RCE
EXPIRY_MONTHS = 12


@dataclass
class _Lot:
    """Jedna partia depozytu (zasilenie z jednego miesiąca)."""
    ym: str           # YYYY-MM przypisania
    original: float   # pełne zasilenie (baza limitu zwrotu)
    remaining: float


@dataclass
class DepositResult:
    # Saldo
    balance_model: float                       # czysty model FIFO od początku
    invoice_latest_balance: Optional[float]    # dep_previous z ostatniej faktury
    invoice_latest_month: Optional[str]
    balance_estimate: Optional[float]          # kotwica fakturowa + delty falownika
    # Przedawnienie — historia
    expired_refund_total: float                # zwrócone nadpłaty (model, dotychczas)
    expired_forfeit_total: float               # umorzone (model, dotychczas)
    # Przedawnienie — prognoza (saldo przeskalowane do balance_estimate)
    expiring_1m: float                         # partia tracąca ważność w przyszłym mies.
    expiring_3m: float
    projected_refund_12m: float
    projected_forfeit_12m: float
    refund_cap_pct: float
    # Struktura wiekowa bieżących partii (do wykresu)
    lots: list = field(default_factory=list)   # [{ym, expires, remaining, original}]
    # Prognoza miesięczna (do wykresu)
    forecast: list = field(default_factory=list)  # [{ym, accrued, consumed, expired_refund, expired_forfeit, balance}]
    months: list = field(default_factory=list)    # historia [{ym, accrued, consumed, expired_refund, expired_forfeit, balance, invoice_balance}]


def _ym_str(d: date) -> str:
    return f'{d.year}-{d.month:02d}'


def _expire(lots: list[_Lot], current_ym: str, cap: float) -> tuple[float, float]:
    """Usuwa partie starsze niż EXPIRY_MONTHS; zwraca (zwrot, umorzenie)."""
    y, m = int(current_ym[:4]), int(current_ym[5:7])
    cutoff = date(y, m, 1) - relativedelta(months=EXPIRY_MONTHS)
    cutoff_ym = _ym_str(cutoff)
    refund = forfeit = 0.0
    keep: list[_Lot] = []
    for lot in lots:
        if lot.ym <= cutoff_ym and lot.remaining > 0:
            r = min(lot.remaining, cap * lot.original)
            refund += r
            forfeit += lot.remaining - r
        elif lot.ym > cutoff_ym:
            keep.append(lot)
    lots[:] = keep
    return refund, forfeit


def _consume(lots: list[_Lot], amount: float) -> float:
    """Zdejmuje FIFO; zwraca faktycznie skonsumowaną kwotę."""
    consumed = 0.0
    for lot in lots:
        if amount <= 0:
            break
        take = min(lot.remaining, amount)
        lot.remaining -= take
        amount -= take
        consumed += take
    lots[:] = [lot for lot in lots if lot.remaining > 0.005]
    return consumed


def _seasonal_factors(values: list[tuple[int, float]]) -> dict[int, float]:
    """{miesiąc: indeks sezonowy} z par (miesiąc, wartość>0)."""
    by_month: dict[int, list[float]] = {}
    for m, v in values:
        if v > 0:
            by_month.setdefault(m, []).append(v)
    if len(by_month) < 2:
        return {m: 1.0 for m in range(1, 13)}
    means = {m: mean(vs) for m, vs in by_month.items()}
    overall = mean(means.values())
    if overall <= 0:
        return {m: 1.0 for m in range(1, 13)}
    return {m: means.get(m, overall) / overall for m in range(1, 13)}


def calculate(
    records: list[MonthlyRecord],
    invoice_data: dict,
    today: Optional[date] = None,
    refund_cap: float = REFUND_CAP_DEFAULT,
    forecast_months: int = 12,
) -> DepositResult:
    """Ledger FIFO z przedawnieniem + prognoza.

    Args:
        records:      MonthlyRecords (historic + bieżący)
        invoice_data: invoice_store.load() — {YYYY-MM: dict} (+ klucze unparsed-*)
        refund_cap:   limit zwrotu nadpłaty (0.20 RCEm / 0.30 RCE godzinowa)
    """
    if today is None:
        today = date.today()
    today_ym = (today.year, today.month)

    complete = sorted([r for r in records if (r.year, r.month) < today_ym],
                      key=lambda r: (r.year, r.month))

    lots: list[_Lot] = []
    expired_refund_total = expired_forfeit_total = 0.0
    months_out: list[dict] = []

    for r in complete:
        mk = f'{r.year}-{r.month:02d}'
        # 1. przedawnienie na początku miesiąca
        ref, forf = _expire(lots, mk, refund_cap)
        expired_refund_total += ref
        expired_forfeit_total += forf
        # 2. zasilenie
        accrued = r.feedin_revenue_pln or 0.0
        if accrued > 0:
            lots.append(_Lot(ym=mk, original=accrued, remaining=accrued))
        # 3. konsumpcja: faktura jeśli jest, inaczej szacunek z falownika
        inv = invoice_data.get(mk) or {}
        inv_used = inv.get('deposit_used_pln')
        want = inv_used if inv_used is not None else round(
            (r.purchased_kwh or 0.0) * (r.buy_price_pln_kwh or 0.0), 2)
        consumed = _consume(lots, want or 0.0)
        balance = round(sum(lot.remaining for lot in lots), 2)
        months_out.append({
            'ym': mk,
            'accrued': round(accrued, 2),
            'consumed': round(consumed, 2),
            'consumed_source': 'faktura' if inv_used is not None else 'szacunek',
            'expired_refund': round(ref, 2),
            'expired_forfeit': round(forf, 2),
            'balance': balance,
            'invoice_balance': inv.get('deposit_previous_pln'),
        })

    balance_model = round(sum(lot.remaining for lot in lots), 2)

    # ── Kotwica fakturowa ─────────────────────────────────────────────────────
    invoice_latest_balance: Optional[float] = None
    invoice_latest_month: Optional[str] = None
    for row in reversed(months_out):
        if row['invoice_balance'] is not None:
            invoice_latest_balance = row['invoice_balance']
            invoice_latest_month = row['ym']
            break

    balance_estimate: Optional[float] = None
    if invoice_latest_balance is not None:
        est = invoice_latest_balance
        for row in months_out:
            if row['ym'] <= invoice_latest_month:
                continue
            est = max(0.0, est + row['accrued'] - row['consumed'])
        # bieżący (częściowy) miesiąc
        curr = next((r for r in records if (r.year, r.month) == today_ym), None)
        if curr:
            est = max(0.0, est + (curr.feedin_revenue_pln or 0.0)
                      - (curr.purchased_kwh or 0.0) * (curr.buy_price_pln_kwh or 0.0))
        balance_estimate = round(est, 2)

    # Skalowanie struktury wiekowej do najlepszego szacunku salda
    scale = 1.0
    if balance_estimate is not None and balance_model > 0:
        scale = balance_estimate / balance_model
    scaled_lots = [_Lot(ym=lot.ym, original=lot.original, remaining=lot.remaining * scale)
                   for lot in lots]

    lots_out = [{
        'ym': lot.ym,
        'expires': _ym_str(date(int(lot.ym[:4]), int(lot.ym[5:7]), 1)
                           + relativedelta(months=EXPIRY_MONTHS)),
        'remaining': round(lot.remaining, 2),
        'original': round(lot.original, 2),
    } for lot in scaled_lots]

    # ── Prognoza 12 mies. (sezonowe średnie zasileń i konsumpcji) ────────────
    sf_acc = _seasonal_factors([(int(m['ym'][5:7]), m['accrued']) for m in months_out])
    sf_con = _seasonal_factors([(int(m['ym'][5:7]), m['consumed']) for m in months_out])
    win = months_out[-12:]
    avg_acc = mean([m['accrued'] for m in win]) if win else 0.0
    avg_con = mean([m['consumed'] for m in win]) if win else 0.0

    fc_lots = [_Lot(ym=lot.ym, original=lot.original, remaining=lot.remaining)
               for lot in scaled_lots]
    forecast: list[dict] = []
    proj_refund_12m = proj_forfeit_12m = 0.0
    cursor = date(today.year, today.month, 1) + relativedelta(months=1)
    for _ in range(forecast_months):
        mk = _ym_str(cursor)
        ref, forf = _expire(fc_lots, mk, refund_cap)
        proj_refund_12m += ref
        proj_forfeit_12m += forf
        acc = round(avg_acc * sf_acc.get(cursor.month, 1.0), 2)
        if acc > 0:
            fc_lots.append(_Lot(ym=mk, original=acc, remaining=acc))
        con = _consume(fc_lots, round(avg_con * sf_con.get(cursor.month, 1.0), 2))
        forecast.append({
            'ym': mk,
            'accrued': acc,
            'consumed': round(con, 2),
            'expired_refund': round(ref, 2),
            'expired_forfeit': round(forf, 2),
            'balance': round(sum(lot.remaining for lot in fc_lots), 2),
        })
        cursor += relativedelta(months=1)

    expiring_1m = round(forecast[0]['expired_refund'] + forecast[0]['expired_forfeit'], 2) if forecast else 0.0
    expiring_3m = round(sum(f['expired_refund'] + f['expired_forfeit'] for f in forecast[:3]), 2)

    return DepositResult(
        balance_model=balance_model,
        invoice_latest_balance=invoice_latest_balance,
        invoice_latest_month=invoice_latest_month,
        balance_estimate=balance_estimate,
        expired_refund_total=round(expired_refund_total, 2),
        expired_forfeit_total=round(expired_forfeit_total, 2),
        expiring_1m=expiring_1m,
        expiring_3m=expiring_3m,
        projected_refund_12m=round(proj_refund_12m, 2),
        projected_forfeit_12m=round(proj_forfeit_12m, 2),
        refund_cap_pct=round(refund_cap * 100, 0),
        lots=lots_out,
        forecast=forecast,
        months=months_out,
    )
