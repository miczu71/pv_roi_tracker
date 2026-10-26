# PLAN E4 – Symulacja: pasek sterowania + 4 wykresy

Źródło prawdy: `docs/ROADMAP_UI_SPLIT.md` (decyzja 7, wariant A z prototypu E0). Backend z E1 wystarcza (`/api/tariff_hourly`), bez zmian.
Ścieżki niżej względem `pv_roi_tracker/pv_roi_tracker/pv_roi_tracker/` (front: `static/{index.html,app.js,app.css}`).

## Zakres
Tab `tab-tsim`: zostają rekomendacja (`#tsimReco`), tabela 12 mies., wykres miesięczny, rozbicie i zgodność z fakturami (bez zmian). Dochodzi blok `#tsimCtl` + `#tsimHourly` **pod rekomendacją, nad tabelą**. Przełącznik wariantu A/B (`#tsimVariantSwitch`) zostaje jednym kontrolkiem — pasek go nie duplikuje, tylko go zawiera (przenieść w DOM do paska, id bez zmian).

### 1. Pasek sterowania `#tsimCtl` (przyklejony: `position: sticky; top: 0`, tło `var(--card)`)
- Checkboxy taryf: G11, G12, G12w, G13, dyn_a, dyn_b (kolory `_TSIM_COLORS`). Domyślnie zaznaczone G12w i dyn_a (o ile są w `_tsim.data.tariffs`, inaczej G12w); min. 1 zaznaczona (ostatniej nie da się odznaczyć).
- Okres (segmenty): Dziś, Wczoraj, 7 dni, 14 dni, Ten miesiąc, 12 mies., Jutro, Własny (dwa `<input type=date>`, widoczne tylko dla „Własny”). Domyślnie „7 dni” (`from = dziś−6`, `to = dziś`). „12 mies.” = ostatnie 365 dni. Limit zakresu 400 dni (błąd API pokazać w `#tsimHourlyMsg`).
- Wariant A/B: istniejący przełącznik. B steruje tylko tabelą/rekomendacją; na wykresach godzinowych B jest ignorowany, a przy aktywnym B w `#tsimHourlyMsg` stoi dopisek „Wykresy godzinowe liczą wariant A (B to miesięczna korekta bez godzin).”.
- Para do różnicy narastająco: dwa `<select>` (taryfa 1, taryfa 2) wśród zaznaczonych; domyślnie pierwsze dwie zaznaczone. Gdy zaznaczona jedna taryfa — wykres różnicy ukryty z komunikatem „Zaznacz co najmniej 2 taryfy”.
- Stan w hashu: `#taryfy/symulacja?t=G12w,dyn_a&p=7d&v=A&pair=G12w,dyn_a&from=YYYY-MM-DD&to=YYYY-MM-DD` (`p` ∈ `today|yesterday|7d|14d|month|12m|tomorrow|custom`; `from/to` tylko dla `custom`). Zapis `history.replaceState` (przez istniejące `_appliedHash`, żeby nie wywołać `applyRoute`); odczyt przy wejściu w tab z `_parseHash().query` (`URLSearchParams`). Nieznane/błędne wartości → domyślne. Link = ten sam widok.
- Mobile (`max-width:640px`): pasek zwinięty do przycisku „Filtry” (`aria-expanded`), rozwijany panel; na mobile mapa stref pokazuje 1 taryfę naraz (`<select>` nad mapą) zamiast wszystkich.

### 2. Dane
`GET api/tariff_hourly?from=&to=&tariffs=<zaznaczone>` (cache po stronie serwera; po stronie JS tylko ostatnia odpowiedź + licznik żądań, wyniki starych żądań odrzucać — `reqId`). `zone_map`, `cost_by_zone`, `cumulative_diff` (dla `pair`: parametr `tariffs` musi mieć parę jako dwa pierwsze elementy → worker sortuje listę tak, by `pair[0],pair[1]` były pierwsze), `hours` (tylko ≤31 dni, inaczej `null`), `tomorrow`.
Taryfa z `available:false` → szara etykieta z `reason`, bez serii.
Błąd HTTP/`ok:false` → komunikat w `#tsimHourlyMsg`, wykresy czyszczone; nigdy cichy fallback.

