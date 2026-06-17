# Changelog

All notable changes to this project will be documented in this file.

## [0.23.0] — 2026-06-17

### Added

- **Oś zmian taryfy wykryta z faktur** — nowa sekcja w zakładce Taryfa „Zmiany taryfy wykryte z faktur": lista punktów zmiany stawek odtworzona z historii wgranych faktur (delta na każdym polu z `_RATE_FIELDS` z epsilonem 1e-4 dla stawek, 0.01 dla opłat stałych). Pierwsza faktura z danymi = punkt bazowy (bez zmiany). Wpisy mające już odpowiadający ręczny wpis taryfowy oznaczone badge „✓ jest wpis". Przycisk **„Utwórz wpis"** pre-wypełnia formularz pełnym snapshotem stawek z danej faktury — użytkownik weryfikuje i zapisuje.
- **`GET /api/tariff_config/derived`** — read-only endpoint zwracający `{changes:[...], existing_effective_from:[...]}`. Nic nie zapisuje.
- **`tariff_config.effective_baseline(cfg, today)`** — kumulatywny baseline: scala `rates` wszystkich wpisów z `effective_from <= today` rosnąco. Wpis 2027-01 z samym `peak_gross/offpeak_gross` dziedziczy `fixed_*/dist_*` z wpisu 2026-02 — wystarczy podać tylko zmienione stawki.
- **15 nowych testów**: `test_tariff_config.py` (×5 dla `effective_baseline`), nowy plik `tests/test_derive_tariff_changes.py` (×9), `test_web_invoice_rates.py` (×1 dla dziedziczenia baseline).

### Changed

- **`latest_invoice_rates()` krok 1** zmieniony z `current_entry(cfg).rates` (jeden wpis) → `effective_baseline(cfg, today)` (wszystkie wpisy scalone). Priorytet i kolejność kroków bez zmian: baseline < faktura < override.
- **`_build_tariff_drift()`** używa `effective_baseline` zamiast `current_entry().rates` — baseline jest teraz kompletny nawet gdy najnowszy wpis zmienił tylko część pól.
- Opis w zakładce Taryfa → dodana notatka: „Puste pola dziedziczą wartości z wcześniejszych wpisów — wystarczy podać tylko zmienione stawki."

### Entities / services touched

| Endpoint | Zmiana |
|---|---|
| `GET /api/tariff_config/derived` | NOWY — read-only oś zmian taryfy z faktur |

> Sensory MQTT bez zmian. Wartości stawek mogą delikatnie różnić się od v0.22.0, jeśli wcześniejszy wpis taryfowy miał inne pola niż nowszy — teraz effective_baseline scala je wszystkie.

---

## [0.22.0] — 2026-06-17

### Added

- **Nowa zakładka UI «Taryfa»** — lista datowanych wpisów taryfy ręcznej, formularz dodawania/edycji/usuwania wpisów, badge statusu (override aktywny / baseline / bez faktur), adnotacja „publikacja MQTT przy najbliższym pollu (≤30 min)".
- **Nowy moduł `tariff_config.py`** — persystencja stawek w `/data/tariff_config.json` jako posortowana lista datowanych wpisów `effective_from` (format `YYYY-MM`). Obsługuje kolejne zmiany taryfy (2027→2028→…) bez nadpisywania historii. Funkcje: `load`/`save` (atomic write), `upsert_entry` (dedup po `effective_from`), `remove_entry`, `seed_if_missing`, `current_entry`, `override_rates`.
- **Mechanizm override — wypełnienie luki ogłoszenia taryfy**: gdy nowe stawki Tauron są znane z wyprzedzeniem (np. od 1.01.2027), ale faktura potwierdzająca je nadejdzie dopiero w lutym, można z góry wpisać wpis `effective_from=2027-01` → add-on użyje go jako override. Override **wygasa automatycznie** gdy pierwsza faktura za ten miesiąc zostanie wgrana — bez żadnej ręcznej akcji.
- **Nowe endpointy REST**: `GET /api/tariff_config`, `POST /api/tariff_config` (upsert), `POST /api/tariff_config/delete`.
- **Seed przy 1. starcie**: jeśli `/data/tariff_config.json` nie istnieje, tworzony jest z jednym wpisem `2026-02` zawierającym dotychczasowe litery (1.23/0.63 + stałe fixed). Nic nie ginie, nie wymagana żadna migracja ręczna.
- **21 nowych testów jednostkowych** (`tests/test_tariff_config.py`): pokrycie `current_entry`, `override_rates`, `upsert`/`remove`, `seed`, priorytetów w `latest_invoice_rates()`.

### Changed

