# PLAN E4 — zakładka „Symulacja taryf” (UI) + ustawienia i rekomendacja

Roadmapa: `docs/ROADMAP_TARIFFS.md`. E2/E3 dają `GET /api/tariff_sim` (warianty A i B, 6 taryf: G11, G12, G12w, G13, dyn_a, dyn_b). E4 pokazuje to userowi, daje rekomendację z progiem i edycję ustawień wpływających na koszty. Rollback: revert commitu (zakładka, endpointy i plik ustawień znikają; `/data/tariff_settings.json` można zostawić).

## Decyzje
1. **Nowa zakładka `tsim`** „Symulacja taryf” (istniejące „Analiza taryf” = `tariff` i „Taryfa” = `taryfa` zostają bez zmian; przeniesienie/wycofanie dopiero w E5).
2. **„Edycja cennika” = edycja ustawień** `TariffSettings` (fazy 1/3, próg mocy umownej, opłata handlowa netto) + próg rekomendacji. Stawki cennika (`tariffs/tauron.json`) NIE są edytowalne w UI (odstępstwo od zdania „edycja cennika” w ROADMAP — stawki są ze źródeł URE/Tauron, user: „bez założeń”; edycja = plik). Zapis ustawień przelicza symulację w tle.
3. **Próg rekomendacji** (`recommend_threshold_pln`, domyślnie 100 zł/rok, zakres 0–5000) trzymany w ustawieniach w add-onie (nie `input_number`). Rekomendacja liczona przy każdym `GET /api/tariff_sim` (zmiana progu nie wymaga przeliczenia).
4. **Rekomendacja** `tariff_job.recommend(payload, threshold)` → `{'A': r, 'B': r}`, gdzie `r = {'best', 'g12w', 'saving_pln', 'switch', 'reason'}`: dla wariantu `V` bierze `last12.gross` (A) / `last12.gross_b` (B); `best` = najtańsza z taryf o wartości ≠ None; `saving_pln = gross['G12w'] − gross[best]`; `switch = best != 'G12w' and saving_pln >= threshold`. Brak `last12`/`G12w`/B → `r = None`. `reason`: krótki polski tekst („G12w jest najtańsza”, „oszczędność X zł poniżej progu Y zł”, „brak pełnych 12 mies. dla G12w”, „zmiana opłaca się: X zł/rok”).
5. **API:** `GET /api/tariff_sim` zwraca `{'ok': True, 'data': payload}` jak dotąd; gdy `payload` nie jest None dodatkowo `'recommendation': {...}` i `'settings': {...}` (ustawienia z pliku). Gdy None → dokładnie `{'ok': True, 'data': None}` (test istniejący). `GET/POST /api/tariff_settings`: GET → `{'ok', 'settings'}`; POST walidacja → 400 z `error` po polsku, sukces → zapis, callback (przeliczenie w wątku), `{'ok': True, 'settings': ..., 'recomputing': True}`.
6. **UI (tryb Operate, styl istniejącego add-onu: `.cards`, `.chart-wrap`, `.tbl-wrap`, `.override-wrap/.override-form`, zmienne CSS z `app.css`; bez emoji — ikona zakładki jako inline SVG).** Układ od góry:
   - `#tsimWaiting` (brak danych) / `#tsimContent`.
   - **Rekomendacja** `#tsimReco`: jedno zdanie werdyktu (duży tekst) + dwie linie A i B („A: zostań przy G12w”, „B: G13 −412 zł/rok”), kolor tła z tokenów (zielony tint = zmiana się opłaca, neutralny = zostań). Bez bocznego paska; bez wielkiej liczby w stylu „hero metric”.
   - Przełącznik wariantu A/B (segmented, `role="tablist"`/przyciski z `aria-pressed`), pod nim jedno zdanie, co oznacza wariant (A: ten sam pobór wyceniony w każdej taryfie; B: z modułem magazynu — treść z `variant_b_note`).
   - **Tabela 12 mies. × 6 taryf** `#tsimTbl`: wiersze = ostatnie 12 pełnych miesięcy (+ bieżący, oznaczony „w toku”, nie wchodzi do sumy), kolumny = taryfy; komórka = brutto zł (bez groszy); najtańsza w wierszu pogrubiona z tłem; `None`/niedostępna → „—” z `title` = `reason`; `reliable:false` → wartość w nawiasie `~` z `title` „pokrycie cen X%”; wiersz sumy 12 mies. i wiersz „vs G12w” (różnica zł, + gorsza / − tańsza). `font-variant-numeric: tabular-nums`, pierwsza kolumna sticky, przewijanie poziome na wąskim ekranie.
   - **Wykres** `#tsimChart` (Chart.js, linia miesięczna brutto per taryfa, wariant z przełącznika; taryfy z `None` jako przerwy `spanGaps:false`).
   - **Rozbicie kosztów** `<details>` z selektem miesiąca (domyślnie ostatni pełny): tabela taryfy × (energia, dystrybucja, opłaty zm., stałe, VAT, brutto) — tylko wariant A (komunikat przy B).
   - **Zgodność z fakturami**: jedno zdanie z `reconciliation_summary` („G12w vs faktury: N z M mies. w ±3%, maks. rozjazd X%”) + `<details>` z tabelą miesięcy.
   - **Ustawienia** (`.override-wrap`): fazy (select 1/3), moc umowna (select `lt500|500_1200|1200_2800|gt2800` z etykietami), opłata handlowa netto zł/mies., próg rekomendacji zł/rok, przycisk „Zapisz i przelicz”, komunikat; po zapisie odpytanie `/api/tariff_sim` co 5 s do zmiany `generated_at` (maks. 24 próby).
   - Stany: ładowanie, brak danych, błąd fetch (komunikat z kodem, nie ciche), pusty wariant B (ukryj przełącznik, gdy brak `last12.gross_b`).
   - Wszystkie teksty po polsku; escapowanie wartości z payloadu (`reason`, `variant_b_note`) przez istniejący helper (sprawdź `esc`/`escapeHtml` w `app.js`; jeśli brak, użyj `textContent`).

