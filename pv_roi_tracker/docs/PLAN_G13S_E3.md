# PLAN E3 — payload, API, MQTT, UI, „Najlepszy wariant w skali roku”

Źródło prawdy: `ROADMAP_G13S_GRIDCHARGE.md` (E3). `PKG` = `addons/pv_roi_tracker/pv_roi_tracker/pv_roi_tracker` (w worktree z prefiksem repo `/config`), `TESTS` = `.../pv_roi_tracker/tests`.

## Decyzje projektowe (dopowiedzenie ROADMAP)
- **Baseline rankingu** = G12w wariant A (rzeczywisty koszt z faktur/poboru; użytkownik ma dziś G12w). `saving_pln` każdego wiersza = `baseline.gross − row.gross`.
- B w payloadzie liczone z `soc_floor=0.16` (wcześniej 0 → wartości B się zmienią; opisać w `variant_b_note`).
- C tylko dla `GRID_TARIFFS` (G12, G12w, G13, G13s); dla G11/dyn_a/dyn_b `c = None`, `gross_c[t] = None`.
- C liczone tylko gdy `with_b` (są `hours` i `battery_cfg`) i tylko dla `from_ym..to_ym` payloadu.
- C startuje każdą dobę z SoC=floor (B przenosi SoC między dobami) — wpis w `variant_c_note`; C ≥ B w payloadzie nie jest gwarantowane (gwarancja dotyczy B z resetem dobowym).
- G13s: `confirmed:false` → w `best_year` wiersz ma `confirmed:false`, a `warnings` zawiera ostrzeżenie o niepotwierdzonym cenniku.
- Zmiana grupy taryfowej raz na 12 mies. → stałe ostrzeżenie w `warnings`, gdy `best.tariff != 'G12w'`.

## Kontrakt payloadu (W1 produkuje, W2 konsumuje — dokładnie tak)
```
months[ym][t]['c'] = None | {'savings_pln','gross','grid_charged_kwh','pv_charged_kwh','discharged_kwh','wear_pln'}
   gross = entry['gross'] − savings_pln; None gdy wpis niedostępny/brak wyniku C
current_month['gross_c'] = {t: float|None}
last12['gross_c'] = {t: float|None}; last12['cheapest_c'] = t|None; last12['saving_vs_g12w_c'] = float|None
plans = {t: {'summer_workday'|'summer_dayoff'|'winter_workday'|'winter_dayoff':
             {'charge_hours':[int],'discharge_hours':[int],'days','days_total','avg_saving_pln','avg_grid_kwh'}}}   # t ∈ GRID_TARIFFS
variant_c_note = str
best_year = {
  'baseline': {'tariff':'G12w','variant':'A','gross':float},
  'rows': [{'tariff','variant':'A'|'B'|'C','gross','saving_pln','confirmed':bool}],   # rosnąco po gross, tylko pełne 12 mies.
  'best': <wiersz>|None,
  'warnings': [str]
}   # None gdy brak baseline (G12w A bez pełnych 12 mies.)
```
`recommend(payload, threshold)` → `{'A','B','C','best_year'}`; `A/B/C` jak dotychczas (`_recommend_one` na `gross`/`gross_b`/`gross_c`); `best_year` = `None` albo
`{'best': wiersz, 'saving_pln', 'tariff_change': bool (best.tariff != 'G12w'), 'above_threshold': bool (saving ≥ threshold), 'reason': str}`.
`GET /api/tariff_sim` zwraca to w `recommendation` (web.py bez zmian poza przekazaniem — sprawdzić, czy `recommend` już jest wołane z payloadem; jeśli tak, nic nie zmieniać).

## Wykonawca

