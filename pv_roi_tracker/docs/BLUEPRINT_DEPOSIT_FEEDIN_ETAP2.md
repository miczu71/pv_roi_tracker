# PV ROI Tracker — Etap 2: fix parsera korekt + śledztwo maj/czerwiec 2025

## Context

Etap 1 (spike) obalił hipotezę H1 (RCE godzinowa) i potwierdził, że `rcem_scraper` jest w 100%
poprawny (zweryfikowane live przeciw oficjalnej stronie PSE). Dalsze śledztwo namierzyło **realny,
potwierdzony błąd w `invoice_parser.py`**: Tauron wysłał 01.01.2026 masową serię 24 faktur
korygujących z listem *„aktualizacja wartości depozytu wg RCE + doliczenie współczynnika ×1,23
wstecznie od 1.01.2025”*. Każda korekta ma dwie sekcje — „POLICZONO” (stara wartość) i „NALEŻAŁO
POLICZYĆ” (nowa, poprawna) — a kod *próbuje* wyciąć tylko drugą, ale `pypdf.extract_text()` (tryb
`plain`) dla tego szablonu PDF zwraca tekst w kolejności innej niż wizualna: fragment „POLICZONO”
pojawia się w strumieniu PO markerze „NALEŻAŁO POLICZYĆ”, więc regex łapie starą wartość. Potwierdzone
na `kor_04.pdf` (korekta kwietnia 2025): parser zwraca 68,06 zł zamiast poprawnych 74,31 zł.
Weryfikacja pokazała, że `pypdf` w wersji zainstalowanej w add-onie (6.14.2) obsługuje
`extraction_mode='layout'`, który dla tego samego pliku daje **jedno, poprawne** dopasowanie (74,31,
bez śladu starej wartości) — potwierdzone bezpośrednio w Pythonie na pobranym pliku.

Ale błąd parsera **nie tłumaczy wszystkiego**: dla maja i czerwca 2025 korekta pokazuje
`correction_delta_pln: 0.0` — POLICZONO = NALEŻAŁO POLICZYĆ, żadna wartość się nie zmieniła — a to
właśnie tam jest największa rozbieżność w rekonsyliacji (maj: model 97,14 zł vs realne 18,14 zł,
+435%). Cel tej sesji: **oba wątki równolegle** — (A) naprawić potwierdzony błąd parsera i
zreparsować 24 faktury z korektą, (B) dociec (w rozsądnym, ograniczonym zakresie) przyczyny
majowej/czerwcowej rozbieżności, która przetrwała nawet oficjalną korektę Taurona.

## Część A — fix parsera korekt (release 0.43.0)

**Plik:** `pv_roi_tracker/pv_roi_tracker/invoice_parser.py`.

1. **Ekstrakcja layout-mode tylko dla scope'owania sekcji korekty**, nie globalnie (żeby nie
   ryzykować regresji na ~15 innych regexów pól, które są dostrojone do formatu `plain`):
   - `_extract_text(pdf_bytes)` (linia 510) zwraca dodatkowo tekst w trybie `layout`
     (`p.extract_text(extraction_mode='layout')` per stronę) — albo jako druga wartość zwracana,
     albo nowa funkcja `_extract_text_layout(pdf_bytes)`.
   - `parse_invoice()`/`parse_invoice_debug()` (linie 524, 535) przekazują obie wersje tekstu do
     `_parse_text(text, layout_text=None)`.
   - W `_parse_text`, w gałęzi `_is_korekta` (linie 653–664): jeśli `layout_text` dostępny, scope
     `_deposit_text`/`_amount_text` licz na NIM (znajdź marker „NALEŻAŁO POLICZY” w `layout_text`,
     tnij od tego miejsca), zamiast na `text` (plain). Reszta pól korekty (energia, dystrybucja)
     zostaje na `text` (plain) bez zmian — nie dotykamy działających regexów.
   - Fallback: jeśli `layout_text` niedostępny/pusty (błąd ekstrakcji), zachowanie jak dziś (plain,
     z ostrzeżeniem w `warnings`).
2. **Test regresyjny na prawdziwym pliku**: zapisać `kor_04.pdf` (korekta kwietnia 2025, pobrana
   przez `/api/invoice/pdf?key=2025-04~kor~...`) jako fixture w `tests/fixtures/` (albo katalogu
   wzorowanym na istniejącej strukturze testów), test `parse_invoice(pdf_bytes)` → `deposit_previous_pln == 74.31`
   (nie 68.06).
3. **Test regresyjny na tekście syntetycznym** w `tests/test_invoice_parser.py` (obok istniejącego
   `_make_korekta_text()`, linia 828): wariant tekstu, w którym stara wartość POLICZONO celowo
   pojawia się PO markerze NALEŻAŁO POLICZYĆ (symulacja pypdf plain-mode reorderingu) — sprawdzić,
   że z `layout_text` parser i tak wybiera nową wartość. Istniejące testy (linie 859–981) korzystają
   z ręcznie budowanego tekstu w poprawnej kolejności — muszą zostać zielone bez zmian (regresja).
4. **Reparse 24 faktur z korektą** — **jawna zgoda usera przed wywołaniem** (przelicza też
   rekonsyliację, lekcja z incydentu 2026-03 w `project_pv_roi_deposit`), `/api/invoice/reparse`
   per miesiąc, restart + weryfikacja po partii. Lista miesięcy z korektą (z `/api/data`):
   2023-06, 2023-11/12, 2024-04, 2024-07…2025-01, 2025-02…2025-12, 2026-01, 2026-03(nota, osobny
   przypadek — pominąć, `doc_type: nota` nie ma sekcji POLICZONO/NALEŻAŁO POLICZYĆ).
