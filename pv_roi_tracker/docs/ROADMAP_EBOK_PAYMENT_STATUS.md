# PV ROI Tracker — „Termin płatności + status zapłacona z eBOK"

## Context

Nadbudowa nad zamkniętym podprojektem [`ROADMAP_EBOK_IMPORT.md`](ROADMAP_EBOK_IMPORT.md) (0.44.0, auto-import
faktur z eBOK TAURON). Ten sam CSV (`/content/platnosci/csv/.../type/archiwumFaktur`), który już
dziś zasila import, zawiera też kolumny `TERMIN PŁATNOŚCI` i `ZAPŁACONA` — dotąd nieparsowane
w `ebok_client.parse_documents_csv`.

Wywiad (28.09.2026):
- **Cel:** przypomnienie o niezapłaconej fakturze — dzień przed terminem, i drugie w dniu terminu
  jeśli nadal niezapłacona.
- **Źródło danych:** CSV eBOK (nie PDF) — jeden tani request pokrywa całą historię i pozwala
  odświeżać status już zaimportowanych faktur, których płatność mogła zmienić się po imporcie.
- **Harmonogram:** nowy, osobny, lekki codzienny job (samo CSV, bez pobierania PDF-ów) — bo
  terminy płatności wypadają w dowolny dzień miesiąca, a istniejące joby (1–15 + poniedziałek)
  nie pokrywają tego równomiernie.
- **Stan/idempotencja:** osobny plik stanu (`ebok_payment_state.json`), NIE rozszerzenie
  `invoice_store` — `invoice_store` napędza rekonsyliację/depozyt (rdzeń finansowy), a to jest
  funkcja czysto informacyjna; osobny plik izoluje ryzyko i naturalnie mieści flagi
  `reminded_before`/`reminded_due`.
- **Odbiorca powiadomień:** `notify.kacper` (spójne z resztą powiadomień eBOK).
- **UI:** pasek statusu na górze strony głównej (`index.html`, pod `<header>`), ukryty gdy nic nie
  wymaga uwagi.
- **Zakres:** TYLKO termin/status/przypomnienia. Inne dane z portalu (zużycie, saldo/nadpłaty,
  dane licznika, zmiana taryfy) → sekcja „Backlog" niżej, nie w tym etapie.

## Etapy (każdy kończy się checkpointem; następny tylko za „go")

### Etap 1 — parsowanie CSV + moduł stanu + funkcja decyzyjna (fundament, zero schedulera/UI)
- `ebok_client.parse_documents_csv`: dodać `due_date` (z `TERMIN PŁATNOŚCI`) i `paid: bool`
  (z `ZAPŁACONA`) do `EbokDocument`.
- Nowy moduł `payment_reminders.py` (czyste funkcje, bez I/O — wzorem `invoice_ingest.py`):
  - `decide_reminders(signature, due_date, paid, today, state) -> list[reminder]` — zwraca które
    przypomnienia (before/due) należy wysłać, biorąc pod uwagę już-wysłane flagi w `state`.
- `ebok_payment_state.py` (albo funkcje w `main.py` wzorem `_load_ebok_state`/`_save_ebok_state`):
  load/save `/data/ebok_payment_state.json`, struktura
  `{signature: {due_date, paid, reminded_before: bool, reminded_due: bool}}`.
- Testy: fixture CSV z Etapu 1 spike'a (`docs/ROADMAP_EBOK_IMPORT.md`) rozszerzony o kolumny
  `TERMIN PŁATNOŚCI`/`ZAPŁACONA`; testy `decide_reminders` na macierzy przypadków (niezapłacona
  jutro/dziś/za tydzień/po terminie/już wysłano/zapłacona w międzyczasie).
- **Checkpoint:** `pytest` zielony, zero zmian w zachowaniu add-onu na żywo (żaden nowy job, żaden
  endpoint, żaden UI element jeszcze aktywny).

### Etap 2 — codzienny job + powiadomienia (0.45.0 lub kolejna wersja)
- Nowy `ebok_payment_check_job` w `main.py`, `CronTrigger(hour=7, minute=0)` (codziennie, przed
  istniejącymi 7:30/7:45) — pobiera sam CSV przez `client.list_documents(...)` (bez `locate_ids`/PDF),
  woła `decide_reminders` dla każdej niezapłaconej pozycji, wysyła `notify.kacper` dla zwróconych
  przypomnień, aktualizuje i zapisuje `ebok_payment_state.json`.
- Health: wpis `_record_job('ebok_payment', ...)` analogicznie do istniejącego `ebok`.
- Ten sam guard co reszta eBOK: brak `ebok_username`/`ebok_password` → job nieaktywny.
- **Checkpoint:** ręczne wywołanie joba na żywo (np. przez tymczasowo przesunięty `due_date` w
  stanie testowym albo prawdziwa nadchodząca faktura), potwierdzony push na OP12, brak duplikatu
  przy drugim uruchomieniu tego samego dnia.

### Etap 3 — API + pasek statusu na stronie głównej (kolejna wersja)
- `GET /api/ebok/payment-status` — najbliższa aktywna (niezapłacona, z terminem) pozycja ze stanu,
  albo `null`.