### 3. Cztery wykresy (Chart.js z `static/vendor`; wspólna paleta `_TSIM_COLORS`)
1. **Mapa stref i cen** — `#tsimZoneMap`: siatka CSS lub canvas, wiersz = dzień, kolumna = godzina 0–23, jeden blok na taryfę (obok siebie na desktopie). Kolor komórki = `gross_per_kwh` na **wspólnej skali koloru** dla wszystkich taryf (min–max z danych; `None` = szary), obramowanie/znacznik strefy (peak/offpeak/…) tooltipem `title` („G12w · 2026-10-26 17:00 · peak · 1,2345 zł/kWh”). Legenda skali. 12 mies. = ≤400 wierszy: canvas lub CSS grid z `contain`, bez zawieszania UI.
2. **Cena godzinowa + pobór** — `#tsimPriceChart`: linie `stepped:true` z `hours[taryfa][].gross_per_kwh`, słupki `kwh` (oś y2, kolor szary, pobór ten sam dla wszystkich taryf — wziąć z pierwszej taryfy). Dostępny dla ≤31 dni (`hours != null`), inaczej komunikat „Wykres godzinowy dla zakresów do 31 dni”. „Jutro”: tylko ceny (pobór wyłączony).
3. **Koszt wg stref** — `#tsimZoneCost`: słupki skumulowane, oś x = taryfa, segmenty = strefa (kWh i zł w tooltipie) + segment „Opłaty stałe” z `fixed_gross`; wartość słupka = `cost_by_zone[t].total_gross`. `missing_kwh > 0` → dopisek pod wykresem. Przy dyn_a dopisek „koszt godzinowy bez miesięcznej podłogi średniej”. „Jutro” → wykres ukryty (koszty niedostępne).
4. **Różnica narastająco** — `#tsimDiffChart`: linia z `cumulative_diff` {days, diff_gross}, tytuł „<t1> − <t2> narastająco (zł brutto, bez opłat stałych)”; `skipped_kwh > 0` → dopisek. „Jutro” → ukryty.

Gdy okres = „Jutro”: `tomorrow.rdn_available === false` lub `reason` → pokazać `reason` (dyn_* bez cen); `eurpln_fallback` → dopisek „kurs EUR/PLN z ostatniego znanego dnia”. Dla „Jutro” żądanie `from=to=jutro`.

### 4. Cykl życia
- `loadTsimTab` po `_renderTsim` woła `_tsimInitCtl()` (jednorazowo buduje pasek) i `_tsimLoadHourly()`.
- Zmiana kontrolki → `_tsimSyncHash()` + `_tsimLoadHourly()` (debounce 150 ms).
- `applyRoute` przy `name === 'tsim'` resize nowych wykresów; zniszczenie wykresów przy ponownym renderze (`chart.destroy()` jak `_tsimChart`).
- Konwencje: `_esc` dla każdego tekstu z API, ARIA (`role=group`, `aria-pressed`, `aria-expanded`), focus-visible jak `.tsim-seg`.

## Kryteria sukcesu
- Dla wybranego dnia i taryf strefy/ceny na mapie zgodne z `zone_map` z API (spot-check 3 komórek G12w względem `tariffs/tauron.json` i `tariff_catalog.zone_for`, nie z pamięci).
- Suma `total_gross` z wykresu 3 za pełny miesiąc (G12w) zgadza się z `/api/tariff_sim` wariant A ±0,01 zł (dyn_a wyjątek).
- Hash odtwarza stan po przeładowaniu (zmiana → F5 → te same kontrolki i wykresy); błędny hash nie rzuca.
- „Jutro”: bez wykresów kosztów, komunikaty o RDN/kursie; wariant B wyszarzony z dopiskiem.
- Konsola bez błędów JS na 1440 i 390; brak przewijania poziomego strony; wszystkie dotychczasowe id Symulacji (`tsimReco`, `tsimTbl`, `tsimChart`, `tsimBreak*`, `tsimRecon*`, `tsimBtnA/B`, `tsimVariantSwitch`) istnieją.
- `pytest -q` zielony (866 passed, 27 skipped).

