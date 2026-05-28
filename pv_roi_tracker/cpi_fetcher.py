"""
Monthly Polish CPI from GUS stat.gov.pl — "Poprzedni miesiąc = 100" chain index.

Source: Miesięczne wskaźniki cen towarów i usług konsumpcyjnych od 1982 roku
        https://stat.gov.pl/.../miesieczne_wskazniki_cen_towarow_i_uslug_konsumpcyjnych_od_1982_roku__1.csv

Encoding: cp1250, semicolon delimiter, comma decimal separator.
GUS publishes the previous month's CPI around the 15th of the following month.

Public API:
    bootstrap()       — load cache on startup; trigger background refresh if stale
    refresh()         — scheduled monthly fetch
    get_deflator()    — CPI[to_ym] / CPI[from_ym]; flat fallback for unknown months
    coverage_desc()   — human-readable coverage string for dashboard/logs
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CPI_CSV_URL = (
    "https://stat.gov.pl/download/gfx/portalinformacyjny/pl/defaultstronaopisowa"
    "/4741/1/1/miesieczne_wskazniki_cen_towarow_i_uslug_konsumpcyjnych_od_1982_roku__1.csv"
)
_CACHE_PATH = Path("/data/cpi_history.json")
_STALE_DAYS = 35

_lock = threading.Lock()
_index: dict[str, float] = {}   # {"YYYY-MM": float} — continuous chain index


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ym_key(year: int, month: int) -> str:
    return f"{year}-{month:02d}"


def _prev_ym(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _next_ym(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


# ── Fetch & parse ──────────────────────────────────────────────────────────────

def _fetch_mom() -> dict[str, float]:
    """Download GUS CSV and return MoM values {"YYYY-MM": float (previous month = 100)}."""
    from urllib.request import Request, urlopen
    req = Request(_CPI_CSV_URL, headers={"User-Agent": "pv_roi_tracker/0.9"})
    with urlopen(req, timeout=30) as resp:
        raw = resp.read()
    text = raw.decode("cp1250", errors="replace")
    mom: dict[str, float] = {}
    for line in text.splitlines():
        parts = line.split(";")
        if len(parts) < 6:
            continue
        # "Poprzedni miesiąc = 100" (MoM); the ą may be garbled in cp1250 replacement,
        # so just check the unambiguous start of the string.
        if "Poprzedni miesi" not in parts[2]:
            continue
        try:
            year = int(parts[3].strip())
            month = int(parts[4].strip())
            val_str = parts[5].strip().replace(",", ".")
            if not val_str:
                continue
            val = float(val_str)
        except (ValueError, IndexError):
            continue
        if val > 0:
            mom[_ym_key(year, month)] = val
    return mom


def _build_chain_index(mom: dict[str, float]) -> dict[str, float]:
    """Convert sorted MoM dict to a cumulative chain index.

    The anchor month (month before the first MoM entry) is set to 100.0.
    Each subsequent month's index = previous * (MoM / 100).
    Stops at the first gap in consecutive months (shouldn't occur for GUS data).
    """
    if not mom:
        return {}
    keys = sorted(mom.keys())
    fy, fm = int(keys[0][:4]), int(keys[0][5:])
    ay, am = _prev_ym(fy, fm)
    anchor_k = _ym_key(ay, am)

    index: dict[str, float] = {anchor_k: 100.0}
    cur = 100.0
    prev_k = anchor_k

    for k in keys:
        py, pm = int(prev_k[:4]), int(prev_k[5:])
        ny, nm = _next_ym(py, pm)
        expected = _ym_key(ny, nm)
        if k != expected:
            logger.warning("CPI chain gap: expected %s, got %s — stopping", expected, k)
            break
        cur = cur * (mom[k] / 100.0)
        index[k] = cur
        prev_k = k

    return index


# ── Cache I/O ──────────────────────────────────────────────────────────────────

def _load_cache() -> Optional[dict]:
    try:
        if not _CACHE_PATH.exists():
            return None
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("CPI cache unreadable — will refetch")
        return None


def _save_cache(index: dict[str, float]) -> None:
    try:
        payload = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "GUS stat.gov.pl (Poprzedni miesiąc = 100)",
            "index": index,
        }
        _CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.exception("CPI cache write failed")


# ── In-memory update ──────────────────────────────────────────────────────────

def _apply_index(new_index: dict[str, float]) -> None:
    global _index
    with _lock:
        _index = new_index
    if new_index:
        mn, mx = min(new_index), max(new_index)
        logger.info("CPI index updated: %s..%s (%d entries)", mn, mx, len(new_index))
    else:
        logger.warning("CPI index empty — using flat fallback")


def _do_refresh() -> None:
    try:
        mom = _fetch_mom()
        if not mom:
            logger.warning("CPI fetch: no MoM rows found in CSV")
            return
        new_index = _build_chain_index(mom)
        _apply_index(new_index)
        _save_cache(new_index)
    except Exception:
        logger.exception("CPI refresh failed")


# ── Public API ─────────────────────────────────────────────────────────────────

def bootstrap() -> None:
    """Load cached CPI index; trigger a background refresh if the cache is missing or stale."""
    cache = _load_cache()
    if cache:
        _apply_index(cache.get("index", {}))
        try:
            ts = datetime.fromisoformat(cache.get("fetched_at", ""))
            age_days = (datetime.now(timezone.utc) - ts).days
        except (ValueError, TypeError):
            age_days = _STALE_DAYS + 1
        if age_days <= _STALE_DAYS:
            logger.info("CPI cache age %d days — skipping refresh", age_days)
            return
        logger.info("CPI cache %d days old — refreshing in background", age_days)
    else:
        logger.info("CPI cache missing — fetching in background")
    threading.Thread(target=_do_refresh, daemon=True, name="cpi-refresh").start()


def refresh() -> None:
    """Trigger a background CPI refresh (called by the monthly APScheduler job)."""
    logger.info("CPI scheduled refresh")
    threading.Thread(target=_do_refresh, daemon=True, name="cpi-refresh").start()


def get_deflator(from_ym: tuple[int, int], to_ym: tuple[int, int], fallback_rate: float) -> float:
    """Return the cumulative price-level ratio CPI[to_ym] / CPI[from_ym].

    Dividing nominal savings earned in from_ym by this value gives real savings
    expressed in to_ym purchasing power.

    Falls back to (1 + fallback_rate) ** years for months not yet in the index
    (e.g. the current month before GUS publishes it).
    """
    with _lock:
        idx = _index

    years = ((to_ym[0] - from_ym[0]) * 12 + (to_ym[1] - from_ym[1])) / 12.0
    if years <= 0:
        return 1.0

    fk = _ym_key(*from_ym)
    tk = _ym_key(*to_ym)

    if fk in idx and tk in idx:
        return idx[tk] / idx[fk]

    if fk in idx:
        # to_ym is beyond the covered range — extend from the last known entry with flat rate
        latest_k = max(idx)
        known_ratio = idx[latest_k] / idx[fk]
        ly, lm = int(latest_k[:4]), int(latest_k[5:])
        gap_months = (to_ym[0] - ly) * 12 + (to_ym[1] - lm)
        if gap_months <= 0:
            return known_ratio
        return known_ratio * (1.0 + fallback_rate) ** (gap_months / 12.0)

    # from_ym not in index at all — pure flat fallback
    return (1.0 + fallback_rate) ** years


def coverage_desc() -> str:
    """Return a short human-readable string describing the CPI data coverage."""
    with _lock:
        idx = _index
    if not idx:
        return "flat fallback"
    return f"GUS ({min(idx)}..{max(idx)})"
