# Symulacja taryf Tauron (G11 / G12 / G12w / G13 / dynamiczna): wspólny silnik w pv_roi_tracker

## Context
User (Tauron, Miasto A, od 2025 G12w) chce zdecydować, czy zmienić taryfę. Dziś porównanie jest rozbite i częściowo błędne:
- HA (`/config/packages/energy_simulation.yaml`) liczy koszty na żywo przez liczniki narastające (utility_meter) i automatyzacje.
- Add-on `pv_roi_tracker` 0.45.12 tylko publikuje stawki z faktur (MQTT `sensor.pv_roi_tracker_pv_rate_*`) i czyta gotowe koszty G12w i dynamicznej z LTS.

Decyzje z wywiadu (2026-10-21):
1. **Cel:** decyzja o zmianie taryfy. Pełne koszty roczne (zmienne i stałe) wstecz od 12.2024 i na bieżąco, plus rekomendacja.
2. **Silnik:** add-on pv_roi. Katalog taryf, kalkulacja godzinowa z LTS, backfill, wyniki do HA przez MQTT.
3. **Stawki:** cenniki Tauron Sprzedaż i Tauron Dystrybucja (URE) w pliku JSON na każdy rok, korekta w UI. G12w nadal z faktur, a rozjazd faktura–cennik służy jako test cennika.
4. **Profil zużycia:** dwa warianty.
   - A: ten sam godzinowy pobór wyceniony w każdej taryfie.
   - B: `battery_sim` przeliczony pod strefy danej taryfy.
5. **Sensory HA:** wycofanie duplikatów po ~1 miesiącu pracy równoległej. Zostaje bieżąca strefa i cena G12w dla baterii.

Kto korzysta: user (decyzja) przez zakładkę Taryfa i dashboard HA.
Co dzieje się automatycznie: przeliczenie dzienne i publikacja MQTT.
Za zgodą: zmiana taryfy u sprzedawcy oraz krok sprzątania HA (E5).
Sukces:
- symulacja G12w trafia w faktury z dokładnością ±3% na miesiąc,
- jest tabela 12 miesięcy × 5 taryf × wariant A/B,
- jest jedna rekomendacja.

## Stan obecny (przegląd)

| Taryfa | Gdzie | Stawki | Strefy | Problem |
|---|---|---|---|---|
| G11 | `energy_simulation.yaml:224` + utility_meter (UI) | 0.98 zł na sztywno | — | brak opłat stałych, stawka bez źródła |
| G12 | `:95-157`, `utility_meter.energy_g12` | stawki G12w | 6–13, 15–22 codziennie | Tauron G12 ma inne stawki i opłaty |
| G12w | `:159-239` + add-on `battery_sim.is_peak` | faktury eBOK | jw., weekendy/święta = dolina | strefy zdublowane (workday vs `polish_holidays`) |
| G13 | brak | — | — | do zbudowania (3 strefy, lato/zima) |
| Dynamiczna | `:4-60` + automatyzacja 15 min → `input_number` | RCE + 0.0892 na sztywno + dystrybucja wg „compass” | 15 min | dryf akumulacji, dystrybucja do weryfikacji |

Opłaty stałe są takie same dla każdej taryfy (4.56/10.86/24.05 netto, wpisane 3×).
`najtansza_taryfa_w_miesiacu` porównuje tylko koszty zmienne.
Na żywo 21.10, miesiąc do dziś, w zł:
- G11: 179.74 (tylko koszt zmienny)
- G12: 188.13
- G12w: 172.04
- dynamiczna: 236.05

## Etapy (repo `/config/addons/pv_roi_tracker`, gałąź main; ROADMAP → `docs/ROADMAP_TARIFFS.md`)

**E0 – Cennik (fundament)**
- docs-scout zbiera dane z oficjalnych PDF-ów Tauron Sprzedaż i Tauron Dystrybucja, z URL-ami źródeł:
  - taryfy na 2025 i 2026: energia i dystrybucja zmienna per strefa, opłaty jakościowa / OZE / kogeneracyjna / mocowa (progi zużycia), abonament i stała sieciowa per grupa;
  - godziny stref G12/G12w/G13 (lato/zima);
  - warunki oferty dynamicznej Tauron: marża, opłata handlowa, dystrybucja przy cenie dynamicznej.
- Plik `pv_roi_tracker/tariffs/tauron.json` z polem `valid_from`.
- Rollback: usunięcie pliku.

