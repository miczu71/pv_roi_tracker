"""
Add-on entry point — steady-state loop.

Jobs:
  • Poll every POLL_INTERVAL_MINUTES: read historic + live → ROI calc → MQTT publish
  • Month-close: 23:55 on last day of each month
  • RCEm scrape: days 11–20 at 09:00/12:00/20:00 local time — fetches full PSE table,
    applies new prices and 'skorygowana RCEm' corrections for any month
  • RCEm correction scan: 1st of each month at 06:00 — catches corrections that appear
    after the 20th-day retry window has closed
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=os.environ.get('LOG_LEVEL', 'info').upper(),
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)

# ── Config from env (set by run.sh via bashio) ────────────────────────────────

HISTORIC_PATH      = Path(os.environ.get('HISTORIC_PATH', '/data/historic.json'))
RCEM_HISTORY_PATH  = Path(os.environ.get('RCEM_HISTORY_PATH', '/data/rcem_history.json'))
CSV_URL            = os.environ.get('CSV_URL', (
    'https://docs.google.com/spreadsheets/d/e/'
    '2PACX-1vT_3gmj8s_2JPJzPijb9T27oh0FS3JhlQ3cB1HkKPbBa2yi8GyudqJb5ZeyM20QQ9IKbfB3SnbKZukC'
    '/pub?output=csv'
))
FORCE_REIMPORT     = os.environ.get('FORCE_REIMPORT', 'false').lower() == 'true'
GROSS_INVESTMENT   = float(os.environ.get('GROSS_INVESTMENT', '51900.0'))
SUBSIDY            = float(os.environ.get('SUBSIDY', '28714.0'))
SYSTEM_KWP         = float(os.environ.get('SYSTEM_KWP', '6.72'))
POLL_INTERVAL      = int(os.environ.get('POLL_INTERVAL_MINUTES', '30'))
MQTT_HOST          = os.environ.get('MQTT_HOST', 'core-mosquitto')
MQTT_PORT          = int(os.environ.get('MQTT_PORT', '1883'))
MQTT_USER          = os.environ.get('MQTT_USER', '')
MQTT_PASSWORD      = os.environ.get('MQTT_PASSWORD', '')


def _ensure_historic() -> None:
    """Import CSV on first start or when FORCE_REIMPORT=true."""
    from . import historic_store, importer, parser

    if HISTORIC_PATH.exists() and not FORCE_REIMPORT:
        logger.info('Historic data present at %s — skipping import', HISTORIC_PATH)
        return

    logger.info('Importing historic CSV from Google Sheets …')
    try:
        csv_text = importer.fetch_csv(CSV_URL)
    except Exception as exc:
        logger.error('CSV fetch failed: %s', exc)
        sys.exit(1)

    records = parser.parse_csv(csv_text)
    if not records:
        logger.error('Parser returned zero records — check CSV URL and layout')
        sys.exit(1)

    historic_store.save(records, HISTORIC_PATH)
    logger.info('Import complete: %d monthly records written to %s', len(records), HISTORIC_PATH)


def main() -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    from . import concat, historic_store, live_reader, rcem_scraper, roi
    from .month_close import close_month
    from .publisher import MQTTPublisher
    from . import __version__

    _ensure_historic()

    from . import web as _web
    _web.start_server(port=8099)

    pub = MQTTPublisher(MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASSWORD, version=__version__)
    pub.connect()

    def _rcem_override(month_str: str, price: float) -> None:
        history = rcem_scraper._load_history(RCEM_HISTORY_PATH)
        history[month_str] = price
        rcem_scraper._save_history(history, RCEM_HISTORY_PATH)
        year, mon = int(month_str[:4]), int(month_str[5:])
        historic_store.backfill_rcem(year, mon, price, HISTORIC_PATH)
        logger.info('RCEm override: %s = %.4f PLN/kWh', month_str, price)
        poll_and_publish()

    _web.set_rcem_override_callback(_rcem_override)

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
            current = live_reader.read_current_month(rcem_price=rcem_price)
            all_records = concat.concat(historic, current)
            result = roi.calculate(all_records,
                                   gross_investment=GROSS_INVESTMENT,
                                   subsidy=SUBSIDY,
                                   system_kwp=SYSTEM_KWP)
            _now = date.today()
            month_closed = any(r.year == _now.year and r.month == _now.month for r in historic)
            current_month_savings = (
                (current.self_consumed_savings_pln or 0.0) + (current.feedin_revenue_pln or 0.0)
                if current else None
            )
            scrape_status = _rcem_scrape_status(_now)
            pub.publish_roi(result, rcem_price=rcem_price,
                            current_month_savings=current_month_savings,
                            rcem_scrape_status=scrape_status)
            _web.update_state(result, all_records, rcem_price, month_closed=month_closed)
            logger.info('Poll complete — ROI %.2f%%, remaining %.0f PLN, payback %s',
                        result.roi_pct, result.remaining_to_recover,
                        result.payback_date or 'unknown')
        except Exception:
            logger.exception('Poll loop error')

    def rcem_job() -> None:
        rcem_scraper.run_scheduled_scrape(
            history_path=RCEM_HISTORY_PATH,
            historic_json_path=HISTORIC_PATH,
            on_update=poll_and_publish,  # recompute immediately on any new or corrected price
        )

    def month_close_job() -> None:
        try:
            close_month(historic_path=HISTORIC_PATH, rcem_history_path=RCEM_HISTORY_PATH)
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

    # RCEm: days 11–20 at 09:00, 12:00, 20:00 local time
    scheduler.add_job(rcem_job, CronTrigger(day='11-20', hour='9,12,20', minute=0),
                      id='rcem_scrape', name='RCEm price scrape')

    # RCEm correction scan: 1st of each month at 06:00 — catches skorygowana corrections
    # that appear after the 20th-day retry window (PSE allows corrections up to 12 months later)
    scheduler.add_job(rcem_job, CronTrigger(day=1, hour=6, minute=0),
                      id='rcem_correction_scan', name='RCEm correction scan')

    logger.info('PV ROI Tracker v%s started — poll every %d min', __version__, POLL_INTERVAL)

    # Startup scan: fetch full PSE table to apply any new prices or skorygowana corrections
    # accumulated since the last run. No on_update callback — poll_and_publish() follows immediately.
    rcem_scraper.run_scheduled_scrape(
        history_path=RCEM_HISTORY_PATH,
        historic_json_path=HISTORIC_PATH,
    )

    poll_and_publish()   # initial poll with up-to-date RCEm history
    scheduler.start()


if __name__ == '__main__':
    main()