## Wykonawca (jedna paczka, sonnet-worker – UI z logiką danych, 4 wykresy; ryzyko średnie)
Pliki: `static/index.html`, `static/app.js`, `static/app.css`, `tests/test_web_static.py`.
Zmiany:
1. `index.html`: w `#tsimContent` po `#tsimReco` blok `#tsimCtl` (checkboxy, okres, daty, para, przycisk „Filtry”; przenieść `#tsimVariantSwitch` do paska — ten sam id, handlery bez zmian), `#tsimHourlyMsg`, `#tsimZoneMap` (+ `<select id="tsimMapTariff">` mobile), canvasy `#tsimPriceChart`, `#tsimZoneCost`, `#tsimDiffChart` w `.chart-wrap`; układ: mapa pełna szerokość, cena+pobór i koszt wg stref w 2 kolumnach (`.grid2`), różnica pod spodem.
2. `app.js`: stan paska + hash (`_tsimParseCtl`, `_tsimSyncHash`), `_tsimInitCtl`, `_tsimRange`, `_tsimLoadHourly` (reqId, błędy do `#tsimHourlyMsg`), cztery renderery (`_tsimRenderZoneMap`, `_tsimRenderPrice`, `_tsimRenderZoneCost`, `_tsimRenderDiff`), niszczenie wykresów, `applyRoute` resize; `_renderTsimVariant` dopisuje notę B dla wykresów.
3. `app.css`: pasek sticky, segmenty okresu, checkboxy z kolorem taryfy, mapa stref (skala koloru, legenda), `@media (max-width:640px)` zwijany pasek i jedna taryfa na mapie; tokeny z `:root`, bez nowych kolorów poza `_TSIM_COLORS` i skalą mapy.
4. `tests/test_web_static.py`: asercje na istnienie nowych id (`tsimCtl`, `tsimZoneMap`, `tsimPriceChart`, `tsimZoneCost`, `tsimDiffChart`, `tsimHourlyMsg`) i starych id Symulacji.
Komenda testu: `cd <WORKTREE>/pv_roi_tracker && /tmp/claude-950/-home-claude/a1b425f4-5b13-4d82-8dc1-b05e8ce0db11/scratchpad/venv/bin/python -m pytest -q`; dodatkowo Playwright na lokalnym serwerze testowym (`.../037200cd-da62-44ea-af33-8500768b5943/scratchpad/serve_e3.py`; zrzuty tylko do `/home/claude/.playwright-mcp`) — jeśli serwer nie jest dostępny dla worktree, zgłosić w raporcie zamiast improwizować.
`DONE:` pkt 1–4 wg kryteriów; pytest zielony; raport z listą zmienionych plików, zrzutami 1440/390 sekcji Symulacja (pasek + 4 wykresy, okresy „7 dni” i „Jutro”) i wynikiem porównania sumy kosztu wg stref z `/api/tariff_sim`. **Dostarcz raport wcześnie** (limit tur).
`NIE RÓB:` nie ruszać backendu (`web.py`, `tariff_*.py`, `price_sources.py`), `tab-settings`, `tab-taryfa`, `tab-invoices`, tabeli/rekomendacji/rozbicia/zgodności Symulacji (poza przeniesieniem przełącznika wariantu); nie zmieniać istniejących id; nie dodawać zależności ani bibliotek; nie commitować; nie edytować plików poza listą.

## Rollback
`git revert` commitu E4 (backend nietknięty).

## Odroczone do E5 (nieblokujące)
`kompas_reason` w `fetch_ahead` (brak ceny dyn_b nie ma wyjaśnienia w UI); słabe asercje `and/or` w `tests/test_web_tariff_hourly.py:68-70`.
