# E6 — JIT i Strażnik baterii na G13 (z propozycji add-onu), w cieniu do dnia zmiany

Repo add-onu: `~/dev/pv_roi_tracker` (wydania) + mirror `/config/addons/pv_roi_tracker/pv_roi_tracker` (dual-push,
bump obu plików wersji). HA: automatyzacje przez ha-mcp. Roadmapa: `ROADMAP_EBOK_MOJ_TAURON.md`, etap E6
(zastępuje szkic „bateria wg `current_zone`”).

## Context
User przejdzie z G12w na G13 (klasyczną). JIT (`automation.bateria_dynamiczne_ladowanie_ad_hoc_op12`, v48)
i Strażnik (`automation.bateria_straznik_drogiej_taryfy_op12`) mają strefy G12w na sztywno
(`sensor.power_tauron_g12w_current_tariff`, mosty 06:00 / 15:00, deficyt 06–13 i 15–22). W G13 Strażnik
przerwałby zimowe ładowanie 13–16 o 15:00. Z katalogu add-onu (ceny brutto all-in): poranny szczyt G13
0,85 zł nie opłaca ładowania z sieci (0,57/0,92 + 0,25 zużycia ≈ 0,87 zł), wieczorny 1,44 zł tak (~0,57 zł/kWh).

## Decyzje usera (wywiad 27.10)
1. **Hybryda:** add-on mówi, gdzie są okna i czy się opłacają; ile ładować, liczy JIT (Solcast + uczone obciążenie).
2. **Osobne automatyzacje** „JIT G13” i „Strażnik G13”; v48 i stary Strażnik dostają tylko warunek „taryfa ≠ G13”.
3. **Opłacalność = marża na kWh mostu** (cena szczytu > tania / sprawność + koszt zużycia), nie roczne `worth_it`.

Przyjęte domyślnie (do korekty przy akceptacji planu):
- Źródło taryfy: `sensor.pv_roi_tracker_pv_current_tariff` (eBOK 07:00 + `current_tariff_override`).
  Dziś `unknown` (pierwszy odczyt umowy jutro 07:00) — sprawdzić format wartości (`G12W` vs `G12w`), porównania bez wielkości liter.
- Czujnik add-onu niedostępny → działa stara logika G12w (pod G13 nieszkodliwa: 22–6 i 13–15 leżą w tanich strefach G13).
- **Tryb cienia:** dopóki taryfa ≠ G13, „JIT G13” liczy decyzję dla G13 i zapisuje ją tylko do `input_text`, bez falownika.
  Daje to tygodnie obserwacji przed dniem zmiany.
- Weekendy/święta G13 = cała doba tania → brak mostów, brak ładowania.

## Kto / co automatycznie / za zgodą / sukces
- Korzysta: dom (sterowanie baterią) i user (status w `input_text`).
- Automatycznie: add-on publikuje harmonogram mostów co poll; w dniu wykrycia G13 JIT i Strażnik G13 przejmują sterowanie same.
- Za zgodą: wydanie add-onu (w łańcuchu), zapis nowych automatyzacji i zmiana warunków v48/Strażnika (pokazuję diff przed zapisem).
- Sukces: w cieniu decyzje G13 sensowne (zimą robocze ładowanie tylko 13–16, cel z prognozy, poranek bez ładowania);
  G12w bez zmian zachowania; w dniu G13 brak ładowania w szczycie G13.

## Etapy

### E6a — add-on: harmonogram mostów do HA (wydanie 0.52.0; ryzyko „sterowanie domem” → `sonnet-worker`, TDD)
- Nowy czysty moduł `jit_schedule.py`: `bridges(tariff, day, catalog, price_fn, cfg) -> list[dict]` dla dziś i jutra:
  `{charge_start, charge_end, gap_end, cheap_pln, peak_pln, margin_pln_kwh, profitable}` + `peaks: [{start, end}]`.
  Most = tanie okno bezpośrednio przed szczytem; marża = średnia cena szczytu − (cena tania / `roundtrip_eff` + `throughput_cost_kwh(cfg)`).
  Ceny z `tariff_battery.make_price_fn`, strefy z `tariff_catalog.zone_for`, `BatteryConfig` z konfiguracji na żywo.
