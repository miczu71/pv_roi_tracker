"""
Add-on entry point — steady-state loop.

Jobs:
  • Poll every POLL_INTERVAL_MINUTES: read historic + live → ROI calc → MQTT publish
  • Month-close: 23:55 on last day of each month
  • RCEm scrape: days 11–20 at 08:00, 10:00, 12:00, 14:00, 16:00, 18:00, 20:00, 22:00 local time
    Skips if previous month price is already known; stops retrying once found.
  • RCEm correction scan: 1st of each month at 06:00 — catches corrections that appear
    after the 20th-day retry window has closed
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

logging.basicConfig(
    level=os.environ.get('LOG_LEVEL', 'info').upper(),
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)

# ── Config from env (set by run.sh via bashio) ────────────────────────────────

HISTORIC_PATH          = Path(os.environ.get('HISTORIC_PATH', '/data/historic.json'))
RCEM_HISTORY_PATH      = Path(os.environ.get('RCEM_HISTORY_PATH', '/data/rcem_history.json'))
RCEM_CORRECTIONS_PATH  = Path(os.environ.get('RCEM_CORRECTIONS_PATH', '/data/rcem_corrections.json'))
INVOICE_PATH           = Path(os.environ.get('INVOICE_PATH', '/data/invoices.json'))
INVOICE_LAYOUTS_PATH   = Path(os.environ.get('INVOICE_LAYOUTS_PATH', '/data/invoice_layouts.json'))
BACKUP_SHARE           = Path(os.environ.get('BACKUP_SHARE', '/share/pv_roi_tracker'))
RCE_HOURLY_CACHE_PATH  = Path(os.environ.get('RCE_HOURLY_CACHE_PATH', '/data/rce_hourly.json'))
MONTHLY_NOTIFY         = os.environ.get('MONTHLY_NOTIFY', 'true').lower() in ('1', 'true', 'yes')
TARIFF_CONFIG_PATH     = Path(os.environ.get('TARIFF_CONFIG_PATH', '/data/tariff_config.json'))
BATTERY_CONFIG_PATH    = Path(os.environ.get('BATTERY_CONFIG_PATH', '/data/battery_config.json'))
BATTERY_HOURS_PATH     = Path(os.environ.get('BATTERY_HOURS_PATH', '/data/battery_sim.json'))
GROSS_INVESTMENT   = float(os.environ.get('GROSS_INVESTMENT', '51900.0'))
SUBSIDY            = float(os.environ.get('SUBSIDY', '28714.0'))
SYSTEM_KWP         = float(os.environ.get('SYSTEM_KWP', '6.72'))
POLL_INTERVAL      = int(os.environ.get('POLL_INTERVAL_MINUTES', '30'))
MQTT_HOST          = os.environ.get('MQTT_HOST', 'core-mosquitto')
MQTT_PORT          = int(os.environ.get('MQTT_PORT', '1883'))
MQTT_USER          = os.environ.get('MQTT_USER', '')
MQTT_PASSWORD      = os.environ.get('MQTT_PASSWORD', '')
DISCOUNT_RATE      = float(os.environ.get('DISCOUNT_RATE_REAL', '0.04'))
INFLATION_RATE     = float(os.environ.get('INFLATION_RATE', '0.05'))
COMPARISON_YIELD   = float(os.environ.get('COMPARISON_YIELD_RATE', '0.055'))
CO2_FACTOR         = float(os.environ.get('CO2_FACTOR_KG_KWH', '0.597'))
DEPOSIT_REFUND_PCT = float(os.environ.get('DEPOSIT_REFUND_PCT', '0.20'))
ASSET_LIFETIME_YEARS       = float(os.environ.get('ASSET_LIFETIME_YEARS', '25.0'))
PANEL_DEGRADATION_PCT_YEAR = float(os.environ.get('PANEL_DEGRADATION_PCT_YEAR', '0.5'))


# ── Pure helpers (module-level so they're unit-testable without booting main()) ──

def previous_month(today) -> tuple:
    """Poprzedni miesiąc kalendarzowy względem `today` (obsługuje zawijanie stycznia)."""
    return (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)


def month_present(records, year: int, month: int) -> bool:
    """Czy dany rok/miesiąc jest już w liście MonthlyRecord (np. z historic_store.load())."""
    return any(r.year == year and r.month == month for r in records)


def month_has_data(records, year: int, month: int) -> bool:
    """Czy dany rok/miesiąc ma realny odczyt produkcji — nie tylko pusty wiersz-placeholder.

    Różni się od month_present(): placeholder (np. z importu CSV pivota Google Sheets,
    który zasiewa po jednym wierszu na każdy miesiąc kalendarzowy roku, także przyszłe)
    przechodzi month_present, bo klucz (rok, miesiąc) istnieje — ale nie ma w nim danych.
    Właśnie to uśpiło startowy catch-up przy incydencie 2026-08-01: lipiec „istniał",
    więc _catch_up_missing_month_close nie odpalił backfillu ze statystyk HA.
    """
    from . import historic_store
    return any(r.year == year and r.month == month and historic_store.has_energy_data(r.produced_kwh)
               for r in records)


def _months_since_commissioning(records, today) -> list:
    """Wszystkie (rok, miesiąc) od najwcześniejszego miesiąca z danymi do
    poprzedniego miesiąca kalendarzowego (włącznie).

    Dawne healery (_catch_up_missing_month_close, month_close_verify_job)
    sprawdzały wyłącznie previous_month(today) — awaria trwająca ≥2 miesiące
    (dłuższy przestój add-onu) zostawiała starsze luki na zawsze niezauważone,
    bo nic nigdy nie patrzyło dalej niż jeden miesiąc wstecz. Ograniczone do
    zakresu z realnymi danymi — sprawdzenie kilku-kilkunastu lat miesięcy
    jest tanie przy każdym starcie/weryfikacji.
    """
    from . import historic_store
    dated = [(r.year, r.month) for r in records if historic_store.has_energy_data(r.produced_kwh)]
    if not dated:
        return []
    start_y, start_m = min(dated)
    end_y, end_m = previous_month(today)
    months = []
    y, m = start_y, start_m
    while (y, m) <= (end_y, end_m):
        months.append((y, m))
        m += 1
        if m == 13:
            m = 1
            y += 1
    return months


def _heal_month_if_needed(records_by_key: dict, year: int, month: int) -> Optional[str]:
    """Zwraca krótki powód naprawy (string) jeśli miesiąc (rok, month) jej
    wymaga — brak danych LUB nadmierna rozbieżność między dwoma niezależnymi
    śledzeniami produkcji (patrz balance.py) — albo None jeśli miesiąc jest
    w porządku.

    Cross-check to jedyny realny sygnał w tym kodzie: dawne healery
    sprawdzały tylko obecność danych (has_energy_data), więc miesiąc z
    prawdopodobnie błędnymi, ale niepustymi, wartościami przechodził bez
    naprawy. balance.py porównuje produced_kwh (rodzina Energy Dashboard) z
    cross_family_produced_kwh (rodzina inverter_total_yield) — normalny
    rozjazd to 0,6-6,5%; duży rozjazd bywa wart uwagi, choć nie dowodzi
    który sensor jest "zły".
    """
    from . import historic_store, balance
    rec = records_by_key.get((year, month))
    if rec is None or not historic_store.has_energy_data(rec.produced_kwh):
        return 'brak danych'
    b = balance.compute_balance(rec)
    if b['reason'] == 'breach':
        return f"niespójny bilans produkcji — rozjazd rodzin ({b['diff_kwh']} kWh, {b['diff_pct']}%)"
    return None


def _notify_ha(title: str, message: str) -> None:
    import requests as _req
    token = os.environ.get('SUPERVISOR_TOKEN', '')
    if not token:
        logger.debug('SUPERVISOR_TOKEN not set — skipping HA notification')
        return
    try:
        _req.post(
            'http://supervisor/core/api/services/notify/family',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json={'title': title, 'message': message},
            timeout=5,
        )
        logger.info('HA notification sent: %s', message)
    except Exception:
        logger.exception('HA notification failed')


# ── Rejestr zdrowia zadań ─────────────────────────────────────────────────────
# Każde zadanie tła raportuje wynik; agregat trafia do sensor.pv_roi_tracker_health.

_job_health: dict = {}


def _record_job(name: str, ok: bool, detail: str = '') -> None:
    from datetime import datetime as _dtt
    now = _dtt.now().isoformat(timespec='seconds')
    entry = _job_health.setdefault(name, {})
    entry['ok'] = ok
    entry['last_run'] = now
    entry['detail'] = detail
    if ok:
        entry['last_ok'] = now


def _health_snapshot() -> tuple[str, dict]:
    """Zwraca (stan, atrybuty). error: pętla poll padła; degraded: zadanie pomocnicze padło."""
    from . import live_reader
    solcast = live_reader.solcast_available()
    attrs: dict = {name: dict(data) for name, data in _job_health.items()}
    attrs['solcast_available'] = solcast

    if not _job_health.get('poll', {}).get('ok', True):
        state = 'error'
    elif (any(not d.get('ok', True) for d in _job_health.values())
          or solcast is False):
        state = 'degraded'
    else:
        state = 'ok'
    attrs['issues'] = sorted(
        [n for n, d in _job_health.items() if not d.get('ok', True)]
        + (['solcast'] if solcast is False else [])
    )
    return state, attrs


def _backup_data() -> None:
    import shutil
    try:
        BACKUP_SHARE.mkdir(parents=True, exist_ok=True)
        for src in [HISTORIC_PATH, RCEM_HISTORY_PATH, RCEM_CORRECTIONS_PATH,
                    INVOICE_PATH, INVOICE_LAYOUTS_PATH, RCE_HOURLY_CACHE_PATH,
                    TARIFF_CONFIG_PATH, BATTERY_CONFIG_PATH, BATTERY_HOURS_PATH]:
            if src.exists():
                shutil.copy2(src, BACKUP_SHARE / src.name)
        logger.info('Data backed up to %s', BACKUP_SHARE)
        _record_job('backup', True)
    except Exception:
        logger.exception('Backup failed')
        _record_job('backup', False, 'backup do /share nie powiódł się')


def main() -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    from . import concat, historic_store, live_reader, rcem_scraper, roi, cpi_fetcher, balance
    from . import invoice_store, invoice_layouts, invoice_parser
    from .month_close import close_month
    from .publisher import MQTTPublisher
    from . import __version__

    from . import web as _web
    _web.start_server(port=8099)

    pub = MQTTPublisher(MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASSWORD, version=__version__)
    pub.connect()

    cpi_fetcher.bootstrap()

    def _rcem_override(month_str: str, price: float) -> None:
        history = rcem_scraper._load_history(RCEM_HISTORY_PATH)
        history[month_str] = price
        rcem_scraper._save_history(history, RCEM_HISTORY_PATH)
        year, mon = int(month_str[:4]), int(month_str[5:])
        historic_store.backfill_rcem(year, mon, price, HISTORIC_PATH)
        logger.info('RCEm override: %s = %.4f PLN/kWh', month_str, price)
        poll_and_publish()

    _web.set_rcem_override_callback(_rcem_override)

    def _historic_patch(year: int, month: int, field: str, value: float) -> bool:
        ok = historic_store.patch_month_field(year, month, field, value, HISTORIC_PATH)
        if ok:
            poll_and_publish()
        return ok

    _web.set_historic_patch_callback(_historic_patch)

    def _reread_month(year: int, month: int):
        """Backfill miesiąca ze statystyk HA, nadpisz historic.json, przelicz ROI."""
        rcem_price = rcem_scraper._load_history(RCEM_HISTORY_PATH).get(f'{year}-{month:02d}')
        peak_gross, offpeak_gross = _tariff_rates_for(date(year, month, 28))
        record = live_reader.read_month_from_statistics(
            year, month, rcem_price=rcem_price,
            peak_gross=peak_gross, offpeak_gross=offpeak_gross)
        if record is None:
            return None
        historic_store.replace_month(record, HISTORIC_PATH)
        poll_and_publish()
        return record

    _web.set_reread_month_callback(_reread_month)

    def _scan_and_heal_all_months() -> list:
        """Przeleć każdy miesiąc od pierwszego z danymi do poprzedniego
        miesiąca kalendarzowego (nie tylko previous_month(today) jak dawne
        healery) i napraw każdy z brakiem danych LUB niespójnym bilansem
        energii (_heal_month_if_needed / balance.py) przez ponowny odczyt z
        lifetime liczników HA. Zwraca listę napisów 'YYYY-MM' naprawionych
        miesięcy (pusta jeśli nic nie wymagało naprawy)."""
        records = historic_store.load(HISTORIC_PATH)
        records_by_key = {(r.year, r.month): r for r in records}
        healed = []
        for y, m in _months_since_commissioning(records, date.today()):
            reason = _heal_month_if_needed(records_by_key, y, m)
            if reason is None:
                continue
            logger.warning('%d-%02d wymaga naprawy (%s) — odtwarzam ze statystyk HA', y, m, reason)
            record = _reread_month(y, m)
            if record is None:
                logger.error('Nie udało się naprawić %d-%02d — brak danych LTS dla tego miesiąca', y, m)
            else:
                healed.append(f'{y}-{m:02d}')
        return healed

    def _reconciled_months() -> set:
        """(year, month) miesięcy rozliczonych fakturą — te pola wygrywają z
        LTS przy rebase (rachunek jest bardziej autorytatywny niż sensor)."""
        out = set()
        for rec in invoice_store.load(INVOICE_PATH).values():
            if rec.get('reconciled', False) and rec.get('doc_type', 'rozliczeniowa') == 'rozliczeniowa':
                y, m = rec.get('year'), rec.get('month')
                if y is not None and m is not None:
                    out.add((y, m))
        return out

    def _roi_kwargs() -> dict:
        return dict(gross_investment=GROSS_INVESTMENT, subsidy=SUBSIDY,
                   system_kwp=SYSTEM_KWP, discount_rate=DISCOUNT_RATE,
                   inflation=INFLATION_RATE, comparison_yield=COMPARISON_YIELD,
                   co2_factor=CO2_FACTOR, asset_lifetime_years=ASSET_LIFETIME_YEARS,
                   panel_degradation_pct_year=PANEL_DEGRADATION_PCT_YEAR)

    def _simulate_rebase() -> dict:
        from . import rebase as _rebase
        records = historic_store.load(HISTORIC_PATH)
        return _rebase.simulate(records, reconciled_months=_reconciled_months(),
                                roi_kwargs=_roi_kwargs())

    _web.set_simulate_rebase_callback(_simulate_rebase)

    def _apply_rebase() -> dict:
        from . import rebase as _rebase
        report = _rebase.apply(path=HISTORIC_PATH, reconciled_months=_reconciled_months(),
                               roi_kwargs=_roi_kwargs())
        poll_and_publish()
        return report

    _web.set_apply_rebase_callback(_apply_rebase)

    # Inject learned layouts into the parser at startup
    invoice_parser.set_layouts_provider(
        lambda fk: invoice_layouts.learned_for(fk, INVOICE_LAYOUTS_PATH)
    )

    def _invoice_reconcile(parsed_list: list, raw_texts: dict = None,
                           pdf_bytes_map: dict = None) -> None:
        for data in parsed_list:
            filename = getattr(data, '_filename', '')
            doc_type = getattr(data, 'doc_type', 'rozliczeniowa')
            raw_text = (raw_texts or {}).get(filename, '') if data.warnings else None
            pdf_bytes = (pdf_bytes_map or {}).get(filename)
            if doc_type in ('korekta', 'nota'):
                # Corrections/notas are stored but do NOT reconcile historic records.
                # deposit.calculate() sees the corrected values via effective_by_month().
                invoice_store.upsert(data, filename=filename, reconciled=False,
                                     raw_text=raw_text, pdf_bytes=pdf_bytes,
                                     path=INVOICE_PATH)
                logger.info('Correction %s %d-%02d stored (doc_type=%s)',
                            data.invoice_number or '', data.year, data.month, doc_type)
            else:
                snap = historic_store.snapshot_month(data.year, data.month, HISTORIC_PATH)
                reconciled = historic_store.reconcile_invoice(data, HISTORIC_PATH)
                invoice_store.upsert(data, filename=filename, reconciled=reconciled,
                                     pre_reconcile=snap, raw_text=raw_text, pdf_bytes=pdf_bytes,
                                     path=INVOICE_PATH)
                logger.info('Invoice %d-%02d ingested (reconciled=%s)',
                            data.year, data.month, reconciled)
        poll_and_publish()

    _web.set_invoice_reconcile_callback(_invoice_reconcile)

    def _invoice_remove(key: str) -> dict:
        """Remove an invoice and revert its historic.json snapshot if present."""
        record = invoice_store.remove(key, INVOICE_PATH)
        if record and record.get('pre_reconcile') and record.get('year') and record.get('month'):
            historic_store.restore_month(record['year'], record['month'],
                                         record['pre_reconcile'], HISTORIC_PATH)
        poll_and_publish()
        return record or {}

    _web.set_invoice_remove_callback(_invoice_remove)

    def _invoice_train(key: str, year: int, month: int, fields: dict,
                       raw_text: str, filename: str) -> dict:
        """Train on a corrected invoice: derive layout patterns + reconcile."""
        from .invoice_parser import InvoiceData
        # Derive and store layout patterns from corrected field values
        learn_result = invoice_layouts.derive_and_store_patterns(
            raw_text, fields, INVOICE_LAYOUTS_PATH)
        learned_fields = [k for k, v in learn_result.items() if v]

        # Build InvoiceData from the corrected fields
        inv_fields = {f: fields.get(f) for f in InvoiceData.__dataclass_fields__}
        inv_fields['year'] = year
        inv_fields['month'] = month
        # Ensure required fields have defaults
        inv_fields.setdefault('warnings', [])
        inv_fields.setdefault('doc_type', 'rozliczeniowa')
        try:
            data = InvoiceData(**inv_fields)  # type: ignore[arg-type]
        except Exception as exc:
            logger.exception('Train: failed to construct InvoiceData')
            return {'ok': False, 'error': str(exc)}

        snap = historic_store.snapshot_month(year, month, HISTORIC_PATH)
        reconciled = historic_store.reconcile_invoice(data, HISTORIC_PATH)
        invoice_store.upsert(data, filename=filename, reconciled=reconciled,
                              pre_reconcile=snap, path=INVOICE_PATH)
        # Remove the stub if it was one
        if key.startswith('unparsed-'):
            invoice_store.remove(key, INVOICE_PATH)
        invoice_layouts.record_example({
            'invoice_number': fields.get('invoice_number'),
            'source_filename': filename,
            'learned_fields': learned_fields,
        }, INVOICE_LAYOUTS_PATH)
        logger.info('Trained invoice %d-%02d; learned fields: %s', year, month, learned_fields)

        # Auto-retry all remaining stubs with the newly learned patterns.
        # Stubs whose raw text now parses successfully are promoted to full invoices.
        auto_promoted = _retry_stubs()

        poll_and_publish()
        return {'ok': True, 'learned_fields': learned_fields, 'reconciled': reconciled,
                'auto_promoted': auto_promoted}

    def _retry_stubs() -> list:
        """Re-parse all stubs using current (possibly just-updated) learned patterns.
        Stubs that now parse successfully are reconciled and promoted automatically."""
        from .invoice_parser import _parse_text, InvoiceParseError
        promoted = []
        all_inv = invoice_store.load(INVOICE_PATH)
        for stub_key, stub in list(all_inv.items()):
            if not stub.get('needs_training'):
                continue
            raw_text = stub.get('raw_text', '')
            if not raw_text:
                continue
            try:
                from .invoice_parser import _parse_text
                data = _parse_text(raw_text)
                data._filename = stub.get('filename', '')  # type: ignore[attr-defined]
                snap = historic_store.snapshot_month(data.year, data.month, HISTORIC_PATH)
                reconciled = historic_store.reconcile_invoice(data, HISTORIC_PATH)
                invoice_store.upsert(data, filename=stub.get('filename', ''),
                                     reconciled=reconciled, pre_reconcile=snap,
                                     path=INVOICE_PATH)
                invoice_store.remove(stub_key, INVOICE_PATH)
                promoted.append(f'{data.year}-{data.month:02d}')
                logger.info('Auto-promoted stub %s → %d-%02d', stub_key, data.year, data.month)
            except InvoiceParseError:
                pass  # still needs training
            except Exception:
                logger.exception('Auto-retry of stub %s failed', stub_key)
        return promoted

    _web.set_invoice_train_callback(_invoice_train)
    _web.set_invoice_path(INVOICE_PATH)
    _web.set_layouts_path(INVOICE_LAYOUTS_PATH)

    from . import tariff_config as _tc
    _tc.seed_if_missing(TARIFF_CONFIG_PATH)
    _web.set_tariff_config_path(TARIFF_CONFIG_PATH)

    from datetime import date

    def _tariff_rates_for(d: date) -> tuple:
        """Stawki brutto peak/offpeak (PLN/kWh) z tariff_config na dany dzień.

        Podmienia zaszyty fallback env TARIFF_PEAK_PRICE/TARIFF_OFFPEAK_PRICE
        (opcje usunięte z config.yaml w 0.22.0, więc dawny fallback nigdy nie
        widział realnej zmiany taryfy zanim nadeszła faktura). Ta sama ścieżka
        co battery_job i rekonsyliacja faktur — jeden pierwotny cennik."""
        rates = _tc.effective_baseline(_tc.load(TARIFF_CONFIG_PATH), d)
        return (
            float(rates.get('peak_gross', live_reader._TARIFF_PEAK_PRICE)),
            float(rates.get('offpeak_gross', live_reader._TARIFF_OFFPEAK_PRICE)),
        )

    # ── Symulacja rozbudowy magazynu (zakładka „Magazyn +5 kWh") ─────────────
    import threading as _threading
    from . import battery_sim, battery_store, rce_hourly as _rceh

    _battery_lock = _threading.Lock()
    _battery_state: dict = {'summary': None}

    def battery_job(fetch_lts: bool = True) -> None:
        """Dociągnij godzinowe LTS (eksport/import), przelicz symulację, odśwież UI."""
        from datetime import datetime as _dtb, timedelta as _tdb, timezone as _tzb
        # Blokująco: zapis konfiguracji z UI musi doczekać się przeliczenia,
        # a równoległe przebiegi (cron + POST) wykonują się wtedy szeregowo.
        _battery_lock.acquire()
        try:
            cfg = battery_store.load_config(BATTERY_CONFIG_PATH)
            hours = battery_store.load_hours(BATTERY_HOURS_PATH)
            if fetch_lts:
                # 48 h zakładki: LTS bieżących godzin bywa opóźnione/korygowane
                if hours:
                    fetch_from = _dtb.strptime(max(hours), '%Y-%m-%dT%H') - _tdb(hours=48)
                else:
                    fetch_from = _dtb.strptime(cfg.start_month + '-01T00', '%Y-%m-%dT%H')
                start_iso = (fetch_from.astimezone().astimezone(_tzb.utc)
                             .strftime('%Y-%m-%dT%H:%M:%S+00:00'))
                new = live_reader.get_hourly_energy(start_iso)
                if new:
                    hours.update(new)
                    battery_store.save_hours(hours, BATTERY_HOURS_PATH)
            if not hours:
                _record_job('battery_sim', False, 'brak godzinowych statystyk liczników')
                return

            # Cennik stref per miesiąc z tariff_config (kumulatywny baseline)
            tcfg = _tc.load(TARIFF_CONFIG_PATH)
            zone_prices: dict = {}
            for ym in sorted({k[:7] for k in hours}):
                rates = _tc.effective_baseline(tcfg, date(int(ym[:4]), int(ym[5:7]), 28))
                zone_prices[ym] = (
                    float(rates.get('peak_gross', live_reader._TARIFF_PEAK_PRICE)),
                    float(rates.get('offpeak_gross', live_reader._TARIFF_OFFPEAK_PRICE)),
                )
            rcem = {ym: float(v) for ym, v in
                    rcem_scraper._load_history(RCEM_HISTORY_PATH).items()}
            rce_prices = _rceh._load_cache(RCE_HOURLY_CACHE_PATH).get('prices', {})

            payload = battery_sim.simulate_all(hours, cfg, zone_prices, rcem,
                                               rce_hourly=rce_prices)
            _web.update_battery_sim(payload)
            _battery_state['summary'] = payload.get('summary')
            _record_job('battery_sim', True)
            logger.info('Battery sim: %d mies., śr. %s zł/mies., payback %s',
                        payload['summary']['months_simulated'],
                        payload['summary']['monthly_avg_savings'],
                        payload['summary']['payback_date'] or 'poza horyzontem')
        except Exception:
            logger.exception('Battery expansion simulation failed')
            _record_job('battery_sim', False, 'symulacja magazynu nie powiodła się')
        finally:
            _battery_lock.release()

    def _battery_config_changed(cfg) -> None:
        battery_store.save_config(cfg, BATTERY_CONFIG_PATH)
        # Zmiana parametrów nie zmienia zmierzonych godzin — licz z cache,
        # bez rundy WebSocket do HA (fetch dołoży cron/boot).
        battery_job(fetch_lts=False)

    _web.set_battery_config_path(BATTERY_CONFIG_PATH)
    _web.set_battery_config_callback(_battery_config_changed)

    def _rcem_scrape_status(now: date) -> str:
        y, m = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        prev = f'{y}-{m:02d}'
        if prev in rcem_scraper._load_history(RCEM_HISTORY_PATH):
            return 'ok'
        if now.day < 11:
            return 'pending'
        if now.day <= 20:
            return 'retrying'
        return 'error'

    from .tariff_analysis import compute_tariff_tab

    _last: dict = {'result': None}
    _last_current: dict = {'record': None, 'key': None}

    def poll_and_publish() -> None:
        try:
            historic = historic_store.load(HISTORIC_PATH)
            rcem_price = rcem_scraper.get_current_month_rcem(RCEM_HISTORY_PATH)
            _today = date.today()
            _current_key = (_today.year, _today.month)
            _peak_gross, _offpeak_gross = _tariff_rates_for(_today)
            current = live_reader.read_current_month(
                rcem_price=rcem_price, historic_records=historic,
                peak_gross=_peak_gross, offpeak_gross=_offpeak_gross)
            if current is not None:
                _last_current['record'] = current
                _last_current['key'] = _current_key
            elif _last_current['record'] is not None and _last_current['key'] == _current_key:
                # Scoped to the current (year, month): a cached record from a
                # PRIOR month must never be reused across the month boundary —
                # it would silently override the just-closed, possibly
                # invoice-reconciled historic row for hours until the next
                # successful live read (concat.concat prefers `current` for
                # its own (year, month) key over the historic entry).
                logger.warning(
                    'sensor.inverter_yield_monthly niedostępny — używam ostatniego odczytu bieżącego miesiąca'
                )
                current = _last_current['record']
            elif _last_current['key'] != _current_key:
                _last_current['record'] = None
                _last_current['key'] = None
            all_records = concat.concat(historic, current)
            result = roi.calculate(all_records,
                                   gross_investment=GROSS_INVESTMENT,
                                   subsidy=SUBSIDY,
                                   system_kwp=SYSTEM_KWP,
                                   discount_rate=DISCOUNT_RATE,
                                   inflation=INFLATION_RATE,
                                   comparison_yield=COMPARISON_YIELD,
                                   co2_factor=CO2_FACTOR,
                                   asset_lifetime_years=ASSET_LIFETIME_YEARS,
                                   panel_degradation_pct_year=PANEL_DEGRADATION_PCT_YEAR)

            # --- Cross-family produkcji: jedyny realny sygnał w tym kodzie ---
            # produced_kwh (rodzina Energy Dashboard) vs cross_family_produced_kwh
            # (rodzina inverter_total_yield) — patrz balance.py; przekracza health
            # sensor do 'degraded' zamiast być niewidoczny tak jak przed 0.35.0.
            try:
                balance_check = balance.check_all(all_records)
                _record_job('energy_balance', balance_check['ok'],
                           '; '.join(f"{b['ym']}: diff={b['diff_kwh']} kWh ({b['diff_pct']}%)"
                                     for b in balance_check['breaches'][:5]))
            except Exception:
                logger.exception('Energy balance check failed — continuing')
                _record_job('energy_balance', False, 'sprawdzenie bilansu energii nie powiodło się')

            _now = date.today()
            month_closed = any(r.year == _now.year and r.month == _now.month for r in historic)
            current_month_savings = (
                (current.self_consumed_savings_pln or 0.0) + (current.feedin_revenue_pln or 0.0)
                if current else None
            )
            scrape_status = _rcem_scrape_status(_now)

            # --- Depozyt prosumencki: ledger FIFO z przedawnieniem ---
            deposit_result = None
            deposit_refund_delta_annual = None
            try:
                from . import deposit as _deposit
                _invoices_effective = invoice_store.effective_by_month(invoice_store.load(INVOICE_PATH))
                deposit_result = _deposit.calculate(
                    all_records,
                    _invoices_effective,
                    refund_cap=DEPOSIT_REFUND_PCT,
                )
                # Hipotetyczny efekt przejścia na RCE godzinową: wyższy limit zwrotu
                # depozytu (30% zamiast 20%) — wejście dla doradcy RCEm→RCE.
                deposit_result_rce = _deposit.calculate(
                    all_records, _invoices_effective, refund_cap=0.30,
                )
                deposit_refund_delta_annual = round(
                    deposit_result_rce.projected_refund_12m - deposit_result.projected_refund_12m, 2)
                _record_job('deposit', True)
            except Exception:
                logger.exception('Deposit ledger failed — continuing')
                _record_job('deposit', False, 'ledger depozytu nie powiódł się')

            # Computed once and reused below for the tariff comparison too —
            # both happen in this same poll cycle against the same invoice data.
            _invoice_rates = _web.latest_invoice_rates()

            pub.publish_roi(result,
                            current_month_savings=current_month_savings,
                            rcem_scrape_status=scrape_status,
                            projected_month_kwh=current.projected_month_kwh if current else None,
                            projected_month_savings=current.projected_month_savings_pln if current else None,
                            deposit_balance=(deposit_result.balance_estimate
                                             if deposit_result and deposit_result.balance_estimate is not None
                                             else (deposit_result.balance_model if deposit_result else None)),
                            deposit_expiring_30d=deposit_result.expiring_1m if deposit_result else None,
                            invoice_rates=_invoice_rates,
                            battery_expansion=_battery_state['summary'])
            _web.update_state(result, all_records, rcem_price, month_closed=month_closed,
                              rcem_scrape_status=scrape_status)
            _web.update_deposit(deposit_result)

            # --- Prognoza wieloletnia (panel 25 lat) ---
            try:
                lifetime_forecast = roi.forecast_lifetime(
                    all_records, today=_now,
                    gross_investment=GROSS_INVESTMENT, subsidy=SUBSIDY,
                    system_kwp=SYSTEM_KWP, discount_rate=DISCOUNT_RATE,
                    asset_lifetime_years=ASSET_LIFETIME_YEARS,
                    panel_degradation_pct_year=PANEL_DEGRADATION_PCT_YEAR,
                )
                _web.update_lifetime_forecast(lifetime_forecast)
                _record_job('lifetime_forecast', True)
            except Exception:
                logger.exception('Lifetime forecast failed — continuing')
                _record_job('lifetime_forecast', False, 'prognoza wieloletnia nie powiodła się')

            # --- Tariff comparison tab ---
            try:
                monthly_stats = live_reader.get_ha_monthly_stats(
                    [
                        'sensor.symulacja_miesieczna_dynamicznej_faktura',
                        'sensor.koszt_zmienny_g12w_miesieczny',
                    ],
                    start_month='2024-12-01',
                )
                dyn_monthly  = monthly_stats.get('sensor.symulacja_miesieczna_dynamicznej_faktura', {})
                g12w_monthly = monthly_stats.get('sensor.koszt_zmienny_g12w_miesieczny', {})
                history_7d = live_reader.get_ha_history_7d([
                    'sensor.calkowity_koszt_1_kwh_dynamiczna',
                    'sensor.power_tauron_g12w_current_price',
                    'sensor.roznica_dzienna_g12w_vs_dynamiczna',
                ])
                live_tariff = live_reader.read_tariff_live()
                # Single source of truth: latest parsed invoice (_invoice_rates,
                # computed once above) overrides the FIXED_GROSS_PLN / 1.23 / 0.63
                # fallback constants when available.
                _fixed_net = _invoice_rates.get('fixed_total_net')
                tariff_data = compute_tariff_tab(
                    records=all_records,
                    dynamic_monthly_stats=dyn_monthly,
                    g12w_monthly_stats=g12w_monthly,
                    current_roi=result,
                    current_month_live=live_tariff,
                    tariff_history_7d=history_7d,
                    fixed_gross_pln=round(_fixed_net * 1.23, 2) if _fixed_net is not None else None,
                    peak_gross=_invoice_rates.get('peak_gross'),
                    offpeak_gross=_invoice_rates.get('offpeak_gross'),
                )
                _web.update_tariff_comparison(tariff_data)
                _record_job('tariff_comparison', True)
            except Exception:
                logger.exception('Tariff comparison update failed — continuing')
                _record_job('tariff_comparison', False, 'porównanie taryf nie powiodło się')

            # --- RCEm vs RCE-godzinowa ---
            try:
                from . import rce_hourly
                rce_payload = rce_hourly.update_and_compare(
                    all_records,
                    rcem_scraper._load_history(RCEM_HISTORY_PATH),
                    cache_path=RCE_HOURLY_CACHE_PATH,
                )
                if deposit_refund_delta_annual is not None:
                    summary = rce_payload['summary']
                    rce_payload['advisor'] = rce_hourly.switch_advisor(
                        summary['avg_monthly_diff_pln'],
                        deposit_refund_delta_annual,
                        summary['n_months'],
                    )
                _web.update_rce_comparison(rce_payload)
                _record_job('rce_hourly', True)
            except Exception:
                logger.exception('RCE hourly comparison failed — continuing')
                _record_job('rce_hourly', False, 'symulacja RCE godzinowej nie powiodła się')

            _record_job('poll', True)
            _last['result'] = result
            logger.info('Poll complete — ROI %.2f%%, remaining %.0f PLN, payback %s',
                        result.roi_pct, result.remaining_to_recover,
                        result.payback_date or 'unknown')
        except Exception:
            logger.exception('Poll loop error')
            _record_job('poll', False, 'pętla poll padła — sprawdź log add-onu')

        try:
            state, attrs = _health_snapshot()
            attrs['rcem_scrape_status'] = _rcem_scrape_status(date.today())
            pub.publish_health(state, attrs)
        except Exception:
            logger.exception('Health publish failed')

    def _target_month_key(now: date) -> str:
        y, m = previous_month(now)
        return f'{y}-{m:02d}'

    def _catch_up_missing_month_close() -> None:
        """
        Month-close fires only at 23:55 on the last day of the month (cron). If the
        add-on wasn't running at that exact minute — update, restart, host reboot —
        the month never lands in historic.json, and the gap is permanent: utility
        meters reset at midnight, so live sensors no longer hold the number.

        HA long-term statistics survive the meter reset, so on every startup sweep
        every month from the earliest one with data through the previous calendar
        month (_scan_and_heal_all_months — not just the single previous month, so
        an outage spanning ≥2 months doesn't leave the older gap unnoticed forever)
        and backfill anything missing OR balance-inconsistent the same way
        /api/historic/reread-month already does manually. Runs BEFORE invoice
        reconciliation below — reconcile_invoice() can only overwrite an existing
        month row, not create one, so a pending invoice for a fully missing month
        would otherwise silently fail to apply.
        """
        healed = _scan_and_heal_all_months()
        if healed:
            logger.info('Startup: naprawiono %d miesiąc(e/y): %s', len(healed), ', '.join(healed))

    def rcem_job() -> None:
        today = date.today()
        target = _target_month_key(today)
        if rcem_scraper._load_history(RCEM_HISTORY_PATH).get(target) is not None:
            logger.debug('RCEm job: %s already known, skipping', target)
            return

        def _on_rcem_update():
            poll_and_publish()
            price = rcem_scraper._load_history(RCEM_HISTORY_PATH).get(target)
            if price is not None:
                _notify_ha('PV ROI Tracker',
                           f'RCEm {target}: {price:.4f} zł/kWh — obliczenia zaktualizowane')

        found = rcem_scraper.run_scheduled_scrape(
            history_path=RCEM_HISTORY_PATH,
            historic_json_path=HISTORIC_PATH,
            corrections_path=RCEM_CORRECTIONS_PATH,
            on_update=_on_rcem_update,
        )
        if not found:
            logger.info('RCEm %s nie opublikowane jeszcze przez PSE — ponowna próba o następnej zaplanowanej porze', target)

    def rcem_correction_job() -> None:
        """Always scrapes — used for the 1st-of-month correction scan."""
        rcem_scraper.run_scheduled_scrape(
            history_path=RCEM_HISTORY_PATH,
            historic_json_path=HISTORIC_PATH,
            corrections_path=RCEM_CORRECTIONS_PATH,
            on_update=poll_and_publish,
        )

    def _monthly_summary_notification() -> None:
        """Polskie podsumowanie zamkniętego miesiąca → notify.family.

        Jeśli rekord brakuje lub nie ma danych produkcji, wysyła jawne
        ostrzeżenie zamiast milczeć albo — gorzej — pokazywać mylące
        „oszczędności 0 zł" tak jak przy incydencie 2026-08-01 (log
        z 2026-07-31 23:55:20 brzmiał dokładnie tak, mimo że lipiec
        faktycznie wyprodukował ponad 800 kWh — dane po prostu nie
        trafiły na dysk).
        """
        today = date.today()
        rec = next((r for r in historic_store.load(HISTORIC_PATH)
                    if r.year == today.year and r.month == today.month), None)
        if rec is None or not historic_store.has_energy_data(rec.produced_kwh):
            _notify_ha(
                f'⚠️ PV — {today.year}-{today.month:02d} bez danych',
                'Zamknięcie miesiąca nie zapisało odczytu produkcji — sprawdź logi add-onu '
                'i w razie potrzeby uzupełnij ręcznie przez /api/historic/reread-month.')
            return
        savings = ((rec.self_consumed_savings_pln or 0.0)
                   + (rec.feedin_revenue_pln or 0.0)
                   + (rec.battery_arbitrage_savings_pln or 0.0))
        parts = [f'Produkcja {rec.produced_kwh:.0f} kWh' if rec.produced_kwh is not None else None,
                 f'oszczędności {savings:.0f} zł'
                 + (' (sprzedaż wg RCEm doliczona po publikacji)' if rec.rcem_status != 'confirmed' else '')]
        result = _last['result']
        if result is not None:
            parts.append(f'ROI {result.roi_pct:.1f}%')
            if result.remaining_to_recover > 0:
                parts.append(f'do spłaty {result.remaining_to_recover:.0f} zł')
            if result.payback_date:
                parts.append(f'przewidywana spłata {result.payback_date.isoformat()[:7]}')
        _notify_ha(f'PV — podsumowanie {today.year}-{today.month:02d}',
                   ', '.join(p for p in parts if p) + '.')

    def month_close_job() -> None:
        try:
            _peak_gross, _offpeak_gross = _tariff_rates_for(date.today())
            appended = close_month(historic_path=HISTORIC_PATH, rcem_history_path=RCEM_HISTORY_PATH,
                                   peak_gross=_peak_gross, offpeak_gross=_offpeak_gross)
            historic_store.reconcile_pending_invoices(INVOICE_PATH, HISTORIC_PATH)
            poll_and_publish()
            if MONTHLY_NOTIFY:
                _monthly_summary_notification()
            # appended=False means close_month() skipped (row already present)
            # or live_reader returned None (sensor unavailable at 23:55) — not
            # itself an error (month_close_reconcile/verify pick up either
            # case), but worth keeping visible rather than always reporting
            # True regardless of what actually happened.
            _record_job('month_close', True,
                       '' if appended else 'snapshot pominięty (już istniał lub sensor niedostępny)')
        except Exception:
            logger.exception('Month-close error')
            _record_job('month_close', False, 'zamknięcie miesiąca nie powiodło się')

    def month_close_reconcile_job() -> None:
        """Nadpisuje prowizoryczny snapshot z 23:55 autorytatywnym odczytem z
        lifetime liczników (LTS) — bezwarunkowo, niezależnie od tego czy
        23:55 już zapisał dane. 23:55 czyta liczniki resetujące się co
        miesiąc: ucinają ostatnie ~5 minut miesiąca i (zmierzone dla lipca
        2026) niedoszacowują produkcję o ~0,6% względem lifetime liczników
        (patrz live_reader.py docstring / plan docs/pv_roi_energy_rebase).
        Odpala się krótko po północy 1. dnia miesiąca — jeśli statystyki HA
        za poprzedni miesiąc nie są jeszcze skompilowane, `_reread_month`
        zwraca None i prowizoryczny snapshot zostaje; month_close_verify
        (01:00) i _scan_and_heal_all_months (każdy start/weryfikacja) łapią
        to później przez sprawdzenie bilansu energii.
        """
        try:
            prev_y, prev_m = previous_month(date.today())
            record = _reread_month(prev_y, prev_m)
            if record is None:
                logger.warning(
                    'month_close_reconcile: brak jeszcze statystyk HA dla %d-%02d — '
                    'prowizoryczny snapshot z 23:55 pozostaje na razie', prev_y, prev_m)
                _record_job('month_close_reconcile', False,
                           f'{prev_y}-{prev_m:02d}: statystyki HA jeszcze niedostępne')
            else:
                logger.info(
                    'month_close_reconcile: %d-%02d nadpisany autorytatywnym odczytem LTS '
                    '(produkcja=%.1f kWh)', prev_y, prev_m, record.produced_kwh)
                _record_job('month_close_reconcile', True)
        except Exception:
            logger.exception('month_close_reconcile error')
            _record_job('month_close_reconcile', False, 'reconciliacja zamknięcia miesiąca nie powiodła się')

    def month_close_verify_job() -> None:
        """Bezpiecznik na wypadek, gdyby month_close_job w ogóle się nie odpalił
        (add-on nie działał o 23:55 i restart nie nastąpił do 1. dnia miesiąca —
        _catch_up_missing_month_close łapie ten przypadek tylko na starcie) LUB
        gdyby month_close_reconcile (00:05) nie zdążył jeszcze pobrać statystyk.

        Odpala się dzień po zamknięciu miesiąca; przelatuje WSZYSTKIE miesiące
        od pierwszego z danymi (_scan_and_heal_all_months — nie tylko
        poprzedni), naprawiając zarówno braki jak i niespójności bilansu.
        """
        try:
            healed = _scan_and_heal_all_months()
            prev_y, prev_m = previous_month(date.today())
            if not month_has_data(historic_store.load(HISTORIC_PATH), prev_y, prev_m):
                _notify_ha(
                    f'⚠️ PV — {prev_y}-{prev_m:02d} bez danych',
                    'Zamknięcie miesiąca nie powiodło się i backfill ze statystyk HA też '
                    'nie znalazł danych — uzupełnij ręcznie przez /api/historic/reread-month.')
                _record_job('month_close_verify', False, f'{prev_y}-{prev_m:02d} pozostaje pusty')
            else:
                _record_job('month_close_verify', True,
                           f'naprawiono: {", ".join(healed)}' if healed else '')
        except Exception:
            logger.exception('month_close_verify error')
            _record_job('month_close_verify', False, 'weryfikacja zamknięcia miesiąca nie powiodła się')

    # ── Scheduler setup ───────────────────────────────────────────────────────
    _tz_name = os.environ.get('TZ', 'Europe/Warsaw')
    scheduler = BlockingScheduler(timezone=ZoneInfo(_tz_name))
    logger.info('Scheduler timezone: %s', _tz_name)

    # misfire_grace_time/coalesce on every job below: APScheduler's default
    # misfire_grace_time is 1 SECOND, so any scheduler stall past a job's
    # trigger time (GC pause, event-loop backlog, container CPU throttling)
    # silently skips that run entirely — including month_close at 23:55,
    # where a skip means the month is never snapshotted at all until the
    # next healer catches it. A generous 1h grace window with coalesce=True
    # (collapse multiple missed runs into one) makes a brief stall harmless.
    _JOB_DEFAULTS = dict(misfire_grace_time=3600, coalesce=True)

    # Poll every N minutes
    scheduler.add_job(poll_and_publish, 'interval', minutes=POLL_INTERVAL,
                      id='poll', name='ROI poll + MQTT publish', **_JOB_DEFAULTS)

    # Month-close: last day of month at 23:55 local time — provisional only
    # (reads the monthly-resetting utility_meter sensors; superseded below).
    scheduler.add_job(month_close_job, CronTrigger(day='last', hour=23, minute=55),
                      id='month_close', name='Month-close snapshot', **_JOB_DEFAULTS)

    # Month-close reconcile: 1st of each month at 00:05 — unconditionally
    # overwrites the 23:55 provisional snapshot with an authoritative reread
    # from the lifetime (never-resetting) meters via long-term statistics,
    # fixing the ~5-minute truncation and the ~0.6% monthly-meter drift
    # measured against the lifetime totals (see live_reader.py docstring).
    scheduler.add_job(month_close_reconcile_job, CronTrigger(day=1, hour=0, minute=5),
                      id='month_close_reconcile', name='Month-close LTS reconcile', **_JOB_DEFAULTS)

    # Month-close verify: 1st of each month at 01:00 — catches a month-close
    # (and reconcile) that silently produced no data, or any earlier month
    # left with a balance-inconsistent record (_scan_and_heal_all_months).
    scheduler.add_job(month_close_verify_job, CronTrigger(day=1, hour=1, minute=0),
                      id='month_close_verify', name='Month-close verify', **_JOB_DEFAULTS)

    # RCEm: days 11–20, every 2 hours starting at 08:00 local time
    # Skips automatically once the previous month price is stored.
    scheduler.add_job(rcem_job, CronTrigger(day='11-20', hour='8,10,12,14,16,18,20,22', minute=0),
                      id='rcem_scrape', name='RCEm price scrape', **_JOB_DEFAULTS)

    # RCEm correction scan: 1st of each month at 06:00 — catches skorygowana corrections
    # that appear after the 20th-day retry window (PSE allows corrections up to 12 months later)
    scheduler.add_job(rcem_correction_job, CronTrigger(day=1, hour=6, minute=0),
                      id='rcem_correction_scan', name='RCEm correction scan', **_JOB_DEFAULTS)

    # Daily backup to /share so data survives accidental add-on removal
    scheduler.add_job(_backup_data, CronTrigger(hour=2, minute=0),
                      id='backup', name='Daily data backup', **_JOB_DEFAULTS)

    # Daily Energy Dashboard prefs refresh: get_energy_dashboard_sources() is
    # cached in-process after its first successful fetch (at startup, via the
    # first poll), so a user reconfiguring Settings -> Energy wouldn't be
    # picked up until the next add-on restart without this — prefs rarely
    # change, but a daily force_refresh is cheap and keeps that window short.
    def energy_prefs_refresh_job() -> None:
        try:
            live_reader.get_energy_dashboard_sources(force_refresh=True)
            _record_job('energy_prefs_refresh', True)
        except Exception:
            logger.exception('Energy Dashboard prefs refresh failed')
            _record_job('energy_prefs_refresh', False, 'odświeżenie konfiguracji Energy Dashboard nie powiodło się')

    scheduler.add_job(energy_prefs_refresh_job, CronTrigger(hour=3, minute=0),
                      id='energy_prefs_refresh', name='Energy Dashboard prefs refresh', **_JOB_DEFAULTS)

    # Monthly CPI refresh: day 16 at 12:00 (GUS publishes prev-month CPI ~15th)
    def cpi_job() -> None:
        try:
            cpi_fetcher.refresh()
            _record_job('cpi', True)
        except Exception:
            logger.exception('CPI refresh failed')
            _record_job('cpi', False, 'odświeżenie CPI z GUS nie powiodło się')

    scheduler.add_job(cpi_job, CronTrigger(day=16, hour=12, minute=0),
                      id='cpi_refresh', name='Monthly CPI refresh', **_JOB_DEFAULTS)

    # Symulacja rozbudowy magazynu: raz dziennie (LTS zmienia się co godzinę,
    # a wynik miesięczny — wolno); pierwszy przebieg w tle przy starcie.
    scheduler.add_job(battery_job, CronTrigger(hour=5, minute=15),
                      id='battery_sim', name='Battery expansion simulation', **_JOB_DEFAULTS)

    logger.info('PV ROI Tracker v%s started — poll every %d min', __version__, POLL_INTERVAL)

    _backup_data()   # ensure /share copy is current on every start

    # Startup: drop empty placeholder rows for months that haven't happened yet
    # (e.g. left over from a CSV import that pre-seeded a full calendar year) —
    # defense in depth alongside append_month's own overwrite-empty logic.
    _pruned = historic_store.prune_future_months(date.today(), HISTORIC_PATH)
    if _pruned:
        logger.info('Startup pruned %d future placeholder month(s)', _pruned)

    # Startup: backfill the previous month if a missed month-close left it out
    _catch_up_missing_month_close()

    # Startup: apply any invoices uploaded before their month was closed
    historic_store.reconcile_pending_invoices(INVOICE_PATH, HISTORIC_PATH)

    # Startup: tag legacy reconciled months that predate the `tariff` field
    _tagged = historic_store.backfill_tariff(HISTORIC_PATH)
    if _tagged:
        logger.info('Startup backfilled tariff for %d legacy month(s)', _tagged)

    # Startup: re-try stubs — learned patterns from a previous session may now
    # resolve invoices that failed when they were first uploaded.
    _promoted = _retry_stubs()
    if _promoted:
        logger.info('Startup auto-promoted %d stub(s): %s', len(_promoted), _promoted)

    # Startup: heal any months whose feedin_price is missing from historic.json
    # but whose RCEm price is already in rcem_history.json (can happen after a crash).
    rcem_scraper.heal_rcem_backfill(
        history_path=RCEM_HISTORY_PATH,
        historic_json_path=HISTORIC_PATH,
    )

    # Startup scan: only hit PSE if rcem_history.json is older than 24 h.
    # Catches corrections after a long outage without hammering PSE on every restart.
    from datetime import datetime as _dt
    _rcem_age_h = (
        (_dt.now().timestamp() - RCEM_HISTORY_PATH.stat().st_mtime) / 3600
        if RCEM_HISTORY_PATH.exists() else float('inf')
    )
    if _rcem_age_h > 24:
        logger.info('RCEm history %.0fh old — running startup scrape', _rcem_age_h)
        rcem_scraper.run_scheduled_scrape(
            history_path=RCEM_HISTORY_PATH,
            historic_json_path=HISTORIC_PATH,
            corrections_path=RCEM_CORRECTIONS_PATH,
        )
    else:
        logger.info('RCEm history %.0fh old — skipping startup scrape', _rcem_age_h)

    poll_and_publish()   # initial poll with up-to-date RCEm history

    # Pierwszy przebieg symulacji magazynu w tle (backfill LTS może potrwać ~30 s)
    _threading.Thread(target=battery_job, daemon=True, name='battery-sim-boot').start()

    scheduler.start()


if __name__ == '__main__':
    main()