**E1 – Silnik (tdd, wariant A)**
- `tariff_catalog.py`: wczytanie JSON, override z `tariff_config.json`, funkcja strefy dla godziny. Święta z istniejącego `battery_sim.polish_holidays`.
- `tariff_sim.py`: z godzinowego poboru (`live_reader.get_hourly_energy`, `sensor.power_meter_consumption`) i RCE godzinowego (cache `rce_hourly.py`) liczy miesięczny koszt każdej taryfy, z rozbiciem: energia / dystrybucja / opłaty / stałe / VAT.
- Testy w `tests/test_tariff_sim.py`: granice stref, święta, przełom lato/zima, ujemne RCE, VAT.
- Rollback: revert commitu (nic nie jest jeszcze podpięte).

**E2 – Backfill, API, MQTT**
- `main.py`: przeliczenie od 2024-12, potem codziennie. Wyniki do `/data/tariff_sim.json`, endpoint `GET /api/tariff_sim`.
- MQTT (`publisher.py`): koszt bieżącego miesiąca per taryfa, suma 12 miesięcy per taryfa, najtańsza taryfa, oszczędność roczna vs G12w.
- Kontrola: G12w z symulacji vs faktury, raport rozjazdu w API.
- Rollback: revert, sensory MQTT znikają (retain=false lub czyszczenie topiców).

**E3 – Wariant B**
- `battery_sim` dostaje funkcję strefy i ceny jako parametr zamiast sztywnego `is_peak`. Symulacja S1 per taryfa. Dynamiczna już ma S2.
- Wynik B obok A w `tariff_sim.json`.
- Rollback: revert.

**E4 – UI zakładki Taryfa**
- Tabela 12 miesięcy × 5 taryf (A i B), rozbicie kosztów, rekomendacja z progiem `input_number.prog_rekomendacji_taryfy` (lub odpowiednikiem w add-onie), edycja cennika.
- Skill `impeccable`, weryfikacja Playwright (screenshot i konsola).
- Rollback: revert.

**Wydanie (jedno, po E4):**
- `simplify` → `code-review` + `silent-failure-hunter` + `pr-test-analyzer` + security + second-opinion (pomiar 5/10).
- `release`: bump obu plików wersji, także `pv_roi_tracker/config.yaml`. Przy okazji usunięcie martwej migracji `drop_bounce_corrections` (notatka `project_pv_roi_remove_bounce_migration`).
- Weryfikacja na żywo.

**E5 – Sprzątanie HA (~2026-11-25, po miesiącu równoległym)**
- Porównanie sensorów add-onu ze starymi sensorami HA.
- Backup automatyzacji i dashboardu (bez pełnego snapshotu).
- `ha_search` konsumentów.
- Usunięcie z `energy_simulation.yaml` symulacji G11, G12 i dynamicznej, akumulacji `input_number` oraz automatyzacji: `taryfa_g12`, `obliczanie_kosztu_symulacji_taryfy_dynamicznej`, utility_meter dla G12 i 15-min.
- Zostają `power_tauron_g12w_current_*` oraz sensory używane przez `battery_*`.
- Dashboard i `podsumowanie_miesieczne_taryf` przełączone na sensory add-onu (ha-mcp i `ha-deploy`).
- Rollback: przywrócenie YAML z gita i dashboardu z backupu.
- Kontrola: przed E5 pokazuję listę usuwanych encji.

## Weryfikacja
- `uv run pytest` w add-onie (testy E1–E3).
- `/api/tariff_sim`: G12w vs faktury ±3% na miesiąc od 2025-01.
- Encje MQTT w HA (`ha_get_state`), zakładka UI w Playwright.
- Po E5: `config_check`, brak encji `unavailable` na dashboardzie.

## Status (2026-10-21)

