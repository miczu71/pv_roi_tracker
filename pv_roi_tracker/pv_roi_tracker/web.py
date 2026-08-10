"""Flask web UI — serves the ROI dashboard on the add-on ingress port."""
from __future__ import annotations

import calendar
import csv
import io
import logging
import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from dateutil.relativedelta import relativedelta
from flask import Flask, Response, jsonify, request

from . import __version__
from .models import MonthlyRecord
from .roi import RoiResult, bill_comparison as _bill_comparison, underperformance_analysis as _underperformance_analysis
from . import tariff_analysis as _tariff_analysis
from . import live_reader as _live_reader
from .tariff_analysis import _month_label

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB per request — bounds batch PDF uploads
log = logging.getLogger(__name__)

_lock = threading.Lock()
_rcem_override_callback = None
_historic_patch_callback = None
_reread_month_callback = None
_invoice_reconcile_callback = None
_invoice_remove_callback = None
_invoice_train_callback = None
_simulate_rebase_callback = None
_apply_rebase_callback = None
_invoice_path = None
_layouts_path = None
_tariff_config_path = None


def set_rcem_override_callback(fn) -> None:
    global _rcem_override_callback
    _rcem_override_callback = fn


def set_historic_patch_callback(fn) -> None:
    global _historic_patch_callback
    _historic_patch_callback = fn


def set_reread_month_callback(fn) -> None:
    global _reread_month_callback
    _reread_month_callback = fn


def set_simulate_rebase_callback(fn) -> None:
    global _simulate_rebase_callback
    _simulate_rebase_callback = fn


def set_apply_rebase_callback(fn) -> None:
    global _apply_rebase_callback
    _apply_rebase_callback = fn


def set_invoice_reconcile_callback(fn) -> None:
    global _invoice_reconcile_callback
    _invoice_reconcile_callback = fn


def set_invoice_path(path) -> None:
    global _invoice_path
    _invoice_path = path


def set_invoice_remove_callback(fn) -> None:
    global _invoice_remove_callback
    _invoice_remove_callback = fn


def set_invoice_train_callback(fn) -> None:
    global _invoice_train_callback
    _invoice_train_callback = fn


def set_layouts_path(path) -> None:
    global _layouts_path
    _layouts_path = path


def set_tariff_config_path(path) -> None:
    global _tariff_config_path
    _tariff_config_path = path


_battery_config_path = None
_battery_config_callback = None


def set_battery_config_path(path) -> None:
    global _battery_config_path
    _battery_config_path = path


def set_battery_config_callback(fn) -> None:
    """fn(BatteryConfig) — zapisuje konfigurację i przelicza symulację magazynu."""
    global _battery_config_callback
    _battery_config_callback = fn


def _load_tariff_cfg() -> dict:
    from . import tariff_config as _tc
    try:
        return _tc.load(_tariff_config_path) if _tariff_config_path else _tc._empty()
    except Exception:
        return _tc._empty()


def _load_real_billing() -> dict:
    if _invoice_path is None:
        return {}
    try:
        from . import invoice_store as _is
        return _is.filter_billing(_is.load_real(_invoice_path))
    except Exception:
        return {}


_state: dict = {
    'result': None,
    'records': [],
    'rcem_price': None,
    'month_closed': False,
    'rcem_scrape_status': None,
    'updated_at': None,
    'tariff_comparison': None,
    'rce_comparison': None,
    'deposit': None,
    'battery_sim': None,
    'lifetime_forecast': None,
}

def update_state(result: RoiResult, records: list[MonthlyRecord],
                 rcem_price: Optional[float], month_closed: bool = False,
                 rcem_scrape_status: Optional[str] = None) -> None:
    with _lock:
        _state['result'] = result
        _state['records'] = list(records)
        _state['rcem_price'] = rcem_price
        _state['month_closed'] = month_closed
        _state['rcem_scrape_status'] = rcem_scrape_status
        _state['updated_at'] = datetime.now().isoformat(timespec='seconds')


def update_tariff_comparison(tariff_data: dict) -> None:
    """Store the computed tariff comparison payload (called from main.py poll loop)."""
    with _lock:
        _state['tariff_comparison'] = tariff_data


def update_rce_comparison(rce_data: dict) -> None:
    """Store the RCEm-vs-hourly-RCE payload (called from main.py poll loop)."""
    with _lock:
        _state['rce_comparison'] = rce_data


def update_deposit(deposit_result) -> None:
    """Store the DepositResult (called from main.py poll loop); None clears it."""
    from dataclasses import asdict
    with _lock:
        _state['deposit'] = asdict(deposit_result) if deposit_result is not None else None


def update_battery_sim(payload: Optional[dict]) -> None:
    """Store the battery expansion simulation payload (called from main.py)."""
    with _lock:
        _state['battery_sim'] = payload


def update_lifetime_forecast(payload: Optional[dict]) -> None:
    """Store the forecast_lifetime() payload (called from main.py poll loop)."""
    with _lock:
        _state['lifetime_forecast'] = payload


def _build_predictions(result: RoiResult) -> list[dict]:
    avg = result.monthly_avg_savings
    if not avg or avg <= 0:
        return []
    today = date.today()
    cursor = today.replace(day=1) + relativedelta(months=1)
    rows: list[dict] = []

    if result.remaining_to_recover <= 0:
        cumulative_profit = result.net_profit
        total_return = result.total_return
        for _ in range(24):
            cumulative_profit += avg
            total_return += avg
            rows.append({
                'month_label': _month_label(cursor.year, cursor.month),
                'projected_savings': round(avg, 2),
                'net_profit': round(cumulative_profit, 2),
                'roi_pct': round(total_return / result.gross_investment * 100, 2),
                'post_payback': True,
            })
            cursor += relativedelta(months=1)
        return rows

    # Ścieżka sezonowa (P50) + wachlarz P10/P90 z residual_cv (jak w roi._walk_payback)
    factors = result.seasonal_factors or {}
    cv = result.residual_cv or 0.0
    remaining = result.remaining_to_recover
    cumulative = result.total_return
    cum_fast = cum_slow = result.total_return
    while remaining > 0 and len(rows) < 120:
        f = factors.get(cursor.month, 1.0)
        m_sav = avg * f
        cumulative += m_sav
        remaining -= m_sav
        cum_fast += max(avg * f * (1.0 + 1.28 * cv), 0.01)  # górna krawędź wachlarza
        cum_slow += max(avg * f * (1.0 - 1.28 * cv), 0.01)  # dolna krawędź
        rows.append({
            'month_label': _month_label(cursor.year, cursor.month),
            'projected_savings': round(m_sav, 2),
            'cumulative_return': round(cumulative, 2),
            'cumulative_fast': round(cum_fast, 2),
            'cumulative_slow': round(cum_slow, 2),
            'remaining': round(max(0.0, remaining), 2),
            'roi_pct': round(cumulative / result.gross_investment * 100, 2),
        })
        cursor += relativedelta(months=1)
    return rows



