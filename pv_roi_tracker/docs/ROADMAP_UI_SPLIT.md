# pv_roi_tracker: dwie główne sekcje (PV ROI | Taryfy i energia) + Ustawienia

## Context
UI add-onu (0.46.5) ma 12 płaskich zakładek. Sprawy PV mieszają się w nich z taryfami i fakturami, ustawienia są rozsiane po zakładkach, a nadpisanie RCEm wisi pod całą stroną. User chce dwóch sekcji: **PV ROI** i **Taryfy i energia**. Druga ma być przejrzysta i mieć konfigurowalne wykresy stref i cen w Symulacji. Ustawienia mają być w jednym miejscu.

Repo: `/config/addons/pv_roi_tracker` (gałąź `main`). Kod jest w `pv_roi_tracker/pv_roi_tracker/`, front w `static/index.html` (623 linie), `app.js` (4194), `app.css`.
Pierwszy krok implementacji: skopiować ten plan do `pv_roi_tracker/docs/ROADMAP_UI_SPLIT.md`.

### Decyzje z wywiadu (2026-10-26)
1. **Nawigacja.** W nagłówku przełącznik „PV ROI | Taryfy i energia”, w rogu ⚙ Ustawienia jako osobny widok. Pod przełącznikiem podzakładki. Stan w hashu adresu (`#taryfy/symulacja?...`).
2. **PV ROI.** Karta główna: kafle KPI, „Rok do roku”, wykres oszczędności i nowy kafel z saldem depozytu. Podzakładki: Historia miesięczna, Prognoza spłaty, Podsumowanie roczne, Wykresy, RCE vs RCEm, Magazyn +5 kWh, Prognoza 25 lat, Pompa ciepła.
3. **Taryfy i energia.** Pasek podsumowania nad modułami: taryfa i strefa teraz, cena kWh teraz, ostatnia faktura (kwota, termin, status), rekomendacja jednym zdaniem. Moduły: Faktury, Taryfa, Symulacja taryf. Widok startowy: Symulacja.
4. **„Analiza taryf” wchłonięta w Symulację.** Stara zakładka jest oparta na sensorach HA, które E5 roadmapy taryf i tak usuwa. Znika razem z osieroconym backendem.
5. **Faktury, od najważniejszego.**
   - KPI: ostatnia faktura, średni koszt, efektywna zł/kWh, depozyt.
   - Lista faktur z wgrywaniem PDF.
   - „Na co idą pieniądze”.
   - Trend stawek.
   - Zwijane: depozyt, rekonsyliacja, pokrycie.
6. **Taryfa.**
   - Twoja taryfa G12w: stawki brutto w strefach, godziny stref, skład ceny, wykres ceny z 7 dni.
   - Oś czasu stawek tylko do odczytu, z przyciskiem „Edytuj w Ustawieniach”.
   - Cennik Tauron: wszystkie taryfy, strefy i stawki 2025/2026 z `tariffs/tauron.json`, tylko do odczytu.
7. **Symulacja.** Zostają rekomendacja, tabela 12 mies. × taryfy A/B, rozbicie i zgodność z fakturami. Dochodzi jeden przyklejony pasek sterowania: checkboxy taryf (G11/G12/G12w/G13/dyn_a/dyn_b), okres (wczoraj, ten tydzień, ten miesiąc, ostatnie 12 mies., Jutro, własny zakres) i wariant A/B. Pasek steruje czterema wykresami:
   - mapa stref i cen: 24 h × dni, taryfy obok siebie;
   - cena godzinowa brutto (schodkowa) z godzinowym poborem w tle;
   - koszt wg stref: kWh i zł na strefę plus opłaty stałe, słupki skumulowane;
   - różnica narastająco A vs B; zastępuje „Analizę wybranego zakresu”.
   „Jutro” pokazuje tylko ceny i strefy (RDN po ok. 14:00), bez kosztów.