- **E0 ZROBIONE** (commity 6f645ad, e10a722 + ten): `pv_roi_tracker/tariffs/tauron.json`. G12w 2026 z cennika = 1,224/0,625 zł brutto vs 1,23/0,63 z faktur, stałe 39,47 zł zgodne.
- **Poprawka poza planem** (c5a4353, wdrożona i zweryfikowana na żywo): `energy_simulation.yaml` używał stawek dystrybucji G14dynamic z 2025, teraz 2026 (0,0224/0,0893/0,3881/2,3756 netto).
- **Luki w cenniku:**
  - Sprzedaż 1.01–30.09.2025: nie ustalono. PDF z pkt 5.1a: `https://bip.ure.gov.pl/download/3/18556/TAURONSprzedazSpzoo.pdf` (czytać narzędziem Read, strony z pkt 5).
  - Gospodarstwa płaciły w 2025 zamrożone 500 zł/MWh netto; ceny stref w 2025 to tylko ceny odniesienia. Nieustalone, czy zamrożenie dotyczyło stref czy średniej.
  - Dynamiczna: baza to TGE RDN Fixing I (nie RCE), marża 0,1097 zł/kWh brutto (0,0892 netto, wyliczone). Podłoga i ujemne ceny niepotwierdzone. G14dynamic należy do osobnej oferty ze stałą ceną sprzedaży.
- **Decyzje z wywiadu (2026-10-21, rozstrzygnięte):**
  1. Dynamiczna w DWÓCH wariantach: `dyn_a` (RDN + marża + dystrybucja G12w) i `dyn_b` (stała cena Tauron + G14dynamic).
  2. Ceny RDN: Energy-Charts API (`bzn=PL`, EUR/MWh) + kurs NBP. Strefy S1–S4 historycznie: PSE `pdgsz`.
  3. 2025: zamrożone 0,50 zł/kWh netto we wszystkich strefach (wpisane w `tauron.json`).
- **Plan E1 (zrealizowany):** `docs/PLAN_TARIFFS_E1.md` (sekcja Wykonawca → `sonnet-worker` w worktree, potem `verifier`).
- **Następny krok:** uzupełnić PARAMETRY (cena stała `dyn_b` z docs-scout), `wt new`, worker, verifier, commit.

## Weryfikacja cenników Tauron (2026-10-21, user: „bez założeń”)
Oficjalne cenniki pobrane ze strony oferty po wskazaniu lokalizacji (Miasto A); linki do PDF dostępne dopiero po wyborze lokalizacji.
- **„Prąd ze zmienną dystrybucją”** (`.../produkty/g14dynamic/ts/cennik/ee-gd-bsc-b-dynd-ts-0.ashx`): cena energii **0,5020 zł/kWh netto (0,6175 brutto), jednakowa w S1–S4**, od 2026-01-01 na czas nieokreślony, brak opłaty handlowej. Założenie „jak G11 (0,4970)” było błędne. Nadwyżki prosumenta: „szczegółowe zasady określa Aneks do Umowy” (nieznane).
- **„Prąd z Ceną Dynamiczną”** (`.../2024/08-2024/dynamiczne/ts/EE-GD-CDzcb-Bezpieczny-TS-0.ashx`), od **2026-04-01**: cena = RDN Fixing I (godzina lub kwadrans) + SC **0,0892 netto / 0,1097 brutto** (wszystkie strefy). Rozliczenie: średnioważona cena miesięczna Σ(cena×kWh)/ΣkWh, **cena minimalna 0,0050 netto dotyczy średniej miesięcznej**, nie godzin. Brak notowania → analogiczny okres poprzedniego tygodnia. Wersje cennika sprzed 2026-04-01: nieznane (brak archiwum na stronie) → `dyn_a` liczone tylko od 2026-04.
- Skutki dla E1: `dyn_a` tylko od 2026-04, `dyn_b` tylko od 2026-01; wcześniej `available:false`.
- **E1 ZROBIONE** (commit 42134bc, 2026-10-21): `tariff_catalog.py`, `price_sources.py` (Energy-Charts RDN + NBP + PSE pdgsz + PriceCache), `tariff_sim.py` (G11/G12/G12w/G13/dyn_a/dyn_b), 3 pliki testów. Wynik: 58 testów tariff_*, pełny zestaw 753 passed/27 skipped, verifier ACCEPT (bez blokujących). Nic nie podpięte do main/web/publisher.
  - NIEBLOKUJĄCE z verifiera (do E2): test DST słaby (jeden klucz bez logiki), test 1.11 rozróżnia tylko 2024, dyn_a nie uwzględnia `dynamic_sales.confirmed` (dyn_b tak; oba true), pola `margin_net_per_kwh`/`margin_gross_per_kwh` w kodzie.
  - NIEZWERYFIKOWANE na żywo: schematy Energy-Charts i PSE tylko na atrapach — pierwszy krok E2. `dtime` w `pdgsz` = początek godziny (sprawdzone: 24 wiersze 00:00–23:00).