- `publisher.py`: sensor `jit_schedule` (stan = `current_tariff`), atrybuty przez `json_attributes_topic`
  (wzorzec jak health, `publisher.py:318`): `schedules: {G12w, G13, G13s, G12}` × dziś/jutro, czasy ISO z TZ, `generated_at`.
  Wywołanie w `poll_and_publish` (`main.py`).
- Przy okazji: usunąć alias `saving_vs_g12w` (dług z E5, nikt nie czyta).
- Testy: G13 zima roboczy → noc→7–13 `profitable=false`, 13–16→16–21 `true`; lato 13–19→19–22; weekend → brak; święto w dzień roboczy (Boże Ciało, 1.11, 11.11, 24.12 —
  `zone_for` → `other` przez `polish_holidays`, `battery_sim.py:93`) → brak mostów;
  G12w 22–6→6–13 i 13–15→15–22 `true`; DST; brak cen → brak mostów (nie wyjątek).
- Plan etapu `docs/PLAN_EBOK_MOJ_TAURON_E6a.md` z sekcją **Wykonawca**, potem verifier + claimcheck.
- Wydanie: simplify → code-review + silent-failure-hunter + pr-test-analyzer + security (+ second-opinion, pomiar) → release.
- Rollback: backup add-onu z `ha_manage_updates`; nowy sensor nieużywany przez nic poza E6b.

### E6b — HA: „JIT G13” i „Strażnik G13” (cień) + warunki starych (ja przez ha-mcp, diff do akceptacji)
- Backup konfiguracji obu automatyzacji (JSON) do `/config/docs/battery_g13_backup_<data>.json`.
- „Bateria: JIT G13”: kopia v48 z podmianą: okna/`t_end_dt`/okno deficytu z `schedules[G13]` (most aktywny teraz albo najbliższy),
  ładowanie tylko gdy `profitable`, deficyt liczony od `charge_end` do `gap_end` (ta sama pętla Solcast, `ev_load`/`avg_load`).
  Gdy taryfa == G13 → `huawei_solar.forcible_charge` jak v48; inaczej → tylko `input_text.jit_g13_cien` („cień: ładowałbym N W do X%”).
  Warunek: `input_boolean.automatyka_baterii` on i sensor `jit_schedule` dostępny.
- „Bateria: Strażnik G13”: trigger szablonu „teraz w `peaks` G13” + taryfa == G13 → `stop_forcible_charge`, status „Poza taryfą”.
- v48 i stary Strażnik: warunek `taryfa ≠ G13` (unknown/unavailable przepuszcza → G12w).
- Nowy `input_text.jit_g13_cien` (helper przez ha-mcp).
- Rollback: wyłączyć 2 nowe automatyzacje, przywrócić v48/Strażnika z backupu.

### E6c — obserwacja w cieniu i przełączenie
- 1–2 tygodnie: `haiku-scout` zbiera historię `input_text.jit_g13_cien` vs faktyczne ładowanie G12w; raport (godziny, cele, kWh).
- Dzień zmiany (data z eBOK lub `current_tariff_override`): weryfikacja na żywo pierwszej doby roboczej (13–16 ładowanie,
  stop 16:00 przez Strażnika G13, brak ładowania nocą), razem z `ROADMAP_energy_panel_g13.md` G1–G3.

## Weryfikacja
- E6a: pytest (baza 1057 passed/23 skipped), na żywo atrybuty `sensor.pv_roi_tracker_pv_jit_schedule` (G13 zima: 13–16 profitable).
- E6b: `ha_config_get_automation` po zapisie; trace „JIT G13” w cieniu (`ha_get_automation_traces`) — brak wywołań falownika;
  trace v48 nadal ładuje wg G12w.

## Ryzyka
- Rozjazd formatu taryfy (`G12W`/`G12w`) → porównania `lower()`.
- Atrybuty > 16 KB odrzucane przez recorder → tylko dziś/jutro, 4 taryfy.
- Duplikat szablonu Solcast w dwóch automatyzacjach (świadomie, decyzja 2); po G13 i obserwacji v48 można wyłączyć.
