"""Przeliczenie historii: wycena autokonsumpcji ceną strefy taryfy każdej godziny.

Dwa kroki wzorem rebase.py — nic nie jest zapisane, dopóki nie zawoła się apply():
  simulate() — tylko odczyt; raport różnic per miesiąc + ROI przed/po.
  apply()    — kopia historic.json (*.bak-<UTC>), potem zapis.

Zmienia WYŁĄCZNIE `self_consumed_savings_pln` i `sc_pricing` rekordu. Miesiąc z fakturą
zachowuje kWh z rekordu (faktura jest ostateczna); godzinowy kształt tylko je rozkłada
na strefy cenowe.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import historic_store, roi
from .models import MonthlyRecord
from .rebase import _roi_headline
from .sc_pricing import hourly_sc, zone_priced_self_consumption

logger = logging.getLogger(__name__)

FetchHourly = Callable[[int, int], tuple]
PriceAt = Callable[[str], Optional[float]]


def _default_fetch_hourly(year: int, month: int) -> tuple:
    from . import live_reader
    sources = live_reader.get_energy_dashboard_sources()
    return live_reader.get_hourly_solar_export(sources, year, month)


def _reprice_record(rec: dict, frozen: bool, fetch_hourly: FetchHourly,
                    price_at: PriceAt) -> tuple:
    """-> (new_pln | None, tag, method, kwh). new_pln None = wartość bez zmian."""
    old_kwh = rec.get('self_consumed_kwh')
    produced_h, exported_h = fetch_hourly(rec['year'], rec['month'])
    fallback = (None, 'monthly_avg', 'monthly_avg', old_kwh)
    if not produced_h or not exported_h:
        return fallback

    if not frozen:
        res = zone_priced_self_consumption(produced_h, exported_h, price_at)
        if res is None:
            return fallback
        kwh, pln = res
        return pln, 'hourly_zone', 'hourly_zone', kwh

    if not isinstance(old_kwh, (int, float)):
        return fallback
    sc_h = hourly_sc(produced_h, exported_h)
    total = sum(sc_h.values())
    if total <= 0:
        return fallback
    factor = old_kwh / total
    pln = 0.0
    for hour, sc in sc_h.items():
        if sc <= 0:
            continue
        price = price_at(hour)
        if price is None:
            logger.warning('sc_reprice: brak ceny dla godziny %s — miesiąc bez zmian', hour)
            return fallback
        pln += sc * factor * price
    return round(pln, 2), 'hourly_zone', 'hourly_zone_invoice', old_kwh


def _build(records: list, reconciled_months: set, fetch_hourly: FetchHourly,
           price_at: Optional[PriceAt], roi_kwargs: dict) -> tuple:
    if price_at is None:
        raise ValueError('price_at is required')
    months_out, unchanged = [], []
    new_dicts: list[dict] = []

    for r in sorted(records, key=lambda rec: (rec.year, rec.month)):
        d = r.to_dict()
        ym = f'{r.year}-{r.month:02d}'
        if not historic_store.has_energy_data(r.produced_kwh):
            unchanged.append(ym)
            new_dicts.append(d)
            continue
        frozen = (r.year, r.month) in reconciled_months
        new_pln, tag, method, kwh = _reprice_record(d, frozen, fetch_hourly, price_at)
        old_pln = d.get('self_consumed_savings_pln')
        if new_pln is not None:
            d['self_consumed_savings_pln'] = new_pln
        if new_pln is not None or d.get('sc_pricing') != 'hourly_zone':
            d['sc_pricing'] = tag  # chwilowy brak danych nie degraduje wyceny godzinowej
        shown_new = new_pln if new_pln is not None else old_pln
        delta = (round(shown_new - old_pln, 2)
                 if isinstance(shown_new, (int, float)) and isinstance(old_pln, (int, float))
                 else None)
        months_out.append({'ym': ym, 'method': method, 'old_pln': old_pln,
                           'new_pln': shown_new, 'delta_pln': delta,
                           'kwh': kwh, 'frozen': frozen})
        new_dicts.append(d)

    report = {
        'months': months_out,
        'unchanged': unchanged,
        'roi_before': _roi_headline(roi.calculate(records, **roi_kwargs)),
        'roi_after': _roi_headline(roi.calculate([MonthlyRecord.from_dict(d) for d in new_dicts], **roi_kwargs)),
        'total_delta_pln': round(sum(m['delta_pln'] or 0.0 for m in months_out), 2),
    }
    return report, new_dicts


def simulate(records: list, reconciled_months: Optional[set] = None,
             fetch_hourly: Optional[FetchHourly] = None, price_at: Optional[PriceAt] = None,
             roi_kwargs: Optional[dict] = None) -> dict:
    """Tylko odczyt — nie dotyka historic.json."""
    report, _ = _build(records, reconciled_months or set(), fetch_hourly or _default_fetch_hourly,
                       price_at, roi_kwargs or {})
    return report


def _write_backup(path: Path) -> Path:
    """Kopia `<plik>.bak-<UTC z mikrosekundami>`; istniejącej kopii nigdy nie nadpisuje
    (tryb 'x', przy kolizji sufiks -1, -2, …)."""
    base = f"{path.name}.bak-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}"
    content = path.read_text(encoding='utf-8')
    n = 0
    while True:
        target = path.with_name(base if n == 0 else f'{base}-{n}')
        try:
            with open(target, 'x', encoding='utf-8') as fh:
                fh.write(content)
            return target
        except FileExistsError:
            n += 1


def apply(path: Path = historic_store.DEFAULT_PATH, reconciled_months: Optional[set] = None,
          fetch_hourly: Optional[FetchHourly] = None, price_at: Optional[PriceAt] = None,
          roi_kwargs: Optional[dict] = None) -> dict:
    """Kopia `<plik>.bak-<UTC>` PRZED zapisem, potem zapis przeliczonych rekordów.
    Wynik jak simulate() + 'backup_path'. Idempotentny."""
    doc = historic_store._load_document(path)
    records = [MonthlyRecord.from_dict(m) for m in doc.get('months', [])]
    report, new_dicts = _build(records, reconciled_months or set(),
                               fetch_hourly or _default_fetch_hourly, price_at, roi_kwargs or {})

    backup_path = None
    if path.exists():
        backup_path = _write_backup(path)

    # Pobieranie godzinowe jest wolne — przed zapisem czytamy dokument ponownie i nakładamy
    # tylko dwa pola, żeby nie nadpisać zmian zapisanych w międzyczasie (faktura, zamknięcie miesiąca).
    repriced = {(d['year'], d['month']): d for d in new_dicts}
    doc = historic_store._load_document(path)
    for m in doc.get('months', []):
        new = repriced.get((m.get('year'), m.get('month')))
        if new is not None:
            m['self_consumed_savings_pln'] = new.get('self_consumed_savings_pln')
            m['sc_pricing'] = new.get('sc_pricing')
    historic_store._save_document(doc, path)
    logger.info('sc_reprice applied: %d month(s), backup %s', len(report['months']), backup_path)
    report['backup_path'] = str(backup_path) if backup_path else None
    return report