@app.route('/api/data')
def api_data():
    with _lock:
        result = _state['result']
        records = list(_state['records'])
        rcem_price = _state['rcem_price']
        month_closed = _state['month_closed']
        rcem_scrape_status = _state['rcem_scrape_status']
        updated_at = _state['updated_at']
        deposit_payload = _state['deposit']

    if result is None:
        return jsonify({'status': 'loading'}), 202

    today = date.today()
    current_ym = (today.year, today.month)

    complete = [
        r for r in records
        if (r.year, r.month) != current_ym
        and ((r.self_consumed_savings_pln or 0) + (r.feedin_revenue_pln or 0)) > 0
    ]

    def _msav(r: MonthlyRecord) -> float:
        return (r.self_consumed_savings_pln or 0) + (r.feedin_revenue_pln or 0)

    best_month = worst_month = None
    if complete:
        best = max(complete, key=_msav)
        worst = min(complete, key=_msav)
        best_month = {'label': _month_label(best.year, best.month), 'savings': round(_msav(best), 2)}
        worst_month = {'label': _month_label(worst.year, worst.month), 'savings': round(_msav(worst), 2)}

    # Lifetime self-sufficiency (autarkia)
    sc_total = sum(r.self_consumed_kwh or 0 for r in records if (r.year, r.month) <= current_ym)
    consumed_total = sum(r.consumed_kwh or 0 for r in records if (r.year, r.month) <= current_ym)
    self_sufficiency_avg = round(sc_total / consumed_total * 100, 1) if consumed_total > 0 else None

    # Net grid cost total: what was paid to the grid after netting feed-in revenue
    net_grid_cost_total = round(sum(
        (r.purchased_kwh or 0) * (r.buy_price_pln_kwh or 0) - (r.feedin_revenue_pln or 0)
        for r in records if (r.year, r.month) <= current_ym
    ), 2)

    from . import rcem_scraper as _rs
    corrections = _rs._load_corrections()

    # Deflatory CPI do wykresu oszczędności realnych (PLN dzisiejsze)
    import os as _os
    from . import cpi_fetcher as _cpi
    _infl = float(_os.environ.get('INFLATION_RATE', '0.05'))
    today_ym_t = (today.year, today.month)

    # Rozjazd rodzin produkcji (balance.py) — tylko do wyświetlenia w
    # diagnostyce zakładki Historia, patrz docs/BLUEPRINT.md 0.35.3. Zbiór
    # rozliczonych miesięcy liczony tak samo jak main.py's _reconciled_months(),
    # żeby oznaczyć w UI, dla których miesięcy rozjazd jest wyłącznie
    # diagnostyką (faktura ostateczna, nigdy nienaprawiane).
    _reconciled_ym: set = set()
    if _invoice_path is not None:
        try:
            from . import invoice_store as _istore_reconciled
            for _inv in _istore_reconciled.load(_invoice_path).values():
                if _inv.get('reconciled', False) and _inv.get('doc_type', 'rozliczeniowa') == 'rozliczeniowa':
                    _y, _m = _inv.get('year'), _inv.get('month')
                    if _y is not None and _m is not None:
                        _reconciled_ym.add((_y, _m))
        except Exception:
            pass

    cumulative = result.subsidy
    records_out = []
    for r in sorted(records, key=lambda x: (x.year, x.month)):
        if (r.year, r.month) > current_ym:
            continue
        month_savings = (r.self_consumed_savings_pln or 0.0) + (r.feedin_revenue_pln or 0.0) + (r.battery_arbitrage_savings_pln or 0.0)
        cumulative += month_savings
        month_key = f'{r.year}-{r.month:02d}'
        rcem_status = r.rcem_status
        if rcem_status == 'confirmed' and r.feedin_price_pln_kwh is None:
            rcem_status = 'pending'
        elif rcem_status == 'confirmed' and len(corrections.get(month_key) or []) >= 2:
            rcem_status = 'updated'
        purchased = r.purchased_kwh or 0.0
        buy_px = r.buy_price_pln_kwh or 0.0
        feedin_rev = r.feedin_revenue_pln or 0.0
        purchase_cost = round(purchased * buy_px, 2)
        net_grid = round(purchase_cost - feedin_rev, 2)
        consumed = r.consumed_kwh or 0.0
        sc_kwh = r.self_consumed_kwh or 0.0
        self_suff = round(sc_kwh / consumed * 100, 1) if consumed > 0 else None
        records_out.append({
            'month_label': _month_label(r.year, r.month),
            'month_key': month_key,
            'is_current': (r.year, r.month) == current_ym,
            'produced_kwh': r.produced_kwh,
            'exported_kwh': r.exported_kwh,
            'self_consumed_kwh': r.self_consumed_kwh,
            'consumed_kwh': r.consumed_kwh,
            'purchased_kwh': r.purchased_kwh,
            'buy_price': r.buy_price_pln_kwh,
            'feedin_price': r.feedin_price_pln_kwh,
            'self_savings': r.self_consumed_savings_pln,
            'feedin_revenue': r.feedin_revenue_pln,
            'battery_arbitrage_savings': r.battery_arbitrage_savings_pln,
            'month_savings': round(month_savings, 2),
            'cumulative_return': round(cumulative, 2),
            'roi_pct': round(cumulative / result.gross_investment * 100, 2),
            'rcem_status': rcem_status,
            'projected_month_kwh': r.projected_month_kwh if (r.year, r.month) == current_ym else None,
            'purchase_cost_pln': purchase_cost,
            'net_grid_cost': net_grid,
            'self_sufficiency_pct': self_suff,
            'purchased_kwh_peak': r.purchased_kwh_peak,
            'purchased_kwh_offpeak': r.purchased_kwh_offpeak,
            'tariff': r.tariff,
            'feedin_corrections': corrections.get(month_key) or None,
            'cpi_deflator': round(_cpi.get_deflator((r.year, r.month), today_ym_t, _infl), 4),
            # v0.27.0: rachunek „bez PV vs z PV"
            'bill_without_pv': round(consumed * buy_px, 2) if consumed > 0 and buy_px > 0 else None,
            'bill_with_pv': round(purchased * buy_px - feedin_rev, 2) if consumed > 0 and buy_px > 0 else None,
            # 0.35.3: diagnostyka rozjazdu rodzin produkcji — patrz balance.py.
            # Informacyjne, nie wpływa na health/roi; None jeśli LTS-fetch
            # dla tego miesiąca nie zebrał drugiej rodziny.
            'cross_family_produced_kwh': r.cross_family_produced_kwh,
            'balance_residual_kwh': r.balance_residual_kwh,
            'balance_reconciled': (r.year, r.month) in _reconciled_ym,
        })

    current_rec = next((r for r in records if (r.year, r.month) == current_ym), None)
    solcast_projected_kwh = current_rec.projected_month_kwh if current_rec else None
    projected_month_savings_pln = current_rec.projected_month_savings_pln if current_rec else None

    # Month progress
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_elapsed  = today.day
    curr_sav = 0.0
    if current_rec:
        curr_sav = (current_rec.self_consumed_savings_pln or 0.0) + (current_rec.feedin_revenue_pln or 0.0)
    _avg = result.monthly_avg_savings
    pace_proj     = round(curr_sav / days_elapsed * days_in_month, 2) if days_elapsed > 0 and curr_sav > 0 else None
    vs_avg_pct    = round(curr_sav / _avg * 100, 1)    if _avg and _avg > 0 else None
    pace_vs_avg   = round(pace_proj / _avg * 100, 1)   if pace_proj and _avg and _avg > 0 else None
    month_progress = {
        'days_elapsed': days_elapsed,
        'days_in_month': days_in_month,
        'savings_so_far': round(curr_sav, 2),
        'pace_projected': pace_proj,
        'vs_avg_pct': vs_avg_pct,
        'pace_vs_avg_pct': pace_vs_avg,
    }

    # Loaded once and shared by _build_invoices_data / _build_tariff_drift /
    # _build_cost_breakdown below — each previously re-read invoices.json
    # independently within this same request.
    _stored_invoices: dict = {}
    _real_invoices: dict = {}
    if _invoice_path is not None:
        try:
            from . import invoice_store as _istore
            _stored_invoices = _istore.load(_invoice_path)
            # filter_billing (after filter_real) gives only rozliczeniowa records —
            # korekty/noty share the same rates/kWh so they must not pollute
            # tariff-drift, cost-breakdown, or the max()-based latest-invoice logic.
            _real_invoices = _istore.filter_billing(_istore.filter_real(_stored_invoices))
        except Exception:
            pass

    return jsonify({
        'status': 'ok',
        'updated_at': updated_at,
        'summary': {
            'roi_pct': result.roi_pct,
            'total_savings': result.total_savings,
            'total_return': result.total_return,
            'self_consumption_savings': result.self_consumption_savings,
            'feedin_revenue': result.feedin_revenue,
            'battery_arbitrage_savings': result.battery_arbitrage_savings,
            'gross_investment': result.gross_investment,
            'subsidy': result.subsidy,
            'net_investment': round(result.gross_investment - result.subsidy, 2),
            'remaining_to_recover': result.remaining_to_recover,
            'monthly_avg_savings': result.monthly_avg_savings,
            'avg_window': result.monthly_avg_window,
            'months_to_payback': result.months_to_payback,
            'years_to_payback': result.years_to_payback,
            'payback_date': result.payback_date.isoformat() if result.payback_date else None,
            'total_produced_kwh': result.total_produced_kwh,
            'total_exported_kwh': result.total_exported_kwh,
            'specific_yield': result.specific_yield_lifetime,
            'rcem_price': rcem_price,
            'rcem_scrape_status': rcem_scrape_status,
            'best_month': best_month,
            'worst_month': worst_month,
            'month_closed': month_closed,
            'solcast_projected_kwh': solcast_projected_kwh,
            'net_profit': result.net_profit,
            'self_sufficiency_avg': self_sufficiency_avg,
            'net_grid_cost_total': net_grid_cost_total,
            'month_progress': month_progress,
            # Seasonal forecast
            'payback_date_seasonal': result.payback_date_seasonal.isoformat() if result.payback_date_seasonal else None,
            'payback_date_p10': result.payback_date_p10.isoformat() if result.payback_date_p10 else None,
            'payback_date_p90': result.payback_date_p90.isoformat() if result.payback_date_p90 else None,
            # Solcast savings projection
            'projected_month_savings_pln': projected_month_savings_pln,
            # Financial analysis
            'real_total_savings': result.real_total_savings,
            'real_total_return': result.real_total_return,
            'real_roi_pct': result.real_roi_pct,
            'npv': result.npv,
            'irr_pct': result.irr_pct,
            'counterfactual_bond_value': result.counterfactual_bond_value,
            'counterfactual_delta': result.counterfactual_delta,
            'inflation_source': result.inflation_source,
            'cumulative_inflation_pct': result.cumulative_inflation_pct,
            # v0.17.0: wskaźniki energetyczne
            'self_consumption_rate_pct': result.self_consumption_rate_pct,
            'autarky_pct': result.autarky_pct,
            'co2_avoided_kg': result.co2_avoided_kg,
            'yoy_yield_delta_pct': result.yoy_yield_delta_pct,
            # v0.27.0: alert „poniżej oczekiwań"
            'underperformance_pct': result.underperformance_pct,
            'underperformance_flag': result.underperformance_flag,
            'underperformance_last_closed_ym': _underperformance_analysis(records, today).get('last_closed_ym'),
            # v0.27.0: współczynnik CO₂ (dla wykresu skumulowanego po stronie frontu)
            'co2_factor_kg_kwh': float(__import__('os').environ.get('CO2_FACTOR_KG_KWH', '0.597')),
            # v0.27.0: rachunek bez PV vs z PV (sumy + avg%)
            'bill_comparison': _bill_comparison(records),
        },
        'records': records_out,
        'predictions': _build_predictions(result),
        'invoices': _build_invoices_data(records, _stored_invoices),
        'tariff_drift': _build_tariff_drift(_real_invoices),
        'cost_breakdown': _build_cost_breakdown(_real_invoices),
        'rate_trend': _build_rate_trend(_real_invoices),
        'layouts_summary': _build_layouts_summary(),
        'tariff_comparison': _state.get('tariff_comparison'),
        'rce_comparison': _state.get('rce_comparison'),
        'deposit': deposit_payload,
        'battery_sim': _state.get('battery_sim'),
        'lifetime_forecast': _state.get('lifetime_forecast'),
        'degradation': _build_degradation(records, today),
        'version': __version__,
    })


