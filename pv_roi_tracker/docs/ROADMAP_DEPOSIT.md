# PV ROI Tracker — „Dług depozytowy": wiarygodne saldo przez odtworzenie reguły Taurona

Kopia zatwierdzonego planu z `/data/home/.claude/plans/`.

## Wynik Etapu 2 (28.09.2026) — wydane, zweryfikowane

**0.42.0** opublikowane (GitHub release, nie draft), Supervisor zaktualizował add-on
(`29a4454d_pv_roi_tracker`), health `ok`. Wszystkie 39 zapisanych faktur re-sparsowane
(`/api/invoice/reparse`, sekwencyjnie — każde wywołanie ~30–60 s, bo `_invoice_reconcile_callback`
odświeża też dane z HA przez WebSocket; brak błędów, wszystkie `ok:true`).

Efekt na żywych danych:
- `deposit.verified_months / total_months`: **21 / 39 (54%)** — nowe pole jakości, widoczne w UI
  jako "21 z 39 mies. potwierdzonych na fakturze (54%)" pod kaflem "Stan bieżący".
- `reconciliation.rows` status `capped`: **7 → 20** (dokładnie zgodnie z przewidywaniem Etapu 1).
- Skrajne `diff_pct` w rekonsyliacji spadły z 300–2000% do max. 435% (najczęściej &lt;25%) —
  te miesiące (2025-04/05/06) to osobny, mniejszy problem spoza zakresu tego Etapu (prawdopodobnie
  błędna detekcja lagu księgowania na przejściu reżimów opłaty handlowej), zostawiony jako
  otwarty temat, nie blokuje.
- `balance_model` bez zmian (474,72 zł) — spójne z oczekiwaniem: strona konsumpcji już była
  poprawna, fix tylko koryguje FLAGĘ jakości/rekonsyliację, nie samo saldo.
- Playwright (direct-IP `172.30.33.15:8099`, desktop 1400×900 + mobile 390×844): kafel jakości
  renderuje się poprawnie na obu, tabela rekonsyliacji pokazuje więcej wierszy „nie do odczytania
  z faktury" (cap-bound), konsola bez nowych błędów (tylko nieszkodliwy 404 favicon.ico,
  niezwiązany ze zmianą). Zrzuty: `playwright/deposit_kpi_desktop_0_42_0.jpg`,
  `playwright/deposit_kpi_mobile_0_42_0.jpg`.
- Pełny pakiet testów: 577/577 zielone (6 nowych).

**Etap 2 zamknięty.** Etap 3 (retro-fix `needs_training` faktury 2024-01, sprzątanie martwego
kodu kotwicy) czeka na „go".

## Wynik Etapu 1 (28.09.2026) — GO

Spike: pobrano 39 PDF-ów faktur (`/api/invoice/pdf?key=YYYY-MM`), sparsowano `pdftotext -layout`
wiersze rozliczenia depozytu (4/5/6/7) i sprzedaży energii (wiersz 1). Wynik: **reguła Taurona
znaleziona z zerowymi naruszeniami na 39/39 fakturach**:

> `deposit_used(M) = min(saldo_dostępne_przed(M), energia_gross(M) − opłata_handlowa_gross(M))`

gdzie `opłata_handlowa_gross` to osobna pozycja na fakturze (28,86 zł netto / 35,50 zł brutto,
czasem 30,92 zł przy niepełnym miesiącu) wliczona w wiersz „1. Sprzedaży energii elektrycznej",
ale **nieuprawniona do pokrycia depozytem** — Tauron usunął tę pozycję z faktury od 2025-08
(fee=0 od tego miesiąca), co tłumaczy, czemu obecny kod widzi „capping" tylko od ~2025-08.

Rozbicie 39 faktur:
- **20/39 „cap-bound"** (`used == cap` dokładnie, ±0,03 zł) — depozyt ograniczony kosztem energii,
  **saldo w tym miesiącu nieznane** (tylko dolna granica). 7 z nich (2025-08…2026-08) już wykryte
  przez istniejący `deposit_capped` w `invoice_parser.py`; **13 wcześniejszych (2023-08…2025-07)
  NIE są wykrywane**, bo obecna definicja `deposit_capped` porównuje `used` z surowym
  `energy_sale_gross_pln`, bez odjęcia opłaty handlowej.
