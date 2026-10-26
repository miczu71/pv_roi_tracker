# PLAN E2: szkielet nawigacji i Ustawienia (ROADMAP_UI_SPLIT.md, etap E2)

Kod: `pv_roi_tracker/pv_roi_tracker/` (dalej `PKG`), front `PKG/static/{index.html,app.js,app.css}`, testy `pv_roi_tracker/tests/`.
Treść modułów NIE zmienia się; przenosimy bloki, dodajemy nawigację, usuwamy „Analizę taryf".
Wariant A z prototypu: przełącznik sekcji w nagłówku, ⚙ w rogu, podzakładki pod spodem.

## Model nawigacji (kontrakt)

| Sekcja (hash) | Podzakładki: slug → id panelu (`tab-<id>`) |
|---|---|
| `pv` (domyślna) | `historia`→hist, `prognoza`→pred, `roczne`→years, `wykresy`→charts, `rce`→rce, `magazyn`→battery, `prognoza25`→forecast, `pompa`→heatpump |
| `taryfy` | `symulacja`→tsim (startowa), `faktury`→invoices, `taryfa`→taryfa |
| `ustawienia` | brak podzakładek, jeden panel `tab-settings` |

- Hash: `#pv/historia`, `#taryfy/symulacja`, `#ustawienia`. Pusty/nieznany hash → `#pv/historia`. Wejście na `#taryfy` bez slugu → `symulacja`. Parametry po `?` w hashu ignorowane (E4 je zagospodaruje), ale nie kasowane przy nawigacji w obrębie tej samej podzakładki.
- `showTab(id)` zostaje jako **adapter**: wyznacza sekcję z id (`settings` → ustawienia), ustawia hash (`location.hash = ...`), a właściwe pokazanie robi `applyRoute()` wołane też z `hashchange` i przy starcie (po `#content` widocznym). Dotychczasowe gałęzie `if (name === ...)` (resize wykresów, `loadTaryfaTab`, `loadTsimTab`) zostają 1:1 w `applyRoute`/`showTab`, leniwe ładowanie bez zmian.
- Przełącznik sekcji w `<header>`: dwa przyciski „PV ROI" / „Taryfy i energia" (`.sec-btn`, `aria-current`) + przycisk ⚙ (SVG, nie emoji) „Ustawienia" po prawej. Pasek podzakładek `.tabs` renderowany z tablicy `ROUTES` w JS (jedno źródło prawdy), nie na sztywno w HTML.
- Karta główna (`#cards`, `#yoyTile`, blok z `barChart`) widoczna tylko w sekcji `pv`. W `taryfy` i `ustawienia` ukryta (`display:none`), po powrocie do `pv` wykresy `.resize()`.
- `paymentBar` zostaje nad wszystkim, bez zmian.

## Ustawienia: przenoszone bloki (id bez zmian, więc JS działa bez edycji)
`tab-settings` ma sekcje (nagłówek h3 + blok, kolejność):
1. **Nadpisanie ceny RCEm** — blok `.override-wrap` z `submitOverride`, `rcemBadge`, `ovMonth/ovPrice/ovMsg` (dziś pod `</div>` zakładek, koniec `#content`).
2. **Parametry symulacji taryf** — `.override-wrap` z `saveTsimSettings` (dziś na końcu `tab-tsim`; pola `tsimPhases/tsimBracket/tsimFee/tsimThreshold/tsimMsg`).
3. **Parametry magazynu** — `.override-wrap` z `saveBatteryConfig` (dziś w `tab-battery`; pola `bat*`, `batMsg`).
4. **eBOK i faktury** — wiersz z `ebokSyncBtn`/`ebokMsg` (dziś w `tab-invoices`), `#invLayoutsPanel`, `<details>` „Diagnoza parsera PDF" z `debugPdfFile/runInvoiceDebug/debugOutput`. Wgrywanie PDF (`invoiceFiles/uploadInvoices`) ZOSTAJE w Fakturach.
5. **Wpisy taryfy (ręczne)** — `h3#taryfaFormTitle` + `form#taryfaForm` (dziś w `tab-taryfa`). Lista `#taryfaList` i `#taryfaBadge` zostają w `tab-taryfa`.

