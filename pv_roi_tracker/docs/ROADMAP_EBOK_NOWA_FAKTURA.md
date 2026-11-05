# pv_roi_tracker — nowa faktura Tauron niewidoczna w eBOK + dług po zmianie taryfy (G13)

## Context
User widzi nową fakturę w Mój TAURON, ale add-on (0.55.4) jej nie importuje.

Diagnoza na żywo z 05.11.2026 (read-only, `GET api/ebok/diag` przez ingress, `api/data`, `api/ebok/status`, logi add-onu):
- Joby eBOK przechodzą bez błędu: `eBOK: 79 dok., brakujących 0` (04.11 i 05.11 o 07:30).
- `GetDocuments` (archiwum dokumentów) zwraca 79 dokumentów. Najnowszy to korekta z końca października (status „Rozliczono”), poprawna **korekta RCE** do faktury za 11.2025 i została zaimportowana prawidłowo jako korekta.
- Statusy w archiwum to wyłącznie {Opłacona, Rozliczono}. Ostatni miesiąc w magazynie faktur to wrzesień 2026.
- **Hipoteza:** archiwum (`EbokArchiveDocuments/GetDocuments`) pokazuje tylko dokumenty rozliczone. Nowa, nieopłacona faktura jest na stronie „Faktury” (`/Home/Pages/Faktury?sid=T18&pp=…`) i pochodzi z innego zapytania API. To samo psuje pasek płatności, bo `payment_reminders` też bierze dane z tej listy, więc przypomnienie o niezapłaconej fakturze nigdy nie powstanie. ROADMAP_EBOK_MOJ_TAURON:234 oznaczał to jako „niesprawdzone do pierwszej nowej faktury”.
- Do wyjaśnienia przy okazji: diag pokazuje `unknown` z jednym dokumentem z 2024 (typ „Faktury”), a sync loguje `brakujących 0`.

Decyzja usera: zamiast adresu z DevTools robimy **sondę diagnostyczną**. „Dług ze zmiany taryf” to długi z `/config/docs/ROADMAP_energy_panel_g13.md` (E1/E3):
(a) brakująca encja strefy G13 jest pomijana bez ostrzeżenia, (b) fallback REST `sensor.monthly_energy_*` nadal wskazuje zamrożone liczniki G12w, (c) brak testów `get_energy_window` i `read_current_month` dla sumy G13.

Repo: `~/dev/pv_roi_tracker` (gałąź `main`, PUBLIC), kopia do `/config/addons/pv_roi_tracker` przy wydaniu (jak 0.55.4). Testy:
`cd ~/dev/pv_roi_tracker/pv_roi_tracker && PATH=$HOME/.local/bin:$PATH uv run --no-project --with pytest --with-requirements requirements.txt python -m pytest -q` (stan 1350 passed / 23 skipped).

## Etapy (krok 1 implementacji: ten plan → `docs/ROADMAP_EBOK_NOWA_FAKTURA.md` w repo)

### E0 — sonda strony Faktury (wydanie 0.55.5, osobne: bez niego nie da się zrobić E1)
- `ebok_client.EbokClient.probe_invoices_page()`: GET `/Home/Pages/Faktury?sid=T18&pp=<płatnik>` (przez `_get`). Zwraca status, rozmiar, unikalne ścieżki `/api/sitecore/<Kontroler>/<Akcja>` znalezione w HTML i w skryptach z tej samej domeny (maks. 8 plików JS, tylko `SERVICE`), nazwy atrybutów `data-*` z elementów z checksumą oraz sygnatury `T/K1/…` ze strony, których nie ma w `GetDocuments`.
- `main.ebok_diag` dokłada sekcję `invoices_page` (maskowanie `_mask`, bez checksum: wycinanie 64-hex). Dalej tylko odczyt: bez importu, bez pushy i bez health.
- TDD z neutralnym fixture HTML. Wykonawca: `haiku-worker` (mała, niegroźna zmiana) + verifier + claimcheck.
- Wydanie: `simplify` → `code-review` (+ silent-failure-hunter, security) → `release` 0.55.5 → `ha_manage_updates` z `backup=true` → `GET api/ebok/diag` przez playwright-ha.
- DONE: diag pokazuje endpoint i format listy faktur do zapłaty (albo dowód, że faktury nie ma też na tej stronie, wtedy staję i pytam).
- Rollback: backup add-onu z `ha_manage_updates` (0.55.4).

### E1 — import faktur nierozliczonych (commit, bez wydania)
- Plan etapu `docs/PLAN_EBOK_NOWA_FAKTURA_E1.md` z sekcją **Wykonawca** piszę po wyniku E0 (format nieznany). Zarys: `list_documents` łączy archiwum z listą „do zapłaty” (dedup po `normalize_signature`, wpis z archiwum ma pierwszeństwo). Dokument z nowego źródła dostaje `paid=False`, poprawny `due_date` i `pdf_url`. Nowy status trafia do `_is_paid_status` albo do testów.
- Wyjaśnić rozjazd `unknown` 2024-01 w diag z `brakujących 0` w sync. Poprawiam tylko wtedy, gdy to realny błąd w tym samym kodzie, inaczej zgłaszam.
- Ryzyko (import do `invoice_store`, płatności) → `sonnet-worker`, TDD, neutralny fixture, verifier.
- DONE: testy zielone. Test: dokument tylko z listy „do zapłaty” wchodzi do `missing` i do `apply_refresh` jako aktywna płatność.

### E2 — dług G13 (commit, bez wydania)
- (a) `live_reader._sum_role_month`: przy częściowym odczycie loguj ostrzeżenie o brakujących encjach roli (raz na encję na proces), zachowanie sumy bez zmian.
- (b) fallback przy braku LTS w `read_current_month` (i ścieżka w `get_energy_window`, jeśli dotyczy): nie bierz samego zamrożonego `sensor.monthly_energy_*`. Dokładną zmianę zapisuję w planie etapu po przeczytaniu kodu: suma żywych stanów stref albo `None` z ostrzeżeniem.
- (c) testy `get_energy_window` i `read_current_month` dla sumy 3 stref G13 (mock `_fetch_lifetime_month_stats`/`_get_state`).
- `haiku-worker` + verifier. DONE: testy zielone, nowe testy czerwone bez zmian (a)/(b).

### Wydanie 0.56.0 (E1+E2, od razu po E2, bez pytania)
`simplify` → równolegle `code-review`, `silent-failure-hunter`, `pr-test-analyzer`, security (general-purpose) na `v0.55.5..HEAD` → `release` (bump, published) → kopia do `/config` + push → `ha_manage_updates` (`backup=true`).

