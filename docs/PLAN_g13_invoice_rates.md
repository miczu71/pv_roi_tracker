# pv_roi_tracker — faktura G13 jako źródło „bieżących stawek” (jak G12W)

## Context
Od 03.11.2026 taryfa G13. Faktury G12W zasilają warstwę 2 `latest_invoice_rates` (`web.py:817`):
czujniki `pv_rate_*`, dryf w UI, a pompa ciepła wycenia miesiące z fakturą jej stawkami (`main.py:1330`).
Faktura G13 ma `peak_gross/offpeak_gross = None`, więc nigdzie z tego nie korzysta: czujniki biorą
popołudniową/pozostałe z katalogu, strefa przedpołudniowa nie ma czujnika, dryf milczy, pompa ciepła
celowo pomija fakturę G13. Wywiad (2026-11-25): **G13 ma działać jak G12W** — wycena ROI/autokonsumpcji
i symulacje zostają na katalogu (jak dla G12W). Stan na żywo: faktura 11/2026 = katalog co do grosza
(0,9048/1,4960/0,6257, kontrola `ok`), więc dziś liczby się nie zmienią — etap zabezpiecza przyszłe zmiany cennika.

Ustalenia:
- kto korzysta: czujniki HA (automatyzacje/kafle), zakładka Taryfa (dryf), wycena pompy ciepła;
- automatycznie: stawki stref z najnowszej faktury G13 obowiązują od jej okresu dalej, aż przyjdzie nowsza
  faktura albo ręczna korekta (warstwa 3, logika bez zmian); bez faktury — katalog;
- bez zmian: kontrola faktur (`_check_g13`, tolerancja 2%, powiadomienie HA), wycena ROI, sim, bateria;
- sukces: 3 nowe czujniki z 0,9048/1,4960/0,6257, dryf G13 widoczny w UI, pompa ciepła dla 11/2026 liczy
  godziny G13 stawkami faktury (dziś ta sama kwota), stare czujniki peak/offpeak bez regresji.

## Zmiany (repo `~/dev/pv_roi_tracker`, potem dual-push do `/config/addons/pv_roi_tracker`)
1. **`web.py` warstwy stawek**
   - `_RATE_FIELDS` + `morning_gross, evening_gross, other_gross` (warstwa 2 nadpisuje tylko niepuste — reguła istniejąca).
   - Warstwa 1 (`_catalog_layer_rates`): gdy `module.tariff_on(today)` ∈ `THREE_ZONE_TARIFFS`, dołożyć `{z}_gross`
     z katalogu tak jak `_check_g13` (`tc.rates_per_kwh` + `module.vat`; mapa `G13_ZONES` z `tariff_invoice_check`).
   - Gdy bieżąca taryfa NIE jest trójstrefowa — `{z}_gross` usunięte z wyniku (stara faktura G13 nie przecieka do G12W).
   - `_build_tariff_drift` (`web.py:856`): dla faktury z `{z}_gross` porównać 3 strefy z warstwą 1 (ta sama
     struktura wpisów co dla peak/offpeak). Historia stawek (`web.py:~894`) dostaje pola stref „za darmo” z `_RATE_FIELDS`.
2. **`publisher.py`** — 3 nowe `_Sensor`: `rate_morning_gross` („PV Rate Morning Gross”), `rate_evening_gross`,
   `rate_other_gross` (PLN/kWh, measurement) + wpisy w mapie pole→czujnik. `None` → `unknown` (jak inne).
3. **`main.py` pompa ciepła (~1330–1365)** — miesiąc trójstrefowy z fakturą G13 (`morning_gross` niepuste):
   `hour_price_fn` dla godzin, w których `tariff_on` jest G13, zwraca `{strefa}_gross` faktury wg `zone_for`
   (mapa `G13_ZONES` odwrotnie); godziny G12W tego miesiąca i miesiące bez faktury — jak dziś (katalog / para
   peak-offpeak). Zdjąć log „miesiące G13 z fakturą wyceniane z cennika” dla tych miesięcy.
4. **Testy (TDD)**: `latest_invoice_rates` — G13 z fakturą (strefy z faktury), G13 bez faktury (strefy z katalogu),
   G12W po starej fakturze G13 (brak stref), G12W bez regresji; dryf G13 (rozjazd > próg → wpis); publisher —
   3 czujniki w payloadzie; pompa ciepła — miesiąc mieszany G12W+G13 z fakturą G13 o stawkach ≠ katalog → godziny
   G13 po stawce faktury, godziny G12W po starej ścieżce.
5. `CHANGELOG.md` (po polsku) + bump **0.61.0** (nowe czujniki) w `config.yaml` i `__init__.py`.

Reużyć: `tariff_invoice_check.G13_ZONES`, `tariff_catalog.rates_per_kwh`, `tariff_zones.THREE_ZONE_TARIFFS`,
`module.zone_for/tariff_on/_period_info`, istniejący wzorzec `_compare`/dryfu.

## Wykonawca (paczka W1, „ryzyko” — pieniądze → `sonnet-worker`, worktree z `wt new`)
- Pliki: `pv_roi_tracker/pv_roi_tracker/{web.py,publisher.py,main.py}`, testy w `pv_roi_tracker/tests/`
  (nowy `test_g13_invoice_rates.py` + dopiski do istniejących testów publishera/heatpump, jeśli są).
- Test: `cd pv_roi_tracker && ~/.local/bin/uv run --with pytest --with-requirements requirements.txt pytest -q`.
- DONE: punkty 1–4 zrobione, nowe testy czerwone przed zmianą i zielone po, cały zestaw zielony (baseline 1620 passed).
- NIE RÓB: `tariffs/*.json`, `tariff_invoice_check.py` (tolerancja/logika), `sc_pricing/sc_reprice/tariff_sim/
  tariff_battery`, `static/*`, wersja/CHANGELOG (robię sam), commit.
- Potem `verifier` (+ `claimcheck`, repo PUBLIC) z BASELINE `git status --short`.

## Wydanie
`simplify` → `code-review` (+ `silent-failure-hunter`, `pr-test-analyzer`, security general-purpose na `<baza>..HEAD`)
→ `release` (pubguard, dual-push, `gh release v0.61.0`, Supervisor update).
Rollback: Supervisor → 0.60.3 / `git revert`; nowe encje MQTT znikają po powrocie (discovery).

## Weryfikacja na żywo
- `ha_get_state` nowych `sensor.pv_rate_{morning,evening,other}_gross` = 0,9048 / 1,496 / 0,6257; peak/offpeak bez zmian.
- `/api/tariff_sim` → `invoice_check["2026-11"].ok == true` (bez regresji); dryf w zakładce Taryfa bez fałszywego alarmu.
- Log add-onu: brak „wyceniane z cennika, nie z faktury” dla 2026-11; koszt pompy ciepła 11/2026 = wartość sprzed wydania.
- Playwright: zakładka Taryfa, konsola 0 błędów.