### Paczka W1 — `sonnet-worker` (ryzyko: pieniądze) — backend
Pliki: `PKG/tariff_job.py`, `PKG/publisher.py`, `TESTS/test_tariff_job.py`, `TESTS/test_publisher.py`, `TESTS/test_web_tariff_sim.py`.
Zmiany (TDD, testy najpierw):
1. `tariff_job.compute_payload`: wywołanie `simulate_variant_b(..., soc_floor=0.16)`; gdy `with_b` także `tariff_grid_charge.simulate_variant_c(hours, battery_cfg, catalog, rdn, kompas, rcem_by_ym or {}, from_ym, to_ym)`; nowy `_attach_variant_c(months, c_monthly)` (jak `_attach_variant_b`, `gross = entry['gross'] − savings_pln`, `None` dla nie-GRID i niedostępnych), `_sum12_c`, `last12.gross_c/cheapest_c/saving_vs_g12w_c`, `current_month.gross_c`, `plans`, `variant_c_note` (treść: pułap — zna dobę z góry; SoC dolny 16%, zakres 16→100%; do 2 cykli/dobę; C startuje dobę z dolnego SoC, B przenosi SoC; ceny G13s niepotwierdzone), uzupełnienie `variant_b_note` o „SoC ≥ 16%”.
2. `best_year` wg kontraktu (funkcja `_best_year(months, yms, l12 gross/gross_b/gross_c)`), `recommend()` wg kontraktu (stała `RECOMMEND_VARIANTS`).
3. `publisher.py`: sensory `tariffsim_12m_c_g12`, `_c_g12w`, `_c_g13`, `_c_g13s` (PLN, jak 12m), `tariffsim_best_variant` (tekst `"G13s C"`), `tariffsim_best_cost` (PLN), `tariffsim_best_saving` (PLN vs baseline); wartości z `payload['last12']['gross_c']` i `payload['best_year']['best']`; brak danych → None. Zaktualizować test zliczający sensory `tariffsim_*` (16 → 23).
4. Testy: C w `months`/`last12` (syntetyczne `hours`, G12w+G13s), `None` dla G11/dyn, brak C bez `hours`; `best_year` (ranking, baseline, `confirmed:false` dla G13s, ostrzeżenia, `None` bez baseline); `recommend` próg powyżej/poniżej, `tariff_change` False gdy best = G12w C; publisher: nowe sloty; `/api/tariff_sim` zawiera `recommendation.C` i `best_year`.
Komenda: `cd <WORKTREE>/addons/pv_roi_tracker/pv_roi_tracker && PATH=$HOME/.local/bin:$PATH uv run --no-project --with pytest --with-requirements requirements.txt pytest -q tests/test_tariff_job.py tests/test_publisher.py tests/test_web_tariff_sim.py`; na koniec pełny `pytest -q` (baza 925 passed/27 skipped + nowe).
`DONE:` punkty 1–4; testy zielone; pełny zestaw bez nowych porażek; zgodność G12w z fakturami niezmieniona (test rekoncyliacji zielony); lista zmienionych plików.
`NIE RÓB:` nie dotykać `tariff_grid_charge.py`, `tariff_battery.py`, `tariff_catalog.py`, `static/*`, `main.py`, `web.py` (chyba że `/api/tariff_sim` nie przekazuje `recommend` — wtedy zgłoś, nie poprawiaj); bez nowych zależności; bez commitu i zmiany wersji.

