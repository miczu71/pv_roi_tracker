# E2 — sugestie ustawień ładowania z sieci (backend)

Roadmapa: `ROADMAP_GRIDCHARGE_SUGGEST.md`. Ryzyko: logika pieniędzy → `sonnet-worker`.

## Problem
Głosowanie godzin DP (`plans[...]['charge_hours']`, próg ≥50% dób) prawie nigdy nic nie zwraca, a DP zna dobę z góry,
więc jego godzin nie da się przenieść do automatyki. Potrzebna jest **prosta reguła**, którą user wpisze w JIT:
okno ładowania z sieci + cel SoC + strefy rozładowania, oceniona na prawdziwych danych godzinowych.

## Decyzje projektowe
1. Nowa funkcja `suggest_plans(...)` w `tariff_grid_charge.py`; `simulate_variant_c` zostaje bez zmian (miesięczne C
   i stare pola `plans` zostają do E3, który przestaje ich używać i je usuwa — UI nie może się wysypać między etapami).
2. Zakres oceny = ostatnie 12 miesięcy kończące się na `to_ym` (nie krótszy niż `from_ym`). Grupa = sezon × typ dnia
   (`summer|winter` × `workday|dayoff`, klucze jak w `simulate_variant_c`: `summer_workday` …), grupa godziny = grupa
   jej daty.
3. **Reguła kandydata** `(okno, cel, strefy rozładowania)`:
   - okno = `[start, end)` w godzinach doby, może przechodzić przez północ; ładowanie z sieci tylko w godzinach okna
     w dobach danej grupy, do celu SoC;
   - cel = SoC całkowity w % pojemności (`cfg.usable_kwh`), krok 10, od 30 do 100, tylko > `soc_floor*100`;
     `target_kwh = (pct/100 - soc_floor)/(1 - soc_floor) * U`, gdzie `U = cfg.usable_kwh*(1-soc_floor)`;
   - strefy rozładowania = godziny doby z ceną profilową ≥ próg; dotyczą **energii z sieci** w magazynie
     (energia z PV jest rozładowywana w każdej godzinie, jak w B), na wszystkich dobach (po godzinie doby, nie po grupie).
4. **Symulator godziny** = konwencje `battery_sim.simulate_s1` (wspólny budżet mocy `P` na godzinę; kolejno:
   ładowanie z eksportu → rozładowanie energii PV przeciw importowi → rozładowanie energii z sieci w strefie rozładowania →
   zakup z sieci w oknie do celu), z `soc_pv` i `soc_grid` osobno, sprawność jednokierunkowa `eff=sqrt(roundtrip_eff)`.
   Rachunek zł: `+deliver*cena` przy rozładowaniu, `-charge*rcem` przy ładowaniu z eksportu,
   `-buy*(cena+wear)` przy zakupie, `wear=throughput_cost_kwh(cfg)`. Magazyn `U` (zakres użyteczny po odjęciu `soc_floor`).
   Godziny bez ceny (`price_fn(key) is None`) pomijane jak w `simulate_s1`. SoC ciągły przez całą serię, reset nie jest
   potrzebny (seria chronologiczna; luka w danych tylko pomija godziny).
5. **Baseline** = ten sam symulator bez kandydata (brak zakupu z sieci; PV rozładowywane zawsze). `saving_vs_b_pln_year`
   to różnica sum oszczędności serii (kandydat stosowany tylko do godzin grupy `g` w oknie/celu, strefy rozładowania wszędzie)
   minus baseline, pomnożona przez `dni_roku(g) / dni_zakresu(g)`; `dni_roku(g)` = liczba dni grupy w 12 miesiącach
   kończących się na `to_ym` (kalendarz, `_is_day_off`), `dni_zakresu(g)` = doby grupy mające ≥1 godzinę z ceną w zakresie.
   Nie przycinać wyniku do ≥0 — ale kandydat „brak ładowania z sieci” (= baseline) zawsze uczestniczy, więc najlepszy ≥ 0.
6. **Przegląd kandydatów** (koordynatowy, by zmieścić się w czasie): profil ceny grupy = mediana ceny po godzinie doby
   z godzin grupy z ceną (`statistics.median`). Poziomy = unikalne `round(cena, 4)` rosnąco. Okna = dla każdego poziomu
   `L` poza najwyższym: maksymalne cykliczne ciągi godzin z profilem ≤ L. Progi rozładowania = poziomy większe od najwyższej
   ceny profilowej w oknie. Etap 1: wszystkie `(okno, próg)` z celem 100%. Etap 2: dla najlepszego — wszystkie cele.
   Wygrywa najwyższy zysk; remis → krótsze okno, potem niższy cel (deterministycznie).
7. **Wynik** `suggest_plans(...) -> {taryfa: {grupa: {...}}}`, pola grupy:
   `charge_window` (`{'start': h, 'end': h}` albo `None`), `target_soc_pct` (int albo `None`),
   `discharge_zones` (lista `{'start','end'}` — maksymalne ciągi godzin ≥ progu, może być pusta),
   `saving_vs_b_pln_year` (float, zł/rok, zawsze ≥ 0, także gdy `worth_it=False` — najlepszy znaleziony),
   `worth_it` (`saving >= MIN_WORTH_PLN_YEAR`, stała `MIN_WORTH_PLN_YEAR = 20.0`), `days_total` (jak wyżej `dni_zakresu`).
   Gdy `worth_it=False`: `charge_window`, `target_soc_pct` = `None`, `discharge_zones` = `[]`.
