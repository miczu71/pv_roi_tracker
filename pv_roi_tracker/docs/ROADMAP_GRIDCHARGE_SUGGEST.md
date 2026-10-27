# pv_roi_tracker — sugestie ładowania magazynu z sieci + objaśnienie wariantów A/B/C

Repo `/config/addons/pv_roi_tracker` (kod `pv_roi_tracker/pv_roi_tracker/`), main, kontynuacja 0.48.0.
Pierwszy krok implementacji: skopiować ten plan do `pv_roi_tracker/docs/ROADMAP_GRIDCHARGE_SUGGEST.md`
i dopisać odsyłacz w sekcji Status `docs/ROADMAP_G13S_GRIDCHARGE.md` (otwarty błąd C < B przechodzi tutaj).

## Context
Karta „Jak ustawić ładowanie z sieci” (0.48.0) pokazuje pustkę w prawie wszystkich komórkach, a warianty
A/B/C nie są wytłumaczone. Diagnoza na żywo (`/api/tariff_sim`, 2026-10-27): z 16 komórek tylko G12w i G13s
zima roboczy mają godzinę (ładuj 0:00), rozładowania nigdzie. Przyczyny w `tariff_grid_charge.py:205-241`:
1. próg „godzina w ≥50% wszystkich dób grupy” — DP rozkłada rozładowanie na różne godziny, nic nie przechodzi;
2. doby wygrane przez politykę B mają puste godziny, a liczą się do mianownika (lato prawie całe puste);
3. ładowanie z sieci często się nie opłaca, a UI pokazuje wtedy „—” zamiast „nie ładuj”;
4. otwarty błąd C < B latem — hipoteza: C resetuje SoC do 16% o północy (B przenosi SoC między dobami).

## Decyzje usera (wywiad 2026-10-27)
- Karta ma dawać **sugestie**, jak przy danej taryfie ustawić ładowanie magazynu, by dalej obniżyć koszt.
  Użytkownik sam przenosi je do automatyki.
- **JIT w HA zostaje bez zmian**; pv_roi niczego w nim nie steruje ani nie publikuje dla niego (brak nowych MQTT).
- Przyjęte domyślnie (bez dalszych pytań): jedna sugestia na sezon × typ dnia; jawny komunikat „nie opłaca się”
  z kwotą; objaśnienie A/B/C w UI.

## Kto, co, kiedy sukces
- Korzysta: user, czytając zakładkę Taryfy → Symulacja; decyzje o zmianie JIT podejmuje sam.
- Automatycznie: przeliczenie przy każdym `compute_payload`; zero zmian w HA.
- Za zgodą: wydanie add-onu (jedno po E3, w ramach łańcucha wydania).
- Sukces: każda komórka karty ma konkretną sugestię albo „nie ładuj z sieci — zysk < X zł/rok”;
  `gross_c ≤ gross_b` dla każdej taryfy na żywo; legenda A/B/C zrozumiała bez czytania kodu.

## Etapy
### E1 — C ≥ B (poprawka logiki, ryzyko: pieniądze → sonnet-worker)
- Diagnoza najpierw: test na syntetycznej dobie letniej (PV w południe, pobór wieczorem i rano) B ciągłe vs C.
- Jeśli hipoteza potwierdzona: `simulate_variant_c` przenosi SoC końca doby jako start następnej
  (DP z SoC startowym i wartością energii końcowej = cena porannego importu × sprawność), polityka B w porównaniu
  też z przeniesionym SoC. Usunąć zdanie o resecie z `variant_c_note` (`tariff_job.py:256`).
- Test regresji: C ≥ B na danych letnich i zimowych; pełny pytest.
- Rollback: revert commitu.

### E2 — sugestie ustawień (backend)
- W `tariff_grid_charge.py` zamiast głosowania godzin: dla każdej taryfy i grupy (sezon × dzień) przegląd
  kandydatów prostego harmonogramu w składni, którą da się przenieść do JIT: okno ładowania z sieci (strefa
  tania, np. noc 22–6 / G13s 10–15) + cel SoC (krok 10%), rozładowanie = przeciw poborowi w strefach drogich.
  Każdy kandydat symulowany na wszystkich dobach grupy (z ciągłym SoC jak B); wybór najlepszego netto
  (z kosztem zużycia `throughput_cost_kwh`).
- Wynik `plans[t][grupa]`: `charge_window`, `target_soc`, `discharge_zones`, `saving_vs_b_pln_year`,
  `worth_it` (próg np. 20 zł/rok), plus „pułap C” dla porównania.
- Testy: duży spread → okno w tańszej strefie i cel > 16%; mały spread → `worth_it=false`; G13s zima roboczy.
- Rollback: revert.

