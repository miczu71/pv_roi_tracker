# PLAN E2 — backfill, API, MQTT symulacji taryf (wariant A)

Roadmapa: `docs/ROADMAP_TARIFFS.md`. Silnik E1 (`tariff_catalog`, `price_sources`, `tariff_sim`) jest gotowy i niepodpięty. E2 podpina go do `main.py`, `web.py`, `publisher.py`. Rollback: revert commitu (sensory MQTT znikają dzięki tombstone w `_TOMBSTONED_SLUGS`, `/data/tariff_sim.json` i `/data/tariff_prices.json` można skasować).

## Sprawdzone na żywo (2026-10-21, przed planem)
- Energy-Charts `price?bzn=PL`: pola `unix_seconds`, `price` (EUR/MWh), `unit`; zakres 2024-11-30..2026-10-20 = 44279 punktów w 1,5 s, krok 3600 s, potem 900 s, bez `null`.
- NBP `rates/a/eur/{a}/{b}`: `rates[].effectiveDate/mid`.
- PSE `pdgsz`: strony po 100 wierszy, `nextLink` (kod E1 po nim idzie), `dtime` = początek godziny lokalnej, `usage_fcst` 0..3, `is_active` jako bool JSON. Bez filtra `is_active` jeden dzień ma kilka wersji, ale kod filtruje po stronie klienta. Wniosek: dodać `and is_active eq true` do `$filter` (mniej stron).
- DST: wiosna 47 godzin (OK), jesień 24 (dwie godziny 02:00 zlane w jeden klucz, akceptowane).

## Decyzje
1. **Źródło poboru:** cache godzinowy battery_sim (`battery_store.load_hours(BATTERY_HOURS_PATH)`, `{klucz: (export, import)}`, od 2023-06). Bierzemy indeks 1 (import). Brak dodatkowego zapytania WebSocket. Zadanie taryf czyta plik, który odświeża `battery_job` (05:15).
2. **Zakres:** od `2024-12` do bieżącego miesiąca włącznie. Bieżący miesiąc to miesiąc do dziś (częściowy).
3. **Harmonogram:** `tariff_sim_job` codziennie 05:30 (po `battery_job` 05:15) i raz w tle przy starcie.
4. **Ustawienia opłat stałych:** domyślne `TariffSettings()` (3 fazy, `gt2800`, `sales_fee_net=0`). Kalibrację `sales_fee_net` robi się po obejrzeniu raportu rozjazdu na żywo (nie zgadujemy).
5. **12 miesięcy:** ostatnie 12 PEŁNYCH miesięcy (kończąc na poprzednim). Taryfa bez któregokolwiek z tych miesięcy (`available:false`) lub z `reliable:false` ma sumę `null` i nie bierze udziału w rankingu. Dziś więc `dyn_a` (od 2026-04) odpada, a `dyn_b` (od 2026-01) też.
6. **Najtańsza:** najmniejsza suma brutto 12 mies. wśród taryf z sumą. Oszczędność vs G12w = `gross_12m(G12w) - gross_12m(najtańsza)` (≥0; dla G12w jako najtańszej 0).
7. **Raport rozjazdu:** per miesiąc z fakturą rozliczeniową (`invoice_store.filter_billing(filter_real(load))`): `invoice_net` = suma pól `energy_amount_net + dist_var_amount_net + dist_jakosciowa_amount_net + dist_oze_amount_net + dist_kogeneracja_amount_net + fixed_total_net` (miesiąc pomijany, gdy któreś pole `None`). Porównanie z `G12w.net` z symulacji. `diff_pct = 100*(sim-inv)/inv`, `ok = abs(diff_pct) <= 3`. Dodatkowo `kwh_sim` vs `purchased_kwh` jako diagnostyka. Nie wpływa na wynik symulacji.
8. **Poprawki z verifiera E1:** `dyn_a` uwzględnia `dynamic_sales.confirmed` (tak jak `dyn_b`); test DST wymuszający logikę (dwie godziny 02:00 sumują się); test `1.11` rozróżniający co najmniej dwa lata; usunąć nieużywane pola `margin_gross_per_kwh` z kodu, jeśli niczego nie czytają (sprawdź grepem; nie ruszaj JSON).

## Kontrakt

