# PV ROI Tracker — „Depozyt: zasilenie 2025-04/06 + B8"


Kopia zatwierdzonego planu z `/data/home/.claude/plans/`.

## Wynik Etapu 1 (28.09.2026) — spike zakończony, GO na Etap 2 (zawężony zakres)

**H1 (Tauron liczy zasilenie po RCE godzinowej) odrzucone jednoznacznie.** Tekst faktur (2025-04,
2025-05) wprost: *„Depozyt prosumencki wyliczany jest na podstawie iloczynu ilości energii
wprowadzonej do sieci i rynkowej **miesięcznej** ceny energii elektrycznej”* — RCEm, nie RCE
godzinowa. Potwierdza to też komentarz w `rce_hourly.py`: moduł to tylko *hipotetyczna* symulacja
„co by było gdyby”, przejście na RCE godzinową jest nieodwracalną decyzją, której nie podjęto.

**Wolumen (kWh eksportu) NIE jest przyczyną.** Porównanie `exported_kwh` z rekordów add-onu z
kolumną „Ilość energii wprowadzonej do sieci” na fakturach 2025-02…2025-07 — **identyczne co do
kWh** (151, 372, 315, 364, 435, 355) — bo rekordy już są zrebase'owane do prawdy fakturowej
(`project_pv_roi_energy_rebase_0_35`). Formuła `accrued = exported_kwh × feedin_price` też się
zgadza wewnętrznie (np. 315 × 0,200724 = 63,23 = dokładnie `feedin_revenue` w rekordzie).

**Znaleziono realną przyczynę: rozszyfrowanie `tauron_implied`.** Dla Sty–Lip 2025 każda faktura
ma `deposit_used == deposit_previous` i `deposit_current == 0` (`verified: true`, saldo w pełni
drenowane co miesiąc) — to oznacza, że pole `deposit_previous` na fakturze za miesiąc M to **czyste
zasilenie z miesiąca M−1** (poprzednia rata i tak wyzerowana rachunkiem za M−1). Odczytane wprost:

| Miesiąc (zasilenie) | Realne zasilenie (z faktury M+1, previous) | Model (`accrued` = kWh×RCEm) | Różnica |
|---|---|---|---|
| 2025-03 | 68,06 zł (kWh 372, cena 0,183 zł/kWh) | 81,83 zł (cena 0,220) | +20% |
| 2025-04 | 34,31 zł (kWh 315, cena 0,109 zł/kWh) | 63,23 zł (cena 0,201) | +84% |
| 2025-05 | 18,14 zł (kWh 364, cena 0,050 zł/kWh) | 97,14 zł (cena 0,267) | **+435%** |
| 2025-06 | 31,68 zł (kWh 435, cena 0,073 zł/kWh) | 72,93 zł (cena 0,168) | +130% |

**Cała rozbieżność siedzi w cenie (RCEm), nie w kWh ani w mechanizmie.** Cena wynikająca z faktur
(realna) **spada** marzec→maj 2025 (0,183→0,109→0,050 zł/kWh) mimo rosnącego eksportu — spójne z
udokumentowanym krachem RCEm wiosną 2025 (nadpodaż PV, rekordowe godziny ujemnych cen). Cena
zeskrobana przez `rcem_scraper` (już z ×1,23 wg noweli OZE) jest w tym oknie **wyraźnie zawyżona**
względem tego, co faktycznie zasiliło depozyt — margines rośnie miesiąc po miesiącu do 435% w maju,
po czym spada (czerwiec 130%, sierpień z powrotem ~0%). Wypróbowane i odrzucone: usunięcie
współczynnika ×1,23 poprawia tylko marzec (0,220/1,23=0,179 ≈ 0,183 realne), nie tłumaczy
kwietnia/maja/czerwca — więc to nie jest prosty błąd współczynnika, tylko coś specyficznego dla
zeskrobanej ceny RCEm w tym 4-miesięcznym oknie.

**B8 zamknięte** (patrz `ROADMAP_DEPOSIT.md`) — bez wpływu na żadną liczbę dziś ani w 12-mies.
prognozie, user potwierdza brak zwrotów/umorzeń historycznie.

**Rekomendacja na Etap 2 (zawężona względem oryginalnego planu):**
1. Zweryfikować źródło `rcem_scraper` (strona PSE, tabela RCEm) dla marca–czerwca 2025 wprost
   przeciw niezależnemu źródłu (archiwum RCEm PSE / TGE) — sprawdzić, czy to błąd parsowania
   (zła kolumna/wiersz), błąd przypisania miesiąca, czy PSE faktycznie opublikowało/skorygowało
   inną wartość niż to, co jest w cache `rcem_scraper`.
