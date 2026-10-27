# PLAN E3 — baza porównań = bieżąca taryfa (add-on + publikacja do HA)

Źródło prawdy: `docs/ROADMAP_EBOK_MOJ_TAURON.md` (sekcja „ZMIANA ZAKRESU”, decyzje 1–4).
Wydanie łączone dopiero po E3 (0.51.0), E2 jest w repo, niewydane.

## Cel
Po wykryciu zmiany taryfy (E2: `tariff_history`, override) wszystkie porównania w add-onie i w HA mają
bazę = bieżąca taryfa zamiast sztywnego G12w. Koszty faktyczne z faktur zostają w taryfie okresu faktury.
Strefy baterii/dyn (dystrybucja wg G12w) NIE wchodzą — to E4/E6.

## Decyzje projektowe
- `baseline` = `tariff_history.current(periods, CURRENT_TARIFF_OVERRIDE)`, brak → `'G12w'` (zachowanie dziś).
- Klucze payloadu: nowe `baseline_tariff`, `last12.saving_vs_baseline[_b|_c]`; stare `saving_vs_g12w[_b|_c]`
  zostają z TĄ SAMĄ wartością (przestarzałe, do migracji dashboardów). Koszty taryfy `*_g12w` (np. `tariffsim_month_g12w`)
  zostają kosztem G12w — to nie baza.
- `_reconcile` (symulacja vs faktura): taryfa symulacji = `tariff_on(periods, 1. dnia miesiąca)`, brak → `'G12w'`;
  miesiąc ze zmianą taryfy w środku (`tariff_on(1.) != tariff_on(ostatni dzień)`) pomijany.
- Edycja `tariff_periods` w UI: `GET/PUT /api/tariff_periods`; PUT waliduje (`tariff_history.validate_periods`) i
  przelicza symulację w wątku (jak `_tariff_settings_changed`).

## Pakiety (sekwencyjnie: W1 → ja przeglądam → W2)

### W1 — backend (ryzyko „pieniądze” → `sonnet-worker`)
**Pliki:** `pv_roi_tracker/tariff_job.py`, `pv_roi_tracker/tariff_history.py`, `pv_roi_tracker/main.py`,
`pv_roi_tracker/web.py`, `pv_roi_tracker/publisher.py`, `tests/test_tariff_job.py`, `tests/test_tariff_history.py`,
`tests/test_publisher.py`, `tests/test_web_tariff_sim.py` (+ nowy `tests/test_web_tariff_periods.py`), `CHANGELOG.md`.
1. `tariff_job.py`: `compute_payload(..., baseline=BASELINE_TARIFF, tariff_periods=None)`. Wszystkie `'G12w'`
   w `compute_payload`, `_best_year(…, baseline)`, `_reconcile(months, invoices, periods)`, `_recommend_one`,
   `_recommend_best_year` zastąp bazą (teksty „względem G12w A”, „G12w jest najtańsza” z nazwą bazy).
   `BASELINE_TARIFF` zostaje jako domyślna. Payload dostaje `baseline_tariff`. `recommend(payload, thr)` czyta
   `payload['baseline_tariff']` (domyślnie `'G12w'`), wynik `_recommend_one` ma `baseline_tariff`,
   `baseline_gross` oraz stare `g12w` (= to samo, przestarzałe). `last12`: `saving_vs_baseline[_b|_c]` + stare aliasy.
2. `tariff_history.py`: `validate_periods(raw) -> list` — niepusta lista; każdy wpis `{tariff, from}`; `tariff`
   przez `normalize` (zapis kanoniczny); `from`: tylko pierwszy wpis może mieć `null`, reszta ISO `YYYY-MM-DD`,
   ściśle rosnące; inaczej `ValueError` z polskim komunikatem.
3. `main.py`: w `tariff_sim_job` policz `periods = _th.load(TARIFF_PERIODS_PATH)`,
   `baseline = _th.current(periods, CURRENT_TARIFF_OVERRIDE) or BASELINE_TARIFF`, przekaż do `compute_payload`.
   `_tariff_inputs()` dokłada `'baseline'`. Dodaj `_tariff_periods_changed(periods)` (zapis `_th.save`, wątek
   `tariff_sim_job`) i `_web.set_tariff_periods_callback(...)`; `_web.set_tariff_periods_path`/odczyt analogicznie
   do `tariff_settings`.
