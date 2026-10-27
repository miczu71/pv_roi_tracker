# PLAN E4 — strefy według taryfy okresu (bateria, pompa ciepła, dystrybucja dyn)

Źródło prawdy: `docs/ROADMAP_EBOK_MOJ_TAURON.md` (decyzje 1–5). Wydanie łączone po E4 (0.51.0 = E2+E3+E4);
E2/E3 są w repo, niewydane.

## Cel
Po zmianie taryfy (np. G12w → G13) godzinowe symulacje add-onu nie mogą dalej liczyć stref wg stałego
kalendarza G12w. Strefa godziny wynika z taryfy obowiązującej w tym dniu (`tariff_history.tariff_on`),
a dystrybucja w dyn_a/dyn_b — z taryfy bazowej (bieżącej), tak jak reszta porównań po E3.

## Rozpoznanie (live, 27.10)
- `tariff_catalog.zone_for(tariff, dt, catalog)` już obsługuje G11/G12/G12w/G13/G13s (kalendarz + sezony), więc **nie
  piszemy nowego kalendarza** — tylko resolver „dzień → taryfa → czy szczyt”.
- Stałe G12w: `battery_sim._is_peak_hour/is_peak` (używane przez `simulate_s1` bez `price_fn` i `heatpump.py:179,247-248`);
  `g12w = rates_per_kwh('G12w', …)` dla dystrybucji/opłat dyn w `tariff_sim.py` (`simulate_month`, `hourly_series`)
  i `tariff_battery.make_price_fn`.
- `tariff_battery._PEAK_ZONES` już zna strefy szczytu G13 (`morning_peak`, `evening_peak`) i G13s.
- Ceny faktyczne to nadal 2 strefy (`tariff_config`: `peak_gross`/`offpeak_gross`).

## Decyzje projektowe
1. Nowy moduł `tariff_zones.py` (czysta logika): `peak_fn(periods, catalog, default='G12w') -> Callable[[datetime], bool]`.
   Dla godziny: `t = tariff_history.tariff_on(periods, data) or default`;
   - `G12w` → `battery_sim.is_peak(dt)` (dokładnie dzisiejsze zachowanie, w tym święta),
   - `G13`/`G13s`/`G12` → `zone_for(t, dt, catalog) in PEAK_ZONES` (wspólna stała; przenieś `_PEAK_ZONES` z `tariff_battery`
     do `tariff_zones` i importuj stamtąd; dla G12 dodaj strefę szczytu z katalogu — sprawdź nazwy w `catalog['zones']['G12']`),
   - `G11` → `True` (jedna strefa, jak dziś `offpeak_gross=None`).
   Brak okresów (`periods` puste) → zawsze zachowanie G12w. Błąd katalogu (ValueError) NIE jest połykany.
2. `battery_sim.simulate_s1(..., is_peak_fn=None)` i `simulate_all(..., is_peak_fn=None)`: `None` = `_is_peak_hour`
   (zgodność wstecz); w gałęzi bez `price_fn` `peak = is_peak_fn(datetime(...))`.
3. `heatpump.grid_price_for_hour(d, hour, rates, is_peak_fn=None)`, `run_hours(..., is_peak_fn=None)`, `compute(..., is_peak_fn=None)`;
   `None` = `is_peak` z `battery_sim`.
4. `tariff_sim`/`tariff_battery`: parametr `dist_tariff: str = 'G12w'` zamiast literału `'G12w'` w `rates_per_kwh`/`zone_for` dla
   dystrybucji i opłat dyn_a/dyn_b (`simulate_month`, `simulate_range`, `hourly_series`, `make_price_fn`, `simulate_variant_b`).
   `tariff_job.compute_payload` przekazuje `dist_tariff=baseline`. Jeśli baza nie jest w `STATIC_TARIFFS` ani w katalogu — `ValueError`.
   Domyślne `'G12w'` → dotychczasowe wyniki bez zmian (testy istniejące zielone bez edycji).