### `pv_roi_tracker/tariff_job.py` (nowy, czysta logika, bez I/O poza podanymi argumentami)
`compute_payload(import_by_hour, catalog, settings, rdn, kompas, invoices, today, from_ym='2024-12') -> dict`
- `import_by_hour`: `{YYYY-MM-DDTHH: kWh}` (konwersję z `(export, import)` robi wywołujący, funkcja pomocnicza `import_series(hours) -> dict` w tym samym module).
- Zwraca:
```
{
  'generated_at': iso, 'from': 'YYYY-MM', 'to': 'YYYY-MM',
  'tariffs': ['G11','G12','G12w','G13','dyn_a','dyn_b'],
  'months': {ym: simulate_month(...)},          # jak w E1
  'current_month': {'ym': ym, 'gross': {tariff: float|None}},
  'last12': {'from': ym, 'to': ym,
             'gross': {tariff: float|None}, 'net': {tariff: float|None},
             'cheapest': tariff|None, 'saving_vs_g12w': float|None},
  'reconciliation': {ym: {'sim_net', 'invoice_net', 'diff_pct', 'ok', 'kwh_sim', 'kwh_invoice'}},
  'reconciliation_summary': {'months': n, 'ok_months': k, 'max_abs_diff_pct': x|None},
  'settings': {'phases','capacity_bracket','sales_fee_net'}
}
```
- Miesiąc z `available:false` w `months` wchodzi do wyniku bez zmian (UI E4 pokaże „brak”).
- `current_month.gross[t]` = `None`, gdy taryfa niedostępna w tym miesiącu.
- Funkcje `load_payload(path)` / `save_payload(payload, path)` (atomowy zapis tmp + `os.replace`, jak `price_sources.PriceCache.save`; błąd odczytu → `None`, nigdy wyjątek).

### `main.py`
- Stałe: `TARIFF_SIM_PATH` (`/data/tariff_sim.json`, env `TARIFF_SIM_PATH`), `TARIFF_PRICE_CACHE_PATH` (`/data/tariff_prices.json`, env `TARIFF_PRICE_CACHE_PATH`) obok `BATTERY_HOURS_PATH`; dopisz je do listy ścieżek w `_backup_data` tylko jeśli ta lista już zawiera analogiczne pliki (sprawdź linię ~271).
- `_tariff_state = {'payload': tariff_job.load_payload(TARIFF_SIM_PATH)}` obok `_battery_state`; `_web.update_tariff_sim(...)` przy starcie z wczytanym payloadem.
- `tariff_sim_job()` z lockiem jak `battery_job`: `hours = battery_store.load_hours(...)` → `import_series`; `cache = PriceCache(path)`; `cache.ensure('2024-12-01', dziś)`; `compute_payload(...)`; zapis; `_web.update_tariff_sim`; `_tariff_state['payload'] = ...`; `_record_job('tariff_sim', True/False, szczegół)`; wyjątek → `logger.exception` + `_record_job(..., False, 'symulacja taryf nie powiodła się')`. Brak godzin → `_record_job(False, 'brak godzinowych statystyk liczników')` i return.
- `scheduler.add_job(tariff_sim_job, CronTrigger(hour=5, minute=30), id='tariff_sim', name='Tariff simulation', **_JOB_DEFAULTS)` oraz wątek startowy `tariff-sim-boot` obok `battery-sim-boot`.
- `poll_and_publish`: do `pub.publish_roi(...)` dodaj `tariff_sim=_tariff_state['payload']`.

### `web.py`
- `_state['tariff_sim'] = None`; `update_tariff_sim(payload)` pod `_lock` (wzór: `update_battery_sim`).
- `GET /api/tariff_sim` → `jsonify({'ok': True, 'data': payload})`; gdy brak payloadu: `{'ok': True, 'data': None}` (200, nie 500).

### `publisher.py`
- 14 nowych `_Sensor` (po bloku `battery_expansion_*`), wszystkie `state_class` `None`, jednostka `PLN`, `device_class` `monetary` (jak `net_profit`), dla tekstowego `cheapest` bez jednostki:
  - `tariffsim_month_<t>` „PV Tariff Sim Month <T>” — koszt brutto bieżącego miesiąca do dziś, `t` ∈ `g11,g12,g12w,g13,dyn_a,dyn_b`;
  - `tariffsim_12m_<t>` „PV Tariff Sim 12M <T>” — suma brutto 12 mies.;
  - `tariffsim_cheapest` „PV Tariff Sim Cheapest” (np. `G12w`), `tariffsim_saving_vs_g12w` „PV Tariff Sim Saving vs G12w” (PLN/rok).
  (to daje 6+6+2 = 14).
- `_render_value` dostaje nowy parametr `tariff_sim: Optional[dict] = None` (ostatni, domyślnie `None`), `publish_roi` też; wartość `None` → `'unknown'` (istniejąca ścieżka).
- Klucze taryf w payloadzie mają wielkość liter jak w E1 (`G12w`, `dyn_a`); slug sensora zawsze małymi literami.

