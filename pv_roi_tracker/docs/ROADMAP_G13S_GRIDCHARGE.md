# G13s w symulacji taryf + planowanie ładowania magazynu z sieci + najlepszy wariant roczny

Zatwierdzone przez usera 2026-10-26 („Tak”). Jedno wydanie **0.48.0** po E3. Repo: `/config/addons/pv_roi_tracker` (kod w `pv_roi_tracker/pv_roi_tracker/`), release przez skill `release` i checklistę `feedback_pv_roi_release_checklist`.

## Prośba usera
1. Dodać G13s do symulacji taryf, dokładnie sprawdzić stawki i wszystkie składowe.
2. Dla wszystkich taryf strefowych (G12, G12w, G13, G13s) zasymulować, jak ustawić ładowanie magazynu z sieci, żeby wykorzystać najdroższą strefę (ładuj w najtańszej, rozładuj w najdroższej). Pokazać warianty **bez** i **z** optymalizacją magazynu.
3. Jeśli nie ma, dodać sekcję z najlepszym wariantem w skali całego roku.
4. Brak parametrów setupu → pytać.

## Decyzje usera (2026-10-26)
- Magazyn: parametry z zakładki Magazyn (`BatteryConfig`: 5 kWh użytecznych, 2,5 kW, sprawność cyklu 0,92, 7599 zł, 4500 cykli) + **dolny limit SoC 16%** (to rezerwa backupu automatyki JIT). Do cykli służy zakres 16→100% (4,2 kWh).
- **Do 2 cykli na dobę** (np. noc → szczyt poranny, południe → szczyt wieczorny).
- Zakres planowania: **G12, G12w, G13, G13s**. Dynamiczne (dyn_a, dyn_b) bez zmian.
- G13s: oferta „Tanie godziny”, od **2026-01**, opłata handlowa **23,49 zł brutto** (19,10 netto) edytowalna w ustawieniach, `confirmed:false`. „Tanie weekendy” poza zakresem (ewentualnie później).

## Zweryfikowane dane G13s (źródła pobrane i przeczytane 2026-10-26)

### Strefy (Tauron Dystrybucja, taryfa 2026, pkt 2.2.2a; https://www.tauron-dystrybucja.pl/grupa-taryfowa-g13s)
| Strefa | Lato (1.IV–30.IX) | Zima (1.X–31.III) |
|---|---|---|
| dzienna pozaszczytowa (`offpeak`) | 9–17 | 10–15 |
| dzienna szczytowa (`peak`) | 7–9 i 17–21 | 7–10 i 15–21 |
| nocna (`night`) | 21–7 | 21–7 |
Godziny stref takie same w dni robocze i wolne; różnią się stawki. Dzień wolny = sobota, niedziela, święto.

### Dystrybucja zmienna netto zł/kWh (źródło A: `.../wyciag-z-taryfy-td-sa-na-rok-2026-dla-odbiorcow-grup-g.ashx`, pkt 7.3; brutto ze strony TD zgodne ×1,23)
| | offpeak | peak | night |
|---|---|---|---|
| lato, dzień roboczy | 0,1000 | 0,2842 | 0,1094 |
| lato, dzień wolny | 0,0400 | 0,1176 | 0,1094 |
| zima, dzień roboczy | 0,1999 | 0,3332 | 0,1094 |
| zima, dzień wolny | 0,1200 | 0,1960 | 0,1094 |
Stałe: sieciowa 7,38 (1 faza) / 10,86 (3 fazy) zł/mies., abonament 4,56 (rozliczenie miesięczne). Jakościowa 0,0331, OZE 0,0073, kogeneracja 0,0030, mocowa wg progu zużycia: jak pozostałe grupy (pkt 7.4–7.7). Warunki: licznik zdalnego odczytu, rozliczenie miesięczne, grupa od 1.07.2025; powrót do poprzedniej grupy bez opłat, gdy średnia stawka zmienna wzrośnie (pkt 3.1.59). Zmiana grupy raz na 12 miesięcy (strona tauron.pl „Zmiana taryfy”).

