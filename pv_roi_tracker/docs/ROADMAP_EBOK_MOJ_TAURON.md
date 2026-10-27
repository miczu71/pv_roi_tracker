# PV ROI Tracker — eBOK na „Mój TAURON”: faktury, płatności, taryfa (G13), wpłaty

Zastępuje poprzednią wersję (E0–E2 sprzed sondy portalu). Plan zatwierdzony 27.10.2026.

## Context
Od 26.10.2026 joby eBOK padają: Tauron przeniósł eBOK na portal Sitecore `moj.tauron.pl`, a stare
`/wyborKlienta`, CSV `archiwumFaktur` i archiwum HTML `/podgladFaktury/id/` zwracają stronę główną.
Zainstalowana jest 0.49.3, która dodaje tylko diagnostykę. Cel usera: działający produkt, czyli
import faktur, przypomnienia o płatności, taryfa z kontrolą oraz wpłaty z kontrolą depozytu.
Zakres zatwierdził user: „Taryfa + kontrola” oraz „Wpłaty i depozyt”. Historia odczytów i kWh z eBOK
nie wchodzą.

**E0 (sonda) jest zrobiony.** 27.10 przejrzałem portal raz, headless (Playwright + `secret-run tauron`,
jedno logowanie, sesja w scratchpadzie). Ustalenia:

| Co | Jak |
|---|---|
| Logowanie | bez zmian: Keycloak `kc-form-login` na `logowanie.tauron.pl/realms/ext/protocol/cas/login`, tylko `service=https://moj.tauron.pl` → redirect `?ticket=` → ciasteczka sesji |
| Suma kontrolna klienta | HTML `GET /Home/Pages/archiwum-dokumentow`: `<div data-client-number="<płatnik>" data-client-checksum="<64 hex>" data-system="T18">` (osobno dla każdego punktu; 3 punkty, aktywny = płatnik z opcji) |
| Lista dokumentów | `GET /api/sitecore/EbokArchiveDocuments/GetDocuments?rangeFrom=1&rangeTo=300&clientNumber=..&clientNumberCheckSum=..&system=T18&from=DD.MM.YYYY&to=DD.MM.YYYY&docKind=All&docNumber=` → JSON `{Status:"OK", TotalNumber, RangeFrom, RangeTo, Documents:[{DocSignature, DocType (FV/FK/NB), DocKindValue, IsDocumentCorrection, AmountGross, AmountNet, IssueDate, PaymentDate, ConsumeEnergy, Status ("Opłacona"/"Rozliczono"), Url, SodId}]}`. Filtr `from/to` = **data wystawienia**, więc problem `dataDo` znika. Od 01.01.2015 jedno zapytanie zwróciło 78 dok. (najstarszy 29.08.2023) |
| PDF | `GET` `Url` z dokumentu (`/api/sitecore/EbokArchiveDocuments/GetDocumentImage?...&mode=1`) → JSON `{Status:"OK", Document:"<base64 PDF>"}` |
| Taryfa | strona główna `GET /`: `<dl class="dl-horizontal"><dt>Umowa</dt><dd>…</dd><dt>Taryfa</dt><dd>G12W</dd><dt>Faktura</dt><dd>E-FAKTURA</dd></dl>`. Szczegóły punktu: link `/en/Home/Pages/umowa/punkty-poboru?sid=T18&cid=<płatnik>&checksum=..` z `GET /Home/Pages/Umowa?sid=T18&pp=<płatnik>` daje Taryfę, Moc umowną (5 kW), Sposób rozliczenia, OSD |
| Wpłaty | checksum z HTML `GET /Home/Pages/lista-wplat` (inny niż w archiwum!) → `GET /api/sitecore/EbokPaymentsHistory/GetPayments?rangeFrom&rangeTo&clientNumber&clientNumberCheckSum&system=T18&from&to&docKind=All&paymentFrom=&paymentTo=` → JSON `Documents:[{Amount, Date, Type ("Wpłata"/"Zwrot"/"Odpisanie"), DocNumber, Details:[{DocSignature, PaymentAmount (brutto faktury), PaymentSettlementAmount (rozliczone wpłatą)}]}]`. Np. 00xx/26: brutto X, wpłata Y, czyli depozyt pokrył X−Y |

Nieznana jest jeszcze wartość `Status` dla faktury niezapłaconej, bo wszystkie są opłacone. Pierwsza
okazja to faktura październikowa, ok. 5–8.11.2026.

## Zasady wspólne
- Repo: `/config/addons/pv_roi_tracker/pv_roi_tracker` (mirror) + wydania z `~/dev/pv_roi_tracker`
  (dual-push, notatka `feedback_pv_roi_release_checklist`: bump **obu** plików wersji).
- Gałąź `main`. Bez commitów w oknie 02:45–03:15.
- Repo jest PUBLIC, więc fixtures są neutralne: płatnik `11111111`, sygnatury `T/K1/BN111111/00xx/26`,
  adres „Miasto A”. Bez e-maila, PESEL i numerów PPE ani licznika. Diagnostyka maskuje płatnika i e-mail.
- Jedno logowanie na job; POST logowania nigdy nie jest ponawiany (blokada konta).
- Testy: `cd /config/addons/pv_roi_tracker/pv_roi_tracker && PATH=$HOME/.local/bin:$PATH uv run --no-project --with pytest --with-requirements requirements.txt python -m pytest -q`
  (baza: 973 passed, 27 skipped).
- Skille: `tdd`, `addon-conventions`. Workerzy w worktree z `wt new`, potem `verifier` (z claimcheck).
- Pierwszy krok implementacji: skopiować ten plan do `docs/ROADMAP_EBOK_MOJ_TAURON.md` (zastępuje stary
  E0–E2) i do `docs/PLAN_EBOK_MOJ_TAURON_E1.md` (sekcja Wykonawca).