8. „Pułap C” z roadmapy = istniejące `last12.gross_b - last12.gross_c` na taryfę (już w payloadzie, E3 go pokaże) —
   nie dodajemy go do `plans`.
9. `tariff_job.py`: po `simulate_variant_c` wywołać `suggest_plans(...)` (te same argumenty co C, bez `overrides` — tak jak C)
   i scalić do `c_plans[t][gk]` przez `update` (nowe pola obok starych). Taryfa/grupa bez dób w zakresie → brak wpisu.

## Wykonawca

WORKTREE: ścieżka z `wt new /config/addons/pv_roi_tracker gridcharge-e2` (podaje rodzic). Kod w
`<WORKTREE>/addons/pv_roi_tracker/pv_roi_tracker/` (tam `pytest.ini`, `requirements.txt`; pakiet w
`pv_roi_tracker/pv_roi_tracker/`) — WORKTREE to cały klon `/config`.

Pliki i zmiany:
1. `pv_roi_tracker/pv_roi_tracker/tariff_grid_charge.py` — dodaj (nie ruszaj istniejących funkcji):
   - stała `MIN_WORTH_PLN_YEAR = 20.0`, `TARGET_STEP_PCT = 10`;
   - `_simulate_schedule(recs, cfg_u, U, cand, active_group)` → suma oszczędności (float). `recs` = lista krotek
     `(hour, exp, imp, price, rcem, group)` (przygotowana raz na taryfę, chronologiczna, bez godzin bez ceny);
     `cand` = `None` albo `(window_hours: frozenset, target_kwh: float, dz: frozenset)`; zakup w oknie tylko gdy
     `group == active_group`. Mała, bez I/O;
   - `suggest_plans(hours, cfg, catalog, rdn, kompas, rcem_by_ym, from_ym, to_ym, overrides=None, *,
     soc_floor=SOC_FLOOR, tariffs=GRID_TARIFFS) -> dict` wg „Decyzje projektowe” 2–7. Helpery prywatne
     (`_window_runs`, `_year_day_counts`, `_group_key(day)`), spójne z istniejącym `_season`/`_is_day_off`;
   - zaktualizuj docstring modułu o sugestie (jedno zdanie).
2. `pv_roi_tracker/pv_roi_tracker/tariff_job.py` — po `simulate_variant_c` (~linia 185) wywołanie i scalenie wg p.9.
3. `pv_roi_tracker/tests/test_tariff_grid_charge.py` — nowe testy (styl pliku, fixture `cat`):
   - duży spread: G12, zima (styczeń 2026, 31 dób), pobór 0,8 kWh/h przez całą dobę, bez eksportu → dla `winter_workday`
     `worth_it`, wszystkie godziny okna mają cenę profilową niższą od najwyższej, `target_soc_pct` > 16,
     `saving_vs_b_pln_year` > `MIN_WORTH_PLN_YEAR`, `discharge_zones` niepuste i rozłączne z oknem;
   - mały spread: `monkeypatch` `gc.make_price_fn` na płaską cenę z różnicą 0,001 zł → `worth_it=False`, okno `None`,
     cel `None`, strefy `[]`, `saving_vs_b_pln_year` ≥ 0;
   - G13s zima roboczy: jak w istniejącym teście G13s (pobór wieczorem), okno nie zawiera godzin o najwyższej cenie;
   - PV: lato, eksport w południe + pobór wieczorem → sugestia nie psuje: `saving_vs_b_pln_year` ≥ 0 i `worth_it`
     zgodne z progiem;
   - schemat: każda grupa ma komplet pól i typów z p.7; godziny bez ceny (G13s przed `g13s_from`) nie psują wyniku;
   - test jednostkowy `_simulate_schedule`: kandydat `None` daje to samo co `battery_sim.simulate_s1` z `cfg_b` na tej samej
     serii (tolerancja 1e-9) — pilnuje zgodności konwencji;
   - wydajność: 365 dób, 4 taryfy, jeden profil godzinowy → `suggest_plans` < 15 s.

Komenda testu (z `<WORKTREE>/addons/pv_roi_tracker/pv_roi_tracker/`):
`uv run --no-project --with pytest --with-requirements requirements.txt pytest tests/test_tariff_grid_charge.py tests/test_tariff_job.py -q`,
na końcu pełny zestaw: `uv run --no-project --with pytest --with-requirements requirements.txt pytest -q`.

DONE: nowe testy przechodzą; pełny pytest zielony (poprzednio 962 passed/27 skipped + nowe); raport zawiera czas
`suggest_plans` na 365 dobach i wynik na syntetycznych danych z `diag_c.py` (jeśli jest) — tabela taryfa × grupa:
okno, cel, `saving_vs_b_pln_year`, `worth_it`.

NIE RÓB: nie ruszaj `battery_sim.py`, `tariff_battery.py`, `tariff_catalog.py`, istniejących funkcji
`solve_day`/`simulate_variant_c`/`_run_dp`, UI (`static/*`), `publisher.py`, `config.yaml`, plików poza trzema powyżej;
nie usuwaj starych pól `plans`; nie commituj; nie dodawaj zależności; nie zmieniaj sygnatur publicznych.

## Weryfikacja (rodzic)
`git diff` w worktree, pełny pytest, `claimcheck`, `verifier`; ocena sensowności tabeli (czy okna leżą w tanich strefach).
Na żywo (realne `hours`) — po wydaniu 0.49.0.

## Rollback
`wt drop` przed landem; po landzie `git revert <commit>`.