- **Priorytet stawek w `latest_invoice_rates()`**: baseline z `tariff_config.current_entry()` < faktura (nadpisuje pola gdzie `not None`) < override z `tariff_config.override_rates()` (aktywny tylko w oknie luki). Schemat priorytetu dotyczy wyłącznie wartości stawek — logika wyboru najnowszej faktury (`_latest_real_invoice()`) bez zmian.
- **`_build_tariff_drift()`** porównuje stawki z faktury do **baseline z `tariff_config`** (pola `peak_gross`, `offpeak_gross`, `fixed_total_net`) zamiast dotychczasowych stałych globalnych `_tariff_peak/_offpeak/_FIXED_NET_EXPECTED`.
- **`test_web_invoice_rates.py`** zaktualizowany: fixture `_reset_paths` tworzy pustą `tariff_config.json` per test (brak baseline = izolacja testu); `test_tariff_drift_unaffected_by_stub_key` używa jawnego zapisu `tariff_config` zamiast usuniętego `set_tariff_config(peak, offpeak)`.

### Removed

- **Opcje konfiguracyjne `tariff_peak_price` i `tariff_offpeak_price`** usunięte z `config.yaml` i `schema:`. Wartości żyją teraz w zakładce Taryfa / `/data/tariff_config.json` (seed tworzy plik przy 1. starcie z wartościami z epoki add-onu).
- **Globalne `_tariff_peak`, `_tariff_offpeak`, `_FIXED_NET_EXPECTED`** usunięte z `web.py`.
- **`set_tariff_config(peak, offpeak)`** usunięte z `web.py`; zastąpione przez `set_tariff_config_path(path)`.

### Entities / services touched

| Encja / endpoint | Zmiana |
|---|---|
| `GET /api/tariff_config` | NOWY — lista wpisów taryfy + status override |
| `POST /api/tariff_config` | NOWY — upsert wpisu (walidacja: klucze ⊆ `_RATE_FIELDS`, wartości ≥ 0, format `YYYY-MM`) |
| `POST /api/tariff_config/delete` | NOWY — usuń wpis po `effective_from` |

> Sensory MQTT bez zmian (te same 42+13 encje). Wartości stawek mogą się zmienić gdy override jest aktywny i różni się od poprzednich stałych.

---

## [0.21.0] — 2026-06-17

### Added

- **Obsługa faktur korekt i not obciążeniowych** — parser rozpoznaje typ dokumentu (`FAKTURA VAT KOREKTA NR …`, `NOTA OBCI…`) i nadaje im unikalne klucze (`YYYY-MM~kor~<nr>`, `YYYY-MM~nota~<nr>`). Korekty zasilają `deposit.calculate()` przez `effective_by_month()`. Noty są tylko archiwalne.
- **Zagnieżdżone pod-wiersze korekt w UI** — badge KOREKTA/NOTA, „było → jest" dla depozytu, delta PLN, powód.

### Entities / services touched

> Sensory MQTT bez zmian — korekty i noty są pomijane przy wyborze najnowszej faktury rozliczeniowej.

---

## [0.20.1] — 2026-06-16

### Changed

- **Refactor (no behavior change)** — usunięto duplikację regexów wydobywających kwoty (`_dist_peak_amount`/`_dist_offpeak_amount` były prawie identyczną kopią `_dist_peak`/`_dist_offpeak`; połączone przez parametryzowaną transformację `_amount_pattern()`, zweryfikowaną jako bajt-identyczną przed zastosowaniem). Filtrowanie stubów `unparsed-...` przeniesione do `invoice_store.filter_real()`/`load_real()` — wcześniej rozproszone po `web.py`. `api_data()` wczytuje teraz `invoices.json` raz na żądanie (wcześniej 3 niezależne odczyty); `main.py` liczy `latest_invoice_rates()` raz na cykl (wcześniej dwa razy).

### Entities / services touched

> Brak zmian — czysto wewnętrzny refaktor wydajności/duplikacji kodu z `/simplify`.

---

## [0.20.0] — 2026-06-16

### Added

