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
