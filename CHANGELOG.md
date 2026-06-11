# Changelog

All notable changes to this project will be documented in this file.

## [0.16.1] — 2026-06-11

### Changed

- **Zakładka RCE vs RCEm — poprawione ostrzeżenie prawne** (źródło: lepiej.tauron.pl, nowelizacja ustawy o OZE z 27.11.2024): przejście na RCE godzinową jest **nieodwracalne** (oświadczenie w Strefie Prosumenta, powrót do RCEm niemożliwy); przy RCE godzinowej można wypłacić do 30% depozytu w 12 mies. (RCEm: 20%); współczynnik 1,23 od 2025-02 obowiązuje wszystkich prosumentów net-billing w obu trybach, więc nie wpływa na znak różnicy RCE−RCEm.
- Zaktualizowany docstring `rce_hourly.py` o podstawę prawną współczynnika 1,23 (potwierdza `_MULTIPLIER_FROM = 2025-02`).

### Entities / services touched

Brak — zmiana tekstów w UI i dokumentacji modułu.

---

## [0.16.0] — 2026-06-11

### Added

- **Zakładka „RCE vs RCEm"** — symulacja rozliczenia sprzedaży nadwyżek po cenie godzinowej RCE zamiast miesięcznej RCEm. Nowy moduł `rce_hourly.py`: godzinowy eksport z statystyk długoterminowych HA × ceny 15-min RCE; ceny dnia bieżącego z integracji `rce_pse` (atrybut `prices`), historyczne z oficjalnego REST API PSE (`api.raporty.pse.pl/api/rce-pln`, 1 request na miesiąc, cache w `/data/rce_hourly.json`). KPI, wykres przychodów, tabela miesięczna z pokryciem danych i rekomendacją (ROZWAŻ RCE / ZOSTAŃ PRZY RCEm / NEUTRALNA). Zamknięte i rozliczone miesiące są zamrażane w cache. Mnożnik VAT ×1.23 od 2025-02 — ta sama reguła co dla RCEm.
- **Sensor zdrowia `sensor.pv_roi_tracker_health`** (`ok`/`degraded`/`error`) z atrybutami per zadanie (poll, rcem, cpi, backup, rce_hourly, tariff_comparison, month_close) + jawna flaga `solcast_available` — koniec cichej degradacji projekcji Solcast. Nowa automatyzacja alertu w pakiecie `pv_roi_rcem_alert.yaml`.
- **Miesięczne podsumowanie push** po zamknięciu miesiąca (produkcja, oszczędności, ROI %, pozostało do spłaty, przewidywana spłata) przez `notify.family`. Nowa opcja `monthly_notify` (domyślnie `true`).
- **Konfigurowalny arbitraż baterii** — nowa opcja `battery_roundtrip_efficiency` (domyślnie 0.92). Add-on liczy oszczędności sam: `kWh z sensora battery_grid_charge_off_peak_monthly × (tariff_peak_price × sprawność − tariff_offpeak_price)`, zamiast czytać sensor szablonowy z zaszytą stawką 0.50 PLN/kWh (zostaje jako fallback).
- **Wersja add-onu w nagłówku UI** oraz w payload `/api/data` (`version`).
- **Kolumny r/r w Podsumowaniu rocznym** — produkcja i oszczędności rok-do-roku, liczone na bazie tych samych miesięcy (rok częściowy porównywany z tym samym wycinkiem roku poprzedniego).

### Fixed

- **`run.sh` nie eksportował `TARIFF_PEAK_PRICE` / `TARIFF_OFFPEAK_PRICE`** — opcje `tariff_peak_price`/`tariff_offpeak_price` z konfiguracji add-onu były ignorowane (zawsze działały wartości domyślne).
- **Parser CSV: etykieta `autokonsumpcja oszczędność` nie była rozpoznawana** — `_norm()` składa teraz znaki diakrytyczne (NFD, usunięcie Mn), więc warianty ę/e w etykietach arkusza nie psują dopasowania.

### Removed (porządki w repo — bez wpływu na działanie)

- Usunięte martwe duplikaty: `config.yaml`/`Dockerfile`/`build.yaml`/`run.sh`/`requirements.txt`/`pytest.ini`/`tests/` w katalogu głównym repo oraz 19 płaskich modułów `.py` (stan ~v0.15.4) w `pv_roi_tracker/`. **Uwaga:** przez te duplikaty pytest importował martwe kopie zamiast realnego pakietu — po sprzątnięciu suita testuje wydawany kod.

### Entities / services touched

