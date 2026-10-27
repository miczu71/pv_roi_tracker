# PLAN E1 — import faktur i płatności z JSON API „Mój TAURON” (wydanie 0.50.0)

Źródło prawdy: `ROADMAP_EBOK_MOJ_TAURON.md` (sekcja E0 = opis API, sekcja E1 = zakres). Ten plik to kontrakt dla wykonawcy.
Ryzyko → `sonnet-worker`, potem `verifier`. Ścieżki względem katalogu add-onu (`<WORKTREE>/pv_roi_tracker`).

## Wykonawca
Skille: `tdd`, `addon-conventions`. Test (z katalogu add-onu):
`PATH=$HOME/.local/bin:$PATH uv run --no-project --with pytest --with-requirements requirements.txt python -m pytest -q`
(baza przed zmianą: 973 passed, 27 skipped).

Pliki: `pv_roi_tracker/ebok_client.py`, `pv_roi_tracker/main.py`, `pv_roi_tracker/web.py`,
`tests/test_ebok_client.py`, `tests/test_main.py` (jeśli dotyczy), `tests/fixtures/ebok_documents.json`,
`tests/fixtures/ebok_archiwum.html`, `tests/fixtures/ebok_pdf.json` (nowe).

1. `ebok_client.py` — dokładnie jak w ROADMAP → E1 → pkt 1 (SERVICE `https://moj.tauron.pl`; `EbokDocument` z
   `pdf_url`, `status`, `doc_type`, bez `numeric_id`; `parse_client_checksum`, `select_payer`, `list_documents`
   stronami po 100 / limit 10 stron, `parse_documents_json`, `PAID_STATUSES`, `download_pdf(pdf_url)`;
   usunięcie osieroconych funkcji i stałych CSV/archiwum; `_mask` zostaje). Zachowaj: `login`, `_get`,
   `_get_retrying`, wyjątki, `normalize_signature`, `known_signatures`, `is_known`.
   `Url` z dokumentu bywa względny (`/api/sitecore/...`) — dołącz do `SERVICE`, gdy nie zaczyna się od `http`.
   Pola JSON dokumentu: `DocSignature, DocType, DocKindValue, IsDocumentCorrection, AmountGross, AmountNet,
   IssueDate, IssueDateFormatted, PaymentDate, ConsumeEnergy, Status, Url, SodId`. Daty w `PaymentDate`/
   `IssueDate` mogą mieć formę `DD.MM.YYYY` albo ISO — parser tolerancyjny (None gdy brak/„---”).
   `doc_name` zostaje (z `DocKindValue`), bo używa go diagnostyka.
2. `main.py` — ROADMAP → E1 → pkt 2: `_ebok_run` (date_from 2015-01-01, date_to dziś, bez komentarza o dataDo+365),
   `ebok_sync.body` (bezpiecznik `MAX_NEW_PER_SYNC = 5`, bez archiwum/`locate_ids`), jedno `ebok_diag()`
   (bez argumentu), usunąć `ebok_diag_page` i jego rejestrację. `refresh_payment_bar(docs)` zostaje.
   Wynik `body` bez kluczy `archive_page_rows`/`not_found_in_archive` (zostaje `not_found` → `failed_pdf`).
3. `web.py` — ROADMAP → E1 → pkt 3 (`/api/ebok/diag` bez `needle`; usunąć `diag-page`, setter i zmienną).
4. Testy — ROADMAP → E1 → pkt 4. Usuń testy usuniętych funkcji. TDD: najpierw czerwone.
- `DONE:` pełny pytest zielony (≥ baza − usunięte + nowe); `grep -rn "wyborKlienta\|archiwumFaktur\|podgladFaktury" pv_roi_tracker/ tests/` pusty;
  `grep -rn "numeric_id\|locate_ids\|probe_home\|diag_page" pv_roi_tracker/ tests/` pusty.
- `NIE RÓB:` zmian w `invoice_ingest.py`, `invoice_store.py`, `payment_reminders.py`, `static/*` (UI), `config.yaml`,
  `__init__.py` (wersja), `CHANGELOG.md`; commitów; prawdziwych danych w fixtures (płatnik `11111111`, sygnatury
  `T/K1/BN111111/00xx/26`, „Miasto A”, checksum `a`×64). Nie loguj się do Taurona.