## Weryfikacja na żywo (po 0.56.0)
1. `GET api/ebok/diag`: nowa faktura w `newest` i w `unknown`, `unknown` ≤ 5 (więcej = stop i pytam przed sync).
2. `POST api/ebok/sync` → `ok`, `imported` zawiera 2026-10, bez `needs_training`. Jeśli PDF G12w→G13 ma nowy układ i wymaga treningu, staję i raportuję.
3. `GET api/ebok/payment-status` → `active` = nowa faktura z kwotą i terminem.
4. Logi add-onu po starcie czyste. Ostrzeżenie (a) się nie pojawia, bo wszystkie encje G13 istnieją.
5. Aktualizacja ROADMAP, notatka pamięci HA `project_ebok_moj_tauron` (+ długi G13 w `ROADMAP_energy_panel_g13.md` jako zamknięte).

## Granice
- Logowanie do eBOK tylko przez diag i sync, maks. kilka razy dziennie (ryzyko blokady Taurona).
- Numer płatnika, adres i checksum nie trafiają do repo, pamięci ani czatu (fixture neutralne, pre-commit pubguard).
- Bez restartu HA. Add-on aktualizuje Supervisor przez `ha_manage_updates`.

## STATUS 2026-11-05

- **E0 ZROBIONE:** 0.55.5 wydane (tag `v0.55.5`, published), zainstalowane na żywo (`installed_version` 0.55.5, logi startu czyste), commity dev `91312b3`, `756cd3b`, wydanie `9240e69`, kopia /config `30a9863`. Testy 1360 passed / 23 skipped. Przeglądy: silent-failure-hunter, pr-test-analyzer, security, code-review bez blokujących; poprawki wprowadzone (regex sygnatur 2 formaty, statystyki skryptów, nazwy parametrów, roadmapa bez prawdziwych numerów).
- **Wynik sondy (`GET api/ebok/diag` → `invoices_page`):** strona Faktury (status 200, 45 KB, 6 skryptów pobranych) nie zawiera sygnatur w HTML (`signatures_total: 0`). Lista faktur ładuje się przez `GET /api/sitecore/EbokInvoiceList/LoadMoreInvoicesProducts?pp&sid` (parametry pokazane tylko jako nazwy; mogą być też inne, dynamiczne). Inne: `EbokInvoiceList/Payment?ids`, `EbokInvoiceList/SetName`. Archiwum (`GetDocuments`) nadal 79 dok., najnowszy to korekta z 31.10.
- **Następny: E0b (0.55.6, mała sonda):** `diag` woła `LoadMoreInvoicesProducts` (sid=T18, pp=<płatnik>, pozostałe parametry z kodu JS strony) i zwraca TYLKO kształt odpowiedzi: typ (JSON/HTML), klucze/tagi, liczbę wierszy, sygnatury `T/K1/…` i statusy (bez kwot, płatnika, checksum). Dopiero potem E1 (plan z sekcją Wykonawca po poznaniu formatu). Wydanie 0.55.6 może od razu nieść E1, jeśli format jest prosty.
- Dług z review sondy (nieblokujący): sonda woła ~9 żądań do Taurona przy każdym `diag`; brak testu `ebok_diag` (closure w `main()`).

## STATUS E0b 2026-11-05 (0.55.6 wydane i na żywo, DONE NIESPEŁNIONE)

- 0.55.6: tag `v0.55.6` published, commity dev `0b7ab85`, `c6cc102`, wydanie z bumpem, kopia /config `e180e5d`; `installed_version` 0.55.6; testy 1367 passed / 23 skipped; przeglądy (code-review, silent-failure-hunter) poprawione przed wydaniem (wyciek ciągów w `_json_shape`, maskowanie tokenów).
- Wynik sondy na żywo: `LoadMoreInvoicesProducts?pp&sid` (bez pozostałych parametrów) zwraca HTML 21,6 KB o 200 z **menu strony** (elementy `data-item` to pozycje menu „Faktury…”, nie wiersze faktur), 0 sygnatur. `js_context` pusty, bo kotwica `LoadMoreInvoicesProducts` jest w skrypcie **inline** w HTML strony Faktury, a `_js_contexts` skanuje tylko pliki JS.
- Wniosek: nie znamy jeszcze parametrów wywołania ani formatu wiersza. Propozycja E0c (0.55.7): skanować też inline `<script>` w HTML strony; szkielet brać z `data-item` poza menu (np. wiersze z kwotą/terminem); po poznaniu parametrów powtórzyć wywołanie. Alternatywa: user podaje z DevTools dokładny URL żądania `LoadMoreInvoicesProducts` (nazwy parametrów, bez wartości).

## STATUS E0c 2026-11-05 (0.55.7 na żywo) — ZNALEZIONE WYWOŁANIE

- Zrzut ekranu usera z Mój TAURON (Faktury i płatności) pokazuje 3 faktury do zapłaty w rozwiniętym punkcie: `K1NBN…/037` (wyst. 31.10, 1,31 zł), `T/K1/BN…/0020/26` (03.11, 95,32 zł), `T/K1/BN…/0021/26` (05.11, 7,85 zł); saldo 104,48 zł = suma. Kolumny: numer+typ, data wystawienia, informacje dodatkowe, termin płatności, kwota do zapłaty + „Zapłać”. Kryterium sukcesu importu: te 3 dokumenty, suma 104,48. Format `K1NBN…/037` (bez `T/K1/`) nie pasuje do `_SIGNATURE_RE` — sprawdzić w E1.
- Inline JS strony Faktury: `loadMoreItems()` robi **POST** `/api/sitecore/EbokInvoiceList/LoadMoreInvoicesProducts?sid=T18&pp=<płatnik>` z danymi `{startingIndex, searchTerm, PickupMediumType, parentViewModel}`; odpowiedź JSON `{ResultHtml, TotalCount, ReturnedCount}`, `ResultHtml` doklejane do `#itemList`. `parentViewModel` = zmienna JS `serializedViewModel` ze strony (przekazywana bez zmian), `PickupMediumType` = `mediumType` (z `#SearchType`). Wcześniejszy GET z samymi `pp`,`sid` zwracał menu strony.
- Następny: **E0d (0.55.8)** — `probe_invoice_list` robi POST z `serializedViewModel` wyciągniętym z HTML strony (wartość nie wychodzi do diag), `startingIndex=0`, `searchTerm=''`, `PickupMediumType` domyślny; zwraca kształt: klucze JSON, TotalCount/ReturnedCount, szkielet wiersza `<tr><td>` z `ResultHtml`, liczba sygnatur. Potem E1: parser `ResultHtml` → dokumenty nierozliczone.