| Encja / serwis | Rola |
|---|---|
| `sensor.pv_roi_tracker_health` | **NOWY** — zdrowie add-onu (MQTT discovery, atrybuty JSON) |
| `sensor.rce_pse_price` | **NOWE źródło** — dzisiejsza krzywa RCE (atrybut `prices`) |
| `sensor.power_meter_exported_energy_monthly` | statystyki godzinowe (WS `recorder/statistics_during_period`) dla symulacji RCE |
| `sensor.battery_grid_charge_off_peak_monthly` | **NOWE źródło** arbitrażu (kWh; PLN liczone w add-onie) |
| `sensor.battery_arbitrage_savings_monthly` | zdeprecjonowany fallback (zaszyta stawka 0.50 PLN/kWh) |
| `notify.family` | miesięczne podsumowanie po zamknięciu miesiąca |

---

## [0.15.6] — 2026-06-11

### Changed (refaktoryzacja wewnętrzna — bez zmian funkcjonalnych)

- **`web.py`:** usunięto zduplikowane `_MONTHS_PL` i `_month_label()` — używane są teraz wersje z `tariff_analysis.py` (jedno źródło prawdy dla etykiet miesięcy).
- **`live_reader.py`:** nagłówki autoryzacji REST (`Authorization: Bearer …`) budowane były identycznie w 3 funkcjach — wydzielono stałą modułową `_HEADERS`. Importy `json`/`websocket` przeniesione z wnętrza `get_ha_tariff_stats()` na poziom modułu (`websocket-client` jest już w `requirements.txt`).
- **`deposit.py` (kopia dev):** formuła aktualizacji salda depozytu FIFO (`available → consumed → balance`) powtarzała się w 5 miejscach (pętla główna, miesiąc bieżący, estymata fakturowa ×2, prognoza) — wydzielono helper `_apply_month()`. Ujednolicono zaokrąglanie `consumed` przed odjęciem (różnice < 1 grosz w estymacie fakturowej).

### Entities / services touched

Brak — czysta refaktoryzacja, payloady API i UI bez zmian.

---

## [0.15.5] — 2026-06-10

### Fixed

- **Wykres „Cena 1 kWh — ostatnie 7 dni" zawsze pokazywał dane z 03→04.06:** Przyczyną był brak parametru `end_time` w zapytaniu REST `/api/history/period/<start>`. HA domyślnie zwraca tylko 24 h od `start`, więc przy `start = now − 7 dni` okno wynosiło zawsze dobę sprzed tygodnia. Dodano `end_time = now` w `get_ha_history_7d()` (`live_reader.py`), co przywraca pełne 7 dni danych do chwili bieżącej.

### Changed

- **Usunięto wykres „Różnica dzienna G12w − Dynamiczna (7 dni)"** z sekcji TERAZ w zakładce Analiza taryf — redundantny wobec wykresu ceny.
- **Wykres „Cena 1 kWh — ostatnie 7 dni"** powiększony do pełnej szerokości zakładki (height: 320 px, poprzednio 220 px w układzie dwukolumnowym).

### Entities / services touched

| Encja | Rola |
|---|---|
| `sensor.calkowity_koszt_1_kwh_dynamiczna` | Cena dynamiczna (wykres 7-dniowy) |
| `sensor.power_tauron_g12w_current_price` | Cena G12w (wykres 7-dniowy) |
| `sensor.roznica_dzienna_g12w_vs_dynamiczna` | Różnica (backend — nadal pobierana, wykres usunięty z UI) |

---

## [0.15.1] — 2026-06-04

### Fixed

- **Zakładka "Analiza taryf" — sekcja HISTORIA:** W HA 2026.x usunięto REST endpoint
  `/api/recorder/statistics_during_period`; statystyki długoterminowe są dostępne
  wyłącznie przez WebSocket. `get_ha_monthly_stats()` w `live_reader.py` przepisano
  na transport WebSocket (`websocket-client`), zachowując identyczną sygnaturę i
  kształt wyniku. Dodano `websocket-client>=1.7` do `requirements.txt`.

- **Koszty G12w w zakładce taryf:** Dotychczas `compute_tariff_tab()` pobierał koszt
  G12w z wewnętrznych rekordów add-onu (`MonthlyRecord.purchase_cost_pln`). Zmieniono
  na `sensor.koszt_zmienny_g12w_miesieczny` — ten sam utility-meter co po stronie
  dynamicznej, jednolita metodologia dla obu taryf. Statystyki obu encji pobierane
  jednym zapytaniem WebSocket.

- **Parsowanie timestampów statystyk:** HA zwraca `start` jako epoch **milliseconds**
  UTC (nie sekundy). Poprzedni kod dzielił przez 1 i interpretował jako UTC, co
  dawało błędny miesiąc dla przejść UTC→CET/CEST (np. 30.11 23:00 UTC → grudzień
  w Warszawie, nie listopad). Poprawiono: `datetime.fromtimestamp(start_ms / 1000)`
  (lokalny TZ kontenera = Europe/Warsaw).

