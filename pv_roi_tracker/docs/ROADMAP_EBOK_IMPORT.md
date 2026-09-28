# PV ROI Tracker — „Auto-import faktur z eBOK TAURON"

## Context

Podprojekty YoY (0.38.0), Pompa ciepła (0.41.0), Dług depozytowy (0.42.0) i Zasilenie depozytu (0.43.0)
są zamknięte. Z backlogu (`ROADMAP_HEATPUMP.md`) user wybrał auto-import faktur — **nie z IMAP, tylko
z eBOK TAURON** (`https://ebok.tauron.pl/`, płatnik **60567872**), tym samym kontem co
`tauron_amiplus.username` / `tauron_amiplus.password` w `/config/secrets.yaml`.

Wywiad (28.09.2026):
- **Cel: wszystko** — (a) zero ręcznej roboty: nowa faktura/korekta/nota sama trafia do add-onu
  w ~dobę od wystawienia, (b) jednorazowe uzupełnienie historii, (c) rozliczenie/depozyt aktualne
  szybciej po zamknięciu miesiąca.
- **Polityka: auto + push przy problemie** — pobrany dokument przechodzi tę samą ścieżkę co ręczny
  upload (`parse_invoice` → `invoice_store.upsert` → callback rekonsyliacji); push `notify.kacper`
  tylko przy ostrzeżeniach parsera/`needs_training` lub awarii logowania/pobierania.

Ustalenia techniczne:
- eBOK przekierowuje na Keycloak `logowanie.tauron.pl/realms/ext/protocol/cas/login?service=https://ebok.tauron.pl`
  — zwykły formularz `kc-form-login` (`username`, `password`, `credentialId`), brak captchy w HTML.
- Integracja `PiotrMachowski/...Tauron-AMIplus` (`connector.py:login_service`) potwierdza działający
  flow: GET formularza → regex `action` → POST → follow redirects; wykrywa złe hasło (formularz dalej
  obecny) i blokadę „Przekroczono maksymalną liczbę logowań.”; reużywa sesji (cookies). **Faktur nie
  pobiera** (`get_moj_tauron` zakomentowane) — służy tylko jako wzorzec logowania, nie instalujemy jej.
- Add-on ma dziś tylko `map: share:rw` — credentiale wejdą jako opcje add-onu z `!secret`.

Wybrane podejście: **`requests` w add-onie** (bez headless Chromium, bez zewnętrznego skryptu).

## Etapy (każdy kończy się checkpointem; następny tylko za „go")

### Etap 0 — dokument
Ten plan → `pv_roi_tracker/docs/ROADMAP_EBOK_IMPORT.md`; w `ROADMAP_HEATPUMP.md` backlog: pozycja
„Auto-import faktur z maila (IMAP)" → „wybrany 28.09.2026, źródło eBOK zamiast IMAP, patrz
ROADMAP_EBOK_IMPORT.md". Commit + push w `/config/addons/pv_roi_tracker` (`main`).

### Etap 1 — spike (tylko odczyt, zero zmian w add-onie, zero wydania) — „go" udzielone 28.09
Skrypt w `$CLAUDE_JOB_DIR/tmp`, credentiale czytane z `/config/secrets.yaml` (nigdy do logu/outputu):
1. **Jedno** logowanie Keycloak → eBOK (wzorem `login_service`); zapis cookies do pliku w tmp,
   kolejne kroki reużywają sesji (bez ponownych logowań — ryzyko blokady).
2. Zmapować po zalogowaniu: strona startowa/SPA, endpointy (XHR/JSON lub HTML) listy dokumentów
   (faktury, korekty, noty) dla płatnika 60567872, paginacja/zakres dat, identyfikatory dokumentów.
3. Pobrać **jeden** PDF (najnowsza faktura) i przepuścić przez `invoice_parser.parse_invoice`
   z kodu add-onu — czy wynik = rekord już w `invoice_store` (klucz, kwoty, depozyt).
4. Porównać pełną listę eBOK z kluczami w add-onie (`/api/data` direct-IP `172.30.33.15:8099`):
   czego brakuje, jak głęboko sięga historia, czy są korekty/noty, jak zdeduplikować
   (numer dokumentu vs klucz `_make_key`).
5. Zapisać zredagowane (bez danych osobowych/tokenów) próbki odpowiedzi jako materiał na fixture'y.

**GO na Etap 2:** lista i PDF osiągalne bez OTP/JS-only. **NO-GO:** raport + powrót do wyboru
podejścia (Playwright / skrypt zewnętrzny). **Checkpoint:** wynik dopisany do ROADMAP, commit.