### E3 — UI (impeccable, playwright-ha)
- Karta: każda komórka „Ładuj z sieci 22–6 do 70%, oszczędność ~N zł/rok” albo „Nie ładuj z sieci — zysk < X zł/rok”;
  linia „pułap C (pełna wiedza o dobie): Y zł/rok”.
- Legenda A/B/C przy przełączniku wariantu (3 krótkie zdania: A bez magazynu; B magazyn jak dziś — tylko z PV;
  C magazyn + ładowanie z sieci wg sugestii). Poprawić guard null `Math.round(r.gross)` (app.js ~4753).
- Rollback: revert.

### Wydanie 0.49.0
simplify → code-review + silent-failure-hunter + pr-test-analyzer + security → release → weryfikacja na żywo.

## Weryfikacja
`uv run --no-project --with pytest --with-requirements requirements.txt pytest` (z `pv_roi_tracker/`);
na żywo `/api/tariff_sim` przez ingress (`gross_c ≤ gross_b`, żadnej komórki bez treści), screenshot
playwright-ha do `/config/playwright/` + `browser_console_messages` bez błędów.

## Status
- 2026-10-27: **E1 ZROBIONE** (plan `PLAN_GRIDCHARGE_E1.md`, sonnet-worker, verifier APPROVE). Commit `4f51b7a`.
  Diagnoza potwierdzona: C < B w 9/9 przypadków syntetycznych (np. G12 B=48,94 C=41,85) — reset SoC co dobę. Poprawka:
  `solve_day(soc0_kwh, end_value)` → `end_soc_kwh`, `simulate_variant_c` przenosi SoC, miesięcznie lepszy z DP i ciągłego B.
  Weryfikacja: pełny pytest 962 passed/27 skipped (941 + 21 nowych), po poprawce C ≥ B w 9/9 (np. G12 B=48,94 C=57,07),
  test wydajności OK. NIEBLOKUJĄCE verifiera: (1) `end_value` mnożony przez eff dwa razy (≈ cena×eff², zachowawczo);
  (2) wczesne wyjście „mały spread” przy `soc0>0` nie rozładowuje energii z poprzedniej doby; (3) test C ≥ B tylko
  jednomiesięczny (brak wielomiesięcznego); (4) `plans` z DP mogą się rozjechać z miesiącem wybranym z B — do E2.
  Niezweryfikowane: C ≥ B na realnych `hours` z LTS (dane na hoście HA) — po wydaniu 0.49.0.
- 2026-10-27: **E2 ZROBIONE** (plan `PLAN_GRIDCHARGE_E2.md`, sonnet-worker, verifier APPROVE). Commit `1b673ab`.
  `suggest_plans` + `_simulate_schedule` (konwencje `simulate_s1`), scalone w `plans[t][gk]` obok starych pól.
  pytest 969 passed/27 skipped (+7), `suggest_plans` 365 dób × 4 taryfy 0,28 s.
  **Ustalenie dla E3:** przy domyślnym `BatteryConfig` (koszt przerobu ≈0,34 zł/kWh) prawie wszystkie grupy `worth_it=False`
  (tylko G12w zima roboczy ≈52 zł/rok); przy ≈0,09 zł/kWh okna 22–6 (G13s 21–7), do ≈200 zł/rok. Testy używają
  `CHEAP_CFG` (module_price 2000). Niezweryfikowane: jaki koszt przerobu ma realna konfiguracja na żywo — sprawdzić na
  `/api/tariff_sim` na początku E3; UI musi sensownie pokazać „nie ładuj z sieci — zysk < 20 zł/rok”.
  NIEBLOKUJĄCE verifiera: test równoważności tylko dla kandydata `None`; remis etapu 1 (wcześniejszy start, wyższy próg)
  i przybliżone skalowanie (strefy rozładowania z dni innych grup) do docstringu; importy w testach na górę.
- 2026-10-27: **E3 ZROBIONE** (bez workera, edycje własne). Decyzja usera: koszt przerobu z konfiguracji; na żywo ustawiono
  `lifetime_cycles` 6000 (było 4500; koszt 0,338 → 0,253 zł/kWh, magazyn Huawei LUNA2000 5 kWh, cena modułu 7599 zł).
  Backend: `simulate_variant_c` zwraca tylko `monthly`, stare pola `plans` (głosowanie godzin) usunięte, `payload['plans']` = wynik `suggest_plans`.
  UI: komórki „Ładuj z sieci HH–HH do N%” + rozładowanie + zysk/rok albo „Nie ładuj z sieci — zysk ok. X zł/rok”, linia pułapu C,
  legenda A/B/C przy przełączniku, guard null `r.gross`. pytest 968 passed/27 skipped. Weryfikacja UI na żywo — po wydaniu 0.49.0.
- Następny: wydanie 0.49.0 (simplify → code-review + silent-failure-hunter + pr-test-analyzer + security → release → weryfikacja na żywo).
