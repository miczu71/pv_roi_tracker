# PLAN sc_zone_pricing E1 — wycena godzinowa autokonsumpcji (bieżący miesiąc + re-read miesiąca)

Etap 1 z `ROADMAP_sc_zone_pricing.md`. Repo `~/dev/pv_roi_tracker`; kod w `pv_roi_tracker/pv_roi_tracker/` (dalej `PKG/`), testy w `pv_roi_tracker/tests/`.
Komenda testów (z katalogu add-onu `<WT>/pv_roi_tracker`):
`uv run --quiet --no-project --python 3.12 --with pytest --with-requirements requirements.txt pytest -q`
Baseline przed zmianą: 1544 passed, 23 skipped.

## Decyzje projektowe E1
- `self_consumed_savings_pln` = Σ_h max(0, produkcja_h − eksport_h) × cena_brutto_strefy_godziny_h (godzina lokalna, klucz `'YYYY-MM-DDTHH'`).
  Cena z `TariffModule.price_at(dt).gross` dla KAŻDEJ taryfy (też G12w; nie `_hour_price_fn_for`, bo ta zwraca None dla G11/G12/G12w).
- Monotoniczność: każdy składnik ≥ 0, więc dokładanie godzin nigdy nie zmniejsza sumy.
- Bez zmian: `self_consumed_kwh` (nadal `produced − exported` z miesięcznego LTS), `buy_price_pln_kwh` (średnia ważona importu — używana do `purchase_cost_pln`), `consumed_kwh`.
- Fallback: brak godzinowych LTS (produkcji lub eksportu), brak ceny dla godziny z autokonsumpcją > 0, albo brak `price_at` → stara wycena (`sc × buy_price`), `sc_pricing='monthly_avg'` (gdy `price_at` podane) albo `None` (gdy nie podane, np. CLI).
- Nowe pole rekordu `sc_pricing: Optional[str]` — `'hourly_zone'` | `'monthly_avg'` | `None`.
- Znane ryzyko do E2: `rebase._default_fetch` (`rebase.py:71`) wywołuje `read_month_from_statistics` bez `price_at` → `rebase apply` nadpisałby wycenę starą metodą. W E1 NIE uruchamiamy rebase; naprawa w E2.
- Znane ryzyko do E2: `historic_store.reconcile_invoice` (`historic_store.py:326`) po fakturze nadpisuje `self_consumed_savings_pln` = sc × blended — to E2.

## Wykonawca

### Paczka W1 — jedna paczka (haiku-worker; REJECT → sonnet-worker)
Pliki (nowe: `PKG/sc_pricing.py`, `tests/test_sc_pricing.py`; edytowane: `PKG/models.py`, `PKG/live_reader.py`, `PKG/main.py`, `PKG/month_close.py`, `tests/test_live_reader_rebase.py`, `tests/test_month_close.py`).

Zmiany (TDD: najpierw testy, potem kod):
1. **`PKG/sc_pricing.py` (nowy)** — dosłownie:
```python
"""Wycena autokonsumpcji PV godzina po godzinie ceną strefy taryfy tej godziny.

Powód: wycena miesięcznej autokonsumpcji średnią ceną importu (live_reader) spadała w nocy
(tani import obniża średnią) i rosła wieczorem bez produkcji. Tu każda godzina dokłada
kWh × cena swojej strefy, więc suma w miesiącu nigdy nie maleje."""
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def zone_priced_self_consumption(
    produced_h: dict,
    exported_h: dict,
    price_at: Callable[[str], Optional[float]],
) -> Optional[tuple]:
    """(kWh, PLN) autokonsumpcji z godzin; None gdy brak danych godzinowych lub ceny.

    produced_h/exported_h: {'YYYY-MM-DDTHH': kWh}. Brak godziny w exported_h = eksport 0.
    price_at(hour_key) -> cena brutto PLN/kWh albo None."""
    if not produced_h or not exported_h:
        return None
    kwh = 0.0
    pln = 0.0
    for hour in sorted(produced_h):
        sc = max(0.0, produced_h[hour] - exported_h.get(hour, 0.0))
        if sc <= 0.0:
            continue
        price = price_at(hour)
        if price is None:
            logger.warning('zone_priced_self_consumption: brak ceny dla godziny %s — fallback na średnią miesięczną', hour)
            return None
        kwh += sc
        pln += sc * price
    return round(kwh, 3), round(pln, 2)
```
2. **`PKG/models.py`**: w `MonthlyRecord` po `cross_family_produced_kwh` dodaj `sc_pricing: Optional[str] = None  # 'hourly_zone' | 'monthly_avg' | None` z jednozdaniowym komentarzem po polsku.
3. **`PKG/live_reader.py`**:
   a. `get_hourly_role_kwh(entity_ids: list, year: int, month: int) -> dict` — obok `get_hourly_energy` (:333), te same wzorce: `_ws_statistics(entity_ids, start_iso, 'hour', end_iso=end_iso, timeout=60)`, granice miesiąca lokalnego `[1. dzień miesiąca, 1. dzień następnego)` konwertowane na UTC w formacie `YYYY-MM-DDTHH:MM:SS+00:00` (jak `main.py:1046` — `datetime(...).astimezone().astimezone(timezone.utc)`), klucz godziny `_dt.fromtimestamp(start_ms/1000).strftime('%Y-%m-%dT%H')`, sumowanie `change` po wszystkich encjach i kolizjach DST (akumuluj, nie nadpisuj), `change < 0` → pomiń z `logger.warning`, `try/except Exception` → `logger.warning` + `{}`; pusta lista encji → `{}`.
   b. `_zone_priced_sc(year, month, sources, price_at) -> tuple` zwraca `(pln | None, tag | None)`: `price_at is None` → `(None, None)`; produkcja = `get_hourly_role_kwh(sources['solar'], year, month)`, eksport = `get_hourly_role_kwh(sources['grid_export'], year, month)`; `zone_priced_self_consumption(...)` None → `(None, 'monthly_avg')`; inaczej `(pln, 'hourly_zone')`.
   c. `_build_record(...)`: nowe argumenty `sc_savings_pln: Optional[float] = None`, `sc_pricing: Optional[str] = None` (na końcu listy, z docstringiem po polsku); po wyliczeniu `self_consumed_savings_pln` (:806): `if sc_savings_pln is not None: self_consumed_savings_pln = round(sc_savings_pln, 2)`; `sc_pricing=sc_pricing` w `MonthlyRecord(...)`.
   d. `read_current_month(...)` i `read_month_from_statistics(...)`: nowy argument `price_at: Optional[Callable[[str], Optional[float]]] = None`; po wyznaczeniu `sources` wołaj `sc_pln, sc_tag = _zone_priced_sc(year, month, sources, price_at)` i przekaż `sc_savings_pln=sc_pln, sc_pricing=sc_tag` do `_build_record`. Import `Callable` z `typing`.
