# PV ROI Tracker — „Depozyt: zasilenie 2025-04/06 + B8"


Kopia zatwierdzonego planu z `/data/home/.claude/plans/`.

## Context

Podprojekty YoY (0.38.0), Pompa ciepła (0.41.0) i Dług depozytowy (0.42.0) są zamknięte.
User wybrał kolejny temat: otwarty rest z `ROADMAP_DEPOSIT.md`, czyli zawyżone `diff_pct`
w rekonsyliacji depozytu oraz B8 (od kiedy biegnie 12-mies. przedawnienie). Cel: **domknięcie,
żeby zakładka depozytu nie miała znanych nieścisłości**.

Ustalenia z wywiadu i żywych danych (28.09.2026, 0.42.0):
- **B8 nie ma dziś wpływu na żadną liczbę.** `expired_refund_total = expired_forfeit_total = 0`,
  prognoza 12 mies. też 0 (saldo zimą spada do ~55 zł w 2026-12, FIFO zjada partie 2026-05…08
  kilka miesięcy przed terminem 2027-05…08). User potwierdza: Tauron nigdy nic nie zwrócił ani
  nie umorzył. Przesunięcie zegara o 1–2 mies. nic nie zmienia → B8 zamykamy notatką, bez kodu.
- **`diff_pct` to realna rozbieżność zasilenia, nie artefakt flagi.** Status `ok` (nie capped):
  2025-04 model 63,23 vs Tauron 34,31 (+84%), 2025-05 97,14 vs 18,14 (+435%), 2025-06 72,93 vs 31,68 (+130%).
  Tauron przypisał 2–5× mniej niż model `eksport × RCEm`. Jeśli przyczyna jest systematyczna,
  **headline `balance_model` 474,72 zł jest zawyżony** w miesiącach wysokiego eksportu.
- Hipotezy: **H1** Tauron wycenia godzinowo RCE (ujemna → 0), nie miesięczną RCEm — wiosna 2025
  miała rekordowo dużo ujemnych godzin w południe; **H2** przesunięty okres rozliczeniowy / lag
  tylko w tych miesiącach; **H3** korekta / faktura korygująca.

Wybrane podejście: **spike, potem decyzja**.

## Etapy (każdy kończy się checkpointem; następny tylko za „go”)

### Etap 0 — dokument
`pv_roi_tracker/docs/ROADMAP_DEPOSIT_FEEDIN.md` = ten plan. W `ROADMAP_DEPOSIT.md` odnośnik
w sekcji otwartych tematów + zamknięcie B8 jako notatka (dowód: zero przedawnień w historii
i prognozie, margines FIFO kilka miesięcy; reguła ustawowa zapisana, bez zmiany kodu).
Commit + push w repo add-onu (`/config/addons/pv_roi_tracker`, `main`).

### Etap 1 — spike analityczny (zero zmian w add-onie, zero wydania)
Skrypt w scratchpadzie, tylko odczyt:
1. Z PDF-ów (`/api/invoice/pdf?key=YYYY-MM`, `pdftotext -layout`) faktur 2025-03…2025-08:
   okres rozliczeniowy, wiersze depozytu, ewentualne pozycje korekty (H2, H3).
2. H1: dla każdego miesiąca eksportu z `implied` (status ok, ~wszystkie nie-capped miesiące,
   nie tylko 3 odstające) policzyć wartość godzinową RCE z ujemną → 0. Reużyć
   `rce_hourly.compare_month()` (już implementuje art. 4b: `max(p, 0)`, VAT factor) z godzinowym
   eksportem z cache `heatpump_hours.json` / `live_reader` i cenami z cache `rce_hourly`.
   Porównać z `tauron_implied` obok obecnego modelu RCEm.
3. Ustalić datę przejścia (jeśli H1): od kiedy Tauron liczy godzinowo (oczekiwanie: ~2024-07,
   zmiana ustawowa) — czy przed nią RCEm pasuje, a po niej RCE godzinowa.
4. Metryka: mediana |diff_pct| i liczba miesięcy w ±10% dla każdego wariantu; odtworzone saldo
   na 2026-08 wg zwycięskiego wariantu vs 474,72 zł.

**GO na Etap 2:** jeden wariant schodzi z odstającymi miesiącami do ±~15% i nie psuje reszty.
**NO-GO:** raport z najlepszym dopasowaniem i wyjaśnieniem; zostaje etykieta jakości z 0.42.0.
**Checkpoint:** tabela miesiąc × wariant, odtworzone saldo, rekomendacja; wynik dopisany do pliku.

### Etap 2 — (warunkowo) poprawka naliczania zasilenia, wydanie 0.43.0
Zakres ustalany po Etapie 1. Jeśli H1: `deposit.py` przyjmuje miesięczne `accrued` z wyceny
godzinowej (dostawca z `rce_hourly`, fallback RCEm + flaga „szac.” gdy brak cen/eksportu
godzinowego), bez zmiany logiki FIFO/przedawnienia. Testy w `tests/test_deposit.py`,
CHANGELOG/README (zmiana znaczenia sensora `PV Deposit Balance Est`), release wg
`feedback_pv_roi_release_checklist`, Playwright direct-IP `172.30.33.15:8099` (desktop + 390px, konsola).

## Ryzyka
- Godzinowy eksport sprzed 2023-06 / luki LTS → część miesięcy bez wariantu H1 (raportowane).
- Ceny RCE w cache mogą nie sięgać wstecz — `rce_hourly._ensure_prices` dociąga z PSE; w spike'u
  tylko do scratchpadu, nie do cache add-onu.
- Poprawka zasilenia zmieni historię sensora salda — opis w release notes.

## Weryfikacja
- Etap 0: pliki w repo, `git log origin/main` pokazuje commit.
- Etap 1: ręczne przeliczenie 2025-05 (najgorszy) z godzin i PDF; tabela dla wszystkich miesięcy ok.
- Etap 2: `pytest` zielony (577 + nowe), `/api/data | jq .deposit.reconciliation` — brak |diff_pct| > 25%
  poza wyjaśnionymi, health `ok`, screenshoty w `/config/playwright/`.