4. `web.py`: `/api/tariff_now` → `tariff = inp.get('baseline') or 'G12w'` (docstring bez „(G12w)”).
   `GET /api/tariff_periods` → `{ok, periods, current_tariff, override: <opcja albo null>}`;
   `PUT /api/tariff_periods` body `{periods:[…]}` → walidacja, callback, `{ok, periods}`; `ValueError` → 400.
5. `publisher.py`: nowe sensory `tariffsim_baseline_tariff` (stan = `baseline_tariff`), `tariffsim_month_baseline`,
   `tariffsim_12m_baseline`, `tariffsim_12m_c_baseline`, `tariffsim_saving_vs_baseline` (wartość z
   `current_month.gross[baseline]` itd.). `tariffsim_saving_vs_g12w` zwraca `saving_vs_baseline` (alias).
   `rate_peak/offpeak_gross` bez zmian.
6. Testy TDD: baza G13 (ranking, `saving_vs_baseline`, aliasy równe), baza domyślna G12w identyczna z dzisiejszym
   wynikiem (istniejące testy zielone), `_reconcile` z okresami (zmiana w środku miesiąca pomijana),
   `validate_periods` (OK / złe wejścia), API `GET/PUT /api/tariff_periods` (200/400), publisher (nowe sloty + alias),
   `recommend` bez `baseline_tariff` w starym payloadzie.
- Test: `cd /config/addons/pv_roi_tracker/pv_roi_tracker && PATH=$HOME/.local/bin:$PATH uv run --no-project --with pytest --with-requirements requirements.txt python -m pytest -q` (w worktree: ten sam polecenie w katalogu worktree; baza 1000 passed, 23 skipped).
- `DONE:` testy zielone; `grep -n "'G12w'" pv_roi_tracker/tariff_job.py` zostaje tylko w `BASELINE_TARIFF` i fallbackach.
- `NIE RÓB:` `tariff_sim.py`, `tariff_battery.py`, `tariff_grid_charge.py`, `battery_sim.py`, `heatpump.py`, `static/*`,
  `ebok_client.py`, naprawy długów E2, wersji i commitów. Żadnych prawdziwych danych w fixtures.

### W2 — UI (`haiku-worker`; weryfikacja na żywo ja, `playwright-ha` + `impeccable`)
**Pliki:** `pv_roi_tracker/static/app.js`, `pv_roi_tracker/static/index.html`.
1. Globalnie `_tfBase()` = `(_tsimData && _tsimData.baseline_tariff) || 'G12w'` (źródło: `/api/tariff_sim` → `data.baseline_tariff`;
   dla zakładki Taryfa pobierz z `/api/tariff_now`.tariff). Zastąp literał G12w bazą w: `index.html:176,178`
   (nagłówek „Twoja taryfa <baza>” ustawiany z JS), `app.js:3573,3582,3593,3680,3760-3770,4023,4057,4066,4362,4506-4516,4596-4614,4666,4704,4761`.
   `_TSIM_COLORS`, listy `_TSIM_ALL`, `app.js:3630` i etykiety kolumn pozostają (to katalog taryf).
2. Edytor `tariff_periods` w sekcji „eBOK i faktury”: tabela wierszy (select taryfy z `_TSIM_ALL` bez dyn, `input type=date`,
   usuń), „Dodaj okres”, „Zapisz” → `PUT api/tariff_periods`; błąd 400 pokaż w linii `ebokMsg`; po zapisie odśwież
   symulację. Linia „Taryfa X · moc … · sprawdzono …” z E2 zostaje. Styl istniejących kontrolek.
- `DONE:` `node --check pv_roi_tracker/static/app.js` OK; `grep -n "G12w" app.js` zostaje tylko w: katalogu taryf, `_TSIM_COLORS`,
  fallbackach `|| 'G12w'`, tekstach arbitrażu baterii (`2159`, `index.html:338,518`, dokumentacja `3268-3269`).
- `NIE RÓB:` arbitraż baterii/pompa ciepła (E4/E6), backend, wersji, commitów.

## Weryfikacja (ja)
pytest zielony; po W1 diff przeglądam; po W2 lokalny test API + screenshot przez ingress po wydaniu.
Rollback: `git revert` commitów E3; pliki `tariff_periods.json`/payload zgodne wstecz (nowe klucze addytywne).