## E1 — import faktur i płatności z JSON API (ryzyko → `sonnet-worker`; **osobne wydanie 0.50.0 od razu**)
**Wykonawca** (pliki: `ebok_client.py`, `main.py`, `web.py`, `tests/test_ebok_client.py`,
`tests/test_main.py` jeśli dotyczy, `tests/fixtures/ebok_*` (nowe), `CHANGELOG.md`):
1. `ebok_client.py`:
   - `SERVICE = 'https://moj.tauron.pl'`. Logowanie, `_get`, ponowienia i wyjątki bez zmian.
   - `EbokDocument`: `numeric_id` → `pdf_url: Optional[str]`, dochodzi `status: str` i `doc_type: str`.
   - nowe `select_payer(payer_id)`: GET `/Home/Pages/archiwum-dokumentow` → `parse_client_checksum(html, payer_id)`
     (czysta funkcja, regex na `data-client-number`/`data-client-checksum`). Brak →
     `EbokError('Płatnik … nie znaleziony na liście punktów')`. Zapamiętuje `self._checksum`.
   - `list_documents(date_from, date_to)`: GET `GetDocuments` stronami po 100 (`rangeFrom/rangeTo`,
     do `TotalNumber`, limit stron 10), daty `DD.MM.YYYY`. `Status != "OK"` albo nie-JSON →
     `EbokLayoutChanged`. Parsowanie: `parse_documents_json(dict) -> list[EbokDocument]` (czysta).
   - `paid = Status in PAID_STATUSES` (`{'Opłacona', 'Rozliczono', 'Zapłacona'}`). Każdy inny status
     z terminem płatności = niezapłacony, a nieznane statusy trafiają do `logger.info` (żeby poznać
     słownik). `due_date` z `PaymentDate`, `issued_on` z `IssueDateFormatted`.
   - `download_pdf(pdf_url) -> bytes`: GET → JSON → `base64.b64decode(Document)`. Wynik musi zaczynać
     się od `%PDF`, inaczej `EbokError`.
   - Usunąć osierocone: `select_client`, `find_client_id`, `probe_home`, `archive_page_ids`, `diagnose`,
     `locate_ids`, `parse_documents_csv`, `find_missing_signatures`, `parse_archive_html`,
     `summarize_client_page`, `parse_client_id_for_payer`, `summarize_home_page`, `payer_html_context`,
     stałe CSV/archiwum. `_mask` zostaje dla diagnostyki. Docstring modułu opisuje nowe API.
2. `main.py`:
   - `_ebok_run`: `date_from = 2015-01-01`, `date_to = dziś` (filtr po dacie wystawienia).
   - `ebok_sync.body`: brakujące = dokumenty z `not is_known(sig, known)`. **Bezpiecznik:** więcej niż
     `MAX_NEW_PER_SYNC = 5` brakujących → nic nie importuj, `_record_job('ebok', False, …)` i push
     z listą sygnatur. Inaczej `download_pdf(doc.pdf_url)` → `ingest_pdfs` jak dziś. Usunąć ścieżkę
     archiwum i `locate_ids`.
   - `ebok_diag` i `ebok_diag_page` zastąpić jednym `ebok_diag()` (tylko odczyt): `total`, 3 najnowsze
     sygnatury ze statusem, zbiór statusów, `unknown` (co zaimportowałby sync), maskowanie.
3. `web.py`: `GET /api/ebok/diag` bez `needle`, bez `diag-page` (usunąć route i setter).
4. Testy TDD na fixture'ach: `tests/fixtures/ebok_documents.json` (FV opłacona, FK „Rozliczono”,
   FV ze statusem nieznanym i terminem), `ebok_archiwum.html` (fragment z 2 punktami), `ebok_pdf.json`
   (base64 małego `%PDF-1.4`). Dochodzi fake session na stronicowanie (TotalNumber > 100), checksum
   nieznaleziony, `Status != OK` i bezpiecznik >5.
- `DONE:` wszystkie testy zielone; `grep -n "wyborKlienta\|archiwumFaktur\|podgladFaktury" pv_roi_tracker/` pusty.
- `NIE RÓB:` zmian w `invoice_ingest.py`, `invoice_store.py`, `payment_reminders.py` (interfejs
  `signature/due_date/paid/amount_gross_pln` zostaje), w UI, wersji ani commitów. Żadnych prawdziwych
  numerów z portalu w fixtures.

**Wydanie i weryfikacja (ja):** simplify → code-review + silent-failure-hunter + pr-test-analyzer + security
(równolegle) + second-opinion (pomiar 5/10) → release 0.50.0 (`ha_manage_updates install`, `backup=true`).
Potem na żywo przez `playwright-ha` (iframe ingress):
1. `GET api/ebok/diag`: `total ≈ 78`, `unknown` ≤ 5. **Gdy > 5: stop i pytam usera przed sync.**
2. `POST api/ebok/sync` → `ok`, „brak nowych dokumentów” (albo import ≤5 znanych starych).
3. `GET api/ebok/payment-status` → brak aktywnej płatności (wszystko opłacone).
4. Następnego dnia sprawdzam logi joba 07:00 (`haiku-scout`): `ebok_payment` ok, bez pushy o błędzie.

Rollback: `ha_manage_updates` przywraca backup add-onu 0.49.3; w repo `git revert` commitów E1.

## E2 — taryfa i kontrola (`haiku-worker`; commit, wydanie łączone z E3)
- `ebok_client.read_contract(payer_id) -> dict`: GET `/` → `parse_home_contract(html)` (`Umowa`,
  `Taryfa`, `Faktura` z `dl-horizontal`), potem GET `/Home/Pages/Umowa?sid=T18&pp=<płatnik>` → link
  `punkty-poboru` → `parse_point_details(html)` (`Taryfa`, `Moc umowna`, `Sposób rozliczenia`,
  `Nazwa OSD`). Bez numeru licznika i PPE. Błąd szczegółów punktu nie psuje wyniku (bierzemy samą
  stronę główną).