8. **Ustawienia (⚙).** Trafiają tu:
   - ręczne nadpisanie RCEm;
   - parametry symulacji taryf;
   - parametry magazynu;
   - eBOK, nauczone układy i diagnoza parsera;
   - formularz ręcznych wpisów taryfy.
9. **Urządzenia:** komputer i telefon po równo. Na telefonie pasek sterowania zwija się do przycisku „Filtry”, a mapa stref pokazuje 1 taryfę naraz albo przewija się poziomo.
10. **Prototyp przed budową:** 2–3 warianty nawigacji i ekranu Symulacji do wyboru.

### Podsumowanie (kto, auto, za zgodą, sukces)
- **Kto korzysta:** user (decyzja o taryfie, kontrola faktur), w HA Companion i w przeglądarce.
- **Automatycznie:** dane jak dziś (cron symulacji 05:30, eBOK, MQTT). Nowe dane godzinowe liczy endpoint na żądanie, z cache.
- **Za zgodą:** wybór wariantu z prototypu. Sensory MQTT i HA bez zmian; E5 roadmapy taryf, czyli sprzątanie HA, zostaje osobno.
- **Sukces:** każda funkcja z dzisiejszych 12 zakładek jest osiągalna w nowym układzie, a znikają tylko świadomie usunięte rzeczy z „Analizy taryf”. Dla wybranego dnia i taryf wykresy pokazują strefy i ceny zgodne z `tariff_catalog.zone_for`/`rates_per_kwh`. Suma kosztu z wykresu „koszt wg stref” za pełny miesiąc zgadza się z `/api/tariff_sim` dla wariantu A z dokładnością ±0,01 zł; wyjątkiem jest dyn_a, bo podłoga średniej działa miesięcznie. Brak błędów w konsoli na desktopie i mobile.

## Etapy
Każdy etap to commit(y) na `main` z rollbackiem `git revert`. Wydanie jest jedno, na końcu (E5).
Kod add-onu powstaje skillem `tdd` z `addon-conventions`, UI skillem `impeccable`. Etapy z ≥5 edycjami mają `docs/PLAN_UI_SPLIT_E<n>.md` z sekcją **Wykonawca** (haiku-worker, a paczka backendu E1 jako „ryzyko” idzie do sonnet-worker) i potem `verifier`.

**E0 – Prototyp (skill `prototype`) — ZROBIONE 2026-10-26, wybrany wariant A**
- Jeden samodzielny HTML z 2–3 wariantami nawigacji i ekranu Symulacji (pasek sterowania + 4 wykresy), przełączanymi na stronie. Dane:
  - zrzut `/api/tariff_sim` z hosta (przez Playwright direct IP, notatka `reference_addon_playwright_direct_ip`);
  - godzinowe dane wygenerowane z `tariff_catalog` na 1 tydzień.
- Publikacja jako prywatny Artifact, oglądany na komputerze i telefonie. User wybiera wariant; to jedyny punkt zatrzymania.
- Rollback: nic, add-on nietknięty.

**E1 – Backend danych godzinowych (tdd) — ZROBIONE 2026-10-26, commit 54b2601**
- `tariff_sim.py`: nowa `hourly_series(from_date, to_date, import_by_hour, catalog, tariffs, rdn, kompas, overrides)`. Każda godzina i taryfa dostaje: strefę, cenę netto i brutto za kWh, kWh i koszt. Używa istniejących `zone_for`, `rates_per_kwh`, `period_for`, `_dyn_b_price`, `fixed_monthly`.
- Agregaty: `zone_map` (dzień × godzina → strefa i cena), `cost_by_zone` (taryfa → strefa → kWh i zł, plus stałe proporcjonalnie do dni), `cumulative_diff`.
- `web.py`:
  - `GET /api/tariff_hourly?from=&to=&tariffs=` z limitem 400 dni i cache w pamięci per zakres;
  - `GET /api/tariff_now`: strefa i cena teraz dla bieżącej taryfy;
  - `GET /api/tariff_catalog`: cennik do odczytu.