def _build_degradation(records, today):
    import os as _os
    from .roi import degradation_analysis
    try:
        kwp = float(_os.environ.get('SYSTEM_KWP', '6.72'))
        degradation_pct_year = float(_os.environ.get('PANEL_DEGRADATION_PCT_YEAR', '0.5'))
        return degradation_analysis(records, system_kwp=kwp, today=today,
                                     panel_degradation_pct_year=degradation_pct_year)
    except Exception:
        log.exception('degradation analysis failed')
        return None


def _build_invoices_data(records, stored: dict):
    out = []
    # Only iterate billing records and stubs — corrections/notas are attached
    # as nested sub-rows via the 'corrections' list on each billing row.
    billing_and_stubs = {
        k: v for k, v in stored.items()
        if '~kor~' not in k and '~nota~' not in k
    }
    for key, inv in sorted(billing_and_stubs.items(), key=lambda kv: kv[0]):
        try:
            # Synthetic keys like "unparsed-<ts>-<name>" — month unknown yet
            is_stub = key.startswith('unparsed-')
            diff_imp = diff_exp = None
            if not is_stub:
                try:
                    iy, im = int(key[:4]), int(key[5:7])
                    hist_rec = next((r for r in records if r.year == iy and r.month == im), None)
                    if hist_rec:
                        if hist_rec.purchased_kwh is not None and inv.get('imported_kwh') is not None:
                            diff_imp = round(inv['imported_kwh'] - hist_rec.purchased_kwh, 2)
                        if hist_rec.exported_kwh is not None and inv.get('exported_kwh') is not None:
                            diff_exp = round(inv['exported_kwh'] - hist_rec.exported_kwh, 2)
                except (ValueError, TypeError):
                    pass

            # Gather corrections/notas nested under this billing month
            corrections = []
            if not is_stub:
                for cor_key in sorted(stored):
                    if not (cor_key.startswith(f'{key}~kor~')
                            or cor_key.startswith(f'{key}~nota~')):
                        continue
                    cor = stored[cor_key]
                    cor_type = 'korekta' if '~kor~' in cor_key else 'nota'
                    corrections.append({
                        'key': cor_key,
                        'doc_type': cor.get('doc_type', cor_type),
                        'invoice_number': cor.get('invoice_number'),
                        'corrects_number': cor.get('corrects_number'),
                        'correction_reason': cor.get('correction_reason'),
                        'correction_delta_pln': cor.get('correction_delta_pln'),
                        'requires_payment': cor.get('requires_payment'),
                        'prev_deposit_previous': cor.get('prev_deposit_previous_pln'),
                        'deposit_previous': cor.get('deposit_previous_pln'),
                        'deposit_used': cor.get('deposit_used_pln'),
                        'amount_due': cor.get('amount_due_pln'),
                        'billing_period_raw': cor.get('billing_period_raw'),
                        'filename': cor.get('filename'),
                        'has_pdf': bool(cor.get('pdf_path')),
                        'warnings': cor.get('warnings', []),
                    })

            inv_warnings = inv.get('warnings', [])
            out.append({
                'key': key,
                'month': key if not is_stub else None,
                'is_stub': is_stub,
                'doc_type': inv.get('doc_type', 'rozliczeniowa'),
                'needs_training': inv.get('needs_training', False),
                'parse_error': inv.get('parse_error'),
                'has_raw_text': bool(inv.get('raw_text')),
                'has_pdf': bool(inv.get('pdf_path')),
                'amount_due': inv.get('amount_due_pln'),
                'deposit_current': inv.get('deposit_current_pln'),
                'deposit_previous': inv.get('deposit_previous_pln'),
                'deposit_used': inv.get('deposit_used_pln'),
                'imported_kwh': inv.get('imported_kwh'),
                'exported_kwh': inv.get('exported_kwh'),
                'imported_kwh_peak': inv.get('imported_kwh_peak'),
                'imported_kwh_offpeak': inv.get('imported_kwh_offpeak'),
                'diff_imported_kwh': diff_imp,
                'diff_exported_kwh': diff_exp,
                'peak_gross': inv.get('peak_gross'),
                'offpeak_gross': inv.get('offpeak_gross'),
                'blended_gross': inv.get('blended_gross'),
                'fixed_total_net': inv.get('fixed_total_net'),
                'avg_price': inv.get('avg_price_pln_kwh'),
                'reconciled': inv.get('reconciled', False),
                'invoice_number': inv.get('invoice_number'),
                'filename': inv.get('filename'),
                'billing_period_raw': inv.get('billing_period_raw'),
                'warnings': inv_warnings,
                'warnings_count': len(inv_warnings),
                # Detail fields for expand panel
                'energy_peak_net': inv.get('energy_peak_net'),
                'energy_offpeak_net': inv.get('energy_offpeak_net'),
                'dist_var_peak_net': inv.get('dist_var_peak_net'),
                'dist_var_offpeak_net': inv.get('dist_var_offpeak_net'),
                'dist_jakosciowa_net': inv.get('dist_jakosciowa_net'),
                'dist_oze_net': inv.get('dist_oze_net'),
                'dist_kogeneracja_net': inv.get('dist_kogeneracja_net'),
                'fixed_mocowa_net': inv.get('fixed_mocowa_net'),
                'fixed_abonament_net': inv.get('fixed_abonament_net'),
                'fixed_stalysieciowy_net': inv.get('fixed_stalysieciowy_net'),
                'corrections': corrections,
            })
        except Exception:
            pass
    return out


def _latest_real_invoice(real: dict) -> Optional[dict]:
    """Return the record for the chronologically latest billing month in an
    already stub-filtered dict (see invoice_store.filter_real/load_real —
    stub keys sort after every real "YYYY-MM" key under plain max(), so
    filtering must happen before this is called, not here)."""
    return real[max(real)] if real else None


# Rate + fixed-charge + computed-gross fields exposed by latest_invoice_rates().
_RATE_FIELDS = [
    'energy_peak_net', 'energy_offpeak_net',
    'dist_var_peak_net', 'dist_var_offpeak_net',
    'dist_jakosciowa_net', 'dist_oze_net', 'dist_kogeneracja_net',
    'fixed_mocowa_net', 'fixed_abonament_net', 'fixed_stalysieciowy_net',
    'peak_gross', 'offpeak_gross', 'fixed_total_net',
]