- `main.ebok_payment_check.body` (07:00, to samo logowanie): `contract = client.read_contract(...)`
  → `state['contract'] = {..., 'checked_at'}`.
  - zmiana któregokolwiek pola względem poprzedniego → push `notify.kacper` „Tauron: zmiana umowy/taryfy:
    G12W → …”;
  - `taryfa.lower() != tariff_job.BASELINE_TARIFF.lower()` → status `tariff_mismatch: true` i push raz
    (flaga w stanie), bo kalkulacje add-onu zakładają G12w.
- `_ebok_status()` zwraca `contract` i `tariff_mismatch`. UI (`static/app.js`, sekcja „eBOK i faktury”):
  linia „Taryfa G12W · moc 5 kW · rozliczenie co 1 mies. · sprawdzono <data>” i ostrzeżenie przy
  niezgodności. Styl istniejących linii `ebokMsg`; mała zmiana, `impeccable` tylko do weryfikacji
  wyglądu.
- Testy: czyste parsery na neutralnych fixture'ach HTML; logika „zmiana/niezgodność → push raz” jako
  czysta funkcja `contract_changes(prev, cur, baseline)`.
- `DONE:` testy zielone; `GET api/ebok/status` lokalnie w teście zwraca `contract`.
- Rollback: revert commitu (stan `contract` w `ebok_state.json` jest ignorowany przez starszy kod).

## E3 — wpłaty i kontrola depozytu (ryzyko „pieniądze” → `sonnet-worker`; potem wydanie 0.51.0 z E2)
- `ebok_client.list_payments(date_from, date_to)`: GET `/Home/Pages/lista-wplat` → checksum (ten sam
  parser co E1, inna strona) → `GetPayments` stronami → `parse_payments_json` →
  `EbokPayment(date, amount, type, settlements=[(signature, invoice_gross, settled)])`.
- `payment_reminders`: nowa czysta `reconcile_payments(docs, payments, invoice_records)`:
  - `settled_sum[sig]` = suma `PaymentSettlementAmount`;
  - dokument z nieznanym statusem, ale z `settled_sum ≥ amount_due_pln − 0,10` → traktuj jako
    zapłacony (zapasowe źródło prawdy dla przypomnień);
  - dla rekordów z `invoice_store` (`amount_due_pln` z PDF): `|settled_sum − amount_due_pln| > 0,10`
    przy statusie zapłaconym → rozbieżność (lista);
  - zwraca `last_payment` (data, kwota, sygnatury) i `mismatches`.
- `main`: w `ebok_payment_check` i `ebok_sync` (to samo logowanie) zakres 13 mies. Wynik do
  `ebok_payment_state.json` pod kluczem `_payments` (bez kolizji z sygnaturami; `select_active_payment`
  pomija klucze z `_`). Nowa rozbieżność → push raz.
- `_ebok_payment_status()` + UI: „Ostatnia wpłata Y zł (19.10) → 00xx/26 · depozyt pokrył X−Y zł”.
  Depozyt liczę jako `invoice_gross − settled`, opisowo, bez zmian w `deposit.py`.
- Testy: fixture `ebok_payments.json` (wpłata częściowa z depozytem, zwrot, wpłata na 2 faktury),
  rozbieżność, nieznany status uratowany wpłatą, klucz `_payments` ignorowany przez `select_active_payment`.
- `DONE:` testy zielone; dla 00xx/26 w fixture'ze analogicznym: depozyt = gross − settled.
- Rollback: revert; klucz `_payments` jest ignorowany przez 0.50.0 (sprawdzić w teście).

**Wydanie po E3:** simplify → code-review + silent-failure-hunter + pr-test-analyzer + security +
second-opinion (6/10) → release 0.51.0 → na żywo: `api/ebok/status` (taryfa G12W, moc 5 kW, brak
niezgodności), `api/ebok/payment-status` (ostatnia wpłata Y → 00xx/26, bez rozbieżności), screenshot
sekcji eBOK w UI. Notatki pamięci: aktualizacja `project_ebok_moj_tauron` (nowe API, bez danych
osobowych) i wpis w MEMORY.md (`-config`).

## STATUS (27.10.2026)
- **E1 ZROBIONE i wydane jako 0.50.0** (commity dev: b6a7979 worker → 6168ceb simplify → 254f942 review → ade2a23 → 627ee56 bump; tag `v0.50.0`, release published; mirror `/config` e1ec5a0). Testy 976 passed, 23 skipped.
- Na żywo (12:2x): `api/ebok/diag` total 78, statusy {Opłacona, Rozliczono}, `unknown` 1 (faktura 2024-01); `POST api/ebok/sync` → ok, imported `2024-01`, bez błędów; `payment-status` → `active: null`.
- Ulepszenie ponad plan: pusty `ebok_payer_id` wybiera jedynego klienta konta (kilku → błąd z prośbą o ustawienie opcji).
- Pominięte: second-opinion (diff 77 kB > limit 60 kB; pomiar zostaje 4/10).
- Do obserwacji: jutro logi joba 07:00 (`ebok_payment` ok, brak pushy o błędzie); status niezapłaconej faktury ok. 5–8.11.
- Znane długi nieblokujące (review): paginacja liczy przefiltrowany batch, brak dedupu po sygnaturze, `parse_documents_json` bez walidacji typów, `/api/ebok/diag` bez sprawdzenia `blocked_until`, stare docstringi, maskowanie checksum w wyjątkach `requests`; bezpiecznik >5 liczy też FK/NB.
- **E2 ZROBIONE (28.10 w repo, niewydane; wydanie łączone z E3+)**: plan `docs/PLAN_EBOK_MOJ_TAURON_E2.md`; commit dev 20afef3 (haiku-worker, verifier ACCEPT). `ebok_client.read_contract` + parsery, `tariff_history.py` (`/data/tariff_periods.json`), job 07:00 zapisuje `state['contract']` i pushuje zmianę taryfy, czujnik `current_tariff`, opcja `current_tariff_override` (schema `str?`). Testy 1000 passed, 23 skipped. Data zmiany = dzień pierwszego wykrycia (decyzja domyślna).
  - Długi nieblokujące E2: uszkodzony `tariff_periods.json` → `load()=[]` i nadpisanie historii; `state['contract']` z numerem umowy trafia do `/api/ebok/status`; ścieżka błędu `read_contract` w `ebok_payment_check` bez testu; schema bez `list()`.
