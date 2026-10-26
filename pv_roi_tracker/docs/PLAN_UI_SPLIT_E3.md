# PLAN E3 – Pasek podsumowania, Taryfa, Faktury, kafel depozytu

Źródło prawdy: `docs/ROADMAP_UI_SPLIT.md` (decyzje 3, 5, 6; wariant A z prototypu E0).
Kod: `pv_roi_tracker/pv_roi_tracker/static/{index.html,app.js,app.css}` (ścieżki niżej względem katalogu `pv_roi_tracker/pv_roi_tracker/pv_roi_tracker/`). Backend bez zmian (API z E1 wystarcza).

## Zakres
1. **Pasek podsumowania** (`#taryfySummary`) nad panelami sekcji „Taryfy i energia” (widoczny tylko gdy sekcja = taryfy; ukrywany w pv/ustawieniach w `applyRoute`). Cztery kafle w jednym rzędzie (zawijane na mobile):
   - Teraz: strefa + nazwa taryfy (`/api/tariff_now`: `tariff`, `zone`, `next_change`) z dopiskiem „do HH:00 → <strefa>”.
   - Cena kWh teraz: `gross_per_kwh` (zł, 4 miejsca).
   - Ostatnia faktura: kwota (`amount_due`), miesiąc, termin i status płatności z `api/ebok/payment-status` (`active` → „do zapłaty do DD.MM”, brak `active` → „opłacona/brak zaległości”); dane faktury z `_lastInvoices`.
   - Rekomendacja: jedno zdanie z `api/tariff_sim` (`recommendation`; użyć tego samego źródła co `#tsimReco`, nie duplikować fetchu jeśli dane są już w pamięci modułu Symulacji).
   Odświeżanie: przy wejściu w sekcję i co 5 min (`setInterval`, czyszczony przy wyjściu).
2. **Taryfa** (`tab-taryfa`) – nowa kolejność:
   - Twoja taryfa G12w: tabela stawek brutto w strefach (peak/offpeak) z `/api/tariff_catalog` (okres aktualny wg dnia; brutto = netto składników × `meta.vat`, rozbicie: energia, dystrybucja zm., opłaty zm.), godziny stref (z `zones.G12w`, święta/weekendy = offpeak cały dzień), skład ceny (słupek skumulowany lub tabela składników zł/kWh).
   - Wykres ceny z 7 dni: `/api/tariff_hourly?from=<dziś-6>&to=<dziś>&tariffs=G12w`, krok schodkowy (`stepped:true`) z `hours.G12w[].gross` (nazwy pól sprawdzić w `tariff_sim.hourly_series` i testach E1). Zastępuje stary wykres z LTS (jeśli istnieje w tab-taryfa; grep `taryfaChart`/`lts`).
   - Oś czasu stawek tylko do odczytu: istniejące `_renderTaryfaTimeline` bez przycisków Edytuj/Usuń (parametr `readonly`), pod nią przycisk „Edytuj w Ustawieniach” (`showTab('settings')`, już jest – zmienić etykietę).
   - Cennik Tauron: `<details>` z tabelą wszystkich taryf (G11/G12/G12w/G13), stref i stawek 2025/2026 z `periods` (wybór okresu `<select>`), tylko odczyt.
3. **Faktury** (`tab-invoices`) – przestawienie sekcji w DOM (id i renderery bez zmian), kolejność:
   1. KPI: ostatnia faktura, średni koszt (średnia `amount_due` z `_lastInvoices` za ostatnie 12 faktur), efektywna zł/kWh (`rate_trend.latest_effective_gross_per_kwh`), depozyt (saldo jak w `_renderInvKpiCards`). Zastępuje obecne 4 kafle (Saldo depozytu/Ostatnia faktura/Pokrycie/Status taryfy); „Pokrycie” i status dryfu przechodzą do zwijanej sekcji pokrycia.
   2. „Wgrane faktury” + wgrywanie PDF (blok `invoiceFiles`/`uploadInvoices`/`invoiceTableWrap`).
   3. „Na co idą pieniądze” (`costBreakdown*`).
   4. Trend stawek (`rateTrendSection`).
   5. Zwijane `<details>` (domyślnie zamknięte): Depozyt (wykres + `depKpiCards`), Rekonsyliacja (`reconKpiCards`…), Pokrycie fakturami (`invCoverageGrid`) wraz z `tariffDriftBanner`.
   Chart.js w zwiniętych `<details>` ma zerowy rozmiar: po `toggle` wołać `chart.resize()` (`_depositChart`).
