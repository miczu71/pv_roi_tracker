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

## Weryfikacja
- Etap 0: `git log origin/main` pokazuje commit.
- Etap 1: tabela lista eBOK vs `invoice_store`; PDF z eBOK parsuje się identycznie jak istniejący rekord.
- Etap 2: `pytest` zielony, sync na żywo dodaje tylko brakujące, rekonsyliacja bez regresji, Playwright.
- Etap 3: ręczne wywołanie joba, push testowy przy symulowanym błędzie, health `ok`.
