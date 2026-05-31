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
BACKUP_SHARE       = Path(os.environ.get('BACKUP_SHARE', '/share/pv_roi_tracker'))
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
TARIFF_PEAK_PRICE    = float(os.environ.get('TARIFF_PEAK_PRICE', '1.23'))
TARIFF_OFFPEAK_PRICE = float(os.environ.get('TARIFF_OFFPEAK_PRICE', '0.63'))


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


def _backup_data() -> None:
    import shutil
    try:
        BACKUP_SHARE.mkdir(parents=True, exist_ok=True)
        for src in [HISTORIC_PATH, RCEM_HISTORY_PATH, RCEM_CORRECTIONS_PATH, INVOICE_PATH]:
            if src.exists():
                shutil.copy2(src, BACKUP_SHARE / src.name)
        logger.info('Data backed up to %s', BACKUP_SHARE)
    except Exception:
        logger.exception('Backup failed')


def main() -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    from . import concat, historic_store, live_reader, rcem_scraper, roi, cpi_fetcher
    from . import invoice_store
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

    def _invoice_reconcile(parsed_list: list) -> None:
        """Callback from web upload: reconcile each parsed InvoiceData."""
        for data in parsed_list:
            filename = getattr(data, '_filename', '')
            reconciled = historic_store.reconcile_invoice(data, HISTORIC_PATH)
            invoice_store.upsert(data, filename=filename,
                                 reconciled=reconciled, path=INVOICE_PATH)
            logger.info('Invoice %d-%02d ingested (reconciled=%s)', data.year, data.month, reconciled)
        poll_and_publish()

    _web.set_invoice_reconcile_callback(_invoice_reconcile)
    _web.set_invoice_path(INVOICE_PATH)
    _web.set_tariff_config(TARIFF_PEAK_PRICE, TARIFF_OFFPEAK_PRICE)

    from datetime import date

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

    def poll_and_publish() -> None:
        try:
            historic = historic_store.load(HISTORIC_PATH)
            rcem_price = rcem_scraper.get_current_month_rcem(RCEM_HISTORY_PATH)
            current = live_reader.read_current_month(rcem_price=rcem_price, historic_records=historic)
            all_records = concat.concat(historic, current)
            result = roi.calculate(all_records,
                                   gross_investment=GROSS_INVESTMENT,
                                   subsidy=SUBSIDY,
                                   system_kwp=SYSTEM_KWP,
                                   discount_rate=DISCOUNT_RATE,
                                   inflation=INFLATION_RATE,
                                   comparison_yield=COMPARISON_YIELD)
            _now = date.today()
            month_closed = any(r.year == _now.year and r.month == _now.month for r in historic)
            current_month_savings = (
                (current.self_consumed_savings_pln or 0.0) + (current.feedin_revenue_pln or 0.0)
                if current else None
            )
            scrape_status = _rcem_scrape_status(_now)
            pub.publish_roi(result,
                            current_month_savings=current_month_savings,
                            rcem_scrape_status=scrape_status,
                            projected_month_kwh=current.projected_month_kwh if current else None,
                            projected_month_savings=current.projected_month_savings_pln if current else None)
            _web.update_state(result, all_records, rcem_price, month_closed=month_closed,
                              rcem_scrape_status=scrape_status)
            logger.info('Poll complete — ROI %.2f%%, remaining %.0f PLN, payback %s',
                        result.roi_pct, result.remaining_to_recover,
                        result.payback_date or 'unknown')
        except Exception:
            logger.exception('Poll loop error')

    def _target_month_key(now: date) -> str:
        y, m = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
        return f'{y}-{m:02d}'

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

    def month_close_job() -> None:
        try:
            close_month(historic_path=HISTORIC_PATH, rcem_history_path=RCEM_HISTORY_PATH)
            # Apply any invoice that was uploaded before the month was closed
            historic_store.reconcile_pending_invoices(INVOICE_PATH, HISTORIC_PATH)
            poll_and_publish()
        except Exception:
            logger.exception('Month-close error')

    # ── Scheduler setup ───────────────────────────────────────────────────────
    scheduler = BlockingScheduler()

    # Poll every N minutes
    scheduler.add_job(poll_and_publish, 'interval', minutes=POLL_INTERVAL,
                      id='poll', name='ROI poll + MQTT publish')

    # Month-close: last day of month at 23:55 local time
    scheduler.add_job(month_close_job, CronTrigger(day='last', hour=23, minute=55),
                      id='month_close', name='Month-close snapshot')

    # RCEm: days 11–20, every 2 hours starting at 08:00 local time
    # Skips automatically once the previous month price is stored.
    scheduler.add_job(rcem_job, CronTrigger(day='11-20', hour='8,10,12,14,16,18,20,22', minute=0),
                      id='rcem_scrape', name='RCEm price scrape')

    # RCEm correction scan: 1st of each month at 06:00 — catches skorygowana corrections
    # that appear after the 20th-day retry window (PSE allows corrections up to 12 months later)
    scheduler.add_job(rcem_correction_job, CronTrigger(day=1, hour=6, minute=0),
                      id='rcem_correction_scan', name='RCEm correction scan')

    # Daily backup to /share so data survives accidental add-on removal
    scheduler.add_job(_backup_data, CronTrigger(hour=2, minute=0),
                      id='backup', name='Daily data backup')

    # Monthly CPI refresh: day 16 at 12:00 (GUS publishes prev-month CPI ~15th)
    scheduler.add_job(cpi_fetcher.refresh, CronTrigger(day=16, hour=12, minute=0),
                      id='cpi_refresh', name='Monthly CPI refresh')

    logger.info('PV ROI Tracker v%s started — poll every %d min', __version__, POLL_INTERVAL)

    _backup_data()   # ensure /share copy is current on every start

    # Startup: apply any invoices uploaded before their month was closed
    historic_store.reconcile_pending_invoices(INVOICE_PATH, HISTORIC_PATH)

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
    scheduler.start()


if __name__ == '__main__':
    main()
