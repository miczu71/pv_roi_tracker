# PLAN E2 — wykrywanie taryfy z „Mój TAURON”, historia `tariff_periods`, encja `current_tariff`

Źródło: `docs/ROADMAP_EBOK_MOJ_TAURON.md` (sekcje „ZMIANA ZAKRESU”, E2). Wartość etapu sam z siebie: add-on wie, jaką
taryfę ma user, zapisuje historię zmian, wysyła push przy zmianie i publikuje bieżącą taryfę do HA. Nie zmienia
jeszcze żadnych porównań (to E3) ani sterowania (E6).

## Decyzje (domyślne, bez pytania)
- Data początku okresu = dzień pierwszego wykrycia nowej taryfy przez job 07:00. Portal nie pokazuje daty zmiany;
  dokładną datę user może ustawić ręcznie przez `tariff_periods.json` (E3 doda edycję w UI). Pierwsze wykrycie
  (brak historii) zapisuje okres z `from: null` (= „od zawsze”).
- Override awaryjny: opcja add-onu `current_tariff_override` (pusta = z eBOK). Gdy ustawiona, wygrywa nad wykrytą.
- Nazwy taryf normalizowane do katalogu (`tariff_catalog.TARIFFS`: G11, G12, G12w, G13, G13s); nieznana nazwa →
  `None` (nie zmieniamy historii, `logger.warning`).
- Błąd odczytu umowy nie psuje joba płatności (`ebok_payment` zostaje ok).

## Wykonawca (paczka jedna, `haiku-worker`; pliki rozłączne z innymi pracami)
Pliki: `pv_roi_tracker/ebok_client.py`, `pv_roi_tracker/tariff_history.py` (nowy), `pv_roi_tracker/main.py`,
`pv_roi_tracker/publisher.py`, `config.yaml` (tylko opcja + schema), `tests/test_ebok_client.py`,
`tests/test_tariff_history.py` (nowy), `tests/test_publisher.py`, `tests/fixtures/ebok_home.html`,
`tests/fixtures/ebok_punkt.html` (nowe). TDD: najpierw czerwone testy.

1. `ebok_client.py`:
   - `parse_home_contract(html) -> dict` (czysta): z `<dl class="dl-horizontal">` pary `dt`→`dd`, zwraca
     `{'umowa': .., 'taryfa': .., 'faktura': ..}` (klucze małymi literami, brak pola = brak klucza). Regex/`html.parser`,
     bez nowych zależności (sprawdź `requirements.txt`).
   - `parse_point_details(html) -> dict` (czysta): `Taryfa`, `Moc umowna`, `Sposób rozliczenia`, `Nazwa OSD` →
     `{'taryfa', 'moc_umowna', 'rozliczenie', 'osd'}`. Bez numeru licznika i PPE.
   - `EbokClient.read_contract() -> dict`: GET `SERVICE + '/'` → `parse_home_contract`; potem (w try, błąd tylko
     `logger.warning`) GET `/Home/Pages/Umowa?sid=T18&pp=<self._payer_id>` → link `punkty-poboru` →
     GET → `parse_point_details`; wynik = strona główna uzupełniona szczegółami (szczegóły mają pierwszeństwo dla
     `taryfa`). Brak `taryfa` w obu → `EbokLayoutChanged('Nie znaleziono taryfy na stronie umowy')`.
2. `tariff_history.py` (nowy, czyste funkcje + prosty zapis atomowy przez `battery_store._atomic_write`):
   - `normalize(name) -> Optional[str]` (case-insensitive do `tariff_catalog.TARIFFS`).
   - `load(path) -> list[dict]` / `save(path, periods)`; plik `/data/tariff_periods.json`, `[{"tariff": "G12w", "from": null|"YYYY-MM-DD"}]`.
   - `current(periods, override=None) -> Optional[str]`: override (znormalizowany) wygrywa, inaczej ostatni okres.
   - `tariff_on(periods, day) -> Optional[str]`: okres obowiązujący w dniu (`from` null = od zawsze).
   - `record_detection(periods, detected, today) -> tuple[list, Optional[tuple]]`: gdy `detected` None → bez zmian;
     pusta historia → `[{tariff, from: None}]`, brak „zmiany”; inna niż ostatnia → dopisuje `{tariff, from: today.isoformat()}`
     i zwraca `(old, new)`; ta sama → bez zmian.
