"""
Cross-family production plausibility check (v0.35.0).

An earlier version of this module computed two residuals —
R1 = produced - exported - self_consumed and
R2 = consumed - self_consumed - purchased — and treated them as an energy
balance proof. That was wrong: on this installation there is no independent
whole-house meter. Reading the actual HA templates,

    house_consumption_energy_total = inverter_total_yield - power_meter_exported + power_meter_consumption
    inverter_yield_self_use_total   = inverter_total_yield - power_meter_exported

both self_consumed_kwh and consumed_kwh are *algebraically derived* from the
same three primitives (production, export, import) that live_reader.py
already uses to build produced_kwh/exported_kwh/purchased_kwh. Once
self_consumed_kwh = produced - exported and consumed_kwh = self_consumed +
imported by construction, R1 and R2 are tautologically zero — that was never
evidence of correctness, just algebra restating itself. See
docs/pv_roi_energy_rebase for the full correction.

The one signal on this system that DOES carry information: there are TWO
independent, differently-sourced solar-production tracking chains —
  dashboard family: sensor.energy_pv (whatever HA's Energy Dashboard is
                    actually configured to use — see
                    live_reader.get_energy_dashboard_sources())
  template family:  sensor.inverter_total_yield (the Huawei integration's own
                    native lifetime register)
These genuinely disagree by a few percent most months (measured: ~0.6-6.5%
across 2025-2026 samples) because they integrate different underlying power
readings. A LARGE disagreement beyond that normal drift is worth an
operator's attention — not proof that either family is "wrong," but a signal
that something changed (sensor reconfigured, integration reloaded, a
correction factor recalibrated, etc.) and is worth a look.
"""
from __future__ import annotations

from typing import Optional

from .models import MonthlyRecord

# Normal dashboard-vs-template drift observed on this installation across
# sampled 2025-2026 months was 0.6-6.5%; alert well above that so routine
# drift doesn't page anyone.
ALERT_TOLERANCE_PCT = 10.0


def compute_balance(record: MonthlyRecord) -> dict:
    """Compare the dashboard-family produced_kwh against the template-family
    cross_family_produced_kwh for the same month.

    Returns {'diff_kwh', 'diff_pct', 'ok', 'reason'}.
    reason is 'incomplete' (cross_family_produced_kwh wasn't computed for
    this record — e.g. a pre-0.35.0 record, or the extra LTS fetch failed —
    not itself an error), 'breach' (|diff_pct| > ALERT_TOLERANCE_PCT), or 'ok'.
    """
    if record.produced_kwh is None or record.cross_family_produced_kwh is None:
        return {'diff_kwh': None, 'diff_pct': None, 'ok': True, 'reason': 'incomplete'}

    diff_kwh = record.produced_kwh - record.cross_family_produced_kwh
    diff_pct = (abs(diff_kwh) / record.produced_kwh * 100.0) if record.produced_kwh > 0 else 0.0
    breach = diff_pct > ALERT_TOLERANCE_PCT
    return {
        'diff_kwh': round(diff_kwh, 3),
        'diff_pct': round(diff_pct, 2),
        'ok': not breach,
        'reason': 'breach' if breach else 'ok',
    }


def residual_kwh(record: MonthlyRecord) -> Optional[float]:
    """Single scalar for storage on the record — |diff_kwh| between the two
    production families, or None if the cross-family figure wasn't computed."""
    b = compute_balance(record)
    if b['diff_kwh'] is None:
        return None
    return abs(b['diff_kwh'])


def check_all(records: list[MonthlyRecord]) -> dict:
    """Scan a full record list for cross-family production breaches — used
    by the health job.

    Returns {'ok': bool, 'breaches': [{'ym', 'diff_kwh', 'diff_pct'}]}.
    """
    breaches = []
    for r in records:
        b = compute_balance(r)
        if b['reason'] == 'breach':
            breaches.append({
                'ym': f'{r.year}-{r.month:02d}',
                'diff_kwh': b['diff_kwh'],
                'diff_pct': b['diff_pct'],
            })
    return {'ok': not breaches, 'breaches': breaches}
