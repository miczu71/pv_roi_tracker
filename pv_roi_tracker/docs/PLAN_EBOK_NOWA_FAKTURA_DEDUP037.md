# PLAN etap „dedup-037” → wydanie 0.56.2

Źródło: ROADMAP_EBOK_NOWA_FAKTURA.md, sekcja „ANALIZA ~13:30”. Repo `~/dev/pv_roi_tracker` (main).

## Korekta planu po przeczytaniu kodu (05.11, sesja kontynuacji)
- Krok 1(b) „nie odświeżaj stanu płatności przy porażce listy do zapłaty” **jest zbędny**: `payment_reminders.refresh_payment_state` scala wpisy (`new_state` startuje od starego stanu, `snapshot_documents` tylko dopisuje/aktualizuje sygnatury obecne w `docs`), więc brak faktury na liście NIE kasuje aktywnej płatności ani nie ustawia `paid`. Zamiast zmiany kodu: **test regresyjny** (aktywna płatność przeżywa odświeżenie listą bez tej faktury) + ostrzeżenie w wyniku sync.
- `ebok_sync` to closure w `main()` (nietestowalne wprost) — logika (c) idzie do funkcji modułowej `_ebok_missing_summary` obok `_ebok_missing_documents`.

## Zakres
1. `ebok_client.py`: retry listy „do zapłaty” (1 raz, 3 s, tylko `ConnectionError`/`RequestException`, nie `EbokLoginBlocked`/`EbokError`), `self.last_unpaid_error` (None gdy OK, inaczej `'<TypWyjątku>'` — sam typ, bez komunikatu).
2. `ebok_client.py`: `EbokClient.download_pdf_any_mode(pdf_url)` — `download_pdf`; przy `EbokLayoutChanged` próby z `docMode` ∈ {0, 2} (podmiana parametru w zapytaniu; pomija wartość równą oryginalnej); zwraca pierwszy poprawny PDF, inaczej rzuca ostatni błąd. Każda nieudana próba loguje się już przez `download_pdf` (zamaskowany kształt z `docMode`).
3. `main.py`: `_ebok_missing_summary(missing)` → lista `{'month': 'YYYY-MM'|'', 'sig': <sygnatura z cyframi→<n>>, 'name': doc_name, 'source': 'archiwum'|'do_zapłaty'}` (źródło z `status == 'Do zapłaty'`); w `ebok_sync`: log `INFO` tej listy, w `diag` pola `missing` (ta lista) i `unpaid_failed` (bool z `client.last_unpaid_error`), ostrzeżenie `WARNING` przy `unpaid_failed`; pobieranie PDF dla `missing` dokumentów ze statusem „Do zapłaty” przez `download_pdf_any_mode`, reszta jak dotąd.
4. `needs_training` w wyniku sync — **poza zakresem** (krok 1(d) „tylko jeśli prosty”: nie jest, wymaga zmiany `invoice_ingest`; zgłaszam jako otwarte).

## Wykonawca
Pliki: `pv_roi_tracker/ebok_client.py`, `pv_roi_tracker/main.py`, `tests/test_ebok_client.py`, `tests/test_main.py`, `tests/test_payment_reminders.py`.
Zmiany:
1. `ebok_client.py`: stała `UNPAID_RETRY_DELAY = 3`; w `__init__` `self.last_unpaid_error = None`; w `list_documents` wywołanie `list_unpaid_invoices` owinąć: ustaw `last_unpaid_error = None`; przy `requests.exceptions.RequestException` jeden retry po `time.sleep(UNPAID_RETRY_DELAY)` (import `time` jeśli brak); po ostatecznej porażce `last_unpaid_error = type(exc).__name__`, ostrzeżenie jak dotąd (log bez zmian), zwróć archiwum. `EbokError` (też `EbokLayoutChanged`) bez retry, ale też ustawia `last_unpaid_error`. `EbokLoginBlocked` przechodzi.
2. `ebok_client.py`: `download_pdf_any_mode` wg pkt 2 zakresu (`DOC_MODE_FALLBACKS = ('0', '2')`).
3. `main.py`: wg pkt 3 zakresu. Nie zmieniaj `refresh_payment_bar`, `_refresh_payments` ani `payment_reminders.py`.
4. Testy (neutralne fixture, bez prawdziwych numerów; `monkeypatch.setattr(ebok_client, 'UNPAID_RETRY_DELAY', 0)`):
   - retry: pierwsze `list_unpaid_invoices` rzuca `requests.exceptions.ConnectionError`, drugie zwraca dokument → w wyniku jest i `last_unpaid_error is None`; dwie porażki → samo archiwum, `last_unpaid_error == 'ConnectionError'`; `EbokLoginBlocked` przechodzi bez retry; `EbokLayoutChanged` → bez retry, `last_unpaid_error == 'EbokLayoutChanged'`.
   - `download_pdf_any_mode`: pierwsza odpowiedź `Status='Error'` (JSON), z `docMode=0` poprawny PDF → zwraca PDF, żądania: `docMode=1`, `docMode=0`; wszystkie błędne → rzuca `EbokLayoutChanged`, 3 żądania; oryginalny `docMode=0` nie jest próbowany drugi raz; pierwsza próba OK → 1 żądanie.
   - `_ebok_missing_summary`: maskuje cyfry w sygnaturze (`<n>`), źródło wg statusu, miesiąc z `issued_on` (`DD.MM.RRRR` → `YYYY-MM`, brak → `''`).
   - `test_payment_reminders.py`: `refresh_payment_state` ze stanem zawierającym niezapłaconą fakturę (z `due_date`) i listą `docs` bez niej → `select_active_payment(new_state, today)` nadal wskazuje tę fakturę.
Komenda testu: `cd ~/dev/pv_roi_tracker/pv_roi_tracker && PATH=$HOME/.local/bin:$PATH uv run --no-project --with pytest --with-requirements requirements.txt python -m pytest -q` (stan wyjściowy 1430 passed / 23 skipped).
DONE: testy zielone (≥1430 + nowe); nowe testy retry/any_mode/summary czerwone bez zmian w `ebok_client.py`/`main.py`.
NIE RÓB: zmian w `payment_reminders.py`, `invoice_store.py`, `invoice_ingest.py`, `invoice_parser.py`; bez zmiany logiki dedupu `known_signatures`; bez formatowania sąsiedniego kodu; bez prawdziwych numerów/płatnika w testach; bez commitu; bez wywołań sieciowych do Taurona.

## Rollback
`wt drop ~/dev/pv_roi_tracker dedup037` przed landem; po wydaniu: backup add-onu z `ha_manage_updates` (0.56.1) / `git revert`.

## Weryfikacja na żywo (od 06.11, limit logowań)
Jeden `POST api/ebok/sync` przez ingress; kryterium: `missing` nazywa brakujące dokumenty (potwierdzenie/obalenie hipotezy kolizji klucza `YYYY-MM`), `unpaid_failed` false, log prób `docMode` dla 037.