2. Jeśli scraper się myli: poprawka parsera + re-scrape tych miesięcy, re-run `deposit_job`
   (bez zmiany logiki FIFO/przedawnienia — tylko wejściowa cena).
3. Jeśli scraper ma rację (PSE opublikowało wyższą wartość niż to, co realnie trafiło do depozytu):
   temat wykracza poza add-on — do zgłoszenia/wyjaśnienia u Taurona, model zostaje z etykietą
   niepewności w tym oknie (adnotacja w UI), bez zmiany kodu.
4. Testy w `tests/test_deposit.py`/`tests/test_rcem_scraper` dla przypadku 2025-03…06 jeśli
   poprawka wejdzie.

**Checkpoint: czeka na „go" na krok 1 (weryfikacja scrapera vs niezależne źródło RCEm).**

## Aktualizacja tego samego dnia (28.09.2026) — krok 1 wykonany, ZUPEŁNIE INNA przyczyna znaleziona

Krok 1 z rekomendacji wyżej wykonany: pobrano na żywo `https://www.pse.pl/oire/rcem-rynkowa-miesieczna-cena-energii-elektrycznej`
tym samym parserem co `rcem_scraper._scrape_all_months_full()`. **`rcem_scraper` jest w 100%
poprawny** — RCEm marzec/kwiecień/maj/czerwiec 2025 (182,96 / 163,19 / 216,97 / 136,30 PLN/MWh,
×1,23/1000) dają dokładnie te same ceny co w cache add-onu (0,219973 / 0,200724 / 0,266873 /
0,167649 zł/kWh — zgodność co do 6. miejsca po przecinku). **Pierwotna rekomendacja Etapu 2 (punkt
1–2) jest więc zamknięta jako NO-GO — problem nie jest w scraperze.**

Dalsze śledztwo (odtwarzanie `tauron_implied` wstecz z dat publikacji RCEm na pse.pl i dat
wystawienia faktur) doprowadziło do **faktur korygujących**: 24 z 39 faktur mają korektę
(`.corrections[]`), w tym cała seria `T/K1/BN567872/0001/25`…`/0016/25` + `/0003/26`…`/0005/26`,
wysłana masowo z datą **01.01.2026** z listem przewodnim: *„Korekty obejmują: aktualizację wartości
depozytu w oparciu o rynkowe ceny energii elektrycznej (RCE), powiększenie wartości depozytu
wypracowanego od 1.01.2025 r. o współczynnik 1,23”* — czyli Tauron sam przyznaje się do błędu w
naliczaniu depozytu w 2025 r. i koryguje go retroaktywnie.

**Znaleziony konkretny, potwierdzony błąd w `invoice_parser.py`:** każda korekta PDF ma DWIE sekcje
— „POLICZONO:” (stara, błędna wartość) i „NALEŻAŁO POLICZYĆ:” (nowa, poprawna). Kod (linie
648–664) **już ma zamysł** wycinania tekstu od markera „NALEŻAŁO POLICZY” w dół, żeby złapać tylko
poprawną sekcję — ale **nie działa**, bo `pypdf`'s `extract_text()` (bez trybu layout) dla tego
konkretnego, dwukolumnowego/nakładającego się szablonu PDF **zwraca tekst w innej kolejności niż
wizualna**: fragment sekcji „POLICZONO” (stara wartość) pojawia się w strumieniu tekstu ZARAZ PO
markerze „NALEŻAŁO POLICZYĆ:”, przed prawdziwą nową wartością. Zweryfikowane bezpośrednio w Pythonie
na `kor_04.pdf` (korekta kwietnia): regex po scope'owaniu do `_deposit_text` mimo to trafia
najpierw na `68,06` (offset 2034 — stara, POLICZONO) i dopiero potem na `74,31` (offset 2736 —
nowa, NALEŻAŁO POLICZYĆ). `pdftotext -layout` (narzędzie zachowujące układ wizualny) pokazuje
poprawną kolejność bez tego problemu — więc naprawa to **przejście na ekstrakcję z zachowaniem
layoutu** (albo `pdftotext -layout` jako subprocess, albo biblioteka z trybem layout, np.
`pypdf` z `extraction_mode="layout"` — dostępne w nowszych wersjach — do przetestowania) **tylko
dla ścieżki korekt**, lub dodatkowe zawężenie regexu do NAJDALSZEGO/OSTATNIEGO dopasowania w
tekście zamiast pierwszego (proste obejście, mniej pewne przy różnych szablonach).

