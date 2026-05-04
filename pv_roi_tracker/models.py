from __future__ import annotations
from dataclasses import dataclass, asdict, field
from typing import Optional


@dataclass
class MonthlyRecord:
    year: int
    month: int  # 1-12

    produced_kwh: Optional[float] = None
    consumed_kwh: Optional[float] = None
    purchased_kwh: Optional[float] = None
    exported_kwh: Optional[float] = None
    self_consumed_kwh: Optional[float] = None
    buy_price_pln_kwh: Optional[float] = None
    feedin_price_pln_kwh: Optional[float] = None
    self_consumed_savings_pln: Optional[float] = None
    purchase_cost_pln: Optional[float] = None
    feedin_revenue_pln: Optional[float] = None
    specific_yield: Optional[float] = None
    rcem_status: str = 'confirmed'  # 'confirmed' | 'pending' | 'missing'
    projected_month_kwh: Optional[float] = None  # Solcast-based projection, current month only

    def key(self) -> tuple[int, int]:
        return (self.year, self.month)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> MonthlyRecord:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})
