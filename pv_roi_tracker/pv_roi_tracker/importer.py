import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_RETRIES = 3
_BACKOFF_SECS = [5, 15, 30]


def fetch_csv(url: str) -> str:
    last_exc: Optional[Exception] = None
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            content = resp.content
            if content.startswith(b'\xef\xbb\xbf'):  # strip UTF-8 BOM
                content = content[3:]
            return content.decode('utf-8')
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < _RETRIES:
                wait = _BACKOFF_SECS[attempt - 1]
                logger.warning(
                    "CSV fetch attempt %d/%d failed: %s — retrying in %ds",
                    attempt, _RETRIES, exc, wait,
                )
                time.sleep(wait)
            else:
                logger.error("CSV fetch failed after %d attempts: %s", _RETRIES, exc)
    raise last_exc