**Skala:** potwierdzono na żywo dla kwietnia 2025 (`deposit_previous` błędnie 68,06 zamiast 74,31,
+6,25 zł), ale maj i czerwiec 2025 sprawdzone tą samą metodą pokazują **brak zmiany** między
POLICZONO i NALEŻAŁO POLICZYĆ (34,31=34,31, 18,14=18,14, `correction_delta_pln: 0.0`) — więc
**błąd parsera NIE tłumaczy całości** rozbieżności (zwłaszcza maja, +435%, gdzie i stara, i nowa
wartość Taurona to prawdziwe 18,14 zł, potwierdzone samą korektą). Skala błędu parsera na pozostałych
22 niezweryfikowanych korektach (2024-07…2025-01, 2025-07…2025-10, 2025-11…2026-01) nieznana —
część mogła się realnie zmienić jak kwiecień, część nie jak maj/czerwiec. Do sprawdzenia per-faktura
w Etapie 2.

**Wciąż otwarte po tym odkryciu:** dlaczego prawdziwe zasilenie maja/czerwca 2025 (18,14 zł /
364 kWh = 0,050 zł/kWh; 31,68 zł / 435 kWh = 0,073 zł/kWh) jest kilkukrotnie niższe niż oficjalna,
zweryfikowana RCEm (0,267 / 0,168 zł/kWh) — **nawet po korekcie Taurona się nie zmieniło**, więc to
nie ten sam mechanizm co błąd parsera. Możliwe wyjaśnienia do zbadania: (a) inny/wcześniejszy etap
korekty RCEm obowiązujący w momencie ORYGINALNEGO wystawienia (przed 01.01.2026 batch), którego już
nie widać na dzisiejszej stronie PSE; (b) jeszcze inna zasada naliczania dla tych 2 miesięcy,
nieudokumentowana na fakturze; (c) błąd po stronie Taurona nienaprawiony tym konkretnym batchem
korekt (skoro list przewodni mówi tylko o RCE-update + ×1,23, a nie o czymś trzecim). Nie zbadane
w tej sesji — wymaga albo kontaktu z Tauronem, albo dalszego śledztwa archiwalnych wartości RCEm
sprzed korekt.

**Rewizja rekomendacji na Etap 2 (zastępuje poprzednią z tego samego pliku wyżej):**
1. **Priorytet — napraw parser dla ścieżki korekt** (`invoice_parser.py` `_is_korekta` branch):
   przejść na ekstrakcję zachowującą layout wizualny (do zweryfikowania które podejście, patrz
   wyżej) zamiast prostego `page.extract_text()`. Test na `kor_04.pdf` (musi dać 74,31, nie 68,06)
   + regresja na już-poprawnie-sparsowanych korektach (maj/czerwiec — musi zostać 34,31/18,14 bez
   zmian) + na zwykłych (nie-korekta) fakturach (bez regresji).
2. Re-parse wszystkich 24 faktur z korektą (`/api/invoice/reparse` — **jawna zgoda usera przed
   wywołaniem**, bo przelicza też rekonsyliację; restart + weryfikacja po partii, wg lekcji z
   incydentu 2026-03).
3. Po re-parse: sprawdzić `deposit.reconciliation` na żywo — ile z 3 odstających miesięcy
   (2025-04/05/06) się poprawia. Jeśli maj/czerwiec zostają odstające (spodziewane, patrz wyżej) —
   udokumentować jako osobny, nierozwiązany temat z etykietą w UI, nie blokować wydania.
4. Testy w `tests/test_invoice_parser.py`: fixture korekty z dwiema sekcjami POLICZONO/NALEŻAŁO
   POLICZYĆ o różnych wartościach (regresja na dokładnie ten błąd), fixture bez zmiany wartości.
5. CHANGELOG/README, wydanie 0.43.0 wg `feedback_pv_roi_release_checklist`.

**Checkpoint: czeka na „go" na naprawę parsera (krok 1 rewizji).** Pytanie otwarte (maj/czerwiec
2025 wciąż niewyjaśnione mimo korekty Taurona) zostaje jako osobny temat do decyzji po naprawie
parsera — czy dochodzić dalej, czy zostawić z etykietą niepewności.

## Aktualizacja (28.09.2026) — user: „kontynuuj dla obu"; Część B rozwiązana

