# PLAN E6a — harmonogram mostów JIT publikowany do HA (add-on 0.52.0)

Źródło prawdy: `docs/PLAN_EBOK_MOJ_TAURON_E6.md` (zatwierdzony 27.10; decyzje: hybryda, osobne automatyzacje,
opłacalność = marża na kWh). Ten plan to kontrakt dla `sonnet-worker` (ryzyko: dane sterujące baterią w domu).
E6b (HA) i E6c (obserwacja) robię sam po wydaniu.

## Cel
Add-on mówi HA, gdzie w danej taryfie są szczyty, jakie tanie okno je poprzedza (most) i czy ładowanie z sieci
w tym moście się opłaca. Ile ładować, liczy JIT w HA. Dotyczy taryf G12w, G13, G13s (G12 odpada: `make_price_fn`
zwraca dla niego `is_peak=False`, znany dług E4; G11 nie ma stref). Dni: dziś i jutro.

## Kontrakt (ma być dokładnie tak)
`pv_roi_tracker/jit_schedule.py` — czysta logika, bez I/O:

```python
TARIFFS = ('G12w', 'G13', 'G13s')
MIN_MARGIN_PLN_KWH = 0.0
MAX_LOOKBACK_H = 96

def bridges(tariff: str, day: date, catalog: dict, cfg: BatteryConfig,
            overrides: Optional[dict] = None) -> dict:
    """{'peaks': [{'start','end','avg_pln_kwh'}], 'bridges': [{'charge_start','charge_end','gap_end',
    'cheap_pln_kwh','peak_pln_kwh','margin_pln_kwh','profitable'}]} dla szczytów zaczynających się w `day`.
    Czasy: naiwny lokalny `datetime` (konwersja na ISO z TZ robi build_schedule)."""

def build_schedule(today: date, catalog: dict, cfg: BatteryConfig, overrides=None) -> dict:
    """{'generated_at', 'roundtrip_eff', 'throughput_cost_pln_kwh',
        'schedules': {taryfa: {'YYYY-MM-DD': <wynik bridges>}}}  (dni: today, today+1)"""
```

Reguły `bridges`:
- Ceny godzin z `tariff_battery.make_price_fn(tariff, catalog, None, None, overrides)`: klucz `YYYY-MM-DDTHH`,
  wynik `(cena_brutto_kwh, is_peak)`. Oś czasu godzinowa, naiwna lokalna (`+ timedelta(hours=1)`).
- Szczyt = ciągły przedział godzin z `is_peak=True`. Dla każdego szczytu zaczynającego się w `day`:
  `charge_end` = początek szczytu, `gap_end` = koniec szczytu (pierwsza godzina po nim),
  `charge_start` = początek ciągłego przedziału `is_peak=False` bezpośrednio przed szczytem (cofanie
  godzina po godzinie, maks. `MAX_LOOKBACK_H`; po weekendzie okno może zaczynać się w piątek wieczorem).
- `cheap_pln_kwh` = minimum cen w oknie ładowania, `peak_pln_kwh` = średnia cen szczytu,
  `margin_pln_kwh = peak − (cheap / cfg.roundtrip_eff + battery_sim.throughput_cost_kwh(cfg))`,
  `profitable = margin > MIN_MARGIN_PLN_KWH`. Nieopłacalne mosty też są zwracane (z `profitable: false`).
- Brak ceny (`None`) w którejkolwiek godzinie szczytu albo okna → ten most pomijany (bez wyjątku).
- Weekend i święto ustawowe: `zone_for` zwraca strefę niszczytową (G12w `offpeak`, G13 `other`, G13s `*_dayoff_*`
  wg katalogu), więc `peaks == []` i `bridges == []`. Święto w dzień roboczy jest już w `battery_sim.polish_holidays`
  (np. Boże Ciało, 1.11, 11.11, 24.12) — nie pisz własnego kalendarza.

## Wykonawca
**Pliki:** `pv_roi_tracker/jit_schedule.py` (nowy), `pv_roi_tracker/publisher.py`, `pv_roi_tracker/main.py`,
`pv_roi_tracker/tariff_job.py`, `tests/test_jit_schedule.py` (nowy), `tests/test_publisher.py`,
`tests/test_tariff_job.py`, `CHANGELOG.md` (w korzeniu repo, nowa sekcja `[0.52.0] — w przygotowaniu`).
Skille: `tdd`, `addon-conventions`. Test najpierw (czerwony), potem kod.

Kroki:
1. `tests/test_jit_schedule.py` + `jit_schedule.py` wg kontraktu. Katalog z `tariff_catalog.load_catalog()`,
   oczekiwane wartości liczone w teście z katalogu (`rates_per_kwh`, `zone_for`), nie wpisane z pamięci. Przypadki:
   - G13 zima, dzień roboczy: szczyty 7–13 i 16–21; most przed 7:00 (`profitable=False`), most 13–16 → 16–21 (`True`);
     `charge_start` pierwszego mostu = 21:00 dnia poprzedniego.
   - G13 lato, dzień roboczy: szczyty 7–13 i 19–22; most 13–19 → 19–22.
   - G12w dzień roboczy: szczyty 6–13 i 15–22, mosty 22–6 → 6–13 i 13–15 → 15–22 (oba `True` przy domyślnym `BatteryConfig`).
   - G13s: sezonowe okna z katalogu; most z minimum po różnych strefach (night vs offpeak).
   - Sobota, niedziela, święto w dzień roboczy (np. 2026-11-11): `peaks == []`.
   - Poniedziałek po weekendzie: `charge_start` w piątek (limit 96 h).
   - Przełom DST (dwa dni z 25/23 godzinami): bez wyjątku, szczyty zgodne ze `zone_for`.
   - Brak ceny (np. G13s przed 2026-01): most pominięty, bez wyjątku.
   - `profitable` zmienia się, gdy `cfg.roundtrip_eff` albo `lifetime_cycles` zmieni marżę przez zero.
