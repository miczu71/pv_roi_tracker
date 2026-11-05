# PLAN E1 — import faktur nierozliczonych (eBOK)

Cel: `list_documents` zwraca także faktury „do zapłaty” (nieobecne w archiwum `GetDocuments`), jako `EbokDocument(paid=False, due_date, pdf_url)`. Sync importuje je i `payment_reminders` widzi aktywną płatność. Kryterium na żywo: 3 faktury, suma 104,48 zł.

## Mechanika (z sond E0d–E0j i DevTools)
1. POST `LoadMoreInvoicesProducts?sid=T18&pp=<płatnik>` jak w `probe_invoice_list` (view-model z `serializedViewModel`, `startingIndex=0`, `searchTerm=''`, `PickupMediumType`). Odpowiedź JSON `{ResultHtml, TotalCount, ReturnedCount}`.
2. W `ResultHtml` element z `data-source-id` (GUID), `data-contractor-number-checksum` (64 hex), `data-api-url`. Parametry `sourceId`, `checkSum` czytane z tych atrybutów; `contractorNumber` = numer płatnika (`self._payer_id`).
3. GET `/api/sitecore/EbokInvoices/GetInvoices?sourceId&contractorNumber&checkSum&take=5&skip=N` → JSON `{status: "OK", html, totalCount}` (uwaga: klucze małymi literami, inaczej niż `Status` w archiwum). Paginacja skip += take, aż `skip >= totalCount` (limit MAX_PAGES).
4. `html` = wiersze `<tr><td>`: (1) `a[data-base]` — wartość to ścieżka `GetDocumentImage?...&docNumber&docType&...` (pdf_url = SERVICE + ścieżka) + tekst numeru, `div` z typem („Faktura”); (2) data wystawienia `DD.MM.RRRR`; (3) `-`; (4) termin `DD.MM.RRRR`; (5) `input[data-charge-id][data-charge-value]` + `div.amount` (kwota brutto „95,32 zł”) + link Payment.
5. Numery: `K1NBN…/037` (bez `T/K1/`) i `T/K1/BN…/0020/26`. `_SIGNATURE_RE` łapie tylko drugi; `normalize_signature` działa na obu.

## Wykonawca

Worktree: z `wt new` (repo ~/dev/pv_roi_tracker). Pliki: `pv_roi_tracker/pv_roi_tracker/ebok_client.py`, `pv_roi_tracker/tests/test_ebok_client.py`. Test: `cd <WORKTREE>/pv_roi_tracker && PATH=$HOME/.local/bin:$PATH uv run --no-project --with pytest --with-requirements requirements.txt python -m pytest -q`.

Zmiany (TDD, najpierw testy czerwone, neutralny fixture: numery `T/K1/XX000000/0020/26`, `K1NXX000000/037`, GUID `00000000-0000-0000-0000-000000000000`, hex `ab`*32, płatnik `123456`):
1. `parse_unpaid_source(result_html: str) -> Optional[dict]` (czysta funkcja): z pierwszego elementu z `data-source-id` zwraca `{'source_id','checksum'}` (checksum z `data-contractor-number-checksum`); brak któregoś atrybutu lub niepoprawny format (GUID / 64 hex, walidacja regexem) → None.
2. `parse_unpaid_invoices_html(html: str) -> list[EbokDocument]` (czysta funkcja): wiersze `<tr>` z `<td>`; `signature` = tekst linku numeru (spacje zwinięte), `doc_name` = tekst `div` typu, `issued_on` = data kolumny 2 w formacie DD.MM.RRRR, `due_date` = `_parse_date` kolumny 4, `amount_gross_pln` = `_to_float` z `div.amount` (usuń „zł”), `paid=False`, `status='Do zapłaty'`, `doc_type` = `docType` z `data-base` (domyślnie 'FV'), `pdf_url` = SERVICE + `data-base` (tylko gdy zaczyna się od `/api/sitecore/` — inaczej None, jak w `parse_documents_json`). Wiersz bez numeru pomijany. Użyj `html.parser` (HTMLParser), nie regexów po całym dokumencie.
3. `EbokClient.list_unpaid_invoices() -> list[EbokDocument]`: pobiera stronę Faktury (`self._get`), robi POST jak `probe_invoice_list` — wydziel wspólną część (budowa formularza) do prywatnej metody `_post_invoice_list(page_html) -> Optional[str]` i użyj jej w obu miejscach bez zmiany zachowania sondy (istniejące testy sondy muszą przejść bez zmian); `parse_unpaid_source`; paginacja `GetInvoices` (take=5, `self._get_retrying`/`_get` z ARCHIVE_TIMEOUT, MAX_PAGES), JSON `status != 'OK'` → `EbokLayoutChanged`. Brak `parse_unpaid_source` (np. użytkownik bez faktur do zapłaty) → `[]` bez błędu i log info. Brak numeru płatnika → EbokError.
4. `list_documents`: po pętli archiwum dołącz `list_unpaid_invoices()`; dedup po `normalize_signature(signature)` — wpis z archiwum wygrywa, nowy dodawany tylko gdy brak w archiwum. Błąd źródła nierozliczonych (`EbokError`, `requests` wyjątki) NIE psuje listy archiwum: loguj `logger.warning` z typem wyjątku i zwróć samo archiwum (ale `EbokLoginBlocked` przepuść).
5. `_SIGNATURE_RE`: dodaj drugi format `K1N[A-Za-z0-9]+/\d{3}` (z `\b`), tak by `T/K1/...` nadal pasowało; test na obu.

Testy (min.): parse_unpaid_source (ok / brak atrybutu / zły format), parse_unpaid_invoices_html (3 wiersze: dwa formaty numeru, kwota, termin, pdf_url, wiersz bez numeru), list_unpaid_invoices z `_SequenceSession` (paginacja 5+2 przy totalCount 7; brak źródła → []), list_documents dedup (archiwum wygrywa), błąd GetInvoices nie psuje archiwum, `_ebok_missing_documents`-podobny test: dokument tylko z listy nierozliczonych trafia do `missing` i do `payment_reminders.apply_refresh` jako aktywna płatność (jeśli prosto da się to przetestować w istniejących plikach testów main/payment_reminders; inaczej test w test_ebok_client).

DONE: pełny pytest zielony (≥1400 passed + nowe), wszystkie istniejące testy sondy bez zmian, nowe testy były czerwone przed zmianą.

NIE RÓB: nie ruszaj `main.py`, `payment_reminders.py`, `invoice_ingest.py`, `config.yaml`, CHANGELOG, wersji; nie commituj; nie dodawaj prawdziwych numerów/GUID/checksum/kwot do testów; nie wołaj sieci; nie zmieniaj `_is_paid_status`.