- **E3 ZROBIONE (27.10 w repo, niewydane; wydanie łączone z E4+)**: plan `docs/PLAN_EBOK_MOJ_TAURON_E3.md`; commity dev e5da0e0 (W1 backend, sonnet-worker, verifier ACCEPT) i 7a44f9a (W2 UI, haiku-worker, verifier ACCEPT). Baza porównań = bieżąca taryfa (`compute_payload(baseline, tariff_periods)`, `baseline_tariff`, `saving_vs_baseline*` + aliasy `saving_vs_g12w*`), `_reconcile` wg taryfy miesiąca (miesiąc ze zmianą pomijany), `GET/PUT /api/tariff_periods` + `validate_periods`, 5 nowych sensorów MQTT (`tariffsim_baseline_tariff`, `tariffsim_{month,12m,12m_c}_baseline`, `tariffsim_saving_vs_baseline`), UI: baza zamiast G12w w zakładkach Taryfa/Symulacja + edytor okresów w „eBOK i faktury”. Testy 1037 passed, 23 skipped. Nie sprawdzone na żywo (po wydaniu: edytor, 400 w ebokMsg, odświeżenie symulacji).
  - Długi nieblokujące E3: `validate_periods` zapisuje surowy `from` (nie `isoformat()`); klucz `g12w` w `recommend()` mylący przy innej bazie; `loadTsimTab()` po PUT bez odpytywania (przeliczenie w tle); tytuł „Twoja taryfa G12w” widoczny do załadowania katalogu; `app.js:4853` fallback G12w; baza dynamiczna (dyn_a/b) nieobsłużona w zakładce Taryfa (nieosiągalne: baza z katalogu).
- **E4 ZROBIONE (27.10 w repo, niewydane; wydanie łączone z E5+)**: plan `docs/PLAN_EBOK_MOJ_TAURON_E4.md`; commit dev 6902562 (sonnet-worker + 3 poprawki rodzica, verifier ACCEPT). `tariff_zones.peak_fn(periods, catalog)` (strefa szczytu wg taryfy dnia; brak okresów = G12w), `is_peak_fn` w `battery_sim.simulate_s1/simulate_all` i `heatpump.*`, `dist_tariff` (= baza) w `tariff_sim`/`tariff_battery`/`compute_payload` dla dyn_a/dyn_b. Testy 1052 passed, 23 skipped. Nie sprawdzone na żywo (po wydaniu: `api/battery_sim`, `api/heatpump` bez zmian liczb dla G12w).
  - Poza E4 (świadomie): 3-strefowe ceny faktyczne i `tariff_config` dopiero po pierwszej fakturze G13; do tego czasu G13 w kosztach faktycznych = 2 strefy (szczyty rano+wieczór = peak) z cenami z `tariff_config` — wpis G13 w `tariff_config` robi user (bez niego koszty po zmianie liczą się starymi cenami G12w).
  - Długi nieblokujące E4: G12 `day` = szczyt w `peak_fn`, ale `make_price_fn` dla G12 daje False (niespójność); błąd katalogu w `peak_fn` przerywa cały `battery_job`/`heatpump_job` (łapie `except`, log); zdublowane lokalne importy w `main.py`; zmienna `g12w` w `simulate_month`/`make_price_fn` trzyma teraz stawki `dist_tariff`; brak testu święta w `test_tariff_zones`; brak testu wiringu `main.py`.
- **0.51.0 (E2–E4) WYDANE 27.10 13:07**: tag `v0.51.0` published, mirror `/config` 36b1c37, dev b183f4f. Przed wydaniem: simplify, code-review, silent-failure-hunter, pr-test-analyzer, security (BLOKUJĄCE: brak); poprawki: host-guard `read_contract`, `EbokLoginBlocked` nie połykany, `dist_tariff` w wariancie C/`suggest_plans`/widoku godzinowym, baza w kluczu cache, testy wiringu `main.py`. Testy 1057 passed, 23 skipped. second-opinion pominięty (4/10).
  - Na żywo: instalacja przez `ha_manage_updates` (timeout narzędzia, Supervisor dokończył) → 0.51.0; logi startu czyste; `GET api/tariff_periods` → `periods: []`; `GET api/tariff_sim` → `baseline_tariff: G12w`, `saving_vs_baseline` = `saving_vs_g12w` = 144,82; w HA 5 nowych sensorów `*_baseline` (stan `baseline_tariff` chwilowo `unknown` z zapisanego payloadu 0.50.0 — sprawdzić po następnym pollu).
  - **Do sprawdzenia jutro po 07:00**: `ebok_payment_check` pierwszy raz czyta umowę (`read_contract`), zapisze `/data/tariff_periods.json` (G12W, `from: null`); `api/ebok/status` → `contract`; brak pushy o błędzie. Nie sprawdzone na żywo: edytor okresów w UI, `battery_sim`/`heatpump` (liczby dla G12w).
  - Długi z review (nieblokujące): `_periods_peak_fn` ignoruje `CURRENT_TARIFF_OVERRIDE`; wykrycie zmiany nie przelicza `tariff_sim_job` od razu; zapis `tariff_periods.json` bez blokady; `current_tariff_override` to `str?` (literówka ignorowana); `ebok_payment_check` raportuje ok mimo błędu odczytu umowy; brak testów body `ebok_payment`, `heatpump.compute` z `is_peak_fn`, `_ebok_status`; dystrybucja dyn jedna wartość na zakres.
