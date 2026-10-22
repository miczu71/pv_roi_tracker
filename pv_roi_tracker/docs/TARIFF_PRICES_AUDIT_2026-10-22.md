# Audyt cen symulacji taryf (tauron.json, pv_roi_tracker 0.46.1), 2026-10-22

Tylko odczyt: nic w katalogu nie zmieniono. Źródła A–G jak w `tariffs/tauron.json` `_meta.sources`.
Oznaczenia: **[S]** sprawdzone przeze mnie w PDF-ie lub liczone skryptem, **[A]** tylko raport subagenta `docs-scout` (cytaty z PDF czytanych Readem), **[W]** źródło czytane przez WebFetch z podsumowaniem modelu (nie dosłownie).

## Część A: ceny aktualne (2026)

Zgodne ze źródłem [A]: energia netto per strefa (E pkt 5), dystrybucja zmienna (A pkt 7.1), G14dynamic S1–S4 (A pkt 7.3), jakościowa 0,0331 (A pkt 7.4), 0,0332 od 1.02 [W: G], OZE 0,0073, kogeneracyjna 0,0030, sieciowa 7,38/10,86, abonament 4,56, mocowa 4,29/10,31/17,18/24,05, strefy G12/G12w/G13, dyn_b 0,5020/0,6175 od 1.01.2026, dyn_a: cena minimalna 0,0050 (dla średniej miesięcznej), start 1.04.2026, baza TGE RDN Fixing I. Brutto z B = netto × 1,23 z dokładnością do ostatniej cyfry [A].

| # | Rozbieżność | Wpływ | Propozycja |
|---|---|---|---|
| A1 | Katalog nie ma akcyzy. E pkt 1.6: ceny „nie zawierają VAT oraz podatku akcyzowego”. Z akcyzą 0,005 zł/kWh netto G12w daje 1,2304 / 0,6306 brutto (faktury 1,23 / 0,63), bez niej 1,2242 / 0,6245 **[S]**. Kwoty 0,005 nie ma w źródłach, to moja wiedza. | ~0,006 zł/kWh brutto we wszystkich taryfach po równo: sumy w zł zaniżone, ranking bez zmian | Potwierdzić kwotę na fakturze, dodać `excise_net` do okresów |
| A2 | `margin_net_per_kwh` 0,0892 to według cennika składnik SC (koszty sprzedaży energii z TGE + marża), nie sama marża [A] | brak (wzór Fixing + SC jest taki sam) | Zmienić etykietę pola |
| A3 | dyn_a: cennik dopuszcza ceny godzinowe lub kwadransowe, strona oferty mówi o godzinowej [A, W] | pomijalny (kwadranse uśredniane do godziny w `fetch_rdn_hourly`) **[S]** | Brak |
| A4 | Zegary stref: E pkt 3.2.2 mówi, że zegary sterujące nie przechodzą na czas letni [A] | możliwe przesunięcie stref o 1 h latem, nie modelowane | Sprawdzić na liczniku/fakturze, czy G12w latem tak działa |
| A5 | Opłata przejściowa 2026: A jej nie ma, B ma mylący nagłówek, wartości to mocowa × 1,23 [A]. Wariant MAX dyn_a (23,49 zł, 0,68 zł/kWh) bez źródła [A]. `_meta.sources` bez D. | brak w symulacji | Odnotować |

## Część B: ceny historyczne (2025)

