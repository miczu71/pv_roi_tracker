# PLAN G13s E1 — katalog i silnik G13s (wariant A i B)

Etap 1 z `ROADMAP_G13S_GRIDCHARGE.md` (źródło prawdy: dane i stawki tam). Kod w `addons/pv_roi_tracker/pv_roi_tracker/pv_roi_tracker/` (dalej: `PKG/`), testy w `addons/pv_roi_tracker/pv_roi_tracker/tests/` (repo `/config`, więc w worktree wszystkie ścieżki mają prefiks `addons/pv_roi_tracker/pv_roi_tracker/`).

## Decyzje projektowe E1
- **Klucz strefy G13s** = `{season}_{daytype}_{band}`: `season` ∈ `summer|winter`, `daytype` ∈ `workday|dayoff`, `band` ∈ `offpeak|peak|night` (12 kluczy). Sezon z miesiąca (lato IV–IX), dzień wolny = `_is_day_off` (sobota, niedziela, święto). Godziny pasm zależą tylko od sezonu; daytype decyduje o stawce.
- **Dostępność:** `catalog['available_from'] = {"G13s": "2026-01-01"}`. Przed tą datą G13s ma `available:false` (miesiąc: `reason`), w serii godzinowej ceny `None`, w `make_price_fn` zwraca `None`. Nigdy `ValueError` z braku stawki dla 2025.
- **Niepotwierdzone:** pole okresu `unconfirmed_tariffs: ["G13s"]` (w okresie `2026-02-01`, dziedziczone przez `2026-01-01`); `simulate_month` ustawia `confirmed:false` dla wymienionych taryf, reszta bez zmian.
- **Opłata handlowa per taryfa:** `fixed_net.sales_fee_by_tariff.G13s = 19.10` w okresie `2026-02-01`. `fixed_monthly(period, settings, tariff=None)`: dla `G13s` pole `sales_fee` = `settings.sales_fee_g13s_net`, gdy nie `None`, inaczej `sales_fee_by_tariff['G13s']` z okresu (brak → `ValueError`); globalna `sales_fee_net` dla G13s **nie** sumuje się (zastępuje ją). Pozostałe taryfy bez zmian.
- Stałe dla G13s jak G12w (sieciowa, abonament, mocowa); `dynamic_*` bez zmian.

## Dane do `tauron.json` (okres `2026-02-01`)
`energy_net.G13s` (netto = brutto/1,23 − 0,005, 4 miejsca; policzone i sprawdzone 2026-10-26):
| | offpeak | peak | night |
|---|---|---|---|
| summer_workday | 0.2700 | 0.7042 | 0.5000 |
| summer_dayoff | 0.1080 | 0.2817 | 0.5000 |
| winter_workday | 0.5500 | 0.7042 | 0.4900 |
| winter_dayoff | 0.3300 | 0.4225 | 0.4900 |

`dist_var_net.G13s` (netto z taryfy dystrybucji, ROADMAP tab. „Dystrybucja zmienna”):
| | offpeak | peak | night |
|---|---|---|---|
| summer_workday | 0.1000 | 0.2842 | 0.1094 |
| summer_dayoff | 0.0400 | 0.1176 | 0.1094 |
| winter_workday | 0.1999 | 0.3332 | 0.1094 |
| winter_dayoff | 0.1200 | 0.1960 | 0.1094 |

`zones.G13s`: `summer` {months 4–9, offpeak [[9,17]], peak [[7,9],[17,21]], night [[21,7]]}, `winter` {months 10–3, offpeak [[10,15]], peak [[7,10],[15,21]], night [[21,7]]}, `source`: „Tauron Dystrybucja taryfa 2026 pkt 2.2.2a”.
`_meta`: dopisać źródło `H` (PDF oferty „Tanie godziny”, URL z ROADMAP) i w `energy_net_note` okresu 2026-02-01: „G13s: brutto z oferty „Tanie godziny” (PDF 12/2025 i 08/2026, identyczne ceny) / 1,23 − 0,005 akcyzy; wniosek, nie zapis dokumentu; dostępność dla Wrocławia niepotwierdzona”.

## Wykonawca