- `index.html`: `<div id="paymentBar">` pod `<header>`, domyślnie ukryty; JS w `app.js` odpytuje
  endpoint przy ładowaniu strony i renderuje pasek z progami kolorów spójnymi z logiką przypomnień:
  - brak aktywnej pozycji / zapłacona → ukryty
  - termin za >1 dzień, niezapłacona → neutralny
  - termin jutro, niezapłacona → ostrzegawczy (żółty)
  - termin dziś lub przeterminowana, niezapłacona → alarmowy (czerwony)
- Cache-busting (`?v=<version>`), zgodnie z `feedback_mobile_webview_cache`.
- **Checkpoint:** Playwright desktop+mobile (screenshot + konsola) na żywych danych, self-review
  przed pokazaniem do akceptacji.

## Backlog — inne dane z portalu eBOK (nie w tym etapie, do rozważenia osobno)

- **Historia zużycia** (jeśli eBOK udostępnia poza fakturami) — potencjalne uzupełnienie/wzajemna
  weryfikacja danych Huawei/liczników.
- **Saldo/nadpłaty na koncie** — mogłoby zasilić `battery_arbitrage`/`pv_roi` deposit tracking,
  jeśli portal liczy to inaczej niż obecny model.
- **Dane licznika** (numer, ostatni odczyt) — czysto informacyjne, niski priorytet.
- **Powiadomienia o zmianie taryfy** — istotne dla `energy_simulation.yaml` (Tauron WSD), ale
  zmiany taryfy są rzadkie i user i tak je zauważa z faktury.

## Ryzyka

- CSV format `TERMIN PŁATNOŚCI`/`ZAPŁACONA` może się różnić dla różnych typów dokumentów (nota
  uznaniowa/korygująca) — `decide_reminders` musi bezpiecznie pomijać pozycje bez sparsowanego
  `due_date` (nie crashować, nie przypominać o czymś bez terminu).
- Nowy codzienny job = kolejne logowanie do eBOK dziennie, poza istniejącymi 2 (razem do 3/dzień w
  dniach 1–15, 2/dzień poza tym) — wciąż daleko od progu blokady zaobserwowanego w Etapie 1 spike'a
  oryginalnego projektu (5 logowań bez blokady), ale odnotowane na wypadek przyszłej zmiany polityki
  Taurona.
- Pasek statusu nie może pokazywać danych finansowych bez override'u — jeśli `paid` z CSV okaże się
  zawodne (opóźnienie księgowania), pasek i push mogą fałszywie alarmować; Etap 2/3 checkpointy na
  żywych danych mają to wyłapać przed uznaniem funkcji za gotową.

## Weryfikacja

- Etap 1: `pytest` zielony (parsowanie CSV + `decide_reminders`), brak zmian w działaniu add-onu.
- Etap 2: ręczne wywołanie joba, potwierdzony push, brak duplikatu przy powtórnym uruchomieniu,
  health `ok`.
- Etap 3: Playwright (desktop + mobile, screenshot + konsola) na żywych danych, pasek zgodny z
  progami z Etapu 2.

## Wynik końcowy (29.09.2026) — 0.45.0 zweryfikowane na żywym add-onie

**Etap 3 (pasek) — zweryfikowany.** Playwright na direct-IP add-onu, desktop 1280 px i mobile 390 px:
- stan żywy (`active: null`, brak niezapłaconej faktury z terminem) → pasek ukryty (`display:none`);
- odpowiedź `/api/ebok/payment-status` podmieniona przez `page.route` na 3 warianty — termin za 5 dni
  → neutralny (`#eef2f7`), jutro → żółty (`#fef3c7`), dziś/po terminie → czerwony (`#fee2e2`);
  progi zgodne z `select_active_payment` i z regułami przypomnień;
- zero błędów w konsoli (poza `favicon.ico` 404 — nieszkodliwe), brak poziomego scrolla na mobile,
  badge wersji `v0.45.0`;
- screenshoty: `/config/playwright/pv_roi_payment_bar_{desktop,mobile}_{live,neutral,warning,alarm}.png`
  (lokalne, gitignored).

**Etap 2 (job) — zweryfikowany częściowo.** Job `eBOK payment status check` uruchomił się 29.09 o 07:00
i zakończył `executed successfully` (log Supervisora), status add-onu `ok`. Realny push na OP12 **nie**
został jeszcze zaobserwowany — od uruchomienia nie było niezapłaconej faktury z terminem, więc
`decide_reminders` nie miał czego przypominać. Idempotencję (brak duplikatu) potwierdzają tylko testy
jednostkowe.

**Otwarty punkt do obserwacji:** przy pierwszej prawdziwej niezapłaconej fakturze sprawdzić (a) push
dzień przed terminem i w dniu terminu, (b) brak duplikatu przy kolejnym uruchomieniu, (c) że `paid`
z CSV nie opóźnia się względem realnej płatności (ryzyko fałszywego alarmu opisane wyżej).

**Uwaga kosmetyczna (bez zmiany kodu w tym kroku):** tekst paska nie ma polskich znaków
(„zl”, „platnosci”, „niezaplacona”), a przy terminie za >1 dzień data pojawia się dwa razy
(„termin platnosci 04.10.2026 (04.10.2026)”). Do ewentualnej poprawki przy następnym wydaniu.

**Obserwacja wydajności:** render strony blokuje wątek ok. 3 s (headless Chromium z `--disable-gpu`),
a `payment-status` jest pobierany dopiero po nim — na wolnym urządzeniu pasek pojawia się z opóźnieniem.
Nieblokujące; do rozważenia, jeśli pasek ma być pierwszą rzeczą, którą widać.
