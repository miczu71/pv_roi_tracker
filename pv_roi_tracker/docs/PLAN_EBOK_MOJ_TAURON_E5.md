# PLAN E5 — HA: porównania taryf na bazie dynamicznej (tylko czyste porównania)

Roadmapa: `/config/addons/pv_roi_tracker/pv_roi_tracker/docs/ROADMAP_EBOK_MOJ_TAURON.md` (mirror) + `~/dev/pv_roi_tracker` (dual-push).
Zakres uzgodniony 27.10: przepinamy tylko porównania oparte na symulacji add-onu. Koszty faktyczne, cena G12w,
panel Energia i bateria zostają bez zmian do daty wejścia G13 (`/config/docs/ROADMAP_energy_panel_g13.md`, E6).

## Context
Add-on 0.51.0 publikuje neutralne sensory `*_baseline`. Ich bazą jest bieżąca taryfa z eBOK, dziś G12w:
`month_baseline` 226,56 = `month_g12w`, `12m_baseline` 3817,45 = `12m_g12w`. Po zmianie na G13 baza
przełączy się sama, ale HA nadal pokazuje „G12w (obecna)” i „Oszczędność vs G12w”. Celem E5 jest, żeby
porównania w HA same podążały za bazą, bez kolejnej ręcznej zmiany w dniu G13.

## Ustalenia z inwentaryzacji (27.10, na żywo)
- **Konsumenci sensorów add-onu w HA:** tylko 2 karty `entities` w dashboardzie `lovelace`, widok 2, sekcja 1:
  - `cards[1]` „Koszt bieżącego miesiąca (zł)” ma wiersz `tariff_sim_month_g12w` „G12w (obecna)”;
  - `cards[2]` „Ostatnie 12 miesięcy (zł)” ma wiersze `tariff_sim_12m_g12w` „G12w (obecna)” i `tariff_sim_saving_vs_g12w` „Oszczędność roczna vs G12w”.
  - Żaden YAML, automatyzacja, skrypt ani inny dashboard nie używa `tariff_sim_*` (grep `/config` + `ha_search`/dashboard search: 0). Wszystkie 6 dashboardów to storage.
- **Pozostałe ~23 encje „g12w” w HA to koszty faktyczne liczone na żywo** (kWh × cena G12w): `power_tauron_g12w_current_{tariff,price}`, `energy_cost_*`, `koszt_zmienny_g12w_miesieczny`, 4 `utility_meter`, `*_g11_vs_g12w`, `*_g12w_vs_dynamiczna`, `spread_g12w_netto`, `dynamiczna_*_niz_g12w*`, automatyzacje `Taryfa`, `Zliczanie godzin…`, `Podsumowanie miesięczne taryf`. Nie mają odpowiednika w `*_baseline`, bo te są symulacją miesięczną, a nie całką ceny. **Poza E5:** przechodzą na G13 razem z sensorem ceny G13 i licznikami 3-strefowymi (`ROADMAP_energy_panel_g13.md` G1–G2, w dniu zmiany). Bateria i JIT należą do E6.

## Zmiana (jedyny zapis produkcyjny: dashboard `lovelace` przez ha-mcp)
Wiersze G12w zostają jako jedna z alternatyw (po zmianie G12w nadal jest taryfą do porównania). Dochodzi
wiersz bazy, a oszczędność liczona jest względem bazy.

`cards[1]` (miesiąc):
```diff
+ - entity: sensor.pv_roi_tracker_pv_tariff_sim_baseline_tariff
+   name: Bieżąca taryfa (baza porównań)
+ - entity: sensor.pv_roi_tracker_pv_tariff_sim_month_baseline
+   name: Bieżąca taryfa — koszt
+ - type: divider
    - entity: …month_g11 / g12 (bez zmian)
-   - entity: sensor.pv_roi_tracker_pv_tariff_sim_month_g12w
-     name: G12w (obecna)
+   - entity: sensor.pv_roi_tracker_pv_tariff_sim_month_g12w
+     name: G12w
    - …g13, dyn_a, dyn_b (bez zmian)
```
`cards[2]` (12 mies.): analogicznie wiersz `12m_baseline` „Bieżąca taryfa — koszt” na górze + divider,
„G12w (obecna)” → „G12w”, a `saving_vs_g12w` „Oszczędność roczna vs G12w” → `saving_vs_baseline`
„Oszczędność roczna vs bieżąca taryfa”. Reszta kart bez zmian.

## Kroki
1. Skopiować ten plan do `docs/PLAN_EBOK_MOJ_TAURON_E5.md` w repo dev (i mirror).
2. Backup: zapisać `card_config` obu kart + `config_hash` do `/config/docs/dashboard_tariffsim_backup_2026-10-27.json`.
3. Sprawdzić stany `month_baseline`, `12m_baseline`, `saving_vs_baseline` i `baseline_tariff` (nie `unknown`; po pollu 0.51.0).
   Jeśli `baseline_tariff` nadal jest `unknown`, wiersz i tak przepinam, ale zgłaszam to w raporcie.
4. Pokazać userowi finalny YAML obu kart (diff jak wyżej), a po „tak” wykonać `ha_config_set_dashboard` (`python_transform` na
   `['views'][2]['sections'][1]['cards'][1|2]`, z `config_hash` jako ochroną przed nadpisaniem).
5. Weryfikacja (niżej).
6. ROADMAP: status E5 (zrobione / zakres / odroczone migracje faktycznych kosztów → `ROADMAP_energy_panel_g13.md`)
   + wzmianka w `ROADMAP_energy_panel_g13.md` G4, że dashboard porównań jest już neutralny. Commit + push
   obu repo (poza 02:45–03:15), `/config` przez `ha-deploy` (sam push docs, bez `github_sync`/reloadu, bo nie dotyka YAML HA).
7. `stage-end` (E6/E7 zostają) i koniec tury.

## Weryfikacja
- `ha_config_get_dashboard(url_path="lovelace", query="saving_vs_g12w")` → 0 trafień; `query="_baseline"` → 4 wiersze.
- `playwright-ha`: widok 2 dashboardu Overview, screenshot do `/config/playwright/e5_tariffsim.png` + `browser_console_messages` (bez błędów);
  wiersze bazy pokazują te same liczby co G12w (226,56 / 3817,45), oszczędność = dotychczasowa.
- Panel Energia i encje kosztów faktycznych bez zmian (nic nie zapisujemy poza dashboardem).

## Rollback
`ha_config_set_dashboard` z kopią kart z kroku 2 (albo usunięcie dodanych wierszy) i `git revert` commitu docs.

## Ryzyka / długi
- Po zmianie na G13 wiersze „G13” i „Bieżąca taryfa” pokażą tę samą wartość. To zamierzone.
- Add-on nadal publikuje alias `saving_vs_g12w` (= baza, więc po zmianie na G13 będzie mylący). Po E5 nikt go nie używa,
  więc do usunięcia w publisherze przy następnym wydaniu add-onu (dług do ROADMAP, nie w E5, bo E5 nie ma wydania).
- Bez workerów i wydania: jedna zmiana dashboardu przez ha-mcp robię sam (CLAUDE.md: edycje ha-mcp sam).