Zależności, które trzeba domknąć w JS:
- `editTaryfaEntry(t)` (app.js ~3847) i reset formularza (~3865): przed `scrollIntoView` wołają `showTab('settings')`. W `tab-taryfa` pod „Historia taryf" dopisać przycisk „Dodaj / edytuj w Ustawieniach" → `showTab('settings')`.
- `applyRoute` dla `settings`: woła `loadTsimTab()` (wypełnia pola `tsim*`; funkcja jest idempotentna i działa na ukrytym DOM) i `loadTaryfaTab()` tylko jeśli formularz jeszcze nie ma danych (sprawdź w kodzie, co wypełnia; nie duplikuj wywołań). Pola magazynu i `invLayoutsPanel` wypełniają się przy ładowaniu danych głównych (`renderBattery`, linia ~2375) — sprawdzić, że nie zależą od widoczności panelu.
- `ebokSync()` po sukcesie odświeża dane faktur — sprawdzić, że robi to nie zakładając, że `tab-invoices` jest widoczny.

## Usunięcie „Analizy taryf" (decyzja 4 roadmapy)
Grep (stan 2026-10-26): `tariff_analysis.py` jest importowane przez `yoy.py` (`_month_label`), `battery_sim.py` (`_DEFAULT_*_GROSS`), `web.py` (`_month_label`), więc **moduł zostaje**, usuwamy tylko jego martwe funkcje.
- `static/index.html`: cały `<div id="tab-tariff">` (linie ~183–264, od komentarza zakładki do końca diva) oraz przycisk zakładki.
- `static/app.js`: `renderTariffTab` (~3358–3583), blok zakresu `_rangeData`/`fetchTariffRange`/`renderRangeKpis`/`renderRangeChart`/… (~3585–3710, do początku `loadTaryfaTab`), deklaracje `_tariffPriceChart…_tariffHistChart` (linia 3), `_rangeData`, gałąź `name === 'tariff'` w `showTab`, wywołanie `if (d.tariff_comparison) renderTariffTab(...)` (~2221). Po usunięciu `grep -n "Tariff\(Price\|Comp\|Cum\|Season\|Hist\)Chart\|_rangeData\|fetchTariffRange\|renderRange"` ma dać 0.
- `web.py`: endpointy `/api/tariff_stats`, `/api/export/tariff_csv`; klucz `tariff_comparison` w `_state` (184), `update_tariff_comparison` (206–209), pole w payloadzie (544); import `tariff_analysis as _tariff_analysis` (22) jeśli po usunięciu nieużywany (`_month_label` import zostaje).
- `main.py`: blok „Tariff comparison tab" (~1171–1205: `get_ha_monthly_stats`, `get_ha_history_7d`, `read_tariff_live`, `compute_tariff_tab`, `_record_job('tariff_comparison')`) i import `compute_tariff_tab` (1041). `_invoice_rates` zostaje (używa go reszta). Sprawdzić `grep -rn "tariff_comparison"` w `main.py`/`publisher.py`/testach — job name nie może być czytany gdzie indziej (np. lista jobów w statusie).
- `tariff_analysis.py`: usunąć `compute_tariff_tab`, `compute_range_data`, `_merge_7d_series` i stałe/helpery używane wyłącznie przez nie (`FIXED_GROSS_PLN`, `_ym`, `_season` – tylko jeśli grep w całym `PKG` i `tests` pokaże 0 innych użyć). Zostają `_month_label`, `_DEFAULT_PEAK_GROSS`, `_DEFAULT_OFFPEAK_GROSS`.
- `tests/test_tariff_analysis_rates.py`: testuje tylko `compute_tariff_tab` → usunąć plik (martwy test usuniętej funkcji).
- `live_reader.py`: `get_ha_history_7d`, `read_tariff_live`, `get_ha_monthly_stats` usunąć **tylko** jeśli po zmianach grep w `PKG`+`tests` nie pokazuje innych wywołań; `get_ha_tariff_stats` zostaje (używa go `rce_hourly`). Przy wątpliwości zostawić i zgłosić w raporcie.

## Wykonawca

Dwie paczki o rozłącznych plikach, żadna nie potrzebuje wyniku drugiej. Test końcowy odpala rodzic po obu.
Komenda testu (w worktree): `cd <WORKTREE>/addons/pv_roi_tracker/pv_roi_tracker && /tmp/claude-950/-home-claude/a1b425f4-5b13-4d82-8dc1-b05e8ce0db11/scratchpad/venv/bin/python -m pytest -q`
(fallback venv: `/tmp/claude-950/-home-claude/e490ab76-afe7-4141-9615-c87bf4f15250/scratchpad/venv/bin/python`, jeśli pierwszy zniknął).

