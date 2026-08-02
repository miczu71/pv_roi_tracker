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
    purchased_kwh_peak: Optional[float] = None
    purchased_kwh_offpeak: Optional[float] = None
    exported_kwh: Optional[float] = None
    self_consumed_kwh: Optional[float] = None
    buy_price_pln_kwh: Optional[float] = None
    feedin_price_pln_kwh: Optional[float] = None
    self_consumed_savings_pln: Optional[float] = None
    purchase_cost_pln: Optional[float] = None
    feedin_revenue_pln: Optional[float] = None
    specific_yield: Optional[float] = None
    battery_arbitrage_savings_pln: Optional[float] = None  # grid-charged off-peak × 0.50 PLN/kWh
    rcem_status: str = 'confirmed'  # 'confirmed' | 'pending' | 'missing'
    projected_month_kwh: Optional[float] = None           # Solcast-based projection, current month only
    projected_month_savings_pln: Optional[float] = None   # Solcast × historical savings/kWh, current month only
    tariff: Optional[str] = None                           # 'G11' | 'G12W' | None (pre-invoice months)

    # v0.35.0 — schema_version 2. Added when the record's kWh basis moved from
    # monthly utility_meter snapshots to lifetime total_increasing meters,
    # dynamically resolved from HA's own Energy Dashboard configuration (see
    # live_reader.get_energy_dashboard_sources()). All Optional so v1 records
    # deserialize unchanged.
    #
    # self_consumed_kwh/consumed_kwh are ALWAYS derived here (produced-exported,
    # and self_consumed+imported respectively) — this installation has no
    # independent whole-house meter, so there is no "measured" alternative to
    # prefer. self_consumed_source is kept for provenance/future installs that
    # might have a real whole-house meter, not because this one does.
    self_consumed_source: Optional[str] = None   # 'derived' | 'sheet' (see note above)
    balance_residual_kwh: Optional[float] = None  # see balance.py — cross-family plausibility check, not a physical proof
    battery_charge_kwh: Optional[float] = None
    battery_discharge_kwh: Optional[float] = None
    source: Optional[str] = None                 # 'lts' | 'live' | 'invoice' | 'sheet'
    # The OTHER production-tracking family's own figure for the same month
    # (e.g. sensor.inverter_total_yield's own LTS change), fetched purely for
    # the balance.py cross-family plausibility check — never used to compute
    # produced_kwh itself. None if not computed (e.g. pre-0.35.0 records, or
    # the extra LTS fetch failed).
    cross_family_produced_kwh: Optional[float] = None

    def key(self) -> tuple[int, int]:
        return (self.year, self.month)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> MonthlyRecord:
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})