- Uwaga: testy add-onu uruchamiać `uv run --no-project --with pytest --with-requirements requirements.txt pytest …` (zwykłe `uv run pytest` nie działa).
- Do sprawdzenia w E2: czy `dtime` z PSE `pdgsz` to początek godziny (worker założył początek).
- **E2 ZROBIONE** (commity a39d12a plan, 6b59492, 2026-10-21): `tariff_job.py` (compute_payload, ranking 12 mies., raport rozjazdu G12w vs faktury ±3%), `tariff_sim_job` (cron 05:30 + wątek startowy), `GET /api/tariff_sim`, 14 sensorów MQTT `tariffsim_*`; poprawki z E1 (dyn_a.confirmed, `is_active` w filtrze PSE). 774 passed/27 skipped, verifier ACCEPT.
  - Schematy Energy-Charts, PSE i NBP sprawdzone na żywo (zgodne z kodem E1; PSE strony po 100 wierszy, kod idzie po `nextLink`).
  - Odstępstwo: katalog zaczyna się od 2025-01, więc `from` = 2025-01 (nie 2024-12).
  - NIEBLOKUJĄCE: wątek `tariff-sim-boot` równolegle z `battery-sim-boot` (przy starcie może czytać starszy cache, korekta 05:30); wiring main/cron bez testu. Uwaga verifiera o DST jesienią nietrafiona (`get_hourly_energy` sumuje kolizje).
  - NIEZWERYFIKOWANE na żywo: rozjazd G12w vs faktury i kalibracja `sales_fee_net` (dane tylko na hoście HA) — po wydaniu przez `/api/tariff_sim`.
- **E3 ZROBIONE** (commity 4a465fe plan, 2ae6786, 2026-10-21): `battery_sim.simulate_s1(price_fn=...)` (bez `price_fn` bez zmian), `tariff_battery.py` (`make_price_fn`, `simulate_variant_b`, arbitraż wyłączony), `compute_payload(hours, battery_cfg, rcem_by_ym)` → `months[ym][t]['b']`, `last12.gross_b/cheapest_b/saving_vs_g12w_b`, `current_month.gross_b`, `variant_b_note`. 791 passed/27 skipped, verifier ACCEPT (bez blokujących). Bez sensorów MQTT dla B (UI w E4).
  - NIEBLOKUJĄCE (do raportu wydania): `make_price_fn` cicho pomija godziny przy `LookupError/ValueError` z katalogu (błąd katalogu zaniży B bez sygnału); B startuje od `from_ym`, Magazyn od `cfg.start_month` (liczby mogą się różnić); `TARIFFS` zduplikowane w `tariff_battery` (cykl importów); `dyn_a` w B bez podłogi miesięcznej, `variant_b_note` tego nie opisuje.
  - NIEZWERYFIKOWANE na żywo: B na prawdziwych danych — po wydaniu przez `/api/tariff_sim`.
- **E4 ZROBIONE** (commity 020f609 plan, b806d51, 2026-10-21): zakładka „Symulacja taryf” (`tab-tsim`): rekomendacja z progiem, tabela 12 mies. × 6 taryf (A/B), wykres, rozbicie kosztów, zgodność z fakturami, ustawienia; `tariff_settings_store.py`, `tariff_job.recommend`, `GET/POST /api/tariff_settings`, `/api/tariff_sim` + `recommendation`/`settings`. 819 passed/27 skipped, verifier ACCEPT, UI sprawdzony Playwrightem (desktop + mobile, na syntetycznym payloadzie).
  - Odstępstwo: „edycja cennika” = edycja ustawień (fazy, moc umowna, opłata handlowa, próg rekomendacji); stawki z `tariffs/tauron.json` tylko w pliku.
  - NIEBLOKUJĄCE (do raportu wydania): `loadTsimTab` nadpisuje niezapisane edycje ustawień przy wejściu w zakładkę; wiring `main.py` (ustawienia → job) bez testu; kolor `#f0fdf4` zaszyty w CSS (jak w istniejących regułach).
  - NIEZWERYFIKOWANE na żywo: zakładka na prawdziwych danych — po wydaniu.
- **Następny krok: wydanie** (`simplify` → `code-review` + równoległe review → `release`), potem E5 (~2026-11-25).

## Zgodność 2025 (0.46.1, 2026-10-22)

Po 0.46.0 zgodność G12w z fakturami: 12/21 mies. w ±3%, maks. 63% (2025-06); cały rozjazd w 2025-01…09, od 2025-10 ≤1,4%.

