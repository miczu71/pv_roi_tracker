# PLAN sc_zone_pricing E2 — przeliczenie historii (`sc_reprice` simulate/apply) [ryzyko → sonnet-worker]

Etap 2 z `ROADMAP_sc_zone_pricing.md`. Repo `~/dev/pv_roi_tracker`; kod w `pv_roi_tracker/pv_roi_tracker/` (dalej `PKG/`), testy w `pv_roi_tracker/tests/`.
Komenda testów (z `<WT>/pv_roi_tracker`):
`uv run --quiet --no-project --python 3.12 --with pytest --with-requirements requirements.txt pytest -q`
Baseline: 1560 passed, 23 skipped.

## Decyzje projektowe E2
- Nowy moduł `PKG/sc_reprice.py` wzorem `rebase.py`: `simulate()` (tylko odczyt) i `apply()` (kopia + zapis). Zmienia WYŁĄCZNIE `self_consumed_savings_pln` i `sc_pricing` rekordu.
- Dane godzinowe: `live_reader.get_hourly_role_kwh(sources['solar'|'grid_export'], y, m)` (`sources = live_reader.get_energy_dashboard_sources()`), ceny: `price_at(hour_key)` jak w E1 (`main._sc_price_at()`, taryfa obowiązująca w danej godzinie). Wstrzykiwane jako `fetch_hourly(y, m) -> (produced_h, exported_h)` i `price_at`, żeby testy nie wołały HA.
- Miesiąc BEZ faktury: `zone_priced_self_consumption(produced_h, exported_h, price_at)` → `(kwh, pln)` → `self_consumed_savings_pln = pln`, `sc_pricing='hourly_zone'`.
- Miesiąc Z fakturą (w `reconciled_months`): kWh z rekordu (`self_consumed_kwh`, już z faktury) zachowane. Godzinowy kształt `sc_h = max(0, prod_h − exp_h)`; `factor = self_consumed_kwh / Σ sc_h`; `pln = Σ sc_h · factor · price_h`; `sc_pricing='hourly_zone'`. `Σ sc_h == 0`, brak `self_consumed_kwh` albo brak ceny godziny → bez zmian + `'monthly_avg'`.
- Miesiąc bez godzinowych LTS (puste produkcja lub eksport) albo bez danych energii (`not has_energy_data(produced_kwh)`): wartość bez zmian; `sc_pricing='monthly_avg'` tylko gdy rekord ma dane energii (placeholdery nietknięte).
- `simulate` zwraca `{'months': [{'ym','method','old_pln','new_pln','delta_pln','kwh','frozen'}], 'unchanged': [...], 'roi_before','roi_after','total_delta_pln'}`; `roi_*` przez `roi.calculate(records, **roi_kwargs)` + `rebase._roi_headline`.
- `apply(path, ...)`: kopia `path.with_name(f"{path.name}.bak-<UTC YYYYMMDDTHHMMSSZ>")` PRZED zapisem, zapis przez `historic_store._save_document`; wynik jak simulate + `'backup_path'`. Idempotentny (drugi apply = te same wartości).
- Dodatkowo (zamknięcie znalezisk E1): `rebase._MERGE_FIELDS` + `'sc_pricing'`; `main` przekazuje do `rebase.simulate/apply` `fetch_month` z `price_at=_sc_price_at()`; `historic_store.reconcile_invoice` — gdy rekord ma `sc_pricing=='hourly_zone'`, to `self_consumed_savings_pln` skaluje się o `new_sc/old_sc` (zamiast nadpisania `sc × buy`), gdy `old_sc>0`.

## Wykonawca

### Paczka W1 — jedna paczka (sonnet-worker, „ryzyko”: pieniądze/migracja danych)
Pliki nowe: `PKG/sc_reprice.py`, `tests/test_sc_reprice.py`, `tests/test_web_sc_reprice.py`.
Pliki edytowane: `PKG/rebase.py`, `PKG/historic_store.py`, `PKG/web.py`, `PKG/main.py`, `tests/test_historic_store.py`, `tests/test_rebase.py`.