- „Jutro”: `PriceCache.ensure` na D+1. Gdy brak kursu EUR/PLN dla D+1, bierze ostatni znany kurs i oznacza to w odpowiedzi. Gdy brak RDN, dynamiczne dostają `available:false` z powodem.
- Testy:
  - granice stref i święta;
  - przełom lato/zima G13;
  - suma `cost_by_zone` za miesiąc = `simulate_month` (A, bez dyn_a);
  - jutro bez RDN.
- Rollback: revert, bo nic w UI jeszcze z tego nie korzysta.

**E2 – Szkielet nawigacji i Ustawienia (bez zmiany treści modułów) — ZROBIONE 2026-10-26, commity f6a507f (W1), 2a2c6e2 (W2)**
- `index.html`, `app.js`, `app.css`:
  - przełącznik sekcji i ⚙;
  - dwie grupy podzakładek;
  - routing hashem w miejsce `showTab`, z zachowaniem leniwego ładowania każdej zakładki;
  - karta główna widoczna tylko w PV ROI.
- Widok Ustawienia: istniejące formularze przeniesione bez zmiany logiki (RCEm `submitOverride`, `saveTsimSettings`, `saveBatteryConfig`, `ebokSync` z panelem układów i debug, formularz `taryfaForm`).
- Usunięcie „Analizy taryf”:
  - `tab-tariff` i jej JS;
  - endpointy `/api/tariff_stats` i `/api/export/tariff_csv`;
  - moduły i testy, które osierocą. Przedtem `grep` użyć (`tariff_analysis.py` ma test stawek, więc sprawdzić, czy korzysta z niego publisher lub main).
- Testy `test_web_static.py` i innych sprawdzających id zakładek dostosowane.
- Rollback: revert commitu.

**E3 – Pasek podsumowania, Taryfa, Faktury, kafel depozytu — ZROBIONE 2026-10-26, commit 1458a8b**
- Pasek podsumowania: dane z `/api/tariff_now`, `/api/tariff_sim` (`recommendation`), ostatniej faktury i statusu płatności (dane `paymentBar`).
- Taryfa: punkty z decyzji 6. Wykres ceny z 7 dni z `/api/tariff_hourly` zastępuje stary wykres z LTS.
- Faktury: nowa kolejność sekcji, KPI i zwijane sekcje z decyzji 5.
- Kafel depozytu w karcie głównej PV ROI: reużycie danych `depKpiCards`.
- Rollback: revert.

**E4 – Symulacja: pasek sterowania + 4 wykresy — ZROBIONE 2026-10-26, commit be9a15d**
- Pasek: stan w hashu i link do tego samego widoku; na mobile zwijany. Wariant B działa tylko w tabeli i rekomendacji, bo jest miesięczną korektą bez godzin. Na wykresach godzinowych B jest wyszarzony z dopiskiem.
- Wykresy Chart.js (vendor już jest):
  - mapa stref: macierz w canvasie albo siatka CSS;
  - cena godzinowa + pobór;
  - koszt wg stref;
  - różnica narastająco.
- Dopisek przy dyn_a: „koszt godzinowy bez miesięcznej podłogi średniej”.
- Rollback: revert.

**E5 – Wydanie (bez pytania, wg CLAUDE.md)**
- `simplify` → `code-review`, a równolegle `silent-failure-hunter`, `pr-test-analyzer`, security review (general-purpose) i second-opinion (pomiar 5/10) → `release`. Bump obu plików wersji: `pv_roi_tracker/config.yaml` czyta Supervisor. Wydanie jako minor 0.47.0.
- Weryfikacja na żywo przez `playwright-ha`: każda sekcja i podzakładka, Ustawienia (tylko otwarcie formularzy, bez zapisu na produkcji), Symulacja z „Ten miesiąc” i „Jutro”, desktop 1440 i mobile 390, `browser_console_messages` czyste.
- Rollback: reinstalacja 0.46.5 przez `ha_manage_updates` z backupem add-onu.

