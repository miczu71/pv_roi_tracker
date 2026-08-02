"""
Dry-run simulation + apply for rebasing historic.json onto lifetime-meter
kWh figures (v0.35.0). See docs/pv_roi_energy_rebase (plan) for the audit
that motivated this: sensor.house_consumption_energy_monthly reads
produced+imported instead of real house consumption (+82% for July 2026),
and self_consumed_kwh was a derived residual (max(0, produced-exported))
rather than a measurement.

Two-step workflow — nothing is written until apply() is explicitly called:
  simulate() — for every month that already has data, refetch it via
               live_reader.read_month_from_statistics (lifetime meters) and
               diff every kWh field plus the ROI headline figures.
  apply()    — snapshot historic.json, then write the rebased records,
               leaving tariff/rcem_status/projected_* untouched exactly as
               replace_month() already does.

Invoice-reconciled months are frozen, not merely field-protected: once a
month has a reconciled invoice, that record is the final word — including
produced_kwh/battery_*/specific_yield, fields an invoice never bills but
which the old design still let the LTS rebuild overwrite. The user made
this explicit: "for the past data that exists on the invoices and is
reconciled - I want to keep it as final data, even if it's slightly off
from inverter data. invoice should be always final." So reconciled months
skip the full multi-entity LTS refetch entirely and pass through byte-for-
byte, except for a cheap single-entity refresh of cross_family_produced_kwh
(and the balance_residual_kwh derived from it) — a read-only diagnostic
overlay, never a billed or produced figure, so refreshing it doesn't
compromise finality.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import balance, historic_store, roi
from .models import MonthlyRecord

logger = logging.getLogger(__name__)

FetchMonth = Callable[[int, int], Optional[MonthlyRecord]]
FetchCrossFamily = Callable[[int, int], Optional[float]]

# Fields the LTS rebuild is allowed to overwrite for non-reconciled months.
# Everything else on the old row (tariff, rcem_status, projected_month_kwh,
# projected_month_savings_pln, feedin_price_pln_kwh, rcem_status) passes
# through untouched. Reconciled months never reach this list at all — see
# module docstring.
_MERGE_FIELDS = (
    'produced_kwh', 'exported_kwh', 'purchased_kwh', 'purchased_kwh_peak',
    'purchased_kwh_offpeak', 'consumed_kwh', 'self_consumed_kwh',
    'self_consumed_savings_pln', 'purchase_cost_pln', 'feedin_revenue_pln',
    'buy_price_pln_kwh', 'specific_yield', 'battery_charge_kwh',
    'battery_discharge_kwh', 'self_consumed_source', 'balance_residual_kwh',
    'source', 'cross_family_produced_kwh',
)


def _default_fetch(year: int, month: int) -> Optional[MonthlyRecord]:
    from . import live_reader
    return live_reader.read_month_from_statistics(year, month)


def _default_fetch_cross_family(year: int, month: int) -> Optional[float]:
    from . import live_reader
    return live_reader.fetch_cross_family_produced(year, month)


def _merge_month(old: dict, new: MonthlyRecord) -> dict:
    """Build the rebased dict for a non-reconciled month: start from the old
    row (preserves tariff/rcem_status/projected_* as-is) and overlay the LTS
    rebuild's kWh fields. Never called for reconciled months — those are
    frozen wholesale in _build() instead."""
    merged = dict(old)
    new_dict = new.to_dict()
    for f in _MERGE_FIELDS:
        merged[f] = new_dict.get(f)
    return merged


def _freeze_month(old: dict, cross_family_produced_kwh: Optional[float]) -> dict:
    """Build the rebased dict for an invoice-reconciled month: every field
    passes through unchanged except cross_family_produced_kwh (a read-only
    diagnostic, refreshed cheaply) and the balance_residual_kwh derived from
    it. Nothing billed, produced, or stored-as-final is touched."""
    merged = dict(old)
    if cross_family_produced_kwh is not None:
        merged['cross_family_produced_kwh'] = cross_family_produced_kwh
        merged['balance_residual_kwh'] = balance.residual_kwh(MonthlyRecord.from_dict(merged))
    return merged


def _roi_headline(res) -> dict:
    return {
        'total_savings': res.total_savings,
        'roi_pct': res.roi_pct,
        'npv': res.npv,
        'irr_pct': res.irr_pct,
        'payback_date': res.payback_date.isoformat() if res.payback_date else None,
        'autarky_pct': res.autarky_pct,
        'self_consumption_rate_pct': res.self_consumption_rate_pct,
        'total_produced_kwh': res.total_produced_kwh,
        'total_exported_kwh': res.total_exported_kwh,
    }


def _build(
    records: list[MonthlyRecord],
    reconciled_months: set,
    fetch_month: FetchMonth,
    fetch_cross_family: FetchCrossFamily,
    roi_kwargs: dict,
) -> tuple[dict, list[dict]]:
    """Shared core for simulate()/apply(). Returns (report, rebased_month_dicts)."""
    months_out = []
    unavailable = []
    still_broken = []
    rebased_dicts: list[dict] = []
    rebased_records: list[MonthlyRecord] = []

    for r in sorted(records, key=lambda rec: (rec.year, rec.month)):
        old_dict = r.to_dict()
        if not historic_store.has_energy_data(r.produced_kwh):
            rebased_dicts.append(old_dict)
            rebased_records.append(r)
            continue

        is_reconciled = (r.year, r.month) in reconciled_months
        if is_reconciled:
            merged_dict = _freeze_month(old_dict, fetch_cross_family(r.year, r.month))
        else:
            new = fetch_month(r.year, r.month)
            if new is None:
                unavailable.append(f'{r.year}-{r.month:02d}')
                rebased_dicts.append(old_dict)
                rebased_records.append(r)
                continue
            merged_dict = _merge_month(old_dict, new)

        merged = MonthlyRecord.from_dict(merged_dict)
        rebased_dicts.append(merged_dict)
        rebased_records.append(merged)

        before = {f: getattr(r, f) for f in _MERGE_FIELDS}
        after = {f: getattr(merged, f) for f in _MERGE_FIELDS}
        delta = {
            f: round(after[f] - before[f], 3)
            for f in _MERGE_FIELDS
            if isinstance(before.get(f), (int, float)) and isinstance(after.get(f), (int, float))
        }
        months_out.append({'ym': f'{r.year}-{r.month:02d}', 'before': before,
                          'after': after, 'delta': delta, 'frozen': is_reconciled})

        b = balance.compute_balance(merged)
        if b['reason'] == 'breach':
            still_broken.append(f'{r.year}-{r.month:02d}')

    roi_before = roi.calculate(records, **roi_kwargs)
    roi_after = roi.calculate(rebased_records, **roi_kwargs)

    report = {
        'months': months_out,
        'unavailable': unavailable,
        'still_broken': still_broken,
        'roi_before': _roi_headline(roi_before),
        'roi_after': _roi_headline(roi_after),
    }
    return report, rebased_dicts


def simulate(
    records: list[MonthlyRecord],
    reconciled_months: Optional[set] = None,
    fetch_month: Optional[FetchMonth] = None,
    fetch_cross_family: Optional[FetchCrossFamily] = None,
    roi_kwargs: Optional[dict] = None,
) -> dict:
    """Read-only dry run — never touches historic.json.

    Returns {'months': [{'ym', 'before', 'after', 'delta', 'frozen'}],
    'unavailable': [...], 'still_broken': [...], 'roi_before': {...},
    'roi_after': {...}}. 'unavailable' months had no LTS data yet and were
    left untouched. 'still_broken' months fail the balance check even after
    rebase — they need investigation, not blind acceptance of the rebuild.
    'frozen' months are invoice-reconciled: only cross_family_produced_kwh/
    balance_residual_kwh were refreshed, every other field is byte-for-byte
    the old value (see module docstring — invoice is always final).
    """
    report, _ = _build(records, reconciled_months or set(), fetch_month or _default_fetch,
                       fetch_cross_family or _default_fetch_cross_family, roi_kwargs or {})
    return report


def apply(
    path: Path = historic_store.DEFAULT_PATH,
    reconciled_months: Optional[set] = None,
    fetch_month: Optional[FetchMonth] = None,
    fetch_cross_family: Optional[FetchCrossFamily] = None,
    roi_kwargs: Optional[dict] = None,
) -> dict:
    """Snapshot historic.json, then write the rebased records in place.

    roi_kwargs: passed through to roi.calculate() for the report's
    roi_before/roi_after (gross_investment, subsidy, etc.) — omitting this
    silently falls back to roi.py's module defaults, which will disagree
    with the add-on's actual configured options if the user has changed
    them from config.yaml's defaults.

    Returns the same shape as simulate() plus 'snapshot_path'.
    """
    doc = historic_store._load_document(path)
    records = [MonthlyRecord.from_dict(m) for m in doc.get('months', [])]

    report, rebased_dicts = _build(records, reconciled_months or set(),
                                   fetch_month or _default_fetch,
                                   fetch_cross_family or _default_fetch_cross_family,
                                   roi_kwargs or {})

    snapshot_path = None
    if path.exists():
        snapshot_path = path.with_name(
            f"{path.stem}.pre-rebase-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json")
        snapshot_path.write_text(path.read_text(encoding='utf-8'), encoding='utf-8')

    doc['months'] = rebased_dicts
    historic_store._save_document(doc, path)
    logger.info('Rebase applied: %d month(s) touched, %d unavailable, snapshot at %s',
               len(report['months']), len(report['unavailable']), snapshot_path)

    report['snapshot_path'] = str(snapshot_path) if snapshot_path else None
    return report