## Wykonawca
WORKTREE: z `wt new /home/claude/ha-config pv-roi-tariffs-e2` (repo `/config` = `~/ha-config`), katalog add-onu `addons/pv_roi_tracker/pv_roi_tracker`. Jedna paczka (pliki zależą od kontraktu `tariff_job`).

Pliki i zmiany:
1. `pv_roi_tracker/tariff_job.py` — NOWY wg kontraktu wyżej.
2. `tests/test_tariff_job.py` — NOWY (TDD, najpierw czerwone): 12 pełnych miesięcy i ranking (`cheapest`, `saving_vs_g12w`), taryfa z `available:false` w którymś z 12 mies. → suma `None` i poza rankingiem, `reliable:false` → suma `None`, bieżący miesiąc częściowy, rozjazd: dopasowanie `ok` ≤3% i `ok:false` >3%, faktura z polem `None` pomijana, brak faktur → `reconciliation == {}` i `max_abs_diff_pct is None`, `save_payload`/`load_payload` round-trip i uszkodzony plik → `None`.
3. `pv_roi_tracker/main.py` — stałe, `_tariff_state`, `tariff_sim_job`, cron, wątek startowy, `tariff_sim=` w `publish_roi`.
4. `pv_roi_tracker/web.py` — `update_tariff_sim`, `GET /api/tariff_sim`; test w `tests/test_web_tariff_sim.py` (NOWY): 200 z `data: None` i z payloadem.
5. `pv_roi_tracker/publisher.py` — 14 sensorów, `_render_value`/`publish_roi` z `tariff_sim`; rozszerz `tests/test_publisher.py` (liczba sensorów discovery w istniejących testach zwiększ o 14 tylko jeśli test ją sztywno zakłada; test renderu: `tariffsim_12m_g12w`, `tariffsim_cheapest`, `None` → `'unknown'`).
6. `pv_roi_tracker/price_sources.py` — `$filter` w `fetch_kompas_hourly` rozszerz o `and is_active eq true` (zachowaj filtr po stronie klienta); test w `tests/test_tariff_price_sources.py`, że URL zawiera `is_active`.
7. `pv_roi_tracker/tariff_sim.py` — `dyn_a`: `res['confirmed'] = res['confirmed'] and bool(sales.get('confirmed', False))` (wzór: `dyn_b`); usuń nieużywane pole `margin_gross_per_kwh`, jeśli grep nic go nie czyta. `tests/test_tariff_sim.py`: dopisz test na `dyn_a.confirmed`; wzmocnij test DST (dwie kolejne godziny o tym samym kluczu lokalnym sumują się w `import_by_hour`, wynik kWh = suma); test `1.11` sprawdza dwa różne lata (dzień tygodnia różny, święto w obu).

Komenda testu (na pierwszym planie): `cd <WORKTREE>/addons/pv_roi_tracker/pv_roi_tracker && uv run --no-project --with pytest --with-requirements requirements.txt pytest -q`.

DONE: wszystkie testy zielone (≥753 passed poprzednio + nowe, bez nowych skipów); `GET /api/tariff_sim` zwraca 200 w testach Flask; `publish_roi` publikuje 14 nowych tematów; `git status --short` w worktree pokazuje tylko pliki z listy powyżej.

NIE RÓB: nie commituj; nie ruszaj `tariffs/tauron.json`, `battery_sim.py`, `config.yaml`, `CHANGELOG`, plików poza listą; nie dodawaj zależności; nie zmieniaj istniejących sensorów MQTT ani ich `unique_id`; nie uruchamiaj żadnych wywołań sieciowych w testach (atrapy); nie wpisuj w kod ani testy danych prywatnych (neutralne nazwy).

Konwencje: skille `addon-conventions` i `tdd`, komentarze po polsku jak sąsiedni kod, bez nowych abstrakcji ponad kontrakt.

## Weryfikacja po etapie (ja, na żywo po wdrożeniu przy wydaniu)
- Testy lokalne pełne.
- Uwaga: wdrożenie add-onu to wydanie (jedno po E4). W E2 sprawdzam lokalnie na realnych danych: skrypt jednorazowy ładuje `/data`-podobne wejście? Nie ma dostępu do `/data` add-onu (osobny host), więc E2 sprawdzam na żywych cenach (Energy-Charts/PSE) z syntetycznym poborem, a rozjazd G12w vs faktury dopiero po wydaniu (`/api/tariff_sim`). Ryzyko: kalibracja `sales_fee_net` może wymagać poprawki po wydaniu.