- **19/39 „balance-bound"** (`used == previous`, `used < cap`) — depozyt w pełni wyczerpany,
  **saldo prawdziwe i wiarygodne** (świeże potwierdzenie na fakturze).
- 1 wyjątek: pierwsza faktura (2023-06) — jednorazowy artefakt rozruchu (naliczono bieżący
  okres bez zwykłego opóźnienia księgowania; `used=5,88` ≈ `accrued(2023-06)=5,90` z modelu).
- 2024-01, 2023-07: `previous=used=0` — brak salda do rozliczenia, brak naruszenia.

**Diagnoza źródła zawyżenia/niepewności salda 474,72 zł**: strona konsumpcji w `deposit.py` już
jest poprawna (`want = inv_used`, czyli bezpośrednio z faktury — to zawsze zgodne z regułą).
Winna jest **rekonsyliacja/wykrywanie lagu** (`_ym_shift`/`implied`/`posting_lag` w `deposit.py`)
i **anchor fallback**: obie mechaniki traktują tych 13 przeoczonych miesięcy cap-bound jak
wiarygodne odczyty salda, mieszając je z prawdziwymi w medianie log-ratio — to wyjaśnia
skokowe `diff_pct` (300–2000%) widoczne w `deposit.reconciliation.rows` dla miesięcy sąsiadujących
z tymi 13 przeoczeniami.

**Rekomendacja (zmienia zakres Etapu 2 — mniejszy niż oryginalny plan replay-ledger):**
1. `invoice_parser.py`: nowe pole `trade_fee_gross_pln` (wiersz „Opłata Handlowa"/„opłata handlowa",
   domyślnie 0,0 gdy nieobecny).
2. Poprawić `deposit_capped = abs(deposit_used_pln - (energy_sale_gross_pln - trade_fee_gross_pln)) <= 0.02`.
3. Re-parse istniejących 39 faktur (`/api/invoice/reparse` per miesiąc albo migracja `invoice_store`),
   żeby `deposit_capped` był poprawny retroaktywnie na całej historii, nie tylko od 2025-08.
4. `deposit.py` automatycznie zacznie poprawnie wykluczać 20/39 (nie 7/39) miesięcy z `implied`/
   `posting_lag` i z kotwicy fakturowej — bez zmiany logiki, tylko dzięki poprawnej fladze.