## Kontrakt

### `pv_roi_tracker/tariff_settings_store.py` (NOWY)
`DEFAULTS = {'phases': 3, 'capacity_bracket': 'gt2800', 'sales_fee_net': 0.0, 'recommend_threshold_pln': 100.0}`; `BRACKETS = ('lt500','500_1200','1200_2800','gt2800')`; `load(path) -> dict` (brak/uszkodzony plik → DEFAULTS, uszkodzony loguje warning); `validate(raw: dict) -> dict` (rzuca `ValueError` po polsku: `phases ∈ {1,3}`, bracket w `BRACKETS`, `sales_fee_net` 0–200, `recommend_threshold_pln` 0–5000; liczby jako float, nieznane klucze ignorowane, brakujące z DEFAULTS); `save(settings, path)` atomowo (wzorzec `battery_store._atomic_write`); `to_catalog_settings(d) -> TariffSettings`.

### `pv_roi_tracker/tariff_job.py`
Dopisz `recommend(payload, threshold_pln)` wg decyzji 4 (czysta funkcja).

### `pv_roi_tracker/web.py`
- `set_tariff_settings_path(path)`, `set_tariff_settings_callback(fn)` (wzorzec `set_battery_config_*`), routes `GET/POST /api/tariff_settings`, zmiana `api_tariff_sim` wg decyzji 5. Importy lokalne w funkcjach jak sąsiednie routes.

### `pv_roi_tracker/main.py`
- Stała `TARIFF_SETTINGS_PATH = Path(os.environ.get('TARIFF_SETTINGS_PATH', '/data/tariff_settings.json'))` obok `TARIFF_SIM_PATH`; dopisz ją do krotki ścieżek w ~l.274 tylko jeśli tam trafiają pozostałe `TARIFF_*` (sprawdź, po co ta krotka).
- `tariff_sim_job` używa `tariff_settings_store.to_catalog_settings(tariff_settings_store.load(TARIFF_SETTINGS_PATH))` zamiast `_tcat.TariffSettings()`.
- `_web.set_tariff_settings_path(...)`, callback zapisuje ustawienia i startuje `Thread(target=tariff_sim_job, daemon=True, name='tariff-sim-settings')`.

