"""
Tariff comparison: G12w (actual) vs Dynamic (HA-simulated via hourly RCE).

G12w costs: HA Statistics for sensor.koszt_zmienny_g12w_miesieczny
            (utility_meter accumulating monthly G12w variable cost).
Dynamic costs: HA Statistics for sensor.symulacja_miesieczna_dynamicznej_faktura
               (accumulated from calkowity_koszt_1_kwh_dynamiczna × energy every 15 min).

Both series are fetched via WebSocket (recorder/statistics_during_period).
Production/self-consumption context still comes from the add-on's own records.

NOTE: RCEm is NOT used here. RCEm is the prosumer SELL price for excess PV — not the
dynamic tariff BUY price.
"""
from __future__ import annotations

import statistics as _stats
from datetime import date, datetime, timezone
from itertools import groupby
from operator import itemgetter
from typing import Optional

from .models import MonthlyRecord

FIXED_GROSS_PLN = round((4.56 + 10.86 + 24.05) * 1.23, 2)  # 48.55 PLN/month, same for all tariffs

_MONTHS_PL = ['', 'Sty', 'Lut', 'Mar', 'Kwi', 'Maj', 'Cze',
               'Lip', 'Sie', 'Wrz', 'Paź', 'Lis', 'Gru']


def _ym(year: int, month: int) -> str:
    return f'{year}-{month:02d}'


def _month_label(year: int, month: int) -> str:
    return f'{year}-{_MONTHS_PL[month]}'


def _season(month: int) -> str:
    return 'summer' if 4 <= month <= 9 else 'winter'


