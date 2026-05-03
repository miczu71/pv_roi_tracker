"""
Month-close job.

Fires at 23:55 on the last day of each month (before utility meters reset at midnight).
Reads live HA values for the current month and appends them to historic.json.
feedin_revenue_pln is left null if RCEm is not yet published; backfill_rcem() fills it later.
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def close_month(
    historic_path: Optional[Path] = None,
    rcem_history_path: Optional[Path] = None,
) -> bool:
    """
    Snapshot the current month (date.today()) into historic.json.
    Returns True if a new entry was appended, False if already present.
    """
    from . import historic_store, live_reader, rcem_scraper

    if historic_path is None:
        historic_path = historic_store.DEFAULT_PATH
    if rcem_history_path is None:
        rcem_history_path = rcem_scraper.DEFAULT_HISTORY_PATH

    today = date.today()
    logger.info('Month-close: snapshotting %d-%02d into historic.json', today.year, today.month)

    # Current month's RCEm won't be published until the 11th of next month.
    # Pass None — the scraper will backfill it when available.
    rcem_price = rcem_scraper.get_current_month_rcem(rcem_history_path)

    record = live_reader.read_current_month(rcem_price=rcem_price)
    if record is None:
        logger.warning('Month-close: live_reader returned None — snapshot skipped for %d-%02d',
                       today.year, today.month)
        return False

    appended = historic_store.append_month(record, historic_path)
    if appended:
        logger.info('Month-close: %d-%02d appended (rcem_status=%s)', today.year, today.month, record.rcem_status)
    else:
        logger.info('Month-close: %d-%02d already in historic.json — skipped', today.year, today.month)
    return appended
