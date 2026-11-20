# ROADMAP: wycena autokonsumpcji po strefach (pv_roi_tracker)

## Context
`sensor.pv_roi_tracker_pv_self_consumption_savings` spada w nocy i rośnie wieczorem bez produkcji.
Przyczyna (potwierdzona historią 36 h + kodem): `live_reader.py:806` mnoży **miesięczne**
`self_consumed_kwh` przez `buy_price` = średnia ważona cena **importu** po strefach
(`weighted_buy_price`, `live_reader.py:644`). Tani nocny import zaniża średnią → cała suma maleje.
Przy okazji: arbitraż baterii 17.11 16:18 spadł 167,13 → 132,83 i wrócił (prawdopodobnie fallback
`sensor.battery_arbitrage_savings_monthly`, `live_reader.py:762`). Feed-in czysty (stoi do potwierdzenia RCEm).

## Ustalenia z wywiadu
- **Kto korzysta:** user (sensory ROI w HA, web UI add-onu, suma ROI/NPV).
- **Automatycznie:** wycena bieżącego miesiąca co godzinę: Σ_h max(0, prod_h − exp_h) × cena strefy godziny h
  (`TariffModule.price_at`, `tariff_module.py:138`; dane `get_hourly_energy`, `live_reader.py:333`).
  Bateria: energia z PV wyceniana strefą godziny produkcji.
- **Za zgodą:** przeliczenie całej historii (simulate → przegląd diffu → apply z kopią), w tym miesiące z fakturą
  (kWh z faktury, rozkład na godziny wg kształtu LTS, ceny stref taryfy obowiązującej wtedy). Miesiące bez
  godzinowych LTS zostają bez zmian, z flagą `sc_pricing='monthly_avg'` (nowe: `'hourly_zone'`).
  Korekta statystyk HA (`recorder/adjust_sum_statistics`) po apply — krok produkcyjny.
- **Sukces:** licznik nie maleje w żadnej godzinie przez dobę na żywo; arbitraż bez chwilowych spadków;
  diff historii przejrzany i zaakceptowany; testy zielone.

## Etapy (commit na etap, jedno wydanie na koniec)
Repo: `~/dev/pv_roi_tracker` (dual-push z `/config/addons/pv_roi_tracker`, checklista
`-config/memory/feedback_pv_roi_release_checklist.md`), gałąź `main`. Pierwszy krok: kopia tego planu do
`docs/ROADMAP_sc_zone_pricing.md` + `docs/PLAN_sc_zone_pricing_E<n>.md` z sekcją **Wykonawca** (haiku-worker
w worktree `wt new`; E2 oznaczony „ryzyko” → sonnet-worker; po każdej paczce `claimcheck` + `verifier`).

**E1 — czysta funkcja wyceny godzinowej + bieżący miesiąc (tdd)**
- Nowa funkcja (np. `zone_priced_self_consumption(hourly, tariff_module) -> (kwh, pln)`) w `live_reader.py`
  albo nowym `sc_pricing.py`; `_build_record` przyjmuje opcjonalne `sc_savings_pln` i zapisuje `sc_pricing`.
- `read_current_month` liczy z pełnych godzin LTS; brak danych → stara metoda + flaga.
- Testy: monotoniczność (dokładanie godzin nigdy nie zmniejsza sumy), G13/G12w strefy, godzina z eksportem > produkcją
  (0), zgodność kWh z dotychczasowym `produced − exported` miesiąca (tolerancja), fallback.
- Rollback: `git revert` commitu.

**E2 — przeliczenie historii (ryzyko)**
- Tryb `simulate/apply` wzorem `rebase.py` (osobna komenda/endpoint `sc_reprice`): per miesiąc stara vs nowa
  wartość, metoda; apply robi kopię `historic_store` (`*.bak-<data>`) i zmienia tylko `self_consumed_savings_pln`
  + `sc_pricing` (miesiące z fakturą: kWh z faktury rozłożone wg kształtu LTS).
- Uwaga z rozpoznania: `rebase._default_fetch` (`rebase.py:71`) nie podaje cen → fallback env 1.23/0.63; nowa
  ścieżka bierze ceny z `TariffModule` (okresy z `/data/tariff_periods.json`).