- **Najnowsza faktura jako jedno źródło prawdy dla stawek** — `latest_invoice_rates()` (i wewnętrzny `_latest_real_invoice()`, używany też przez detekcję driftu) udostępnia stawki netto + opłaty stałe z chronologicznie najnowszej przetworzonej faktury (wybór po kluczu `YYYY-MM`, niezależnie od kolejności wgrywania — wgranie starszej faktury po nowszej **nigdy** nie nadpisuje aktualnie używanych stawek).
- **13 nowych sensorów MQTT** publikujących stawki z najnowszej faktury: `sensor.pv_roi_rate_energy_peak_net`, `rate_energy_offpeak_net`, `rate_dist_var_peak_net`, `rate_dist_var_offpeak_net`, `rate_jakosciowa_net`, `rate_oze_net`, `rate_kogeneracja_net`, `fixed_mocowa_net`, `fixed_abonament_net`, `fixed_stalysieciowy_net`, `fixed_total_net`, `rate_peak_gross`, `rate_offpeak_gross`. Stan `unknown` gdy żadna faktura nie jest jeszcze wgrana.
- **`energy_simulation.yaml` (pakiet HA) czyta stawki z faktury** — opłaty OZE/jakościowa/kogeneracyjna oraz suma opłat stałych (abonament+składnik stały sieciowy+mocowa) są teraz odczytywane z sensorów `sensor.pv_roi_rate_*`/`sensor.pv_roi_fixed_*`, z **fallbackiem identycznym jak dotychczasowe stałe** (np. `0.0073`, `39.47`) gdy sensor nie istnieje/jest `unknown`. Formuły bez zmian — zmieniono tylko źródło wartości.
- **Analiza taryf zsynchronizowana z fakturą** — `compute_tariff_tab()` przyjmuje opcjonalne `fixed_gross_pln`/`peak_gross`/`offpeak_gross`, źródłowane z najnowszej faktury (fallback = stałe `FIXED_GROSS_PLN`/`1.23`/`0.63` jak dotychczas). Linie referencyjne na wykresie 7-dniowym i podsumowanie zakładki odzwierciedlają realne stawki z faktury. Baner driftu konfiguracji vs faktury zmienił charakter z ostrzeżenia (akcja wymagana) na informację (system już automatycznie synchronizuje się z fakturą; `config.yaml` to tylko wartość zapasowa).

### Fixed

- **Detekcja "najnowszej" faktury ignorowała kluczowanie stub-ów** — rekordy nieudanego parsowania (`unparsed-<epoch>-<nazwa>`) sortują się leksykograficznie *za* każdym realnym kluczem `YYYY-MM`, więc `max(stored)` mógł wybrać stub (bez pól stawek) zamiast realnej najnowszej faktury, cicho gubiąc detekcję driftu. Naprawione przez `_latest_real_invoice()`, który filtruje stuby przed wyborem `max()` — używane teraz przez `_build_tariff_drift()` i `latest_invoice_rates()`.

### Entities / services touched

| Encja | Zmiana |
|---|---|
| `sensor.pv_roi_rate_energy_peak_net` / `rate_energy_offpeak_net` | NOWA — stawka netto energii (zł/kWh) z najnowszej faktury |
| `sensor.pv_roi_rate_dist_var_peak_net` / `rate_dist_var_offpeak_net` | NOWA — składnik zmienny sieciowy netto (zł/kWh) |
| `sensor.pv_roi_rate_jakosciowa_net` | NOWA — stawka jakościowa netto (zł/kWh) |
| `sensor.pv_roi_rate_oze_net` | NOWA — opłata OZE netto (zł/kWh) |
| `sensor.pv_roi_rate_kogeneracja_net` | NOWA — opłata kogeneracyjna netto (zł/kWh) |
| `sensor.pv_roi_fixed_mocowa_net` / `fixed_abonament_net` / `fixed_stalysieciowy_net` / `fixed_total_net` | NOWA — opłaty stałe netto (zł/mc) |
| `sensor.pv_roi_rate_peak_gross` / `rate_offpeak_gross` | NOWA — stawki brutto G12w (zł/kWh) z faktury |

> Pozostałe sensory bez zmian.

---

## [0.19.0] — 2026-06-16

### Added

- **Składniki kosztów faktur** (zakładka Faktury) — nowa sekcja "Składniki kosztów — na co idą pieniądze": tabela sum per składnik (energia, składnik zmienny sieciowy, jakościowa, OZE, kogeneracja, mocowa, abonament, składnik stały sieciowy, + opłata przejściowa/handlowa/akcyza gdy występują) z udziałem % i sumą, oraz wykres słupkowy skumulowany per miesiąc. Przełącznik **Netto / Brutto** (×1,23 VAT); widok netto dodaje informacyjny wiersz VAT, tak by netto+VAT odtwarzało sumę brutto.
- **Parser faktur: realne kwoty złotówkowe** — `invoice_parser.py` wyciąga teraz kolumnę "wartość netto" (nie tylko stawkę jednostkową) dla energii, składnika zmiennego, jakościowej, OZE i kogeneracji, plus opłatę przejściową/handlową i akcyzę (rzadkie, brak ostrzeżenia gdy nieobecne). Gdy faktura była przetworzona przed tą zmianą lub kolumna wartości nie została odnaleziona, rozbicie kosztów dolicza się ze stawki × kWh (banner "część wartości oszacowana…" sygnalizuje to w UI).
- **Trwałe przechowywanie oryginalnych PDF-ów faktur** — wgrane pliki są teraz zapisywane w `/data/pdfs/<YYYY-MM>.pdf` (obok dotychczasowego `/data/invoices.json`). Nowe przyciski w tabeli faktur: **PDF** (otwiera oryginał) i **↻ PDF** (przelicza fakturę ponownie z zapisanego PDF-a — przydatne po poprawce parsera, bez konieczności ponownego wgrywania pliku). Usunięcie faktury usuwa też jej zapisany PDF. `invoices.json` `schema_version` 1→2 (zgodność wsteczna zachowana — starsze wpisy bez nowych pól nadal się wczytują).