5. Nowe pole jakości w `DepositResult`: udział miesięcy „balance-bound" w całej historii
   (`verified_months / total_months`), pokazane w UI przy saldzie jako miara wiarygodności
   („X z Y miesięcy potwierdzonych na fakturze") — żeby nie prezentować liczby z fałszywą precyzją.
6. B8 (zegar przedawnienia: eksport vs zaksięgowanie) — **wciąż otwarte**, do zbadania w Etapie 2
   po naprawie rekonsyliacji (13 nowo odkrytych cap-bound miesięcy zanieczyszczało też tę analizę).

Surowe dane spike'u (parsowanie 39 PDF, tabela dopasowań) w scratchpadzie sesji — nieprzechowywane
w repo (odtwarzalne z `/api/invoice/pdf`).

## Kontekst

Pompa ciepła × PV (Etapy 0–3, 0.41.0) i Rok do roku (0.38.0) są zamknięte. Z backlogu
(`docs/ROADMAP_HEATPUMP.md`) user wybrał **dług depozytowy**, a jako cel: **wiarygodne saldo depozytu**.
Źródło prawdy: **odtworzyć algorytm Taurona z faktur**. Ręczna kotwica z eBOK i scraping odpadły.

Fakty z żywych danych (28.09.2026, 0.41.0):
- `balance_estimate = null`, headline = `balance_model` **474,72 zł** (FIFO: zasilenie = eksport × RCEm,
  konsumpcja = `deposit_used` z faktury), rośnie latem o ~100–145 zł/mies.
- Faktury od 2026-05 są „capped”: `deposit_previous == deposit_used`, `deposit_current = 0` → kotwica fakturowa nie działa.
- **Linii „zasilenie depozytu” w PDF nie ma.** Faktura ma tylko 3 pozycje depozytu (sprawdzone na 2026-04 i 2026-08):
  „w rozliczanym okresie” (0,00), „z okresów poprzednich”, „rozliczenie”. Pozycja backlogu „parsowanie zasileń” jest więc niewykonalna.
- Obserwowany wzór: zimą `used(M) == accrued(M−1)` co do grosza (2025-12, 2026-02, 2026-04). Latem `used` < `accrued(M−1)`,
  a jesienią zdejmowane są duże kwoty (2024-11: 363,99 zł, 2025-10: 185,37 zł).
  Hipoteza H1: **Tauron zdejmuje min(dostępne saldo, część rachunku, którą depozyt może pokryć)**,
  a nadwyżka przechodzi dalej. To pasowałoby do tego, że saldo modelu jest rzędu prawdy, a nie zawyżone.
  Wymaga weryfikacji.
- Saldo trafia do sensora MQTT `PV Deposit Balance Est`, kafli w zakładce faktur i prognozy przedawnienia.
  **Nie** wpływa na ROI, NPV ani okres zwrotu.

## Etapy (każdy kończy się checkpointem; na następny potrzebne „go”)

### Etap 0 — dokument
Zapis tego planu jako `pv_roi_tracker/docs/ROADMAP_DEPOSIT.md`, commit. Oznaczenie pozycji backlogu
w `ROADMAP_HEATPUMP.md` jako „→ ROADMAP_DEPOSIT”.

### Etap 1 — spike analityczny (zero zmian w add-onie, zero wydania)
Skrypt w scratchpadzie. Wejście: `/api/data` (39 faktur + `deposit.months`), PDF-y przez `/api/invoice/pdf?key=YYYY-MM`
(`pdftotext -layout`). Kroki:
1. Wyciągnąć z każdego PDF pozycje tabeli rozliczenia: „Wynik rozliczenia” (netto/VAT/brutto), podział energia czynna
   vs dystrybucja/opłaty (sprawdzić, czy parser `invoice_parser` już je ma; jeśli nie, doraźny regex w skrypcie).
2. Przetestować hipotezy reguły konsumpcji na odtwarzanym łańcuchu salda:
   H1 min(saldo, energia brutto); H2 min(saldo, cały wynik rozliczenia); H3 min(saldo, wynik − opłaty stałe);
   przy każdej lag zasilenia 1 i 2 mies. oraz netto/brutto (×1,23 od 2025-02).
3. Metryka: dla ilu faktur reguła odtwarza `deposit_used` z dokładnością ±0,05 zł, plus lista wyjątków z wyjaśnieniem.
4. Jednocześnie ustalić, od kiedy biegnie 12-miesięczny zegar przedawnienia (B8): od miesiąca eksportu czy od zaksięgowania.

**Kryterium GO:** jedna reguła odtwarza `used` na ≥ 90% faktur, a wyjątki są wyjaśnione (np. faktury korygujące, zmiana taryfy).
**NO-GO:** raport z najlepszym dopasowaniem i powrót do usera z opcjami (ręczna kotwica z eBOK / pozostawienie modelu z etykietą „szacunek”).
**Checkpoint:** tabela faktura × hipoteza, odtworzone saldo Taurona na koniec 2026-08 vs `balance_model` 474,72 zł, rekomendacja.
Wynik dopisany do tego pliku.

### Etap 2 — poprawka `deposit_capped` + jakość salda (zmieniony zakres po Etapie 1), wydanie 0.42.0
Reguła Taurona jest już poprawnie zaimplementowana po stronie konsumpcji (`want = inv_used`) —
**nie potrzeba nowego ledgera/replay**. Naprawa jest węższa:
1. `invoice_parser.py`: pole `trade_fee_gross_pln` (wiersz „Opłata Handlowa", domyślnie 0,0).
2. Poprawiony `deposit_capped = abs(deposit_used_pln - (energy_sale_gross_pln - trade_fee_gross_pln)) <= 0.02`.
3. Migracja/reparse 39 istniejących faktur, żeby flaga była poprawna retroaktywnie (nie tylko od 2025-08) —
   przez `/api/invoice/reparse` per miesiąc; **ręczne wywołanie z jawną zgodą usera**, bo reparse
   przelicza też rekonsyliację (patrz lekcja z incydentu 2026-03 w `[[project_pv_roi_deposit]]`);
   restart add-onu i weryfikacja po każdej partii.
4. `deposit.py`: bez zmian logiki — `implied`/`posting_lag`/kotwica automatycznie zaczną poprawnie
   wykluczać 20/39 (nie 7/39) miesięcy cap-bound dzięki poprawnej fladze z kroku 2.
5. Nowe pole jakości w `DepositResult`: `verified_months`/`total_months` (udział miesięcy
   balance-bound w historii) — pokazane w UI przy saldzie („X z Y miesięcy potwierdzonych na fakturze").
6. Testy w `tests/test_deposit.py` i `tests/test_invoice_parser.py`: parsowanie `trade_fee_gross_pln`
   (obecny/nieobecny wariant), poprawiona flaga `deposit_capped` na fixture z 2023-08 i 2025-08,
   pole jakości.
- CHANGELOG, README (tabela encji: zmiana znaczenia sensora salda + nowe pole jakości), release
  zgodnie z `feedback_pv_roi_release_checklist` (bump `pv_roi_tracker/config.yaml` + `__init__.py`,
  published release, update przez Supervisora).
- UI: etykieta jakości przy kaflu salda w zakładce faktur, cache-busting `?v=` jest już zrobiony;
  Playwright direct-IP `172.30.33.15:8099`.

### Etap 3 — domknięcie reszty długu, wydanie 0.42.1
- Retro-fix `needs_training` faktury 2024-01. `/api/invoice/reparse` przelicza też rekonsyliację,
  więc przed wywołaniem trzeba jawnej zgody usera; po reparse restart add-onu i ponowna weryfikacja
  (lekcja z incydentu 2026-03).
- Usunięcie martwego kodu kotwicy fakturowej, jeśli Etap 2 go zastąpił.
- Aktualizacja backlogu w `ROADMAP_HEATPUMP.md`.

## Ryzyka
- Reguła Taurona może być nieodtwarzalna, np. ręczne korekty albo zmiany systemu bilingowego w 2025 → dlatego Etap 1 to spike z NO-GO.
- Parser faktur może nie wyciągać części rachunku potrzebnej regule. W spike’u używamy doraźnego regexu,
  a rozszerzenie parsera przesuwamy do Etapu 2; to zwiększa jego zakres.
- Dopasowanie na historii ≠ gwarancja na przyszłość. Flaga jakości (odtworzone faktury / wszystkie) pokazywana obok salda.
- Zmiana znaczenia sensora `PV Deposit Balance Est` zmienia jego historię. Trzeba to opisać w release notes.

## Weryfikacja
1. Etap 1: tabela dopasowań + ręczne przeliczenie 3 faktur (zima „pełne zdjęcie”, lato „capped”, jesień „duże zdjęcie”).
2. Etap 2: `pytest` z katalogu `pv_roi_tracker/` zielony (dziś ok. 540 testów + nowe). Po wdrożeniu
   `curl 172.30.33.15:8099/api/data | jq .deposit` pokazuje `balance_tauron` i jakość ≥ 90%.
   Sensor health `ok`, restart add-onu nie zmienia wyniku. Playwright: screenshot zakładki faktur desktop + 390px
   i konsola bez błędów (`/config/playwright/`).
3. Etap 3: po reparse 2024-01 `needs_training: true` w `/api/data`, rekonsyliacja kWh 2024-01 bez zmian (diff 0).
