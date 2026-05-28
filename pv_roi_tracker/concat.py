from __future__ import annotations
from typing import Optional
from .models import MonthlyRecord


def concat(historic: list[MonthlyRecord], current: Optional[MonthlyRecord]) -> list[MonthlyRecord]:
    """
    Merge the frozen historic list with the volatile current-month record.
    The current month wins if its key already exists in historic (e.g. after force re-import).
    """
    by_key = {r.key(): r for r in historic}
    if current is not None:
        by_key[current.key()] = current
    return sorted(by_key.values(), key=lambda r: (r.year, r.month))
