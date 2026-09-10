# Naprawa sekcji „Depozyt — faktury vs falownik (rekonsyliacja zasileń)"

## Context

Użytkownik zgłosił, że w tabeli rekonsyliacji miesiące **2026-06, 2026-07, 2026-08** pokazują
„jeszcze niezaksięgowane", mimo że faktury za lipiec i sierpień 2026 są wgrane i sparsowane.

### Diagnoza (potwierdzona na żywych danych, add-on 0.35.5, `172.30.33.15:8099`)

**Warstwa 1 — dlaczego akurat te trzy miesiące.** Wiersze tabeli to **miesiące eksportu**, nie
miesiące faktur. `deposit.py:232` pobiera wartość z faktury `export_month + posting_lag`. Na żywo
`posting_lag_months = 3`, najnowsza faktura to `2026-08` → najstarszy dopasowalny miesiąc eksportu
to `2026-05`. Faktury 07 i 08 **są** użyte — tyle że jako wartość dla eksportu z 04 i 05.

**Warstwa 2 — lag 3 jest błędny.** Porównanie `implied(M)` z `accrued(M−lag)` na wszystkich 39
fakturach: lag 1 daje **8 trafień co do grosza** (2023-12, 2024-01..03, 2025-02, 2025-12, 2026-02,
2026-04), lag 2 i lag 3 — **zero**. Fizycznie też lag 1: RCEm za miesiąc M publikowany jest w
połowie M+1, więc depozyt za eksport M pojawia się dopiero na fakturze M+1.
Detekcja w `deposit.py:216-226` minimalizuje **MAE**, co na rosnącym szeregu (model wiosna→lato
2026: 72 → 139 zł) systematycznie premiuje większy lag — porównanie ze starszym, niższym miesiącem
daje mniejszy błąd bezwzględny. Stąd 25,71 (lag 3) < 27,67 (lag 2) < 29,21 (lag 1).

**Warstwa 3 — rekonstrukcja łańcucha przestała działać od maja 2026 (przyczyna źródłowa).**
Z PDF-ów faktur:

| Faktura | „Sprzedaż energii elektrycznej" | „Depozyt z okresów poprzednich" | stan |
|---|---|---|---|
| 2026-02 | 402,83 | 37,34 | saldo prawdziwe |
| 2026-03 | 189,47 | 26,27 | saldo prawdziwe |
| 2026-04 | 114,14 | 72,48 | saldo prawdziwe |
| 2026-05 | 68,89 | **68,89** | **zaczepione (capped)** |
| 2026-06 | 63,24 | **63,24** | **capped** |
| 2026-07 | 44,22 | **44,22** | **capped** |
| 2026-08 | 57,57 | **57,57** | **capped** |