## STATUS E0d 2026-11-05 (0.55.8 na żywo; tag `v0.55.8`, kopia /config `fa7be9e`; testy 1371 passed / 23 skipped)

- Wynik na żywo: `invoice_list` → `ok: false`, POST **pominięty** (zabezpieczenie zadziałało). `serializedViewModel` w inline JS to **obiekt JS/JSON** (`var serializedViewModel = {"…":null,"…":null,"…":"…","…":"Brak…`), nie literał tekstowy; `mediumType` też nie jest literałem tekstowym (`medium_type_found: false`). Obok: `var isInitExpanded = false; var currentItemCount = 9; var totalItems = null;` (w JS `currentItemCount` = `startingIndex` kolejnego `loadMoreItems`).
- Wniosek: jQuery wysyła obiekt jako zagnieżdżone pola formularza (`parentViewModel[Klucz]=wartość`; `null` → puste; tablice prymitywów `Klucz[]=…`, tablice obiektów `Klucz[0][pole]=…`).
- **Następny: E0e (0.56.0-rc / 0.55.9)** — w `probe_invoice_list`: (1) wyciągnąć literał obiektu `serializedViewModel = {…}` dopasowaniem nawiasów z uwzględnieniem cudzysłowów, `json.loads`; (2) spłaszczyć wg reguł `jQuery.param` do listy par dla `requests` (`parentViewModel[...]`); (3) `mediumType`: pokazać (zamaskowany) kontekst przypisania `var mediumType`, wartość domyślna z `#SearchType`/`SearchType` option; (4) `startingIndex=0`, `searchTerm=''`; POST; zwrócić kształt (`ResultHtml`: wiersze `<tr><td>`, TotalCount/ReturnedCount, sygnatury). Do diag nie wychodzą wartości view-modelu (tylko liczba kluczy i typy). Testy z neutralnym obiektem JSON. Jeśli E0e też nie da formatu `ResultHtml` — prosić usera o żądanie z DevTools (Network → `LoadMoreInvoicesProducts` → Payload: tylko nazwy pól).
- Przeglądy 0.55.8 (code-review, silent-failure-hunter): bez blokujących; poprawki wprowadzone przed wydaniem. Niezrobione doradcze: retry POST, maskowanie zagnieżdżonych szkieletów (zawierają tylko znaczniki i słowa statusu).

## STATUS E0e 2026-11-05 (0.55.9 na żywo; tag `v0.55.9`, dev `44c3d55`, kopia /config `8895c84`; testy 1378 passed / 23 skipped)

- Przegląd silent-failure-hunter: 2 BLOKUJĄCE (wyciek `medium_type` z literału bez walidacji i bez maskowania; `_search_type_default` czytał `value` z ogona dokumentu) + 4 niższe — wszystkie poprawione przed wydaniem (walidacja `[A-Za-z]{1,20}`, mask w `main.ebok_diag`, regex tylko po znaczniku, pusty `{}` = brak, flaga `view_model_object_unparsed`, `_var_context` maskuje na szerszym oknie, float jak JS).
- **Wynik na żywo: POST działa.** `invoice_list` → status 200, JSON `{ResultHtml, TotalCount: 3, ReturnedCount: 3}` — 3 faktury = zrzut usera (kryterium sukcesu). `view_model_kind: object`, 70 kluczy, `view_model_object_unparsed: false`. `mediumType` w JS to nie literał (`medium_type_found: false`, brak `#SearchType`) — POST poszedł z pustym `PickupMediumType` i mimo to zwrócił komplet.
- **Format wiersza NIEZNANY:** `ResultHtml` (21 KB) nie zawiera `<tr><td>` (`rows: 0`, `signatures: 0`). Tagi: div 136, dd 27, dt 27, a 37, span 21, th 10, img 10, input 9. Układ to najpewniej `dl/dt/dd` w `div`-ach (kolumny: numer+typ, data wystawienia, informacje dodatkowe, termin, kwota). Sygnatury nie pasują do `_SIGNATURE_RE` (format `K1NBN…/037` bez `T/K1/`, a może rozbite na elementy).
- **Następny: E0f (0.55.10, mała sonda):** `_ITEM_START_RE`/`_html_skeleton` nie łapią tego układu. Zwrócić szkielet jednego bloku faktury: znaleźć powtarzający się kontener (np. element zawierający `dt`/`dd` i `input`/`a` „Zapłać”), wypisać znaczniki + klasy + nazwy `data-*` + liczbę potomków, tekst jako „…” poza słowami statusu; dla każdego `dt` jego etykietę (słowa kolumn: „Termin płatności”, „Data wystawienia” — to etykiety UI, nie dane), kształt wartości (maska: cyfry→9, litery→a, np. `a9aaa…/999`) i typ elementu, w którym leży. Po E0f: E1 (parser `ResultHtml` → dokumenty nierozliczone; `sonnet-worker`, TDD, neutralny fixture zbudowany z kształtu), E2, wydanie 0.56.0.
- Dług: sonda woła ~10 żądań do Taurona na `diag`; `mediumType` do ustalenia dopiero, jeśli brak go zacznie zmieniać wynik (np. e-faktury vs papier).

## STATUS E0f 2026-11-05 (0.55.10 na żywo; tag `v0.55.10`, dev `bc19d26` + wydanie, kopia /config `5300883`; testy 1382 passed / 23 skipped)