| # | Pole | Katalog | Źródło | Status |
|---|---|---|---|---|
| B1 | Ceny stref sprzedaży I–IX 2025 | „NIE USTALONO” | URE 18556, pkt 5.1a (str. 5 biuletynu): G11 0,6270; G12 0,7120/0,4620; G12w 0,8440/0,4620; G13 0,6700/0,9560/0,5140 **[S]**. Obowiązują od 1.07.2024, koniec 30.09.2025 wg 20036 pkt 7 [A] | rozbieżne: dokument istnieje |
| B2 | Cena maksymalna | 0,50 płasko we wszystkich strefach | 18556 pkt 5.3: cena maks. 0,500 zł/kWh, a gdy cena z taryfy jest niższa, stosuje się niższą **[S]** → min(strefa; 0,50): G12 noc, G12w pozaszczyt = 0,462. Pkt 5.3 w tym dokumencie mówi o okresie do 31.12.2024; przedłużenie na 2025 wg 20036 [A] | rozbieżne w strefach nocnych; netto/brutto nie ustalono |
| B3 | Faktury G12w 2025 | — | ROADMAP: faktury I–VI i X pokazują strefowo 0,76/0,42, więc ani 0,50 płasko, ani min(strefa; 0,50) (0,50/0,462) ich nie odtwarza | otwarte: potrzebna pozycja faktury (rekompensata?) |
| B4 | Ceny odniesienia Q4 2025 | `energy_net_reference_q4` | 20036 pkt 5.1b [A] | zgodne (9 wartości) |
| B5 | Dystrybucja zmienna VII–XII 2025 | tab. 7.1 | C str. 3, obowiązuje od 1.07.2025 [A] | zgodne |
| B6 | Dystrybucja zmienna I–VI 2025 | te same stawki co VII–XII | Taryfa z decyzji 16.12.2024 (https://www.tauron-dystrybucja.pl/-/media/offer-documents/dystrybucja/archiwum-taryf/2025/taryfa-td_sa-na-rok-2025.ashx), tab. 7.1, str. 41 taryfy **[S]**: G11 0,2541; G12 0,2899/0,0609; G12w 0,3271/0,0518; G13 0,1883/0,3332/0,0349; stała 7,02/10,34; abonament 4,56. Jakościowa 0,0321 i G14dynamic S1–S4 [A] | **zgodne** (rozstrzygnięte 2026-10-22). OZE, kogeneracja i mocowa nie mają stawek liczbowych w tej taryfie |
| B7 | Opłaty stałe | I–VI 14,90; VII–XII 30,91 | 10,34 + 4,56 = 14,90; + mocowa 16,01 (C str. 4 pkt 7.7 **[S]**) = 30,91 | zgodne z fakturami; zwolnienie z mocowej w I–VI nie ma podstawy w odczytanych dokumentach |
| B8 | Opłata przejściowa 2025 | brak | C str. 4: 0,02 / 0,10 / 0,33 zł/m-c, „od 1 stycznia 2025” **[S]** | rozbieżne, ale faktury (14,90 i 30,91) jej nie zawierają, więc bez zmiany (0,33 zł/mies. to ≤0,41 zł brutto) |
| B9 | Jakościowa 0,0321, OZE 0,0035, kogeneracyjna 0,0030 | jak w katalogu | C str. 4 [A]; jakościowa i przejściowa mają nagłówek „od 1.01.2025” **[S]** | zgodne (OZE i kogeneracja I–VI nie ustalono) |
| B10 | G14dynamic S1–S4 2025 | 0,0118 / 0,0470 / 0,3528 / 2,3521 | C str. 4 tab. 7.3 **[S]** | zgodne (VII–XII) |
| B11 | Strefy 2025 | jak 2026 | G12w i G13 zgodne wg C; G12: C podaje tylko liczbę godzin (14/10), nie ustalono godzin | częściowo |
| B12 | Kompas PSE 2025, kody `usage_fcst` | — | brak w dokumentach urzędowych; schemat sprawdzony na żywo w E2 (ROADMAP) | nie ustalono dla 2025-01 |

## Do decyzji usera
1. B1+B2+B3: czy zostać przy 0,50 płasko w 2025 (decyzja z 21.10), czy zmienić na min(strefa; 0,50). Najpierw potrzebna pozycja z faktury 2025 (B3), bo żadna wersja nie odtwarza 0,76/0,42.
2. A1: dodać akcyzę po potwierdzeniu kwoty.
3. B6: rozstrzygnięte, katalog zgodny.
Zmiany `tauron.json` wymagają osobnego planu i wydania (bump, release).

## Wykonane 2026-10-22 (decyzja usera: „popraw / dodaj / poszukaj”)
- A1: `excise_net` 0,005 w okresach 2025 i 2026, wliczone do `fees` (`tariff_catalog.rates_per_kwh`).
- B1+B2: 2025 liczone jako min(cena strefy; 0,50): okres I–IX wg 5.1a, Q4 wg 5.1b (osobny okres 2025-10-01).
- B6: dokument znaleziony, bez zmian stawek.
- Otwarte: B3 (faktury 0,76/0,42). Zgodność z fakturami sprawdzić po wydaniu w `/api/tariff_sim`; jeśli się pogorszy, wycofać zmianę 2025.
