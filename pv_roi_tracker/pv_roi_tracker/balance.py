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

Root cause of the normal drift itself (confirmed 2026-08-03): sensor.energy_pv
is a `platform: integration, method: left` Riemann sum of
sensor.input_power_with_efficiency_loss, which scales the inverter's raw DC
input power by 0.90/0.95/0.98 depending on power band before integrating.
sensor.inverter_total_yield is the inverter's own native lifetime register —
no such scaling. At low irradiance, production sits mostly in the low-power
(x0.90/x0.95) bands, so energy_pv structurally under-reads versus
inverter_total_yield, worst in winter months. Lifetime confirmation:
total_produced_kwh 21746.6 vs sensor.inverter_total_yield 22192.64 over
2023-06..2026-07 = -446 kWh (-2.0%), concentrated in low-production months.
This is expected sensor-chain behavior, not a data fault — see check_all()'s
`reconciled` parameter and ALERT_MIN_ABS_KWH below for why it must not page
anyone once a month is small.
"""
from __future__ import annotations

from typing import Optional

from .models import MonthlyRecord

# Normal dashboard-vs-template drift observed on this installation across
# sampled 2025-2026 months was 0.6-6.5%; alert well above that so routine
# drift doesn't page anyone.
ALERT_TOLERANCE_PCT = 10.0

# A purely relative threshold false-positives on low-production months: the
# same ~30-85 kWh absolute gap that is <10% in a ~850 kWh summer month is
# >10% in a ~220 kWh winter month, purely because the x0.90/x0.95
# efficiency-loss bands (see module docstring) apply to a larger share of a
# low-irradiance month's energy. Confirmed 2026-08-03: all 7 months that
# broke the pure-percent rule (2023-06, 2025-01, 2025-10/11/12, 2026-01/02)
# were 174-368 kWh months with 30-85 kWh absolute gaps — routine drift, not
# a signal worth surfacing. Require both conditions before alerting.
ALERT_MIN_ABS_KWH = 100.0


def compute_balance(record: MonthlyRecord) -> dict:
    """Compare the dashboard-family produced_kwh against the template-family
    cross_family_produced_kwh for the same month.

    Returns {'diff_kwh', 'diff_pct', 'ok', 'reason'}.
    reason is 'incomplete' (cross_family_produced_kwh wasn't computed for
    this record — e.g. a pre-0.35.0 record, or the extra LTS fetch failed —
    not itself an error), 'breach' (|diff_pct| > ALERT_TOLERANCE_PCT AND
    |diff_kwh| > ALERT_MIN_ABS_KWH), or 'ok'.
    """
    if record.produced_kwh is None or record.cross_family_produced_kwh is None:
        return {'diff_kwh': None, 'diff_pct': None, 'ok': True, 'reason': 'incomplete'}

    diff_kwh = record.produced_kwh - record.cross_family_produced_kwh
    diff_pct = (abs(diff_kwh) / record.produced_kwh * 100.0) if record.produced_kwh > 0 else 0.0
    breach = diff_pct > ALERT_TOLERANCE_PCT and abs(diff_kwh) > ALERT_MIN_ABS_KWH
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


def check_all(records: list[MonthlyRecord],
              reconciled: Optional[set[tuple[int, int]]] = None) -> dict:
    """Scan a full record list for cross-family production breaches — used
    by the health job.

    reconciled: (year, month) keys that are invoice-reconciled — mirrors
    main.py's _heal_action() rule that a reconciled month's balance breach
    is never grounds to touch the record (the invoice is final, see
    rebase.py). Without this, the health job would stay 'degraded' forever
    on months nothing will ever repair (see docs/BLUEPRINT.md 0.35.3). None
    (default) checks every record, matching pre-0.35.3 behavior.

    Returns {'ok': bool, 'breaches': [{'ym', 'diff_kwh', 'diff_pct'}]}.
    """
    reconciled = reconciled or set()
    breaches = []
    for r in records:
        if (r.year, r.month) in reconciled:
            continue
        b = compute_balance(r)
        if b['reason'] == 'breach':
            breaches.append({
                'ym': f'{r.year}-{r.month:02d}',
                'diff_kwh': b['diff_kwh'],
                'diff_pct': b['diff_pct'],
            })
    return {'ok': not breaches, 'breaches': breaches}