3. `main.py`:
   - stała `TARIFF_PERIODS_PATH = Path(os.environ.get('TARIFF_PERIODS_PATH', '/data/tariff_periods.json'))`,
     odczyt opcji `current_tariff_override` jak sąsiednie opcje (sprawdź jak czytane są `ebok_*` — env z `run.sh`;
     dopisz do `run.sh` tylko jeśli tak to działa).
   - `ebok_payment_check.body`: po `apply_refresh` blok `try: contract = client.read_contract()`; `except Exception`
     → `logger.exception`, bez zmiany wyniku joba. Zapisz `state['contract'] = {**contract, 'checked_at': iso}`
     (`_save_ebok_state`), `record_detection`, `save`, a przy zmianie `_notify_ha('PV ROI Tracker — taryfa',
     f'Wykryto zmianę taryfy: {old} → {new}. Porównania w add-onie przejdą na nową bazę.', target='kacper')`.
     Treść pusha nie zawiera danych osobowych.
   - `_ebok_status()` dodaje `contract` (ze `state`), `current_tariff` (`tariff_history.current(...)`), `tariff_periods`.
   - `publish_roi(..., current_tariff=...)` w wywołaniu pollera (`current_tariff` liczone z historii + override).
4. `publisher.py`: czujnik `current_tariff` (`_Sensor('current_tariff', 'PV Current Tariff', None, None, None, None,
   'mdi:flash')`, wartość tekstowa), nowy parametr `current_tariff: Optional[str] = None` w `publish_roi` i
   `_render_value`; `None` → `'unknown'`... (zgodnie z tym, jak publisher renderuje brak wartości dla czujników tekstowych).
   Test w `tests/test_publisher.py`.
5. `config.yaml`: `options.current_tariff_override: ""` i `schema.current_tariff_override: list(|G11|G12|G12w|G13|G13s)?`
   (bez bumpu wersji).
6. Testy: parsery na neutralnych fixture'ach (umowa „UMOWA-TEST-1”, taryfa „G12W”, moc „5 kW”, bez PPE/licznika/adresu);
   `tariff_history` (pusta historia, ta sama, zmiana G12w→G13, override, `tariff_on` przed/po zmianie, nieznana nazwa);
   `read_contract` na fake session (błąd szczegółów punktu nie psuje wyniku; brak taryfy → `EbokLayoutChanged`);
   publisher renderuje `current_tariff`.
- `DONE:` pełny pytest zielony (baza 976 passed, 23 skipped + nowe); `grep -rn "11111111" tests/fixtures/ebok_home.html`
  nie jest wymagane, ale w fixture'ach nie ma e-maila, PESEL, PPE, numeru licznika ani prawdziwego adresu.
- `NIE RÓB:` zmian w `tariff_job.py`, `tariff_sim.py`, `battery_sim.py`, `web.py` (`/api/ebok/status` już zwraca wynik `_ebok_status`), `static/*`, `payment_reminders.py`, wersji (`config.yaml: version`, `__init__.py`),
  `CHANGELOG.md`, commitów; logowania do Taurona.

## Weryfikacja i wydanie
Wydanie łączone z E3 (E2 sam nic nie zmienia dla usera poza pushem i czujnikiem). Po etapie: pytest, `verifier`,
commit na dev + mirror; na żywo dopiero przy wydaniu.

## Rollback
`git revert` commitów E2. `tariff_periods.json` i `state['contract']` są ignorowane przez starszy kod; czujnik
`current_tariff` w HA zostaje jako `unavailable` (można usunąć przez `ha_remove_entity`).
