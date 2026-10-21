# PLAN E3 — wariant B: magazyn (S1) przeliczony pod strefy i ceny każdej taryfy

Roadmapa: `docs/ROADMAP_TARIFFS.md`. E2 daje wariant A (ten sam pobór wyceniony w każdej taryfie). E3 dodaje wariant B: ten sam wirtualny moduł (`BatteryConfig` z zakładki Magazyn) ładowany eksportem PV i rozładowywany przeciw importowi, ale uniknięty zakup wyceniany ceną **danej taryfy** w danej godzinie. Rollback: revert commitu (wariant B znika z payloadu, A bez zmian).

## Decyzje
1. **Definicja B:** `gross_b = gross_a − savings_pln`, gdzie `savings_pln = avoided_buy − foregone_rcem` z `simulate_s1` (jak w zakładce Magazyn). Koszt modułu (`module_price_pln`) NIE wchodzi do kosztu miesiąca — to osobna analiza zwrotu w zakładce Magazyn. Oznaczone w payloadzie `variant_b_note`.
2. **Arbitraż wyłączony w B** (`arbitrage_enabled=False` wymuszone kopią cfg): porównujemy taryfy tym samym, prostym scenariuszem. Dla G11 arbitraż nie ma sensu (brak stref).
3. **Cena godziny (brutto, zł/kWh)** = `(energia + dystrybucja + opłaty zmienne netto) × vat`, z tych samych funkcji co wariant A (`period_for`, `zone_for`, `rates_per_kwh`):
   - G11/G12/G12w/G13: stawki strefy godziny.
   - `dyn_a`: `(rdn/1000 + margin) + g12w.dist + g12w.fees` netto, `rdn` z fallbackiem „ta sama godzina tydzień wcześniej” jak w `tariff_sim`; brak ceny → godzina pomijana (moduł jej nie obsługuje, zero oszczędności). Podłoga miesięczna (`min_avg_price`) pominięta w B — przybliżenie.
   - `dyn_b`: `fixed_price + dynamic_dist_var_net[strefa Kompas] + g12w.fees`; brak strefy → godzina pomijana.
   - Taryfa `available:false` lub `reliable:false` w miesiącu A → wpis B `None` (jak A).
4. **Flaga „szczyt”** (do statystyk `disch_peak_kwh`): strefa ∈ {`peak`,`morning_peak`,`evening_peak`}; G11, dyn_a, dyn_b → `False`.
5. **Sprzedaż (foregone):** `rcem_by_ym` jak w zakładce Magazyn (`rcem_scraper._load_history`), taka sama dla każdej taryfy.
6. **Zakres:** od `from_ym` payloadu (≥ 2025-01) do bieżącego miesiąca; stan baterii ciągły przez całe okno, start pusty. Pobór ze zbioru `hours` cache battery_sim (`{klucz: (export, import)}`).
7. **Payload:** `months[ym][t]['b'] = {'savings_pln','gross','charged_kwh','discharged_kwh'}` (lub `None`); `last12` dostaje `gross_b`, `cheapest_b`, `saving_vs_g12w_b` liczone jak A (suma 12 pełnych miesięcy, `None` gdy brak któregokolwiek); `current_month.gross_b`. Pola A bez zmian. Bez nowych sensorów MQTT (UI w E4).
8. **Zgodność wsteczna:** `simulate_s1` bez `price_fn` działa identycznie jak dziś (testy `test_battery_sim.py` bez zmian muszą przejść).

## Kontrakt

### `pv_roi_tracker/battery_sim.py`
`simulate_s1(hours, cfg, zone_prices, rcem_by_ym, keys=None, price_fn=None)`:
- `price_fn(key) -> Optional[tuple[float, bool]]` = `(cena_brutto_kwh, is_peak)`; `None` → godzina pominięta (nie liczy się ani do `hours`, ani do przepływów).
- Gdy `price_fn` podane, zastępuje `zone_prices` i `_is_peak_hour`; reszta logiki (ładowanie, rozładowanie, limit mocy, rcem) bez zmian.
- Gdy `price_fn=None` — kod ścieżki dotychczasowej bez zmiany zachowania.

### `pv_roi_tracker/tariff_battery.py` (NOWY, czysta logika)
- `make_price_fn(tariff, catalog, rdn, kompas, overrides=None) -> Callable[[str], Optional[tuple[float,bool]]]` — wg decyzji 3–4. Dla `dyn_b` stała cena przez `tariff_sim._dyn_b_price`.
- `simulate_variant_b(hours, cfg, catalog, rdn, kompas, rcem_by_ym, from_ym, to_ym, overrides=None) -> dict` → `{ym: {tariff: {'savings_pln','charged_kwh','discharged_kwh'}}}` dla wszystkich 6 taryf (`TARIFFS` z `tariff_job`) na kopii `cfg` z `arbitrage_enabled=False`; klucze `hours` przycięte do `[from_ym, to_ym]`. Miesiące bez godzin → brak wpisu. Wywołuje `battery_sim.simulate_s1(..., price_fn=...)` raz na taryfę.