- Dodane: `_invoice_block_shape` (drzewo HTML, blok = najniższy przodek pierwszego `dt` powtórzony u rodzeństwa), `dl_blocks` w `_json_shape`, maskowanie w `ebok_diag`. Przegląd silent-failure-hunter: 0 blokujących; poprawione: domykanie dt/dd, cyfry w klasach → 9, osłona RecursionError. Nieobsłużone (niskie): etykieta `dt` idzie dosłownie (≤40 zn., bez cyfr).
- **Wynik na żywo: sonda wybrała ZŁY blok** (pkt 5 przeglądu się sprawdził). `dl_blocks`: blocks 2, pola `Należność` / `Nota odsetkowa` / `Łącznie` (kształt `9,99 aa`) — to `dl.dl-horizontal` podsumowania salda w `div.col-xs-*`, nie wiersz faktury. Kolumny faktury (numer+typ, data wystawienia, termin, kwota, „Zapłać”) nie są w `dl`; 27 `dt` / 3 faktury = 9 `dt` na fakturę, reszta układu w `th`(10)/`span`(21)/`a`(37)/`img`(10)/`input`(9).
- **Następny: E0g (0.55.11, mała sonda):** blok = powtarzający się kontener o liczności == `ReturnedCount` (3) (najwyższy przodek z dokładnie tyle samych rodzeństwa o tej samej sygnaturze tag+klasy), nie pierwszy `dt`. Zwrócić jego szkielet (limit 3000 zn.), a dla każdego liścia tekstowego: ścieżka tagów/klas, zamaskowany kształt (cyfry→9, litery→a) i etykieta tylko gdy to słowo UI bez cyfr. Alternatywa gdy dalej nie da formatu: user podaje fragment HTML jednej faktury z DevTools (wartości zamaskowane). Potem E1, E2, wydanie 0.56.0.

## STATUS E0g 2026-11-05 (0.55.11 na żywo; tag `v0.55.11`, dev `e013b6d`, kopia /config `93fdbe4`; testy 1386 passed / 23 skipped)

- Dodane: `_repeated_block_shape(html, ReturnedCount)` (najwyższa grupa rodzeństwa o liczności == ReturnedCount, priorytet grupy z „Zapłać”), `item_blocks` w `_json_shape`, maskowanie w `ebok_diag`. silent-failure-hunter: 0 blokujących; poprawione: `any()` dla „Zapłać”, znacznik `no_group`.
- **Wynik na żywo:** `item_blocks.blocks == 3`, 42 potomków. Blok to `div.row.invoice-row` = **wiersz punktu poboru/umowy** (nie faktury): 3 kolumny `col-*` — (1) nazwa punktu `h3`, adres, numer klienta, numer umowy, `input[data-agreement-id]` (zmiana nazwy), lista linków `ul>li>a.btn-link`; (2) `amount-column`: `a.toggle-button[data-expand-init]`, `h3`, `label.checkbox>input.check-all-in-pp-expanded` + `div.amount` (kształt `999,99 aa` — saldo punktu, zgodne z 104,48 zł), `div.toggle-box` **pusty** (poza `div.amount` + `a.toggle-details-button` „Pokaż szczegóły”).
- **Wniosek:** lista ma dwa poziomy. `LoadMoreInvoicesProducts` zwraca punkty poboru z saldem; same faktury (numer, daty, kwota) ładują się dopiero po rozwinięciu (`data-expand-init` / `toggle-details-button`, `data-agreement-id`) — osobnym żądaniem, którego nie znamy. Kryterium sukcesu (3 faktury, 104,48) wymaga tego drugiego wywołania.
- **Następny: E0h (0.55.12, mała sonda):** w inline `<script>` strony Faktury (i w skryptach z tej samej domeny) znaleźć handler `data-expand-init` / `toggle-details-button` / `check-all-in-pp-expanded`: kotwice `EbokInvoiceList/…` (nazwy akcji i parametrów, bez wartości) i kontekst przypisania `url`/`$.ajax`/`$.post` wokół nich (maskowany). Potem wywołanie tej akcji z `data-agreement-id` punktu (wartość z HTML, nie do diag) i kształt odpowiedzi przez `_repeated_block_shape`. Alternatywa: user podaje z DevTools (Network po kliknięciu „Pokaż szczegóły”) nazwę żądania i nazwy parametrów.

## STATUS E0h 2026-11-05 (0.55.12 na żywo; tag `v0.55.12`, dev `51e5451` + `ffe6802`, kopia /config `9d464a3`; testy 1394 passed / 23 skipped)

- Dodane: `_code_contexts` (maskowane literały JS: selektory, ścieżki API bez wartości), `expand_context` w `invoices_page`, `_expand_attrs` → `expand_attrs` w kształcie `ResultHtml`. silent-failure-hunter: 0 blokujących; poprawione przed tagiem: ReDoS w regexie literałów (rozdzielone wzorce per cudzysłów), komentarze i backticki maskowane, wąska whitelista literałów, cyfry w nazwach `data-*`. Pubguard czysty. Instalacja przez Supervisora trwała ~6 min (timeout MCP, bez ponawiania).
- **Wynik na żywo — założenie z E0g obalone:** „Pokaż szczegóły” (`.toggle-details-button`) to handler **kliencki**: `toggleDetailsRow(t)` przełącza `.row-details-container` będący następnym rodzeństwem wiersza `div.row.invoice-row` (`.amount-column`.parent().next). `data-expand-init` na `a.toggle-button` ma wartość krótką (nie ścieżkę API; kształt `aaaa`), `expandTableRow` działa lokalnie. **Nie ma drugiego żądania.** Jedyne akcje `EbokInvoiceList`: `LoadMoreInvoicesProducts` (POST, lista punktów + wyszukiwarka), `SetName`, `Payment`.
- Wiersze faktur najpewniej siedzą w `ResultHtml` jako `.row-details-container` (ukryte, 3 szt.) → `.table-inside` > `.table-wrapper` > `.row` z `[data-charge-id]` i `.ebok-checkbox-payment`/`.ebok-button-pay-agreement` (z JS strony). `data-charge-id` = id obciążenia dla `Payment?ids`. Dotychczasowe sondy nie złapały ich, bo grupa o liczności 3 wybrała `div.row.invoice-row` (priorytet „Zapłać”/głębokość).
- **Następny: E0i (0.55.13, mała sonda):** w `_json_shape` dla HTML dodać `details_blocks`: elementy z klasą `row-details-container` (liczność, szkielet ≤3000 zn.) i dla pierwszego z nich liście przez `_leaf_fields` (kształt zamaskowany; etykiety UI) plus wiersze `.row` z `data-charge-id` (liczba, maska kształtu id, nazwy `data-*`). Po E0i: E1 (parser → dokumenty nierozliczone; numer w formacie `K1NBN…/037` poza `_SIGNATURE_RE`; `data-charge-id` do `Payment`), E2, wydanie 0.56.0. Jeśli E0i pokaże, że kontener jest pusty (ładowany leniwie przez `expandTableRow`/`data-expand-init`), prosić usera o Network z DevTools po kliknięciu strzałki rozwijania.

## STATUS E0i 2026-11-05 (0.55.13 na żywo; tag `v0.55.13`, dev `d97ef79` + wydanie, kopia /config `d3827a6`; testy 1397 passed / 23 skipped)