- Testy: faktura zachowuje kWh, brak LTS → bez zmian + flaga, apply idempotentny, kopia powstaje.
- Rollback: przywrócenie kopii `historic_store`.

**E3 — skok arbitrażu baterii**
- Skill `diagnose`: potwierdzić, czy 16:18 17.11 `arb_kwh` był None i zadziałał fallback; poprawka (np. ostatnia znana
  wartość zamiast sensora o innej semantyce) + test regresji.
- Rollback: `git revert`.

**E4 — wydanie i wdrożenie**
- `simplify` → `code-review` + `silent-failure-hunter` + `pr-test-analyzer` + security (równolegle) → `release`
  (bump wersji, opublikowane wydanie) → aktualizacja przez Supervisor.
- Na żywo: `sc_reprice simulate`, pokazuję diff userowi → **apply za zgodą** → korekta statystyk HA
  (`adjust_sum_statistics` o różnicę sumy) **za zgodą**.
- Weryfikacja: `ha_get_history` sensora przez 24 h — zero spadków; arbitraż bez spadków; web UI pokazuje flagi metod.
- Rollback: poprzednia wersja add-onu + kopia `historic_store`; adjust_sum odwrotnym przesunięciem.

## Weryfikacja (ogólnie)
`uv run pytest` w `~/dev/pv_roi_tracker/pv_roi_tracker` po każdym etapie; po wydaniu historia sensorów z HA (24 h).

## Status
- **E1 ZROBIONE 2026-11-20** — commit `bcf4823` (kod + 16 nowych testów), `67c68e1` (docs). Weryfikacja: pełny pytest 1560 passed / 23 skipped
  (baseline 1544), claimcheck 0/7 flag, verifier (Haiku) APPROVE bez blokujących. Wydanie NIE zrobione (jedno wydanie na koniec, E4); kopia
  `/config/addons/pv_roi_tracker` niezsynchronizowana (dual-push przy wydaniu).
- **E2 ZROBIONE 2026-11-20** — commit `921e88e` (kod + 21 testów), plan `5af6e45`. Nowe: `sc_reprice.py` (simulate/apply, kopia `historic.json.bak-<UTC µs>` w trybie 'x'),
  trasy `POST /api/historic/simulate-sc-reprice` i `apply-sc-reprice`; `rebase._MERGE_FIELDS` + `sc_pricing`; `main` daje `rebase` `fetch_month` z `price_at`;
  `reconcile_invoice` skaluje PLN dla `hourly_zone`. Weryfikacja: pytest 1582 passed / 23 skipped (baseline 1560), claimcheck 0/7,
  verifier REJECT (kolizja nazwy kopii w tej samej sekundzie) → poprawka sonnet-worker, sprawdzona przeze mnie (pytest zielony). Apply na produkcji NIE uruchomiony (E4, za zgodą).
- E3, E4: do zrobienia.

### Znaleziska E2 (do E4)
- Zapis przez `MonthlyRecord.from_dict→to_dict` gubi pola spoza dataclass i dopisuje brakujące jako null (dotyczy też istniejącego `rebase.apply`) — przed apply na produkcji porównać klucze `historic.json`.
- `simulate` dla miesiąca z fallbackiem `monthly_avg` zwraca `kwh` = `self_consumed_kwh` rekordu (może być None).
- `main._sc_price_at` i `read_month_from_statistics(price_at=)` niewywołane na żywo — pierwszy test na żywo w E4 (`simulate`).

### Znaleziska E1 (do E2/E4)
- `rebase._default_fetch` (`rebase.py:71`) woła `read_month_from_statistics` bez `price_at` → `rebase apply` cofnąłby wycenę do starej metody. Nie uruchamiać rebase przed E2.
- `historic_store.reconcile_invoice` (`historic_store.py:326`) po fakturze nadpisuje `self_consumed_savings_pln` = sc × blended → E2.
- Każdy poll (30 min) robi 2 dodatkowe zapytania LTS godzinowe (timeout 60 s) — sprawdzić czas na żywo przy E4.
- Σ_h max(0, p−e) kWh może przekraczać miesięczne `self_consumed_kwh` (godziny z eksportem > produkcją, np. rozładowanie baterii) — PLN nie jest powiązany z tym kWh; zgodne z założeniem.
- ruff bez konfiguracji projektu daje 748 uwag w całym repo (stan sprzed E1) — nie bramka.