- **E5 ZROBIONE (27.10, tylko HA; bez wydania add-onu)**: plan `docs/PLAN_EBOK_MOJ_TAURON_E5.md`. Zakres zawężony za zgodą usera do czystych porównań: dashboard `lovelace`, widok `energy`, karty „Koszt bieżącego miesiąca” i „Ostatnie 12 miesięcy” mają wiersze `*_baseline` (+ `baseline_tariff`) na górze, „G12w (obecna)” → „G12w”, a oszczędność roczna czyta `saving_vs_baseline`. Zapis przez `ha_config_set_dashboard` (config_hash `da26e585…` → `24e10d6c…`), backup kart w `/config/docs/dashboard_tariffsim_backup_2026-10-27.json`. Na żywo: baza 226,56 / 3 817,45 zł, oszczędność 144,82 zł (= dotychczasowe `*_g12w`), konsola bez nowych błędów (jedyny błąd to apexcharts-card poza zakresem zmiany). Poza HA-owymi kartami nic nie używało `tariff_sim_*`.
  - **Odroczone do dnia G13** (koszty faktyczne, nie baza): ~23 encje `*_g12w` w `packages/energy_simulation.yaml`, 4 `utility_meter`, automatyzacje `Taryfa`/`Zliczanie godzin…`/`Podsumowanie miesięczne taryf`, panel Energia → `/config/docs/ROADMAP_energy_panel_g13.md` (G1–G3, razem z licznikami 3-strefowymi). Bateria/JIT = E6.
  - Dług: alias `saving_vs_g12w` w publisherze (= baza, po zmianie na G13 myląca nazwa) do usunięcia przy następnym wydaniu add-onu; nikt go już nie czyta.
- **E6 ZATWIERDZONY 27.10 (w toku): JIT i Strażnik baterii na G13 z propozycji add-onu**; plan `docs/PLAN_EBOK_MOJ_TAURON_E6.md`, kontrakt E6a `docs/PLAN_EBOK_MOJ_TAURON_E6a.md`. Decyzje: hybryda (add-on podaje okna i opłacalność, ile ładować liczy JIT z Solcast), osobne automatyzacje „JIT G13”/„Strażnik G13” (v48 i stary Strażnik dostają warunek „taryfa ≠ G13”), opłacalność = marża na kWh mostu. Zamiast `current_zone` add-on publikuje `sensor.pv_roi_tracker_pv_jit_schedule` (atrybuty: szczyty i mosty na dziś/jutro dla G12w, G13, G13s; G12 pominięty przez dług E4).
  - E6a add-on 0.52.0 (`jit_schedule.py`, publisher, usunięcie aliasu `saving_vs_g12w`) → E6b HA w trybie cienia (zapis automatyzacji za akceptacją diffu) → E6c obserwacja 1–2 tyg. i przełączenie w dniu G13.
  - **E6a ZROBIONE i WYDANE jako 0.52.0 (27.10 14:26)**: commit dev `7f8a830` (sonnet-worker; claimcheck 0/4, verifier APPROVE). `jit_schedule.py` (`bridges`, `build_schedule`), sensor `jit_schedule` (discovery z `json_attributes_topic`, `publish_jit_schedule`), blok w `poll_and_publish` z osobnym `try/except` i `_record_job('jit_schedule')`, usunięty alias `saving_vs_g12w[_b|_c]` + tombstone `tariffsim_saving_vs_g12w`. Testy 1088 passed, 23 skipped. Nie sprawdzone na żywo.
    - Odstępstwa: G12 poza harmonogramem (G12w, G13, G13s); worker napisał kod przed testami (brak fazy czerwonej).
    - Długi nieblokujące E6a: `test_lookback_obciety_do_96h` nic nie dowodzi; brak ceny w oknie zostawia szczyt w `peaks` bez mostu; G13s w dni wolne ma szczyty z katalogu (mosty `profitable=false`); blok `jit_schedule` w `main.py` bez testu; ISO z TZ na osi naiwnej (DST).
    - **0.52.0 WYDANE 27.10 14:26**: tag `v0.52.0` published, dev `186a7ff`, mirror `1d11728`. Przed wydaniem: simplify (bez zmian), code-review, silent-failure-hunter, pr-test-analyzer, security (BLOKUJĄCE: brak); poprawka review `f4e70d5`: `LookupError` na końcu katalogu (31.12.2026 'jutro' poza zakresem) → godzina bez ceny, test `test_koniec_katalogu_nie_psuje_harmonogramu`. Testy 1089 passed, 23 skipped. second-opinion pominięty (4/10). pubguard 0 oflagowanych.
    - Na żywo: instalacja 0.52.0 (`homeassistant.update_entity` odświeżył sklep, install z `backup=true` — timeout narzędzia, Supervisor dokończył); `sensor.pv_roi_tracker_pv_jit_schedule` istnieje, stan `unknown` (current_tariff do 28.10 07:00), G13 zima dzień roboczy `profitable` = `[false, true]` (marże −0,029 / +0,563), G12w oba `true`; brak `tariffsim_saving_vs_g12w`; logi startu czyste.
    - Długi nieblokujące (review wydania): `overrides` nie przekazane do `build_schedule` (spójność z `TariffSettings`); `cheap = min(window)` optymistyczne dla długich okien (weekend); brak testów: `overrides`, granica marży = 0, negatyw aliasu `saving_vs_g12w`; `publish_jit_schedule` przy rozłączonym MQTT zapisuje job jako ok; generyczny detail błędu w health; retained sensor zostaje nieświeży po porażce (jest `generated_at`); brak znacznika `cfg_default`; publikacja bez change detection; brak parsera dla szczytu bez ceny na końcu zakresu (szczyt znika z `peaks`); `TARIFFS` zdublowane względem `tariff_sim`.
  - **E6b ZROBIONE (27.10 14:35, tylko HA; bez wydania add-onu)**: user zaakceptował diff. Backup v48 i Strażnika: `/config/docs/battery_g13_backup_2026-10-27.json` (z klona po sync 03:00, nie z odczytu na żywo); szkic: `/config/docs/battery_g13_draft_2026-10-27.yaml`. Utworzone: `input_text.jit_g13_cien`, `automation.bateria_jit_g13_cien_do_dnia_zmiany_taryfy` (id 1793108043739), `automation.bateria_straznik_g13_szczyt_z_harmonogramu_add_onu` (id 1793108047128). Warunek `lower(sensor.pv_roi_tracker_pv_current_tariff) != 'g13'` dodany do v48 (hash `76b5b4e3ada9e11c` → `8c920f3486b01b00`) i starego Strażnika (`08203c59cf60d08d` → `ec8f6994f3b116c8`); `unknown` przepuszcza → stara logika G12w.
    - Weryfikacja na żywo: szablony przetestowane `ha_eval_template` na żywych atrybutach (wybór mostu, deficyt Solcast); ręczny run „JIT G13” → `input_text.jit_g13_cien` = „27.10 14:35 cień: ładowałbym 2500 W do 100% (most 13:00–16:00, marża 0.56 zł/kWh)”, trace `finished`; falownik nietknięty (taryfa unknown → `is_g13` false), `ostatni_status_ladowania` zapisuje tylko v48 (G12w) jak dotąd. Strażnik G13 jeszcze nie wyzwolony (taryfa ≠ G13).
    - Nie sprawdzone: gałąź falownika (`forcible_charge`/`stop`) obu nowych automatyzacji — nieosiągalna do dnia G13; test dopiero w E6c (możliwy wcześniej przez `current_tariff_override=G13` — ryzyko sterowania domem, tylko za zgodą). Cień 2500 W/100% przy SoC 1% to chwilowy `srednie_zuzycie_domu_1h` (3,1 kW) — ta sama formuła co v47, do oceny w obserwacji.
    - Długi nieblokujące E6b: duplikat szablonu Solcast (świadomie); warunki/trigger szablonowe (ostrzeżenia best-practice); „JIT G13” nie ma `for` na starzejący się harmonogram poza 3 h limitem; stary Strażnik zostaje przy `lower != g13` także gdy sensor `unavailable`.
    - Rollback: wyłączyć 2 nowe automatyzacje, usunąć warunek z v48/Strażnika lub przywrócić z backupu JSON.
  - **Następny: E6c** (obserwacja w cieniu 1–2 tyg. `haiku-scout` + przełączenie w dniu G13), potem E7. **28.10 po 07:00:** `read_contract` pierwszy raz → `/data/tariff_periods.json`, `api/ebok/status` → `contract`, `current_tariff` ≠ unknown, brak pushy o błędzie. Handoff: `~/.claude/mods-data/auto-handoff/handoffs/2026-10-27-pv-roi-e6-E6a-release.md`.