- Dodane: `_details_blocks` (kontenery `row-details-container`, wiersze `data-charge-id`, maski kształtu), `details_blocks` w `_json_shape`, maskowanie w `ebok_diag`. silent-failure-hunter: 0 blokujących; poprawione przed wydaniem: maskowanie cyfr w nazwach `data-*` w `_node_skeleton`. Niskie nieobsłużone: obcięcie szkieletu przed `mask()`, brak znacznika truncated, `_safe_details_blocks` łapie tylko RecursionError.
- **Wynik na żywo — DONE NIESPEŁNIONE:** `containers: 3`, `charge_rows: 0`. Każdy kontener to podsumowanie salda (subheader + `dl`: Należność / Nota odsetkowa / Łącznie, kształty `9,99 aa` / `999,99 aa`), bez numerów faktur, dat, terminów i `data-charge-id`. Wiersze faktur NIE są w `ResultHtml` z `LoadMoreInvoicesProducts` (start 0, bez `PickupMediumType`) ani nie ładuje ich klient przez osobne żądanie `EbokInvoiceList` (E0h). Zostają: leniwe ładowanie innym mechanizmem (np. żądanie spoza `EbokInvoiceList`, zdarzenie `expandTableRow`) albo inny `PickupMediumType`/parametr.
- **Następny krok wymaga usera:** Network z DevTools w Mój TAURON po kliknięciu „Pokaż szczegóły” przy punkcie z fakturami: URL żądania (same nazwy parametrów, bez wartości), metoda i kształt odpowiedzi. Alternatywa bez DevTools: sonda skanuje WSZYSTKIE skrypty strony (nie tylko inline) pod kątem `expandTableRow`/`data-charge-id`/`charge` i wywołuje `GetDocuments`-podobne akcje — mniej pewna.

## ZNALEZIONY ENDPOINT 2026-11-05 (z DevTools usera; po E0i) — następny E1

- Kliknięcie „Pokaż szczegóły” **nie wysyła żądania** (potwierdzone przez usera). Faktury nierozliczone zwraca osobny endpoint: `GET /api/sitecore/EbokInvoices/GetInvoices?sourceId&contractorNumber&checkSum&take&skip` (nazwy parametrów; wartości nie trafiają do repo). Odpowiedź JSON `{status: "OK", html, totalCount}`.
- `sourceId` = GUID, `contractorNumber` = numer płatnika (= `clientNumber`), `checkSum` = 64 hex, **inny** niż `clientNumberCheckSum` z linków PDF. `take=5&skip=0` (paginacja). Skąd wziąć `sourceId` i `checkSum` — **nieznane**, pierwszy krok E1 (kandydaci: `data-*` na `.invoice-row`/`.amount-column` w `ResultHtml`, `serializedViewModel`, inline JS strony Faktury).
- Format `html` = wiersze `<tr><td>` (5 kolumn): (1) `a[data-base=/api/sitecore/EbokArchiveDocuments/GetDocumentImage?clientNumber&clientNumberCheckSum&system=T18&docNumber&docType=FV&sodId&docMode=1]` z numerem w tekście (formaty `K1NBN…/037` i `T/K1/BN…/0020/26`) + `div` z typem („Faktura”); (2) data wystawienia `DD.MM.RRRR`; (3) `-`; (4) termin `DD.MM.RRRR`; (5) `input[type=checkbox][data-charge-id][data-charge-value]` + `div.amount` + `a.ebok-button-pay-agreement[href=/api/sitecore/EbokShortListPP/Payment?ids=…]`.
- Dla usera: 3 wiersze, `totalCount: 3`, suma 104,48 zł = saldo ze zrzutu. Kryterium sukcesu E1 spełnialne bez dalszych sond.
- **Plan E1:** (1) ustalić `sourceId`+`checkSum` ze strony Faktury (jeśli się nie da — sonda E0j i pytanie do usera); (2) `EbokClient.list_unpaid_invoices()` woła `GetInvoices` (paginacja po `totalCount`), parser `<tr><td>` → `EbokDocument(paid=False, due_date, pdf_url, charge_id)`; (3) `list_documents` łączy z archiwum (dedup po znormalizowanym numerze, archiwum wygrywa); (4) `_SIGNATURE_RE`/`normalize_signature` dla `K1NBN…/037`; (5) testy z neutralnym fixture. Plan z sekcją **Wykonawca** w `docs/PLAN_EBOK_NOWA_FAKTURA_E1.md` na początku sesji kontynuacji.

## STATUS E0j 2026-11-05 (0.55.14 na żywo; tag `v0.55.14`, dev `1b27498`, `121c6c1` + wydanie `ad4b64f`, kopia /config `7a13d40`; testy 1400 passed / 23 skipped) — ŹRÓDŁO PARAMETRÓW ZNALEZIONE

- `ResultHtml` z `LoadMoreInvoicesProducts` niesie parametry `GetInvoices` w atrybutach tego samego `div`-a (nazwy, bez wartości): `data-source-id` (GUID = `sourceId`), `data-contractor-number-checksum` (64 hex = `checkSum`), `data-api-url` (zawiera 64 hex, najpewniej gotowy adres `GetInvoices`). View-model strony ma tylko `DataSourceId`. Kod JS wokół `GetInvoices` nie jest na stronie (pusty `result_html_code`).
- Wniosek: E1 nie potrzebuje już sond; `list_unpaid_invoices` czyta te atrybuty z `ResultHtml` (POST jak w `probe_invoice_list`), potem woła `GetInvoices`.

## STATUS E1 2026-11-05 (ZROBIONE, commit dev `ded382c`, bez wydania; testy 1415 passed / 23 skipped)