def latest_invoice_rates(real: Optional[dict] = None, _today=None) -> dict:
    """Canonical rate provider — jedno źródło prawdy dla stawek taryfy.

    Priorytet (rosnący, każdy poziom nadpisuje poprzedni):
      1. Baseline z tariff_config (current_entry.rates) — ręcznie utrzymywana taryfa
      2. Najnowsza faktura rozliczeniowa — automatycznie nadpisuje baseline
      3. Override z tariff_config — gdy ogłoszona taryfa jest nowsza niż faktura
         (wypełnia lukę styczeń→luty przy zmianie roku taryfowego); wygasa, gdy
         faktura za dany okres dotrze i max(billing_key) >= effective_from.

    `real` może być przekazane z zewnątrz (załadowane raz w tym samym cyklu) —
    pomija wtedy zbędny odczyt dysku. Gdy None, ładuje samodzielnie.
    `_today` jest wyłącznie dla testów; normalnie None → date.today().
    Zwraca {} tylko gdy brak zarówno tariff_config jak i faktur.
    """
    from . import tariff_config as _tc
    from datetime import date as _date
    today = _today or _date.today()

    cfg = _load_tariff_cfg()

    if real is None:
        real = _load_real_billing()

    # 1. Kumulatywny baseline z tariff_config (wszystkie wpisy ≤ dziś scalone rosnąco)
    rates: dict = _tc.effective_baseline(cfg, today)

    # 2. Faktura nadpisuje baseline (dla pól, które faktycznie sparsowała)
    latest = _latest_real_invoice(real)
    if latest is not None:
        rates.update({k: latest[k] for k in _RATE_FIELDS if latest.get(k) is not None})

    # 3. Override wygrywa w oknie luki (ogłoszona taryfa nowsza niż faktura)
    ov = _tc.override_rates(cfg, real, today)
    if ov:
        rates.update(ov)

    return rates


def _build_tariff_drift(real: dict):
    """`real` is an already stub-filtered billing invoices dict.
    Porównuje najnowszą fakturę do baseline z tariff_config.
    Zwraca None gdy brak dryftu lub brak faktury."""
    try:
        latest = _latest_real_invoice(real)
        if latest is None:
            return None
        from . import tariff_config as _tc
        from datetime import date as _date
        baseline_rates = _tc.effective_baseline(_load_tariff_cfg(), _date.today())
        baseline_peak = baseline_rates.get('peak_gross', 1.23)
        baseline_offpeak = baseline_rates.get('offpeak_gross', 0.63)
        baseline_fixed = baseline_rates.get('fixed_total_net', 39.47)
        drift = {}
        pk = latest.get('peak_gross')
        op = latest.get('offpeak_gross')
        if pk is not None and abs(pk - baseline_peak) > 0.02:
            drift['peak'] = {'configured': round(baseline_peak, 4), 'invoice': round(pk, 4)}
        if op is not None and abs(op - baseline_offpeak) > 0.02:
            drift['offpeak'] = {'configured': round(baseline_offpeak, 4), 'invoice': round(op, 4)}
        ft = latest.get('fixed_total_net')
        if ft is not None and abs(ft - baseline_fixed) > 0.50:
            drift['fixed_net'] = {'expected': round(baseline_fixed, 2), 'invoice': round(ft, 2)}
        return drift or None
    except Exception:
        return None


_FIXED_RATE_FIELDS = {'fixed_mocowa_net', 'fixed_abonament_net', 'fixed_stalysieciowy_net', 'fixed_total_net'}


def _derive_tariff_changes(real: dict) -> list:
    """Odtwórz oś zmian stawek taryfy na podstawie historii faktur billing.

    Iteruje faktury YYYY-MM rosnąco; emituje wiersz gdy którekolwiek pole z
    _RATE_FIELDS zmienia się vs poprzednia faktura (lub jako wiersz bazowy dla
    pierwszej faktury).  Epsilon: 1e-4 dla stawek PLN/kWh, 0.01 dla fixed PLN/mc.

    Zwraca listę dicts (najstarszy → najnowszy):
      { effective_from, changed: [{field, from, to}], rates, source_invoice }
    """
    if not real:
        return []
    prev: dict = {}
    changes = []
    for key in sorted(real):
        inv = real[key]
        snapshot = {k: inv.get(k) for k in _RATE_FIELDS if inv.get(k) is not None}
        if not snapshot:
            continue
        if not prev:
            # Punkt bazowy — pierwsza faktura z danymi
            changes.append({
                'effective_from': key,
                'changed': [],  # brak poprzedniego = punkt startowy, nie "zmiana"
                'rates': snapshot,
                'source_invoice': key,
            })
        else:
            delta = []
            for field, val in snapshot.items():
                old = prev.get(field)
                if old is None:
                    continue
                eps = 0.01 if field in _FIXED_RATE_FIELDS else 1e-4
                if abs(val - old) > eps:
                    delta.append({'field': field, 'from': round(old, 5), 'to': round(val, 5)})
            if delta:
                changes.append({
                    'effective_from': key,
                    'changed': delta,
                    'rates': snapshot,
                    'source_invoice': key,
                })
        # Carry forward: update only fields present in this invoice
        prev = {**prev, **snapshot}
    return changes


def _recon_zoned(inv: dict, peak_field: str, offpeak_field: str) -> Optional[float]:
    """Reconstruct a variable component's monthly amount from rate × kWh when
    the parser didn't capture the 'wartość netto' column directly."""
    rp, ro = inv.get(peak_field), inv.get(offpeak_field)
    if rp is None and ro is None:
        return None
    ip = inv.get('imported_kwh_peak') or 0.0
    io = inv.get('imported_kwh_offpeak') or 0.0
    return round((rp or 0.0) * ip + (ro or 0.0) * io, 2)


def _recon_flat(inv: dict, rate_field: str) -> Optional[float]:
    """Reconstruct a component whose rate is identical across zones (OZE,
    jakościowa, kogeneracja) — rate × total imported kWh."""
    rate = inv.get(rate_field)
    kwh = inv.get('imported_kwh')
    if rate is None or kwh is None:
        return None
    return round(rate * kwh, 2)


# A stored 'wartość netto' amount is treated as unusable — not just missing —
# when a rate×kWh reconstruction is available and the stored figure is far
# below what the rate implies. Confirmed real case (docs/AUDIT_2026_08_10.md,
# caught during 0.35.4's own post-release verification): 2023-12's
# energy_amount_net parsed to ~1.0 PLN net on a 1703 kWh month whose own
# energy_peak_net rate (0.698 PLN/kWh) implies ~1189 PLN — a parser artifact
# for that specific old-format invoice, not a genuine near-zero energy
# charge (energy is never a rounding error on a bill this size). 0.2 leaves
# ample room for legitimately small amounts in low-import months, which
# _build_rate_trend separately flags via _LOW_VOLUME_KWH rather than
# excluding here.
_IMPLAUSIBLE_STORED_RATIO = 0.2


def _pick_amount(inv: dict, amount_field: str, fallback) -> tuple[Optional[float], bool]:
    """Pick the best-known monthly amount for a cost component.

    Returns (amount, reconstructed). Prefers the stored amount field unless a
    rate×kWh reconstruction is available AND the stored figure is below
    _IMPLAUSIBLE_STORED_RATIO of it — see the constant's docstring above.
    fallback=None (fixed fees, which ARE the amount — there's no per-kWh rate
    to reconstruct from) always returns the stored value as-is.
    """
    stored = inv.get(amount_field)
    if fallback is None:
        return stored, False
    recon = fallback(inv)
    if stored is None:
        return recon, recon is not None
    if recon is not None and recon > 0 and stored < _IMPLAUSIBLE_STORED_RATIO * recon:
        return recon, True
    return stored, False


# Below this many imported kWh in a billing month, fixed fees (~15-30 PLN
# historically) dominate effective_gross_per_kwh — e.g. 9 kWh in 2024-06
# produced 5.26 PLN/kWh. Mathematically correct, but not a usable "current
# rate" for the headline sensor or a r/r comparison — see _build_rate_trend.
_LOW_VOLUME_KWH = 30.0


# Ordered energia → dystrybucja zmienna → opłaty stałe → opłaty dodatkowe, so the
# breakdown table/chart reads as a natural cost narrative top-to-bottom.
# (key, polish label, amount field on the stored invoice record, fallback fn or None)
_COST_COMPONENTS = [
    ('energia', 'Energia', 'energy_amount_net',
     lambda inv: _recon_zoned(inv, 'energy_peak_net', 'energy_offpeak_net')),
    ('dist_var', 'Składnik zmienny sieciowy', 'dist_var_amount_net',
     lambda inv: _recon_zoned(inv, 'dist_var_peak_net', 'dist_var_offpeak_net')),
    ('jakosciowa', 'Stawka jakościowa', 'dist_jakosciowa_amount_net',
     lambda inv: _recon_flat(inv, 'dist_jakosciowa_net')),
    ('oze', 'Opłata OZE', 'dist_oze_amount_net',
     lambda inv: _recon_flat(inv, 'dist_oze_net')),
    ('kogeneracja', 'Opłata kogeneracyjna', 'dist_kogeneracja_amount_net',
     lambda inv: _recon_flat(inv, 'dist_kogeneracja_net')),
    ('mocowa', 'Opłata mocowa', 'fixed_mocowa_net', None),
    ('abonament', 'Abonament', 'fixed_abonament_net', None),
    ('staly_sieciowy', 'Składnik stały sieciowy', 'fixed_stalysieciowy_net', None),
    ('przejsciowa', 'Opłata przejściowa', 'oplata_przejsciowa_net', None),
    ('handlowa', 'Opłata handlowa', 'oplata_handlowa_net', None),
    ('akcyza', 'Akcyza', 'akcyza_net', None),
]