### Entities / services touched

> Brak zmian w sensorach MQTT — ta wersja dotyczy tylko UI/parsera/przechowywania w add-onie.

---

## [0.18.1] — 2026-06-12

### Fixed

- **Detekcja lagu księgowania na oknie ostatnich 6 par** — globalna średnia błędu obejmowała miesiące 2023–24 z nasyconym depozytem i zbiorczymi księgowaniami, przez co wybierała lag 3. Bieżący rytm Taurona to lag 1 (trzy ostatnie dopasowania implied↔accrued **co do grosza**). Detekcja używa teraz ostatnich `_LAG_WINDOW = 6` par (min. 4; przy mniejszej liczbie — fallback 2 mies.). Efekt na danych referencyjnych: lag 3→1, estymat salda 238 zł → 89 zł (tylko realnie niezaksięgowane miesiące).

---

## [0.18.0] — 2026-06-12

### Fixed

- **Kotwica salda depozytu** — `balance_estimate` startował od `deposit_previous` z ostatniej faktury, ignorując że ta kwota została rozliczona (`deposit_used`) na tej samej fakturze. U Taurona `previous == used` praktycznie co miesiąc (depozyt konsumowany w całości), więc sensor `PV Deposit Balance Est` zaniżał stan. Nowa kotwica: **saldo po fakturze** (`max(0, previous − used)`) + zasilenia z falownika za miesiące eksportu jeszcze niezaksięgowane przez Taurona (wykrywany lag księgowania, domyślnie 2 mies.), łącznie z bieżącym częściowym miesiącem. Konsumpcja szacunkowa nie jest już odejmowana od estymatu — następuje dopiero przy fakturze.

### Added

- **Rekonsyliacja depozytu: faktury vs falownik** (zakładka Faktury) — odpowiedź na pytanie „czy symulacja z falownika pokrywa się z fakturami?". Z łańcucha sald fakturowych rekonstruowane są zasilenia implikowane (`implied(M) = previous(M) − saldo po fakturze M−1`), automatycznie dopasowywany jest lag księgowania (1–3 mies., ≥6 próbek; fallback 2), a tabela zestawia miesiąc-po-miesiącu wartość z falownika (eksport × RCEm ×1,23) z wartością z faktur — **bez kalibracji**, z różnicą w zł i % (wiersze >10% wyróżnione). KPI: Σ model, Σ Tauron, różnica skumulowana (zł i %), wykryty lag. Nowe pola w `/api/data` → `deposit`: `posting_lag_months`, `anchor_balance`, `unposted_accrual`, `reconciliation{rows, totals}`. KPI „Stan bieżący (estymat)" pokazuje rozbicie: saldo po fakturze + niezaksięgowane.
- **Przebudowany wykres „Produkcja miesięczna — ranking"** (Wykresy) — kolor słupka = rok (legenda u góry), wartości kWh zawsze widoczne na końcach słupków, medale 🥇🥈🥉 dla top 3, przerywana linia średniej z podpisem, wysokość dopasowana do liczby miesięcy (22 px/słupek), tooltip z pozycją w rankingu („#5 z 36 • powyżej średniej").
- **RWD — pełne wsparcie ekranów mobilnych** — zakładki przewijane poziomo (bez zawijania, ukryty scrollbar); gridy 2-kolumnowe (Analiza taryf) składane do 1 kolumny <700 px; breakpoint <640 px: ciaśniejsze karty/nagłówek/wykresy; tabele z przyklejoną pierwszą kolumną przy przewijaniu poziomym; modal treningu parsera mieści się w 96vw, panele 40vh na telefonie.

### Entities / services touched

| Encja | Zmiana |
|---|---|
| `sensor.pv_roi_tracker_deposit_balance_est` | POPRAWKA wartości — kotwica = saldo po fakturze + niezaksięgowane zasilenia (skok wartości po aktualizacji jest zamierzony) |

> Pozostałe sensory bez zmian. Nazwy/unique_id bez zmian — żadnych zmian w automatyzacjach nie trzeba.