def _merge_7d_series(g12w_raw: list, dyn_raw: list, diff_daily_raw: list) -> dict:
    """
    Merge raw HA history [{t: iso_str, v: float}] series into aligned chart data.
    Returns {'labels', 'g12w', 'dynamic', 'diff_daily_labels', 'diff_daily'}.
    """
    # Price chart: merge G12w + Dynamic into aligned labels/values
    all_pts: dict = {}
    for p in g12w_raw:
        all_pts.setdefault(p['t'], {})['g12w'] = p['v']
    for p in dyn_raw:
        all_pts.setdefault(p['t'], {})['dynamic'] = p['v']

    sorted_ts = sorted(all_pts.keys())
    last_g12w: Optional[float] = None
    last_dyn: Optional[float] = None
    labels, g12w_vals, dyn_vals = [], [], []

    for ts in sorted_ts:
        pt = all_pts[ts]
        if 'g12w' in pt:
            last_g12w = pt['g12w']
        if 'dynamic' in pt:
            last_dyn = pt['dynamic']
        try:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            label = dt.strftime('%d.%m %H:%M')
        except Exception:
            label = ts[:16]
        labels.append(label)
        g12w_vals.append(last_g12w)
        dyn_vals.append(last_dyn)

    # Downsample to ≤336 points (15-min intervals × 7 days)
    if len(labels) > 336:
        step = max(1, len(labels) // 336)
        labels = labels[::step]
        g12w_vals = g12w_vals[::step]
        dyn_vals = dyn_vals[::step]

    # Daily diff chart: group by date, take last value per day
    daily: dict = {}
    for p in diff_daily_raw:
        try:
            dt = datetime.fromisoformat(p['t'].replace('Z', '+00:00'))
            day_key = dt.strftime('%d.%m')
            daily[day_key] = p['v']
        except Exception:
            pass
    sorted_days = sorted(daily.keys())
    diff_labels = sorted_days
    diff_vals = [daily[d] for d in sorted_days]

    return {
        'labels': labels,
        'g12w': g12w_vals,
        'dynamic': dyn_vals,
        'diff_daily_labels': diff_labels,
        'diff_daily': diff_vals,
    }


def compute_tariff_tab(
    records: list,
    dynamic_monthly_stats: dict,
    g12w_monthly_stats: dict,
    current_roi,
    current_month_live: dict,
    tariff_history_7d: dict,
) -> dict:
    """
    Build the full tariff comparison payload for the frontend tab.

    Args:
        records: All MonthlyRecord objects (historic + live current month) — used
                 for PV production/self-consumption context, NOT for tariff costs.
        dynamic_monthly_stats: {YYYY-MM: float} variable PLN cost from HA Stats
                               (sensor.symulacja_miesieczna_dynamicznej_faktura).
        g12w_monthly_stats: {YYYY-MM: float} variable PLN cost from HA Stats
                            (sensor.koszt_zmienny_g12w_miesieczny).
        current_roi: RoiResult from roi.calculate()
        current_month_live: Live sensor values fetched from HA
        tariff_history_7d: {entity_id: [{t, v}]} — raw 7-day history series
    """
    today = date.today()
    current_ym_str = _ym(today.year, today.month)

    # Build per-month comparison rows.
    # Both tariff costs come from HA Statistics (WebSocket); records supply
    # PV production/self-consumption context only.
    months_out = []
    for r in sorted(records, key=lambda x: (x.year, x.month)):
        ym = _ym(r.year, r.month)
        dyn_var  = dynamic_monthly_stats.get(ym)
        g12w_var = g12w_monthly_stats.get(ym)

        if g12w_var is None or dyn_var is None:
            continue  # need both data points

        diff = round(g12w_var - dyn_var, 2)  # positive = dynamic cheaper

        sc_savings = r.self_consumed_savings_pln or 0.0
        total_value = g12w_var + sc_savings
        pv_offset_pct = round(sc_savings / total_value * 100, 1) if total_value > 0 else 0.0

        months_out.append({
            'ym': ym,
            'month_label': _month_label(r.year, r.month),
            'is_current': ym == current_ym_str,
            'g12w_variable_pln': round(g12w_var, 2),
            'dynamic_variable_pln': round(dyn_var, 2),
            'diff_pln': diff,
            'produced_kwh': r.produced_kwh or 0.0,
            'purchased_kwh': r.purchased_kwh or 0.0,
            'self_consumption_savings_pln': round(sc_savings, 2),
            'pv_offset_pct': pv_offset_pct,
            'season': _season(r.month),
        })

    n = len(months_out)
    n_warning = n < 3
    diffs = [m['diff_pln'] for m in months_out]

    avg_savings = round(_stats.mean(diffs), 2) if diffs else 0.0
    stddev = round(_stats.stdev(diffs), 2) if len(diffs) >= 2 else 0.0
    projected_annual = round(avg_savings * 12, 2)
    projected_pessimistic = round((avg_savings - stddev) * 12, 2)
    projected_optimistic = round((avg_savings + stddev) * 12, 2)

    summer_diffs = [m['diff_pln'] for m in months_out if m['season'] == 'summer']
    winter_diffs = [m['diff_pln'] for m in months_out if m['season'] == 'winter']
    summer_avg = round(_stats.mean(summer_diffs), 2) if summer_diffs else None
    winter_avg = round(_stats.mean(winter_diffs), 2) if winter_diffs else None

    months_dyn_cheaper = sum(1 for d in diffs if d > 0)
    pct_dyn_cheaper = round(months_dyn_cheaper / n * 100, 1) if n > 0 else 0.0

    # Recommendation
    if n_warning:
        recommendation = 'BRAK DANYCH'
        rec_reason = f'Za mało danych ({n} mies.) — potrzeba min. 3 miesięcy'
    elif avg_savings > 20:
        recommendation = 'ZMIEŃ'
        rec_reason = f'Dynamiczna tańsza średnio {avg_savings:.0f} PLN/mies. ({pct_dyn_cheaper:.0f}% czasu)'
    elif avg_savings < -5:
        recommendation = 'ZOSTAŃ'
        rec_reason = f'G12w tańsza średnio o {abs(avg_savings):.0f} PLN/mies.'
    else:
        recommendation = 'NEUTRALNA'
        rec_reason = f'Różnica {avg_savings:.0f} PLN/mies. — zbyt mała by uzasadnić zmianę taryfy'

    # Payback impact
    payback_impact_months = None
    new_payback_months = None
    new_payback_date = None
    if current_roi is not None and avg_savings != 0:
        monthly_avg = getattr(current_roi, 'monthly_avg_savings', None)
        payback_months = getattr(current_roi, 'months_to_payback', None)
        payback_date = getattr(current_roi, 'payback_date', None)
        if monthly_avg and monthly_avg > 0 and payback_months:
            impact = round(avg_savings / monthly_avg * payback_months, 1)
            payback_impact_months = impact
            new_months = max(0.0, payback_months - impact)
            new_payback_months = round(new_months, 1)
            if payback_date:
                try:
                    from dateutil.relativedelta import relativedelta
                    target = payback_date + relativedelta(months=int(-impact))
                    new_payback_date = target.isoformat()[:7]
                except Exception:
                    pass

    # Histogram of monthly differences
    buckets = [
        {'label': '< −20', 'lo': None, 'hi': -20},
        {'label': '−20 – 0', 'lo': -20, 'hi': 0},
        {'label': '0 – 20', 'lo': 0, 'hi': 20},
        {'label': '20 – 50', 'lo': 20, 'hi': 50},
        {'label': '50 – 100', 'lo': 50, 'hi': 100},
        {'label': '> 100', 'lo': 100, 'hi': None},
    ]
    for b in buckets:
        b['count'] = sum(
            1 for d in diffs
            if (b['lo'] is None or d >= b['lo']) and (b['hi'] is None or d < b['hi'])
        )

    # Sensitivity: what if G12w rate changes
    sensitivity_g12w = []
    for pct_change in (-20, -10, 0, 10, 20):
        factor = 1 + pct_change / 100
        adj = [m['g12w_variable_pln'] * factor - m['dynamic_variable_pln'] for m in months_out]
        adj_avg = round(_stats.mean(adj) * 12, 0) if adj else 0.0
        sensitivity_g12w.append({
            'label': f'G12w {("+" if pct_change >= 0 else "")}{pct_change}%',
            'pct_change': pct_change,
            'projected_annual': adj_avg,
        })

    # PV context
    avg_purchased = round(_stats.mean([m['purchased_kwh'] for m in months_out]), 1) if months_out else 0.0
    avg_produced = round(_stats.mean([m['produced_kwh'] for m in months_out]), 1) if months_out else 0.0
    avg_sc = round(_stats.mean([m['self_consumption_savings_pln'] for m in months_out]), 2) if months_out else 0.0
    avg_g12w = round(_stats.mean([m['g12w_variable_pln'] for m in months_out]), 2) if months_out else 0.0
    pv_reduces = round(avg_sc / (avg_g12w + avg_sc) * 100, 1) if (avg_g12w + avg_sc) > 0 else 0.0

    # Merge 7d history for charts
    g12w_7d = tariff_history_7d.get('sensor.power_tauron_g12w_current_price', [])
    dyn_7d = tariff_history_7d.get('sensor.calkowity_koszt_1_kwh_dynamiczna', [])
    diff_7d = tariff_history_7d.get('sensor.roznica_dzienna_g12w_vs_dynamiczna', [])
    chart_7d = _merge_7d_series(g12w_7d, dyn_7d, diff_7d)

    return {
        'months': months_out,
        'summary': {
            'n_months': n,
            'n_months_warning': n_warning,
            'avg_monthly_savings_pln': avg_savings,
            'savings_stddev_pln': stddev,
            'projected_annual_pln': projected_annual,
            'projected_annual_pessimistic': projected_pessimistic,
            'projected_annual_optimistic': projected_optimistic,
            'summer_avg_pln': summer_avg,
            'winter_avg_pln': winter_avg,
            'months_dynamic_cheaper': months_dyn_cheaper,
            'months_total': n,
            'pct_dynamic_cheaper': pct_dyn_cheaper,
            'recommendation': recommendation,
            'recommendation_reason': rec_reason,
            'payback_impact_months': payback_impact_months,
            'new_payback_months': new_payback_months,
            'new_payback_date': new_payback_date,
            'fixed_gross_pln': FIXED_GROSS_PLN,
            'histogram': [{'label': b['label'], 'count': b['count']} for b in buckets],
        },
        'sensitivity_g12w': sensitivity_g12w,
        'current_month': current_month_live,
        'chart_7d': chart_7d,
        'pv_context': {
            'avg_purchased_kwh': avg_purchased,
            'avg_produced_kwh': avg_produced,
            'avg_self_consumption_savings_pln': avg_sc,
            'pv_reduces_tariff_benefit_pct': pv_reduces,
            'note': (
                f'Twoja autokonsumpcja PV ({avg_sc:.0f} PLN/mies. śr.) pochłania '
                f'{pv_reduces:.0f}% potencjalnej wartości zakupu z sieci. '
                'Im wyższa autokonsumpcja, tym mniejsza różnica między taryfami.'
                ' Koszty dynamiczne oparte na godzinowych cenach RCE (nie RCEm — RCEm to cena sprzedaży nadwyżki PV).'
            ),
        },
    }


def compute_range_data(
    dyn: dict,
    g12w: dict,
    period: str,
) -> dict:
    """
    Compute KPIs and chart series for an arbitrary date range and granularity.

    Args:
        dyn:    {key: float} — variable PLN cost for Dynamic tariff.
                key format: 'YYYY-MM-DD' (day), 'YYYY-MM' (month), 'YYYY-MM-DDTHH' (hour).
        g12w:   {key: float} — variable PLN cost for G12w tariff.
        period: 'day' | 'month' | 'hour' (used only for label formatting).

    Returns dict with 'kpis' and 'series' keys.
    """
    # Align on common keys, sorted chronologically
    common_keys = sorted(set(dyn.keys()) & set(g12w.keys()))
    if not common_keys:
        return {'kpis': {'n_periods': 0}, 'series': {
            'labels': [], 'g12w': [], 'dynamic': [], 'diff': [], 'cumulative': []
        }}

    g12w_vals = [g12w[k] for k in common_keys]
    dyn_vals  = [dyn[k]  for k in common_keys]
    diffs     = [round(g - d, 2) for g, d in zip(g12w_vals, dyn_vals)]

    n = len(diffs)
    n_dyn_cheaper = sum(1 for d in diffs if d > 0)
    pct_dyn_cheaper = round(n_dyn_cheaper / n * 100, 1) if n else 0.0
    avg_diff    = round(_stats.mean(diffs), 2) if diffs else 0.0
    median_diff = round(_stats.median(diffs), 2) if diffs else 0.0

    max_idx = diffs.index(max(diffs))
    min_idx = diffs.index(min(diffs))
    best_period  = {'date': common_keys[max_idx], 'diff_pln': diffs[max_idx]}
    worst_period = {'date': common_keys[min_idx], 'diff_pln': diffs[min_idx]}

    # Longest streak of consecutive periods where dynamic is cheaper (diff > 0)
    longest_streak = 0
    current_streak = 0
    for d in diffs:
        if d > 0:
            current_streak += 1
            longest_streak = max(longest_streak, current_streak)
        else:
            current_streak = 0

    # Cumulative savings series
    cumulative: list[float] = []
    running = 0.0
    for d in diffs:
        running = round(running + d, 2)
        cumulative.append(running)

    # Human-readable labels
    def _fmt_label(k: str) -> str:
        if period == 'day' and len(k) == 10:
            try:
                return datetime.strptime(k, '%Y-%m-%d').strftime('%d.%m.%y')
            except Exception:
                return k
        if period == 'month' and len(k) == 7:
            try:
                dt = datetime.strptime(k, '%Y-%m')
                return f"{dt.year}-{_MONTHS_PL[dt.month]}"
            except Exception:
                return k
        return k

    labels = [_fmt_label(k) for k in common_keys]

    return {
        'kpis': {
            'n_periods':           n,
            'n_dyn_cheaper':       n_dyn_cheaper,
            'pct_dyn_cheaper':     pct_dyn_cheaper,
            'avg_diff_pln':        avg_diff,
            'median_diff_pln':     median_diff,
            'best_period':         best_period,
            'worst_period':        worst_period,
            'longest_dyn_streak':  longest_streak,
            'g12w_total_pln':      round(sum(g12w_vals), 2),
            'dyn_total_pln':       round(sum(dyn_vals), 2),
            'cumulative_savings_pln': cumulative[-1] if cumulative else 0.0,
        },
        'series': {
            'labels':     labels,
            'g12w':       g12w_vals,
            'dynamic':    dyn_vals,
            'diff':       diffs,
            'cumulative': cumulative,
        },
    }