### Etap 2 — `ebok_client.py` + uzupełnienie historii (0.44.0)
- Nowy moduł `pv_roi_tracker/ebok_client.py` (login + sesja w `/data/ebok_session.json`, lista
  dokumentów, pobranie PDF; czyste funkcje parsujące odpowiedzi osobno od I/O).
- Opcje `ebok_username`, `ebok_password` (`password`), `ebok_payer_id` — opcjonalne; user ustawia
  `!secret tauron_amiplus.username/password`.
- `POST /api/ebok/sync` + przycisk w zakładce Faktury „Pobierz brakujące z eBOK": pobiera tylko
  dokumenty, których nie ma (dedup po numerze dokumentu), reużywa ścieżki uploadu
  (`web.invoice_upload` → wydzielić wspólną funkcję ingest).
- Blokada logowań → wstrzymanie prób na 24 h, stan w health.
- Testy `tests/test_ebok_client.py` na fixture'ach z Etapu 1; CHANGELOG/README, release wg
  `feedback_pv_roi_release_checklist`, cache-busting, Playwright direct-IP (desktop + 390 px, konsola).

### Etap 3 — stały auto-import (0.45.0)
- Job APScheduler: codziennie w dniach 1–15 miesiąca, poza tym raz w tygodniu (korekty/noty).
- Push `notify.kacper` przez HA API przy: ostrzeżeniach parsera/`needs_training`, błędzie logowania,
  blokadzie, zmianie formatu (brak oczekiwanych pól). Bez pushu przy sukcesie.
- Health: `ebok` job + „ostatnia synchronizacja" w UI.

## Ryzyka
- Blokada logowań Taurona — minimalizacja logowań (reużycie sesji), stop na 24 h.
- Zmiana frontendu eBOK → import się psuje; push o awarii, ręczny upload dalej działa.
- Duplikaty przy innym kluczu niż ręczny upload — dedup po numerze dokumentu.
- Credentiale: tylko opcje add-onu (`password` w schemie), nigdy w logach.

## Wynik Etapu 1 (28.09.2026) — spike zakończony, GO

Skrypt w scratchpadzie (`requests`, credentiale z `secrets.yaml` czytane w procesie, nigdzie nie
drukowane), 5 logowań w trakcie sesji spike'a — **żadnej blokady** („Przekroczono maksymalną liczbę
logowań" nie wystąpiło), brak captchy/OTP na żadnym etapie.

**Flow logowania potwierdzony** dokładnie jak w `connector.py` (`login_service`): GET
`logowanie.tauron.pl/realms/ext/protocol/cas/login?service=https://ebok.tauron.pl` → regex `action`
z `kc-form-login` → POST `username`/`password`/`credentialId=""` → redirect do `ebok.tauron.pl/wyborKlienta`.