---

## [0.17.0] — 2026-06-12

### Fixed

- **Ceny ujemne RCE liczone po 0 zł (art. 4b ustawy o OZE)** — symulacja RCE godzinowej wyceniała eksport surową ceną RCE, włącznie z cenami ujemnymi, zaniżając symulowany przychód i obciążając rekomendację przeciw przejściu na RCE. Teraz `p_eff = max(p, 0)` — ustawowa reguła domyślna dla prosumentów. Nowe pola per miesiąc: `neg_kwh`, `neg_share_pct`, `neg_saved_pln` (ile chroni reguła zera) + agregaty w podsumowaniu. Cache `rce_hourly.json` dostał wersjonowanie (`v: 2`) — zamrożone miesiące przeliczają się automatycznie wg nowej reguły (surowe ceny zostają).

### Added

- **Depozyt prosumencki: przedawnienie 12 mies. + limit zwrotu** — nowy czysty moduł `deposit.py`: ledger FIFO zasileń (feed-in revenue) i konsumpcji (faktury, fallback: szacunek z falownika), ustawowe 12-miesięczne przedawnienie każdej partii oraz zwrot nadpłaty ograniczony do `deposit_refund_pct` × zasilenie miesiąca (20% RCEm / 30% RCE godzinowa, art. 4 ust. 11 ustawy o OZE). Saldo kotwiczone na ostatniej fakturze (lag dopisywania depozytu przez Taurona 1–2 mies.). Zakładka Faktury: KPI (saldo, traci ważność za 1/3 mies., prognoza zwrot/umorzenie 12 mies.) + wykres saldo model/faktury/prognoza z słupkami przedawnień.
- **Nowe sensory MQTT (6)**: `PV Self-Consumption Rate` (%), `PV Autarky` (%), `PV CO2 Avoided` (kg, wskaźnik KOBiZE konfigurowalny), `PV YoY Yield Delta` (% r/r po sparowanych miesiącach), `PV Deposit Balance Est` (PLN), `PV Deposit Expiring 30d` (PLN).
- **Heatmapa eksport × cena RCE** (zakładka RCE vs RCEm) — siatka 24h × miesiąc z cache'owanych cen godzinowych i profilu eksportu; przełącznik cena/kWh; komórki z ceną ujemną na czerwono. Nowe pole `hours_profile` w wierszach miesięcznych.
- **Wachlarz spłaty (fan chart)** w Prognozie spłaty — skumulowany zwrot: historia + ścieżka sezonowa P50 z pasmem P10–P90 (czynniki sezonowe + residual CV z `roi.py`). Prognoza w tabeli też używa teraz ścieżki sezonowej zamiast płaskiej średniej.
- **Waterfall miesięczny** (Wykresy) — dekompozycja wybranego miesiąca: autokonsumpcja → sprzedaż → arbitraż → opłaty stałe (z faktury) → netto.
- **Sankey przepływu energii** (Wykresy) — produkcja → autokonsumpcja/eksport, zakup → zużycie; selektor roku; plugin `chartjs-chart-sankey` (przy braku CDN wykres jest ukrywany).
- **Oszczędności nominalne vs realne (CPI)** (Wykresy) — skumulowane oszczędności deflowane CPI GUS (`cpi_deflator` per miesiąc w `/api/data`).
- **Trend degradacji** (Wykresy) — kroczący uzysk 12-mies. (kWh/kWp) z dopasowaniem liniowym (%/rok) i delta r/r; nowa funkcja `roi.degradation_analysis()`. Karty: CO₂ uniknięte, Autokonsumpcja, Produkcja r/r.
- **Nowe opcje konfiguracyjne**: `co2_factor_kg_kwh` (domyślnie 0.597, KOBiZE dla odbiorców końcowych) i `deposit_refund_pct` (domyślnie 0.20).

### Entities / services touched

| Encja | Zmiana |
|---|---|
| `sensor.pv_roi_tracker_self_consumption_rate` | NOWA — autokonsumpcja % (produkcja zużyta na miejscu) |
| `sensor.pv_roi_tracker_autarky` | NOWA — autarkia % (zużycie pokryte z PV) |
| `sensor.pv_roi_tracker_co2_avoided` | NOWA — kg CO₂ unikniętego, `total_increasing` |
| `sensor.pv_roi_tracker_yoy_yield_delta` | NOWA — produkcja r/r % |
| `sensor.pv_roi_tracker_deposit_balance_est` | NOWA — szacowane saldo depozytu (PLN) |
| `sensor.pv_roi_tracker_deposit_expiring_30d` | NOWA — depozyt tracący ważność w ciągu mies. (PLN) |
| `sensor.pv_roi_tracker_health` | atrybuty: nowe zadanie `deposit` |

