# E1 — wariant C ≥ B (przenoszenie SoC między dobami)

Roadmapa: `ROADMAP_GRIDCHARGE_SUGGEST.md`. Ryzyko: logika pieniędzy → `sonnet-worker`.

## Diagnoza (potwierdzona 2026-10-27)
Skrypt syntetyczny (lipiec, PV 10–15, pobór poza południem, co 3. doba bez wieczornego poboru), G12/G12w/G13 × 3 ziarna:
C < B w 9/9 przypadków (np. G12 B=48,94 C=41,85). Przyczyna: `simulate_variant_c` liczy każdą dobę od SoC = dolny próg,
bez wartości energii na koniec doby; polityka B w payloadzie (`simulate_variant_b`) przenosi SoC między dobami.
Energia z PV zostawiona na noc/rano jest więc dla C stracona.

## Decyzja projektowa
1. `solve_day` dostaje `soc0_kwh` (energia w zakresie użytecznym na starcie doby) i `end_value` (zł za 1 kWh
   zmagazynowanej na koniec doby, wartość końcowa = `end_value * s * step * eff`); zwraca też `end_soc_kwh`.
2. `simulate_variant_c` idzie po dobach chronologicznie i przenosi `end_soc_kwh` na start następnej doby, gdy doby są
   kolejne kalendarzowo (luka w danych = reset do 0). `end_value` = średnia 3 najwyższych cen następnej doby (jeśli
   jest i ma ceny) razy sprawność jednokierunkowa `sqrt(roundtrip_eff)`; brak następnej doby = 0.
3. Gwarancja C ≥ B: dla każdej taryfy `simulate_variant_c` liczy też ciągłe B (`battery_sim.simulate_s1` na całym
   zakresie kluczy, `cfg_b` jak dotąd, `price_fn` taryfy) i **dla każdego miesiąca bierze lepszy** wynik (DP vs B);
   gdy wygrywa B: `savings_pln`=B, `pv_charged_kwh`=`charged_kwh` B, `grid_charged_kwh`=0, `wear_pln`=0,
   `discharged_kwh` B. Zastępuje to dotychczasowe `max(DP, B z resetem)` na dobę.
4. Plany (`plans`) nadal z doby DP (E2 je zastąpi): `charge_hours`/`discharge_hours` z DP-u nawet gdy miesiąc
   wygrał B (plan opisuje strategię planowania, nie politykę B).

## Wykonawca

WORKTREE: ścieżka z `wt new /config/addons/pv_roi_tracker gridcharge-e1` (podaje rodzic). Katalog pracy kodu:
`<WORKTREE>/pv_roi_tracker/` (tam `pytest.ini`, `requirements.txt`; pakiet w `pv_roi_tracker/pv_roi_tracker/`).

Pliki i zmiany:
1. `pv_roi_tracker/pv_roi_tracker/tariff_grid_charge.py`
   - `_run_dp(hours, n, lam, s0=0, end_bonus=None)`: start `best[s0]=0` zamiast `best[0]`; wybór końcowego stanu
     `max(range(n+1), key=lambda i: (best[i] + (end_bonus[i] if end_bonus else 0.0), -i))`; `total` z bonusem;
     rekonstrukcja ścieżki bez zmian (idzie od wybranego `s`). Porównanie `grid_stored` bez zmian.
   - `solve_day(..., soc0_kwh=0.0, end_value=0.0)`: `s0 = min(n, max(0, round(soc0_kwh / step)))`;
     `end_bonus = [end_value * s * step * eff for s in range(n+1)]`; przekaż do wszystkich wywołań `_run_dp`
     (też w bisekcji). Wynik dostaje `end_soc_kwh` (stan po ostatniej akcji ścieżki * step; policz z `s0` i delt ścieżki).
     `_zero_result()` dostaje `'end_soc_kwh': 0.0`; wczesne `return _zero_result()` ustawiają `end_soc_kwh` na
     `s0 * step` (bez akcji energia zostaje) — dla `soc0_kwh=0` bez zmiany zachowania.
     Uwaga: wczesne wyjścia „brak importu / mały spread” przy `end_value>0` mogą pominąć wartość energii na koniec — to
     akceptowane (zwracamy zero akcji), ale `end_soc_kwh=s0*step`.
   - `simulate_variant_c`: pętla po dobach per taryfa z przenoszeniem `soc` i `end_value` jak w „Decyzja projektowa” p.2;
     usuń blok `b_rows = simulate_s1(... keys=keys ...)` i podmianę `res` na dobę; po pętli dla taryfy policz ciągłe B
     (`simulate_s1(hours, cfg_b, {}, rcem_by_ym, keys=<wszystkie klucze zakresu, posortowane>, price_fn=price_fn)`)
     i podmień miesiące, w których B ≥ DP (p.3). Zachowaj pola wiersza: `savings_pln, wear_pln, grid_charged_kwh,
     pv_charged_kwh, discharged_kwh`. Zaktualizuj docstring modułu i funkcji (usuń „C >= B z resetem SoC co dobę”).