### Frontend: `static/index.html`, `static/app.js`, `static/app.css`
Wg decyzji 6. W `app.js`: przycisk w pasku (`showTab('tsim')`), `'tsim'` w tablicy `TABS` w pozycji zgodnej z kolejnością przycisków (wstaw przycisk i element tablicy w tym samym miejscu — po `taryfa`), `loadTsimTab()` wołane z `showTab`, resize wykresu. Unikaj klas/ID `tariff*` i `taryfa*` (zajęte). Style w `app.css` jako klasy `.tsim-*`, tokeny istniejące, `@media (max-width: 700px)`, `:focus-visible` dla nowych kontrolek, `prefers-reduced-motion` bez animacji w ogóle.

## Wykonawca
WORKTREE: `wt new /home/claude/ha-config pv-roi-tariffs-e4`; katalog add-onu `addons/pv_roi_tracker/pv_roi_tracker`. Jedna paczka (frontend zależy od kontraktu API). Skill `addon-conventions` obowiązuje.

Pliki i zmiany (TDD dla backendu — testy najpierw):
1. `pv_roi_tracker/tariff_settings_store.py` — NOWY wg kontraktu.
2. `tests/test_tariff_settings_store.py` — NOWY: defaults przy braku pliku i uszkodzonym JSON; round-trip; każda walidacja (fazy 2, bracket „x”, opłata −1, próg 6000 → ValueError); nieznane klucze ignorowane; `to_catalog_settings`.
3. `pv_roi_tracker/tariff_job.py` + `tests/test_tariff_job.py` (dopisz): `recommend` — zostań (G12w najtańsza), zmiana ≥ progu, saving poniżej progu → `switch False`, B brak → `None`, G12w `None` → `None`, taryfa z `None` pomijana.
4. `pv_roi_tracker/web.py` + `tests/test_web_tariff_sim.py` (dopisz) + `tests/test_web_tariff_settings.py` NOWY: `GET /api/tariff_sim` bez payloadu bez zmian; z payloadem zawiera `recommendation` i `settings`; `POST /api/tariff_settings` 400 przy błędzie, 200 i wywołanie callbacku przy poprawnych danych, 500 gdy nieskonfigurowane. Istniejący `test_z_payloadem` zaktualizuj (data równa payloadowi, dodatkowe klucze obecne).
5. `pv_roi_tracker/main.py` — wiring wg kontraktu.
6. `static/index.html`, `static/app.js`, `static/app.css` — zakładka wg decyzji 6.
7. `tests/test_web_static.py` — dopisz test: `GET /` zawiera `id="tab-tsim"`, `showTab('tsim')`; `app.js` zawiera `'tsim'` w `TABS` i `loadTsimTab`.

Komenda testu: `cd <WORKTREE>/addons/pv_roi_tracker/pv_roi_tracker && uv run --no-project --with pytest --with-requirements requirements.txt pytest -q`.

DONE: wszystkie testy zielone (791 passed / 27 skipped poprzednio + nowe, bez nowych skipów); `node --check static/app.js` bez błędu (jeśli node jest; inaczej pomiń i napisz); `git status --short` w worktree pokazuje tylko pliki z listy; endpointy działają na testowym kliencie Flask; brak `emoji`/ikon Unicode w nowym UI.

NIE RÓB: nie zmieniaj `tariff_sim.py`, `tariff_battery.py`, `battery_sim.py`, `publisher.py`, `tariffs/tauron.json`, plików wersji (`config.yaml`, `__init__`), istniejących zakładek poza dodaniem przycisku/`TABS`/`showTab`; nie dodawaj sensorów MQTT; nie commituj; nie ruszaj plików poza listą (w `/config` są zmiany spoza roadmapy); nie dodawaj bibliotek ani plików w `static/vendor`.