5. `main.py`: `peak_fn` budowany z `periods = _th.load(...)` i `_tcat.load_catalog()` w `battery_job` i `heatpump_job`
   i przekazywany do `simulate_all` / `heatpump.compute`. Brak okresów → `None` (stare zachowanie).
6. **Poza E4 (świadomie):** 3-strefowe ceny faktyczne (`tariff_config`, liczniki HA) — dopiero po pierwszej fakturze G13
   (parser nie zna jej układu, brak danych do kalibracji); do tego czasu G13 w kosztach faktycznych = 2 strefy
   (szczyty rano+wieczór = „peak”, reszta = „offpeak”) z cenami z `tariff_config` — wpis G13 w `tariff_config` robi user. HA i bateria
   (E5/E6) osobno.

## Pakiet W1 (jedna paczka, ryzyko „pieniądze/bateria” → `sonnet-worker`)
**Pliki:** `pv_roi_tracker/tariff_zones.py` (nowy), `battery_sim.py`, `heatpump.py`, `tariff_sim.py`, `tariff_battery.py`,
`tariff_job.py`, `main.py`, `tests/test_tariff_zones.py` (nowy), `tests/test_battery_sim.py`, `tests/test_heatpump.py`,
`tests/test_tariff_sim.py`, `tests/test_tariff_battery.py`, `tests/test_tariff_job.py`, `CHANGELOG.md` (wpis w sekcji „w przygotowaniu”).
Kroki:
1. `tariff_zones.py` wg decyzji 1 + testy: pusta historia = G12w; dzień przed/po zmianie na G13 (szczyt rano 06–?: wartości
   z katalogu, nie z pamięci — test czyta `tariff_catalog.load_catalog()`); święto/weekend G12w i G13 poza szczytem; G11 = True.
2. `battery_sim.py`: `is_peak_fn` w `simulate_s1` i `simulate_all` (decyzja 2) + test: ta sama seria, `is_peak_fn=lambda dt: False`
   daje inne `disch_*` niż domyślna; domyślna identyczna z dzisiejszą.
3. `heatpump.py`: decyzja 3 + test `grid_price_for_hour` z własnym `is_peak_fn`.
4. `tariff_sim.py`, `tariff_battery.py`, `tariff_job.py`: decyzja 4; test: `dist_tariff='G13'` zmienia dist/fees dyn_a i dyn_b
   względem G12w (wartości z katalogu), a `dist_tariff='G12w'` = wynik sprzed zmiany; `compute_payload(baseline='G13')` używa G13.
5. `main.py`: decyzja 5 (`battery_job`, `heatpump_job`); jeśli `tariff_history.load` zwraca `[]` → `is_peak_fn=None`.
6. `CHANGELOG.md`: jedna linia o strefach wg okresu taryfy.
- Test: `cd <WORKTREE>/pv_roi_tracker && PATH=$HOME/.local/bin:$PATH uv run --no-project --with pytest --with-requirements requirements.txt python -m pytest -q`
  (baza: 1037 passed, 23 skipped).
- `DONE:` testy zielone; `grep -n "_is_peak_hour\|is_peak(" pv_roi_tracker/*.py` — użycia poza `battery_sim`/`tariff_zones`
  przyjmują `is_peak_fn`; `grep -n "'G12w'" pv_roi_tracker/tariff_sim.py pv_roi_tracker/tariff_battery.py` zostają tylko
  wartości domyślne parametru `dist_tariff`.
- `NIE RÓB:` `static/*`, `publisher.py`, `web.py`, `tariff_config.py`, `live_reader.py`, `tariff_grid_charge.py`,
  `ebok_client.py`, długów E1–E3, wersji i commitów. Żadnych prawdziwych danych w fixtures. Błędów katalogu nie łapać
  ogólnym `except`.

## Weryfikacja (ja)
pytest zielony; `git diff` przeglądam sam (pieniądze), `verifier` z claimcheck. Na żywo dopiero w wydaniu 0.51.0:
`api/battery_sim` i `api/heatpump` bez zmiany liczb przy historii bez okresów G13 (regresja = brak różnic dla G12w).
Rollback: `git revert` commitów E4 (parametry opcjonalne, brak zmian formatu danych).
