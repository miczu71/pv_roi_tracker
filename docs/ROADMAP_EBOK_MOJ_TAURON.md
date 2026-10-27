# PV ROI Tracker — „eBOK po migracji do Mój TAURON”

## Context

Tauron przebudował eBOK na portal „Mój TAURON”. Stary mechanizm add-onu przestał działać:
`/wyborKlienta/id/<id>` nie zwraca listy klientów (tylko stronę główną), a eksport
`/content/platnosci/csv/.../type/archiwumFaktur` zwraca HTML strony głównej (status 200, ~72 KB)
zamiast CSV. Od 26.10.2026 oba joby eBOK (import faktur — pon. 07:45, sprawdzanie płatności — 07:00)
kończą się błędem. Logowanie działa.

Zależą od tego zamknięte podprojekty [`ROADMAP_EBOK_IMPORT.md`](ROADMAP_EBOK_IMPORT.md) i
[`ROADMAP_EBOK_PAYMENT_STATUS.md`](ROADMAP_EBOK_PAYMENT_STATUS.md).

Ustalenia z `GET api/ebok/diag-page` (0.49.3, 27.10.2026):
- strona główna zawiera sekcję „Moje umowy” z taryfą (`<dt>Taryfa</dt><dd>G12W</dd>`) i typem faktury;
- umowy wybiera się parametrami w linkach: `/Home/Pages/Faktury?sid=T18&pp=<płatnik>`
  i `/Home/Pages/Umowa?sid=T18&pp=<płatnik>`;
- menu ma też „Zmiana grupy taryfowej” i „Umowa i dane / Twoje umowy”.

Cele: (1) przywrócić import faktur i status płatności, (2) umieć odczytać aktualną taryfę.

## Status

- 0.49.1–0.49.3 wydane: tylko diagnostyka (`select_payer`, `probe_home`, `GET api/ebok/diag-page`
  z `payer_html` i `csv_probe`). 0.49.3 zainstalowane na żywo 27.10.2026 11:18.
- Tag/release `v0.49.2` powstał przy niepodbitym `config.yaml` — zostawiony, 0.49.3 go zastępuje.
  Decyzja o ewentualnym usunięciu: user.

## Etapy

### E0 — sonda strony Faktury (0.49.4)
Pomijamy, jeśli user poda z DevTools (Network) adres eksportu faktur z `/Home/Pages/Faktury`.
- `diag-page` pobiera `/Home/Pages/Faktury?sid=T18&pp=<płatnik>` i zwraca (maskowane, bez
  płatnika/adresu/e-maila): linki eksportu/pobierania, nagłówki tabeli, 3 wiersze, status i rozmiar.
- Tylko odczyt. Rollback: poprzednia wersja add-onu z backupu.
- DONE: wynik `diag-page` pokazuje, skąd pobrać listę faktur (adres + format).

### E1 — naprawa wyboru płatnika i listy faktur
Ryzyko (dotyka importu do `invoice_store`) → `sonnet-worker`, TDD, fixture neutralny.
- `select_payer` i `list_documents` na nowych adresach/formacie wg E0.
- Weryfikacja na żywo: `POST api/ebok/sync` → ok, job payment check 07:00 kończy się `ok`,
  brak powiadomień o błędzie. Osobne wydanie od razu (funkcja zepsuta na produkcji).
- Rollback: przywrócenie 0.49.3 z backupu add-onu.

### E2 — odczyt taryfy
Zakres do uzgodnienia z userem: pole w statusie eBOK i/lub powiadomienie `notify.kacper` przy
zmianie taryfy. Parsowanie `Taryfa` ze strony głównej (fixture neutralny). Wydanie łączone z E1
albo osobne wg uzgodnienia.

## Do sprawdzenia ręcznie (po zalogowaniu na logowanie.tauron.pl)
- `https://ebok.tauron.pl/wyborKlienta` — strona główna „Moje umowy”
- `https://ebok.tauron.pl/Home/Pages/Faktury?sid=T18&pp=<płatnik>` — faktury; czy jest przycisk eksportu i dokąd prowadzi
- `https://ebok.tauron.pl/Home/Pages/Umowa?sid=T18&pp=<płatnik>` — umowa i dane

## Testy
`cd /config/addons/pv_roi_tracker/pv_roi_tracker && PATH=$HOME/.local/bin:$PATH uv run --no-project --with pytest --with-requirements requirements.txt python -m pytest -q`
(stan: 973 passed, 27 skipped).

## Handoff
[`~/docs/HANDOFF_ebok_tariff_2026-10-27.md`](../../../../../docs/HANDOFF_ebok_tariff_2026-10-27.md) (poza repo).