def _build_cost_breakdown(real: dict) -> Optional[dict]:
    """Aggregate per-component grid-purchase costs across all parsed invoices,
    for the Faktury tab's "gdzie idą pieniądze" table + stacked chart.
    Falls back to rate × kWh reconstruction for invoices parsed before the
    monetary-amount fields existed (or where the value column wasn't found).

    `real` is an already stub-filtered invoices dict (see
    invoice_store.filter_real/load_real)."""
    months = real
    if not months:
        return None

    labels = sorted(months.keys())
    series: dict = {key: [] for key, *_rest in _COST_COMPONENTS}
    totals: dict = {key: 0.0 for key, *_rest in _COST_COMPONENTS}
    ever_observed: dict = {key: False for key, *_rest in _COST_COMPONENTS}
    any_reconstructed = False

    for month_key in labels:
        inv = months[month_key]
        for key, _label, amount_field, fallback in _COST_COMPONENTS:
            val, was_reconstructed = _pick_amount(inv, amount_field, fallback)
            if was_reconstructed:
                any_reconstructed = True
            series[key].append(val)
            if val is not None:
                totals[key] += val
                ever_observed[key] = True

    grand_total = round(sum(totals.values()), 2)
    components = []
    for key, label, *_rest in _COST_COMPONENTS:
        if not ever_observed[key]:
            continue  # never observed on any invoice (e.g. an absent optional fee)
        total = round(totals[key], 2)
        components.append({
            'key': key,
            'label': label,
            'total_net': total,
            'share_pct': round(total / grand_total * 100, 1) if grand_total > 0 else 0.0,
        })
    components.sort(key=lambda c: c['total_net'], reverse=True)

    return {
        'components': components,
        'per_month': {'labels': labels, 'series': series},
        'grand_total_net': grand_total,
        'any_reconstructed': any_reconstructed,
    }


def _build_rate_trend(real: dict) -> Optional[dict]:
    """Buduje serię stawek jednostkowych per faktura rozliczeniowa.

    Zwraca:
      labels:               lista 'YYYY-MM' (chronologicznie)
      rates_per_month:      [{ym, energy_peak_net, energy_offpeak_net,
                               dist_var_peak_net, dist_var_offpeak_net,
                               jakosciowa_net, oze_net, kogeneracja_net,
                               effective_gross_per_kwh, low_volume}]
      latest_effective_gross_per_kwh: stawka z ostatniej faktury o miarodajnym
                                       wolumenie (lub None)
      yoy_effective_gross_pct:        r/r efektywnej ceny all-in, tylko wśród
                                       miesięcy o miarodajnym wolumenie (lub None)

    effective_gross_per_kwh = (wszystkie składniki netto, zmienne + stałe) ×
    1.23 / imported_kwh — czyli PEŁNY koszt miesiąca na kWh, nie sama stawka
    zmienna. Poniżej _LOW_VOLUME_KWH ta miara jest matematycznie poprawna,
    ale zdominowana przez opłaty stałe rozłożone na garstkę kWh (np. 9 kWh w
    2024-06 → 5.26 PLN/kWh) i nie nadaje się do porównań r/r ani jako
    nagłówkowa "aktualna stawka" — stąd low_volume flaguje, nie usuwa, te
    miesiące (patrz docs/AUDIT_2026_08_10.md, punkt C).

    Składniki zmienne (energia/dist_var/jakościowa/OZE/kogeneracja) są
    rekonstruowane z rate × kWh dokładnie tym samym _pick_amount() co
    _build_cost_breakdown — brakujący LUB niewiarygodnie mały składnik
    (patrz _IMPLAUSIBLE_STORED_RATIO) jest zastępowany rekonstrukcją zamiast
    liczyć się jako 0/wartość-artefakt i zaniżać eff (np. 2023-12: parser
    zapisał energy_amount_net≈1,0 PLN na 1703 kWh importu przy stawce
    energy_peak_net=0,698 implikującej ~1189 PLN — 0,39 PLN/kWh zamiast ~1,25).
    """
    if not real:
        return None

    labels = sorted(k for k in real.keys() if not k.startswith('unparsed-'))
    months_out = []
    for ym in labels:
        inv = real[ym]
        kwh = inv.get('imported_kwh') or 0.0

        eff = None
        if kwh > 0:
            comps_net = [
                _pick_amount(inv, 'energy_amount_net',
                            lambda i: _recon_zoned(i, 'energy_peak_net', 'energy_offpeak_net'))[0],
                _pick_amount(inv, 'dist_var_amount_net',
                            lambda i: _recon_zoned(i, 'dist_var_peak_net', 'dist_var_offpeak_net'))[0],
                _pick_amount(inv, 'dist_jakosciowa_amount_net', lambda i: _recon_flat(i, 'dist_jakosciowa_net'))[0],
                _pick_amount(inv, 'dist_oze_amount_net', lambda i: _recon_flat(i, 'dist_oze_net'))[0],
                _pick_amount(inv, 'dist_kogeneracja_amount_net', lambda i: _recon_flat(i, 'dist_kogeneracja_net'))[0],
                inv.get('fixed_mocowa_net'),
                inv.get('fixed_abonament_net'),
                inv.get('fixed_stalysieciowy_net'),
            ]
            # The energy component dominates the bill — without it, "total
            # netto" is not a usable stand-in for the invoice total, so
            # leave eff as None rather than silently under-reporting it.
            if comps_net[0] is not None:
                total_netto = sum(c for c in comps_net if c is not None)
                eff = round(total_netto * 1.23 / kwh, 4)

        months_out.append({
            'ym':                    ym,
            'energy_peak_net':       inv.get('energy_peak_net'),
            'energy_offpeak_net':    inv.get('energy_offpeak_net'),
            'dist_var_peak_net':     inv.get('dist_var_peak_net'),
            'dist_var_offpeak_net':  inv.get('dist_var_offpeak_net'),
            'jakosciowa_net':        inv.get('dist_jakosciowa_net'),
            'oze_net':               inv.get('dist_oze_net'),
            'kogeneracja_net':       inv.get('dist_kogeneracja_net'),
            'effective_gross_per_kwh': eff,
            'imported_kwh':          kwh if kwh > 0 else None,
            'low_volume':            0 < kwh < _LOW_VOLUME_KWH,
        })

    def _representative(m: dict) -> bool:
        return m['effective_gross_per_kwh'] is not None and not m['low_volume']

    latest_eff = next((m['effective_gross_per_kwh'] for m in reversed(months_out)
                       if _representative(m)), None)
    # r/r efektywnej ceny — porównaj ostatni miarodajny miesiąc z tym samym
    # miesiącem rok wcześniej (też miarodajnym)
    yoy_eff_pct = None
    last_representative = next((m for m in reversed(months_out) if _representative(m)), None)
    if last_representative:
        last_ym = last_representative['ym']
        try:
            ly, lm = int(last_ym[:4]), int(last_ym[5:7])
            prev_ym = f'{ly - 1}-{lm:02d}'
            prev = next((m for m in months_out if m['ym'] == prev_ym), None)
            if prev and _representative(prev) and prev['effective_gross_per_kwh'] > 0:
                yoy_eff_pct = round(
                    (last_representative['effective_gross_per_kwh']
                     / prev['effective_gross_per_kwh'] - 1) * 100, 1
                )
        except (ValueError, TypeError):
            pass

    return {
        'labels':                         labels,
        'rates_per_month':                months_out,
        'latest_effective_gross_per_kwh': latest_eff,
        'yoy_effective_gross_pct':        yoy_eff_pct,
    }


