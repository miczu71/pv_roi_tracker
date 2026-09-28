# PV ROI Tracker — „Pompa ciepła × PV": prawdziwy koszt grzania

Pełny plan (interview + design) — kopia zatwierdzonego planu z `/data/home/.claude/plans/`.
Zob. też szerszy backlog rozbudowy add-onu na końcu tego pliku.

## Kontekst

User poprosił o przegląd możliwości rozbudowy pv_roi_tracker (0.38.0 live, health `ok`, 518 testów).
Na pytanie o cel wybrał „wszystkie" (decyzje, mniej ręcznej roboty, UX, dług), więc zakres rozbito
na podprojekty, a jako **pierwszy** wybrano **pompę ciepła × PV**. Decyzja, którą ma to obsłużyć:
**ile naprawdę kosztuje ogrzewanie i CWU** (zł/mies., zł/sezon), po uwzględnieniu pokrycia z PV i baterii,
z uczciwym porównaniem sezonów.

Ustalenia z wywiadu:
1. Wycena: **obie wersje obok** — *gotówkowa* (PV = 0 zł, płacisz sieć wg G12w/G11 danej godziny)
   i *ekonomiczna* (kWh z PV = utracona sprzedaż po RCEm miesiąca).
2. Podział **grzanie / CWU / inne** godzinowo z trybu pracy pompy.
3. Atrybucja **proporcjonalna**: pompa dostaje ten sam miks źródeł co cały dom w danej godzinie;
   bateria ma koszt własny śledzony pulą (średnia ważona ładowań z PV lub z sieci).
4. Zakres v1: **zakładka „Pompa ciepła"**, **porównanie sezonów ze stopniodniami (HDD)**,
   **linia w pushu miesięcznym**. Bez nowych sensorów MQTT.
5. Architektura: **godzinowo w PV ROI** (wzorzec battery_store/battery_sim), nie miesięcznie,
   nie w add-onie Heiko.

Fakty z danych, które kształtują projekt:
- `sensor.energia_pompa_ciepla_energy` (total_increasing, kWh): LTS od 2023-06 (jak historia PV),
  ~1,9 MWh/rok (I 2026: 432 kWh, lato ~50 kWh). Miesiąc 2023-05 ma artefakt −5956 (start licznika) → start 2023-06.
- `sensor.pompa_heating` / `sensor.pompa_hot_water` (history_stats, dzienne narastające godziny,
  measurement): LTS od 2023-10 — pokrywa 3 sezony grzewcze.
- Temperatura: `sensor.openweathermap_temperature` LTS dopiero od 2024-07 → **`sensor.termometr_dwor_temperature`**
  (od 2023-09) jako domyślne źródło HDD.
- Ceny zakupu: **faktury** mają per-miesiąc `peak_gross`/`offpeak_gross` (G11 do 2024-12 = tylko `peak_gross`, flat);
  `tariff_config` sięga tylko 2026-02 → nie nadaje się do historii.
- Zimą pompa pracuje nocą/rano, PV w południe → podział miesięczny zawyżyłby pokrycie z PV; stąd godzinowo.
- Add-on Heiko wycenia każdą kWh pompy po cenie sieci (brak PV/baterii) — ten projekt to uzupełnia, nie dubluje.

## Projekt

### 1. Dane godzinowe — `live_reader` + `heatpump_store.py` (nowy)
- Rozszerzyć `live_reader._ws_statistics` o parametr `types` (domyślnie `['change']` — zero zmian dla obecnych wywołań).
- Nowa `live_reader.get_heatpump_hours(start, end)` → `{'YYYY-MM-DDTHH': {...}}` (czas lokalny, kumulacja
  kolizji DST jak w `get_hourly_energy`): `produced, exported, imported, batt_charge, batt_discharge`
  (role z `get_energy_dashboard_sources()`), `hp_kwh` (change), `heat_h`, `dhw_h` (delta `max` godzina-do-godziny;
  w godzinie 00 lub przy spadku = sama wartość `max`, bo licznik dzienny się zeruje), `t_out` (mean).
  Ujemne `change` pomijane z ostrzeżeniem (wzorzec istniejący).
