# ROADMAP — dwie faktury rozliczeniowe w jednym miesiącu (zmiana taryfy)

Powód: 05.11.2026 przyszła częściowa faktura 2026-11 (02 dni, G12W), a po niej ma przyjść druga
(03–30.11, G13). Obie dostają klucz `2026-11`, więc `upsert` nadpisuje pierwszą (utrata danych,
pierwsza przestaje być „znana” → możliwa pętla importu).

Decyzja: druga faktura rozliczeniowa w miesiącu NIE nadpisuje pierwszej. Wcześniejsza (wg początku okresu,
przy braku daty: starsza w kolejności napływu) ląduje pod `YYYY-MM~cz~<numer>`; pod `YYYY-MM` zostaje
faktura obejmująca koniec miesiąca (źródło stawek/depozytu). Rekonsyliacja historic używa SUMY kWh części.

## E1 — magazyn + rekonsyliacja (fundament) — ZROBIONE, commit `6841a32` w /config, pytest 1454 passed, 23 skipped (nie wydane)
- `invoice_store.py`: `_period_start`, `upsert` przenosi poprzednią fakturę o innym numerze do `~cz~`
  (wraz z PDF); ten sam numer = nadpisanie w miejscu (idempotencja, także dla części);
  `filter_billing` wyklucza `~cz~`; `month_parts`/`combined_kwh` (suma kWh miesiąca).
- `historic_store.py`: `reconcile_pending_invoices`/`_invoice_kwh_diverged` i `main._invoice_reconcile`
  używają sumy kWh miesiąca; `'~' in key` już pomija części w kandydatach.
- Testy: obie faktury zachowane, obie „znane”, ponowny import tej samej = bez duplikatu, kolejność
  odwrotna (druga przed pierwszą), suma kWh w rekonsyliacji, regresja całego zestawu.
- DONE: pytest zielony, symulacja na kopii danych: 2 rekordy, 0021 znana.
- Rollback: revert commitu (rekordy `~cz~` ignorowane przez starszy kod poza `filter_billing` — przed
  wdrożeniem wydania brak takich rekordów w produkcji).

## E2 — UI/API (wartość widoczna) — ZROBIONE (kod + testy, pytest 1456 passed; podgląd Playwright po wydaniu)
- `web._build_invoices_data`: części jako zagnieżdżone wiersze pod miesiącem (jak korekty), `diff_*`
  liczone względem sumy kWh; `app.js` — etykieta „część miesiąca”.
- DONE: test web + podgląd w Playwright (po wydaniu na żywo, bo dane prod mają jedną fakturę).

## E3 — wydanie 0.56.5 (ostatni etap)
simplify → code-review (+ silent-failure-hunter, pr-test-analyzer, security) → release → weryfikacja.

### E3 — wyniki przeglądu (2026-11-05)
Naprawione przed wydaniem: usuwanie części/głównej (`promote_part`, re-rekonsyliacja z sumy zamiast
`restore_month`), PDF przy demote, ten sam początek okresu = nadpisanie (ponowne wystawienie), escape
numeru w app.js, log zamiast `pass` w `_build_invoices_data`.
Znane kompromisy (nie blokują): koszty/depozyt miesiąca tylko z faktury późniejszej, taryfa/cena w historic
z bazy; `_invoice_train`/`_retry_stubs` rekonsyliują pojedynczą fakturę (samonaprawa przy restarcie);
bezpieczny numerycznie, ale kolizja sufiksów `~cz~` przy numerach różniących się tylko znakami specjalnymi.

## Poza zakresem
`K1NBN567872/037` — błąd PDF po stronie eBOK, tylko ponawiać sprawdzanie (notatka pamięci).