Od maja 2026 obie liczby są **identyczne co do grosza w każdym miesiącu**. Zweryfikowane
niezależnie: „Sprzedaż energii elektrycznej" = koszt energii pobranej (np. 08/2026:
103 kWh × 0,418 + 6 kWh × 0,627 = 46,81 net × 1,23 = **57,58**). Czyli Tauron przestał drukować w
tej linii **saldo depozytu** — drukuje tylko **kwotę pobraną**, zaczepioną na wartości rachunku za
energię. Prawdziwe saldo jest większe i na fakturze go nie ma („Wypełnij formularz online w Moim
TAURONie i poznaj szczegóły dotyczące rozliczenia depozytu").

Reżim zmienił się, bo latem eksport przewyższa import — depozyt przestał być co miesiąc do zera
konsumowany. Do kwietnia 2026 saldo < rachunku, więc linia 5 = prawdziwe saldo (stąd trafienia
co do grosza przy lagu 1).

**Skutki, wszystkie widoczne na żywo:**
- kolumna „Faktury (Tauron)" od maja 2026 mierzy rachunek za energię, nie zasilenie depozytu →
  fałszywe różnice +55,0% (2026-05), +73,0% (2026-04), +14,6% (2026-03);
- `anchor_balance = 0,00 zł` przy `balance_model = 329,45 zł` — bo `max(0, previous − used)` z
  zaczepionej faktury zawsze wychodzi 0;
- `balance_estimate = 242,88 zł` (kotwica 0 + „niezaksięgowane" 242,88) — liczba przypadkowa;
- `lots` skalowane `balance_estimate / balance_model` = 0,74× → zaniżona struktura wiekowa i
  prognoza przedawnień.

### Cel

Tabela ma **rozróżniać „jeszcze nie zaksięgowane" od „nie da się odczytać z faktury"** i przestać
publikować liczby, które nie są zasileniem depozytu. Lepiej pokazać brak danych niż złą liczbę.

---

## Etapy

Realizacja etapami, checkpoint po każdym (CLAUDE.md „Feature work"). Repo:
`/config/addons/pv_roi_tracker` (origin `miczu71/pv_roi_tracker`, branch `main`, HEAD = origin,
0.35.5). Praca na `main` zgodnie z `feedback_use_main_branch`.

### Etap 1 — parser: linia „Sprzedaż energii elektrycznej" + flaga capped

Pliki: `pv_roi_tracker/pv_roi_tracker/invoice_parser.py`,
`pv_roi_tracker/pv_roi_tracker/invoice_store.py`

- Nowy klucz w `_BUILTIN_PATTERNS` (~`invoice_parser.py:165`, obok `deposit_current`):
  ```python
  'energy_sale_total': [
      r'1\.\s*Sprzeda[żz]y?\s+energii elektrycznej\s+([\d ]+,\d{2})',
      r'Sprzeda[żz]y?\s+energii elektrycznej\s+([\d ]+,\d{2})\s*z',
  ],
  ```
  Dwa layouty potwierdzone w PDF-ach: `Sprzedaż energii elektrycznej` (2026-03..08) oraz
  `Sprzedaży energii elektrycznej` (2026-02). Wzorce idą przez `_patterns_for()`
  (`invoice_parser.py:212`), więc uczenie layoutów działa automatycznie.
- Nowe pole `energy_sale_gross_pln` na `InvoiceData` (obok `deposit_*_pln`,
  `invoice_parser.py:314-316`), ekstrakcja obok `deposit_used_pln` (`invoice_parser.py:918-920`),
  warning gdy nie znalezione.
- Flaga pochodna `deposit_capped: bool` — liczona przy parsowaniu:
  ```python
  deposit_capped = (deposit_used_pln is not None
                    and energy_sale_gross_pln is not None
                    and abs(deposit_used_pln - energy_sale_gross_pln) <= 0.02)
  ```
  Porównujemy z `deposit_used` (linia „Rozliczenie depozytu"), nie z `deposit_previous` — cap
  dotyczy kwoty pobranej łącznie, a starsze faktury miały niezerowy `deposit_current`.
- Oba pola przechodzą przez `invoice_store` (schema v2 → **v3**, migracja: brak pola = `None`,
  wypełniane przy reparse) i przez `effective_by_month()` (`invoice_store.py:329`) — nakładka z
  korekty musi obejmować te pola tak samo jak `deposit_*`.
- Istniejące faktury: **reparse** przez `/api/invoice/reparse` (PDF-y są w add-onie), bez
  ponownego wgrywania.

### Etap 2 — `deposit.py`: uczciwe statusy zamiast jednej etykiety

Plik: `pv_roi_tracker/pv_roi_tracker/deposit.py` (`calculate()`, linie 195-251)

- Rekonstrukcja łańcucha (`deposit.py:201-212`): gdy faktura `M−1` jest `deposit_capped`,
  `after(M−1)` jest **nieznane** (wiadomo tylko, że ≥ 0) → `implied(M)` nie jest wyliczalne.
  Zamiast liczyć `prev − 0`, pomiń wpis i zapamiętaj powód w `implied_status[mk] = 'capped'`.
- Każdy wiersz dostaje pole `status`:
  - `'ok'` — wartość zrekonstruowana,
  - `'unposted'` — brak faktury dla `export_month + lag` (naprawdę jeszcze nie zaksięgowane),
  - `'capped'` — faktura jest, ale linia depozytu zaczepiona na rachunku za energię,
  - `'gap'` — dziura w łańcuchu faktur (istniejący `continue` z `deposit.py:207-208`).
- Totals (`deposit.py:239-240`) już sumują tylko wiersze z `tauron is not None` — po zmianie
  miesiące capped wypadną z Σ. To zamierzone: Σ przestaje wchłaniać wartości, które nie są
  zasileniem. **Σ z faktur spadnie** względem obecnych 2501,20 zł — to poprawka, nie regresja.

### Etap 3 — detekcja lagu odporna na trend

Plik: `pv_roi_tracker/pv_roi_tracker/deposit.py` (linie 42-46, 216-226)

- `DEFAULT_POSTING_LAG = 2` → **1** (fizyczne minimum: depozyt za eksport M nie może trafić na
  fakturę wcześniejszą niż M+1).
- Detekcja liczona **wyłącznie na parach ze statusem `ok`** (bez capped, bez gap).
- Zamiast MAE — miara skalowo-niezmiennicza, żeby rosnący sezon nie premiował większego lagu:
  mediana `abs(log(implied / accrued))` po parach, gdzie obie wartości > 0. Na żywych danych
  wskazuje lag 1 (8 trafień co do grosza vs 0 dla lagu 2 i 3).
- Gdy par `ok` < `_MIN_LAG_SAMPLES` → fallback 1, nie 2.
- Zachować `_LAG_WINDOW = 6` i komentarz o erze nasyconego depozytu 2023-24 (CHANGELOG:893).

### Etap 4 — kotwica salda i skalowanie partii

Plik: `pv_roi_tracker/pv_roi_tracker/deposit.py` (linie 253-287)

- Gdy najnowsza faktura jest `deposit_capped`: `anchor_balance = None`,
  `balance_estimate = None`, `unposted_accrual` nadal liczone (informacyjnie).
- Nowe pole `anchor_source: 'faktura' | 'model'` w `DepositResult` — UI ma powiedzieć, skąd
  wzięło saldo.
- Skalowanie partii (`deposit.py:274-279`): gdy `balance_estimate is None` → `scale = 1.0`
  (czysty model FIFO). Usuwa obecne zniekształcenie 0,74×.
- `invoice_latest_balance` zostaje, ale przy capped opisujemy je jako **dolne ograniczenie**.

### Etap 5 — frontend: trzy etykiety zamiast jednej

Pliki: `pv_roi_tracker/pv_roi_tracker/static/app.js` (`renderReconSection`, 1588-1637),
`static/index.html:460-467`

- `app.js:1624` — zamiast jednego stringu, mapowanie po `r.status`:
  - `unposted` → „jeszcze niezaksięgowane"
  - `capped` → „nie do odczytania z faktury" + `title` z wyjaśnieniem (Tauron drukuje tylko kwotę
    pobraną, zaczepioną na rachunku za energię)
  - `gap` → „brak faktury w łańcuchu"
- KPI „Lag księgowania Taurona" (`app.js:1610`): fallback `|| 2` → `|| 1`.
- Nowe/poprawione KPI salda: pokazać `anchor_source`; gdy `balance_estimate is None` —
  „saldo z modelu (faktura nie pokazuje salda)".
- `reconNote` (`app.js:1634-1636`): dopisać zdanie o reżimie capped — od kiedy faktura przestaje
  pokazywać saldo, rekonstrukcja jest niemożliwa i miesiąc nie wchodzi do Σ.
- Cache-busting już jest (`?v=<version>` w `index.html`); bump wersji załatwia sprawę
  (`CLAUDE.md` → Frontend/Asset Caching).

### Etap 6 — testy i wydanie

- `pv_roi_tracker/tests/test_deposit.py` (istniejący `test_posting_lag_detection_from_invoice_chain`
  na linii 104-135 asertuje lag 2 — **wymaga aktualizacji**):
  - łańcuch z fakturami capped → wiersze `status='capped'`, wykluczone z totals,
    `anchor_balance is None`, `scale == 1.0`;
  - rosnący szereg modelu + prawdziwy lag 1 → detekcja zwraca 1 (regresja na dokładnie ten błąd);
  - brak faktur → wszystkie wiersze `status='unposted'`, Σ tauron = 0.
- `pv_roi_tracker/tests/test_invoice_parser.py`: `energy_sale_total` na obu layoutach; flaga
  `deposit_capped` True dla 2026-08 (57,57/57,57) i False dla 2026-04 (114,14 vs 72,48).
- `pv_roi_tracker/tests/test_invoice_store.py`: migracja schema v2 → v3.
- Uruchomić pełny zestaw: `cd /config/addons/pv_roi_tracker/pv_roi_tracker && python3 -m pytest`.
- Wydanie wg `feedback_pv_roi_release_checklist`: bump **obu** plików —
  `pv_roi_tracker/config.yaml:2` i `pv_roi_tracker/pv_roi_tracker/__init__.py:1` (Supervisor czyta
  ten pierwszy) → `0.36.0`. Push na `main`, **opublikowany** (nie draft) GitHub release z tabelami
  zmian encji/pól wg `feedback_release_notes`; README sekcja o rekonsyliacji
  (`README.md:75`) do aktualizacji. CHANGELOG.md — wpis z przyczyną źródłową (cap od 2026-05).
- Update add-onu przez Supervisor (`feedback_no_local_rebuild`): GH release → odświeżenie sklepu
  (`reference_supervisor_store_reload`) → update slug `29a4454d_pv_roi_tracker`.

---

## Weryfikacja end-to-end

1. `python3 -m pytest` w `pv_roi_tracker/` — wszystkie testy zielone (pokazać output, nie zakładać).
2. Po update add-onu: reparse faktur z UI, potem
   `curl -s http://172.30.33.15:8099/api/data` i sprawdzić w bloku `deposit`:
   - `posting_lag_months == 1`;
   - wiersze `2026-05..2026-08` mają `status == 'capped'`, `2026-04` i wcześniej `'ok'`;
   - `anchor_balance is None`, `anchor_source == 'model'`;
   - `lots[].remaining` sumują się do `balance_model` (skala 1,0), nie do 0,74× tego.
3. Playwright direct-IP (`reference_addon_playwright_direct_ip`): screenshot sekcji do
   `/config/playwright/pv_roi_036_recon.jpg` + `browser_console_messages(error)` —
   self-review przed pokazaniem użytkownikowi (`feedback_playwright_self_review`).
4. Sanity na liczbach: różnice % dla 2026-03 i wcześniej mają zostać bez zmian; wiersze od
   2026-04/05 wzwyż mają przestać pokazywać fałszywe +55%/+73%.

## Ryzyka

- **Σ z faktur spadnie** i „Różnica skumulowana" się zmieni — to zamierzone (usuwamy fałszywe
  wartości), ale trzeba to jawnie zakomunikować w release notes, żeby nie wyglądało na regresję.
- Wzorzec `energy_sale_total` może nie trafić w starsze layouty (2023-2024) — wtedy
  `deposit_capped = None`, zachowanie jak dotąd (fail-safe: brak flagi ≠ capped). Sprawdzić na
  wszystkich 39 fakturach po reparse, ile ma pole wypełnione.
- Reparse wszystkich faktur — `invoice_store` pisze atomowo z `.bak`, ale przed reparse zrobić
  kopię `invoices.json` (add-on ma `backup_share: /share/pv_roi_tracker`).
- Zmiana `DEFAULT_POSTING_LAG` wpływa też na `unposted_accrual` i „Saldo (szacunek)" na innych
  zakładkach — sprawdzić, czy żadna karta HA / sensor MQTT nie zakłada starej wartości.