- Pobieranie **w paczkach miesięcznych** (limit rozmiaru WS), od 2023-06.
- `heatpump_store.py`: cache `/data/heatpump_hours.json` (wersjonowany, zapis atomowy — kopia wzorca `battery_store`),
  dociąganie końcówki z 48 h zakładką (jak `battery_job`).

### 2. Obliczenia — `heatpump.py` (nowy, czyste funkcje, bez I/O)
Na godzinę:
- `load = produced + imported + batt_discharge − exported − batt_charge` (≥ 0)
- `pv_net = max(0, produced − exported)`; `charge_from_pv = min(batt_charge, pv_net)`;
  `charge_from_grid = batt_charge − charge_from_pv`
- źródła domu: `pv = pv_net − charge_from_pv`, `batt = batt_discharge`, `grid = max(0, imported − charge_from_grid)`
- `share = min(1, hp_kwh / load)` → `hp_pv, hp_batt, hp_grid` proporcjonalnie; nadwyżka `hp > load` → do sieci
  + licznik godzin-anomalii (flaga jakości).
- **Cena sieci godziny**: miesiąc z `offpeak_gross` na fakturze → G12w wg `battery_sim._is_peak_hour`
  (dni robocze 06–13, 15–22, święta PL); inaczej flat `peak_gross`. Fallback: rekord `buy_price` → `tariff_config`.