@app.route('/api/invoice/upload', methods=['POST'])
def invoice_upload():
    if _invoice_reconcile_callback is None:
        return jsonify({'ok': False, 'error': 'not initialized'}), 503
    files = request.files.getlist('files') or request.files.getlist('file')
    if not files:
        return jsonify({'ok': False, 'error': 'no file(s) attached (field name: files)'}), 400
    from .invoice_parser import parse_invoice, parse_invoice_debug, InvoiceParseError
    results = []
    parsed_list = []
    raw_texts: dict = {}    # fname → raw text (for warnings-aware storage)
    pdf_bytes_map: dict = {}  # fname → original PDF bytes (persisted so we can return to it)
    for f in files:
        fname = f.filename or 'upload'
        pdf_bytes = f.read()
        pdf_bytes_map[fname] = pdf_bytes
        try:
            data = parse_invoice(pdf_bytes)
            data._filename = fname  # type: ignore[attr-defined]
            parsed_list.append(data)
            if data.warnings:
                # Store raw text so Train can work later
                debug = parse_invoice_debug(pdf_bytes)
                raw_texts[fname] = debug.get('text', '')
            results.append({'filename': fname, 'month': f'{data.year}-{data.month:02d}',
                             'doc_type': getattr(data, 'doc_type', 'rozliczeniowa'),
                             'imported_kwh': data.imported_kwh, 'exported_kwh': data.exported_kwh,
                             'peak_gross': data.peak_gross, 'offpeak_gross': data.offpeak_gross,
                             'amount_due': data.amount_due_pln, 'deposit_used': data.deposit_used_pln,
                             'correction_delta_pln': getattr(data, 'correction_delta_pln', None),
                             'warnings': data.warnings,
                             'ok': True})
        except InvoiceParseError as exc:
            # Store failed parse as a stub so user can train later
            error_msg = str(exc)
            stub_key = None
            if _invoice_path is not None:
                try:
                    debug = parse_invoice_debug(pdf_bytes)
                    raw_text = debug.get('text', '')
                    from . import invoice_store as _is
                    stub_key = _is.upsert_stub(fname, raw_text, error_msg, _invoice_path,
                                               pdf_bytes=pdf_bytes)
                except Exception:
                    log.exception('Failed to store stub for %s', fname)
            results.append({'filename': fname, 'ok': False, 'needs_training': True,
                             'error': error_msg, 'stub_key': stub_key})
        except Exception as exc:
            log.exception('Invoice parse error: %s', fname)
            results.append({'filename': fname, 'ok': False, 'error': str(exc)})
    if parsed_list:
        try:
            _invoice_reconcile_callback(parsed_list, raw_texts, pdf_bytes_map)
        except Exception as exc:
            log.exception('Invoice reconcile callback failed')
            return jsonify({'ok': False, 'error': str(exc), 'results': results}), 500
    return jsonify({'ok': True, 'results': results})


@app.route('/api/invoice/debug', methods=['POST'])
def invoice_debug():
    """Parse a PDF and return raw extracted text + fields, without saving."""
    files = request.files.getlist('files') or request.files.getlist('file')
    if not files:
        return jsonify({'ok': False, 'error': 'no file attached'}), 400
    from .invoice_parser import parse_invoice_debug
    f = files[0]
    result = parse_invoice_debug(f.read())
    return jsonify(result)


@app.route('/api/invoice/remove', methods=['POST'])
def invoice_remove():
    """Remove an invoice (and revert its historic.json snapshot)."""
    if _invoice_remove_callback is None:
        return jsonify({'ok': False, 'error': 'not initialized'}), 503
    data = request.get_json(silent=True) or {}
    key = str(data.get('key', ''))
    if not key:
        return jsonify({'ok': False, 'error': 'key required'}), 400
    try:
        record = _invoice_remove_callback(key)
        return jsonify({'ok': True, 'removed_key': key, 'had_snapshot': bool((record or {}).get('pre_reconcile'))})
    except Exception as exc:
        log.exception('Invoice remove failed')
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/invoice/pdf')
def invoice_pdf():
    """Serve the originally uploaded PDF for an invoice, if it was persisted."""
    key = request.args.get('key', '')
    if not key or _invoice_path is None:
        return jsonify({'ok': False, 'error': 'key required'}), 400
    from . import invoice_store as _istore
    pdf_bytes = _istore.load_pdf(key, _invoice_path)
    if pdf_bytes is None:
        return jsonify({'ok': False, 'error': 'no stored PDF for this invoice'}), 404
    rec = _istore.get(key, _invoice_path) or {}
    download_name = (rec.get('filename') or f'{key}.pdf').replace('"', '')
    return Response(pdf_bytes, mimetype='application/pdf', headers={
        'Content-Disposition': f'inline; filename="{download_name}"',
    })


@app.route('/api/invoice/reparse', methods=['POST'])
def invoice_reparse():
    """Re-run the parser over a stored PDF — e.g. after a parser/layout
    improvement — without asking the user to find and re-upload the file."""
    if _invoice_reconcile_callback is None or _invoice_path is None:
        return jsonify({'ok': False, 'error': 'not initialized'}), 503
    data = request.get_json(silent=True) or {}
    key = str(data.get('key', ''))
    if not key:
        return jsonify({'ok': False, 'error': 'key required'}), 400
    try:
        from . import invoice_store as _istore
        pdf_bytes = _istore.load_pdf(key, _invoice_path)
        if pdf_bytes is None:
            return jsonify({'ok': False, 'error': 'no stored PDF for this invoice'}), 404
        rec = _istore.get(key, _invoice_path) or {}
        fname = rec.get('filename') or key
        from .invoice_parser import parse_invoice, parse_invoice_debug, InvoiceParseError
        try:
            parsed = parse_invoice(pdf_bytes)
        except InvoiceParseError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400
        parsed._filename = fname  # type: ignore[attr-defined]
        raw_texts = {}
        if parsed.warnings:
            raw_texts[fname] = parse_invoice_debug(pdf_bytes).get('text', '')
        _invoice_reconcile_callback([parsed], raw_texts, {fname: pdf_bytes})
        return jsonify({'ok': True, 'key': f'{parsed.year}-{parsed.month:02d}',
                        'warnings': parsed.warnings})
    except Exception as exc:
        log.exception('Invoice reparse failed')
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/invoice/train_form')
def invoice_train_form():
    """Return raw_text + best-effort field values for a given invoice key."""
    key = request.args.get('key', '')
    if not key or _invoice_path is None:
        return jsonify({'ok': False, 'error': 'key required'}), 400
    try:
        from . import invoice_store as _istore
        rec = _istore.get(key, _invoice_path)
        if rec is None:
            return jsonify({'ok': False, 'error': 'invoice not found'}), 404
        raw_text = rec.get('raw_text', '')
        # Re-parse from stored raw_text if available (to apply any newly learned patterns)
        fields: dict = {}
        spans: dict = {}
        if raw_text:
            try:
                from .invoice_parser import _parse_text, find_field_spans, InvoiceParseError
                from dataclasses import asdict
                parsed = _parse_text(raw_text)
                fields = asdict(parsed)
                spans = find_field_spans(raw_text, fields)
            except Exception:
                # Even if parse failed entirely, try to get spans for whatever was extracted
                try:
                    from .invoice_parser import find_field_spans
                    spans = find_field_spans(raw_text, fields)
                except Exception:
                    pass
        # Fall back to stored values for any field not parsed
        for fk in ['year', 'month', 'imported_kwh', 'exported_kwh', 'imported_kwh_peak',
                   'imported_kwh_offpeak', 'energy_peak_net', 'energy_offpeak_net',
                   'amount_due_pln', 'avg_price_pln_kwh', 'deposit_current_pln',
                   'deposit_previous_pln', 'deposit_used_pln', 'fixed_mocowa_net',
                   'fixed_abonament_net', 'fixed_stalysieciowy_net', 'invoice_number']:
            if fields.get(fk) is None and rec.get(fk) is not None:
                fields[fk] = rec[fk]
        return jsonify({'ok': True, 'key': key, 'raw_text': raw_text,
                        'filename': rec.get('filename', ''), 'fields': fields,
                        'spans': spans})
    except Exception as exc:
        log.exception('train_form failed')
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/invoice/train', methods=['POST'])
def invoice_train():
    """Accept corrected field values, derive layout patterns, and reconcile the invoice."""
    if _invoice_train_callback is None:
        return jsonify({'ok': False, 'error': 'not initialized'}), 503
    data = request.get_json(silent=True) or {}
    key = str(data.get('key', ''))
    try:
        year  = int(data.get('year', 0))
        month = int(data.get('month', 0))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'year and month must be integers'}), 400
    if not key or not (1 <= month <= 12) or year < 2000:
        return jsonify({'ok': False, 'error': 'key, valid year and month required'}), 400
    fields = data.get('fields', {})
    raw_text = data.get('raw_text', '')
    filename = data.get('filename', '')
    try:
        result = _invoice_train_callback(key, year, month, fields, raw_text, filename)
        return jsonify(result)
    except Exception as exc:
        log.exception('Invoice train failed')
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/invoice/layouts/clear', methods=['POST'])
def invoice_layouts_clear():
    """Reset all learned layout patterns."""
    if _layouts_path is None:
        return jsonify({'ok': False, 'error': 'not initialized'}), 503
    try:
        from . import invoice_layouts as _il
        _il.clear(_layouts_path)
        return jsonify({'ok': True})
    except Exception as exc:
        log.exception('Layouts clear failed')
        return jsonify({'ok': False, 'error': str(exc)}), 500