2. `pv_roi_tracker/pv_roi_tracker/tariff_job.py` (~linia 256): w `variant_c_note` usuń zdanie
   „C startuje każdą dobę z dolnego SoC (B przenosi SoC między dobami)”; dodaj: „SoC przenoszony między dobami;
   w miesiącu liczony lepszy z planowania i polityki B”.
3. `pv_roi_tracker/tests/test_tariff_grid_charge.py`:
   - test `solve_day` z `soc0_kwh>0`: energia startowa jest rozładowywana w drogiej godzinie bez ładowania;
   - test `end_value`: wysoka wartość końcowa zostawia energię w magazynie (`end_soc_kwh>0`), zerowa nie;
   - `simulate_variant_c` C ≥ B miesięcznie na syntetycznych danych letnich (generator jak w diagnozie: PV 10–15
     eksport 0,5–2,5 kWh, poza południem pobór 0,2–1,2 kWh, co 3. doba bez poboru od 16:00; lipiec 2026, 20 dób;
     taryfy G12, G12w, G13; ziarna 1–3) i zimowych (styczeń, eksport mały) — porównanie z
     `tariff_battery.simulate_variant_b(..., soc_floor=gc.SOC_FLOOR)`, tolerancja 1e-6;
   - przeredaguj istniejące `test_c_nie_gorsze_niz_b_z_resetem_losowe` i `test_j_c_nie_gorsze_niz_b` tak, by
     porównywały z ciągłym B (nie z B z resetem), jeśli dotychczasowa asercja przestaje być prawdziwa.

Komendy testu (z `<WORKTREE>/pv_roi_tracker/`):
`uv run --no-project --with pytest --with-requirements requirements.txt pytest tests/test_tariff_grid_charge.py tests/test_tariff_job.py -q`
a na końcu pełny zestaw: `uv run --no-project --with pytest --with-requirements requirements.txt pytest -q`.

DONE: nowe testy przechodzą; test wydajności `test_k_wydajnosc` przechodzi; pełny pytest zielony (poprzednio
941 passed/27 skipped + nowe); na danych syntetycznych z diagnozy C ≥ B dla wszystkich 9 przypadków; raport z
wynikami pytest i tabelą B/C z 9 przypadków.

NIE RÓB: nie ruszaj `battery_sim.py`, `tariff_battery.py`, `tariff_catalog.py`, UI (`static/*`), `publisher.py`,
`config.yaml`, plików poza trzema powyżej; nie zmieniaj formatu `plans`; nie commituj; nie zmieniaj sygnatur
publicznych poza dodaniem argumentów z wartościami domyślnymi; nie dodawaj zależności.

## Weryfikacja (rodzic)
`git diff` w worktree, pytest pełny, skrypt diagnostyczny
`/tmp/claude-950/-home-claude/abd2af48-7ce4-4d20-8960-1bd55f82a06e/scratchpad/diag_c.py` (C ≥ B), potem `verifier`.
Na żywo C vs B na realnych `hours` — po wydaniu 0.49.0.

## Rollback
`wt drop` przed landem; po landzie `git revert <commit>`.