### W1 – backend: usunięcie martwego kodu „Analizy taryf" (haiku-worker)
Pliki: `PKG/web.py`, `PKG/main.py`, `PKG/tariff_analysis.py`, `PKG/live_reader.py`, `tests/test_tariff_analysis_rates.py` (usunąć), ewentualnie testy odwołujące się do usuniętych symboli (znajdź grepem).
Zmiana: sekcja „Usunięcie Analizy taryf" powyżej, punkty `web.py`, `main.py`, `tariff_analysis.py`, `live_reader.py`, test. Najpierw grep, potem usuwanie; nie ruszać nic poza wymienionym.
Test: pełny pytest zielony (było 864 passed, 27 skipped; ubywa testów z usuniętego pliku).
DONE: `grep -rn "compute_tariff_tab\|compute_range_data\|api/tariff_stats\|tariff_csv\|update_tariff_comparison\|'tariff_comparison'" PKG tests` → 0 trafień (poza ewentualnym komentarzem historycznym w docs); `python -c "import pv_roi_tracker.main, pv_roi_tracker.web"` bez błędu; pytest zielony.
NIE RÓB: nie edytuj `static/*`, `tariff_sim.py`, `price_sources.py`, `docs/*`; nie usuwaj `get_ha_tariff_stats`, `_month_label`, `_DEFAULT_*_GROSS`; nie commituj; nie formatuj sąsiedniego kodu.

### W2 – frontend: nawigacja, Ustawienia, usunięcie zakładki (sonnet-worker; logika routingu + przenoszenie DOM, nie czysto mechaniczne)
Pliki: `PKG/static/index.html`, `PKG/static/app.js`, `PKG/static/app.css`, `tests/test_web_static.py`.
Zmiana: sekcje „Model nawigacji", „Ustawienia: przenoszone bloki", „Usunięcie Analizy taryf" (części `index.html` i `app.js`) powyżej.
Szczegóły UI: przełącznik w `header` (flex, zawija się na wąskim ekranie), aktywna sekcja wyraźnie zaznaczona kolorem `--accent`; ⚙ jako inline SVG z `aria-label="Ustawienia"`; podzakładki jak dotychczasowe `.tab-btn`/`.tabs` (przewijane poziomo); fokus klawiatury widoczny; bez nowych bibliotek. Istniejące style `.override-wrap`/`.override-form` reużyć.
Test: `tests/test_web_static.py` – dostosować asercje (`id="tab-tsim"` zostaje; `showTab('tsim')` w HTML już nie jest w onclick przycisku → asercja na `ROUTES`/slug `symulacja` w `app.js`), dodać: brak `id="tab-tariff"`, obecność `id="tab-settings"`, obecność `submitOverride`, `saveTsimSettings`, `saveBatteryConfig`, `taryfaForm` wewnątrz `tab-settings` (parsowanie stringowe fragmentu między `id="tab-settings"` a kolejnym panelem lub regex), brak `renderTariffTab`/`fetchTariffRange`/`api/tariff_stats` w `app.js`.
DONE: pytest zielony; `grep -c 'id="tab-tariff"' index.html` = 0; wszystkie id formularzy z listy Ustawień występują dokładnie raz w `index.html`; `node --check app.js` bez błędu (jeśli `node` jest; inaczej pominąć i zgłosić); w `app.js` `showTab` nadal istnieje i jest jedyną funkcją wołaną z `onclick`.
NIE RÓB: nie edytuj plików Pythona poza `tests/test_web_static.py`; nie zmieniaj treści/logiki modułów (Faktury, Taryfa, Symulacja, Magazyn…); nie dotykaj `tsim*` wykresów ani `/api/tariff_hourly` (E3/E4); nie zmieniaj `{{VERSION}}` ani `config.yaml`; nie commituj.

## Weryfikacja (rodzic, po workerach i verifierze)
1. pełny pytest zielony.
2. Lokalny serwer z danymi testowymi + Playwright (desktop 1440 i mobile 390): każda sekcja i podzakładka otwiera się, hash się zmienia, odświeżenie na `#taryfy/faktury` wraca do Faktur, ⚙ otwiera Ustawienia z 5 sekcjami, `browser_console_messages` bez błędów, karta główna znika w „Taryfy" i wraca w „PV ROI".
3. Formularz RCEm, Symulacja i Magazyn: tylko otwarcie i sprawdzenie, że pola są wypełnione (bez zapisu).

## Rollback
`git revert` commitu(ów) E2 (nic nie jest wydane; stan na hoście HA bez zmian do E5).
