# PLAN E1 — silnik symulacji taryf (wariant A: ten sam pobór wyceniony w każdej taryfie)

Roadmapa: `docs/ROADMAP_TARIFFS.md`. Nic nie jest podpięte do `main.py`/`web.py`/`publisher.py` (to E2), więc rollback = revert commitu.

## Decyzje z wywiadu (2026-10-21)
1. **Dynamiczna w dwóch wariantach:**
   - `dyn_a` — TGE RDN Fixing I + marża Tauron + dystrybucja G12w (strefy G12w, stawki G12w).
   - `dyn_b` — stała cena sprzedaży Tauron („Prąd ze zmienną dystrybucją”) + dystrybucja G14dynamic (strefy S1–S4 godzina po godzinie).
2. **Ceny RDN:** Energy-Charts API (`bzn=PL`, EUR/MWh; do 2025-09 godzinowe, później 15-min → średnia godzinowa) + kurs EUR/PLN z NBP. Zweryfikowane 2026-10-21: 2025-01-15 średnia 199,5 EUR ≈ 859 PLN vs RCE PSE 859,4 PLN.
3. **Strefy S1–S4 historycznie:** PSE `https://api.raporty.pse.pl/api/pdgsz` (godzinowo, `usage_fcst` 0..3). Mapowanie: 0→S1, 1→S2, 2→S3, 3→S4 (jak w `energy_simulation.yaml`: recommended_usage=0, normal=1, saving=2, restriction=3).
4. **2025:** zamrożona cena 0,50 zł/kWh netto we wszystkich strefach i taryfach (już wpisane w `tauron.json`). Dystrybucja 2025 jest `confirmed:false` (stawki po 1.07.2025) — wynik 2025 raportować z flagą `confirmed` z okresu.
5. **Pobór:** bierzemy IMPORT z sieci (`get_hourly_energy` → `(export, import)`, indeks 1), nie zużycie — rachunek dotyczy energii kupionej. Eksport (net-billing) jest taki sam w każdej taryfie, poza symulacją.

## Kontrakt modułów

### `tariff_catalog.py`
- `load_catalog(path=None) -> dict` — domyślnie `tariffs/tauron.json` obok modułu.
- `period_for(catalog, d: date, overrides=None) -> dict` — okres z `valid_from <= d <= valid_to`; wpis z `inherits`/`override` rozwiń do pełnego okresu; `overrides` (dict) scal głęboko; brak okresu → `LookupError`. Zwraca kopię z polem `confirmed`.
- `zone_for(tariff, dt: datetime, catalog) -> str` — `tariff` ∈ `G11,G12,G12w,G13`. Strefy z `catalog["zones"]`: G11 `all`; G12 `day`/`night`; G12w `peak`/`offpeak`; G13 `morning_peak`/`evening_peak`/`other`. Przedziały godzin `[a,b)`, przedział `[22,6]` przechodzi przez północ. Święta i weekendy: G12w cała doba `offpeak`, G13 cała doba `other`; G12 bez zmian (codziennie). Święta z `battery_sim.polish_holidays(year)` (import, nie kopiuj).
- `rates_per_kwh(tariff, period, zone) -> dict` — `{energy, dist, fees}` netto zł/kWh, gdzie `fees = quality_net + oze_net + cogeneration_net`; `energy`/`dist` z `energy_net[tariff][zone]`/`dist_var_net[tariff][zone]`. Wartość `null` → `ValueError` (brak cichego zera).
- `fixed_monthly(period, settings) -> dict` — netto zł/mies.: `network` (`network_3ph` gdy `settings.phases==3`, inaczej `network_1ph`), `subscription_dist`, `capacity` (`capacity_by_annual_kwh[settings.capacity_bracket]`), `transitional` (`transitional_by_annual_kwh[...]` jeśli pole istnieje w okresie, klucz: `lt500`/`500_1200`/`gt1200` wg bracket; bracket `1200_2800` i `gt2800` → `gt1200`), `sales_fee` (`settings.sales_fee_net`). Defaulty ustawień: `phases=3`, `capacity_bracket="gt2800"`, `sales_fee_net=0.0` (kalibracja z fakturami w E2). Dataclass `TariffSettings`.