### Paczka W1 — backend (haiku-worker; ryzyko średnie: stawki, ale tabele podane dosłownie)
Pliki: `PKG/tariffs/tauron.json`, `PKG/tariff_catalog.py`, `PKG/tariff_sim.py`, `PKG/tariff_battery.py`, `PKG/tariff_settings_store.py`, `PKG/tariff_job.py`, `PKG/publisher.py`, testy: `tests/test_tariff_catalog.py`, `tests/test_tariff_sim.py`, `tests/test_tariff_hourly.py`, `tests/test_tariff_battery.py`, `tests/test_tariff_settings_store.py`, `tests/test_tariff_job.py`, `tests/test_publisher.py`.
Zmiany:
1. `tauron.json`: wg sekcji „Dane do tauron.json” (zones.G13s, energy_net.G13s, dist_var_net.G13s, fixed_net.sales_fee_by_tariff, `unconfirmed_tariffs`, top-level `available_from`, źródło H). Wartości dosłownie z tabel; nie przeliczać.
2. `tariff_catalog.py`: `TARIFFS` o `'G13s'`; `TariffSettings.sales_fee_g13s_net: Optional[float] = None`; `zone_for` dla G13s zwraca klucz `{season}_{daytype}_{band}` (pasma z `zones.G13s[season]`, `_in_hours`; sezon po `months`; brak pasma → `ValueError`); `fixed_monthly(period, settings, tariff=None)` wg „Opłata handlowa per taryfa”.
3. `tariff_sim.py`: `STATIC_TARIFFS` o `'G13s'`; `simulate_month`: G13s przed `available_from` → `{'available': False, 'reason': 'oferta „Tanie godziny” od 2026-01'}` i pomijana w pętli stawek; opłaty stałe liczone per taryfa (`fixed_monthly(first, settings, n)`, `res['fixed']` = wartość taryfy); `confirmed` False dla taryf z `period['unconfirmed_tariffs']`. `hourly_series`: godziny G13s przed `available_from` → `zone=None`, ceny `None` (cała seria przed → `available:False`, jak dyn); `cost_by_zone` liczy opłaty stałe per taryfa (`fixed_monthly(..., n)`).
4. `tariff_battery.py`: `_PEAK_ZONES` o `summer_workday_peak, summer_dayoff_peak, winter_workday_peak, winter_dayoff_peak`; `make_price_fn` zwraca `None` dla G13s przed `available_from`.
5. `tariff_settings_store.py`: `DEFAULTS['sales_fee_g13s_net'] = None`; `validate` przyjmuje `None`/`''` → `None`, inaczej float 0–200 (komunikat po polsku jak dla `sales_fee_net`); `to_catalog_settings` przenosi pole. `tariff_job.py`: `payload['settings']['sales_fee_g13s_net']`.
6. `publisher.py`: sensory `tariffsim_month_g13s` i `tariffsim_12m_g13s` (nazwy „PV Tariff Sim Month G13s” / „12M G13s”, ikony jak G13); slug małymi liter działa już w `_tariff_sim_value`.
7. Testy (TDD, najpierw czerwone): (a) `zone_for` G13s: granice lato 8:59/9:00, 16:59/17:00, 20:59/21:00, 6:59/7:00; zima 9:59/10:00, 14:59/15:00; przełom 31.III 23:00 (zima) / 1.IV 00:00 (lato) i 31.III 12:00 vs 1.IV 12:00; sobota, niedziela, święto (np. 2026-11-11) → `dayoff`; DST (2026-03-29, 2026-10-25) bez wyjątków. (b) `rates_per_kwh` G13s = 12 kluczy × (energia, dystrybucja) zgodnie z tabelami (parametryzacja z tabel w tym pliku), `fees` = 0.0331+0.0073+0.0030+0.005 dla stycznia (jakościowa 0,0331) i 0.0332+… od lutego. (c) `fixed_monthly`: G13s = 19.10 (domyślnie), nadpisanie `sales_fee_g13s_net=10` → 10, globalne `sales_fee_net=5` nie dodaje się do G13s, G12w nadal dostaje 5. (d) `simulate_month`: 2025-12 → G13s `available:False`; 2026-03 G13s policzone (prosta próbka: 1 kWh w wybranych godzinach — roboczy wtorek 2026-03-10 godz. 3, 8, 12, 16 i sobota 2026-03-14 godz. 12 — z oczekiwanym kosztem liczonym ręcznie w teście z tabel), `confirmed` False dla G13s i True dla G12w w tym samym miesiącu. (e) `hourly_series` G13s: zakres obejmujący 2025-12-31 i 2026-01-01 → przed datą ceny `None`. (f) `make_price_fn('G13s')` przed i po `available_from`; `is_peak` True dla `winter_workday_peak`. (g) store: domyślnie `None`, wartość 19.1 przechodzi, 500 → `ValueError`, `None` zostaje `None`. (h) publisher: liczba sensorów `tariffsim_*` = 16, render `tariffsim_12m_g13s`. (i) `compute_payload`: `tariffs` zawiera `G13s`, G13s w `months['2025-12']` ma `available:False` i nie psuje `last12`/`cheapest`.
8. Istniejące testy zależne od liczby taryf/sensorów (`test_publisher`: 14 → 16; `test_web_tariff_hourly` używa `ALL_TARIFFS`) dostosować; zgodność G12w vs faktury (`test_tariff_job` / reconciliation) musi przejść bez zmian oczekiwanych wartości.
Komenda testu: `cd <WORKTREE>/addons/pv_roi_tracker/pv_roi_tracker && uv run --no-project --with pytest --with-requirements requirements.txt pytest -q tests/test_tariff_*.py tests/test_publisher.py tests/test_web_tariff_*.py`, na koniec pełny `pytest -q`.
`DONE:` punkty 1–8 wykonane; testy wyżej zielone; pełny zestaw bez nowych porażek (baza ok. 819 passed/27 skipped); raport z listą zmienionych plików i wynikiem testów.
`NIE RÓB:` nie zmieniać stawek G11/G12/G12w/G13 ani okresów 2025; nie dodawać nowych okresów (G13s w istniejącym `2026-02-01`); nie ruszać `static/*`, `web.py`, `main.py`, `battery_sim.py`; nie dodawać „Tanich weekendów”; nie commitować; nie zmieniać wersji; nie edytować plików poza listą.

