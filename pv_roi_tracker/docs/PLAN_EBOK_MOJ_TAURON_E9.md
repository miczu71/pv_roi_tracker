# PLAN E9 — weryfikacja katalogu G13/G13s (tylko odczyt)

Źródła: wyciąg z taryfy TAURON Dystrybucja 2026 (grupy G, decyzja URE DRE.WRE.4211.54.8.2025.ASa1) oraz taryfa TAURON Sprzedaż 2026 (BIP URE nr 265). Porównanie z `pv_roi_tracker/tariffs/tauron.json` (okres 2026-02-01).

## Wynik (27.10.2026)
- Strefy G13 (lato 7–13 / 19–22, zima 7–13 / 16–21, dni wolne = `other`): zgodne.
- Strefy G13s (lato 9–17 offpeak, 7–9 i 17–21 peak, noc 21–7; zima 10–15, 7–10 i 15–21): zgodne.
- Dystrybucja zmienna G13 (0,2203 / 0,3898 / 0,0392) i G13s: zgodne. Energia G13 (0,4668 / 0,7780 / 0,4210): zgodna.
- Opłaty zmienne: OZE 0,0073, kogeneracja 0,003, akcyza 0,005 zgodne. Jakościowa 0,0332 od 2026-02-01 (0,0331 w styczniu), zgodne z serwisem porównawczym.
- Do wyjaśnienia (bez zmiany katalogu): abonament dystrybucyjny — taryfa podaje 2,28 zł netto przy rozliczeniu 1-miesięcznym, katalog 4,56 (kalibracja z faktur). Ceny G13s u sprzedawcy: brak w taryfie Tauron Sprzedaż (oferta „Tanie godziny”).
- Poprawka katalogu: brak, więc bez zmian w kodzie.