> Uwaga: HA skleja entity_id z nazwy urządzenia i encji — rzeczywiste id sprawdź w Ustawienia → Urządzenia → PV ROI Tracker.

---

## [0.16.1] — 2026-06-11

### Changed

- **Zakładka RCE vs RCEm — poprawione ostrzeżenie prawne** (źródło: lepiej.tauron.pl, nowelizacja ustawy o OZE z 27.11.2024): przejście na RCE godzinową jest **nieodwracalne** (oświadczenie w Strefie Prosumenta, powrót do RCEm niemożliwy); przy RCE godzinowej można wypłacić do 30% depozytu w 12 mies. (RCEm: 20%); współczynnik 1,23 od 2025-02 obowiązuje wszystkich prosumentów net-billing w obu trybach, więc nie wpływa na znak różnicy RCE−RCEm.
- Zaktualizowany docstring `rce_hourly.py` o podstawę prawną współczynnika 1,23 (potwierdza `_MULTIPLIER_FROM = 2025-02`).

### Entities / services touched

Brak — zmiana tekstów w UI i dokumentacji modułu.

---

## [0.16.0] — 2026-06-11

### Added

- **Zakładka „RCE vs RCEm"** — symulacja rozliczenia sprzedaży nadwyżek po cenie godzinowej RCE zamiast miesięcznej RCEm. Nowy moduł `rce_hourly.py`: godzinowy eksport z statystyk długoterminowych HA × ceny 15-min RCE; ceny dnia bieżącego z integracji `rce_pse` (atrybut `prices`), historyczne z oficjalnego REST API PSE (`api.raporty.pse.pl/api/rce-pln`, 1 request na miesiąc, cache w `/data/rce_hourly.json`). KPI, wykres przychodów, tabela miesięczna z pokryciem danych i rekomendacją (ROZWAŻ RCE / ZOSTAŃ PRZY RCEm / NEUTRALNA). Zamknięte i rozliczone miesiące są zamrażane w cache. Mnożnik VAT ×1.23 od 2025-02 — ta sama reguła co dla RCEm.
- **Sensor zdrowia `sensor.pv_roi_tracker_health`** (`ok`/`degraded`/`error`) z atrybutami per zadanie (poll, rcem, cpi, backup, rce_hourly, tariff_comparison, month_close) + jawna flaga `solcast_available` — koniec cichej degradacji projekcji Solcast. Nowa automatyzacja alertu w pakiecie `pv_roi_rcem_alert.yaml`.
- **Miesięczne podsumowanie push** po zamknięciu miesiąca (produkcja, oszczędności, ROI %, pozostało do spłaty, przewidywana spłata) przez `notify.family`. Nowa opcja `monthly_notify` (domyślnie `true`).
- **Konfigurowalny arbitraż baterii** — nowa opcja `battery_roundtrip_efficiency` (domyślnie 0.92). Add-on liczy oszczędności sam: `kWh z sensora battery_grid_charge_off_peak_monthly × (tariff_peak_price × sprawność − tariff_offpeak_price)`, zamiast czytać sensor szablonowy z zaszytą stawką 0.50 PLN/kWh (zostaje jako fallback).
- **Wersja add-onu w nagłówku UI** oraz w payload `/api/data` (`version`).
- **Kolumny r/r w Podsumowaniu rocznym** — produkcja i oszczędności rok-do-roku, liczone na bazie tych samych miesięcy (rok częściowy porównywany z tym samym wycinkiem roku poprzedniego).

### Fixed

- **`run.sh` nie eksportował `TARIFF_PEAK_PRICE` / `TARIFF_OFFPEAK_PRICE`** — opcje `tariff_peak_price`/`tariff_offpeak_price` z konfiguracji add-onu były ignorowane (zawsze działały wartości domyślne).
- **Parser CSV: etykieta `autokonsumpcja oszczędność` nie była rozpoznawana** — `_norm()` składa teraz znaki diakrytyczne (NFD, usunięcie Mn), więc warianty ę/e w etykietach arkusza nie psują dopasowania.

### Removed (porządki w repo — bez wpływu na działanie)

- Usunięte martwe duplikaty: `config.yaml`/`Dockerfile`/`build.yaml`/`run.sh`/`requirements.txt`/`pytest.ini`/`tests/` w katalogu głównym repo oraz 19 płaskich modułów `.py` (stan ~v0.15.4) w `pv_roi_tracker/`. **Uwaga:** przez te duplikaty pytest importował martwe kopie zamiast realnego pakietu — po sprzątnięciu suita testuje wydawany kod.

### Entities / services touched