def _build_layouts_summary() -> Optional[dict]:
    if _layouts_path is None:
        return None
    try:
        from . import invoice_layouts as _il
        return _il.summary(_layouts_path)
    except Exception:
        return None


@app.route('/api/rcem/override', methods=['POST'])
def rcem_override():
    if _rcem_override_callback is None:
        return jsonify({'ok': False, 'error': 'not initialized'}), 503
    data = request.get_json(silent=True) or {}
    month = str(data.get('month', ''))
    price = data.get('price')
    if not re.match(r'^\d{4}-\d{2}$', month):
        return jsonify({'ok': False, 'error': 'invalid month (YYYY-MM)'}), 400
    try:
        price = float(price)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'invalid price'}), 400
    if not (0 < price <= 2.0):
        return jsonify({'ok': False, 'error': 'price out of range (0, 2.0]'}), 400
    try:
        _rcem_override_callback(month, price)
        return jsonify({'ok': True, 'month': month, 'price': price})
    except Exception as exc:
        log.exception('RCEm override failed')
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/historic/patch', methods=['POST'])
def historic_patch():
    if _historic_patch_callback is None:
        return jsonify({'ok': False, 'error': 'not initialized'}), 503
    data = request.get_json(silent=True) or {}
    try:
        year = int(data['year'])
        month = int(data['month'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'year and month (integers) required'}), 400
    field = str(data.get('field', ''))
    value = data.get('value')
    if not field:
        return jsonify({'ok': False, 'error': 'field required'}), 400
    try:
        value = float(value)
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'value must be numeric'}), 400
    try:
        ok = _historic_patch_callback(year, month, field, value)
        if not ok:
            return jsonify({'ok': False, 'error': f'{year}-{month:02d} not found in historic.json'}), 404
        return jsonify({'ok': True, 'year': year, 'month': month, 'field': field, 'value': value})
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception as exc:
        log.exception('Historic patch failed')
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/historic/reread-month', methods=['POST'])
def historic_reread_month():
    """
    Backfill miesiąca ze statystyk długoterminowych HA i nadpisz rekord w historic.json.

    Używane jednorazowo po błędzie strefy czasowej (month_close strzelił po resecie
    utility_meter i zapisał zera). Statystyki HA przeżywają reset liczników.

    Body: {"year": 2026, "month": 6}
    """
    if _reread_month_callback is None:
        return jsonify({'ok': False, 'error': 'not initialized'}), 503
    data = request.get_json(silent=True) or {}
    try:
        year = int(data['year'])
        month = int(data['month'])
    except (KeyError, TypeError, ValueError):
        return jsonify({'ok': False, 'error': 'year and month (integers) required'}), 400
    try:
        result = _reread_month_callback(year, month)
        if result is None:
            return jsonify({'ok': False,
                            'error': f'Brak danych w statystykach HA dla {year}-{month:02d}. '
                                     'Sprawdź czy recorder ma statystyki dla encji produkcji.'}), 404
        return jsonify({'ok': True, 'year': year, 'month': month,
                        'produced_kwh': result.produced_kwh,
                        'exported_kwh': result.exported_kwh,
                        'self_consumed_savings_pln': result.self_consumed_savings_pln,
                        'feedin_revenue_pln': result.feedin_revenue_pln,
                        'rcem_status': result.rcem_status})
    except Exception as exc:
        log.exception('Reread-month failed')
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/historic/simulate-rebase', methods=['POST'])
def historic_simulate_rebase():
    """
    v0.35.0 dry run: re-fetch every historic month from HA's lifetime
    (never-resetting) meters, dynamically resolved from the HA Energy
    Dashboard's own configuration (see live_reader.get_energy_dashboard_sources),
    and diff every kWh field + the ROI headline figures against what's
    currently stored — WITHOUT writing anything.

    See docs/pv_roi_energy_rebase (plan) for why: the monthly utility_meter
    sensors this add-on read before 0.35.0 disagree with the Energy
    Dashboard's own configured production source by several percent some
    months, and consumed_kwh (previously read from
    sensor.house_consumption_energy_monthly) is now computed from
    produced/exported/imported directly — that sensor measured +82% high for
    July 2026 due to a utility_meter/non-monotonic-source interaction (see
    balance.py). Review the report, then call /api/historic/apply-rebase to
    actually rewrite historic.json.
    """
    if _simulate_rebase_callback is None:
        return jsonify({'ok': False, 'error': 'not initialized'}), 503
    try:
        report = _simulate_rebase_callback()
        return jsonify({'ok': True, **report})
    except Exception as exc:
        log.exception('Simulate-rebase failed')
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/historic/apply-rebase', methods=['POST'])
def historic_apply_rebase():
    """
    Write the v0.35.0 kWh rebase to historic.json. Snapshots the pre-rebase
    file first (historic.pre-rebase-<timestamp>.json) and keeps any
    invoice-reconciled month's billed fields authoritative — see rebase.py.
    Always run /api/historic/simulate-rebase first and review the diff.
    """
    if _apply_rebase_callback is None:
        return jsonify({'ok': False, 'error': 'not initialized'}), 503
    try:
        report = _apply_rebase_callback()
        return jsonify({'ok': True, **report})
    except Exception as exc:
        log.exception('Apply-rebase failed')
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/export/csv')
def export_csv():
    with _lock:
        records = list(_state['records'])
        result  = _state['result']
    if result is None:
        return Response('Brak danych', status=202)

    today = date.today()
    current_ym = (today.year, today.month)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        'Rok', 'Miesiac',
        'Wyprodukowane kWh', 'Sprzedane kWh', 'Autokons. kWh', 'Zuzycie kWh',
        'Zakup lacznie kWh', 'Zakup szczyt kWh', 'Zakup poza szczytem kWh',
        'Cena zakupu PLN/kWh', 'RCEm PLN/kWh',
        'Oszcz. autokons. PLN', 'Przychod sprzedazy PLN', 'Oszczednosci lacznie PLN',
        'Autarkia %', 'Koszt netto sieci PLN', 'Status RCEm',
    ])
    for r in sorted(records, key=lambda x: (x.year, x.month)):
        if (r.year, r.month) > current_ym:
            continue
        consumed = r.consumed_kwh or 0.0
        sc       = r.self_consumed_kwh or 0.0
        suff     = round(sc / consumed * 100, 1) if consumed > 0 else ''
        purchased = r.purchased_kwh or 0.0
        buy_px    = r.buy_price_pln_kwh or 0.0
        net_grid  = round(purchased * buy_px - (r.feedin_revenue_pln or 0.0), 2)
        savings   = round((r.self_consumed_savings_pln or 0.0) + (r.feedin_revenue_pln or 0.0), 2)
        w.writerow([
            r.year, r.month,
            r.produced_kwh, r.exported_kwh, r.self_consumed_kwh, r.consumed_kwh,
            r.purchased_kwh, r.purchased_kwh_peak, r.purchased_kwh_offpeak,
            r.buy_price_pln_kwh, r.feedin_price_pln_kwh,
            r.self_consumed_savings_pln, r.feedin_revenue_pln, savings,
            suff, net_grid, r.rcem_status,
        ])

    filename = f'pv_roi_{today.strftime("%Y%m%d")}.csv'
    return Response(
        '﻿' + buf.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


@app.route('/api/tariff_config')
def api_tariff_config_get():
    """GET — zwraca listę wpisów taryfowych + status aktywnego override."""
    from . import tariff_config as _tc
    from datetime import date as _date
    cfg = _load_tariff_cfg()
    today = _date.today()
    cur = _tc.current_entry(cfg, today)
    cur_ef = cur.get('effective_from') if cur else None
    real = _load_real_billing()
    ov = _tc.override_rates(cfg, real, today)
    is_override = bool(ov)
    max_inv = max(real) if real else None
    if is_override:
        reason = (f'Faktura nie nadeszła (ostatnia: {max_inv or "brak"}, '
                  f'ogłoszona taryfa od: {cur_ef})')
    elif cur_ef:
        reason = (f'Faktura pokrywa okres (ostatnia: {max_inv}, baseline: {cur_ef})'
                  if max_inv else f'Baseline (brak faktur, current: {cur_ef})')
    else:
        reason = 'Brak wpisów taryfowych — używane litery fallback w kodzie'
    return jsonify({
        'tariffs': cfg.get('tariffs', []),
        'current_effective_from': cur_ef,
        'is_override_active': is_override,
        'active_reason': reason,
    })


@app.route('/api/tariff_config', methods=['POST'])
def api_tariff_config_upsert():
    """POST — upsert wpisu taryfowego. Body JSON: {effective_from, note?, rates{}}."""
    from . import tariff_config as _tc
    if not _tariff_config_path:
        return jsonify({'ok': False, 'error': 'tariff_config_path not configured'}), 500
    data = request.get_json(silent=True) or {}
    try:
        cfg = _tc.load(_tariff_config_path)
        cfg = _tc.upsert_entry(cfg, data)
        _tc.save(cfg, _tariff_config_path)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        log.exception('tariff_config upsert failed')
        return jsonify({'ok': False, 'error': str(e)}), 500
    return jsonify({'ok': True})


@app.route('/api/tariff_config/delete', methods=['POST'])
def api_tariff_config_delete():
    """POST — usuń wpis. Body JSON: {effective_from: 'YYYY-MM'}."""
    from . import tariff_config as _tc
    if not _tariff_config_path:
        return jsonify({'ok': False, 'error': 'tariff_config_path not configured'}), 500
    data = request.get_json(silent=True) or {}
    ef = data.get('effective_from', '')
    if not ef:
        return jsonify({'ok': False, 'error': 'effective_from wymagane'}), 400
    try:
        cfg = _tc.load(_tariff_config_path)
        cfg = _tc.remove_entry(cfg, ef)
        _tc.save(cfg, _tariff_config_path)
    except Exception as e:
        log.exception('tariff_config delete failed')
        return jsonify({'ok': False, 'error': str(e)}), 500
    return jsonify({'ok': True})


@app.route('/api/tariff_config/derived')
def api_tariff_config_derived():
    """GET — read-only oś zmian taryfy odtworzona z historii faktur billing.

    Zwraca:
      { changes: [{effective_from, changed:[{field,from,to}], rates, source_invoice}],
        existing_effective_from: [YYYY-MM] }
    changes posortowane najnowsze-pierwsze (ułatwia renderowanie).
    existing_effective_from: lista kluczy ręcznych wpisów taryfowych —
    pozwala UI oznaczyć, które wykryte zmiany mają już odpowiadający wpis.
    """
    from . import tariff_config as _tc
    real = _load_real_billing()
    changes = list(reversed(_derive_tariff_changes(real)))

    try:
        existing = [t['effective_from'] for t in _load_tariff_cfg().get('tariffs', [])
                    if isinstance(t.get('effective_from'), str)]
    except Exception:
        existing = []

    return jsonify({'changes': changes, 'existing_effective_from': existing})


@app.route('/api/battery_config')
def api_battery_config_get():
    """Konfiguracja wirtualnego drugiego modułu magazynu (zakładka Magazyn)."""
    from . import battery_store as _bs
    if not _battery_config_path:
        return jsonify({'ok': False, 'error': 'battery_config_path not configured'}), 500
    return jsonify({'ok': True, 'config': _bs.load_config(_battery_config_path).to_dict()})


@app.route('/api/battery_config', methods=['POST'])
def api_battery_config_post():
    """Zapisz konfigurację magazynu i przelicz symulację (callback z main.py)."""
    from .battery_sim import BatteryConfig
    if not _battery_config_path or _battery_config_callback is None:
        return jsonify({'ok': False, 'error': 'battery config not wired'}), 500
    try:
        payload = request.get_json(force=True) or {}
        cfg = BatteryConfig.from_dict(payload)
        if cfg.usable_kwh <= 0 or cfg.power_kw <= 0 or cfg.module_price_pln <= 0:
            raise ValueError('pojemność, moc i cena muszą być > 0')
        if not (0.5 <= cfg.roundtrip_eff <= 1.0):
            raise ValueError('sprawność musi być w zakresie 0.5–1.0')
        if len(cfg.start_month) != 7 or cfg.start_month[4] != '-':
            raise ValueError('start_month musi być YYYY-MM')
        _battery_config_callback(cfg)
        return jsonify({'ok': True, 'config': cfg.to_dict()})
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception as exc:
        log.exception('battery_config POST failed')
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/tariff_stats')
def api_tariff_stats():
    """
    On-demand tariff statistics for the interactive chart.
    Query params:
      from   — start date ISO string, default '2025-01-01'
      period — 'day' or 'month', default 'day'
    Returns JSON with 'kpis' and 'series' keys.
    """
    from_date = request.args.get('from', '2025-01-01')
    period    = request.args.get('period', 'day')
    if period not in ('day', 'month'):
        return jsonify({'error': "period must be 'day' or 'month'"}), 400
    stats = _live_reader.get_ha_tariff_stats(
        [
            'sensor.symulacja_miesieczna_dynamicznej_faktura',
            'sensor.koszt_zmienny_g12w_miesieczny',
        ],
        start=from_date,
        period=period,
    )
    dyn  = stats.get('sensor.symulacja_miesieczna_dynamicznej_faktura', {})
    g12w = stats.get('sensor.koszt_zmienny_g12w_miesieczny', {})
    return jsonify(_tariff_analysis.compute_range_data(dyn, g12w, period))


@app.route('/api/export/tariff_csv')
def export_tariff_csv():
    with _lock:
        tc = _state.get('tariff_comparison')
    if tc is None or not tc.get('months'):
        return Response('Brak danych porównania taryf', status=202)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        'Miesiac', 'kWh zakup', 'kWh prod.',
        'G12w PLN', 'Dynamiczna PLN', 'Roznica PLN',
        'G12w gr/kWh', 'Dyn gr/kWh', 'PV offset %',
    ])
    for m in tc['months']:
        purchased = m.get('purchased_kwh') or 0
        g12w_var = m.get('g12w_variable_pln') or 0
        dyn_var = m.get('dynamic_variable_pln') or 0
        g12w_price = round(g12w_var / purchased * 100, 1) if purchased > 0 else ''
        dyn_price = round(dyn_var / purchased * 100, 1) if purchased > 0 else ''
        w.writerow([
            m.get('ym', ''),
            round(purchased, 2),
            round(m.get('produced_kwh') or 0, 2),
            round(g12w_var, 2),
            round(dyn_var, 2),
            round(m.get('diff_pln') or 0, 2),
            g12w_price,
            dyn_price,
            m.get('pv_offset_pct', ''),
        ])
    filename = f'taryfa_porownanie_{date.today().strftime("%Y%m%d")}.csv'
    return Response(
        '﻿' + buf.getvalue(),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )


_STATIC_DIR = Path(__file__).parent / 'static'
_INDEX_HTML = (_STATIC_DIR / 'index.html').read_text(encoding='utf-8').replace('{{VERSION}}', __version__)
_APP_JS  = (_STATIC_DIR / 'app.js').read_text(encoding='utf-8')
_APP_CSS = (_STATIC_DIR / 'app.css').read_text(encoding='utf-8')
_VENDOR_FILES = {
    'chart.umd.min.js':            (_STATIC_DIR / 'vendor' / 'chart.umd.min.js').read_bytes(),
    'chartjs-chart-sankey.min.js': (_STATIC_DIR / 'vendor' / 'chartjs-chart-sankey.min.js').read_bytes(),
}


@app.after_request
def _set_cache_headers(response):
    """Mobile WebView (HA Companion) caches HTML/JS aggressively and never
    revalidates — force-close does not clear it. The shell and API responses
    must never be cached; the versioned static assets (URL carries ?v=<version>,
    so a release bump is a new URL) are safe to cache forever."""
    if request.path == '/' or request.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    elif request.path in ('/app.js', '/app.css') or request.path.startswith('/vendor/'):
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return response


@app.route('/')
def index():
    # mimetype WITHOUT an explicit charset — Werkzeug appends its own
    # "; charset=utf-8" for text/* types, and does so unconditionally even
    # when one is already present, producing a duplicated header otherwise.
    return Response(_INDEX_HTML, mimetype='text/html')


@app.route('/app.js')
def app_js():
    return Response(_APP_JS, mimetype='application/javascript; charset=utf-8')


@app.route('/app.css')
def app_css():
    return Response(_APP_CSS, mimetype='text/css')  # see index() re: duplicated charset


@app.route('/vendor/<path:filename>')
def vendor(filename):
    data = _VENDOR_FILES.get(filename)
    if data is None:
        return '', 404
    return Response(data, mimetype='application/javascript; charset=utf-8')


def start_server(port: int = 8099) -> None:
    from waitress import serve
    logging.getLogger('waitress').setLevel(logging.ERROR)
    t = threading.Thread(
        target=lambda: serve(app, host='0.0.0.0', port=port, threads=4),
        daemon=True,
        name='waitress-ui',
    )
    t.start()
    log.info('Web UI started on port %d (waitress)', port)