### Entities / services touched

| Encja | Rola |
|---|---|
| `sensor.symulacja_miesieczna_dynamicznej_faktura` | Miesięczny koszt zmienny taryfy Dynamicznej (źródło: HA Statistics) |
| `sensor.koszt_zmienny_g12w_miesieczny` | Miesięczny koszt zmienny taryfy G12w (źródło: HA Statistics) |

---

## [0.2.0] — 2026-05-03

### Added

- **`live_reader.py`** — reads six live HA entities via the Supervisor REST API (`SUPERVISOR_TOKEN`) and builds a `MonthlyRecord` for the current calendar month. Derived fields (`self_consumed_kwh`, `self_consumed_savings_pln`, `purchase_cost_pln`, `feedin_revenue_pln`, `specific_yield`) calculated internally.
- **`rcem_scraper.py`** — scrapes the PSE website for last month's RCEm feed-in price (PLN/MWh ÷ 1000 → PLN/kWh). Runs on days 11–20 at 20:00 UTC until the value is found. Persists to `/data/rcem_history.json`; on success calls `historic_store.backfill_rcem()` and triggers an immediate ROI recompute. Robust row-matching handles numeric (`2026-05`) and Polish month-name formats.
- **`month_close.py`** — APScheduler job firing at 23:55 on the last day of each month (before utility meters reset). Reads live HA values and appends the closing month to `historic.json` with `rcem_status='pending'`; the scraper backfills feed-in revenue later.
- **`publisher.py`** — MQTT discovery publisher. On connect: publishes 12 discovery payloads under one `PV ROI Tracker` device. On every recompute: publishes state for all 12 sensors with `retain=True`. LWT sets `pv_roi/availability` to `offline` if the add-on dies.
- **`main.py` steady-state loop** — APScheduler with three jobs (poll, month-close, RCEm). Startup RCEm catch-up: if today ≥ 11th and last month's price is missing, attempts an immediate scrape before the scheduler starts.
- `tzlocal` added to `requirements.txt` for APScheduler local-timezone cron triggers.

### Changed

- `live_reader.py` replaced the Phase 2 stub (`return None`) with real Supervisor REST calls.
- `config.yaml` and `__init__.py` bumped to `0.2.0`.

---

## [0.1.0] — 2026-05-03

### Added

- **`parser.py`** — pivot CSV parser supporting Polish metric row labels (NFC-normalised), Polish decimal comma format (`291,80 zł`, `4 249,56 zł` with NBSP thousands separator), Polish and English month abbreviations including `Paź` (October with `ź`), and full Polish month names. Skips the `SUMA` column to avoid double-counting. Handles the two-row summary block at the top of the export.
- **`models.py`** — `MonthlyRecord` dataclass with 11 metric fields and `rcem_status` (`confirmed` / `pending` / `missing`). `to_dict()` / `from_dict()` for JSON serialisation.
- **`historic_store.py`** — atomic write (`tmp` → rename) with `.bak` fallback on corrupt JSON. `load()`, `save()`, `append_month()` (idempotent), `backfill_rcem()` (touches only `feedin_price_pln_kwh`, `feedin_revenue_pln`, `rcem_status`).
- **`roi.py`** — pure ROI calculation. Formula matches the source spreadsheet: `ROI % = (subsidy + savings) / gross_investment × 100`. Outputs `RoiResult` with total return, payback months/years/date, monthly average savings, energy totals, and specific yield.
- **`concat.py`** — merges the frozen historic list with the volatile current-month record; current month wins on key collision.
- **`importer.py`** — fetches the Google Sheets published CSV with 3× exponential backoff; strips UTF-8 BOM.
- **`live_reader.py`** — Phase 1 stub returning `None`.
- **`cli.py`** — `import-csv`, `show`, `roi` subcommands.
- **`main.py`** — Phase 1 entry point: imports CSV on first start, then exits.
- **`config.yaml`**, **`Dockerfile`**, **`run.sh`** — HA add-on packaging for `aarch64`, `amd64`, `armv7`.
- 44 pytest tests covering parser, ROI engine, historic store, and concatenator.

### ROI formula detail

The spreadsheet defines:
- `status` = `dofinansowanie` (subsidy) + `oszczędności` (savings)
- `%` = `status / inwestycja` (gross investment)

This means the subsidy itself counts as a return on the gross investment. With data through 2026-04 the result is **80.94 %**, matching the spreadsheet exactly.
