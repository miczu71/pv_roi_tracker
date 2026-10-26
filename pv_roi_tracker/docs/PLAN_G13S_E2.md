# PLAN G13s E2 — planowanie ładowania z sieci (wariant C, DP)

Etap 2 z `ROADMAP_G13S_GRIDCHARGE.md` (decyzje usera tam). Kod w `addons/pv_roi_tracker/pv_roi_tracker/pv_roi_tracker/` (dalej `PKG/`), testy w `addons/pv_roi_tracker/pv_roi_tracker/tests/` (w worktree prefiks `addons/pv_roi_tracker/pv_roi_tracker/`). Czysta logika, nic niepodpięte (E3 podpina do payloadu/UI) → zero wpływu na żywe sensory.

## Decyzje projektowe E2
- **Doba lokalna = niezależny problem DP.** Klucze `YYYY-MM-DDTHH` grupowane po `key[:10]`, godziny posortowane (doby DST mają 23/25 kluczy — bez wyjątków). Godzina, dla której `price_fn(key)` zwraca `None`, jest pomijana (brak akcji); doba bez żadnej ceny → pominięta.
- **Stan** = energia zmagazynowana `E` na siatce `step_kwh` (domyślnie 0,2) od `floor = usable_kwh * soc_floor` do `usable_kwh` (domyślnie 0,8 → 5,0 kWh; zakres `range = usable_kwh − floor` = 4,2). Liczba kroków `n = round(range/step)`, krok efektywny `range/n`. Start doby = `floor`, koniec doby dowolny ≥ `floor` (bez wartości końcowej — zachowawczo; brak przenoszenia SoC między dobami).
- **Akcje godziny** (jedna na godzinę: ładuj ALBO rozładuj; `P = cfg.power_kw`, `eff = sqrt(cfg.roundtrip_eff)`):
  - ładuj o `Δ` zmagazynowanych kWh: pobór po stronie sieci/PV `x = Δ/eff ≤ P`; część `min(x, export_h)` z PV po koszcie `rcem(ym)` (utracona sprzedaż), reszta z sieci po koszcie `price_h + wear`, gdzie `wear = throughput_cost_kwh(cfg)` (zużycie liczone tylko na kWh z sieci — ładowanie z PV jest takie samo jak w B). Gdy `rcem > price_h + wear`, najpierw sieć, potem PV (tańsze najpierw).
  - rozładuj o `Δ`: dostarczone `d = Δ·eff ≤ min(import_h, P)`; wartość `price_h · d` (uniknięty zakup). Brak oddawania do sieci.
- **Limit cykli**: energia zmagazynowana ładowana **z sieci** ≤ `max_cycles · range` na dobę (domyślnie 2 → 8,4 kWh). Realizacja bez wymiaru stanu: najpierw DP bez limitu; jeśli przekracza limit, relaksacja Lagrange'a — kara `λ` zł/kWh za ładowanie z sieci, bisekcja `λ` (maks. 20 iteracji, górna granica = max cena doby), wybierz najmniejsze `λ` spełniające limit; rozwiązanie zwrócić z tej iteracji (raportowana oszczędność BEZ kary λ).
- **Oszczędność doby** = Σ wartości rozładowania − koszt energii z sieci (`price·x_grid`) − `wear · x_grid` − `rcem · x_pv`. (= oszczędność netto względem braku modułu; PV-owe zużycie modułu jak w B, bez `wear`.) Zwracane osobno: `savings_pln`, `wear_pln`, `grid_charged_kwh` (po stronie sieci), `pv_charged_kwh`, `discharged_kwh`.
- **Gwarancja C ≥ B**: wariant „tylko PV” (B) jest dopuszczalną ścieżką DP o zerowym `wear`, więc przy tym samym dziennym resecie SoC oszczędność C ≥ oszczędność ścieżki PV-only. (Różnica względem `simulate_s1`: B przenosi SoC między dobami, C startuje doby z `floor` — odnotować w `variant_c_note` w E3.)
- **Wariant B z limitem SoC**: `tariff_battery.simulate_variant_b(..., soc_floor=0.0)` — nowy parametr; `cfg_b = replace(cfg, arbitrage_enabled=False, usable_kwh=cfg.usable_kwh*(1-soc_floor))`. Domyślnie 0 (bez zmiany zachowania i istniejących testów); E3 przekaże 0,16.
- **Plany okien**: dla taryfy i grupy `(sezon, typ_dnia)` (`summer` = miesiące 4–9, `winter` = reszta; typ dnia z `tariff_catalog._is_day_off(date)` → `dayoff` / `workday`): `charge_hours` = godziny, w których sieć ładowała w ≥50% dób grupy, `discharge_hours` analogicznie, `days`, `avg_saving_pln` (średnia dobowa), `avg_grid_kwh`. Tylko doby z jakąkolwiek akcją liczą się do `days` z akcją; `days_total` = wszystkie doby grupy (progi 50% względem `days_total`).
- **Wydajność** (czysty Python, brak numpy — nie dodawać zależności): doba bez eksportu PV i bez opłacalnego spreadu (`max_price·eff² − min_price − wear ≤ 0`) → oszczędność 0 bez DP. Transition tylko w zasięgu mocy (`|Δ| ≤ P·eff` / `P/eff`). Cel: 700 dób × 4 taryfy < 20 s (zmierzyć w teście oznaczonym `@pytest.mark.slow`-bez-markera: zwykły test z limitem 60 s na syntetycznych 120 dobach × 4 taryfy).

