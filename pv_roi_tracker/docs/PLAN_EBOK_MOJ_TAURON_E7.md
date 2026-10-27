# PLAN E7 — wpłaty i kontrola depozytu (ryzyko „pieniądze” → `sonnet-worker`)

Specyfikacja źródłowa: `ROADMAP_EBOK_MOJ_TAURON.md`, sekcja „E3 — wpłaty i kontrola depozytu” + tabela sondy E0
(wiersz „Wpłaty”). Wydanie dopiero po E9 (0.53.0); E7 to commit(y) na `main`.

## Decyzje
- Format `GetPayments` znamy tylko z sondy E0 (nie ma nagranej odpowiedzi) → fixture `ebok_payments.json` jest
  sfabrykowany wg opisu pól; parser ma być tolerancyjny (brak pola = pominięty wpis, nigdy wyjątek) i logować
  nieznany `Type` raz (jak statusy dokumentów). Poprawność na żywo sprawdzę po wydaniu (`api/ebok/payment-status`).
- Błąd pobrania wpłat **nie psuje** joba płatności ani importu: `try/except` lokalny, log + brak zmiany `_payments`
  (poza `EbokLoginBlocked`, który idzie dalej do `_ebok_run`).
- Depozyt opisowo: `invoice_gross − settled` (bez zmian w `deposit.py`).
- `_payments` w `ebok_payment_state.json` to **dict** (nie lista): `build_reminder_notifications` robi `dict(entry)`
  po wszystkich wpisach stanu, a `select_active_payment` pomija wpisy bez `due_date`.

## Wykonawca

Pliki (wszystko w `pv_roi_tracker/` addonu; testy w `tests/`):

1. `pv_roi_tracker/ebok_client.py`
   - `@dataclass EbokPayment`: `date: Optional[date]`, `amount: Optional[float]`, `type: str`,
     `doc_number: str`, `settlements: list[tuple[str, Optional[float], Optional[float]]]`
     (= `(DocSignature, PaymentAmount, PaymentSettlementAmount)`).
   - `parse_payments_json(data) -> list[EbokPayment]` (czysta; wiersze bez `Details` → `settlements=[]`;
     `Type` spoza `{'Wpłata','Zwrot','Odpisanie'}` → `logger.info` raz, wiersz zachowany; daty przez `_parse_date`,
     kwoty przez `_to_float`).
   - `EbokClient.list_payments(date_from, date_to) -> list[EbokPayment]`: wymaga `self._payer_id`; GET
     `{SERVICE}/Home/Pages/lista-wplat` przez `self._get` → `parse_client_checksum(html, payer_id)` (brak =
     `EbokError`); potem strony jak w `list_documents`:
     `GET {SERVICE}/api/sitecore/EbokPaymentsHistory/GetPayments?rangeFrom&rangeTo&clientNumber&clientNumberCheckSum&system=T18&from&to&docKind=All&paymentFrom=&paymentTo=`
     (daty `DD.MM.YYYY`, `PAGE_SIZE`, `MAX_PAGES`, `_json_or_layout_error`, `TotalNumber`).
2. `pv_roi_tracker/payment_reminders.py`
   - `reconcile_payments(docs, payments, invoice_records) -> dict` (czysta):
     `settled_sum[sig]` = suma trzeciego elementu `settlements` po wszystkich wpłatach typu `Wpłata`/`Odpisanie`,
     minus `Zwrot`; zwraca `{'last_payment': {...}|None, 'mismatches': [...], 'rescued': [sig,...], 'settled': {sig: kwota}}`.
     - `rescued`: dokument `not doc.paid` ze `settled_sum ≥ amount_gross_pln − 0,10` → uznany za zapłacony.
     - `mismatches`: rekord z `invoice_records` (po `normalize_signature(invoice_number)`), dla którego
       dokument jest zapłacony (lub w `rescued`) i `|settled_sum − amount_due_pln| > 0,10`
       → `{'signature','settled','amount_due','diff'}`. Rekord bez `amount_due_pln` pomijany.
     - `last_payment`: najnowsza po `date` wpłata typu `Wpłata` → `{'date': ISO, 'amount', 'signatures': [...], 'deposit_covered': {sig: gross−settled}}`
       (`deposit_covered` tylko dla `gross − settled > 0,01`).
   - `apply_reconciliation(state_path, result, today) -> list[str]`: pod `_refresh_lock`; zapisuje
     `state['_payments'] = {'last_payment','mismatches','checked_at': today.isoformat(), 'notified': [...]}`;
     zwraca komunikaty push wyłącznie dla rozbieżności, których klucz (`signature`) nie był jeszcze w `notified`
     (potem dopisuje go do `notified`; rozbieżność, która zniknęła, wypada z `notified`).
     Dla `rescued` ustawia `state[sig]['paid'] = True` (jeśli wpis istnieje).
   - `select_active_payment` bez zmian zachowania, ale test dowodzi, że klucz `_payments` jest pomijany.