### Paczka W2 — UI listy taryf (haiku-worker; ryzyko niskie)
Pliki: `PKG/static/app.js`, `tests/test_web_static.py`.
Zmiany:
1. `_TSIM_ALL`: wstaw `'G13s'` po `'G13'`; `_TSIM_COLORS.G13s = '#0891b2'` (kolor spoza już użytych).
2. Zakładka Taryfa: lista `['G11','G12','G12w','G13']` w tabeli cennika (ok. l. 3608) o `'G13s'`; etykiety stref G13s w `ZN`/nowej funkcji `_tfG13sLabel(zone)`: „lato, dzień roboczy — poza szczytem/szczyt/noc” itd. (z klucza `{season}_{daytype}_{band}`); `_tfZoneDesc` dla `G13s`: godziny pasma z `z.summer`/`z.winter` (np. „poza szczytem: lato 09–17, zima 10–15; szczyt: …; noc 21–07”), obsłużone w jednym wierszu opisu; wiersze 12 stref nie powielają opisu godzin (opis tylko w pierwszym wierszu `rowspan` albo skrócony do stawki dnia).
3. Mapa stref (`_tsimRenderZoneMap`, legenda ok. l. 4279–4296): dla G13s kolor znacznika wg pasma (`offpeak|peak|night`), a nie wg 12 kluczy; podpis w legendzie przez `_tfG13sLabel`.
4. Opis „G11/G12/G12w/G13” w tekście pomocy (ok. l. 3182) o G13s; napis przy G13s w pasku taryf: „G13s (oferta „Tanie godziny”, cennik niepotwierdzony)” jako `title` etykiety checkboxa.
5. `tests/test_web_static.py`: asercja, że `app.js` zawiera `G13s` w `_TSIM_ALL` i `_TSIM_COLORS` (styl istniejących asercji w pliku).
Komenda testu: `cd <WORKTREE>/addons/pv_roi_tracker/pv_roi_tracker && uv run --no-project --with pytest --with-requirements requirements.txt pytest -q tests/test_web_static.py`; dodatkowo `node --check PKG/static/app.js`.
`DONE:` punkty 1–5; `node --check` bez błędów; test zielony; raport ze zmienionymi plikami. Weryfikację w przeglądarce robię sam po landzie obu paczek.
`NIE RÓB:` nie ruszać backendu ani `index.html`/`app.css`; nie zmieniać układu wykresów ani logiki wariantów A/B; nie dodawać zależności; nie commitować; nie edytować plików poza listą.

## Weryfikacja etapu (ja, po `wt land` obu paczek)
1. Pełny `pytest -q` w `pv_roi_tracker/`; `claimcheck` + `verifier` per paczka (przed landem).
2. G13s dla marca 2026 policzone ręcznie na próbce godzin (skrypt jednorazowy w scratchpadzie vs `simulate_month`).
3. Zgodność G12w vs faktury bez pogorszenia (±3%: `reconciliation_summary` przed i po).
4. Podgląd Symulacji i Taryfy z G13s (Playwright, konsola bez błędów) — na lokalnym serwerze testowym lub po wydaniu (ingress); jeśli niedostępne lokalnie, odnotować i sprawdzić po 0.48.0.

## Rollback
`git revert` commitu E1 (nic poza katalogiem, listą taryf i ustawieniami nie zależy); `wt drop` przed landem.