- **KOLEJNOŚĆ PO E6b (user 27.10: „rób te 3 punkty teraz, z autohandoff”)** — E6c (obserwacja w cieniu + przełączenie w dniu G13) trwa w tle kalendarzowo i nie blokuje. Jedno wydanie **0.53.0** po E9 (etapy to commity). Każdy etap: plan etapu `docs/PLAN_EBOK_MOJ_TAURON_E<n>.md` z sekcją **Wykonawca** (pliki, zmiana, test, `DONE:`, `NIE RÓB:`) pisany na początku etapu po przeczytaniu kodu, potem worker + `verifier` + claimcheck, `stage-end`.
  - **E7 — wpłaty i depozyt** (ryzyko „pieniądze” → `sonnet-worker`): specyfikacja = sekcja „E3 — wpłaty i kontrola depozytu” wyżej (`ebok_client.list_payments` + `parse_payments_json` → `EbokPayment`; `payment_reminders.reconcile_payments`; klucz `_payments` w `ebok_payment_state.json`, ignorowany przez `select_active_payment`; push raz przy nowej rozbieżności; UI „Ostatnia wpłata Y zł → 00xx/26 · depozyt pokrył X−Y zł”). Fixture `ebok_payments.json` neutralny (płatnik `11111111`). Rollback: revert; `_payments` ignorowany przez 0.52.0 (test).
  - **E7 ZROBIONE (27.10, niewydane; commit dev `8f3578b`, plan `PLAN_EBOK_MOJ_TAURON_E7.md`)**: `sonnet-worker`, verifier APPROVE, testy 1121 passed / 23 skipped. `list_payments`/`parse_payments_json`, `reconcile_payments`/`apply_reconciliation` (klucz `_payments`), `_refresh_payments` w `ebok_sync` i `ebok_payment_check`, linia „Ostatnia wpłata” + rozbieżność w UI. **Nic nie sprawdzone na żywo**: format `GetPayments` (fixture sfabrykowany z sondy E0), znak rozliczenia dla `Zwrot`. Po wydaniu 0.53.0 sprawdzić `api/ebok/payment-status`.
    - Długi nieblokujące E7: `rescued` ustawia `paid` dopiero po `apply_refresh`, więc nie wycisza przypomnień (dotyczy faktury z nieznanym statusem portalu); `_record_job('ebok_payment', False)` w `_refresh_payments` nadpisywany w `ebok_payment_check`; `logger.exception` może nieść URL z checksum; `_plMoney(null)` → „0,00 zł”; brak testu ciała `ebok_payment_check` i endpointu HTTP.
  - **E8 — utwardzenie przed G13** (`haiku-worker`, drobne, TDD): (1) `tariff_history.load()` przy uszkodzonym `/data/tariff_periods.json` nie nadpisuje historii (kopia `.corrupt`, błąd w logu/health); (2) `_periods_peak_fn` uwzględnia `CURRENT_TARIFF_OVERRIDE`; (3) wykrycie zmiany taryfy od razu przelicza `tariff_sim_job`; (4) G12: spójność `peak_fn` ↔ `make_price_fn` (`day` = szczyt albo nie, jedno źródło); (5) `jit_schedule`: dla długich okien (weekend) `cheap` nie `min(window)` tylko średnia ważona godzinami okna ładowania, test; (6) `publish_jit_schedule` nie zapisuje joba jako ok przy rozłączonym MQTT. Poza zakresem: reszta długów z ROADMAP.
  - **E8 ZROBIONE (27.10, niewydane; commit dev `9fcd213`, plan `PLAN_EBOK_MOJ_TAURON_E8.md`)**: `haiku-worker`, verifier ACCEPT (blokujących brak), testy 1138 passed / 23 skipped. `tariff_history.load_status` (kwarantanna `.corrupt`, `ebok_payment_check` przy uszkodzeniu nie nadpisuje historii) + `with_override`; `_periods_peak_fn` uwzględnia override; zmiana taryfy od razu startuje `tariff_sim_job`; `tariff_zones.is_peak_zone` jako jedno źródło (**G12 `day` = szczyt także w `make_price_fn` → zmienia wyniki symulacji G12; odnotować w raporcie wydania**); `jit_schedule` `cheap` = średnia K=ceil(usable/power) najtańszych godzin; `publish_jit_schedule` → bool, job `jit_schedule` nie ok przy rozłączonym MQTT. Pominięte testy wiringu `ebok_payment_check` (closure w `main()`; tylko przegląd kodu).
  - **E9 — weryfikacja katalogu G13** (tylko odczyt + ewentualna korekta `tariff_catalog`): porównać strefy (zima/lato, dni robocze/wolne/święta) i ceny brutto G13 (i G13s) w katalogu add-onu z oficjalnymi dokumentami Tauronu (`docs-scout`, źródła z URL) oraz z umową usera; rozbieżności → raport + poprawka katalogu z testem. Ceny z umowy (ew. oferta) tylko od usera — nie zgaduję; brak dokumentu → pytanie do usera, nie zatrzymanie reszty.
  - **E9 ZROBIONE (27.10, tylko odczyt; plan `PLAN_EBOK_MOJ_TAURON_E9.md`)**: strefy i stawki G13/G13s w katalogu zgodne z oficjalną taryfą OSD i sprzedawcy 2026 (docs-scout, PDF URE/Tauron); bez poprawki katalogu. Otwarte: abonament dystrybucyjny 4,56 (katalog, z faktur) vs 2,28 (taryfa, rozliczenie 1-mies.) — do sprawdzenia przy pierwszej fakturze G13.
  - **Wydanie 0.53.0** po E9: `simplify` → `code-review` + `silent-failure-hunter` + `pr-test-analyzer` + security (równolegle) → `release` → weryfikacja na żywo (`api/ebok/payment-status`, `api/ebok/status`, `sensor.pv_roi_tracker_pv_jit_schedule`, UI sekcja eBOK).
