# Plan: parser korekt Mój TAURON w pv_roi_tracker (0.55.2)

## Context
1.11.2026 07:30 import eBOK oznaczył `T/K1/…/0019/26` jako „wymaga treningu”
(`parse failed: Imported kWh (Pobrano z sieci) not found`). To **„Korekta do faktury VAT”**
w nowym układzie Mój TAURON: dotyczy faktury 0009/26 (XI 2025), PSE skorygowało RCEm, więc zmienił się
depozyt z okresów poprzednich 228,90 → 227,59 zł; „8. Do zapłaty” 232,52 → 233,83; delta brutto +1,31 zł.
Parser zna tylko stary szablon korekty („FAKTURA VAT KOREKTA”, „Pobrano z sieci”). Takie korekty RCEm
mogą przyjść za kolejne miesiące, więc parser ma je wczytywać sam.

Ustalenia z kodu:
- Korekta jest zapisywana jako `YYYY-MM~kor~…`, a `effective_by_month()` (`invoice_store.py:368`) nadpisuje
  w miesiącu **tylko** depozyt, `energy_sale_gross_pln`, `deposit_capped` i `amount_due_pln`.
  kWh z korekty nie trafiają do wyliczeń, więc `exp_total` musi się tylko nie wywracać.
- Nowy szablon ma w tekście sekcję „Przed korektą”, a po niej „Po korekcie”. Wzorce depozytu i
  `amount_due` (`invoice_parser.py:150–182`) już pasują, a `_field()` z ostatnim trafieniem bierze
  wartość skorygowaną. Brakuje tylko rozpoznania korekty i kilku etykiet.

## Zmiana (repo `/config`, gałąź `main`, plik `addons/pv_roi_tracker/pv_roi_tracker/pv_roi_tracker/invoice_parser.py`)
1. `_is_korekta` (l. 698): dodać `Korekta do faktury VAT` (IGNORECASE już działa, wystarczy alternatywa w regexie).
2. `_BUILTIN_PATTERNS['imp_total']`: dodać `Ilo.. energii pobranej po korekcie\s+([\d,]+)\s*kWh`.
3. `exp_total`: nowy szablon korekty nie ma podsumowania. Fallback tylko dla korekty: suma stref z bloku
   `Licznik energii elektrycznej \(oddanie\)` (`szczyt 42,0000`, `pozaszczytowa 62,0000`). Jeśli nie ma
   bloku → dotychczasowy `InvoiceParseError`.
4. `invoice_number`: dodać `Numer korekty\s+([\w/]+)` na początek listy.
5. Pola korekty (l. 1105–1112): `corrects_number` dodatkowo z `Dotyczy dokumentu numer\s+([\w/]+)`,
   `correction_reason` dodatkowo z linii po `Dlaczego skorygowali.my Twoj. faktur.\?`
   (litery z ogonkami jako `.`, bo pypdf zamienia „ł” na „ø”).

Testy: `tests/test_invoice_parser.py`, nowa klasa z tekstem fixture w stylu `_parse_text` (zanonimizowany:
numer `T/K1/XX000000/0019/26`, bez nazwiska, adresu, PPE, licznika, konta; kwoty mogą zostać). Sprawdza:
`doc_type=='korekta'`, `invoice_number`, `corrects_number`, rok/miesiąc 2025-11, `imported_kwh==523`,
`exported_kwh==104`, `deposit_previous_pln==227.59`, `prev_deposit_previous_pln==228.90`,
`amount_due_pln==233.83`, `correction_delta_pln==1.31`, reason zawiera „RCE”. Do tego regresja: dotychczasowe
testy korekt i faktur (cały plik) przechodzą.

## Wykonawca (paczka W1, „ryzyko”: depozyt/pieniądze → `sonnet-worker`, worktree z `wt new`)
- Pliki: `pv_roi_tracker/invoice_parser.py`, `tests/test_invoice_parser.py` (w `addons/pv_roi_tracker/pv_roi_tracker/`).
- Zmiana: punkty 1–5 i testy jak wyżej, TDD (najpierw czerwony test).
- Test: `cd addons/pv_roi_tracker/pv_roi_tracker && python -m pytest tests/ -q`
- DONE: nowa klasa testów zielona, cały `tests/` zielony, diff tylko w 2 plikach.
- NIE RÓB: zmian w `invoice_store.py`/`main.py`/`web.py`, bumpu wersji, commitów, danych osobowych w fixture.
Potem claimcheck + `verifier`.

## Wydanie i weryfikacja na żywo
1. `simplify` → `code-review` (+ `silent-failure-hunter`, `pr-test-analyzer`, security review) na `<baza>..HEAD`.
2. Skill `release`: bump 0.55.1 → 0.55.2, CHANGELOG (polski, neutralny), published release, aktualizacja przez
   `ha_manage_updates` z `backup=true`. Poza oknem 02:45–03:15.
3. Na żywo przez playwright-ha (ingress): `POST api/invoice/reparse` dla stuba
   `unparsed-<id>-ebok_<numer-korekty>_pdf` (albo „Trenuj/Ponów” w UI). Oczekiwane:
   rekord `2025-11~kor~…`, depozyt 227,59, brak „wymaga treningu”, `effective_by_month` dla 2025-11
   pokazuje 233,83 zł.
4. Log add-onu bez błędów, a jutro o 07:30 import eBOK bez nowego pusha.

## Rollback
Revert commita i wydanie 0.55.3 albo przywrócenie add-onu z backupu sprzed aktualizacji (`ha_manage_updates`
`backup=true`). Stub zostaje nietknięty, dopóki reparse się nie powiedzie.