### `pv_roi_tracker/tariff_job.py`
`compute_payload(..., today, from_ym='2024-12', hours=None, battery_cfg=None, rcem_by_ym=None)` — nowe argumenty opcjonalne na końcu. Gdy `hours` i `battery_cfg` podane: wywołaj `simulate_variant_b`, dołącz `months[ym][t]['b']` (tylko gdy A użyteczne wg `_usable_month` i miesiąc ma wynik B; inaczej `None`), policz `gross_b = gross − savings_pln`. `last12`: `gross_b`, `cheapest_b`, `saving_vs_g12w_b` (jak A, na `gross_b`); `current_month['gross_b']`. Bez `hours`/`battery_cfg` payload identyczny jak w E2 (brak pól B).

### `pv_roi_tracker/main.py`
`tariff_sim_job`: dołóż `hours=hours`, `battery_cfg=battery_store.load_config(BATTERY_CONFIG_PATH)`, `rcem_by_ym={ym: float(v) for ym, v in rcem_scraper._load_history(RCEM_HISTORY_PATH).items()}` do `compute_payload`. Nic więcej.

## Wykonawca
WORKTREE: z `wt new /home/claude/ha-config pv-roi-tariffs-e3` (repo `/config` = `~/ha-config`), katalog add-onu `addons/pv_roi_tracker/pv_roi_tracker`. Jedna paczka (pliki zależą od kontraktu).

Pliki i zmiany (TDD: testy najpierw, czerwone):
1. `pv_roi_tracker/battery_sim.py` — `price_fn` w `simulate_s1` wg kontraktu. Minimalna zmiana: wyznaczanie `(peak_px/offpeak_px, peak)` w pętli zastąp gałęzią `price_fn`; reszta bez zmian.
2. `tests/test_battery_sim.py` — dopisz: (a) `price_fn=None` daje wynik identyczny z dotychczasowym (porównaj z wywołaniem bez argumentu na tych samych danych), (b) stała `price_fn` zwracająca `(0.9, False)` daje `avoided_buy_pln == discharged_kwh*0.9`, (c) `price_fn` zwracająca `None` dla godziny pomija ją (`hours` licznik, brak przepływów).
3. `pv_roi_tracker/tariff_battery.py` — NOWY wg kontraktu.
4. `tests/test_tariff_battery.py` — NOWY: cena G12w szczyt vs dolina (godzina robocza 10:00 vs sobota) = stawki z katalogu × vat; G11 jednakowa cena; G13 flaga szczytu; `dyn_a` z cenami RDN i fallbackiem tydzień wcześniej oraz `None` bez ceny; `dyn_b` `None` gdy brak strefy Kompas; `simulate_variant_b`: arbitraż nie działa nawet przy `cfg.arbitrage_enabled=True`, dla G12w wynik `savings_pln` równy `simulate_s1` z `zone_prices` ze stawek G12w z katalogu na tych samych godzinach (oszczędność liczona z katalogu, nie z tariff_config), miesiące poza zakresem pominięte.
5. `pv_roi_tracker/tariff_job.py` — nowe argumenty i pola B wg kontraktu.
6. `tests/test_tariff_job.py` — dopisz: brak `hours` → brak kluczy `b`/`gross_b`; z `hours` → `gross_b = gross − savings`, `last12.gross_b` sumuje 12 pełnych miesięcy, taryfa niedostępna → `None`, `cheapest_b` i `saving_vs_g12w_b`. Użyj monkeypatcha jak istniejące testy (`_patch_range`) i `monkeypatch.setattr(tariff_battery, 'simulate_variant_b', ...)` z kontrolowanym wynikiem.
7. `pv_roi_tracker/main.py` — wywołanie `compute_payload` wg kontraktu.

Komenda testu (na pierwszym planie): `cd <WORKTREE>/addons/pv_roi_tracker/pv_roi_tracker && uv run --no-project --with pytest --with-requirements requirements.txt pytest -q`.

DONE: wszystkie testy zielone (774 passed / 27 skipped poprzednio + nowe, bez nowych skipów, `test_battery_sim.py` dotychczasowe testy nietknięte); `compute_payload` z `hours` zwraca `last12.gross_b` i `months[ym][t]['b']`; bez `hours` payload bez zmian; `git status --short` w worktree pokazuje tylko pliki z listy.

NIE RÓB: nie zmieniaj `simulate_s2`, `summarize`, `simulate_all`, `web.py`, `publisher.py`, `tariff_sim.py`, `tariffs/tauron.json`, plików wersji; nie dodawaj sensorów MQTT; nie commituj; nie ruszaj plików poza listą (w `/config` są zmiany spoza roadmapy).