4. **Kafel depozytu w karcie głównej PV ROI**: nowa karta w `#cards` (`renderCards`) „Saldo depozytu” – wartość = `bal` jak w `renderDepositSection` (`balance_estimate ?? balance_model`), sub: „traci ważność za 3 mies.: X zł” (`expiring_3m`). Dane z `d.deposit`; `renderCards(s)` ma tylko `summary`, więc kafel dopisać osobną funkcją `renderDepositTile(dep)` wołaną obok `renderDepositSection` w `loadData` (wstawia kartę do `#cards` po wywołaniu `renderCards`; idempotentnie – usuwa poprzednią po `id`). Brak `dep` → brak kafla.

## Kryteria sukcesu
- Pasek widoczny tylko w sekcji Taryfy; `tariff_now` błąd/503 → kafel „—” bez wyjątku w konsoli.
- Wszystkie dotychczasowe id (`invKpiCards`, `depKpiCards`, `costBreakdown*`, `rateTrendChart`, `depositChart`, `recon*`, `invCoverageGrid`, `taryfaList`, `taryfaBadge`) istnieją; renderery nie rzucają.
- Konsola bez błędów (desktop 1440, mobile 390), brak przewijania poziomego.
- `pytest -q` zielony (865 passed); `test_web_static.py` dostosowany, jeśli sprawdza treść/porządek.

## Wykonawca (jedna paczka, sonnet-worker – UI z logiką danych, ryzyko średnie)
Pliki: `static/index.html`, `static/app.js`, `static/app.css`, `tests/test_web_static.py` (jeśli wymaga).
Zmiany:
1. `index.html`: kontener `#taryfySummary` po `#paymentBar`/przed `.tab-panel` sekcji; przebudowa `tab-taryfa` (4 bloki z p.2) i `tab-invoices` (kolejność p.3, `<details>` dla depozytu/rekonsyliacji/pokrycia).
2. `app.js`: `renderTaryfySummary()` + timer, `loadTaryfaTab` rozszerzony o stawki/skład/godziny/7 dni/cennik, `_renderTaryfaTimeline(…, readonly)`, nowe KPI w `_renderInvKpiCards`, `renderDepositTile`, `applyRoute` (pokaż/ukryj pasek, resize wykresów po `toggle`).
3. `app.css`: style paska i kafli (tokeny z istniejącego `:root`; bez nowych kolorów poza już używanymi), `@media (max-width:640px)` zawijanie.
Komenda testu: `cd /config/addons/pv_roi_tracker/pv_roi_tracker && <venv>/bin/python -m pytest -q`; dodatkowo Playwright na lokalnym serwerze testowym (zrzuty tylko do `/home/claude/.playwright-mcp`).
`DONE:` pkt 1–4 zrealizowane wg kryteriów wyżej; pytest zielony; raport z listą zmienionych plików i zrzutami 1440/390 sekcji Taryfy (4 panele) i Faktury.
`NIE RÓB:` nie ruszać backendu (`web.py`, `tariff_*.py`), `tab-tsim` i jego JS (to E4), ani `tab-settings`; nie zmieniać id istniejących elementów; nie dodawać zależności ani bibliotek; nie commitować; nie edytować plików poza listą.

## Rollback
`git revert` commitu(ów) E3 (backend nietknięty).
