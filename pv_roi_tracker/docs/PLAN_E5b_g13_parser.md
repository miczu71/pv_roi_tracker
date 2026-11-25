# E5b — parser faktury G13 (trzy strefy) + kontrola stawek + długi E5a

## Context
W Mój TAURON jest nowa faktura `T/K1/BN567872/0022/26` (wyst. 23.11.2026, okres 03.11–18.11, 63,08 zł,
termin 07.12). To pierwsza faktura G13 i znów częściowa: Tauron przelicza po zgłoszeniu mocy umownej
(na fakturze nadal „Moc umowna 5 kW”, portal już pokazuje 15 kW; żadna opłata G nie zależy od mocy
umownej, więc parser jej nie potrzebuje). Za 19–30.11 przyjdzie trzecia część listopada.

Obecny parser (0.59.0, uruchomiony na tym PDF w kontenerze, bez zapisu) zwraca:
- strefy: wszystkie `*_peak/_offpeak` = None (układ: „szczyt przedpołudniowy / szczyt popołudniowy /
  pozostałe godziny doby”, ilość–kWh–cena–netto–VAT%–VAT–brutto),
- jakościowa / OZE / kogeneracja = None (teraz po jednym wierszu na strefę),
- `fixed_stalysieciowy_net` = None (nowa etykieta „Stała przesyłowa 3 fazowa tar. G 0,5333 mc 10,86 5,79”),
- `vat_total_pln` = 2,95 (powinno 41,17), `tariff` = G12W (ingest `apply_zone_tariff` zmieni na G13
  i wyzeruje strefy, ale ostrzeżenia „nie znaleziony” → `needs_training`).
Dobrze: kWh 287/39, mocowa 12,83, abonament 0, depozyt, do zapłaty 63,08, okres.
Import add-onu: poniedziałek 30.11 07:45 → wydanie przed tym terminem (decyzja usera).

Decyzje z wywiadu (2026-11-25): nowe pola trzech stref (peak/offpeak dla G13 zostają None);
zakres = parser + pola wspólne, kontrola stawek per strefa, długi z przeglądu E5a; wydanie przed 30.11.

Kto korzysta: rekord faktury listopada (historic/rekonsyliacja), kontrola stawek z cennikiem, powiadomienia.
Automatycznie: import 30.11 parsuje 0022/26 bez `needs_training`. Za zgodą: nic poza planem.
Sukces: 0022/26 sparsowana z polami stref = wartości z PDF, bez ostrzeżeń „nie znaleziony”,
kontrola stawek G13 zgodna z katalogiem (po odjęciu akcyzy 0,005), testy zielone, 0.60.0 na żywo.

## Repo i dane
- Kod: `~/dev/pv_roi_tracker` (PUBLIC, main) → kopia `/config/addons/pv_roi_tracker` przy wydaniu (skill `release`).
- Testy: `cd <WORKTREE>/pv_roi_tracker && PATH=$HOME/.local/bin:$PATH uv run --no-project --python 3.12 --with pytest --with-requirements requirements.txt pytest -q` (baza 1538 passed / 23 skipped — potwierdzić na starcie).
- PDF faktury zawiera dane osobowe → **nie do repo**. Kopia prywatna: `~/private_fixtures/pv_roi/FV_0022_26.pdf`
  (przenoszę ze scratchpadu); test „real PDF” z `skipif`, jak `TestRealPdf` (`tests/test_invoice_parser.py:26`).
  Do repo tylko anonimizowany tekst syntetyczny odwzorowujący układ **pypdf** (nie pdftotext — parser czyta pypdf).
- Kluczowe pliki: `pv_roi_tracker/invoice_parser.py` (wzorce `_BUILTIN_PATTERNS`, `_parse_text` ~743–1100,
  `neutralize_three_zone`/`apply_zone_tariff` 57–100), `invoice_store.py` (`_warnings_need_training`),
  `tariff_invoice_check.py` (`check_invoices`, `CHECK_FIELDS`), `tariff_module.py` (`zone_rates`, `_period_info`),
  `main.py:622` (`apply_zone_tariff` w `_invoice_train`), katalog `tariffs/tauron.json` (okres 5: G13 energy_net
  0,4668/0,778/0,421 + excise 0,005 = stawki z faktury 0,4718/0,783/0,426; dist_var 0,2203/0,3898/0,0392 = faktura).

## Etapy (roadmapa: dopisać E5b-1..E5b-4 do `/config/docs/ROADMAP_tariff_ha_migration.md` + `docs/ROADMAP` w repo)

### E5b-1 — parser trzech stref + pola wspólne (wartość: 0022/26 parsuje się poprawnie)
Kroki:
1. Skopiować plan do `docs/PLAN_E5b_g13_parser.md`; PDF do `~/private_fixtures/pv_roi/`; zrzut tekstu pypdf
   (`parse_invoice_debug`) do scratchpadu jako wzór dla testu syntetycznego.
