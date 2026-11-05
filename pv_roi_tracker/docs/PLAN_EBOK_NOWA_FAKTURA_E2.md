# PLAN E2 — dług G13 w live_reader (commit, bez wydania)

Kod: `pv_roi_tracker/live_reader.py`. Testy: `tests/test_live_reader_rebase.py`, `tests/test_timezone_fix.py`.

## Decyzje (po przeczytaniu kodu)
- (a) `_sum_role_month(vals, entities, warn_missing=False)`: gdy `warn_missing` i odczyt częściowy (część encji roli ma wartość, część nie), loguj `logger.warning` raz na encję na proces (zbiór `_warned_missing_entities`). Suma bez zmian. `warn_missing=True` tylko w `read_current_month` (stare miesiące nie mają `g13_energia_*`, ostrzeżenie byłoby fałszywe).
- (b) Fallback REST `sensor.monthly_energy_peak/offpeak` w `read_current_month` jest zamrożony od 05.11 (źródło `daily_energy` zamrożone), a `g13_energia_*` resetują się dziennie, więc żadna suma żywych stanów nie da sumy miesiąca. Zmiana: gdy LTS dla peak/offpeak jest `None`, wynik = `None` + `logger.warning` (zamiast zamrożonej wartości); usunąć odczyty `peak_live`/`offpeak_live`. `_build_record` obsługuje `None`.
- `get_energy_window` nie ma fallbacku REST — bez zmian kodu, tylko testy.

## Wykonawca
Pliki: `pv_roi_tracker/live_reader.py`, `tests/test_live_reader_rebase.py`, `tests/test_timezone_fix.py`.
Zmiany:
1. `live_reader.py`: dodaj `_warned_missing_entities: set = set()` przy innych globalnych; zmodyfikuj `_sum_role_month` jak w (a) (wywołania w `read_current_month` dla solar/export/peak/offpeak/import/battery dostają `warn_missing=True`); w `read_current_month` usuń `peak_live`, `offpeak_live`, a `if peak is None: peak = peak_live` zamień na `logger.warning('read_current_month: brak LTS peak/offpeak — fallback REST pominięty (liczniki monthly_energy_* zamrożone od G13)')` i zostaw `None` (jedno ostrzeżenie na wywołanie, gdy którakolwiek z dwóch ról `None`). Zaktualizuj docstring zdania o fallbacku REST.
2. `test_live_reader_rebase.py`: w `test_read_current_month_falls_back_when_dashboard_correction_unavailable` asercje peak/offpeak → `purchased_kwh_peak is None`, `purchased_kwh_offpeak is None`, usuń stany `monthly_energy_*`; zaktualizuj docstring. W `test_read_current_month_corrects_...` usuń stany `monthly_energy_*`.
3. `test_timezone_fix.py::test_read_current_month_forwards_tariff_rates_to_build_record`: zamiast fallbacku REST podaj `_fetch_lifetime_month_stats` zwracające `{'sensor.daily_energy_peak': 120.0, 'sensor.daily_energy_offpeak': 100.0}`; asercja bez zmian.
4. Nowe testy (mock `_fetch_lifetime_month_stats` / `_get_state`, `_ws_statistics` dla okna):
   - `read_current_month` z LTS: `daily_energy_peak=10, g13_energia_morning_peak=2, g13_energia_evening_peak=3, daily_energy_offpeak=5, g13_energia_other=4` → peak 15, offpeak 9 (pola `purchased_kwh_peak/offpeak` przy `imported=None` w źródłach; użyj `_fixed_sources()` z pustym grid_import jeśli się da, inaczej imported = 24 i skalowanie zachowuje proporcję — asercja na proporcję).
   - `_sum_role_month(..., warn_missing=True)`: brak jednej encji → `caplog` ma 1 ostrzeżenie z nazwą encji; drugie wywołanie nie dubluje; `warn_missing=False` → brak ostrzeżenia (czyść `lr._warned_missing_entities` w teście).
   - `read_current_month` z `{}` z LTS → peak/offpeak `None` i ostrzeżenie w `caplog`.
   - `get_energy_window` (patch `_ws_statistics` zwracające `{eid: [{'start':..., 'change': x}, ...]}`, patch `get_energy_dashboard_sources`): peak = suma `daily_energy_peak`+`g13_energia_morning_peak`+`g13_energia_evening_peak`, offpeak = `daily_energy_offpeak`+`g13_energia_other`; wariant z brakiem `g13_energia_other` → offpeak = sama stara encja; ujemny `change` pominięty.
Komenda testu: `cd ~/dev/pv_roi_tracker/pv_roi_tracker && PATH=$HOME/.local/bin:$PATH uv run --no-project --with pytest --with-requirements requirements.txt python -m pytest -q`
DONE: testy zielone (≥1415 + nowe), nowe testy czerwone po cofnięciu zmian (a)/(b) w `live_reader.py` (sprawdź `git stash` tylko pliku live_reader.py i wróć).
NIE RÓB: żadnych innych plików; bez zmian `get_energy_window`, `_build_record`, `ebok_client.py`; bez formatowania sąsiedniego kodu; bez commitu.

## Rollback
`wt drop ~/dev/pv_roi_tracker e2` (przed landem) albo `git revert` commita E2.