### Sprzedaż (NIE ma w taryfie URE `TauronSprzedaz.pdf` ani na https://www.tauron.pl/dla-domu/prad/taryfa-sprzedawcy; tylko oferta handlowa „Tanie godziny”)
PDF https://www.tauron.pl/-/media/Additional/smart/img/sh135-template-lp-g13s/pdf/tanie_godziny_jak_dziala_taryfa_g13s_12_2025.pdf (opłata handlowa 23,49 brutto) oraz `..._serwisant_08_2026.pdf` (29,50 brutto). **Ceny identyczne w obu**, sprawdzone na renderze PDF. Brutto zł/kWh:
| | offpeak | peak | night |
|---|---|---|---|
| lato, dzień roboczy | 0,3383 | 0,8723 | 0,6212 |
| lato, dzień wolny | 0,1390 | 0,3526 | 0,6212 |
| zima, dzień roboczy | 0,6827 | 0,8723 | 0,6089 |
| zima, dzień wolny | 0,4121 | 0,5258 | 0,6089 |
Netto do katalogu: `brutto / 1,23 − 0,005` (akcyza wliczona w cenę: 0,6212 = 0,505 netto = cena max 0,50 + 0,005 akcyzy, jak na fakturach 2025; to wniosek, nie zapis z dokumentu) — katalog dolicza `excise_net` osobno. Okres: PDF 12/2025 powstał 2.01.2026, cennik „trwa 12 miesięcy”, data startu nieznana → od 2026-01 (decyzja usera). Dostępność dla Wrocławia niepotwierdzona (formularz https://moj.tauron.pl/Home/Pages/Prad/Biezaca/Tgzserwisantem).

Bloki, których nie ma: osobnej standardowej sprzedaży G13s; opłata handlowa w taryfie sprzedawcy = 0 zł dla G11/G12/G12w/G13.

## Stan kodu (rozpoznanie)
- `tariff_catalog.py`: `TARIFFS`, `zone_for` (G11/G12/G12w/G13), `rates_per_kwh(tariff, period, zone)`, `fixed_monthly(period, settings)` (opłata handlowa tylko globalna `settings.sales_fee_net`).
- `tariff_sim.py`: `STATIC_TARIFFS`, `ALL_TARIFFS`, `simulate_month` (wariant A).
- `tariff_battery.py`: `make_price_fn`, `simulate_variant_b` (`arbitrage_enabled=False`), `_PEAK_ZONES`.
- `battery_sim.py`: `BatteryConfig`, `simulate_s1(price_fn=...)` (zachłanny: PV → magazyn, rozładowanie przeciw importowi), `throughput_cost_kwh(cfg)`.
- `tariff_job.py`: `compute_payload`, `recommend`, `_attach_variant_b`, `_sum12_b`; UI `static/app.js` (`_TSIM_ALL`, `_renderTsim`, `loadTsimTab`), `index.html` (`tab-tsim`).
- `tariffs/tauron.json`: `zones`, `periods` (energy_net / dist_var_net po taryfach i strefach), `dynamic_sales*`.
- Testy: `uv run --no-project --with pytest --with-requirements requirements.txt pytest tests/test_tariff_*.py` (z katalogu `pv_roi_tracker/`); pełny zestaw przed commitem etapu.

## Etapy

### E1 — Katalog i silnik G13s (wariant A i B)
Kroki:
1. `tauron.json`: `zones.G13s` (okna sezonowe wg tabeli), `energy_net.G13s` i `dist_var_net.G13s` w okresie 2026: 12 kluczy `{summer|winter}_{workday|dayoff}_{offpeak|peak|night}`; wartości z tabel wyżej (energia: brutto/1,23 − 0,005, zaokrąglone do 4 miejsc; udokumentować w `_meta`). Stałe jak pozostałe grupy; `fixed_net.sales_fee_by_tariff.G13s` = 19,10 (23,49/1,23); `confirmed:false` dla G13s (osobna flaga w okresie lub lista `unconfirmed_tariffs`).
2. `tariff_catalog.py`: `'G13s'` w `TARIFFS`; `zone_for` zwraca klucz sezon+dzień+strefa; `fixed_monthly` dodaje opłatę handlową per taryfa (nadpisanie przez `TariffSettings.sales_fee_g13s_net`, gdy ustawione); ustawienia: `tariff_settings_store` + `GET/POST /api/tariff_settings`.
3. `tariff_sim.py`: `STATIC_TARIFFS`/`ALL_TARIFFS` o G13s; `tariff_battery._PEAK_ZONES` o klucze `*_peak`.
4. `tariff_job`, `publisher.py` (sensory MQTT `tariffsim_*` dla G13s: miesiąc i 12 mies.), lista taryf w UI (`_TSIM_ALL`, kolory, etykiety, wykresy).
5. Testy: granice stref lato/zima, święta i weekendy, przełom 31.III/1.IV, DST, stawki 12 kluczy vs tabele, opłata handlowa per taryfa, `confirmed:false` w zgodności.
Weryfikacja: testy tariff_*, pełny zestaw, G13s dla marca 2026 policzony ręcznie na próbce godzin.
Rollback: revert commitu etapu (nic poza katalogiem i listą taryf nie zależy).
Plan etapu z sekcją **Wykonawca** (≥5 edycji → `haiku-worker` w worktree `wt new`, potem `verifier`) piszę na początku etapu: `docs/PLAN_G13S_E1.md`.

### E2 — Planowanie ładowania z sieci (logika, wariant C)
Kroki:
1. Nowy `tariff_grid_charge.py` (czysta logika): dla doby lokalnej, taryfy i `BatteryConfig` DP po SoC (krok ~0,1 kWh, zakres 16%→100% pojemności użytecznej). Wejście godzinowe: cena brutto all-in godziny (`make_price_fn`), eksport PV i import z LTS (`hours`), `rcem` (koszt utraconego eksportu przy ładowaniu z PV). Decyzje godziny: ładuj z PV, ładuj z sieci `g_h` (≤ `power_kw`), rozładuj przeciw importowi (wartość = cena godziny). Straty: sprawność jednokierunkowa `sqrt(roundtrip_eff)`, koszt zużycia `throughput_cost_kwh` na kWh przepustowości. Limit: energia ładowana z sieci ≤ 2 × zakres użyteczny na dobę. Start doby = SoC dolny (16%), koniec ≥ 16% (bez wartości końcowej). Założenie „zna dobę z góry” opisać w UI jako pułap.
2. Wynik: `simulate_variant_c(...)` → `{ym: {taryfa: {savings_pln, grid_charged_kwh, discharged_kwh}}}` dla G12, G12w, G13, G13s; wariant bez optymalizacji = istniejący B (z limitem SoC 16%, jeśli B go jeszcze nie ma, dopisać i uwzględnić w opisie).
3. Plan okien: dla każdej taryfy i każdego (sezon × dzień roboczy/wolny) typowe godziny ładowania z sieci i rozładowania (godziny wybrane w ≥50% dób) + oczekiwana oszczędność doby.
4. Testy: spread zbyt mały → brak ładowania; ładowanie w najtańszej, rozładowanie w najdroższej strefie (przykłady G12w, G13s zima roboczy); limit 2 cykli; SoC ≥ 16%; sprawność; PV ma pierwszeństwo, gdy eksport > 0; brak ujemnej oszczędności względem B.
Weryfikacja: testy, ręczna doba G13s zima roboczy (ładuj noc 21–7 lub 10–15, rozładuj 7–10 i 15–21).
Rollback: revert commitu (nic niepodpięte).
Plan: `docs/PLAN_G13S_E2.md` z sekcją Wykonawca (ryzyko: logika pieniędzy → `sonnet-worker`).

### E3 — Payload, API, MQTT, UI, sekcja „Najlepszy wariant w roku”
Kroki:
1. `compute_payload`: `months[ym][t]['c']`, `last12.gross_c / cheapest_c / saving_vs_g12w_c`, `plans` (okna z E2), `variant_c_note` (założenia: zna dobę z góry, SoC 16%, 2 cykle, brak ujemnych cen); `best_year`: ranking 12 mies. taryfa × wariant (A/B/C, bez dyn niedostępnych) z oszczędnością vs G12w i progiem rekomendacji; ostrzeżenie o zmianie grupy raz na 12 mies. i o `confirmed:false` (G13s).
2. `publisher.py`: sensory MQTT wariantu C i najlepszego wariantu (nazwa, koszt roczny, oszczędność).
3. UI (`impeccable`, playwright-ha): przełącznik wariantu A/B/C, karta „Jak ustawić ładowanie” per taryfa strefowa (okna sezon × dzień, oszczędność doby, roczna z/bez optymalizacji), sekcja „Najlepszy wariant w skali roku” (tabela rankingu + zdanie rekomendacji), oznaczenie G13s „cennik niepotwierdzony”. Pole opłaty handlowej G13s w Ustawieniach.
4. Testy: `test_tariff_job`, `test_web_tariff_sim`, `test_publisher`, kontrakt `/api/tariff_sim`.
Weryfikacja: testy, screenshot i `browser_console_messages` przez ingress.
Rollback: revert commitu.

### Wydanie 0.48.0 (po E3)
`simplify` → `code-review` + `silent-failure-hunter` + `pr-test-analyzer` + security (general-purpose) + second-opinion (pomiar 5/10) → `release` (bump `pv_roi_tracker/config.yaml` i `__version__`, dual-push, release published) → weryfikacja na żywo `/api/tariff_sim` (G13s widoczne, C ≥ B, `best_year`). Nie zapomnieć: wynik pomiaru G12w vs faktury nie może się pogorszyć (±3%).

## Ryzyka i otwarte punkty
- Ceny G13s niepotwierdzone w dokumencie urzędowym; dostępność dla Wrocławia nieznana → `confirmed:false`, napis w UI.
- 2025 dla G13s: brak (dystrybucja G13s od 1.07.2025, ale cen sprzedaży 2025 nie mamy) → `available:false` przed 2026-01.
- Wariant C to pułap (pełna znajomość doby); realna automatyka (Heiko/JIT) da mniej.
- „Tanie weekendy” (inne stawki, tylko roboczy szczyt 1,2727) — poza zakresem.
- Kontekst: sesja startowa miała ok. 155 tys. tokenów; kontynuacja po `/clear` od E1.

## Status
- 2026-10-26: plan zatwierdzony, dane zweryfikowane (E0).
- 2026-10-26: **E1 ZROBIONE** (plan `PLAN_G13S_E1.md`, paczki W1 backend + W2 UI, haiku-worker, verifier ACCEPT obie). Commity: `54306e5` (E1a backend), `82552a8` (E1b UI). Weryfikacja: pełny pytest 908 passed/27 skipped; G13s marzec 2026 policzone ręcznie (próbka 4 godzin: 3,0307 zł zgodne z kodem; opłaty stałe 58,57 zł = 10,86+4,56+24,05+19,10); 2025-12 `available:false`; `confirmed:false` tylko dla G13s; reconciliation G12w bez zmian. Odstępstwa nieblokujące: dostępność miesięczna w `simulate_month` vs dzienna w hourly/`make_price_fn`; kWh G13s przed 2026-01 w `cost_by_zone` trafiają do `missing_kwh`. Niezweryfikowane: render UI (Taryfa, mapa stref) z realnymi danymi G13s – Playwright po E3/wydaniu; stawki brutto z PDF oferty sprawdzone w E0, nie ponownie.
- 2026-10-26: **E2 ZROBIONE** (plan `PLAN_G13S_E2.md`, sonnet-worker, 1. REJECT verifiera: C < B na losowych danych → poprawka: dziennie C = max(DP, polityka B z resetem SoC), 2. próba sprawdzona przeze mnie). Commit `f846524`. Weryfikacja: pełny pytest 925 passed/27 skipped; 700 dób × 4 taryfy ≈ 1,4 s; ręczna doba G13s zima roboczy: ładowanie 0–2 (noc), rozładowanie 7–10 i 15–17, G13s lato: ładowanie noc + 9–11, rozładowanie 7–9 i 17–21 (zgodne z ROADMAP). Odstępstwa: `_TIE_BREAK=2e-3` w bisekcji λ limitu cykli; dyskretyzacja 0,2 kWh; C startuje doby z floor (B w payloadzie przenosi SoC → opisać w `variant_c_note`); `simulate_variant_b(soc_floor=0.0)` domyślnie 0 — E3 przekazuje 0,16; `days_total` = doby z choć jedną ceną; test DST przez `simulate_variant_c` tylko „bez wyjątku” (klucze godzin nie odróżniają 25. godziny). Niezweryfikowane: wydajność i C ≥ B na realnych `hours` z LTS (dane na hoście HA) — po wydaniu.
- 2026-10-26: **E3 ZROBIONE** (plan `PLAN_G13S_E3.md`; W1 backend sonnet-worker + W2 UI haiku-worker, verifier ACCEPT obie). Commity: `74c5e0c` (E3a payload/recommend/MQTT), `cde1f1c` (E3b UI). Weryfikacja: pełny pytest 941 passed/27 skipped; skrypt na syntetycznych `hours` (2026): plan G13s zima roboczy ładowanie 0–2, rozładowanie 7–10 i 15–19, `best_year`/`recommend` zgodne z kontraktem. Odstępstwa: B w payloadzie liczone z SoC 16% (wartości B się zmieniły); `best_year` bez `with_b` zawiera tylko wiersze A; ostrzeżenie o niepotwierdzonym cenniku jedno na taryfę. NIEBLOKUJĄCE verifiera: brak guarda null w `Math.round(r.gross)` (app.js ~4753), `recommendation.best_year.saving_pln` nie wyświetlane. Niezweryfikowane: render UI (C, karta ładowania, best_year) w przeglądarce, C ≥ B i wydajność na realnych `hours` z LTS — po wydaniu na hoście HA.
- **Następny krok: wydanie 0.48.0** (simplify → code-review + silent-failure-hunter + pr-test-analyzer + security + second-opinion 5/10 → release → weryfikacja na żywo).
