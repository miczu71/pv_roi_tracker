# PLAN E8 — utwardzenie przed G13 (`haiku-worker`, TDD, jedna paczka)

Specyfikacja źródłowa: `ROADMAP_EBOK_MOJ_TAURON.md`, wpis „E8”. Wydanie dopiero po E9 (0.53.0); E8 to commit na `main`.
Do tych samych plików sięgają wszystkie punkty (`main.py`, `tariff_history.py`), więc jedna paczka, bez W1..W3.

## Decyzje
- Długi E7 (`rescued`, URL w logu) **poza zakresem**.
- Pkt 1: `load()` zostaje (zwraca `[]`), dochodzi `load_status(path) -> (periods, corrupt)`; `load = load_status()[0]`.
  Uszkodzony plik → kopia `<plik>.corrupt` (nie nadpisuje istniejącej kopii) + `logger.error`. Brak pliku to NIE uszkodzenie.
  `ebok_payment_check` przy `corrupt` pomija `record_detection` i `save` (historia nietknięta) i dopisuje to do `detail` joba.
- Pkt 2: `tariff_history.with_override(periods, override, today)` — czysta. Override poprawny i różny od taryfy ostatniego
  okresu → `[*bazowe, {tariff: override, from: today.isoformat()}]`, gdzie bazowe = `periods` albo (pusta historia)
  `[{tariff: 'G12w', from: None}]`. Brak/niepoprawny override albo ten sam → `periods` bez zmian (pusta zostaje pusta).
- Pkt 4: jedno źródło prawdy `tariff_zones.is_peak_zone(tariff, zone)` (= `zone in PEAK_ZONES` lub G12 `day`);
  używają go `peak_fn` i `tariff_battery.make_price_fn`. Skutek: symulacje G12 w `make_price_fn` liczą strefę dzienną jako szczyt.
- Pkt 5: `cheap` = średnia z K najtańszych godzin okna, K = `ceil(cfg.usable_kwh / cfg.power_kw)` ograniczone do 1..`len(window)`
  (bateria nie naładuje się szybciej niż `power_kw`).

## Wykonawca
Katalog roboczy: `pv_roi_tracker/` w worktree (podkatalog addonu); testy w `tests/`.

1. `pv_roi_tracker/tariff_history.py`
   - `load_status(path) -> tuple[list, bool]` jak w Decyzjach; `load(path)` deleguje. Kopia: `shutil.copy2` do `Path(str(path) + '.corrupt')`
     tylko gdy ta nie istnieje; błąd kopiowania łapany i logowany (nigdy wyjątek z load).
   - `with_override(periods, override, today) -> list`.
2. `pv_roi_tracker/main.py`
   - `_periods_peak_fn()`: `periods = _th.with_override(_th.load(...), CURRENT_TARIFF_OVERRIDE, date.today())`; `None` gdy puste.
   - `ebok_payment_check.body`: `periods, corrupt = _th.load_status(TARIFF_PERIODS_PATH)`; `corrupt` → `logger.error`, pomiń
     `record_detection`/`save`, `detail` joba z dopiskiem „uszkodzony tariff_periods.json — historia taryf nietknięta”.
     Gdy `change` → po zapisie `_threading.Thread(target=tariff_sim_job, daemon=True, name='tariff-sim-change').start()`
     (`tariff_sim_job` jest zdefiniowany niżej w tym samym zakresie — wywołanie w runtime, nie w czasie definicji).
   - blok `jit_schedule` w `poll_and_publish`: `ok = pub.publish_jit_schedule(...)`; `_record_job('jit_schedule', bool(ok), '' if ok else 'MQTT rozłączony — harmonogram nie opublikowany')`.
3. `pv_roi_tracker/publisher.py`: `publish_jit_schedule(...) -> bool` — `False` gdy `not self._connected`, `True` po publikacji.
4. `pv_roi_tracker/tariff_zones.py`: `is_peak_zone(tariff, zone) -> bool`; `peak_fn` go używa.
   `pv_roi_tracker/tariff_battery.py:54`: `zone in PEAK_ZONES` → `is_peak_zone(tariff, zone)`; import zmieniony odpowiednio.
5. `pv_roi_tracker/jit_schedule.py`: `cheap` wg Decyzji (import `math`); reszta bez zmian.
6. Testy (najpierw czerwone, potem kod):
   - `tests/test_tariff_history.py`: uszkodzony JSON → `(.., True)` + powstaje `.corrupt` z oryginalną treścią, drugi odczyt nie nadpisuje
     kopii; brak pliku → `([], False)`; `with_override` (zmiana dopisuje okres z dzisiejszą datą; pusta historia + override;
     ten sam override; niepoprawny override; pusta bez override).
   - `tests/test_tariff_zones.py`: `is_peak_zone` (G12 `day` tak, G12 `night` nie, `peak`/`morning_peak` tak, `other` nie).
   - `tests/test_tariff_battery.py` (lub istniejący plik testów `make_price_fn`): G12 `day` → `is_peak` True, `night` → False.
   - `tests/test_jit_schedule.py`: okno weekendowe z różnymi cenami (np. G13s dzień wolny) → `cheap_pln_kwh` = średnia 2 najtańszych
     (przy domyślnym `BatteryConfig`), nie `min`; zdegradowane `power_kw` ≥ `usable_kwh` → K=1 = `min`.
   - `tests/test_publisher.py`: `publish_jit_schedule` zwraca `False` przy rozłączonym kliencie i nic nie publikuje, `True` po połączeniu.
   - `tests/test_main.py`: (a) `ebok_payment_check` z uszkodzonym `tariff_periods.json` nie zmienia pliku i zostawia `.corrupt`;
     (b) wykrycie zmiany taryfy uruchamia `tariff_sim_job` (podstaw wątek/monkeypatch na `Thread`); (c) `_periods_peak_fn` z
     override G13 zwraca funkcję traktującą szczyt G13 jak szczyt (dzień roboczy, godzina szczytu porannego z katalogu).
     Jeśli któryś test wiringu `main.py` nie da się napisać bez przebudowy `main.py` — pomiń go i napisz w raporcie dlaczego.

Komenda testu (z katalogu worktree, podkatalog `pv_roi_tracker/`):
`PATH=$HOME/.local/bin:$PATH uv run --no-project --with pytest --with-requirements requirements.txt python -m pytest -q`

`DONE:` pełny zestaw zielony (baza 1121 passed, 23 skipped + nowe, 0 failed); `git diff --stat` tylko z plików powyżej + testów;
raport podaje liczby testów, listę plików i które testy wiringu pominięto.
`NIE RÓB:` nie zmieniaj `config.yaml`, `__version__`, `ebok_client.py`, `payment_reminders.py`, `deposit.py`, `invoice_*`, `static/`, `tariff_catalog.py`;
nie commituj; nie sprzątaj długów spoza listy; żadnych prawdziwych danych w fixtures (repo PUBLIC).