Zmiany (TDD: najpierw testy):
1. **`PKG/sc_reprice.py`**: `_hourly_sc(produced_h, exported_h) -> dict` (sc_h ≥ 0 per godzina), `_reprice_record(rec_dict, frozen, fetch_hourly, price_at) -> (new_pln|None, tag, method, kwh)`, `simulate(records, reconciled_months=None, fetch_hourly=None, price_at=None, roi_kwargs=None) -> dict`, `apply(path=historic_store.DEFAULT_PATH, reconciled_months=None, fetch_hourly=None, price_at=None, roi_kwargs=None) -> dict`. Domyślny `fetch_hourly` woła `live_reader` (import w funkcji). `price_at is None` → `ValueError`. Logika wg „Decyzje projektowe”. `method` ∈ `'hourly_zone'`, `'hourly_zone_invoice'` (faktura), `'monthly_avg'`.
2. **`PKG/rebase.py`**: do `_MERGE_FIELDS` dodaj `'sc_pricing'` (nic więcej).
3. **`PKG/historic_store.py` `reconcile_invoice._apply`**: przed zmianą `self_consumed_kwh` zapamiętaj `old_sc`, `old_pln`; po wyliczeniu `sc`: jeśli `m.get('sc_pricing') == 'hourly_zone'` i `old_sc` i `old_pln` są liczbami, `old_sc > 0` → `m['self_consumed_savings_pln'] = round(old_pln * sc / old_sc, 2)`; w przeciwnym razie dotychczasowe `sc * buy`.
4. **`PKG/main.py`** (obok `_simulate_rebase`/`_apply_rebase`, ~:494–509): (a) `_simulate_rebase`/`_apply_rebase` przekazują `fetch_month=lambda y, m: live_reader.read_month_from_statistics(y, m, price_at=_sc_price_at())` (jeśli `_sc_price_at` zdefiniowane niżej niż te funkcje — wołaj leniwie wewnątrz funkcji, jest closure); (b) `_simulate_sc_reprice()` i `_apply_sc_reprice()` (ten drugi po `apply` woła `poll_and_publish()`), obie z `reconciled_months=_reconciled_months()`, `price_at=_sc_price_at()`, `roi_kwargs=_roi_kwargs()`; rejestracja `_web.set_simulate_sc_reprice_callback(...)`, `_web.set_apply_sc_reprice_callback(...)`.
5. **`PKG/web.py`**: zmienne `_simulate_sc_reprice_callback`, `_apply_sc_reprice_callback`, settery (wzorem `set_simulate_rebase_callback`, :35–70) i trasy `POST /api/historic/simulate-sc-reprice`, `POST /api/historic/apply-sc-reprice` (wzorem :1816–1860: 503 gdy brak callbacku, `{'ok': True, **report}`, 500 + `log.exception` przy błędzie).
6. **Testy**: `tests/test_sc_reprice.py` — faktura zachowuje `self_consumed_kwh` i skaluje kształt (Σ kWh po skalowaniu = kWh z faktury), brak LTS → bez zmian + `'monthly_avg'`, placeholder (brak produkcji) nietknięty i bez flagi, brak ceny godziny → bez zmian + `'monthly_avg'`, `simulate` nie zapisuje pliku, `apply` tworzy kopię `*.bak-*` z oryginalną treścią, zmienia tylko `self_consumed_savings_pln`/`sc_pricing` (reszta pól identyczna), `apply` dwukrotnie → ten sam wynik (idempotencja), `price_at=None` → `ValueError`. `tests/test_historic_store.py` — `reconcile_invoice` na rekordzie `hourly_zone` skaluje PLN; na `None`/`monthly_avg` zachowuje starą formułę. `tests/test_rebase.py` — `sc_pricing` z `fetch_month` trafia do wyniku `apply`. `tests/test_web_sc_reprice.py` — 503 bez callbacku, 200 z callbackiem (wzorem `tests/test_web_rebase.py`), 500 przy wyjątku.

`DONE:` komenda testów zielona (≥1560 passed + nowe, 0 failed); `grep -c "sc-reprice" pv_roi_tracker/pv_roi_tracker/web.py` ≥ 2; `git status --short` w worktree wyłącznie pliki z listy.
`NIE RÓB:` zmian w `live_reader.py`, `sc_pricing.py`, `roi.py`, `invoice_store.py`, `publisher.py`, `models.py`; zmiany pól innych niż `self_consumed_savings_pln`/`sc_pricing` w `sc_reprice.apply`; uruchamiania apply na `/data` ani jakichkolwiek wywołań HA; commitu; `rm -rf`, `--no-verify`; dotykania `/config/addons/pv_roi_tracker`.

## Rollback
`git revert` commitu E2; na produkcji (E4) przywrócenie `historic.json.bak-<data>`.