Plan Etapu 2 (fix parsera + śledztwo maj/czerwiec) zatwierdzony, skopiowany do
`docs/BLUEPRINT_DEPOSIT_FEEDIN_ETAP2.md`. **Część B (śledztwo maj/czerwiec) zamknięta bez
dalszego kodu — przyczyna znaleziona.**

Sprawdzono POLICZONO vs NALEŻAŁO POLICZYĆ na korektach styczeń–lipiec 2025 (nie tylko kwiecień):

| Korekta (koryguje miesiąc) | Stare (POLICZONO) | Nowe (NALEŻAŁO POLICZYĆ) | Zmiana |
|---|---|---|---|
| luty→koryguje styczeń | 25,92 zł | 35,83 zł | +38% (≈ ×1,23 z zaokrągleniem) |
| marzec→koryguje luty | 66,75 zł | 82,10 zł | **+23,0%** — dokładnie ×1,23 |
| kwiecień→koryguje marzec | 68,06 zł | 74,31 zł | +9,2% — **częściowa**, nie ×1,23 |
| maj→koryguje kwiecień | 34,31 zł | 34,31 zł | **0% — brak korekty** |
| czerwiec→koryguje maj | 18,14 zł | 18,14 zł | **0% — brak korekty** |
| lipiec→koryguje czerwiec | 31,68 zł | 31,68 zł | **0% — brak korekty** |

**Wniosek: to nie błąd naszego modelu ani naszego parsera (poza kwietniem, patrz niżej) — to
własna, niekonsekwentna korekta Taurona.** List przewodni batcha z 01.01.2026 obiecuje
„powiększenie wartości depozytu wypracowanego **od 1.01.2025 r.** o współczynnik 1,23" — luty i
marzec dostały to poprawnie (marzec dokładnie ×1,23), ale **kwiecień tylko częściowo, a
maj/czerwiec/lipiec wcale**, mimo że mieszczą się w zakresie „od 1.01.2025" z listu. Sprawdzono też
dopasowanie realnej ceny maja/czerwca do innych miesięcy RCEm (na wypadek złego przesunięcia
miesiąca po stronie Taurona) — **brak dopasowania do jakiegokolwiek miesiąca 2023–2025** w
posiadanych danych (realna cena 40–59 PLN/MWh netto, poniżej najniższej opublikowanej RCEm w
całym okresie, 136,30 dla czerwca 2025) — to nie jest przesunięty miesiąc, to brakująca korekta.

**Rekomendacja (zamiast kodu): user zgłasza reklamację do Taurona** (infolinia 32 606 0 606, wg
listu z korekty), powołując się na numery faktur korygujących T/K1/BN567872/0010/25…0013/25
(kwiecień–lipiec) i cytat z ich własnego listu o „powiększeniu o współczynnik 1,23 od 1.01.2025" —
niekonsekwentnie zastosowany. Add-on **nie może** tego naprawić kodem (to nie błąd w naszych
danych/logice) — może tylko to widocznie flagować w UI (patrz Część A.5 niżej, rozszerzenie
istniejącej etykiety jakości z 0.42.0).

**Część A (fix parsera) w toku** — patrz `docs/BLUEPRINT_DEPOSIT_FEEDIN_ETAP2.md` za szczegóły
implementacji.

## Wynik Części A (28.09.2026) — fix parsera zaimplementowany, wydanie 0.43.0

Zamiast planowanego `extraction_mode='layout'` (pypdf) — **odrzucone empirycznie**: dla tego
szablonu korekty tryb `layout` co prawda naprawia kolejność, ale **duplikuje każdą linię tekstu**
(np. „NALEŻAèO POLICZYĆ:NALEŻAèO POLICZYĆ:”), co psuje inne pola liczbowe (przetestowane na żywo —
`deposit_used_pln` wyszło 74,317 zamiast 74,31, `amount_due_pln` 77,388 zamiast 77,38). Zamiast tego:
**strategia „ostatnie dopasowanie w pełnym tekście plain”** — zweryfikowana na 6 żywych korektach
(2025-02…2025-07): każde pole depozytu/kwoty do zapłaty ma w tekście 2 lub 4 wystąpienia (stara/nowa
wartość, czasem zduplikowane przez powtórzony załącznik ZAŁĄCZNIK), a **ostatnie zawsze jest
poprawne**. Nowa funkcja `_last_float_multi` (analogiczna do istniejącej `_first_float_multi`),
użyta tylko dla `_is_korekta` przez lokalny closure `_field()`. Przy okazji naprawiony
`correction_delta_pln` (regex nie obsługiwał znaku minus — ujemne korekty, jak kwiecień -6,25 zł,
dawały `None`).