2. TDD (skill `tdd`): test syntetyczny `TestG13Format` (tekst anonimizowany: okres 03.11.2026–18.11.2026, wiersze
   sprzedaży i dystrybucji per strefa, sekcja „oddanie” per strefa, „Stała przesyłowa 3 fazowa tar. G”) + `TestRealG13Pdf` (skipif).
3. `InvoiceData`: nowe pola Optional (domyślnie None, zgodne wstecz z `invoices.json`):
   `imported_kwh_{morning,evening,other}`, `exported_kwh_{morning,evening,other}`,
   `energy_{morning,evening,other}_net`, `dist_var_{morning,evening,other}_net`, `{morning,evening,other}_gross`.
4. `_parse_text`: wykrycie G13 po etykietach stref („szczyt przedpołudniowy/popołudniowy/pozostałe godziny doby”)
   → `tariff='G13'`; parsowanie stref energii, dystrybucji zmiennej, odczytów pobór/oddanie; jakościowa/OZE/kogeneracja
   = stawka (jednakowa w strefach) + kwota = suma wierszy; nowa etykieta stałej przesyłowej we wzorcach
   `fixed_stalysieciowy`; VAT z „Rozliczenie VAT … Kwota VAT” (41,17) — sprawdzić też G12W na starych testach;
   ostrzeżenia peak/offpeak pomijane dla G13 (jak `_is_single_zone` dla G11).
5. `neutralize_three_zone`: nie zerować nowych pól stref; gdy parser sam wykrył G13 — bez ostrzeżenia E5a.
6. Testy pełne; commit.
DONE: 0022/26 (real PDF) → kWh stref 26/12/249 i 9/0/30, ceny energii 0,4718/0,783/0,426, dist 0,2203/0,3898/0,0392,
jakościowa 0,0332, OZE 0,0073, kogen 0,003, stała 5,79, mocowa 12,83, VAT 41,17, tariff G13, brak ostrzeżeń
„nie znalezion/nieobliczon”, `_warnings_need_training` = False; stare testy bez zmian.
Rollback: `git revert` commitu etapu.

### E5b-2 — kontrola stawek per strefa (wartość: alarm, gdy faktura G13 odbiega od cennika)
1. `tariff_invoice_check.check_invoices`: dla G13 porównać pola stref z `module` (energy_net + excise, dist_var_net,
   quality/oze/cogeneration, fixed network_3ph) zamiast `legacy_rates`; miesiąc przejściowy porównywać wg okresu
   faktury (`billing_period_raw`), nie 1./ostatniego dnia miesiąca — inaczej listopad zawsze pominięty.
2. Testy (`tests/test_tariff_invoice_check.py`): G13 zgodny, G13 rozbieżny, część miesiąca w G13, G12w bez zmian.
DONE: 0022/26 wychodzi `ok: True` przeciw katalogowi okresu 5. Rollback: revert.

### E5b-3 — długi z przeglądu E5a (ROADMAP 175–183)
1. Jedna definicja „miesiąc/okres trzystrefowy” (helper w `tariff_zones`/`invoice_parser`, użyty w parserze,
   guardzie, heatpump_job, rozbiciu stref) — początek I koniec okresu faktury.
2. Logi w cichych `except` (`_zone_tariff_for`, `has_three_zone_days`, wykluczone miesiące `heatpump_job`).
3. `_invoice_remove`: `restore_month`/log przy pozostałej części niepełnej; `_invoice_train` nie nadpisuje stref G13.
4. Brakujące testy z listy (apply_zone_tariff G12w→G13, luty/grudzień w `period_covers_month`, ponowny import części).
DONE: pozycje z listy zamknięte albo opisane w ROADMAP jako świadomie pominięte. Rollback: revert.
Paczki: E5b-1 i E5b-2 to jedna paczka każdy (`sonnet-worker` — parser to ryzyko danych finansowych);
E5b-3 może iść jako W1 (store/main) + W2 (tariff_zones/heatpump) jeśli listy plików rozłączne.

### E5b-4 — wydanie 0.60.0 (przed 30.11 07:45)
`simplify` → `code-review` (+ `silent-failure-hunter`, `pr-test-analyzer`, security general-purpose na
`<baza>..HEAD`) → `release` (bump, CHANGELOG, kopia do /config/addons, GitHub release, Supervisor update) →
weryfikacja na żywo: w kontenerze `parse_invoice` na `/tmp/0022_26.pdf` = wartości z DONE E5b-1; po imporcie
30.11 sprawdzić rekord 2026-11 (część `~cz~`), brak `needs_training`, brak błędów w logu.
Rollback: Supervisor → poprzednia wersja 0.59.0 (backup przed update), `invoices.json.bak`.

## Ryzyka
- pypdf może rozbić wiersze stref inaczej niż pdftotext → wzorce budować na zrzucie pypdf.
- Faktura częściowa: trzy części listopada (G12w 01–02, G13 03–18, G13 19–30) — rekonsyliacja przez istniejące
  `~cz~`; nie ruszać logiki części poza punktami E5b-3.
- Nie wysyłać danych osobowych z PDF do repo PUBLIC ani do `second-opinion`.