**Wybór klienta:** strona `/wyborKlienta` listuje dwa punkty — płatnik **60567872** (umowa aktywna,
opis „DOM") = client id **9810070**; drugi (42591637, umowa nieaktywna) = 8456841. Wybór:
`GET /wyborKlienta/id/9810070`.

**Lista dokumentów — dwa użyteczne źródła:**
1. `GET /content/platnosci` — ostatnie ~8 dokumentów z linkami `/podgladFaktury/id/<numeric_id>`.
2. `GET /content/platnosci/csv/dataOd/<YYYY-MM-DD>/dataDo/<YYYY-MM-DD>/type/archiwumFaktur` —
   **CSV z pełną historią w jednym żądaniu, bez paginacji** (76 wierszy dla `dataOd=2022-01-01`,
   sięga do 2023-08-29 — początek konta). Kodowanie **windows-1250**. Kolumny: `SYGNATURA` (numer
   dokumentu), `NAZWA DOKUMENTU` (Faktura rzeczywista/korygująca/rozliczeniowa/prognozowa, Nota
   uznaniowa, Korekta noty obciążeniowej), `DATA WYSTAWIENIA`, `KWOTA BRUTTO`, `TERMIN PŁATNOŚCI`,
   `ZAPŁACONA`. **Brak numerycznego id** potrzebnego do pobrania PDF.
3. `GET /content/platnosci/archiwumFaktur/display/50/dataOd/.../dataDo/...` — HTML, **max 50/stronę**
   (paginacja wymagana dla >50 dokumentów), mapuje `SYGNATURA` → numeryczne `id` (do PDF). Domyślny
   filtr dat bez parametrów to tylko ostatnie ~6 mies. — trzeba zawsze podawać `dataOd`/`dataDo`.
   Strony z pełną historią czasem odpowiadają wolno (>20s) — potrzebny timeout ~45-60s.

**PDF:** `GET /podgladFaktury/id/<numeric_id>` zwraca **bezpośrednio bajty PDF**
(`Content-Type: application/octet-stream`, `Content-Disposition: attachment; filename=...pdf`,
magic bytes `%PDF-1.4`) — bez dodatkowego kroku/formularza.

**Zgodność z parserem add-onu potwierdzona na żywo:** pobrany PDF faktury 08.09.2026
(`T/K1/BN567872/0017/26`, id 557539539) przepuszczony przez `invoice_parser.parse_invoice()` z kodu
add-onu — **zero `warnings`**, wynik identyczny co do grosza z rekordem już ręcznie wgranym w
`invoice_store` (`amount_due_pln=63.3`, `deposit_used_pln=57.57`, `deposit_previous_pln=57.57`,
`tariff=G12W`, `doc_type=rozliczeniowa`).

**Otwarty temat na Etap 2 (nie blokujący GO):** CSV nie daje numerycznego id, więc dla dokumentów
spoza ostatnich 50 trzeba przejść po stronach HTML archiwum i dopasować po `SYGNATURA` — do
zaimplementowania jako paginacja w `ebok_client.py` (sprawdzić w Etapie 2, czy istnieje
endpoint wyszukiwania po numerze dokumentu, żeby tego uniknąć).

**GO na Etap 2.** Ryzyko sesyjnego cache'u z Etapu 1 (spłaszczenie `session.cookies` do dict psuje
scoping domen → pętla przekierowań) odnotowane — `ebok_client.py` ma trzymać pełny `requests.Session`
(pickle) albo `cookiejar`, nie płaski dict.

## Wynik końcowy (28.09.2026) — 0.44.0 wydane, live, pierwsza realna synchronizacja OK

Release opublikowany, Supervisor zaktualizował add-on (0.43.0→0.44.0), health `ok`. User wpisał
`ebok_username`/`ebok_password` w Konfiguracji add-onu i zrestartował. Playwright (desktop+mobile)
potwierdził: przycisk „Pobierz brakujące z eBOK" poprawnie **ukryty** przy `configured: false` (przed
restartem), zero nowych błędów konsoli.

**Pierwsza realna synchronizacja (po restarcie) — pełny łańcuch zadziałał end-to-end:**
1. Logowanie do eBOK: `eBOK: zalogowano`.
2. CSV pełnej historii pobrany, porównany z `invoices.json` — znalezione 2 brakujące miesiące
   (2026-03, 2026-05) + jedna nota uznaniowa.
3. PDF-y pobrane przez `/podgladFaktury/id/<id>`, sparsowane, zreconciled — **2026-03 i 2026-05
   zaimportowane** przez tę samą ścieżkę co ręczny upload.
4. Nota uznaniowa (`K1N0474969`) — parser jej nie rozpoznał (`Imported kWh not found`, oczekiwane:
   noty nie mają pól energii/kWh) → poprawnie zapisana jako stub `needs_training`, nie zgubiona.
5. **Push na `notify.kacper` wysłany** dokładnie w tej jednej sytuacji (problem) — potwierdzone w
   logu add-onu: `HA notification sent (kacper): eBOK — zaimportowano 2 nowych dokumentów, ale:
   wymaga treningu: ebok_K1N0474969.pdf`.

**Otwarty, nieblokujący temat:** parser nie ma dedykowanego wzorca dla „Nota uznaniowa” (inny typ niż
już obsługiwana „NOTA OBCIĄŻENIOWA” w `_parse_nota`) — do treningu ręcznego przez UI, albo do rozszerzenia
`invoice_parser.py` jeśli trening nie wystarczy. Nie blokuje działania auto-importu.

**Podprojekt „Auto-import faktur z eBOK TAURON” zamknięty.** Cel z wywiadu (zero ręcznej roboty +
uzupełnienie historii + szybsze dane po zamknięciu miesiąca) osiągnięty i zweryfikowany na żywych
danych, nie tylko testach.

## Weryfikacja
- Etap 0: `git log origin/main` pokazuje commit.
- Etap 1: tabela lista eBOK vs `invoice_store`; PDF z eBOK parsuje się identycznie jak istniejący rekord.
- Etap 2: `pytest` zielony, sync na żywo dodaje tylko brakujące, rekonsyliacja bez regresji, Playwright.
- Etap 3: ręczne wywołanie joba, push testowy przy symulowanym błędzie, health `ok`.