| Encja / serwis | Rola |
|---|---|
| `sensor.pv_roi_tracker_health` | **NOWY** — zdrowie add-onu (MQTT discovery, atrybuty JSON) |
| `sensor.rce_pse_price` | **NOWE źródło** — dzisiejsza krzywa RCE (atrybut `prices`) |
| `sensor.power_meter_exported_energy_monthly` | statystyki godzinowe (WS `recorder/statistics_during_period`) dla symulacji RCE |
| `sensor.battery_grid_charge_off_peak_monthly` | **NOWE źródło** arbitrażu (kWh; PLN liczone w add-onie) |
| `sensor.battery_arbitrage_savings_monthly` | zdeprecjonowany fallback (zaszyta stawka 0.50 PLN/kWh) |
| `notify.family` | miesięczne podsumowanie po zamknięciu miesiąca |

---

## [0.15.6] — 2026-06-11

### Changed (refaktoryzacja wewnętrzna — bez zmian funkcjonalnych)

- **`web.py`:** usunięto zduplikowane `_MONTHS_PL` i `_month_label()` — używane są teraz wersje z `tariff_analysis.py` (jedno źródło prawdy dla etykiet miesięcy).
- **`live_reader.py`:** nagłówki autoryzacji REST (`Authorization: Bearer …`) budowane były identycznie w 3 funkcjach — wydzielono stałą modułową `_HEADERS`. Importy `json`/`websocket` przeniesione z wnętrza `get_ha_tariff_stats()` na poziom modułu (`websocket-client` jest już w `requirements.txt`).
- **`deposit.py` (kopia dev):** formuła aktualizacji salda depozytu FIFO (`available → consumed → balance`) powtarzała się w 5 miejscach (pętla główna, miesiąc bieżący, estymata fakturowa ×2, prognoza) — wydzielono helper `_apply_month()`. Ujednolicono zaokrąglanie `consumed` przed odjęciem (różnice < 1 grosz w estymacie fakturowej).

### Entities / services touched

Brak — czysta refaktoryzacja, payloady API i UI bez zmian.

---

## [0.15.5] — 2026-06-10

### Fixed

- **Wykres „Cena 1 kWh — ostatnie 7 dni" zawsze pokazywał dane z 03→04.06:** Przyczyną był brak parametru `end_time` w zapytaniu REST `/api/history/period/<start>`. HA domyślnie zwraca tylko 24 h od `start`, więc przy `start = now − 7 dni` okno wynosiło zawsze dobę sprzed tygodnia. Dodano `end_time = now` w `get_ha_history_7d()` (`live_reader.py`), co przywraca pełne 7 dni danych do chwili bieżącej.

### Changed

- **Usunięto wykres „Różnica dzienna G12w − Dynamiczna (7 dni)"** z sekcji TERAZ w zakładce Analiza taryf — redundantny wobec wykresu ceny.
- **Wykres „Cena 1 kWh — ostatnie 7 dni"** powiększony do pełnej szerokości zakładki (height: 320 px, poprzednio 220 px w układzie dwukolumnowym).

### Entities / services touched

| Encja | Rola |
|---|---|
| `sensor.calkowity_koszt_1_kwh_dynamiczna` | Cena dynamiczna (wykres 7-dniowy) |
| `sensor.power_tauron_g12w_current_price` | Cena G12w (wykres 7-dniowy) |
| `sensor.roznica_dzienna_g12w_vs_dynamiczna` | Różnica (backend — nadal pobierana, wykres usunięty z UI) |

---

## [0.15.1] — 2026-06-04

### Fixed

- **Zakładka "Analiza taryf" — sekcja HISTORIA:** W HA 2026.x usunięto REST endpoint
  `/api/recorder/statistics_during_period`; statystyki długoterminowe są dostępne
  wyłącznie przez WebSocket. `get_ha_monthly_stats()` w `live_reader.py` przepisano
  na transport WebSocket (`websocket-client`), zachowując identyczną sygnaturę i
  kształt wyniku. Dodano `websocket-client>=1.7` do `requirements.txt`.

- **Koszty G12w w zakładce taryf:** Dotychczas `compute_tariff_tab()` pobierał koszt
  G12w z wewnętrznych rekordów add-onu (`MonthlyRecord.purchase_cost_pln`). Zmieniono
  na `sensor.koszt_zmienny_g12w_miesieczny` — ten sam utility-meter co po stronie
  dynamicznej, jednolita metodologia dla obu taryf. Statystyki obu encji pobierane
  jednym zapytaniem WebSocket.

