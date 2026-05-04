"""
Add-on entry point — steady-state loop (Phase 3).

Jobs:
  • Poll every POLL_INTERVAL_MINUTES: read historic + live → ROI calc → MQTT publish
  • Month-close: 23:55 on last day of each month (before utility meters reset at midnight)
  • RCEm scrape: day 11–20 of each month at 20:00 UTC (stops retrying once price is found)
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

    # ── Startup RCEm catch-up ─────────────────────────────────────────────────
    # If today >= 11th and last month's RCEm is missing, try immediately.
    from datetime import date
    today = date.today()
    if today.day >= 11:
        rcem_scraper.run_scheduled_scrape(
            history_path=RCEM_HISTORY_PATH,
            historic_json_path=HISTORIC_PATH,
            on_success=lambda p: logger.info('Startup RCEm catch-up: found %.4f PLN/kWh', p),
        )

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
            pub.publish_roi(result, rcem_price=rcem_price)
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
            on_success=lambda _: poll_and_publish(),  # recompute immediately on new price
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

    # RCEm: days 11–20 at 20:00 UTC (no-op if already cached for this cycle)
    scheduler.add_job(rcem_job, CronTrigger(day='11-20', hour=20, minute=0, timezone='UTC'),
                      id='rcem_scrape', name='RCEm price scrape')

    logger.info('PV ROI Tracker v%s started — poll every %d min', __version__, POLL_INTERVAL)

    poll_and_publish()   # run once immediately before entering the scheduler loop
    scheduler.start()


if __name__ == '__main__':
    main()