## Weryfikacja
- Każdy etap: `cd /config/addons/pv_roi_tracker/pv_roi_tracker && python -m pytest -q`, wszystkie zielone (dziś ~819 passed).
- E1: wyrywkowo `curl` lokalnego Flaska z `cli.py`/test client, porównanie sum z `/api/tariff_sim`.
- E2–E4: Playwright na lokalnym serwerze z danymi testowymi (screenshot desktop + mobile, konsola), na koniec live na hoście HA (E5).

## Ryzyka
- `app.js` ma 4194 linie, a `showTab` jest wołane z wielu miejsc. Routing w E2 robić jako adapter (`showTab(id)` → nawigacja hashem), żeby nie przepisywać callerów.
- Dane godzinowe za 12 mies. × 6 taryf to ok. 52k punktów. Agregacja jest po stronie serwera, a surowe godziny idą tylko dla zakresów ≤31 dni.
- Kurs EUR/PLN i Kompas na D+1 mogą być niedostępne. Wtedy odpowiedź ma flagę i komunikat, nie cichy fallback.

## Status (2026-10-26)
- **E0 ZROBIONE.** Prototyp (3 warianty, dane syntetyczne na prawdziwym katalogu): artifact https://claude.ai/artifact/JQzps2wcYqguQEMURtPeyt, źródła w scratchpadzie sesji (`proto/template.html`, `gen_proto_data.py`; nie w repo).
- **Decyzja usera: wariant A** — przełącznik „PV ROI / Taryfy i energia” w nagłówku, ⚙ Ustawienia w rogu, podzakładki pod spodem, przyklejony pasek sterowania (taryfy, okres: Dziś/Wczoraj/7 dni/14 dni/Jutro, wariant A/B) nad tabelą wyników i wykresami: mapa stref pełna szerokość, cena+pobór i koszt wg stref w 2 kolumnach, różnica narastająco z wyborem pary taryf. Na telefonie pasek zwija się do „Filtry”.
- Wnioski z prototypu do E4: wspólna skala koloru mapy dla wszystkich taryf, stałe zł/dzień liczone proporcjonalnie, „Jutro” bez kosztów, wariant B wyszarzony na wykresach godzinowych.
- **E1 ZROBIONE** (commit `54b2601`, plan `docs/PLAN_UI_SPLIT_E1.md`). sonnet-worker + verifier ACCEPT; pełny pytest 864 passed, 27 skipped.
  Dodane: `tariff_sim.hourly_series/zone_map/cost_by_zone/cumulative_diff`, `price_sources.fetch_ahead`, `/api/tariff_hourly|tariff_now|tariff_catalog`, `main._tariff_inputs`.
  Odstępstwa od planu: dyn_* `available:false` tylko gdy cały zakres przed `valid_from` (w szerszym zakresie godziny sprzed mają ceny `None`); `cumulative_diff` ma `skipped_kwh`; `fetch_ahead` bierze kurs tylko z `cache.eurpln` (fallback z flagą), bez NBP; `tariff_now` na stałe G12w; `hours` w odpowiedzi to `{taryfa:[wiersze]}` tylko ≤31 dni; dni > dziś+2 mają ceny `None`.
  Do E4 (nieblokujące z verifiera): błąd Kompasu w `fetch_ahead` tylko w logu, `reason` pusty, więc UI nie wyjaśni braku ceny dyn_b (rozważyć dopisanie `kompas_reason`); słabe asercje `and/or` w `test_web_tariff_hourly.py:68-70`.