## Interfejs (dosłownie)
Nowy plik `PKG/tariff_grid_charge.py`:
```python
GRID_TARIFFS = ('G12', 'G12w', 'G13', 'G13s')

def solve_day(prices, export_kwh, import_kwh, rcem, cfg, *, soc_floor=0.16,
              max_cycles=2.0, step_kwh=0.2) -> dict:
    """prices/export_kwh/import_kwh: równe listy (po godzinach doby, brak ceny = None);
    zwraca {'savings_pln','wear_pln','grid_charged_kwh','pv_charged_kwh','discharged_kwh',
            'charge_hours':[idx...], 'discharge_hours':[idx...]} (idx = pozycja w liście)."""

def simulate_variant_c(hours, cfg, catalog, rdn, kompas, rcem_by_ym, from_ym, to_ym,
                       overrides=None, *, soc_floor=0.16, max_cycles=2.0,
                       step_kwh=0.2, tariffs=GRID_TARIFFS) -> tuple[dict, dict]:
    """-> (monthly, plans); monthly = {ym: {taryfa: {savings_pln, wear_pln, grid_charged_kwh,
    pv_charged_kwh, discharged_kwh}}}; plans = {taryfa: {'summer_workday': {...}, ...}}.
    Ceny: tariff_battery.make_price_fn(t, catalog, rdn, kompas, overrides); rcem: rcem_by_ym.get(ym)
    lub średnia wartości (jak battery_sim.simulate_s1; 0.35 gdy pusto)."""
```
Godziny z zakresem `from_ym <= key[:7] <= to_ym`, `hours[key] = (export_kwh, import_kwh)`.

## Wykonawca

