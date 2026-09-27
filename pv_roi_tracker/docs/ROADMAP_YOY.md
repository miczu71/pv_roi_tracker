# PV ROI Tracker — kafel „Rok do roku” zamiast wachlarza spłaty

## Kontekst

Na głównej stronie add-onu (obok „Miesięczne oszczędności”) stoi wykres „Wachlarz spłaty” (P10–P90).
User chce go przenieść do zakładki **Wykresy**, a na jego miejsce dać **jeden duży kafel porównania
rok do roku** — uważa to porównanie za najbardziej miarodajne.

Ustalone w wywiadzie:
1. **Cel: pieniądze + dlaczego** — główna liczba to oszczędności zł r/r, pod nią rozbicie przyczyn
   (produkcja/pogoda, autokonsumpcja, cena zakupu, cena RCEm, arbitraż).
2. **Miesiąc = bieżący, te same dni** — np. 1–27.09.2026 vs 1–27.09.2025 (nie ostatni zamknięty).
3. **RCEm bieżącego miesiąca = szacunek z godzinowych RCE** (oznaczony „szac.”), zastąpiony prawdziwą
   RCEm po publikacji.
4. **Postęp roku vs poprzedni** — osobny blok: bilans „od 1.01 do dziś vs do tej samej daty rok
   temu” (tabela) + wykres „wyścigu” narastająco.

Odkrycia z kodu, które kształtują projekt:
- Rekord bieżącego miesiąca ma `feedin_revenue = None` (RCEm nieznana do publikacji,
  `rcem_scraper.get_current_month_rcem`) → bez szacunku r/r pokazałby sztuczny spadek.
- Rekordy są miesięczne; „te same dni rok temu” wymaga nowego odczytu LTS HA dla okna godzinowego
  (`live_reader._ws_statistics` obsługuje `period='hour'`, lifetime-liczniki z Energy Dashboard).
- Zmiana taryfy G11→G12w (01.2025) **nie** zaburza porównania 2026 vs 2025 (oba lata G12w).
- Istniejący r/r: karta „Produkcja r/r” (`roi.degradation_analysis`) i kolumny r/r w
  „Podsumowaniu rocznym” (`app.js:1030`, parowanie miesięcy) — wzorzec parowania do reużycia.

## Jakie dane (serce planu)

Dwa okresy, liczone identyczną metodą po obu stronach, z tym samym odcięciem
(ostatnia pełna godzina „teraz” i dokładnie rok wcześniej):

- **MTD**: 1. dzień bieżącego miesiąca → odcięcie, vs to samo okno rok temu.
- **YTD**: 1.01 → odcięcie, vs to samo rok temu = zamknięte miesiące z rekordów (parowane jak w
  tabeli rocznej) + okno MTD. Miesiące bez pary (brak danych rok temu) pomijane i wymienione we flagach.

### Metryki na okres (strona `cur` i `prev`)
| Pole | Źródło / wzór |
|---|---|
| `savings_pln` (główna liczba) | autokons. + sprzedaż + arbitraż |
| `self_savings_pln`, `feedin_revenue_pln`, `arbitrage_pln` | składniki |
| `produced_kwh`, `specific_yield` (kWh/kWp) | LTS / rekordy |
| `self_consumed_kwh`, `exported_kwh`, `purchased_kwh`, `consumed_kwh` | jw. |
| `self_consumption_rate_pct` (autokons./produkcja), `autarky_pct` (autokons./zużycie) | pochodne |
| `buy_price` (ważona zakupem), `feedin_price` (RCEm ważona eksportem) | ceny okresu |
| `net_grid_cost_pln` | zakup − sprzedaż (kontekst „rachunku”) |

Plus `delta_savings_pln`, `delta_savings_pct`.

### Rozbicie „dlaczego” — dokładne, addytywne (suma efektów = Δ oszczędności)
Dla miesiąca, gdzie P = produkcja, s = sc/P, p = cena zakupu, r = RCEm, A = arbitraż, indeks 0 = rok temu, 1 = teraz:

| Efekt | Wzór | Co mówi |
|---|---|---|
| Produkcja (pogoda/instalacja) | (P1−P0)·(s0·p0 + (1−s0)·r0) | więcej/mniej kWh wycenione po zeszłorocznemu |
| Autokonsumpcja (struktura) | (s1−s0)·P1·(p0−r0) | większy udział zużycia na miejscu (bateria, nawyki) |
| Cena zakupu | sc1·(p1−p0) | autokonsumpcja warta więcej, bo prąd droższy |
| Cena RCEm | ex1·(r1−r0) | zmiana ceny sprzedaży (w MTD: „szac.”) |
| Arbitraż baterii | A1−A0 | |

YTD = suma efektów miesięcznych (ceny różnią się per miesiąc — nie wolno liczyć na sumach rocznych).
Tożsamość sumy sprawdzana testem.

### YTD — bilans narastający „do dziś vs do tej samej daty rok temu”
Tabela ten rok | rok temu | Δ (zł/kWh i %), kolor wg korzystności (mniej kosztów = zielony):

| Grupa | Wiersze |
|---|---|
| Pieniądze | oszczędności razem (autokons./sprzedaż/arbitraż w podpisie), zakup energii zł, przychód ze sprzedaży zł, **koszt netto sieci** (zakup − sprzedaż), śr. cena zakupu, śr. RCEm |
| Energia | produkcja kWh + uzysk kWh/kWp, zużycie domu, pobór z sieci, oddane do sieci, autokonsumpcja %, autarkia % (Δ w pp) |
| Tempo | projekcja roku = pełny rok poprzedni × (YTD teraz / YTD rok temu), dla zł i kWh, podpis „orientacyjnie”; „do zeszłorocznego wyniku brakuje X” / „przebite o X” |

Rozbicie przyczyn YTD pod tabelą w zwijanym „Dlaczego?”.

### Wyścig — wykres narastający
Dwie linie po miesiącach: rok bieżący (punkty na koniec zamkniętych miesięcy + punkt „dziś”) i rok
poprzedni (cały rok, przerywana), znacznik „dziś” na obu. Przełącznik: oszczędności zł | produkcja kWh |
koszt netto sieci zł. Punkt „ta sama data rok temu” pochodzi z okna MTD, nie z interpolacji.

### Bieżący miesiąc — szczegóły
- kWh obu stron: nowa funkcja okna LTS (period=hour, lifetime-liczniki z `get_energy_dashboard_sources`,
  te same encje co `read_month_from_statistics`), wspólne odcięcie godzinowe.
- Ceny `prev`: miesięczne z zeszłorocznego rekordu (`buy_price`, `feedin_price`); arbitraż `prev`
  proporcjonalnie do kWh ładowania w dolinie w oknie (fallback: proporcja dni).
- Ceny `cur`: `buy_price` z bieżącego rekordu; RCEm = średnia godzinowych RCE od 1. dnia (cache
  `rce_hourly`) × współczynnik 1,23 (`rce_hourly._vat_factor`); flaga `rcem_estimated`.
- Liczone w pętli odczytu i cache'owane w pamięci; `/api/data` tylko serwuje (LTS WS bywa wolne).

### Kształt w `/api/data` (jak faktycznie zaimplementowane, `yoy.py`)
```
yoy: { as_of, days_elapsed, method_default: 'estimate', methods: ['estimate','prior_year'],
       mtd: { estimate: Period, prior_year: Period },
       ytd: { estimate: Period, prior_year: Period },
       projection: { estimate: Projection|null, prior_year: Projection|null },
       race: { cur_year, prev_year,
               prev:        {savings_pln:[12], produced_kwh:[12], net_grid_cost_pln:[12]},  // narastająco, pełny rok
               cur_closed:  {savings_pln:[..], produced_kwh:[..], net_grid_cost_pln:[..]},  // narastająco, tylko zamknięte mies.
               cur_today:   { estimate: {savings_pln, produced_kwh, net_grid_cost_pln}|null,
                              prior_year: {...}|null } } }   // front dokleja jako ostatni punkt za cur_closed
Period:     { label, cur: Metrics, prev: Metrics, delta_savings_pln, delta_savings_pct,
              effects: {production, self_consumption, buy_price, feedin_price, arbitrage}|null,
              flags: {rcem_estimated, unpaired_months?, correction_factor?, ...} }
Projection: { savings_pln, produced_kwh, prev_full_savings_pln, prev_full_produced_kwh,
              gap_savings_pln, pace_pct }
```
`yoy` całość jest `null` 1. dnia miesiąca (okno MTD puste — patrz `days_elapsed`).
Poza zakresem (YAGNI): sensory HA dla r/r, selektor dowolnego miesiąca.