3. `pv_roi_tracker/main.py`
   - Funkcja pomocnicza w zasięgu `create_app`-owego bloku z `ebok_sync`/`ebok_payment_check`
     (obok `_send_payment_notes`): `refresh_payments(client, docs)` — `try`: `client.list_payments(EBOK_DATE_FROM`
     ograniczony do 13 mies. wstecz`, today)` → `reconcile_payments(docs, payments, invoice_store.load(INVOICE_PATH))`
     → `apply_reconciliation(...)` → `_notify_ha('PV ROI Tracker — wpłaty', msg, target='kacper')` dla każdego
     komunikatu. `except _ec.EbokLoginBlocked: raise`; `except Exception: logger.exception(...)`
     + `_record_job('ebok_payment', False, 'odczyt wpłat nie powiódł się')` — bez przerywania.
   - Wywołanie w `ebok_payment_check.body` po `apply_refresh` oraz w `ebok_sync.body` po `refresh_payment_bar(docs)`
     (ta sama sesja, bez dodatkowego logowania).
   - `_ebok_payment_status()` dokłada klucze `last_payment` i `mismatches` z `state.get('_payments', {})`.
4. `pv_roi_tracker/static/index.html` + `static/app.js`
   - `<div id="lastPaymentLine" class="payment-last" style="display:none"></div>` pod `#paymentBar`;
     `_refreshPaymentBar` wypełnia go (`textContent`, nie `innerHTML`):
     `Ostatnia wpłata Y zł (DD.MM) → <sygnatury> · depozyt pokrył X zł` (część po `·` tylko gdy `deposit_covered`);
     przy `mismatches` druga linia `⚠ Rozbieżność wpłat: <sygnatura> rozliczono A zł, na fakturze B zł`.
     Brak `last_payment` → ukryty. Styl: klasa `payment-last` w istniejącym pliku CSS, neutralny kolor (nie alarm).
5. `tests/fixtures/ebok_payments.json` (neutralne: płatnik `11111111`, sygnatury `T/K1/BN111111/00xx/26`):
   wpłata częściowa z depozytem, zwrot, wpłata rozliczająca 2 faktury, wiersz z `Type` spoza listy.
6. Testy (kolejność TDD: najpierw czerwone):
   - `tests/test_ebok_client.py`: `parse_payments_json` (mapowanie, brak `Details`, nieznany `Type` logowany raz,
     śmieci → `[]`); `list_payments` z `FakeSession` (checksum z `lista-wplat`, URL `GetPayments`, paginacja,
     brak płatnika → `EbokError`, `Status != OK` → `EbokLayoutChanged`).
   - `tests/test_payment_reminders.py`: `reconcile_payments` (suma, zwrot odejmuje, `rescued`, rozbieżność,
     brak `amount_due_pln`, `deposit_covered`), `apply_reconciliation` (push raz, ponowny run bez pusha,
     zniknięcie rozbieżności czyści `notified`, `rescued` ustawia `paid`), `select_active_payment` pomija `_payments`,
     `build_reminder_notifications` nie wywala się na wpisie `_payments`.
   - `tests/test_main.py`: `ebok_payment_check` z podstawionym klientem — błąd `list_payments` nie zmienia `ok`
     joba i nie rzuca; wynik trafia do `/api/ebok/payment-status`.
7. `config.yaml` i `__version__`: **NIE ruszać** (wersja 0.53.0 po E9).

Komenda testu (z katalogu worktree, podkatalog `pv_roi_tracker/`):
`PATH=$HOME/.local/bin:$PATH uv run --no-project --with pytest --with-requirements requirements.txt python -m pytest -q`

`DONE:` pełny zestaw testów zielony (baza 1089 passed, 23 skipped + nowe, 0 failed); fixture bez danych osobowych;
`git diff --stat` zawiera wyłącznie pliki z listy powyżej; raport workera podaje liczby testów i listę zmienionych plików.

`NIE RÓB:` nie zmieniaj `deposit.py`, `invoice_store.py`, `config.yaml`, `__version__`, żadnych plików `tariff_*`
ani `jit_schedule.py`; nie dodawaj zależności; nie loguj płatnika, loginu ani kwot z konkretnych wpłat poniżej
poziomu `debug`; nie commituj; nie wykonuj połączeń sieciowych (testy tylko na `FakeSession`).

## Rollback
`git revert` commitów E7. Klucz `_payments` jest ignorowany przez 0.52.0 (test `select_active_payment`), więc
stan na dysku nie wymaga czyszczenia.
