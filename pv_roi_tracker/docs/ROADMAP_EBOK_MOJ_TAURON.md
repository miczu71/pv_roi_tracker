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