## Kafel (UI)

Pełna szerokość w miejscu wachlarza; górny rząd dwie kolumny (MTD | YTD), pod spodem wyścig;
na telefonie wszystko jedno pod drugim:
```
┌ Rok do roku ─────────────────────────────────── stan: 27.09 14:00 ┐
│ WRZESIEŃ 1–27             │ OD POCZĄTKU ROKU (1.01–27.09)        │
│ 612 zł  vs 548 zł         │              2026     2025     Δ     │
│ ▲ +64 zł (+11,7%)         │ Oszczędności 5 840 zł 5 410 ▲+7,9%   │
│ Dlaczego:                 │ Koszt sieci  1 920    2 150 ▼−10,7%  │
│  Produkcja (pogoda)  +42  │ Produkcja    5 980    5 610 ▲+6,6%   │
│  Autokonsumpcja      +18  │ Zużycie      6 400    6 520 ▼−1,8%   │
│  Cena zakupu          +9  │ … (pełna lista wierszy wyżej)        │
│  Cena RCEm (szac.)    −7  │ Tempo: rok ~7 400 zł (2025: 6 860)   │
│  Arbitraż baterii     +2  │ ▸ Dlaczego? (rozbicie YTD)           │
│ produkcja/zużycie/autok.  │                                      │
├ Wyścig narastająco  [zł | kWh | koszt sieci] ──────────────────────┤
│  2026 ── do „dziś” ●     2025 ┄┄ cały rok                        │
└────────────────────────────────────────────────────────────────────┘
```
Wachlarz: przeniesiony 1:1 do zakładki Wykresy (+ `_fanChart` w liście `resize()` w `showTab('charts')`).

## Wynik Etapu 1 (27.09.2026)

**Przełącznik metody** (dodany po pytaniu usera w trakcie brainstormingu): oprócz
skorygowanego szacunku RCE, `mtd`/`ytd`/`projection`/`race.cur_today` mają teraz
klucz na każdą z metod (`estimate`, `prior_year`) — user backtestował samą
propozycję „RCEm sprzed roku" (śr. błąd 22,4%, gorzej niż 15,1% dla szacunku
godzinowego, pojedyncze pudło −50% przy skoku cen r/r) i mimo to chciał opcję
wyboru w UI, domyślnie `estimate`.

**Błąd metodologiczny złapany na żywych danych, zanim trafił do usera:** pierwsza
wersja `estimate_current_month_feedin_price` liczyła PROSTĄ (nieważoną) średnią
z 24 godzin/dobę. Weryfikacja na żywym HA (get_energy_window + realne ceny PSE,
27.09.2026) pokazała, że to zawyżało cenę ~2× (sierpień 2026: prosta średnia
0,71 zł/kWh vs eksport-ważona 0,29 zł/kWh vs realna RCEm 0,362 zł/kWh) — nasz
eksport PV skupia się w tanich południowych godzinach (często ujemna RCE), a
prosta średnia dobowa waży równo drogie godziny wieczorne/nocne, w których w
ogóle nie eksportujemy. Naprawione: `estimate_current_month_feedin_price`
reużywa teraz `compare_month()`'s `rce_weighted_price_pln_kwh` (ta sama metoda
co reszta modułu, ważona NASZYM profilem eksportu) jako bazę, dopiero na niej
liczona jest korekta medianą z ostatnich 6 zamkniętych miesięcy. Po poprawce:
`raw_pln_kwh` dla 2026-09 wyszło 0,3808 — dokładnie tyle, ile żywy add-on już
sam raportował jako `rce_weighted` dla tego miesiąca — potwierdzenie zgodności.

**Przy okazji potwierdzone (nie błąd):** ceny RCE we wrześniu 2026 realnie
skoczyły 2–3× względem sierpnia — zweryfikowane bezpośrednio przez oficjalne
PSE REST API (`api.raporty.pse.pl`, dzień po dniu), niezależnie od kodu add-onu.

