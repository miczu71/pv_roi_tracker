# PLAN E1: backend danych godzinowych (ROADMAP_UI_SPLIT.md, etap E1)

Kod: `pv_roi_tracker/pv_roi_tracker/`, testy: `pv_roi_tracker/tests/`. Tylko backend, UI nietknięty.
Testy: `cd /config/addons/pv_roi_tracker/pv_roi_tracker && <venv>/bin/python -m pytest -q` (stan wyjściowy: wszystkie zielone).

## Kontrakt

### `tariff_sim.py`
```python
def hourly_series(from_date: date, to_date: date, import_by_hour: dict, catalog: dict,
                  tariffs=ALL_TARIFFS, settings=None, rdn=None, kompas=None,
                  overrides=None) -> dict
```
Zwraca `{tariff: {'available': bool, 'reason': str|None, 'hours': [row, ...]}}`; `hours` obejmuje KAŻDĄ godzinę
doby od `from_date` do `to_date` włącznie (także bez poboru, kWh = 0), w kolejności czasu. DST: jak `simulate_month`
(klucz `YYYY-MM-DDTHH`, bez rozróżniania dwóch godzin jesienią; wiersz jest jeden na klucz).
`row = {'key', 'zone', 'net_per_kwh', 'gross_per_kwh', 'kwh', 'cost_net', 'cost_gross', 'fallback'}`.
- Statyczne (G11/G12/G12w/G13): `zone = zone_for`, `net_per_kwh = energy+dist+fees` z `rates_per_kwh(tariff, period_for(d, overrides), zone)`.
- dyn_a: strefa jak G12w (dystrybucja G12w), cena = `rdn/1000 + margin_net_per_kwh + dist + fees`. Brak RDN → cena z tej godziny tydzień wcześniej
  (`fallback: True`, jak w `simulate_month`); nadal brak → `net_per_kwh = gross_per_kwh = cost_* = None`, `zone` nadal podana.
  Bez miesięcznej podłogi średniej (to różnica względem `simulate_month`, udokumentować w docstringu).
  Dzień przed `dynamic_sales.valid_from` → taryfa `available: False`, `reason` jak w `simulate_month`, `hours: []`.
- dyn_b: `zone` = `kompas[key]` (S1–S4), cena = `_dyn_b_price + dynamic_dist_var_net[zone] + fees`; brak strefy → ceny `None`. Przed `valid_from` → `available: False`.
- `gross = net * catalog['_meta']['vat']`; `cost_net = kwh * net_per_kwh` (przy `net_per_kwh is None` → `None`, kWh liczy się do braków).
- Brak wartości w katalogu → `ValueError` (jak dziś), nigdy cichy zero.

Agregaty (czyste funkcje w tym samym module, wejście: wynik `hourly_series`):
- `zone_map(series) -> {tariff: {'days': [YYYY-MM-DD...], 'cells': {day: [24 × {'zone', 'net_per_kwh', 'gross_per_kwh'}]}}}` (godzina 0..23; dla dni DST ostatni zapis klucza wygrywa).
- `cost_by_zone(series, catalog, settings=None, overrides=None) -> {tariff: {'zones': {zone: {'kwh', 'cost_net', 'cost_gross'}}, 'fixed_net', 'fixed_gross', 'missing_kwh', 'total_gross'}}`.
  Opłaty stałe = Σ po dniach `sum(fixed_monthly(period_for(dzień), settings).values()) / liczba_dni_miesiąca` (proporcjonalnie). Godziny z ceną `None` → `missing_kwh`, poza `zones`.
- `cumulative_diff(series, a, b) -> {'days': [...], 'diff_gross': [...]}` = narastająca suma dziennych `cost_gross[a] − cost_gross[b]` (bez opłat stałych, bo identyczne; pominąć godziny, gdzie któraś cena `None`, i policzyć je w `skipped_kwh`).

### `price_sources.py`
`fetch_ahead(day: date, cache: PriceCache, http_get=None) -> dict` zwraca `{'rdn': {...}, 'kompas': {...}, 'eurpln_fallback': bool, 'eurpln_used': float|None, 'rdn_available': bool, 'reason': str|None}` dla dnia bieżącego/przyszłego, BEZ zapisu do cache na dysku (ceny dnia bieżącego i przyszłych nie są ostateczne). Kurs: `cache.eurpln[day]` jeśli jest, inaczej ostatni znany kurs z `cache.eurpln` (`eurpln_fallback: True`); brak jakiegokolwiek kursu → `rdn_available: False`, reason. Błąd sieci/pusta odpowiedź RDN → `rdn_available: False`, reason z treścią błędu (nigdy cichy fallback). Kompas pobierany osobno, jego brak nie blokuje RDN.