### Paczka W2 — `haiku-worker` — UI
Pliki: `PKG/static/app.js`, `PKG/static/index.html`, `PKG/static/app.css`, `TESTS/test_web_static.py`.
Zasada: czytaj kontrakt payloadu powyżej; `_esc()` dla każdego tekstu z payloadu; wszystkie ikony SVG; bez nowych bibliotek; styl kart jak `.tsim-reco`; skill `impeccable` nie wymagany (układ wg istniejących klas), weryfikacja wizualna po wydaniu.
Zmiany:
1. Przełącznik wariantu: przycisk `tsimBtnC` („Wariant C”) w `#tsimVariantSwitch`; `_tsimVariant` ∈ A/B/C; `_tsimHasC()` (= `last12.gross_c` istnieje); `tsimSetVariant`, `_renderTsimVariant` (aria-pressed, notka: `variant_c_note`), `_tsimCell` (dla C: `e.c.gross`, brak → „brak wyniku wariantu C” — G11/dyn pokazują „—”), `_renderTsimTable` (`sums = gross_c`), parametr hash `v=C`, komunikaty „Wykresy godzinowe liczą wariant A” także dla C, `renderTsimBreakdown` komunikat „tylko dla wariantu A” także dla C, `_renderTsim` ukrywa przełącznik, gdy brak B.
2. `_renderTsimReco`: linia także dla C (`rec.C`), `sw` uwzględnia C.
3. Karta „Jak ustawić ładowanie z sieci” (`#tsimPlan`, wstawiona po `#tsimReco`): dla każdej taryfy z `plans` (select/zakładki taryf lub cztery bloki) tabela sezon × dzień (roboczy/wolny): „Ładuj z sieci” i „Rozładuj” jako zakresy godzin (`h` = [h, h+1); helper `_tsimHourRanges([0,1,2,21,22,23]) → "21:00–03:00"`), „oszczędność doby” (`avg_saving_pln`), „dni z akcją x/y”; pod tabelą roczna oszczędność z/bez optymalizacji: `gross_b − gross_c` z `last12` (jeśli oba) i zdanie „pułap — zna dobę z góry” (`variant_c_note`). Brak `plans` → karta ukryta. G13s z odznaką „cennik niepotwierdzony”.
4. Sekcja „Najlepszy wariant w skali roku” (`#tsimBest`): tabela rankingu z `data.best_year.rows` (taryfa, wariant, koszt 12 mies., vs baseline `−/+ zł`, odznaka „niepotwierdzony”), pierwszy wiersz wyróżniony; zdanie rekomendacji z `recommendation.best_year` (`reason`, `tariff_change`, `above_threshold`) i lista `best_year.warnings`; baseline w stopce („punkt odniesienia: G12w, wariant A”). Brak `best_year` → tekst „Brak pełnych 12 miesięcy danych.”.
5. Ustawienia: pole `tsimFeeG13s` (opłata handlowa G13s netto, puste = domyślna 19,10) obok `tsimFee`; `_fillTsimSettings` (`s.sales_fee_g13s_net`), `saveTsimSettings` (puste → `null`); `index.html` z unikalnymi id.
6. `test_web_static.py`: test, że `index.html` ma `tsimBtnC`, `tsimPlan`, `tsimBest`, `tsimFeeG13s` (id unikalne — istniejący `test_settings_ids_unique` zielony), a `app.js` zawiera `_tsimHasC`, `_tsimHourRanges`, `best_year`.
Komenda: `cd <WORKTREE>/addons/pv_roi_tracker/pv_roi_tracker && PATH=$HOME/.local/bin:$PATH uv run --no-project --with pytest --with-requirements requirements.txt pytest -q tests/test_web_static.py` + `node --check static/app.js` (jeśli node jest; inaczej pominąć i zaznaczyć).
`DONE:` punkty 1–6; test static zielony; `node --check` bez błędów (lub adnotacja o braku node); lista zmienionych plików.
`NIE RÓB:` nie dotykać plików `*.py` poza testem, `tariffs/*`; nie zmieniać kontraktu payloadu; nie zmieniać istniejących id; bez commitu i zmiany wersji.

## Weryfikacja etapu (ja, po `wt land` obu paczek)
1. `verifier` per paczka (BASELINE `git status --short` sprzed delegacji, ścieżka worktree); `claimcheck` pomija repo `/config` (odnotować).
2. Pełny `pytest -q`.
3. Skrypt jednorazowy (scratchpad): `compute_payload` na syntetycznych `hours` → sprawdzić kontrakt (`best_year`, `gross_c`, C dla G13s, `None` dla G11), `recommend` na tym payloadzie.
4. UI: render na fikcyjnym payloadzie nie jest możliwy w kontenerze bez danych → Playwright przez ingress po wydaniu 0.48.0 (zapisane jako niezweryfikowane do wydania).

## Rollback
`wt drop` przed landem; po landzie `git revert` commitu(ów) E3 (payload/UI/sensory addytywne; jedyna zmiana istniejących wartości to B z `soc_floor=0.16`).