### Paczka W1 — `sonnet-worker` (ryzyko: logika pieniędzy)
Pliki: `PKG/tariff_grid_charge.py` (nowy), `PKG/tariff_battery.py`, testy: `tests/test_tariff_grid_charge.py` (nowy), `tests/test_tariff_battery.py`.
Zmiany:
1. `tariff_grid_charge.py` wg „Decyzje projektowe” i „Interfejs” (TDD: najpierw testy czerwone). Bez importu z `static`, `web`, bez nowych zależności; używa `battery_sim.BatteryConfig/throughput_cost_kwh`, `tariff_battery.make_price_fn`, `tariff_catalog._is_day_off`.
2. `tariff_battery.simulate_variant_b`: parametr `soc_floor: float = 0.0` (kwarg po `overrides`), efekt wg „Wariant B z limitem SoC”. Nie zmieniać niczego innego.
3. Testy `test_tariff_grid_charge.py` (funkcje `solve_day` na ręcznych cenach, `cfg=BatteryConfig()`; `eff = sqrt(0.92) ≈ 0.9592`):
   a. Spread zbyt mały (ceny stałe lub różnica < straty+wear) i brak PV → `savings_pln == 0`, `grid_charged_kwh == 0`.
   b. Dwustrefowa doba jak G12w (22–6 i 13–15 tanio 0,64; reszta 1,18 zł; import 1 kWh/h w całej dobie) → ładowanie tylko w godzinach taniej strefy, rozładowanie tylko w drogiej; `discharged_kwh ≤ range·eff` na cykl, SoC nigdy < floor (odtworzyć przebieg z `charge_hours`/`discharge_hours` nie trzeba — test sprawdza bilans: `discharged_kwh ≤ (grid_charged_kwh+pv_charged_kwh)·eff·eff` i `≤ max_cycles·range·eff`).
   c. Doba G13s zima roboczy (ceny brutto all-in z `make_price_fn('G13s', catalog,…)` dla 2026-01-14; import 1 kWh/h): `charge_hours` ⊂ noc 21–7 ∪ offpeak 10–15 (indeksy godzin), `discharge_hours` ⊂ szczyt 7–10 ∪ 15–21; `savings_pln > 0`.
   d. Doba G12w analogicznie: ładowanie w dolinie, rozładowanie w szczycie (godziny z `zone_for`).
   e. Limit cykli: ceny z 3 głębokimi dolinami i 3 szczytami → `grid_charged_kwh·eff ≤ max_cycles·range + 1e-6`; z `max_cycles=1` mniejsza oszczędność niż z 2 (ścisła nierówność na danych, gdzie limit wiąże).
   f. PV ma pierwszeństwo: eksport 2 kWh w tanich godzinach, `rcem` = 0,2 < cena doliny → `pv_charged_kwh > 0` i `grid_charged_kwh` ładuje tylko resztę; gdy `rcem` > cena doliny + wear → sieć przed PV (PV nieładowane w tej godzinie).
   g. Sprawność: pojedynczy cykl — `discharged_kwh ≈ grid_charged_kwh·roundtrip_eff` (tolerancja siatki); zmiana `roundtrip_eff` 0,92→0,5 usuwa ładowanie przy małym spreadzie.
   h. `wear_pln == throughput_cost_kwh(cfg)·grid_charged_kwh`; `savings_pln` zawiera ten koszt (porównanie z ręcznym bilansem: Σ price·d − Σ price·x_grid − wear − rcem·x_pv w granicach 0,01 zł).
   i. Dzień DST (23 i 25 kluczy) i godziny z `None` ceną: bez wyjątku, wynik zgodny z pominięciem.
   j. `simulate_variant_c` na syntetycznych `hours` (2 doby: roboczy zima i sobota lato, bez PV, import 0,5 kWh/h): struktura `monthly`/`plans` (klucze `summer_dayoff` itd.), progi 50% godzin, `avg_saving_pln`; `savings_pln(C) ≥ savings_pln(B)` dla każdej taryfy i miesiąca na danych z eksportem PV (B z `simulate_variant_b(..., soc_floor=0.16)`), doby startujące z pustego floor po obu stronach (dane bez przenoszenia SoC: eksport tylko w obrębie doby i wyczerpywany do północy).
   k. Wydajność: 120 dób × 4 taryfy na syntetycznych danych < 60 s (`time.perf_counter`).
   l. `test_tariff_battery.py`: `soc_floor=0.16` zmniejsza użyteczną pojemność do 4,2 (przypadek z pełnym eksportem przekraczającym pojemność: `charged_kwh` maleje), domyślne `soc_floor=0` daje stare wyniki.
Komenda testu: `cd <WORKTREE>/addons/pv_roi_tracker/pv_roi_tracker && PATH=$HOME/.local/bin:$PATH uv run --no-project --with pytest --with-requirements requirements.txt pytest -q tests/test_tariff_grid_charge.py tests/test_tariff_battery.py`; na koniec pełny `pytest -q` (baza 908 passed/27 skipped + nowe).
`DONE:` punkty 1–3; testy a–l zielone; pełny zestaw bez nowych porażek; w raporcie czas z testu k i lista zmienionych plików.
`NIE RÓB:` nie dotykać `tariff_job.py`, `tariff_sim.py`, `publisher.py`, `static/*`, `web.py`, `battery_sim.py`, `tariffs/*.json`; nie dodawać zależności (numpy!); nie zmieniać domyślnego zachowania `simulate_variant_b`; nie commitować; nie zmieniać wersji; nie edytować plików poza listą.

## Weryfikacja etapu (ja, po `wt land`)
1. `verifier` (BASELINE `git status --short` sprzed delegacji, ścieżka worktree); `claimcheck` pomija repo `/config` (odnotować).
2. Pełny `pytest -q`.
3. Ręczna doba G13s zima roboczy: skrypt jednorazowy w scratchpadzie — `solve_day` na cenach z `make_price_fn` (2026-01-14) vs ręczny bilans (okna ładowania/rozładowania zgodne z ROADMAP: noc 21–7 lub 10–15 / 7–10 i 15–21).
4. Smoke na realnych `hours` z LTS nie jest możliwy w kontenerze (dane na hoście HA) → wydajność na syntetycznych; pomiar na żywo po wydaniu 0.48.0.

## Rollback
`wt drop` przed landem; po landzie `git revert` commitu E2 (nowy plik + parametr `soc_floor` z domyślną 0; nic niepodpięte).