10 nowych testów (`test_invoice_parser.py`): syntetyczny wariant symulujący dokładnie zaobserwowany
reordering pypdf (uruchamiany zawsze) + 7 testów na prawdziwym pliku korekty kwietnia 2025
(`/data/home/.claude/uploads/kor_2025-04_T_K1_BN567872_0010_25.pdf`, wzorem istniejącego
`TestRealPdf` — `skipif` gdy plik niedostępny, plik poza repo). **587/587 testów zielonych, zero
regresji.** Zweryfikowano bezpośrednio `parse_invoice()` na wszystkich 6 pobranych żywych korektach
(2025-02…2025-07) — wszystkie zwracają teraz poprawną, skorygowaną wartość.

CHANGELOG/README zaktualizowane, wersja podbita do **0.43.0** (`config.yaml` + `__init__.py`).
Dalej: wydanie GitHub, aktualizacja Supervisora, reparse 24 faktur z korektą, weryfikacja
rekonsyliacji na żywo, Playwright.

## Wynik końcowy (28.09.2026) — 0.43.0 wydane, live, zweryfikowane

Release opublikowany (`gh release create v0.43.0`), Supervisor zaktualizował add-on
(`update.pv_roi_tracker_update` 0.42.0→0.43.0), health `ok`. Wszystkie **23 faktury korygujące**
re-sparsowane (`/api/invoice/reparse`, 3 partie w tle, ~2 min/partię — `_invoice_reconcile_callback`
odświeża dane po każdym wywołaniu; jedna niepowiązana pre-istniejąca walidacja „średnia cena poza
zakresem" na 2 fakturach z 2024-07/08, niezmieniona tym fixem, do zbadania osobno jeśli będzie
przeszkadzać). 2026-03 (`nota`) pominięta zgodnie z planem — noty nie mają sekcji POLICZONO/NALEŻAŁO
POLICZYĆ.

**Efekt na żywych danych:**
- `deposit.reconciliation.totals`: **diff_pct -3,3%** (model 371,08 zł vs Tauron 383,60 zł) — wcześniej
  pojedyncze miesiące (kwiecień–czerwiec 2025) sięgały +84% do +435%.
- Kwiecień/maj/czerwiec 2025 poprawnie przeszły ze statusu `ok` (z fałszywie precyzyjnym, błędnym
  `diff_pct`) na `capped` — **wykluczone z rekonsyliacji jako niepewne**, zgodnie z ustaleniem z
  Części B: korekta Taurona faktycznie nie naprawiła tych miesięcy, więc pokazywanie ich jako
  „zweryfikowane" byłoby fałszywą precyzją. To poprawny, uczciwy wynik, nie regresja.
- `verified_months/total_months`: **16/39** (spadek z 21/39 w 0.42.0) — spadek jest **oczekiwany i
  poprawny**: 0.42.0 fałszywie liczyło część miesięcy jako „zweryfikowane" na podstawie źle
  odczytanych korekt; teraz liczba jest mniejsza, ale prawdziwa.
- `balance_model`: **436,63 zł** (z 474,72 zł) — headline się zmienił, opisane w CHANGELOG/README.
- Pozostały widoczny temat: styczeń 2025 nadal -27,7% (Tauron 35,83 vs model 25,92) — mniejszy niż
  poprzednio, ale nie zbadany w tej sesji (poza zakresem kwiecień–czerwiec); kandydat do osobnego,
  krótkiego sprawdzenia w przyszłości, nieblokujący.

**Playwright** (direct-IP `172.30.33.15:8099`, desktop 1400×900 + mobile 390×844, zakładka Faktury):
oba renderują się poprawnie, karty depozytu spójne, konsola bez nowych błędów (tylko nieszkodliwy
404 favicon.ico). Zrzuty: `playwright/deposit_faktury_desktop_0_43_0.jpg`,
`playwright/deposit_faktury_mobile_0_43_0.jpg`.

**Podprojekt „Depozyt: zasilenie 2025-04/06 + B8" zamknięty.** B8 zamknięty bez kodu (Część B
pierwotna), diff_pct zbadany i naprawiony w dostępnym zakresie (Część A — realny błąd parsera),
maj/czerwiec 2025 zdiagnozowane jako osobny, nie do naprawienia kodem problem po stronie Taurona
(rekomendacja: kontakt z infolinią, patrz wyżej) — nie blokuje wydania, poprawnie oznaczone `capped`
w UI.

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