**Weryfikacja bez wdrożenia:** cały pipeline (`get_energy_window`, skorygowany
szacunek, `yoy.build_yoy_payload`) przetestowany z tej powłoki bezpośrednio na
żywym HA (real Supervisor token, real Energy Dashboard, realne LTS) oraz na
prawdziwych rekordach historycznych pobranych z żywego `/api/data` add-onu —
bez rebuildu/restartu samego add-onu. Wynik dla 27.09.2026, dzień 26:
MTD (estimate) 327,75 zł vs 338,39 zł rok temu (−3,1%, produkcja i cena zakupu
w dół, cena RCEm w górę); MTD (prior_year) pokazałoby mylące −17,9% (nie widzi
tegorocznego skoku cen). Tożsamość sumy efektów = Δ oszczędności zweryfikowana
zarówno testami jednostkowymi, jak i na tych żywych liczbach.

15/15 nowych testów (`tests/test_yoy.py`) + pełny pakiet (518 testów) zielone.

## Etapy (każdy kończy się checkpointem — bez kolejnego bez „go”)

**Etap 0 — dokument.** Zapis tego planu jako `docs/ROADMAP_YOY.md` w repo add-onu. ✅ (ten plik)

**Etap 1 — dane (zero zmian UI), release 0.37.0.**
- Nowy moduł `pv_roi_tracker/yoy.py`: czyste funkcje metryk, dekompozycji, parowania YTD.
- `live_reader.py`: funkcja okna LTS (reużywa `_ws_statistics`, `get_energy_dashboard_sources`,
  `_all_role_entities`, `_sum_role_month`).
- `rce_hourly.py`: średnia RCE bieżącego miesiąca do odcięcia.
- `main.py`: wyliczenie w pętli + cache; `web.py` `api_data`: blok `yoy`.
- Testy `tests/test_yoy.py` (tożsamość sumy efektów, parowanie, brak danych, flaga szac.,
  projekcja i serie wyścigu — ostatni punkt `cur` = YTD, `prev.at_today` = YTD rok temu).
- Backtest szacunku RCEm na zamkniętych miesiącach 2025–2026 vs opublikowana RCEm — wynik raportuję;
  jeśli błąd duży, wracamy do decyzji o wycenie eksportu.
- Checkpoint: pokazuję żywy JSON `yoy` (direct-IP add-onu) z liczbami do sprawdzenia przez usera.

**Etap 2 — UI, release 0.38.0.**
- `static/index.html`: kafel zamiast wachlarza; wachlarz do `tab-charts`.
- `static/app.js`: `renderYoyTile(d.yoy)` (MTD + tabela YTD + tempo) i `renderYoyRaceChart` (Chart.js);
  przełącznik metody `estimate`/`prior_year` (mały toggle w kaflu, domyślnie `estimate`, zmienia
  wyłącznie który klucz z `mtd`/`ytd`/`projection`/`race.cur_today` jest odczytywany — bez re-fetchu),
  resize fanChart w `showTab`; `app.css`: siatka 2→1 kol.
- Cache-busting jest (`?v={{VERSION}}`, badge `appVer`) — sprawdzić, że wersja rośnie.
- Dokumentacja w modalu (`openDocsModal`) + README/CHANGELOG + release notes z tabelą pól.
- Checkpoint: screenshoty desktop + mobile i konsola bez błędów.

## Pliki
`pv_roi_tracker/{yoy.py (nowy), live_reader.py, rce_hourly.py, main.py, web.py,
static/index.html, static/app.js, static/app.css}`, `tests/test_yoy.py`,
`config.yaml` + `pv_roi_tracker/__init__.py` (bump), `CHANGELOG.md`, `README.md`.
Release wg checklisty: push do `miczu71/pv_roi_tracker` (ten checkout), opublikowany GH release,
update add-onu przez Supervisor (slug `29a4454d_pv_roi_tracker`), oraz sync do `/config` (jeśli
osobno śledzony) wg dotychczasowego workflow.

## Weryfikacja
- `pytest` z katalogu `pv_roi_tracker/` (ten repo) — całość zielona.
- Po update: `/api/data` przez direct-IP → blok `yoy`; ręcznie sprawdzić, że YTD cur/prev dla
  zamkniętych miesięcy zgadza się z tabelą „Podsumowanie roczne” (Oszcz. r/r) i że suma efektów = Δ.
- Etap 2: Playwright direct-IP, screenshot desktop + 390px do `/config/playwright/`, konsola bez błędów,
  wachlarz renderuje się w zakładce Wykresy.