4. **`PKG/month_close.py`**: `close_month(..., price_at=None)` przekazuje `price_at=price_at` do `live_reader.read_current_month` (docstring: jedno zdanie).
5. **`PKG/main.py`**:
   a. Obok `_zone_prices_for` (:1018) funkcja `_sc_price_at()` zwracająca `fn(hour_key: str) -> Optional[float]`: `tmod = _tariff_module_now()`; `fn` robi `tmod.price_at(datetime.strptime(hour_key, '%Y-%m-%dT%H')).gross`, przy `LookupError`/`ValueError` zwraca None. (Użyj już zaimportowanego `datetime` w main.py; jeśli go nie ma w zasięgu — `_dt_now` pokazuje, jak jest zaimportowany; sprawdź grepem.)
   b. Przekaż `price_at=_sc_price_at()` w trzech wywołaniach: `_reread_month` (:415–419, `read_month_from_statistics`), `poll_and_publish` (:1371–1374, `read_current_month`), `month_close_job` (:1733–1736, `close_month`).
6. **Testy**:
   - `tests/test_sc_pricing.py` (nowy): (a) dwie godziny w różnych strefach → suma kWh×cena; (b) godzina z eksportem > produkcją → 0; (c) monotoniczność: dla kolejnych prefiksów godzin `pln` nie maleje; (d) brak ceny dla godziny z sc>0 → None; (e) puste `produced_h` lub `exported_h` → None; (f) brak godziny w `exported_h` = eksport 0; (g) godziny z sc==0 nie wołają `price_at`.
   - `tests/test_live_reader_rebase.py`: `_build_record` z `sc_savings_pln=123.45, sc_pricing='hourly_zone'` → `self_consumed_savings_pln == 123.45` i `sc_pricing == 'hourly_zone'`; bez nich → `sc_pricing is None` i `self_consumed_savings_pln == round(sc*buy,2)` (jak dotąd); `_zone_priced_sc` z `patch.object(lr, 'get_hourly_role_kwh', ...)`: sukces → `(pln, 'hourly_zone')`, pusta produkcja → `(None, 'monthly_avg')`, `price_at=None` → `(None, None)`; `get_hourly_role_kwh` z `patch.object(lr, '_ws_statistics', ...)`: sumuje dwie encje, pomija ujemny `change`, zwraca `{}` gdy `_ws_statistics` rzuca.
   - `tests/test_month_close.py`: `close_month(price_at=fn)` przekazuje `price_at=fn` do `live_reader.read_current_month` (wzorem istniejących testów w pliku).

`DONE:` komenda testów zielona (≥1544 passed + nowe testy, 0 failed); `grep -n "price_at=_sc_price_at()" pv_roi_tracker/pv_roi_tracker/main.py` daje 3 trafienia; `git status --short` w worktree pokazuje wyłącznie pliki z listy powyżej.
`NIE RÓB:` zmian w `rebase.py`, `historic_store.py`, `invoice_store.py`, `roi.py`, `web.py`, `publisher.py`; zmiany semantyki `weighted_buy_price`/`buy_price_pln_kwh`/`self_consumed_kwh`; commitu (robi rodzic); `rm -rf`, `--no-verify`; dotykania `/config/addons/pv_roi_tracker` (to kopia HA, synchronizacja przy wydaniu).