- Dodane w `ebok_client.py`: `parse_unpaid_source`, `parse_unpaid_invoices_html`, `EbokClient.list_unpaid_invoices` (POST `LoadMoreInvoicesProducts` → `data-source-id` + `data-contractor-number-checksum` → GET `GetInvoices`, `take=5`, paginacja po `totalCount`), wspólna część sondy wydzielona do `_invoice_list_form` / `_post_invoice_form` / `_post_invoice_list`. `list_documents` dokleja nierozliczone bez filtra dat, dedup po `normalize_signature` (archiwum wygrywa); błąd źródła nie psuje archiwum, `EbokLoginBlocked` przechodzi. `_SIGNATURE_RE` łapie też `K1N…/037`. 15 nowych testów, neutralne fixture.
- Weryfikacja: pytest na `main` po landzie 1415 passed; `verifier` ACCEPT (zakres = 2 pliki z kontraktu, testy sondy bez zmian, brak prawdziwych danych w fixture).
- Zastrzeżenia nieblokujące z verifiera (do raportu wydania, sprawdzam sam): (1) `list_documents` łapie tylko `EbokError`/`RequestException` — np. `TypeError`/`KeyError` z nieoczekiwanego kształtu przerwie też listę archiwum (zgodne z kontraktem pkt 4; rozważyć `except Exception` z logiem); (2) nowy format `K1N…` trafia też do liczników sond (tylko diagnostyka); (3) „testy czerwone przed zmianą” niezweryfikowane per test (ImportError na HEAD).
- Claimcheck: „brak punktów w raporcie” (raport nie był wypunktowany) — pominięty.
- **Nie zweryfikowane na żywo** — dopiero po wydaniu 0.56.0 (diag / sync / payment-status, kryterium: 3 faktury, 104,48 zł).
- **Następny: E2** (dług G13, `/config/docs/ROADMAP_energy_panel_g13.md`): (a) ostrzeżenie o brakującej encji strefy w `live_reader._sum_role_month`, (b) fallback bez zamrożonego `sensor.monthly_energy_*` w `read_current_month`/`get_energy_window`, (c) testy sumy 3 stref G13. Plan etapu z sekcją Wykonawca → `docs/PLAN_EBOK_NOWA_FAKTURA_E2.md` przed implementacją. Potem wydanie 0.56.0 (E1+E2).

## STATUS E2 2026-11-05 (ZROBIONE, commit dev `568d7ef`, bez wydania; testy 1423 passed / 23 skipped)

- (a) `_sum_role_month(..., warn_missing)`: ostrzeżenie raz na encję/proces przy częściowym odczycie, tylko w `read_current_month` (stare miesiące nie mają `g13_energia_*`). (b) `read_current_month` nie używa zamrożonych `sensor.monthly_energy_*`: brak LTS peak/offpeak → `None` + ostrzeżenie (przy każdym wywołaniu). (c) testy sumy 3 stref G13 dla `read_current_month` i `get_energy_window` (+ brak strefy, ujemny `change`). `get_energy_window` bez zmian kodu.
- haiku-worker + verifier ACCEPT. Nieblokujące: ostrzeżenie o braku peak/offpeak leci co poll (nie raz na proces); test fallbacku przechodzi też na starym kodzie, regresję (b) chroni `test_read_current_month_none_peak_offpeak_when_lts_empty_warns`; nieużywane stany `monthly_energy_*` w `test_timezone_fix.py`.
- **Następny: wydanie 0.56.0** (E1+E2): simplify → code-review + silent-failure-hunter + pr-test-analyzer + security na `v0.55.14..HEAD` → release → weryfikacja na żywo.

## STATUS wydanie 0.56.0 2026-11-05 (NA ŻYWO, weryfikacja eBOK WSTRZYMANA)

- 0.56.0: tag `v0.56.0` published, dev `d3ad872` (+ `467657a` poprawki z review, `fb89876` simplify), kopia /config `3394436`; `installed_version` 0.56.0 (instalacja z `backup=true`; MCP dał timeout, instalacja dokończyła się po kilku minutach); logi startu czyste; testy 1427 passed / 23 skipped; pubguard 0.
- Review: security 0 znalezisk; silent-failure-hunter i code-review: 1 BLOKUJĄCE (`month_close` zapisałby rekord bez peak/offpeak po usunięciu fallbacku REST) — poprawione + test; paginacja kończy się na pustej stronie; ostrzeżenie w `list_documents` zawiera komunikat wyjątku. Niewprowadzone (doradcze): `list_documents` zwraca samo archiwum po błędzie listy „do zapłaty” bez powiadomienia; `_post_invoice_list` → `None` = „brak faktur” (bez ostrzeżenia); `except Exception` w parserach; brak retry POST; testy dla gałęzi błędów `list_unpaid_invoices`.
- **Weryfikacja na żywo NIEUKOŃCZONA:** `payment-status` → `active: null` (przed sync), `status` → taryfa G13 OK. `GET api/ebok/diag` zwrócił 2× `Read timed out (read timeout=45)` z `moj.tauron.pl` — nie ponawiane (granica: kilka logowań dziennie). Nie wiadomo, czy to chwilowa awaria Taurona, czy nowy kod (diag woła teraz też `list_unpaid_invoices`).
- **Dalej:** po jakimś czasie `GET api/ebok/diag` (≤5 `unknown`, nowa faktura w `newest`), `POST api/ebok/sync` (kryterium: 3 faktury, 104,48 zł, `imported` zawiera 2026-10), `GET api/ebok/payment-status` (`active`), logi bez ostrzeżeń (a). Potem notatka pamięci HA `project_ebok_moj_tauron` i zamknięcie długów G13 w `/config/docs/ROADMAP_energy_panel_g13.md`.

## WERYFIKACJA NA ŻYWO 0.56.0 2026-11-05 (~13:05)

- `GET api/ebok/diag` (ponowione po 2 timeoutach Taurona, przeszło): `ok`, total 82 (79 archiwum + 3 do zapłaty), `unknown` 4 (≤5), `newest` 3.
- `POST api/ebok/sync`: `ok`, errors [], `needs_training` [], `imported` = 2024-01, 2026-10, 2026-11; `latest` = 3 faktury do zapłaty (`K1NBN…/037`, `T/K1/…/0020/26`, `…/0021/26`). Log: „82 dok., brakujących 4”. Powiadomienie: „zaimportowano 3 nowych dokumentów, ale: nie pobrano PDF: K1NB…/037”.
- `GET api/ebok/payment-status`: `active` = `K1NBN…/037`, 1,31 zł, termin 2026-11-16 (pozostałe dwie nie są pokazane — `active` to jedna, najbliższa; suma 104,48 zł NIE potwierdzona tym endpointem).
- Logi: brak ostrzeżenia (a) o brakującej encji strefy; brak ostrzeżenia o braku LTS peak/offpeak.
- **Do wyjaśnienia:** (1) pobranie PDF `K1NB…/037` → `EbokLayoutChanged` („Odpowiedź (PDF) ze statusem 'Error'”) — prawdopodobnie inny `docType`/`docMode` tego dokumentu (mała kwota 1,31 zł, może nota/korekta) albo PDF jeszcze niedostępny; (2) skąd import 2024-01 (stary dokument z diag `unknown`, wcześniej „brakujących 0”); (3) rekord 2026-11 `reconciled=False` — sprawdzić, czy dokument 05.11 (0021/26) nie powinien być przypisany do innego miesiąca; (4) dokładne kwoty 3 faktur w store.