- **Po E6: E7** (wpłaty/depozyt). Migracja kosztów faktycznych do G13 robi się w dniu wejścia taryfy wg `ROADMAP_energy_panel_g13.md`.

## ZMIANA ZAKRESU (user, 27.10, po odrzuceniu planu) — STAN ROBOCZY / HANDOFF
User: „będę zmieniał taryfę na G13. Gdy wykryjemy zmianę taryfy, wszystkie porównania mają pokazywać
bazę jako nową taryfę, zarówno w add-onie, jak i w HA”. E1 (naprawa eBOK) jest bez zmian i pilny.
E2 (taryfa) rośnie w wielostopniowy projekt.

### Ustalenia (Explore + ha_search, 27.10)
- **Add-on: brak ustawienia „bieżąca taryfa”.** G12w jest na sztywno:
  - `tariff_job.py:24 BASELINE_TARIFF`, `:149 _reconcile`, `:201-238` klucze `saving_vs_g12w*`, `:265-292` rekomendacje;
  - `web.py:1207-1211` `/api/tariff_now` na sztywno `'G12w'`;
  - dyn_a/dyn_b liczą dystrybucję wg G12w (`tariff_sim.py:118-288`, `tariff_battery.py:58-71`).
- **Strefy faktyczne:**
  - `battery_sim._is_peak_hour` (`:111-121`) to stały kalendarz G12w dla całego czasu, używany też przez `heatpump.py:36,179,247`;
  - koszt faktyczny jest 2-strefowy: `tariff_config` ma tylko peak/offpeak, a liczniki HA to `sensor.daily_energy_peak/offpeak` (`live_reader.py:66-67,592`). **G13 ma 3 strefy**, więc model trzeba rozszerzyć;
  - `models.MonthData.tariff` przyjmuje tylko `G11|G12W`, ustawia go `invoice_parser.py:377,1180`, a `historic_store.backfill_tariff:407-417` uzupełnia historię. Parser nie zna faktur G13.
- **Publisher → HA:** `tariffsim_month_g12w`, `tariffsim_12m_g12w`, `tariffsim_12m_c_g12w`, `tariffsim_saving_vs_g12w`
  (`publisher.py:87-107,166`) i `rate_peak/offpeak_gross` (opisane jako „cena G12w”).
