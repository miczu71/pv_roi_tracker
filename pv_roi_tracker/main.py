"""
Add-on steady-state entry point.
Phase 1: imports CSV on first start, then exits (steady-state loop added in Phase 3).
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

HISTORIC_PATH = Path(os.environ.get('HISTORIC_PATH', '/data/historic.json'))
CSV_URL = os.environ.get(
    'CSV_URL',
    'https://docs.google.com/spreadsheets/d/e/'
    '2PACX-1vT_3gmj8s_2JPJzPijb9T27oh0FS3JhlQ3cB1HkKPbBa2yi8GyudqJb5ZeyM20QQ9IKbfB3SnbKZukC'
    '/pub?output=csv',
)
FORCE_REIMPORT = os.environ.get('FORCE_REIMPORT', 'false').lower() == 'true'


def main() -> None:
    from . import historic_store, importer, parser

    if not HISTORIC_PATH.exists() or FORCE_REIMPORT:
        logger.info("Importing historic CSV data from Google Sheets …")
        csv_text = importer.fetch_csv(CSV_URL)
        records = parser.parse_csv(csv_text)
        if not records:
            logger.error("Parser returned zero records — check CSV URL and layout")
            sys.exit(1)
        historic_store.save(records, HISTORIC_PATH)
        logger.info("Import complete: %d monthly records written to %s", len(records), HISTORIC_PATH)
    else:
        logger.info("Historic data already present at %s — skipping import", HISTORIC_PATH)

    # Phase 3 will add the APScheduler steady-state loop here.
    logger.info("Phase 1 complete. Steady-state loop not yet implemented (Phase 3).")


if __name__ == '__main__':
    main()