## NASTĘPNY ETAP "pdf-037-diag" — wydanie 0.56.1 (decyzja usera 05.11: wariant 2)

Cel: ustalić, dlaczego pobranie PDF `K1NBN…/037` kończy się `EbokLayoutChanged` ('Error'). Kroki: (1) TDD, neutralny fixture: `download_pdf` przy odpowiedzi ze statusem ≠ OK loguje zamaskowany kształt (klucze JSON, słowo `Status`, długości; bez numerów, checksum i wartości) oraz `docType`/`docMode` z adresu (`ebok_client.py` `download_pdf` ~304, `_json_or_layout_error` ~1080; wywołanie `main.py` ~734); (2) simplify → code-review + silent-failure-hunter + security; (3) release 0.56.1 (kopia do /config, push, `ha_manage_updates` `backup=true`); (4) jeden `POST api/ebok/sync` i odczyt logu o PDF 037 (haiku-scout). Rollback: backup add-onu 0.56.0. Poza tym otwarte: import 2024-01, rekord 2026-11 `reconciled=False`, kwoty 3 faktur (suma 104,48), notatka pamięci HA `project_ebok_moj_tauron`, zamknięcie długów G13 w `/config/docs/ROADMAP_energy_panel_g13.md`.

## STATUS pdf-037-diag 2026-11-05 (0.56.1 na żywo; tag `v0.56.1` published, dev `591adaf` (+ `d626a7c`, `84711ff`), kopia /config `67cae60`; testy 1430 passed / 23 skipped)

- Przeglądy: security 0 znalezisk; silent-failure-hunter 0 blokujących (wprowadzone: osłona `_safe_pdf_shape`, log także przy „Status OK, ale nie PDF”, węższy regex docType/docMode); skill code-review zwrócił pusty wynik (5 s, bez ustaleń — nie traktuję jako dowodu). Pubguard 0. Instalacja `ha_manage_updates` backup=true trwała ~2 min (timeout MCP, bez ponawiania); start 0.56.1 czysty (bez ERROR/WARNING, G13 bez ostrzeżeń).
- **Wynik sondy (POST api/ebok/sync, 13:18):** `eBOK: odpowiedź PDF nie jest OK (docType=FV docMode=1 pola: Document=NoneType, Status='Error', StatusEnum=int)`. Czyli Tauron odpowiada poprawnym JSON-em, ale z błędem dla żądania `docType=FV&docMode=1` dla dokumentu K1N…/037: brak `Message`, `Document=null`, numeryczny `StatusEnum`. Nie jest to zmiana układu odpowiedzi ani brak pliku w Base64.
- **Hipoteza (niezweryfikowana):** dokument `…/037` (1,31 zł) nie jest fakturą typu FV (np. nota/korekta/odsetki), więc `docType=FV` jest dla niego błędny albo PDF jeszcze nie istnieje. Link PDF budowany przez `parse_unpaid_invoices_html` bierze `docType` z atrybutu `data-base` wiersza — warto sprawdzić, czy wiersz niesie inny `docType`/`docMode`, a dla `GetInvoices` ewentualnie inny `docMode` niż archiwum.
- **Nowa obserwacja:** sync zwraca `imported` = 2024-01, 2026-11 przy każdym uruchomieniu i `brakujących 3` mimo że te rekordy już są w store (przed 0.56.1 `brakujących 4`). Dokumenty są pobierane i importowane ponownie przy każdym sync (dodatkowe logowania do Taurona + zbędne ingest). Do wyjaśnienia: `known_signatures` vs `normalize_signature` dla tych dwóch dokumentów.
- **Otwarte:** (1) PDF 037 — decyzja usera (sprawdzić w portalu typ dokumentu 037 albo kolejna sonda z wariantami `docType`/`docMode`); (2) powtarzany import 2024-01/2026-11; (3) rekord 2026-11 `reconciled=False`; (4) kwoty 3 faktur (suma 104,48) — payment-status pokazuje tylko `active`; (5) notatka pamięci HA `project_ebok_moj_tauron`; (6) zamknięcie długów G13 w `/config/docs/ROADMAP_energy_panel_g13.md`.

## ANALIZA 2026-11-05 ~13:30 (po 0.56.1) — plan etapu „dedup-037” → wydanie 0.56.2

Ustalenia na żywo (store przez `api/data`, 1 diag, logi):
- W store 42 rekordy, wszystkie `rozliczeniowa`; **brak rekordów `nota`** i stubów. Nota `037` NIE jest zaimportowana (PDF nie da się pobrać). Parser `_parse_nota` istnieje (`invoice_parser.py:599`), ale żadna nota nie weszła do store; śladu treningu noty w pamięci HA/repo nie znaleziono.
- `needs_training: true` mają `2024-01` (stara faktura, brak stawek/abonamentu) i `2026-11` (faktura 0021/26, 7,85 zł: brak opłaty mocowej i składnika stałego sieciowego, `reconciled=False`). Sync zwraca `needs_training: []`, store pokazuje `true` — rozjazd raportowania.
- **Cicha porażka listy „do zapłaty”:** `diag` (13:27) → `ConnectionError` w `list_unpaid_invoices`, `list_documents` zwraca samo archiwum (79 zamiast 82). Dla `diag` to tylko niepełny widok, ale `refresh_payment_bar(docs)`/`apply_refresh` w sync dostają wtedy listę bez faktur do zapłaty — **do sprawdzenia, czy kasuje to aktywną płatność** (ryzyko fałszywego „wszystko opłacone”).
- Powtarzany import 2024-01/2026-11 przy każdym sync: **przyczyna niepotwierdzona.** `known_signatures` (numer + nazwa pliku) powinno znać oba rekordy. Hipoteza: dwa dokumenty dają ten sam klucz `YYYY-MM` (`invoice_store._make_key`: rozliczeniowa nadpisuje rekord) i ich sygnatury na zmianę wypadają z `known`. Brakuje listy sygnatur „brakujących” (sync loguje tylko liczbę).
- Logowań do eBOK dziś: ≥7 (diag ×4–5, sync ×2). Limit „kilka dziennie” — NIE wołać eBOK dalej do jutra bez potrzeby.

