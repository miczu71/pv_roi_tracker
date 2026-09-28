# PV ROI Tracker — „Dług depozytowy": wiarygodne saldo przez odtworzenie reguły Taurona

Kopia zatwierdzonego planu z `/data/home/.claude/plans/`.

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

### Etap 2 — ledger wg reguły Taurona (tylko przy GO), wydanie 0.42.0
- `deposit.py`: nowa czysta funkcja replay (zasilenie z lagiem, konsumpcja wg reguły z Etapu 1, FIFO + przedawnienie
  wg ustalonego zegara, czyli domknięcie B8). Wynik `balance_tauron` z flagą jakości (ile faktur odtworzonych).
  Stała `DEFAULT_POSTING_LAG` i logika log-ratio zostają tylko wtedy, gdy spike je potwierdzi.
- Headline salda (`main.py:820`, sensor MQTT, kafle UI) = `balance_tauron`. `balance_model` i `balance_estimate`
  zostają jako diagnostyka albo zostają usunięte, zależnie od wyniku spike’u (decyzja na checkpoincie 1).
- Prognoza 12 mies. i `expiring_*` liczone z nowego ledgera; konsumpcja w prognozie wg tej samej reguły, nie średnia sezonowa.
- Testy w `tests/test_deposit.py`: odtworzenie `used` dla 39 faktur (fixture z żywych danych), przypadek capped,
  jesienne zdjęcie nadwyżki, przedawnienie od właściwego miesiąca, bieżący miesiąc częściowy.
- CHANGELOG, README (tabela encji: zmiana znaczenia sensora salda), release zgodnie z `feedback_pv_roi_release_checklist`
  (bump `pv_roi_tracker/config.yaml` + `__init__.py`, published release, update przez Supervisora).
- UI: tylko etykiety i źródło salda w zakładce faktur, cache-busting `?v=` jest już zrobiony; Playwright direct-IP `172.30.33.15:8099`.

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