### `web.py` (wzór: `set_tariff_settings_path`)
- `set_tariff_inputs_callback(fn)`; `fn() -> dict` z kluczami `import_by_hour, catalog, settings (TariffSettings), rdn, kompas, overrides, price_cache` (wołane per request, nie trzymać stanu).
- `GET /api/tariff_hourly?from=YYYY-MM-DD&to=YYYY-MM-DD&tariffs=G11,G12,...`: walidacja (format, `from<=to`, maks. 400 dni, nieznana taryfa → 400 `{'ok': False, 'error': ...}`; brak callbacku → 503). `tariffs` domyślnie wszystkie.
  Odpowiedź: `{'ok', 'from', 'to', 'zone_map', 'cost_by_zone', 'cumulative_diff' (para pierwsza→druga z `tariffs`, tylko gdy ≥2 taryfy), 'hours' (surowe wiersze TYLKO gdy zakres ≤31 dni, inaczej `None`), 'tomorrow': {...} (tylko gdy `to` > dziś)}`.
  Dla dni > dziś: ceny z `fetch_ahead`; kWh = 0; `cost_by_zone` bez kosztów dla tych dni (tylko `zone_map`); dynamiczne bez RDN dostają `available: False` z `reason`.
  Cache w pamięci per `(from, to, tariffs, wersja danych)` – klucz zawiera `len(rdn)`, `len(kompas)`, `len(import_by_hour)`, `settings`; maks. 32 wpisy (najstarszy wypada); wpis z dniem > dziś NIE jest cache'owany.
- `GET /api/tariff_now`: `{'ok', 'now': 'YYYY-MM-DDTHH', 'tariff': <z tariff_settings/ostatni payload: 'G12w'>, 'zone', 'net_per_kwh', 'gross_per_kwh', 'next_change': {'at', 'zone'}|None}` dla bieżącej taryfy użytkownika (G12w, stała w tej wersji) liczone z `zone_for` + `rates_per_kwh`.
- `GET /api/tariff_catalog`: `{'ok', 'meta': catalog['_meta'], 'zones', 'periods': lista rozwiniętych okresów (`_expand`), bez sekretów (katalog ich nie zawiera)}` tylko do odczytu.

### `main.py`
`_web.set_tariff_inputs_callback(...)` obok `set_tariff_settings_callback`: ładuje `hours` (`battery_store.load_hours`) → `tariff_job.import_series`, `_tcat.load_catalog()`, `to_catalog_settings(load(TARIFF_SETTINGS_PATH))`, `PriceCache(TARIFF_PRICE_CACHE_PATH)` (`.rdn`, `.kompas`), `overrides=None`.

## Wykonawca (W1, jedna paczka, ryzyko → sonnet-worker)
Jedna paczka: `web.py` zależy od API z `tariff_sim.py`.

Pliki i zmiany:
1. `pv_roi_tracker/tariff_sim.py`: `hourly_series`, `zone_map`, `cost_by_zone`, `cumulative_diff` wg kontraktu. Reużyć `zone_for`, `rates_per_kwh`, `period_for`, `fixed_monthly`, `_dyn_b_price`, `_required`; nie kopiować logiki stref.
2. `pv_roi_tracker/price_sources.py`: `fetch_ahead`.
3. `pv_roi_tracker/web.py`: callback, trzy endpointy, cache.
4. `pv_roi_tracker/main.py`: podpięcie callbacku.
5. `tests/test_tariff_hourly.py` (nowy): granice stref (G12 6/13/15/22, G12w robocze vs weekend i święto 2026-11-11, G13 przełom 2026-03-31→04-01 i 09-30→10-01), brak ceny RDN → `None` + fallback tydzień wstecz, kompas brak, `available: False` dla dyn_a przed 2026-04, suma `cost_by_zone` (stałe statyczne G11/G12/G12w/G13, miesiąc 2026-03) = `simulate_month(...)['gross']` ±0,01, `cumulative_diff` (monotoniczne sumowanie, `skipped_kwh`), `zone_map` (24 komórki/dzień).
6. `tests/test_web_tariff_hourly.py` (nowy): walidacja (400/503), limit 400 dni, `hours` tylko ≤31 dni, cache (drugie wywołanie nie liczy ponownie – licznik wywołań atrapy), „jutro" bez RDN → `available: False` z reason, `/api/tariff_now`, `/api/tariff_catalog`; `fetch_ahead` w `tests/test_tariff_price_sources.py` (atrapa `http_get`: ok, brak kursu → fallback z flagą, błąd sieci).

Komenda testu: `cd /config/addons/pv_roi_tracker/pv_roi_tracker && /tmp/claude-950/-home-claude/a1b425f4-5b13-4d82-8dc1-b05e8ce0db11/scratchpad/venv/bin/python -m pytest -q`
(w worktree: ta sama komenda z `cd <WORKTREE>/pv_roi_tracker`).

DONE: wszystkie nowe testy zielone + pełny `pytest -q` zielony; `git diff --stat` dotyka tylko plików z listy powyżej; `simulate_month`/`simulate_range`/`compute_payload` bez zmian (regresja: istniejące testy przechodzą).

NIE RÓB: zmian w `static/*` (UI), w `tariff_job.py`, `tariff_catalog.py`, `tariffs/tauron.json`, `config.yaml`, w endpointach `/api/tariff_sim|tariff_stats`; bez commitów; bez nowych zależności; bez zmiany sygnatur istniejących funkcji; bez danych prywatnych w testach/fixtures (neutralne nazwy); nie czytać `~/.secrets`.

## Rollback
`git revert` commitu E1 (nic w UI z tego nie korzysta).
