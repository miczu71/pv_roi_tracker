"""
Live HA data reader — Phase 1 stub.
Phase 3 will replace this with real Supervisor REST API calls.
"""
from __future__ import annotations
from typing import Optional
from .models import MonthlyRecord


def read_current_month(rcem_price: Optional[float] = None) -> Optional[MonthlyRecord]:
    """Return None until Phase 3 wires up the real HA Supervisor REST reader."""
    return None