### Plan 0.56.2 (kroki z góry; po wyniku staję tylko przy problemie)
1. TDD (neutralne fixture): (a) `list_documents` — przy `ConnectionError`/`RequestException` listy „do zapłaty” jedna ponowna próba po 3 s, a przy dalszej porażce zwraca archiwum **z flagą/atrybutem** `unpaid_failed` (np. `client.last_unpaid_error`), (b) `ebok_sync`/`refresh_payment_bar`: gdy lista „do zapłaty” niedostępna, NIE odświeżaj stanu płatności (zachowaj poprzedni) i dodaj ostrzeżenie w wyniku; test: aktywna płatność przeżywa porażkę listy, (c) sync loguje zamaskowaną listę brakujących (data, sygnatura z cyframi→`<n>`, `doc_name`, źródło archiwum/do zapłaty) i w wyniku `missing` (zamaskowane) — pozwala potwierdzić hipotezę kolizji klucza miesiąca, (d) wynik sync zwraca `needs_training` także z rekordów w store dotkniętych importem (rozjazd raportowania) — tylko jeśli prosty.
2. PDF 037: przy `Status='Error'` dla `docType=FV&docMode=1` jedna próba z `docMode` z zestawu {0, 2} (tylko dla dokumentów z listy „do zapłaty”, tylko po porażce; log zamaskowany wynik każdej próby: Status/StatusEnum). To ≤2 dodatkowe żądania na sync i tylko dla 037. Wykonawca: `sonnet-worker` (pieniądze/płatności = ryzyko), verifier.
3. simplify → code-review + silent-failure-hunter + pr-test-analyzer + security (na `v0.56.1..HEAD`) → release 0.56.2 → `ha_manage_updates` backup=true.
4. Weryfikacja na żywo **jutro** (limit logowań): jeden `POST api/ebok/sync`; odczyt logu o brakujących i o próbach docMode (haiku-scout). Kryterium: lista brakujących nazwana, import 2024-01/2026-11 nie powtarza się albo ma wskazaną przyczynę; `payment-status` nie gubi aktywnej płatności; 037 pobrany albo znamy odpowiedź dla docMode 0/2.
Rollback: backup add-onu z `ha_manage_updates` (0.56.1).

## STATUS dedup-037 2026-11-05 (0.56.2 na żywo; tag `v0.56.2` published, dev `1e58313`, `8edddd1`, `eda6852` + wydanie, kopia /config `045e534`; testy 1442 passed / 23 skipped)

- Zrobione wg `docs/PLAN_EBOK_NOWA_FAKTURA_DEDUP037.md`: retry listy „do zapłaty” (1×, `RequestException`) + `last_unpaid_error`; `download_pdf_any_mode` (docMode 0/2 po `EbokError`, nie `EbokLoginBlocked`; log udanego trybu) dla dokumentów „Do zapłaty”; sync zwraca i loguje zamaskowaną listę `missing` oraz `unpaid_failed`; health „brak nowych (lista do zapłaty niedostępna)”.
- Korekty planu: krok 1(b) zbędny (`apply_refresh` scala wpisy — porażka listy nie kasuje aktywnej płatności; dodany test regresyjny); krok 1(d) `needs_training` poza zakresem (wymaga zmiany `invoice_ingest`) — nadal otwarte.
- Przeglądy: security 0 znalezisk; pr-test-analyzer i silent-failure-hunter 0 blokujących; code-review: poprawione (fallback po zwykłym `EbokError`, reset `last_unpaid_error` na starcie `list_documents`). Pubguard 0. Nieblokujące, niewprowadzone: brak testu `ebok_sync` (closure), literał `'Do zapłaty'` ×3, brak retry dla `EbokError` 5xx, `diag['latest']` niezamaskowane (stare), `ebok_payment_check` nie czyta `last_unpaid_error`.
- **Weryfikacja na żywo dopiero 06.11** (limit logowań do eBOK): jeden `POST api/ebok/sync`. Kryteria: `missing` nazywa brakujące dokumenty (hipoteza kolizji klucza `YYYY-MM` dla 2024-01/2026-11), `unpaid_failed` false, log prób docMode dla 037, `payment-status` nie gubi aktywnej płatności.
- Otwarte: PDF 037, powtarzany import 2024-01/2026-11, `needs_training`/`reconciled=False` dla 2026-11, kwoty 3 faktur (suma 104,48), notatka pamięci HA `project_ebok_moj_tauron`, zamknięcie długów G13 w `/config/docs/ROADMAP_energy_panel_g13.md`.

## WERYFIKACJA NA ŻYWO 0.56.2 2026-11-05 ~13:46 (na polecenie usera, bez czekania na 06.11; 1 logowanie do eBOK)

- `POST api/ebok/sync`: `ok`, `errors` [], `unpaid_failed` false (retry listy zadziałał: pierwsze połączenie zerwane, ponowienie po 10 s), `total` 82, `needs_training` [], `not_found` [`K1NBN…/037`]. `payment-status`: `active` = 037, 1,31 zł, termin 2026-11-16 (przeżyła sync), `mismatches` [].
- **`missing` (zamaskowane) — 3 pozycje:** (1) archiwum, miesiąc 2024-02, nazwa „Faktury”; (2) do_zapłaty, 2026-10, nazwa „Faktura”, sygnatura `K<n>NBN<n>/<n>` (czyli nota/dokument 037); (3) do_zapłaty, 2026-11, `T/K<n>/BN<n>/<n>/<n>` (0021/26).
- **docMode dla 037:** próby 1, 0, 2 → wszystkie `Status='Error'`, `Document=NoneType`. PDF 037 NIE do pobrania żadnym trybem `FV`; zostaje `docType` inny niż FV albo dokument bez PDF.
- **Hipoteza kolizji klucza `YYYY-MM` — częściowo potwierdzona:** dokument z archiwum datowany 2024-02 jest zapisywany pod kluczem 2024-01 (rekord 2024-01 istnieje, `needs_training` true), więc sygnatura archiwalna nigdy nie trafia do `known` i sync importuje go przy każdym uruchomieniu. 037 trafia na miesiąc 2026-10, gdzie klucz zajmuje już faktura rozliczeniowa. Dla 2026-11 przyczyna niepotwierdzona (rekord istnieje, `reconciled=False`, `needs_training` true).
- Store: 42 rekordy, nadal brak rekordu `nota`.
- **Następny etap (propozycja, do zgody przed kodem):** (a) `known_signatures` oparte o sygnaturę z listy, nie o klucz miesiąca (koniec powtarzanego importu), (b) 037: sonda `docType` z wiersza „do zapłaty” (`data-base`) zamiast sztywnego FV, (c) `needs_training` w wyniku sync z rekordów dotkniętych importem.