- **Parsowanie timestampów statystyk:** HA zwraca `start` jako epoch **milliseconds**
  UTC (nie sekundy). Poprzedni kod dzielił przez 1 i interpretował jako UTC, co
  dawało błędny miesiąc dla przejść UTC→CET/CEST (np. 30.11 23:00 UTC → grudzień
  w Warszawie, nie listopad). Poprawiono: `datetime.fromtimestamp(start_ms / 1000)`
  (lokalny TZ kontenera = Europe/Warsaw).

### Entities / services touched

| Encja | Rola |
|---|---|
| `sensor.symulacja_miesieczna_dynamicznej_faktura` | Miesięczny koszt zmienny taryfy Dynamicznej (źródło: HA Statistics) |
| `sensor.koszt_zmienny_g12w_miesieczny` | Miesięczny koszt zmienny taryfy G12w (źródło: HA Statistics) |

---

## [0.2.0] — 2026-05-03

### Added

- **`live_reader.py`** — reads six live HA entities via the Supervisor REST API (`SUPERVISOR_TOKEN`) and builds a `MonthlyRecord` for the current calendar month. Derived fields (`self_consumed_kwh`, `self_consumed_savings_pln`, `purchase_cost_pln`, `feedin_revenue_pln`, `specific_yield`) calculated internally.
- **`rcem_scraper.py`** — scrapes the PSE website for last month's RCEm feed-in price (PLN/MWh ÷ 1000 → PLN/kWh). Runs on days 11–20 at 20:00 UTC until the value is found. Persists to `/data/rcem_history.json`; on success calls `historic_store.backfill_rcem()` and triggers an immediate ROI recompute. Robust row-matching handles numeric (`2026-05`) and Polish month-name formats.
- **`month_close.py`** — APScheduler job firing at 23:55 on the last day of each month (before utility meters reset). Reads live HA values and appends the closing month to `historic.json` with `rcem_status='pending'`; the scraper backfills feed-in revenue later.
- **`publisher.py`** — MQTT discovery publisher. On connect: publishes 12 discovery payloads under one `PV ROI Tracker` device. On every recompute: publishes state for all 12 sensors with `retain=True`. LWT sets `pv_roi/availability` to `offline` if the add-on dies.
- **`main.py` steady-state loop** — APScheduler with three jobs (poll, month-close, RCEm). Startup RCEm catch-up: if today ≥ 11th and last month's price is missing, attempts an immediate scrape before the scheduler starts.
- `tzlocal` added to `requirements.txt` for APScheduler local-timezone cron triggers.

### Changed

- `live_reader.py` replaced the Phase 2 stub (`return None`) with real Supervisor REST calls.
- `config.yaml` and `__init__.py` bumped to `0.2.0`.

---

## [0.1.0] — 2026-05-03

### Added

- **`parser.py`** — pivot CSV parser supporting Polish metric row labels (NFC-normalised), Polish decimal comma format (`291,80 zł`, `4 249,56 zł` with NBSP thousands separator), Polish and English month abbreviations including `Paź` (October with `ź`), and full Polish month names. Skips the `SUMA` column to avoid double-counting. Handles the two-row summary block at the top of the export.
- **`models.py`** — `MonthlyRecord` dataclass with 11 metric fields and `rcem_status` (`confirmed` / `pending` / `missing`). `to_dict()` / `from_dict()` for JSON serialisation.
- **`historic_store.py`** — atomic write (`tmp` → rename) with `.bak` fallback on corrupt JSON. `load()`, `save()`, `append_month()` (idempotent), `backfill_rcem()` (touches only `feedin_price_pln_kwh`, `feedin_revenue_pln`, `rcem_status`).
- **`roi.py`** — pure ROI calculation. Formula matches the source spreadsheet: `ROI % = (subsidy + savings) / gross_investment × 100`. Outputs `RoiResult` with total return, payback months/years/date, monthly average savings, energy totals, and specific yield.
- **`concat.py`** — merges the frozen historic list with the volatile current-month record; current month wins on key collision.
- **`importer.py`** — fetches the Google Sheets published CSV with 3× exponential backoff; strips UTF-8 BOM.
- **`live_reader.py`** — Phase 1 stub returning `None`.
- **`cli.py`** — `import-csv`, `show`, `roi` subcommands.
- **`main.py`** — Phase 1 entry point: imports CSV on first start, then exits.
- **`config.yaml`**, **`Dockerfile`**, **`run.sh`** — HA add-on packaging for `aarch64`, `amd64`, `armv7`.
- 44 pytest tests covering parser, ROI engine, historic store, and concatenator.

### ROI formula detail

The spreadsheet defines:
- `status` = `dofinansowanie` (subsidy) + `oszczędności` (savings)
- `%` = `status / inwestycja` (gross investment)

This means the subsidy itself counts as a return on the gross investment. With data through 2026-04 the result is **80.94 %**, matching the spreadsheet exactly.