### `price_sources.py`
- Wszystkie funkcje sieciowe przyjmują `http_get(url) -> dict` (domyślnie `urllib` z timeoutem 40 s) — testy podają atrapę.
- `fetch_rdn_hourly(date_from: str, date_to: str, http_get=None) -> dict[str, float]` — `{YYYY-MM-DDTHH: PLN/MWh}` w czasie lokalnym (Europe/Warsaw). Energy-Charts `price?bzn=PL&start=&end=` zwraca `unix_seconds` (UTC) i `price` (EUR/MWh); 15-min uśrednij do godziny; przelicz po kursie z `fetch_eurpln(...)` z dnia (ostatni dostępny kurs z dni poprzednich).
- `fetch_eurpln(date_from, date_to, http_get=None) -> dict[str, float]` — NBP tabela A, `https://api.nbp.pl/api/exchangerates/rates/a/eur/{from}/{to}/?format=json`, max 93 dni na zapytanie (dziel zakres); wypełnij weekendy ostatnim kursem.
- `fetch_kompas_hourly(date_from, date_to, http_get=None) -> dict[str, str]` — `{YYYY-MM-DDTHH: "S1".."S4"}` z `pdgsz` (`business_date ge/le`, pole `dtime` lokalne `YYYY-MM-DD HH:MM`, pole `usage_fcst`); tylko wiersze `is_active`.
- `PriceCache(path)` — JSON `{rdn:{}, kompas:{}, eurpln:{}}`, zapis atomowy (tmp + `os.replace`, jak `rce_hourly._save_cache`); `ensure(date_from, date_to)` dociąga brakujące dni < dziś (dzień bieżący nie jest cache'owany); błąd sieci → `logger.warning` i zostaje to, co jest (pokrycie raportuje silnik).

### `tariff_sim.py`
- `simulate_month(ym: str, import_by_hour: dict[str, float], catalog, settings=None, rdn=None, kompas=None, overrides=None) -> dict`.
  - `import_by_hour`: `{YYYY-MM-DDTHH: kWh}` (z `get_hourly_energy` bierz indeks 1; konwersję robi wywołujący).
  - Taryfy zwracane pod kluczami `G11, G12, G12w, G13, dyn_a, dyn_b`.
  - Koszt godziny netto = `kwh * (energy + dist + fees)`.
    - `G*`: wg `rates_per_kwh` dla strefy z `zone_for`.
    - `dyn_a`: `energy = max(rdn_pln_mwh/1000, min_price_net) + margin_net` (jeśli `min_price_net` ustawiony w `dynamic_sales`, inaczej bez podłogi — ujemne ceny zmniejszają koszt), `dist`/`fees` jak G12w w tej godzinie (strefa G12w).
    - `dyn_b`: `energy` = cena G11 z okresu (lub `overrides`, zob. sekcja `tauron.json`), `dist = dynamic_dist_var_net[S?]` wg `kompas`, `fees` jak wyżej.
  - Wynik taryfy: `{kwh, energy, dist, fees, fixed, net, vat, gross, confirmed, coverage_pct, missing_kwh}`; `gross = (variable+fixed) * catalog._meta.vat`, `vat = gross - net`; `fixed` z `fixed_monthly` (dla `dyn_b` i `dyn_a` identyczne jak G12w). `confirmed` = `period.confirmed` (False gdy któryś okres miesiąca niepotwierdzony).
  - Brak ceny RDN / strefy S dla godziny z poborem > 0: godzina NIE jest liczona po cichu jako 0 — jej kWh trafia do `missing_kwh`, `coverage_pct = 100*(kwh-missing)/kwh`; przy `coverage_pct < 95` taryfa dynamiczna ma `"reliable": false`.
  - Miesiąc obejmujący dwa okresy cennika (np. 2026-01 → okres styczniowy) liczony godzina po godzinie wg okresu daty godziny.
- `simulate_range(from_ym, to_ym, ...) -> dict[str, dict]` — pętla po miesiącach; `import_by_hour` podaje wywołujący jako jeden słownik (filtr po prefiksie `ym`).

### `tariffs/tauron.json` — uzupełnienie
- W `dynamic_sales` dodaj `dynamic_sales_b`: `{"energy_same_as": "G11", "confirmed": false, "note": "cena stała oferty „Prąd ze zmienną dystrybucją” niepotwierdzona (cennik PDF nieznaleziony); strona dla firm: część sprzedażowa jak Taryfa Sprzedawcy G11; brak stref po stronie sprzedaży; opłata handlowa 0 zł/mc niepotwierdzona dla tej oferty", "sources": ["https://www.tauron.pl/dla-domu/prad/prad-z-usluga/prad-dynamiczna-dystrybucja", "https://www.tauron.pl/dla-firm/prad/wspolnoty-mieszkaniowe/prad-dynamiczna-dystrybucja"]}`.
- `dyn_b`: `energy` = `energy_net["G11"]["all"]` z okresu godziny; wynik taryfy ma `confirmed: false` zawsze. Nadpisanie przez `overrides["dynamic_sales_b"]["fixed_price_net_per_kwh"]` (jeśli podane, ma pierwszeństwo).

## Wykonawca
Pliki do utworzenia/zmiany (WORKTREE z `wt new`, repo `/home/claude/ha-config`, katalog add-onu `addons/pv_roi_tracker/pv_roi_tracker`):
1. `pv_roi_tracker/tariff_catalog.py` (nowy)
2. `pv_roi_tracker/price_sources.py` (nowy)
3. `pv_roi_tracker/tariff_sim.py` (nowy)
4. `tests/test_tariff_catalog.py` (nowy): granice stref (06:00/13:00/15:00/22:00, G13 lato/zima 19/16/21), święta (1.11 = piątek/sobota wg roku, Wielkanoc), weekend G12w/G13, przejście przez północ G12, okresy 2025/2026-01/2026-02, `null` → ValueError.
5. `tests/test_tariff_price_sources.py` (nowy): atrapa `http_get`: 15-min→godzina, EUR→PLN z kursem weekendowym, zakres NBP > 93 dni, `pdgsz` mapowanie 0..3→S1..S4, cache zapis/odczyt, błąd sieci nie kasuje cache.
7. `tariffs/tauron.json` (tylko dodanie `dynamic_sales_b`)
6. `tests/test_tariff_sim.py` (nowy): VAT, rozbicie energia/dystrybucja/opłaty/stałe, cena RDN ujemna (bez podłogi i z `min_price_net`), brak ceny → `missing_kwh` i `reliable:false`, przełom lato/zima G13, DST jesień (dwie godziny 02:00 w jednym kluczu), `dyn_b` ze strefami S, miesiąc 2026-01 vs 2026-02 (jakościowa 0,0331 vs 0,0332).

Komenda testu: `cd <WORKTREE>/addons/pv_roi_tracker/pv_roi_tracker && uv run pytest tests/test_tariff_catalog.py tests/test_tariff_price_sources.py tests/test_tariff_sim.py -q` oraz pełne `uv run pytest -q` (bez regresji).

Konwencje: skill `addon-conventions` i `tdd` (czerwone testy najpierw), komentarze i docstringi po polsku jak sąsiedni kod, typy jak w `rce_hourly.py`/`battery_sim.py`, `logger = logging.getLogger(__name__)`, żadnych nowych zależności.

DONE:
- 3 moduły + 3 pliki testów istnieją, wszystkie nowe testy przechodzą, pełne `uv run pytest -q` zielone.
- Test regresyjny z liczbami: godzina roboczego dnia 10:00 w G12w 2026-03 (energy 0,6220 + dist 0,3298 + fees 0,0332+0,0073+0,0030 = 0,9953 zł/kWh netto → 1,2242 brutto) oraz 14:00 (0,4130+0,0512+0,0435 = 0,5077 netto).
- `git status --short` pokazuje wyłącznie pliki z listy powyżej.

NIE RÓB:
- Nie ruszaj `main.py`, `web.py`, `publisher.py`, `battery_sim.py` (tylko import `polish_holidays`), `rce_hourly.py`, `live_reader.py`, `config.yaml`, `CHANGELOG.md`, wersji.
- Dodaj do `tauron.json` tylko klucz `dynamic_sales_b`, nie zmieniaj istniejących stawek; nie commituj; nie wykonuj żywych wywołań sieci w testach; nie dodawaj zależności; nie dotykaj HA ani `/config/packages`.

## PARAMETRY
Zamknięte: patrz `dynamic_sales_b` wyżej (założenie G11, niepotwierdzone). Test `dyn_b` ma sprawdzić zarówno G11 jako cenę, jak i pierwszeństwo `overrides`.