- **E2 ZROBIONE** (plan `docs/PLAN_UI_SPLIT_E2.md`, commity `f6a507f` backend, `2a2c6e2` front). W1 haiku-worker, W2 sonnet-worker, obaj verifier ACCEPT; pytest 865 passed, 27 skipped.
  Zrobione: `ROUTES`/`applyRoute`/`showTab` jako adapter hasha (`#pv/<slug>`, `#taryfy/<slug>`, `#ustawienia`), przełącznik sekcji + ⚙ w nagłówku, `tab-settings` z 5 sekcjami (RCEm, parametry symulacji, magazyn, eBOK/układy/diagnoza, formularz wpisów taryfy), usunięta „Analiza taryf” (front + `/api/tariff_stats`, `/api/export/tariff_csv`, `compute_tariff_tab`, `compute_range_data`, `get_ha_history_7d`, `read_tariff_live`; test `test_tariff_analysis_rates.py`). `tariff_analysis.py` zostaje (`_month_label`, `_DEFAULT_*_GROSS` używają yoy/battery_sim/web); `get_ha_monthly_stats` i `get_ha_tariff_stats` zostają (używane).
  Weryfikacja na żywo lokalnie (Playwright, serwer z danymi testowymi): wszystkie 15 tras, nieznany/pusty hash → `#pv/historia`, `#taryfy` → symulacja, karta główna tylko w PV ROI, mobile 390 bez przewijania poziomego, konsola bez błędów (tylko favicon testowego serwera).
  Do E3/E4 (nieblokujące): docstring na górze `tariff_analysis.py` nieaktualny; w Ustawieniach `h2.set-h` dubluje `h3` z `.override-wrap`; przełącznik sekcji nie pamięta ostatniej podzakładki; przycisk „Eksportuj CSV” w nagłówku bez zmian.
- **E3 ZROBIONE** (plan `docs/PLAN_UI_SPLIT_E3.md` `ce6d9e2`, kod `1458a8b`). Jedna paczka sonnet-worker (wznowiony po limicie tur), verifier APPROVE; pytest 866 passed, 27 skipped.
  Zrobione: `#taryfySummary` (Teraz, Cena kWh, Ostatnia faktura, Rekomendacja; tylko w sekcji Taryfy, timer 5 min), Taryfa (stawki brutto G12w + skład, godziny stref, wykres 7 dni `stepped` z `gross_per_kwh`, oś czasu readonly, cennik Tauron w `<details>`), Faktury w nowej kolejności (KPI, wgrane+upload, na co idą pieniądze, trend; `<details>`: depozyt, rekonsyliacja, pokrycie+dryf), `renderDepositTile` → `#depositTile` jako 5. karta w PV ROI.
  Lokalnie (Playwright, serwer testowy): konsola bez błędów JS, 1440 i 390 bez przewijania poziomego, tryb 503 → „—”. Zrzuty `/home/claude/.playwright-mcp/e3-*.png`.
  Do E4/E5 (nieblokujące): `a.due_date` w `renderTaryfySummary` bez `_esc`; rekomendacja z pamięci `_tsim` może być nieaktualna przy odświeżaniu co 5 min; `active=null` z payment-status wygląda jak „opłacona / brak zaległości”; niezweryfikowane na realnych danych: kafel „Ostatnia faktura”, Rekomendacja, brak RDN dla 7 dni (sprawdzić live w E5).
- **E4 ZROBIONE** (plan `docs/PLAN_UI_SPLIT_E4.md` `2c5de96`, kod `be9a15d`). sonnet-worker + verifier APPROVE; pytest 867 passed, 27 skipped.
  Zrobione: przyklejony `#tsimCtl` (taryfy, okres Dziś…Własny, wariant A/B, para różnicy, stan w hashu, „Filtry” na mobile), 4 wykresy z `/api/tariff_hourly` (mapa stref canvas, cena+pobór, koszt wg stref, różnica narastająco). Lokalnie Playwright 1440/390 bez błędów konsoli.
  Odstępstwa: mapa to canvas (nie siatka CSS); brak `reason` dla `available:false` (ogólna etykieta; wymaga backendu).
  Do E5 (nieblokujące, verifier): serie ceny łączone indeksem a nie `key`; nawigacja wstecz nie aktualizuje paska; zmiana breakpointu 640 nie przerysowuje mapy; B + „Jutro” zostaje B (tylko szary przycisk). Niezweryfikowane lokalnie: suma kosztu wg stref vs `/api/tariff_sim` (pokryte testem E1), „Jutro” na realnym API, wariant B — sprawdzić live w E5.
- **Następny krok: E5** (wydanie 0.47.0).