- **UI:** `index.html:176,178,338,518`; `app.js:2159,3572-3592,3679,3759,4022-4065,4361,4505-4515,4595-4613,4665,4741-4760`.
- **HA (poza add-onem):**
  - 23 encje z g12w: `power_tauron_g12w_*`, `energy_cost_g12w_*`, `*_g11_vs_g12w`, `*_g12w_vs_dynamiczna`, `symulacja_miesieczna_g12w_faktura`, `spread_g12w_netto`, binary `dynamiczna_{tansza,drozsza}_niz_g12w*`;
  - 4 `utility_meter`: Difference G11 vs G12W, Energy cost G12W monthly total, `koszt_zmienny_g12w_miesieczny`, `symulacja_miesieczna_g12w_faktura`;
  - YAML: `packages/energy_simulation.yaml` (70 trafień), `sensors.yaml` (2), `packages/battery_arbitrage_package.yaml` (3), `packages/battery_jit_prediction.yaml` (2), `automations.yaml` (19);
  - automatyzacje: `bateria_straznik_drogiej_taryfy_op12`, `bateria_dynamiczne_ladowanie_ad_hoc_op12` (strefy G12w sterują baterią!), `podsumowanie_miesieczne_taryf`, `taryfa`, `zliczanie_godzin_dynamiczna_tansza`.

### Otwarte decyzje (wywiad grill-me, jedno pytanie na raz)
1. ✅ **Odpowiedź usera: G13** (klasyczna, nie G13s). Data zmiany jeszcze otwarta. Pytanie było: **G13 czy G13s?** Strefy G13 Tauron zależą od sezonu, a katalog ma obie taryfy. Od kiedy zmiana: data z eBOK czy z pierwszej faktury G13?
2. ✅ **Odpowiedź usera: baza = zawsze bieżąca taryfa**, także dla starych miesięcy; koszty faktyczne z faktur zostają w taryfie z tamtego okresu; historia `tariff_periods` z datą zmiany z eBOK. Domyślnie (bez pytania): ręczny override taryfy w ustawieniach jako awaryjny, bo detekcja z eBOK może zawieść. Pytanie było: **Źródło prawdy:** detekcja z eBOK zapisuje historię `tariff_periods: [{tariff, from}]`. Czy potrzebny ręczny override w ustawieniach? Bazą jest taryfa z okresu, w którym jest porównywany miesiąc, czy zawsze bieżąca?
3. ✅ **Odpowiedź usera: nowe neutralne + stare do migracji** — `*_baseline`/`saving_vs_baseline` z atrybutem `baseline_tariff`; stare `*_g12w` liczone względem nowej bazy do migracji dashboardów, potem usunięte. Pytanie było: **Encje HA:** rekomendacja to nowe, neutralne encje `..._baseline` / `saving_vs_baseline` z atrybutem `baseline_tariff`, a stare `*_g12w` zostają do migracji dashboardów (zmiana `entity_id` psuje historię i odwołania). Alternatywa: przemianowanie.
4. ✅ **Odpowiedź usera: auto wg bieżącej taryfy** — add-on publikuje do HA bieżącą taryfę i strefę (np. `sensor.pv_roi_tracker_current_zone`), automatyzacje baterii czytają strefę stąd i same się przełączają; wdrożenie osobnym etapem (E6), za zgodą na diff. Pytanie było: **Bateria i automatyzacje HA na strefach G12w:** czy po zmianie mają przejść na strefy G13? To zmienia sterowanie domem, więc to osobny etap za zgodą. Czy pakiety YAML `energy_simulation` też przechodzą na „bazę dynamiczną” (sensor `sensor.pv_roi_tracker_current_tariff` jako źródło)?
5. **Koszt faktyczny w 3 strefach:** liczniki HA mają tylko peak/offpeak. Trzeba nowych liczników stref G13 (`utility_meter` z taryfami) i `tariff_config` z 3 cenami. Parser faktur G13 dopiero po pierwszej fakturze G13.

### Wywiad zakończony (27.10). Decyzje: G13 (klasyczna); baza zawsze = bieżąca taryfa; nowe neutralne encje
`*_baseline` + stare do migracji; bateria auto wg `current_zone` (osobny etap, za zgodą na diff).
Zasady domyślne: ręczny override taryfy w ustawieniach (awaryjny); 3-strefowe liczniki HA dla G13 dopiero
w E4–E5, przed pierwszą fakturą G13 koszty faktyczne zostają 2-strefowe; push przy wykryciu zmiany.

### Szkic etapów (zatwierdzany razem z planem)
- E1: naprawa eBOK, wydanie 0.50.0, jak wyżej.
- E2: detekcja taryfy i historia `tariff_periods` oraz encja `current_tariff` do HA; push przy zmianie.
- E3: baza dynamiczna w add-onie (tariff_job, web, UI, publisher z neutralnymi encjami).
- E4: strefy według okresu taryfy (battery_sim/heatpump/dyn), model 3-strefowy, tariff_config.
- E5: HA, czyli pakiety, liczniki, dashboardy na bazie dynamicznej.
- E6: bateria i automatyzacje na strefach bieżącej taryfy (za osobną zgodą).
- E7: wpłaty i depozyt (dawne E3).
- Parser faktur G13 dopiero po pierwszej fakturze G13.

## Po wdrożeniu (do obserwacji)
- Pierwsza nowa faktura (ok. 5–8.11): jaki `Status` ma niezapłacona; czy przypomnienie przyszło; czy
  import poszedł w oknie 1–15 o 07:30. Nowy status dopisuję do `PAID_STATUSES` albo do testów.
- Tag `v0.49.2` (niepodbity config) zostaje; decyzja należy do usera.