- **RCEm miesiąca** (`feedin_price` z rekordu; bieżący miesiąc → `rce_hourly.estimate_current_month_feedin_price`, flaga „szac.").
- **Pula baterii** (E, C_cash, C_econ): ładowanie `E += charge × battery_roundtrip_efficiency`,
  `C_cash += charge_from_grid × cena`, `C_econ += charge_from_grid × cena + charge_from_pv × RCEm`;
  rozładowanie po średnim koszcie jednostkowym puli. Start pusty 2023-06.
- Koszt pompy: `cash = hp_grid × cena + hp_batt × unit_cash`; `econ = cash_grid + hp_batt × unit_econ + hp_pv × RCEm`.
- Podział trybu: udział `heat_h : dhw_h` w godzinie; oba 0 przy `hp_kwh > 0` → „inne" (postój/cyrkulacja/antyzamarzanie).

Agregaty:
- **Miesiąc**: kWh {grzanie, CWU, inne, razem}, źródła {PV, bateria, sieć szczyt, sieć dolina}, koszt got./ekon.
  (także per tryb), śr. zł/kWh, % pokrycia PV+bateria, HDD, kontrola: Σ godzin vs miesięczne LTS licznika pompy.
- **Sezon** IX–VIII (etykieta „2025/26"): sumy + HDD (baza 15 °C, doba = średnia z godzinowych mean) +
  kWh grzania/HDD, zł grzania/HDD (got./ekon.); bieżący sezon także „do tej samej daty" vs poprzednie.
- Blok `heatpump` w `/api/data` (`null`, gdy funkcja wyłączona/brak danych), liczony w jobie, serwowany z pamięci.

### 3. Konfiguracja (`config.yaml`, opcjonalne z domyślnymi)
`heatpump_energy_entity` (domyślnie `sensor.energia_pompa_ciepla_energy`; pusty = funkcja wyłączona),
`heatpump_heating_hours_entity`, `heatpump_dhw_hours_entity`, `outdoor_temp_entity`
(`sensor.termometr_dwor_temperature`), `hdd_base_temp` (15).

### 4. Job
`heatpump_job` w `main.py` (wzorzec `battery_job`: lock, fetch → cache → compute → `_web.update_heatpump`,
`_record_job('heatpump', …)` do sensora health); cron raz dziennie + przebieg przy starcie w tle.

### 5. UI — zakładka „🔥 Pompa ciepła" (`static/index.html|app.js|app.css`)
KPI bieżącego sezonu (koszt got./ekon., pokrycie PV+bateria %, kWh) → wykres słupkowy miesięczny kWh wg źródła
(PV / bateria / sieć dolina / szczyt) z linią kosztu → tabela sezonów (kWh grzanie/CWU, HDD, kWh/HDD, zł/HDD, koszt,
wiersz „do tej samej daty") → tabela miesięczna. Chart.js już vendored. Cache-busting `?v=` już jest.

### 6. Push miesięczny
Linia w `_monthly_summary_notification` (main.py): „Pompa ciepła: X kWh (grzanie Y / CWU Z), z sieci Q zł
(ekon. W zł), PV+bateria pokryły P%". Przed wysłaniem świeży `heatpump_job(fetch)`; brak danych → linia pominięta.

### 7. Testy (`tests/test_heatpump.py`, `tests/test_heatpump_store.py`)
Bilans źródeł godziny (Σ = hp_kwh), `hp > load`, pula baterii (PV-only ⇒ cash 0; grid-only ⇒ cena doliny),
G11 vs G12w + święto, reset licznika dziennego trybu o 00, kolizja DST jesienią, HDD, granica sezonu IX,
cache: dociąganie z zakładką.

## Etapy (każdy kończy się checkpointem i wymaga „go" na następny)

| Etap | Zakres | Wydanie |
|---|---|---|
| 0 | Zapis tego planu + backlogu reszty podprojektów (ten plik, commit) | — |
| 1 | Backend: §1–4, §7, blok `heatpump` w `/api/data`, CHANGELOG/README. **Checkpoint**: tabela sezonów z żywych danych + kontrola Σ vs licznik + ręczne sprawdzenie jednej zimowej doby | 0.39.0 |
| 2 | UI zakładki (§5), weryfikacja Playwright (screenshot + konsola, direct-IP `172.30.33.15:8099`) | 0.40.0 |
| 3 | Linia w pushu (§6) | 0.40.1 |

## Ryzyka

- Pierwsze pobranie ~3,3 roku godzinowych LTS (~29 tys. godzin × ~10 encji) — paczki miesięczne, job w tle,
  nie blokuje poll-loopu.
- Licznik pompy może nie być mierzony tym samym torem co licznik sieci → `hp > load` w części godzin; liczone i pokazywane jako flaga.
- history_stats trybu to dzienny licznik — delta `max` w godzinie jest przybliżeniem (dokładność do minut).
- Pula baterii startuje pusta 2023-06 — pierwsze dni lekko zaniżone, pomijalne.
- Nowe opcje Supervisora z domyślnymi — dodatkowo defaulty w kodzie (`options.get(..., default)`), żeby stara instalacja startowała bez edycji opcji.
- Brak licznika ciepła → **brak COP i zł/kWh ciepła**; porównanie sezonów tylko przez HDD.

## Weryfikacja

1. `pytest` — pełny pakiet zielony (518 + nowe).
2. Po wdrożeniu 0.39.0: `curl 172.30.33.15:8099/api/data` → `heatpump`: Σ kWh miesięcy == miesięczne LTS licznika pompy
   (±1%), Σ źródeł == kWh pompy, liczba godzin-anomalii mała; ręczne przeliczenie jednej styczniowej doby 2026 z LTS.
3. Sensor health `ok` z jobem `heatpump`; restart add-onu → cache przetrwał, brak ponownego pełnego pobrania.
4. Etap 2: Playwright screenshot zakładki desktop + mobile, `browser_console_messages(error)` puste, zapis do `/config/playwright/`.
5. Etap 3: wywołanie ścieżki pushu (test jednostkowy + ręczny podgląd treści w logu).

## Backlog dalszej rozbudowy (kolejność do ustalenia z userem po tym podprojekcie)

- **Auto-import faktur z maila (IMAP)** — mniej ręcznej roboty, dziś PDF wgrywany ręcznie co miesiąc.
- **Symulator +kWp paneli** — „czy dołożyć paneli?": godzinowa symulacja jak Magazyn +5 kWh, z net-billingiem i depozytem.
- **UX / dashboard HA** — widok „PV ROI" w Lovelace z kluczowymi liczbami r/r, uproszczenie 10 zakładek na mobile.
- **Dług depozytowy**: zegar 12-mies. od zaksięgowania (nie eksportu) — B8 z code review 2026-07; który balance
  (`balance_model` vs `balance_estimate`) jest headline; retro-fix `needs_training` faktury 2024-01;
  parsowanie linii zasilenia (zasilenie/top-up) depozytu zamiast tylko zużycia.