2. `publisher.py`: sensor `jit_schedule` (nazwa „PV JIT Schedule”, ikona `mdi:battery-clock`) publikowany jak health:
   discovery z `json_attributes_topic` w `discovery` (obok `health_payload`), stan i atrybuty w nowej metodzie
   `publish_jit_schedule(state: str, attributes: dict)` (retain, `ensure_ascii=False`, wzorzec `publish_health`).
   Stan = bieżąca taryfa albo `unknown`. Zaktualizuj licznik w `logger.info('MQTT discovery published …')`.
   Test: payload discovery zawiera `json_attributes_topic`; `publish_jit_schedule` publikuje stan i JSON atrybutów; brak
   połączenia → bez wyjątku.
3. `main.py` (`poll_and_publish`, obok `pub.publish_roi`, po wyliczeniu `_current_tariff`): w osobnym `try/except Exception`
   (`logger.exception` + `_record_job('jit_schedule', False, 'harmonogram JIT nie powiódł się')`, sukces →
   `_record_job('jit_schedule', True)`) wywołaj
   `jit_schedule.build_schedule(date.today(), _tcat.load_catalog(), battery_store.load_config(BATTERY_CONFIG_PATH))`,
   zamień czasy na ISO z TZ `Europe/Warsaw` (`ZoneInfo`, jak w `price_sources.py:27`) i `pub.publish_jit_schedule(...)`.
   Błąd harmonogramu nie może zatrzymać reszty publikacji ani joba poll. Atrybuty < 16 KB (3 taryfy × 2 dni).
4. Alias `saving_vs_g12w` (dług E5, zatwierdzony w E6): usuń z `tariff_job.py` klucze `saving_vs_g12w`, `saving_vs_g12w_b`,
   `saving_vs_g12w_c` (linie ~237, 245, 263), z `publisher.py` sensor `tariffsim_saving_vs_g12w` (linia ~112) oraz gałąź
   w `_render_value` (~175–178); dopisz `'tariffsim_saving_vs_g12w'` do `_TOMBSTONED_SLUGS` (czyści retained discovery
   w HA). Zaktualizuj testy (`test_tariff_job.py`: 77, 85, 281, 302, 417, 525, 536, 545; `test_publisher.py`: 66, 89,
   159, 171): asercje na `saving_vs_baseline*`, test tombstone. Sensory `*_g12w` (`month_g12w`, `12m_g12w`,
   `12m_c_g12w`) ZOSTAJĄ — to prawdziwa symulacja G12w, nie alias.
5. `CHANGELOG.md`: sekcja 0.52.0 — harmonogram mostów JIT (`sensor.pv_roi_tracker_pv_jit_schedule`), usunięcie
   przestarzałego `saving_vs_g12w[_b|_c]`. Neutralne sformułowania, bez danych prywatnych.

- Test: `cd <WORKTREE>/pv_roi_tracker && PATH=$HOME/.local/bin:$PATH uv run --no-project --with pytest --with-requirements requirements.txt python -m pytest -q`
  (baza: 1057 passed, 23 skipped).
- `DONE:` wszystkie testy zielone; `grep -rn "saving_vs_g12w" pv_roi_tracker/ tests/` zwraca tylko tombstone w
  `publisher.py` i jego test; `python -c "from pv_roi_tracker import jit_schedule"` działa z korzenia `pv_roi_tracker/`;
  `build_schedule` dla zimowego dnia roboczego daje `profitable` równe `[False, True]` dla G13 (pierwszy, drugi most).
- `NIE RÓB:` zmian w `tariff_zones.py`, `tariff_battery.py`, `tariff_catalog.py`, `battery_sim.py`, `static/*`,
  `web.py`, `ebok_client.py`; poprawiania długu „G12 `day`”; podbijania wersji (`config.yaml`, `__version__`) i commitów
  (robię ja). Bez prawdziwych danych w fixtures. Błędów katalogu nie łapać ogólnym `except` wewnątrz `jit_schedule.py`.

## Moja weryfikacja po workerze
`verifier` (z `git status --short` sprzed delegacji + claimcheck), przegląd `git diff` samodzielnie (dane sterujące),
potem simplify → code-review + silent-failure-hunter + pr-test-analyzer + security → release 0.52.0 → na żywo:
atrybuty `sensor.pv_roi_tracker_pv_jit_schedule` (G13 zima: `profitable` `[False, True]`, G12w oba mosty `True`),
tombstone `tariffsim_saving_vs_g12w`, brak błędów w logach startu.

## Rollback
`ha_manage_updates` przywraca backup add-onu 0.51.0; w repo `git revert` commitów E6a. Nowy sensor jest czytany
dopiero przez E6b, więc do tego czasu nic w HA od niego nie zależy.