| Przyczyna | Dowód | Stan |
|---|---|---|
| Opłaty stałe 2025 w katalogu za wysokie (~16 zł/mies.) | faktury I–VI: 14,90; VII–XII: 30,91; symulacja 31,24 | poprawione w 0.46.1 (okres 2025 podzielony na I–VI i VII–XII) |
| kWh z HA > kWh z faktury IV–IX 2025 (+5…8 kWh/mies., ~3,5 zł) | od X 2025 zgodne ±1% | do diagnozy w E5, bez zmiany kodu (hipotezy: rekonstrukcja dropoutów Modbus, inny sensor w 2025) |
| Energia G12w 2025 strefowa (0,76/0,42), nie płaska 0,50 (~1 zł) | faktury I–VI, X | świadomie bez zmiany (decyzja usera: bez założeń przy cennikach) |

Miesiące z cennikiem `confirmed: false` są oznaczone w zgodności „cennik niepotwierdzony” i nie wchodzą do podsumowania.

## Przegląd zakładek i dashboardu (2026-10-22)

Zakładki add-onu:
- **„Analiza taryf”** (`tab-tariff`) dubluje „Symulację taryf”: tylko G12w i dynamiczna, stare sensory HA (`koszt_zmienny_g12w_miesieczny`, `symulacja_miesieczna_dynamicznej_faktura`, `calkowity_koszt_1_kwh_dynamiczna`), bez opłat stałych, ze starą dystrybucją. Po E5 przestaje działać, więc **do usunięcia w E5**.
- **„Taryfa”** (`tariff_config.py`, `/api/tariff_config*`) to nie duplikat: historia stawek z faktur zasila `month_close`, `battery_sim` i `live_reader`. Zostaje, opcjonalnie nowa nazwa „Stawki z faktur”.

Do dopisania w E5 (kod add-onu, jedno wydanie z sprzątaniem HA):
- usunąć `tab-tariff` z `index.html` i kod z `app.js`, `/api/tariff_stats` w `web.py`, wywołanie `compute_tariff_tab` i odczyty starych sensorów w `main.py` (ok. 1027, 1159–1176), `tariff_analysis.py` z testami;
- najpierw przenieść to, czego używają inne moduły: `_DEFAULT_PEAK_GROSS`/`_DEFAULT_OFFPEAK_GROSS` (`battery_sim.py:40`) i `_month_label` (`web.py:22`, `yoy.py:41`);
- usunąć z dashboardu `lovelace`, widok `energy`, pozostałe stare karty porównania: blok „G12w vs Dynamiczna — decyzja”, wykres 30 dni, wykresy G11/G12w/G14D, „Oszczędność G12W vs G11”.

Dashboard HA, widok `lovelace/energy` (zrobione 2026-10-22):
- dodana na górze sekcja „Symulacja taryf (pv_roi)”: `sensor.pv_roi_tracker_pv_tariff_sim_*` (miesiąc i 12 mies. dla 6 taryf, najtańsza, oszczędność vs G12w); `12m_dyn_a` i `12m_dyn_b` są `unavailable`, bo cennik dynamicznej startuje od 2026-04 i 2026-01;
- poprawiona karta „Składowe ceny dynamicznej”: WSD netto z sensora (`wsd / 1.23`) zamiast sztywnych stawek 2025;
- usunięte karty z G12 liczoną stawkami G12w oraz zdublowane mniej dokładne: „Najtańsza taryfa w tym miesiącu”, „Porównanie Taryf Energetycznych”, „Symulacja Faktury Miesięcznej”, „Ranking kosztów miesięcznych”, wiersz „Koszt 1 kWh G12”;
- rollback: `scratchpad/rollback_lovelace_energy_sections3.json` (sesja 2026-10-22, plik tymczasowy).

## Audyt cen (2026-10-22)
Raport: `docs/TARIFF_PRICES_AUDIT_2026-10-22.md`. Zmiany w katalogu (niewydane, w commicie): akcyza 0,005 zł/kWh w `fees`, 2025 = min(cena strefy; 0,50) z osobnym okresem Q4. I–VI 2025 potwierdzone. Następny krok: `simplify` → `code-review` → `release` (0.46.2), potem sprawdzenie zgodności z fakturami w `/api/tariff_sim` (otwarte B3: faktury 0,76/0,42).