5. Po reparse: sprawdzić `/api/data | jq .deposit.reconciliation.rows` — ile z odstających
   miesięcy się poprawia (oczekiwanie: marzec/kwiecień tak, maj/czerwiec nie — patrz Część B).
6. CHANGELOG, README (opis fixu + wpływ na `PV Deposit Balance Est`), bump `config.yaml` +
   `__init__.py`, release **0.43.0** wg `feedback_pv_roi_release_checklist`, Playwright direct-IP
   `172.30.33.15:8099` (zakładka Faktury/Depozyt, desktop + 390px, konsola bez błędów).

## Część B — śledztwo maj/czerwiec 2025 (time-boxed, bez gwarancji rozwiązania)

Dane: model (kWh×RCEm oficjalna) mówi 97,14 zł (maj) / 72,93 zł (czerwiec); realne zasilenie
potwierdzone PRZEZ SAMEGO Taurona (nawet po korekcie z 01.01.2026, delta=0,00) to 18,14 / 31,68 zł.
Wykluczone już w Etapie 1: zła cena RCEm (scraper poprawny), zły wolumen kWh (identyczny z fakturą),
zła metoda rozliczenia (RCEm potwierdzona tekstem faktury). PSE nie opublikowało żadnej korekty
(„skorygowana RCEm”) dla maja/czerwca 2025 — brak śladu w historii publikacji.

**Zakres (ograniczony, bez otwierania nowego dużego podprojektu):**
1. Sprawdzić, czy realna cena maja/czerwca (0,050 / 0,073 zł/kWh) pasuje do RCEm **innego**
   miesiąca (przesunięcie o więcej niż 1 mies., np. lipiec/sierpień, gdyby wewnętrzny system
   Taurona użył złego miesiąca referencyjnego przy oryginalnym airing) — szybkie porównanie
   liczbowe z już znaną tabelą PSE (Część A nie wymaga tego kroku, robimy go równolegle).
2. Sprawdzić sąsiednie miesiące (styczeń–kwiecień, lipiec–wrzesień 2025) tą samą metodą
   (POLICZONO vs NALEŻAŁO POLICZYĆ z ich korekt) — czy któryś jeszcze ma `delta=0,00` mimo
   dużego realnego zaniżenia, żeby ustalić, czy maj/czerwiec to odosobniony przypadek, czy wzór
   (np. wszystkie miesiące „w pełni drenowane” — `verified: true` — mają ten sam problem).
3. Jeśli krok 1–2 nie da spójnego wyjaśnienia: **zamknięcie z etykietą niepewności**, bez dalszego
   kodu — dopisanie do `deposit.months`/UI flagi (np. rozszerzenie istniejącego pola jakości z
   0.42.0, `verified_months/total_months`) dla miesięcy, gdzie `tauron_implied` mocno odbiega od
   modelu MIMO potwierdzenia przez samego Taurona (żeby użytkownik widział to w UI, a nie tylko w
   `reconciliation.rows`), i rekomendacja: zgłoszenie zapytania do infolinii/eBOK Taurona (32 606 0
   606, wg listu przewodniego korekty) jako jedyna droga do ostatecznego wyjaśnienia.
4. Nie inwestować dalej w automatyczne dochodzenie tej konkretnej niezgodności kodem — ryzyko
   nieograniczonego pogłębiania bez twardych danych źródłowych poza tym, co już sprawdzone.

## Pliki

- `pv_roi_tracker/pv_roi_tracker/invoice_parser.py` — fix ekstrakcji dla korekt (Część A.1)
- `pv_roi_tracker/tests/test_invoice_parser.py` — nowe testy (Część A.2, A.3)
- `pv_roi_tracker/tests/fixtures/` — nowy plik `kor_04.pdf` (lub podobna lokalizacja zgodna z
  istniejącą strukturą testów — zweryfikować przy implementacji, czy testy już mają katalog na
  binarne fixture'y PDF)
- `pv_roi_tracker/CHANGELOG.md`, `pv_roi_tracker/README.md`, `pv_roi_tracker/config.yaml`,
  `pv_roi_tracker/pv_roi_tracker/__init__.py` — release 0.43.0
- `docs/ROADMAP_DEPOSIT_FEEDIN.md` — dopisanie wyniku obu części (checkpoint)

## Weryfikacja

1. `pytest` z katalogu `pv_roi_tracker/` — pełny pakiet zielony (577 + nowe), w tym nowy test na
   `kor_04.pdf` (`deposit_previous_pln == 74.31`) i regresja na istniejących testach korekt/nie-korekt.
2. Po reparse: `curl 172.30.33.15:8099/api/data | jq .deposit.reconciliation.rows` — marzec/kwiecień
   2025 blisko zera, maj/czerwiec udokumentowane jako otwarty temat (Część B wynik).
3. Health add-onu `ok`, restart nie zmienia wyniku.
4. Playwright: zakładka Faktury/Depozyt, desktop + 390px, `browser_console_messages(error)` puste,
   zrzuty w `/config/playwright/`.
5. Część B: notatka w `ROADMAP_DEPOSIT_FEEDIN.md` z wynikiem (wyjaśnione / nie wyjaśnione +
   rekomendacja kontaktu z Tauronem), niezależnie od wyniku — nie blokuje wydania 0.43.0.
