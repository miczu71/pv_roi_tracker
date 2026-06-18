"""Flask web UI — serves the ROI dashboard on the add-on ingress port."""
from __future__ import annotations

import calendar
import csv
import io
import logging
import re
import threading
from datetime import date, datetime
from typing import Optional

from dateutil.relativedelta import relativedelta
from flask import Flask, Response, jsonify, request

from . import __version__
from .models import MonthlyRecord
from .roi import RoiResult
from . import tariff_analysis as _tariff_analysis
from . import live_reader as _live_reader
from .tariff_analysis import _month_label

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB per request — bounds batch PDF uploads
log = logging.getLogger(__name__)

_lock = threading.Lock()
_rcem_override_callback = None
_historic_patch_callback = None
_invoice_reconcile_callback = None
_invoice_remove_callback = None
_invoice_train_callback = None
_invoice_path = None
_layouts_path = None
_tariff_config_path = None


def set_rcem_override_callback(fn) -> None:
    global _rcem_override_callback
    _rcem_override_callback = fn


def set_historic_patch_callback(fn) -> None:
    global _historic_patch_callback
    _historic_patch_callback = fn


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
        },
        'records': records_out,
        'predictions': _build_predictions(result),
        'invoices': _build_invoices_data(records, _stored_invoices),
        'tariff_drift': _build_tariff_drift(_real_invoices),
        'cost_breakdown': _build_cost_breakdown(_real_invoices),
        'layouts_summary': _build_layouts_summary(),
        'tariff_comparison': _state.get('tariff_comparison'),
        'rce_comparison': _state.get('rce_comparison'),
        'deposit': deposit_payload,
        'degradation': _build_degradation(records, today),
        'version': __version__,
    })


def _build_degradation(records, today):
    import os as _os
    from .roi import degradation_analysis
    try:
        kwp = float(_os.environ.get('SYSTEM_KWP', '6.72'))
        return degradation_analysis(records, system_kwp=kwp, today=today)
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
            val = inv.get(amount_field)
            if val is None and fallback is not None:
                val = fallback(inv)
                if val is not None:
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


@app.route('/')
def index():
    return Response(_HTML, mimetype='text/html; charset=utf-8')


def start_server(port: int = 8099) -> None:
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    t = threading.Thread(
        target=lambda: app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False),
        daemon=True,
        name='flask-ui',
    )
    t.start()
    log.info('Web UI started on port %d', port)


_HTML = r"""<!DOCTYPE html>
<html lang="pl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PV ROI Tracker</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-chart-sankey@0.12.1/dist/chartjs-chart-sankey.min.js"></script>
<style>
:root {
  --bg: #f0f4f8; --card: #fff; --border: #e2e8f0;
  --text: #1a202c; --muted: #718096;
  --accent: #2563eb; --green: #16a34a; --red: #dc2626; --yellow: #b45309;
  --radius: 8px; --shadow: 0 1px 3px rgba(0,0,0,.1);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); font-size: 14px; }

header {
  background: linear-gradient(135deg, #1e3a5f 0%, #2563eb 100%);
  color: #fff; padding: 14px 24px;
  display: flex; align-items: center; gap: 16px;
}
header h1 { font-size: 17px; font-weight: 700; flex: 1; }
#updated { font-size: 11px; opacity: .75; }
.csv-btn { font-size: 11px; font-weight: 600; color: rgba(255,255,255,.85); text-decoration: none;
           border: 1px solid rgba(255,255,255,.35); border-radius: 4px; padding: 3px 10px;
           white-space: nowrap; }
.csv-btn:hover { background: rgba(255,255,255,.12); }

main { max-width: 1600px; margin: 0 auto; padding: 18px 16px; }
.loading { text-align: center; padding: 60px 0; color: var(--muted); font-size: 15px; }

/* -- Summary cards -- */
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 10px; margin-bottom: 18px; }
.card { background: var(--card); border-radius: var(--radius); padding: 14px 16px; box-shadow: var(--shadow); }
.card .lbl { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .6px; margin-bottom: 5px; }
.card .val { font-size: 21px; font-weight: 700; line-height: 1.1; }
.card .sub { font-size: 11px; color: var(--muted); margin-top: 4px; }
.c-blue  .val { color: var(--accent); }
.c-green .val { color: var(--green); }

/* -- Charts -- */
.charts  { display: grid; grid-template-columns: 3fr 2fr; gap: 12px; margin-bottom: 12px; }
.charts2 { display: grid; grid-template-columns: 1fr; gap: 12px; margin-bottom: 18px; }
.grid2   { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
@media (max-width: 900px) { .charts { grid-template-columns: 1fr; } }
@media (max-width: 700px) { .grid2  { grid-template-columns: 1fr; } }
.chart-wrap    { background: var(--card); border-radius: var(--radius); padding: 16px; box-shadow: var(--shadow); height: 280px; position: relative; }
.chart-wrap.sm { height: 200px; }
.chart-wrap h3 { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 8px; }

/* -- Tabs -- */
.tabs { display: flex; gap: 3px; flex-wrap: nowrap; overflow-x: auto; -webkit-overflow-scrolling: touch;
        scrollbar-width: none; }
.tabs::-webkit-scrollbar { display: none; }
.tab-btn { flex-shrink: 0; }
.tab-btn {
  padding: 8px 18px; border: none; cursor: pointer; font-size: 12px; font-weight: 600;
  border-radius: var(--radius) var(--radius) 0 0;
  background: #dde3eb; color: var(--muted); transition: background .15s;
}
.tab-btn.active { background: var(--card); color: var(--text); box-shadow: 0 -1px 3px rgba(0,0,0,.08); }
.tab-panel { background: var(--card); border-radius: 0 var(--radius) var(--radius) var(--radius); box-shadow: var(--shadow); overflow: hidden; }

/* -- Tables -- */
.tbl-wrap { overflow-x: auto; max-height: 540px; overflow-y: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
thead th {
  position: sticky; top: 0; z-index: 2;
  background: #f7fafc; padding: 7px 8px;
  text-align: right; font-weight: 600; font-size: 10px;
  text-transform: uppercase; letter-spacing: .4px; color: var(--muted);
  border-bottom: 2px solid var(--border); white-space: normal; line-height: 1.3;
}
thead th:first-child { text-align: left; }
tbody td { padding: 5px 8px; text-align: right; border-bottom: 1px solid var(--border); white-space: nowrap; }
tbody td:first-child { text-align: left; font-weight: 600; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: #f7fafc; }
tbody tr.cur td { background: #eff6ff; }
tbody tr.cur td:first-child::after { content: "\00a0\2605"; color: var(--accent); font-size: 10px; }
tbody tr.pb  td { background: #f0fdf4; color: var(--green); font-weight: 700; }
tbody tr.yr  td { background: #f7fafc; font-weight: 700; font-size: 11.5px; color: #4a5568;
                  border-top: 2px solid #cbd5e0; border-bottom: 2px solid #cbd5e0; }
.tbl-foot { padding: 10px 14px; font-size: 11px; color: var(--muted); border-top: 1px solid var(--border); }

/* -- Sensitivity table -- */
.sensi-wrap { border-top: 2px solid var(--border); margin-top: 2px; }
.sensi-wrap table { font-size: 12px; }
.sensi-wrap caption { font-size: 10px; color: var(--muted); padding: 8px 11px; text-align: left; font-weight: 600; letter-spacing: .4px; text-transform: uppercase; }
.sensi-wrap tr.base td { font-weight: 700; }

/* -- Badges -- */
.badge { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 10px; font-weight: 700; }
.badge-ok      { background: #dcfce7; color: var(--green); }
.badge-pending { background: #fef3c7; color: var(--yellow); }
.badge-missing { background: #fee2e2; color: var(--red); }
.badge-updated { background: #e0f2fe; color: #0369a1; }
.badge-live    { background: #fef3c7; color: var(--yellow); }
.badge-snap    { background: #dcfce7; color: var(--green); }
.badge-g11     { background: #ede9fe; color: #6d28d9; }

/* -- Cost breakdown netto/brutto toggle -- */
.cost-toggle-btn { padding: 3px 10px; border: 1px solid var(--border); cursor: pointer; font-size: 11px; background: var(--card); color: var(--fg); }
.cost-toggle-btn:first-child { border-radius: 4px 0 0 4px; }
.cost-toggle-btn:last-child  { border-radius: 0 4px 4px 0; border-left: none; }
.cost-toggle-btn.active { background: #3182ce; color: #fff; font-weight: 600; }

/* -- Projected hint -- */
.proj-hint { font-size: 10px; color: var(--muted); font-weight: 400; }

/* -- RCEm correction tooltip -- */
.price-corrected { cursor: help; border-bottom: 1px dotted currentColor; position: relative; }
.price-down { color: #e53e3e; }
.price-up   { color: #38a169; }
.price-corrected::after {
  content: attr(data-tip);
  position: absolute;
  bottom: 130%;
  left: 50%;
  transform: translateX(-50%);
  background: #2d3748;
  color: #fff;
  padding: 7px 11px;
  border-radius: 6px;
  font-size: 11px;
  white-space: pre;
  min-width: 210px;
  opacity: 0;
  pointer-events: none;
  z-index: 200;
  transition: opacity 0.15s;
  line-height: 1.6;
  text-align: left;
  font-weight: 400;
}
.price-corrected:hover::after,
.price-corrected.tip-open::after { opacity: 1; }

/* -- RCEm override form -- */
.override-wrap { background: var(--card); border-radius: var(--radius); padding: 14px 16px; box-shadow: var(--shadow); }
.override-wrap h3 { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 10px; }
.override-form { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.override-form label { font-size: 12px; color: var(--muted); }
.override-form input { font-size: 13px; padding: 5px 8px; border: 1px solid var(--border); border-radius: 5px; background: var(--bg); color: var(--text); }
.override-form button { padding: 6px 16px; background: var(--accent); color: #fff; border: none; border-radius: 5px; font-size: 13px; font-weight: 600; cursor: pointer; }
.override-form button:hover { background: #1d4ed8; }
#ovMsg.ok  { color: var(--green); }
#ovMsg.err { color: var(--red); }
.rcem-badge { font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 10px; text-transform: uppercase; letter-spacing: .4px; margin-left: 8px; vertical-align: middle; }
.rcem-badge.ok       { background: #d1fae5; color: #065f46; }
.rcem-badge.pending  { background: #e0e7ff; color: #3730a3; }
.rcem-badge.retrying { background: #fef3c7; color: #92400e; }
.rcem-badge.error    { background: #fee2e2; color: #991b1b; }

/* -- Train modal source highlighting -- */
.train-grid { display: grid; grid-template-columns: 55% 43%; gap: 16px; align-items: start; }
.train-pane { height: 460px; overflow-y: auto; }
@media (max-width: 700px) {
  .train-grid { grid-template-columns: 1fr; }
  .train-pane { height: 40vh; }
}
.tx-span { border-radius: 2px; cursor: pointer; transition: outline 0.08s; }
.tx-span:hover, .tx-active { outline: 2px solid #2b6cb0 !important; position: relative; z-index: 1; }
.tf-found { border-left: 3px solid transparent; transition: background 0.1s; }
.tf-active { background: #ebf8ff !important; }

/* -- Mobile (RWD) -- */
@media (max-width: 640px) {
  main { padding: 10px 8px; }
  header { padding: 10px 12px; gap: 10px; }
  header h1 { font-size: 15px; }
  #updated { display: none; }
  .cards { grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 8px; margin-bottom: 12px; }
  .card { padding: 10px 12px; }
  .card .val { font-size: 18px; }
  .chart-wrap { height: 240px; padding: 12px 10px; }
  .chart-wrap.sm { height: 180px; }
  .tab-btn { padding: 7px 12px; font-size: 11px; }
  .tbl-wrap { max-height: 440px; }
  /* pierwsza kolumna tabel przyklejona przy przewijaniu poziomym */
  .tbl-wrap tbody td:first-child, .tbl-wrap thead th:first-child {
    position: sticky; left: 0; z-index: 1; background: var(--card);
  }
  .tbl-wrap thead th:first-child { z-index: 3; background: #f7fafc; }
  .tbl-wrap tbody tr.cur td:first-child { background: #eff6ff; }
  .tbl-wrap tbody tr.pb  td:first-child { background: #f0fdf4; }
  .tbl-wrap tbody tr.yr  td:first-child { background: #f7fafc; }
}

/* -- Docs modal -- */
.docs-body { line-height: 1.6; color: var(--text); overflow-wrap: anywhere; }
.docs-body h2 { font-size: 15px; font-weight: 700; margin: 20px 0 8px; padding-bottom: 4px;
                border-bottom: 2px solid var(--border); color: var(--accent); }
.docs-body h2:first-child { margin-top: 0; }
.docs-body h3 { font-size: 13px; font-weight: 700; margin: 14px 0 5px; color: var(--text); }
.docs-body p  { font-size: 13px; margin: 0 0 8px; }
.docs-body ul { font-size: 13px; margin: 0 0 8px; padding-left: 20px; }
.docs-body li { margin-bottom: 3px; }
.docs-body table { width: 100%; border-collapse: collapse; font-size: 12px; margin-bottom: 12px; }
.docs-body thead th { background: #f7fafc; padding: 6px 8px; text-align: left;
                      font-weight: 600; font-size: 11px; text-transform: uppercase;
                      letter-spacing: .4px; color: var(--muted); border-bottom: 2px solid var(--border); }
.docs-body tbody td { padding: 5px 8px; border-bottom: 1px solid var(--border);
                      font-size: 12px; vertical-align: top;
                      overflow-wrap: anywhere; word-break: break-word; }
.docs-body tbody tr:hover { background: #f7fafc; }
.docs-body code { background: var(--bg); border: 1px solid var(--border); border-radius: 3px;
                  padding: 1px 5px; font-size: 11px; font-family: monospace;
                  white-space: normal; overflow-wrap: anywhere; word-break: break-word; }
.docs-body .intro-box { background: #eff6ff; border-left: 3px solid var(--accent);
                        padding: 8px 12px; border-radius: 0 6px 6px 0;
                        font-size: 13px; margin-bottom: 12px; }

</style>
</head>
<body>
<header>
  <h1>&#9728;&#65039; PV ROI Tracker <span id="appVer" style="font-size:12px;font-weight:400;opacity:.65;vertical-align:middle"></span></h1>
  <a href="api/export/csv" class="csv-btn" download>&#8595; Eksportuj CSV</a>
  <a href="#" class="csv-btn" onclick="openDocsModal();return false;">&#128214; Dokumentacja</a>
  <span id="updated">Ladowanie&hellip;</span>
</header>
<main>
  <div id="loading" class="loading">Pobieranie danych&hellip;</div>
  <div id="content" style="display:none">
    <div class="cards" id="cards"></div>
    <div class="charts">
      <div class="chart-wrap">
        <h3>Laczny zwrot kumulatywny</h3>
        <canvas id="lineChart"></canvas>
      </div>
      <div class="chart-wrap">
        <h3>Miesieczne oszczednosci (ostatnie 24 mies.)</h3>
        <canvas id="barChart"></canvas>
      </div>
    </div>
    <div class="tabs">
      <button class="tab-btn active" onclick="showTab('hist')">Historia miesieczna</button>
      <button class="tab-btn"        onclick="showTab('pred')">Prognoza splaty</button>
      <button class="tab-btn"        onclick="showTab('years')">Podsumowanie roczne</button>
      <button class="tab-btn"        onclick="showTab('charts')">Wykresy</button>
      <button class="tab-btn"        onclick="showTab('invoices')">&#128196; Faktury</button>
      <button class="tab-btn"        onclick="showTab('tariff')">&#128200; Analiza taryf</button>
      <button class="tab-btn"        onclick="showTab('taryfa')">&#128209; Taryfa</button>
      <button class="tab-btn"        onclick="showTab('rce')">&#9889; RCE vs RCEm</button>
    </div>
    <div class="tab-panel">
      <div id="tab-hist">
        <div class="tbl-wrap"><table id="histTbl"></table></div>
        <div class="tbl-foot" id="histFoot"></div>
      </div>
      <div id="tab-pred" style="display:none">
        <div style="padding:12px 12px 0">
          <div class="chart-wrap" style="height:300px;margin-bottom:12px">
            <h3>Wachlarz spłaty — skumulowany zwrot z pasmem niepewności (P10–P90)</h3>
            <canvas id="fanChart"></canvas>
          </div>
        </div>
        <div class="tbl-wrap"><table id="predTbl"></table></div>
        <div class="tbl-foot" id="predFoot"></div>
        <div class="sensi-wrap" id="sensiWrap">
          <div class="tbl-wrap"><table id="sensiTbl"></table></div>
        </div>
      </div>
      <div id="tab-years" style="display:none">
        <div class="tbl-wrap"><table id="yearsTbl"></table></div>
        <div class="tbl-foot" id="yearsFoot"></div>
      </div>
      <div id="tab-charts" style="display:none">
        <div style="padding:12px">
          <div class="charts" style="margin-bottom:12px">
            <div class="chart-wrap">
              <h3>Historia ceny RCEm (zl/kWh)</h3>
              <canvas id="rcemChart"></canvas>
            </div>
            <div class="chart-wrap">
              <h3>Autarkia miesięczna (%)</h3>
              <canvas id="autarkiaChart"></canvas>
            </div>
          </div>
          <div class="charts" style="margin-bottom:12px">
            <div class="chart-wrap">
              <h3>Arbitraż baterii (zl/mies.)</h3>
              <canvas id="arbitrageChart"></canvas>
            </div>
            <div class="chart-wrap">
              <h3>Koszt netto sieci (zl/mies.)</h3>
              <canvas id="netCostChart"></canvas>
            </div>
          </div>
          <div class="charts" style="margin-bottom:12px">
            <div class="chart-wrap">
              <h3>Spread cen: zakup vs sprzedaz (zl/kWh)</h3>
              <canvas id="priceSpreadChart"></canvas>
            </div>
            <div class="chart-wrap">
              <h3>Uzysk specyficzny (kWh/kWp)</h3>
              <canvas id="yieldChart"></canvas>
            </div>
          </div>
          <div class="charts2" style="margin-bottom:12px">
            <div class="chart-wrap">
              <h3>Bilans energetyczny: produkcja vs zuzycie (kWh)</h3>
              <canvas id="energyBalChart"></canvas>
            </div>
          </div>
          <div class="charts2" style="margin-bottom:12px">
            <div class="chart-wrap">
              <h3>Produkcja i zakup z sieci — szczyt vs poza szczytem (kWh)</h3>
              <canvas id="prodChart"></canvas>
            </div>
          </div>
          <div class="charts2">
            <div class="chart-wrap" style="height:320px">
              <h3>Porownanie roczne produkcji (kWh)</h3>
              <canvas id="yearCompChart"></canvas>
            </div>
          </div>
          <div class="charts" style="margin-bottom:12px">
            <div class="chart-wrap">
              <h3>Oszczędności skumulowane: nominalne vs realne (CPI, dzisiejsze zł)</h3>
              <canvas id="cpiRealChart"></canvas>
            </div>
            <div class="chart-wrap">
              <h3>Uzysk kroczący 12 mies. (kWh/kWp) — trend degradacji <span id="degradBadge" style="font-size:11px;font-weight:400;color:var(--muted)"></span></h3>
              <canvas id="degradChart"></canvas>
            </div>
          </div>
          <div class="charts" style="margin-bottom:12px">
            <div class="chart-wrap">
              <h3>Dekompozycja miesiąca (waterfall)
                <select id="waterfallMonth" onchange="redrawWaterfall()" style="font-size:11px;margin-left:8px"></select>
              </h3>
              <canvas id="waterfallChart"></canvas>
            </div>
            <div class="chart-wrap">
              <h3>Przepływ energii (Sankey)
                <select id="sankeyYear" onchange="redrawSankey()" style="font-size:11px;margin-left:8px"></select>
              </h3>
              <canvas id="sankeyChart"></canvas>
            </div>
          </div>
          <div class="charts2">
            <div class="chart-wrap" id="prodRankWrap">
              <h3>Produkcja miesięczna — ranking (najlepsza → najgorsza)</h3>
              <canvas id="prodRankChart"></canvas>
            </div>
          </div>
        </div>
      </div>
      <!-- Analiza taryf tab -->
      <div id="tab-tariff" style="display:none;padding:12px">
        <div id="tariffWarning" style="display:none;margin-bottom:12px;padding:10px 14px;background:#fff3cd;border-left:4px solid #f0ad4e;border-radius:4px;font-size:13px">
          &#9888;&#65039; Za mało danych — prognoza i wykres historyczny będą dostępne po 3 pełnych miesiącach.
        </div>
        <!-- SEKCJA 1: TERAZ -->
        <h3 style="margin:0 0 8px;font-size:14px;font-weight:700;color:#555">&#9889; TERAZ</h3>
        <div id="tariffStatusBar" style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px"></div>
        <div style="margin-bottom:16px">
          <div class="chart-wrap" style="height:320px">
            <h3>Cena 1 kWh — ostatnie 7 dni</h3>
            <canvas id="tariffPriceChart"></canvas>
          </div>
        </div>
        <!-- SEKCJA 2: TEN MIESIĄC I HISTORIA -->
        <h3 style="margin:0 0 8px;font-size:14px;font-weight:700;color:#555">&#128197; TEN MIESIĄC I HISTORIA</h3>
        <div id="tariffKpiCards" style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:16px"></div>
        <div class="chart-wrap" style="height:260px;margin-bottom:16px">
          <h3>Koszt miesięczny G12w vs Dynamiczna</h3>
          <canvas id="tariffCompChart"></canvas>
        </div>
        <div class="grid2" style="margin-bottom:16px">
          <div class="chart-wrap" style="height:220px">
            <h3>Skumulowane oszczędności (PLN)</h3>
            <canvas id="tariffCumChart"></canvas>
          </div>
          <div class="chart-wrap" style="height:220px">
            <h3>Sezonowość (lato vs zima)</h3>
            <canvas id="tariffSeasonChart"></canvas>
          </div>
        </div>
        <div class="grid2" style="margin-bottom:16px">
          <div class="chart-wrap" style="height:200px">
            <h3>Rozkład różnic miesięcznych (PLN)</h3>
            <canvas id="tariffHistChart"></canvas>
          </div>
          <div id="tariffPredSection" style="padding:12px;background:#f9f9f9;border-radius:6px">
            <h4 style="margin:0 0 8px;font-size:13px;font-weight:600">Prognoza roczna</h4>
            <div id="tariffPredCards" style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:10px"></div>
            <h4 style="margin:8px 0 6px;font-size:12px;font-weight:600;color:#666">Wrażliwość na zmianę stawki G12w</h4>
            <table id="tariffSensiTbl" style="width:100%;font-size:12px;border-collapse:collapse"></table>
          </div>
        </div>
        <!-- Monthly table -->
        <details style="margin-bottom:14px">
          <summary style="cursor:pointer;font-weight:600;font-size:13px;padding:6px 0">&#128203; Dane miesiąc po miesiącu <button onclick="window.location='api/export/tariff_csv'" style="font-size:11px;padding:2px 8px;margin-left:10px;cursor:pointer">&#128229; Eksportuj CSV</button></summary>
          <div class="tbl-wrap" style="margin-top:8px"><table id="tariffMonthTbl" style="width:100%;font-size:12px"></table></div>
        </details>
        <!-- Explanation -->
        <details style="margin-bottom:8px">
          <summary style="cursor:pointer;font-weight:600;font-size:13px;padding:6px 0">&#8505;&#65039; Kontekst prosumenta i metodologia</summary>
          <div id="tariffContextText" style="font-size:12px;line-height:1.6;padding:10px;background:#f5f5f5;border-radius:4px;margin-top:6px"></div>
        </details>

        <!-- SEKCJA 3: INTERAKTYWNA ANALIZA ZAKRESU -->
        <h3 style="margin:16px 0 8px;font-size:14px;font-weight:700;color:#555">&#128202; ANALIZA WYBRANEGO ZAKRESU</h3>
        <div style="display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:10px;padding:10px;background:#f8f8f8;border-radius:6px;font-size:13px">
          <label style="font-weight:600">Od:</label>
          <input type="date" id="tariffFrom" style="padding:3px 6px;font-size:13px;border:1px solid #ccc;border-radius:4px">
          <label style="font-weight:600">Do:</label>
          <input type="date" id="tariffTo" style="padding:3px 6px;font-size:13px;border:1px solid #ccc;border-radius:4px">
          <span style="color:#aaa">|</span>
          <label><input type="radio" name="tariffPeriod" value="day" checked onchange="fetchTariffRange()"> Dzień</label>
          <label><input type="radio" name="tariffPeriod" value="month" onchange="fetchTariffRange()"> Miesiąc</label>
          <button onclick="fetchTariffRange()" style="padding:4px 14px;cursor:pointer;background:#2980b9;color:#fff;border:none;border-radius:4px;font-size:13px">&#128260; Odśwież</button>
          <span id="tariffRangeStatus" style="color:#888;font-size:12px"></span>
        </div>
        <!-- Range KPI pills -->
        <div id="tariffRangeKpis" style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px"></div>
        <!-- Interactive chart -->
        <div class="chart-wrap" style="height:280px;margin-bottom:8px">
          <canvas id="tariffRangeChart"></canvas>
        </div>
        <!-- Series toggles -->
        <div style="font-size:12px;color:#555;margin-bottom:16px;display:flex;gap:14px;flex-wrap:wrap">
          <strong>Serie:</strong>
          <label><input type="checkbox" id="chkRangeG12w" checked onchange="redrawRangeChart()"> G12w PLN</label>
          <label><input type="checkbox" id="chkRangeDyn" checked onchange="redrawRangeChart()"> Dynamiczna PLN</label>
          <label><input type="checkbox" id="chkRangeDiff" checked onchange="redrawRangeChart()"> Różnica PLN</label>
          <label><input type="checkbox" id="chkRangeCum" onchange="redrawRangeChart()"> Skumulowana PLN</label>
        </div>
      </div>
      <!-- Taryfa configuration tab -->
      <div id="tab-taryfa" style="display:none;padding:12px">
        <div id="taryfaBadge" style="margin-bottom:14px;padding:10px 14px;border-radius:4px;font-size:13px;background:#e8f4fd;border-left:4px solid #2196F3">
          Ładowanie stanu taryfy...
        </div>
        <h3 style="margin:0 0 6px">Historia taryf</h3>
        <p style="font-size:12px;color:#666;margin:0 0 12px">
          Jedna oś czasu łącząca: <strong>&#128203; z faktury</strong> — zmiany stawek
          wykryte automatycznie z wgranych faktur (tylko podgląd); <strong>&#9998; ręczny</strong> —
          wpisy wyprzedzające fakturę (luka / override / przyszłe zmiany). Wpis ręczny jest
          <strong>override</strong>, gdy jego data jest nowsza niż najnowsza faktura;
          automatycznie ustępuje, gdy faktura za ten okres dotrze. Puste pola dziedziczą
          wartości z wcześniejszych wpisów — wystarczy wpisać tylko co się zmieniło.
        </p>
        <div id="taryfaList" style="margin-bottom:18px"></div>
        <h3 style="margin:0 0 10px" id="taryfaFormTitle">Dodaj / edytuj wpis</h3>
        <form id="taryfaForm" style="max-width:640px">
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">
            <label style="font-size:13px">Obowiązuje od (YYYY-MM)<br>
              <input type="month" id="tfEffectiveFrom" style="width:100%;padding:5px;margin-top:3px" required>
            </label>
            <label style="font-size:13px">Notatka<br>
              <input type="text" id="tfNote" placeholder="np. Taryfa TD 2027" style="width:100%;padding:5px;margin-top:3px">
            </label>
          </div>
          <details open style="margin-bottom:10px">
            <summary style="cursor:pointer;font-weight:600;font-size:13px;margin-bottom:8px">Cena brutto wszystko-w-jednym (PLN/kWh)</summary>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:6px 0">
              <label style="font-size:12px">Szczyt (peak_gross)<br>
                <input type="number" id="tf_peak_gross" step="0.0001" min="0" style="width:100%;padding:4px;margin-top:2px" placeholder="1.23">
              </label>
              <label style="font-size:12px">Pozaszczyt (offpeak_gross)<br>
                <input type="number" id="tf_offpeak_gross" step="0.0001" min="0" style="width:100%;padding:4px;margin-top:2px" placeholder="0.63">
              </label>
            </div>
          </details>
          <details style="margin-bottom:10px">
            <summary style="cursor:pointer;font-weight:600;font-size:13px;margin-bottom:8px">Energia netto (PLN/kWh)</summary>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:6px 0">
              <label style="font-size:12px">Energia szczyt (energy_peak_net)<br>
                <input type="number" id="tf_energy_peak_net" step="0.0001" min="0" style="width:100%;padding:4px;margin-top:2px">
              </label>
              <label style="font-size:12px">Energia pozaszczyt (energy_offpeak_net)<br>
                <input type="number" id="tf_energy_offpeak_net" step="0.0001" min="0" style="width:100%;padding:4px;margin-top:2px">
              </label>
            </div>
          </details>
          <details style="margin-bottom:10px">
            <summary style="cursor:pointer;font-weight:600;font-size:13px;margin-bottom:8px">Dystrybucja zmienna netto (PLN/kWh)</summary>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:6px 0">
              <label style="font-size:12px">Składnik sieciowy szczyt (dist_var_peak_net)<br>
                <input type="number" id="tf_dist_var_peak_net" step="0.0001" min="0" style="width:100%;padding:4px;margin-top:2px">
              </label>
              <label style="font-size:12px">Składnik sieciowy poza (dist_var_offpeak_net)<br>
                <input type="number" id="tf_dist_var_offpeak_net" step="0.0001" min="0" style="width:100%;padding:4px;margin-top:2px">
              </label>
            </div>
          </details>
          <details style="margin-bottom:10px">
            <summary style="cursor:pointer;font-weight:600;font-size:13px;margin-bottom:8px">Opłaty jakościowe netto (PLN/kWh)</summary>
            <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;padding:6px 0">
              <label style="font-size:12px">Jakościowa (dist_jakosciowa_net)<br>
                <input type="number" id="tf_dist_jakosciowa_net" step="0.0001" min="0" style="width:100%;padding:4px;margin-top:2px">
              </label>
              <label style="font-size:12px">OZE (dist_oze_net)<br>
                <input type="number" id="tf_dist_oze_net" step="0.0001" min="0" style="width:100%;padding:4px;margin-top:2px">
              </label>
              <label style="font-size:12px">Kogeneracja (dist_kogeneracja_net)<br>
                <input type="number" id="tf_dist_kogeneracja_net" step="0.0001" min="0" style="width:100%;padding:4px;margin-top:2px">
              </label>
            </div>
          </details>
          <details style="margin-bottom:10px">
            <summary style="cursor:pointer;font-weight:600;font-size:13px;margin-bottom:8px">Opłaty stałe netto (PLN/miesiąc)</summary>
            <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;padding:6px 0">
              <label style="font-size:12px">Abonament (fixed_abonament_net)<br>
                <input type="number" id="tf_fixed_abonament_net" step="0.01" min="0" style="width:100%;padding:4px;margin-top:2px">
              </label>
              <label style="font-size:12px">Stały sieciowy (fixed_stalysieciowy_net)<br>
                <input type="number" id="tf_fixed_stalysieciowy_net" step="0.01" min="0" style="width:100%;padding:4px;margin-top:2px">
              </label>
              <label style="font-size:12px">Mocowa (fixed_mocowa_net)<br>
                <input type="number" id="tf_fixed_mocowa_net" step="0.01" min="0" style="width:100%;padding:4px;margin-top:2px">
              </label>
            </div>
          </details>
          <p style="font-size:11px;color:#888;margin:0 0 10px">
            Pola puste = wartości nie zostaną nadpisane przez ten wpis. Publikacja do MQTT
            nastąpi przy najbliższym pollu (do 30 min).
          </p>
          <button type="submit" class="btn" style="margin-right:8px">Zapisz wpis</button>
          <button type="button" class="btn" onclick="clearTaryfaForm()" style="background:#6c757d">Wyczyść formularz</button>
        </form>
        <div id="taryfaMsg" style="margin-top:10px;font-size:13px"></div>
      </div>
      <!-- RCE vs RCEm tab -->
      <div id="tab-rce" style="display:none;padding:12px">
        <div id="rceWarning" style="display:none;margin-bottom:12px;padding:10px 14px;background:#fff3cd;border-left:4px solid #f0ad4e;border-radius:4px;font-size:13px">
          &#9888;&#65039; Za mało rozliczonych miesięcy — rekomendacja będzie wiarygodna po 3 pełnych miesiącach z opublikowaną RCEm.
        </div>
        <div id="rceKpiCards" style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:16px"></div>
        <div class="chart-wrap" style="height:280px;margin-bottom:16px">
          <h3>Przychód ze sprzedaży nadwyżek: RCEm vs RCE godzinowa (zł/mies.)</h3>
          <canvas id="rceCmpChart"></canvas>
        </div>
        <div style="margin-bottom:16px">
          <h4 style="margin:0 0 6px;font-size:13px;font-weight:600">Heatmapa: eksport × cena RCE (godzina doby × miesiąc)
            <label style="font-weight:400;font-size:11px;margin-left:10px"><input type="radio" name="hmMetric" value="price" checked onchange="redrawRceHeatmap()"> cena RCE</label>
            <label style="font-weight:400;font-size:11px;margin-left:6px"><input type="radio" name="hmMetric" value="kwh" onchange="redrawRceHeatmap()"> eksport kWh</label>
          </h4>
          <div id="rceHeatmap" style="overflow-x:auto"></div>
          <div style="font-size:11px;color:var(--muted);margin-top:4px">Kolor = śr. cena RCE ważona eksportem (czerwień = godziny z ceną ujemną → 0 zł, art. 4b ustawy o OZE) lub wolumen eksportu. Najedź na komórkę po szczegóły.</div>
        </div>
        <div class="tbl-wrap"><table id="rceTbl"></table></div>
        <div class="tbl-foot" id="rceFoot"></div>
      </div>
      <!-- Faktury Tauron tab -->
      <div id="tab-invoices" style="display:none;padding:12px">
        <!-- KPI summary cards -->
        <div id="invKpiCards" style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px"></div>
        <!-- Drift banners -->
        <div id="tariffDriftBanner" style="display:none;margin-bottom:10px;padding:8px 12px;background:#fff3cd;border-left:4px solid #f0ad4e;border-radius:4px;font-size:13px"></div>
        <!-- Coverage grid -->
        <div style="margin-bottom:16px">
          <h4 style="margin:0 0 8px;font-size:13px;font-weight:600">Pokrycie fakturami</h4>
          <div id="invCoverageGrid"></div>
        </div>
        <!-- Cost breakdown: where the grid-purchase money goes, per component -->
        <div style="margin-bottom:16px">
          <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:8px">
            <h4 style="margin:0;font-size:13px;font-weight:600">Składniki kosztów — na co idą pieniądze</h4>
            <div id="costBreakdownToggle" style="display:flex;gap:0;font-size:11px"></div>
          </div>
          <div id="costBreakdownNote" style="font-size:11px;color:var(--muted);margin-bottom:8px"></div>
          <div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start">
            <div id="costBreakdownTableWrap" class="tbl-wrap" style="flex:1;min-width:280px"></div>
            <div class="chart-wrap" style="flex:2;min-width:320px;height:280px">
              <canvas id="costBreakdownChart"></canvas>
            </div>
          </div>
        </div>
        <!-- Deposit: expiry/refund KPIs + trend chart -->
        <div style="margin-bottom:16px">
          <h4 style="margin:0 0 8px;font-size:13px;font-weight:600">Depozyt prosumencki — saldo, przedawnienie 12 mies. i zwrot</h4>
          <div id="depKpiCards" style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:10px"></div>
          <div class="chart-wrap" style="height:240px;margin-bottom:8px">
            <canvas id="depositChart"></canvas>
          </div>
          <div id="depositNote" style="font-size:11px;color:var(--muted)"></div>
        </div>
        <!-- Deposit reconciliation: invoices vs inverter model -->
        <div style="margin-bottom:16px">
          <h4 style="margin:0 0 8px;font-size:13px;font-weight:600">Depozyt — faktury vs falownik (rekonsyliacja zasileń)</h4>
          <div id="reconKpiCards" style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:10px"></div>
          <div id="reconTableWrap" class="tbl-wrap" style="max-height:380px"></div>
          <div id="reconNote" style="font-size:11px;color:var(--muted);margin-top:6px"></div>
        </div>
        <!-- Invoice table (click to expand) -->
        <div style="margin-bottom:16px">
          <h4 style="margin:0 0 8px;font-size:13px;font-weight:600">Wgrane faktury</h4>
          <div class="override-form" style="align-items:flex-start;gap:12px;margin-bottom:10px">
            <label style="display:flex;flex-direction:column;gap:4px;font-size:13px">
              Wgraj faktury PDF (jedna lub wiele):
              <input type="file" id="invoiceFiles" accept=".pdf" multiple style="margin-top:4px">
            </label>
            <button onclick="uploadInvoices()" style="align-self:flex-end">Wgraj i uzgodnij</button>
            <span id="invoiceMsg" style="align-self:flex-end;font-size:12px"></span>
          </div>
          <div id="invoiceTableWrap" style="overflow-x:auto"></div>
        </div>
        <!-- Trained layouts panel -->
        <div id="invLayoutsPanel" style="margin-bottom:16px;padding:10px 12px;background:var(--bg);border-radius:4px;border:1px solid var(--border)"></div>
        <!-- Raw-text debug view -->
        <details style="margin-bottom:16px">
          <summary style="cursor:pointer;font-size:13px;color:var(--muted)">Diagnoza parsera PDF (debug)</summary>
          <div style="margin-top:8px;display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
            <label style="font-size:13px">Plik PDF:
              <input type="file" id="debugPdfFile" accept=".pdf" style="margin-left:6px">
            </label>
            <button onclick="runInvoiceDebug()" style="font-size:12px">Pokaż surowy tekst</button>
          </div>
          <div id="debugOutput" style="margin-top:10px"></div>
        </details>
      </div>
    </div>

    <div class="override-wrap" style="margin-top:18px">
      <h3>Reczne nadpisanie ceny RCEm<span id="rcemBadge" class="rcem-badge"></span></h3>
      <div class="override-form">
        <label>Miesiac: <input type="month" id="ovMonth"></label>
        <label>Cena (zl/kWh): <input type="number" id="ovPrice" step="0.0001" min="0.0001" max="2" placeholder="0.0000" style="width:100px"></label>
        <button onclick="submitOverride()">Zapisz</button>
        <span id="ovMsg"></span>
      </div>
    </div>

  </div>
</main>
<script>
'use strict';
let _lineChart = null, _barChart = null, _rcemChart = null, _autarkiaChart = null, _prodChart = null, _arbitrageChart = null, _netCostChart = null, _priceSpreadChart = null, _yieldChart = null, _energyBalChart = null, _yearCompChart = null, _prodRankChart = null, _depositChart = null, _costBreakdownChart = null;
let _tariffPriceChart = null, _tariffCompChart = null, _tariffCumChart = null, _tariffSeasonChart = null, _tariffHistChart = null;
let _rceCmpChart = null;
let _fanChart = null, _waterfallChart = null, _sankeyChart = null, _cpiRealChart = null, _degradChart = null;
let _lastRecords = [], _lastInvoices = [], _lastRceMonths = [];

/* -- Formatters -- */
function fmt(v, dp, sfx) {
  if (v == null) return '—';
  let s = Number(v).toLocaleString('pl-PL', {minimumFractionDigits: dp, maximumFractionDigits: dp});
  return sfx ? s + ' ' + sfx : s;
}
const pln   = (v, dp=0) => fmt(v, dp, 'zl');
const pct   = (v)       => fmt(v, 2, '%');
const kwh   = (v)       => fmt(v, 1, 'kWh');
const price = (v)       => fmt(v, 4, 'zl/kWh');
const num   = (v, dp=1) => fmt(v, dp);

function feedinPriceCell(r) {
  const hist = r.feedin_corrections;
  if (!hist || hist.length < 2) return price(r.feedin_price);
  const prev = hist[hist.length - 2].price;
  const curr = hist[hist.length - 1].price;
  const dir  = curr < prev ? 'price-down' : 'price-up';
  let tip = 'Historia cen RCEm:\n';
  hist.forEach((h, i) => {
    const marker = i === hist.length - 1 ? ' ◄' : '';
    tip += (h.price * 1000).toFixed(2) + ' zl/MWh  (' + h.recorded_at + ')' + marker + '\n';
  });
  const safeTip = tip.trimEnd().replace(/"/g, '&quot;');
  return '<span class="price-corrected ' + dir + '" data-tip="' + safeTip + '">' + price(r.feedin_price) + '</span>';
}

// Toggle correction tooltip on click (mobile-friendly)
document.addEventListener('click', function(e) {
  const el = e.target.closest('.price-corrected');
  document.querySelectorAll('.price-corrected.tip-open').forEach(x => { if (x !== el) x.classList.remove('tip-open'); });
  if (el) el.classList.toggle('tip-open');
});

/* -- Tab switching -- */
function showTab(name) {
  const TABS = ['hist','pred','years','charts','invoices','tariff','taryfa','rce'];
  TABS.forEach(t => {
    document.getElementById('tab-' + t).style.display = (t === name) ? '' : 'none';
  });
  document.querySelectorAll('.tab-btn').forEach((b, i) =>
    b.classList.toggle('active', TABS[i] === name)
  );
  if (name === 'rce' && _rceCmpChart) _rceCmpChart.resize();
  if (name === 'pred' && _fanChart) _fanChart.resize();
  if (name === 'charts') {
    [_rcemChart, _autarkiaChart, _prodChart, _arbitrageChart, _netCostChart,
     _priceSpreadChart, _yieldChart, _energyBalChart, _yearCompChart, _prodRankChart,
     _cpiRealChart, _degradChart, _waterfallChart, _sankeyChart].forEach(c => c && c.resize());
  }
  if (name === 'invoices' && _depositChart) _depositChart.resize();
  if (name === 'invoices' && _costBreakdownChart) _costBreakdownChart.resize();
  if (name === 'tariff') {
    [_tariffPriceChart, _tariffCompChart, _tariffCumChart,
     _tariffSeasonChart, _tariffHistChart].forEach(c => c && c.resize());
    if (!_rangeData) fetchTariffRange();
  }
  if (name === 'taryfa') loadTaryfaTab();
}

/* -- Summary cards -- */
function renderCards(s) {
  const roi = s.roi_pct;
  const cards = [
    { lbl: 'ROI',               val: pct(roi),                    cls: roi >= 100 ? 'c-green' : 'c-blue' },
    { lbl: 'Laczny zwrot',      val: pln(s.total_return),         sub: 'subsydium ' + pln(s.subsidy) + ' + oszcz. ' + pln(s.total_savings) },
    { lbl: 'Oszczednosci',      val: pln(s.total_savings),        sub: 'autokons. ' + pln(s.self_consumption_savings) + ' / sprzedaz ' + pln(s.feedin_revenue) + (s.battery_arbitrage_savings > 0 ? ' / arbitraz ' + pln(s.battery_arbitrage_savings) : '') },
    { lbl: 'Pozostalo',         val: pln(s.remaining_to_recover), sub: 'inwestycja brutto ' + pln(s.gross_investment) },
    { lbl: 'Srednia mies.',     val: pln(s.monthly_avg_savings),  sub: 'ost. ' + s.avg_window + ' mies.' },
    { lbl: 'Splata', val: s.payback_date || '—', sub: (s.years_to_payback != null ? 'za ' + num(s.years_to_payback, 1) + ' lat' : '') + (s.payback_date_p10 && s.payback_date_p90 ? '  •  P10: ' + s.payback_date_p10.slice(0,7) + '  P90: ' + s.payback_date_p90.slice(0,7) : '') },
    { lbl: 'Produkcja lacznie', val: kwh(s.total_produced_kwh),   sub: 'uzysk ' + num(s.specific_yield, 0) + ' kWh/kWp' },
    s.battery_arbitrage_savings > 0 ? { lbl: 'Arbitraż baterii', val: pln(s.battery_arbitrage_savings, 2), sub: 'łącznie z sieci w taniej taryfie', cls: 'c-green' } : null,
    s.battery_arbitrage_savings > 0 && s.total_savings > 0 ? { lbl: 'Udział arbitrażu', val: pct(s.battery_arbitrage_savings / s.total_savings * 100), sub: 'bateria w łącznych oszczędnościach', cls: 'c-green' } : null,
    s.best_month  ? { lbl: 'Najlepszy miesiac',  val: pln(s.best_month.savings),  sub: s.best_month.label,  cls: 'c-green' } : null,
    s.worst_month ? { lbl: 'Najslabszy miesiac', val: pln(s.worst_month.savings), sub: s.worst_month.label } : null,
    s.solcast_projected_kwh != null ? { lbl: 'Prognoza produkcji', val: kwh(s.solcast_projected_kwh), sub: 'produkcja + Solcast 7 dni' } : null,
    s.projected_month_savings_pln != null ? { lbl: 'Prognoza oszcz. (mies.)', val: pln(s.projected_month_savings_pln), sub: 'Solcast × sr. zl/kWh (mies.)', cls: 'c-green' } : null,
    // Financial analysis
    s.real_roi_pct != null ? { lbl: 'ROI realny', val: pct(s.real_roi_pct), sub: 'po inflacji ' + num(s.cumulative_inflation_pct, 1) + '% (' + (s.inflation_source && s.inflation_source !== 'flat fallback' ? 'GUS' : 'szacunek') + ')', cls: s.real_roi_pct >= 100 ? 'c-green' : 'c-blue' } : null,
    s.cumulative_inflation_pct != null ? { lbl: 'Inflacja skumulowana', val: pct(s.cumulative_inflation_pct), sub: 'od uruchomienia · ' + (s.inflation_source || 'szacunek') } : null,
    s.npv != null ? { lbl: 'NPV @ 4%', val: pln(s.npv), sub: 'stopa dyskontowa 4%', cls: s.npv >= 0 ? 'c-green' : '' } : null,
    s.irr_pct != null ? { lbl: 'IRR', val: pct(s.irr_pct), sub: 'wewnętrzna stopa zwrotu', cls: 'c-green' } : null,
    s.counterfactual_delta != null ? { lbl: 'vs Obligacje 10Y', val: pln(s.counterfactual_delta), sub: 'PV vs 5.5% obligacja', cls: s.counterfactual_delta >= 0 ? 'c-green' : '' } : null,
    { lbl: 'Zysk netto', val: pln(s.net_profit || 0), cls: (s.net_profit || 0) > 0 ? 'c-green' : '', sub: (s.net_profit || 0) > 0 ? 'ponad inwestycje brutto' : 'przed splata' },
    s.self_sufficiency_avg != null ? { lbl: 'Autarkia', val: pct(s.self_sufficiency_avg), sub: 'udzial autokonsumpcji' } : null,
    s.self_consumption_rate_pct != null ? { lbl: 'Autokonsumpcja', val: fmt(s.self_consumption_rate_pct, 1, '%'), sub: 'udział produkcji zużytej na miejscu' } : null,
    s.co2_avoided_kg != null && s.co2_avoided_kg > 0 ? { lbl: 'CO₂ unikniete', val: s.co2_avoided_kg >= 1000 ? fmt(s.co2_avoided_kg / 1000, 2, 't') : fmt(s.co2_avoided_kg, 0, 'kg'), sub: 'wskaźnik KOBiZE', cls: 'c-green' } : null,
    s.yoy_yield_delta_pct != null ? { lbl: 'Produkcja r/r', val: fmt(s.yoy_yield_delta_pct, 1, '%'), sub: 'te same miesiące rok do roku', cls: s.yoy_yield_delta_pct >= 0 ? 'c-green' : '' } : null,
    { lbl: 'Koszt netto sieci', val: pln(s.net_grid_cost_total), sub: 'zakup − sprzedaz lacznie' },
    (() => {
      const mp = s.month_progress;
      if (!mp || !s.monthly_avg_savings) return null;
      const pace = mp.pace_projected != null ? pln(mp.pace_projected) + ' proj.' : null;
      const sub = mp.days_elapsed + '/' + mp.days_in_month + ' dni'
        + (pace ? '  •  ' + pace : '')
        + (mp.pace_vs_avg_pct != null ? '  •  ' + pct(mp.pace_vs_avg_pct) + ' sr.' : '');
      const cls = mp.pace_vs_avg_pct != null && mp.pace_vs_avg_pct >= 100 ? 'c-green' : '';
      return { lbl: 'Postep miesiaca', val: pln(mp.savings_so_far), sub, cls };
    })(),
  ].filter(Boolean);

  document.getElementById('cards').innerHTML = cards.map(c =>
    '<div class="card ' + (c.cls||'') + '">' +
      '<div class="lbl">' + c.lbl + '</div>' +
      '<div class="val">' + c.val + '</div>' +
      (c.sub ? '<div class="sub">' + c.sub + '</div>' : '') +
    '</div>'
  ).join('');
}

/* -- Line chart (cumulative) -- */
function renderLineChart(records, predictions, gross, netInvestment) {
  const histLbls = records.map(r => r.month_label);
  const histVals = records.map(r => r.cumulative_return);
  const predLbls = predictions.map(p => p.month_label);
  const predVals = predictions.map(p => p.cumulative_return || (p.net_profit != null ? gross + p.net_profit : null));

  const allLbls  = [...histLbls, ...predLbls];
  const grossLine = allLbls.map(() => gross);
  const netLine   = netInvestment != null ? allLbls.map(() => netInvestment) : null;

  const hDs = [...histVals, ...predLbls.map(() => null)];
  const pDs = [...histLbls.map(() => null)];
  if (histVals.length) pDs[histVals.length - 1] = histVals[histVals.length - 1];
  pDs.push(...predVals);

  const datasets = [
    { label: 'Zwrot (historia)', data: hDs,       borderColor: '#2563eb', backgroundColor: 'rgba(37,99,235,.07)', fill: true,  tension: 0.35, pointRadius: allLbls.length > 60 ? 0 : 3, spanGaps: false },
    { label: 'Zwrot (prognoza)', data: pDs,       borderColor: '#2563eb', borderDash: [6,4], backgroundColor: 'transparent', fill: false, tension: 0.35, pointRadius: 0, spanGaps: false },
    { label: 'Inwestycja brutto', data: grossLine, borderColor: '#dc2626', borderDash: [4,4], backgroundColor: 'transparent', fill: false, pointRadius: 0 },
  ];
  if (netLine) {
    datasets.push({ label: 'Inwestycja netto', data: netLine, borderColor: '#16a34a', borderDash: [4,4], backgroundColor: 'transparent', fill: false, pointRadius: 0 });
  }

  const ctx = document.getElementById('lineChart').getContext('2d');
  if (_lineChart) _lineChart.destroy();
  _lineChart = new Chart(ctx, {
    type: 'line',
    data: { labels: allLbls, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: c => c.raw == null ? null : c.dataset.label + ': ' + Number(c.raw).toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' zl' } }
      },
      scales: {
        x: { ticks: { maxTicksLimit: 24, font: { size: 10 }, maxRotation: 45 } },
        y: { ticks: { callback: v => (v/1000).toFixed(0) + 'k zl', font: { size: 10 } } },
      },
    },
  });
}

/* -- Bar chart (monthly breakdown) -- */
function renderBarChart(records) {
  const nonEmpty = records.filter(r => (r.self_savings || 0) + (r.feedin_revenue || 0) + (r.battery_arbitrage_savings || 0) > 0);
  const recent   = nonEmpty.slice(-24);
  const labels   = recent.map(r => r.month_label);
  const autokons = recent.map(r => r.self_savings              || 0);
  const sprzedaz = recent.map(r => r.feedin_revenue            || 0);
  const arbitraz = recent.map(r => r.battery_arbitrage_savings || 0);

  const ctx = document.getElementById('barChart').getContext('2d');
  if (_barChart) _barChart.destroy();
  _barChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Autokonsumpcja', data: autokons, backgroundColor: 'rgba(37,99,235,0.75)',  borderColor: '#2563eb', borderWidth: 1, stack: 'savings' },
        { label: 'Sprzedaz',       data: sprzedaz, backgroundColor: 'rgba(22,163,74,0.75)',  borderColor: '#16a34a', borderWidth: 1, stack: 'savings' },
        { label: 'Arbitraz bat.',  data: arbitraz, backgroundColor: 'rgba(234,179,8,0.80)',  borderColor: '#ca8a04', borderWidth: 1, stack: 'savings' },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } },
        tooltip: {
          mode: 'index',
          callbacks: {
            label: c => c.dataset.label + ': ' + Number(c.raw).toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' zl',
            footer: items => {
              const sum = items.reduce((a, c) => a + (c.raw || 0), 0);
              return 'Lacznie: ' + sum.toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' zl';
            },
          },
        },
      },
      scales: {
        x: { stacked: true, ticks: { maxTicksLimit: 24, font: { size: 9 }, maxRotation: 45 } },
        y: { stacked: true, ticks: { callback: v => v.toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' zl', font: { size: 10 } } },
      },
    },
  });
}

/* -- RCEm price history chart -- */
function renderRcemChart(records) {
  const withPrice = records.filter(r => r.feedin_price != null);
  const labels = withPrice.map(r => r.month_label);
  const prices = withPrice.map(r => r.feedin_price);
  const ctx = document.getElementById('rcemChart').getContext('2d');
  if (_rcemChart) _rcemChart.destroy();
  _rcemChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{ label: 'RCEm (zl/kWh)', data: prices, borderColor: '#b45309', backgroundColor: 'rgba(180,83,9,.07)', fill: true, tension: 0.3, pointRadius: 3, pointHoverRadius: 5 }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => 'RCEm: ' + Number(c.raw).toLocaleString('pl-PL', {minimumFractionDigits: 4}) + ' zl/kWh' } }
      },
      scales: {
        x: { ticks: { maxTicksLimit: 30, font: { size: 9 }, maxRotation: 45 } },
        y: { ticks: { callback: v => v.toFixed(4) + ' zl', font: { size: 10 } }, beginAtZero: false },
      },
    },
  });
}

/* -- Autarkia chart -- */
function renderAutarkiaChart(records) {
  const withData = records.filter(r => r.self_sufficiency_pct != null);
  const labels = withData.map(r => r.month_label);
  const values = withData.map(r => r.self_sufficiency_pct);
  const ctx = document.getElementById('autarkiaChart').getContext('2d');
  if (_autarkiaChart) _autarkiaChart.destroy();
  _autarkiaChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{ label: 'Autarkia (%)', data: values, borderColor: '#16a34a', backgroundColor: 'rgba(22,163,74,.07)', fill: true, tension: 0.3, pointRadius: 2, pointHoverRadius: 5 }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => 'Autarkia: ' + Number(c.raw).toLocaleString('pl-PL', {minimumFractionDigits: 1}) + '%' } }
      },
      scales: {
        x: { ticks: { maxTicksLimit: 30, font: { size: 9 }, maxRotation: 45 } },
        y: { min: 0, max: 100, ticks: { callback: v => v + '%', font: { size: 10 } } },
      },
    },
  });
}

/* -- Production + grid purchase per tariff chart -- */
function renderProdChart(records) {
  const recent = records.slice(-36);
  const labels  = recent.map(r => r.month_label);
  const sc      = recent.map(r => r.self_consumed_kwh       || 0);
  const exp     = recent.map(r => r.exported_kwh            || 0);
  const buyPeak = recent.map(r => r.purchased_kwh_peak      || 0);
  const buyOff  = recent.map(r => r.purchased_kwh_offpeak   || 0);
  const ctx = document.getElementById('prodChart').getContext('2d');
  if (_prodChart) _prodChart.destroy();
  _prodChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Autokonsumpcja',      data: sc,      backgroundColor: 'rgba(37,99,235,0.80)',  borderColor: '#2563eb', borderWidth: 1, stack: 'prod' },
        { label: 'Eksport',             data: exp,     backgroundColor: 'rgba(22,163,74,0.80)',  borderColor: '#16a34a', borderWidth: 1, stack: 'prod' },
        { label: 'Zakup poza szczytem', data: buyOff,  backgroundColor: 'rgba(139,92,246,0.75)', borderColor: '#7c3aed', borderWidth: 1, stack: 'buy' },
        { label: 'Zakup w szczycie',    data: buyPeak, backgroundColor: 'rgba(234,88,12,0.80)',  borderColor: '#c2410c', borderWidth: 1, stack: 'buy' },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: c => c.dataset.label + ': ' + Number(c.raw).toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' kWh' } }
      },
      scales: {
        x: { stacked: true, ticks: { maxTicksLimit: 36, font: { size: 9 }, maxRotation: 45 } },
        y: { stacked: false, ticks: { callback: v => v.toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' kWh', font: { size: 10 } } },
      },
    },
  });
}

/* -- Battery arbitrage chart -- */
function renderArbitrageChart(records) {
  const recent = records.filter(r => r.battery_arbitrage_savings != null).slice(-24);
  const labels = recent.map(r => r.month_label);
  const values = recent.map(r => r.battery_arbitrage_savings || 0);
  const ctx = document.getElementById('arbitrageChart').getContext('2d');
  if (_arbitrageChart) _arbitrageChart.destroy();
  _arbitrageChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{ label: 'Arbitraż baterii (zl)', data: values, backgroundColor: 'rgba(22,163,74,0.75)', borderColor: '#16a34a', borderWidth: 1 }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => 'Arbitraż: ' + Number(c.raw).toLocaleString('pl-PL', {minimumFractionDigits: 2}) + ' zl' } }
      },
      scales: {
        x: { ticks: { maxTicksLimit: 24, font: { size: 9 }, maxRotation: 45 } },
        y: { beginAtZero: true, ticks: { callback: v => v.toLocaleString('pl-PL', {maximumFractionDigits: 2}) + ' zl', font: { size: 10 } } },
      },
    },
  });
}

/* -- Net grid cost chart -- */
function renderNetCostChart(records) {
  const recent = records.filter(r => r.net_grid_cost != null).slice(-36);
  const labels = recent.map(r => r.month_label);
  const values = recent.map(r => r.net_grid_cost || 0);
  const colors = values.map(v => v >= 0 ? 'rgba(220,38,38,0.75)' : 'rgba(22,163,74,0.75)');
  const borders = values.map(v => v >= 0 ? '#dc2626' : '#16a34a');
  const ctx = document.getElementById('netCostChart').getContext('2d');
  if (_netCostChart) _netCostChart.destroy();
  _netCostChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{ label: 'Koszt netto sieci (zl)', data: values, backgroundColor: colors, borderColor: borders, borderWidth: 1 }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => 'Koszt netto: ' + Number(c.raw).toLocaleString('pl-PL', {minimumFractionDigits: 2}) + ' zl' } }
      },
      scales: {
        x: { ticks: { maxTicksLimit: 36, font: { size: 9 }, maxRotation: 45 } },
        y: { ticks: { callback: v => v.toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' zl', font: { size: 10 } } },
      },
    },
  });
}

/* -- Price spread: buy vs sell -- */
function renderPriceSpreadChart(records) {
  const withBoth = records.filter(r => r.buy_price != null && r.feedin_price != null);
  const labels = withBoth.map(r => r.month_label);
  const buy  = withBoth.map(r => r.buy_price);
  const sell = withBoth.map(r => r.feedin_price);
  const ctx = document.getElementById('priceSpreadChart').getContext('2d');
  if (_priceSpreadChart) _priceSpreadChart.destroy();
  _priceSpreadChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Zakup (zl/kWh)',   data: buy,  borderColor: '#dc2626', backgroundColor: 'rgba(220,38,38,.06)',  fill: true,  tension: 0.3, pointRadius: 2, pointHoverRadius: 5 },
        { label: 'Sprzedaz (zl/kWh)', data: sell, borderColor: '#b45309', backgroundColor: 'rgba(180,83,9,.06)',  fill: false, tension: 0.3, pointRadius: 2, pointHoverRadius: 5 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: c => c.dataset.label + ': ' + Number(c.raw).toLocaleString('pl-PL', {minimumFractionDigits: 4}) + ' zl/kWh' } }
      },
      scales: {
        x: { ticks: { maxTicksLimit: 30, font: { size: 9 }, maxRotation: 45 } },
        y: { beginAtZero: false, ticks: { callback: v => v.toFixed(4) + ' zl', font: { size: 10 } } },
      },
    },
  });
}

/* -- Monthly specific yield (kWh/kWp) -- */
function renderYieldChart(records, systemKwp) {
  const recent = records.filter(r => r.produced_kwh != null).slice(-36);
  const labels = recent.map(r => r.month_label);
  const yields = recent.map(r => systemKwp > 0 ? Math.round(r.produced_kwh / systemKwp * 10) / 10 : 0);
  const ctx = document.getElementById('yieldChart').getContext('2d');
  if (_yieldChart) _yieldChart.destroy();
  _yieldChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{ label: 'Uzysk (kWh/kWp)', data: yields, backgroundColor: 'rgba(234,179,8,0.75)', borderColor: '#ca8a04', borderWidth: 1 }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => 'Uzysk: ' + Number(c.raw).toLocaleString('pl-PL', {minimumFractionDigits: 1}) + ' kWh/kWp' } }
      },
      scales: {
        x: { ticks: { maxTicksLimit: 36, font: { size: 9 }, maxRotation: 45 } },
        y: { beginAtZero: true, ticks: { callback: v => v + ' kWh/kWp', font: { size: 10 } } },
      },
    },
  });
}

/* -- Energy balance: produced vs consumed -- */
function renderEnergyBalChart(records) {
  const recent = records.filter(r => r.produced_kwh != null && r.consumed_kwh != null).slice(-36);
  const labels   = recent.map(r => r.month_label);
  const produced = recent.map(r => r.produced_kwh  || 0);
  const consumed = recent.map(r => r.consumed_kwh  || 0);
  const colors   = recent.map((r, i) => produced[i] >= consumed[i] ? 'rgba(37,99,235,0.80)' : 'rgba(220,38,38,0.80)');
  const ctx = document.getElementById('energyBalChart').getContext('2d');
  if (_energyBalChart) _energyBalChart.destroy();
  _energyBalChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Produkcja (kWh)', data: produced, backgroundColor: colors,                         borderColor: '#2563eb', borderWidth: 1 },
        { label: 'Zuzycie (kWh)',   data: consumed, backgroundColor: 'rgba(100,116,139,0.40)', borderColor: '#475569', borderWidth: 1 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: c => c.dataset.label + ': ' + Number(c.raw).toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' kWh' } }
      },
      scales: {
        x: { ticks: { maxTicksLimit: 36, font: { size: 9 }, maxRotation: 45 } },
        y: { beginAtZero: true, ticks: { callback: v => v.toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' kWh', font: { size: 10 } } },
      },
    },
  });
}

/* -- Year-over-year production comparison -- */
function renderYearCompChart(records) {
  const PL_M = ['Sty','Lut','Mar','Kwi','Maj','Cze','Lip','Sie','Wrz','Paź','Lis','Gru'];
  const byYear = {};
  for (const r of records) {
    if (r.produced_kwh == null) continue;
    const year = r.month_label.substring(0, 4);
    const abbr = r.month_label.slice(5);
    const mi   = PL_M.indexOf(abbr);
    if (mi < 0) continue;
    if (year === '2023' && mi === 5) continue; // June 2023 — partial month (system started mid-June)
    if (!byYear[year]) byYear[year] = new Array(12).fill(null);
    byYear[year][mi] = Math.round(r.produced_kwh * 10) / 10;
  }
  const years = Object.keys(byYear).sort().slice(-5);
  const palette = ['#2563eb','#16a34a','#b45309','#dc2626','#7c3aed'];
  const datasets = years.map((y, i) => ({
    label: y,
    data: byYear[y],
    borderColor: palette[i % palette.length],
    backgroundColor: 'transparent',
    tension: 0.3,
    pointRadius: 3,
    pointHoverRadius: 6,
    spanGaps: false,
  }));
  const ctx = document.getElementById('yearCompChart').getContext('2d');
  if (_yearCompChart) _yearCompChart.destroy();
  _yearCompChart = new Chart(ctx, {
    type: 'line',
    data: { labels: PL_M, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: c => c.dataset.label + ': ' + (c.raw != null ? Number(c.raw).toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' kWh' : '—') } }
      },
      scales: {
        x: { ticks: { font: { size: 10 } } },
        y: { beginAtZero: true, ticks: { callback: v => v.toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' kWh', font: { size: 10 } } },
      },
    },
  });
}

/* -- Production ranking chart (best to worst months) -- */
const _prodRankPlugin = {
  id: 'prodRankExtras',
  afterDatasetsDraw(chart) {
    const ctx = chart.ctx, area = chart.chartArea;
    const values = chart.data.datasets[0].data;
    const meta = chart.getDatasetMeta(0);
    ctx.save();
    ctx.font = '10px system-ui, sans-serif';
    // wartości kWh na końcach słupków
    ctx.fillStyle = '#475569';
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    meta.data.forEach((bar, i) => {
      ctx.fillText(Number(values[i]).toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' kWh',
                   Math.min(bar.x, area.right) + 4, bar.y);
    });
    // linia średniej
    const avg = chart.options._avgValue;
    if (avg != null && chart.scales.x) {
      const x = chart.scales.x.getPixelForValue(avg);
      if (x > area.left && x < area.right) {
        ctx.strokeStyle = 'rgba(220,38,38,0.65)';
        ctx.setLineDash([4, 3]);
        ctx.beginPath(); ctx.moveTo(x, area.top); ctx.lineTo(x, area.bottom); ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = 'rgba(220,38,38,0.9)';
        ctx.textAlign = 'center';
        ctx.fillText('śr. ' + Math.round(avg).toLocaleString('pl-PL') + ' kWh', x, area.bottom - 10);
      }
    }
    ctx.restore();
  },
};

function renderProdRankChart(records) {
  const ranked = records
    .filter(r => r.produced_kwh != null && !r.is_current)
    .sort((a, b) => b.produced_kwh - a.produced_kwh);
  if (!ranked.length) return;

  const years = [...new Set(ranked.map(r => r.month_label.slice(0, 4)))].sort();
  const YEAR_COLORS = ['rgba(100,116,139,0.80)', 'rgba(37,99,235,0.80)', 'rgba(22,163,74,0.80)',
                       'rgba(234,88,12,0.85)', 'rgba(147,51,234,0.80)', 'rgba(202,138,4,0.85)'];
  const colorOfYear = y => YEAR_COLORS[years.indexOf(y) % YEAR_COLORS.length];
  const MEDALS = ['\u{1F947}', '\u{1F948}', '\u{1F949}'];

  const labels = ranked.map((r, i) => (i < 3 ? MEDALS[i] + ' ' : '') + r.month_label);
  const values = ranked.map(r => Math.round(r.produced_kwh * 10) / 10);
  const colors = ranked.map(r => colorOfYear(r.month_label.slice(0, 4)));
  const avg = values.reduce((s, v) => s + v, 0) / values.length;

  // wysokość dopasowana do liczby miesięcy (22 px na słupek + legenda/osie)
  const wrap = document.getElementById('prodRankWrap');
  if (wrap) wrap.style.height = Math.max(260, ranked.length * 22 + 110) + 'px';

  const ctx = document.getElementById('prodRankChart').getContext('2d');
  if (_prodRankChart) _prodRankChart.destroy();
  _prodRankChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{ label: 'Produkcja (kWh)', data: values, backgroundColor: colors, borderWidth: 0 }],
    },
    options: {
      indexAxis: 'y',
      responsive: true, maintainAspectRatio: false,
      layout: { padding: { right: 64 } },
      _avgValue: avg,
      plugins: {
        legend: {
          display: true, position: 'top', onClick: () => {},
          labels: {
            boxWidth: 12, font: { size: 10 },
            generateLabels: () => years.map(y => ({
              text: y, fillStyle: colorOfYear(y), strokeStyle: colorOfYear(y), lineWidth: 0,
            })),
          },
        },
        tooltip: { callbacks: {
          label: c => 'Produkcja: ' + Number(c.raw).toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' kWh',
          afterLabel: c => '#' + (c.dataIndex + 1) + ' z ' + values.length + ' • ' +
            (Number(c.raw) >= avg ? 'powyżej' : 'poniżej') + ' średniej (' + Math.round(avg) + ' kWh)',
        } },
      },
      scales: {
        x: { beginAtZero: true, ticks: { callback: v => v.toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' kWh', font: { size: 10 } } },
        y: { ticks: { font: { size: 10 }, autoSkip: false } },
      },
    },
    plugins: [_prodRankPlugin],
  });
}

/* -- History table -- */
function renderHistTable(records, monthClosed, invoices) {
  const g11BadgeSpan = (title) => ' <span class="badge badge-g11" title="' + title + '">G11</span>';
  // Build lookup: YYYY-MM → invoice data
  const invByMonth = {};
  (invoices || []).forEach(inv => { invByMonth[inv.month] = inv; });
  const head = '<thead><tr>' +
    '<th>Miesiąc</th>' +
    '<th title="kWh wyprodukowane">Produkcja</th>' +
    '<th title="kWh sprzedane">Sprzedane</th>' +
    '<th title="kWh autokonsumpcja">Autokons.</th>' +
    '<th title="kWh zakupione w szczycie">Zakup szczyt</th>' +
    '<th title="kWh zakupione poza szczytem">Zakup poza</th>' +
    '<th title="PLN/kWh cena zakupu">Cena zakupu</th>' +
    '<th title="PLN/kWh RCEm">RCEm</th>' +
    '<th title="PLN oszczednosci autokonsumpcji">Oszcz. autokons.</th>' +
    '<th title="PLN przychod ze sprzedazy">Przych. sprzedazy</th>' +
    '<th title="PLN arbitraz baterii (ladowanie z sieci w taniej taryfie)">Arbitraż bat.</th>' +
    '<th title="PLN lacznie w miesiacu">Łącznie mies.</th>' +
    '<th title="PLN kumulatywnie">Kumulatywnie</th>' +
    '<th>ROI</th>' +
    '<th title="udzial autokonsumpcji w zuzyciu">Autarkia</th>' +
    '<th title="koszt zakupu minus przychod sprzedazy">Koszt netto sieci</th>' +
    '<th title="status ceny RCEm">Status RCEm</th>' +
  '</tr></thead>';

  const badgeMap = { ok: 'badge-ok', pending: 'badge-pending', missing: 'badge-missing', confirmed: 'badge-ok', updated: 'badge-updated' };
  const closedBadge = monthClosed
    ? '<span class="badge badge-snap" title="Wartosci zapisane w migawce">migawka</span>'
    : '<span class="badge badge-live" title="Dane z czujnikow na zywo">na zywo</span>';

  const yearTotals = {};
  for (const r of records) {
    const y = r.month_label.substring(0, 4);
    if (!yearTotals[y]) yearTotals[y] = {produced: 0, exported: 0, sc: 0, self_sav: 0, feedin: 0, arbitrage: 0, total: 0, purchase: 0, net_grid: 0, consumed: 0, peak_kw: 0, offpeak_kw: 0, peak_kw_count: 0, g11_count: 0};
    const t = yearTotals[y];
    t.produced    += r.produced_kwh          || 0;
    t.exported    += r.exported_kwh          || 0;
    t.sc          += r.self_consumed_kwh     || 0;
    t.self_sav    += r.self_savings          || 0;
    t.feedin      += r.feedin_revenue        || 0;
    t.arbitrage   += r.battery_arbitrage_savings || 0;
    t.total       += r.month_savings         || 0;
    t.purchase    += r.purchase_cost_pln     || 0;
    t.net_grid    += r.net_grid_cost         || 0;
    t.consumed    += r.consumed_kwh          || 0;
    if (r.purchased_kwh_peak != null) { t.peak_kw += r.purchased_kwh_peak; t.peak_kw_count++; }
    if (r.purchased_kwh_offpeak != null) t.offpeak_kw += r.purchased_kwh_offpeak;
    if (r.tariff === 'G11') t.g11_count++;
  }

  function yearSummaryRow(y) {
    const t = yearTotals[y];
    const suff = t.consumed > 0 ? pct(t.sc / t.consumed * 100) : '—';
    const g11YearBadge = t.g11_count > 0
      ? g11BadgeSpan('Zawiera ' + t.g11_count + ' mies. taryfy G11 (całodobowa) — suma szczyt obejmuje pełne zużycie tych miesięcy')
      : '';
    const pkCell  = t.peak_kw_count > 0 ? kwh(t.peak_kw) + g11YearBadge : '—';
    const opkCell = t.peak_kw_count > 0 ? kwh(t.offpeak_kw) : '—';
    return '<tr class="yr">' +
      '<td>' + y + ' – suma</td>' +
      '<td>' + kwh(t.produced) + '</td>' +
      '<td>' + kwh(t.exported) + '</td>' +
      '<td>' + kwh(t.sc)       + '</td>' +
      '<td>' + pkCell  + '</td>' +
      '<td>' + opkCell + '</td>' +
      '<td colspan="2">—</td>' +
      '<td>' + pln(t.self_sav,  0) + '</td>' +
      '<td>' + pln(t.feedin,   0) + '</td>' +
      '<td>' + pln(t.arbitrage,0) + '</td>' +
      '<td>' + pln(t.total,    0) + '</td>' +
      '<td colspan="2">—</td>' +
      '<td>' + suff + '</td>' +
      '<td>' + pln(t.net_grid, 0) + '</td>' +
      '<td>—</td>' +
    '</tr>';
  }

  let html = '';
  let lastYear = null;

  for (const r of records) {
    const year = r.month_label.substring(0, 4);
    if (lastYear !== null && year !== lastYear) {
      html += yearSummaryRow(lastYear);
    }
    lastYear = year;

    const cls = r.is_current ? ' class="cur"' : '';
    const st  = r.rcem_status || 'ok';

    // Invoice badge
    const invRec = invByMonth[r.month_key];
    let invBadge = '';
    if (invRec) {
      const warn = invRec.warnings_count > 0 ? ' ⚠' : '';
      const tip = 'Faktura ' + (invRec.invoice_number || invRec.month) +
        (invRec.amount_due != null ? ' • do zapłaty: ' + invRec.amount_due.toFixed(2) + ' zł' : '') +
        (invRec.deposit_used != null ? ' • depozyt: ' + invRec.deposit_used.toFixed(2) + ' zł' : '') +
        (invRec.warnings_count > 0 ? ' • ostrzeżenia: ' + invRec.warnings_count : '');
      const ico = invRec.reconciled ? '📄✓' : '📄…';
      const col = invRec.reconciled ? '#27ae60' : '#e67e22';
      invBadge = ' <span title="' + tip.replace(/"/g, '&quot;') + '" style="font-size:11px;cursor:help;color:' + col + '">' + ico + warn + '</span>';
    }

    const monthLabel = r.month_label + invBadge;

    const producedCell = r.is_current && r.projected_month_kwh != null
      ? kwh(r.produced_kwh) + '<br><span class="proj-hint">→ ' + kwh(r.projected_month_kwh) + ' proj.</span>'
      : kwh(r.produced_kwh);

    const autarkia = r.self_sufficiency_pct != null ? pct(r.self_sufficiency_pct) : '—';

    const g11Badge = r.tariff === 'G11'
      ? g11BadgeSpan('Taryfa G11 (całodobowa) — pełne zużycie ujęte w kolumnie szczyt; brak strefy poza szczytem')
      : '';
    const pkCell  = r.purchased_kwh_peak    != null ? kwh(r.purchased_kwh_peak) + g11Badge : '—';
    const opkCell = r.purchased_kwh_offpeak != null ? kwh(r.purchased_kwh_offpeak)         : '—';

    html += '<tr' + cls + '>' +
      '<td>' + monthLabel + '</td>' +
      '<td>' + producedCell + '</td>' +
      '<td>' + kwh(r.exported_kwh) + '</td>' +
      '<td>' + kwh(r.self_consumed_kwh) + '</td>' +
      '<td>' + pkCell + '</td>' +
      '<td>' + opkCell + '</td>' +
      '<td>' + price(r.buy_price) + '</td>' +
      '<td>' + feedinPriceCell(r) + '</td>' +
      '<td>' + pln(r.self_savings, 2) + '</td>' +
      '<td>' + pln(r.feedin_revenue, 2) + '</td>' +
      '<td>' + pln(r.battery_arbitrage_savings, 2) + '</td>' +
      '<td>' + pln(r.month_savings, 2) + '</td>' +
      '<td>' + pln(r.cumulative_return) + '</td>' +
      '<td>' + pct(r.roi_pct) + '</td>' +
      '<td>' + autarkia + '</td>' +
      '<td>' + pln(r.net_grid_cost, 2) + '</td>' +
      '<td><span class="badge ' + (badgeMap[st]||'badge-ok') + '">' + st + '</span></td>' +
    '</tr>';
  }
  if (lastYear !== null) html += yearSummaryRow(lastYear);

  document.getElementById('histTbl').innerHTML  = head + '<tbody>' + html + '</tbody>';
  document.getElementById('histFoot').textContent = records.length + ' miesiecy danych';
}

/* -- Predictions table -- */
function renderPredTable(predictions, summary, window) {
  if (!predictions.length) {
    document.getElementById('predTbl').innerHTML =
      '<tbody><tr><td colspan="5" style="padding:24px;text-align:center;color:#718096">Brak danych do prognozy.</td></tr></tbody>';
    document.getElementById('predFoot').textContent = '';
  } else {
    const postPb = predictions[0].post_payback === true;
    const head = '<thead><tr>' +
      '<th>Miesiac</th>' +
      '<th>Proj. oszczednosci</th>' +
      (postPb ? '<th>Zysk netto</th>' : '<th>Kumulatywnie</th><th>Pozostalo</th>') +
      '<th>ROI</th>' +
    '</tr></thead>';

    const rows = predictions.map((p, i) => {
      if (postPb) {
        return '<tr>' +
          '<td>' + p.month_label + '</td>' +
          '<td>' + pln(p.projected_savings, 2) + '</td>' +
          '<td>' + pln(p.net_profit) + '</td>' +
          '<td>' + pct(p.roi_pct) + '</td>' +
        '</tr>';
      }
      const isPb = p.remaining <= 0 && (i === 0 || predictions[i-1].remaining > 0);
      const cls  = isPb ? ' class="pb"' : '';
      return '<tr' + cls + '>' +
        '<td>' + p.month_label + (isPb ? ' 🎉' : '') + '</td>' +
        '<td>' + pln(p.projected_savings, 2) + '</td>' +
        '<td>' + pln(p.cumulative_return) + '</td>' +
        '<td>' + pln(Math.max(0, p.remaining)) + '</td>' +
        '<td>' + pct(p.roi_pct) + '</td>' +
      '</tr>';
    }).join('');

    document.getElementById('predTbl').innerHTML  = head + '<tbody>' + rows + '</tbody>';
    document.getElementById('predFoot').textContent = postPb
      ? 'Prognoza zysku po splacie – srednia z ost. ' + window + ' mies.'
      : 'Prognoza oparta na sredniej z ostatnich ' + window + ' pelnych miesiecy';
  }

  /* Seasonal P10/P50/P90 confidence table */
  const sw = document.getElementById('sensiWrap');
  const hasConf = summary && (summary.payback_date_p10 || summary.payback_date_seasonal || summary.payback_date_p90);
  if (!hasConf) { sw.style.display = 'none'; return; }
  sw.style.display = '';
  const sHead = '<caption>Sezonowe przedziały pewności daty spłaty</caption>' +
    '<thead><tr><th>Scenariusz</th><th>Prognozowana data splaty</th></tr></thead>';
  const confRows = [
    ['Optymistyczny (P10)', summary.payback_date_p10],
    ['Bazowy (P50)',        summary.payback_date_seasonal || summary.payback_date],
    ['Pesymistyczny (P90)', summary.payback_date_p90],
  ].map(([label, d], i) => {
    const cls = i === 1 ? ' class="base"' : '';
    return '<tr' + cls + '><td>' + label + '</td><td>' + (d ? d.slice(0,7) : 'już spłacone') + '</td></tr>';
  }).join('');
  document.getElementById('sensiTbl').innerHTML = sHead + '<tbody>' + confRows + '</tbody>';
}

/* -- Year-over-year table -- */
function renderYearsTable(records, systemKwp) {
  const yearMap = {};
  for (const r of records) {
    const y = r.month_label.substring(0, 4);
    if (!yearMap[y]) yearMap[y] = {
      produced: 0, exported: 0, sc: 0, consumed: 0, purchased: 0,
      self_sav: 0, feedin: 0, arbitrage: 0, purchase_cost: 0, net_grid: 0,
      buy_price_sum: 0, buy_price_n: 0, feedin_price_sum: 0, feedin_price_n: 0, months: 0,
      byMonth: {},
    };
    const t = yearMap[y];
    if (r.month_key) {
      const mNum = r.month_key.slice(5, 7);
      t.byMonth[mNum] = {
        produced: r.produced_kwh || 0,
        savings: (r.self_savings || 0) + (r.feedin_revenue || 0) + (r.battery_arbitrage_savings || 0),
      };
    }
    t.produced      += r.produced_kwh      || 0;
    t.exported      += r.exported_kwh      || 0;
    t.sc            += r.self_consumed_kwh || 0;
    t.consumed      += r.consumed_kwh      || 0;
    t.self_sav      += r.self_savings      || 0;
    t.feedin        += r.feedin_revenue    || 0;
    t.arbitrage     += r.battery_arbitrage_savings || 0;
    t.purchase_cost += r.purchase_cost_pln || 0;
    t.net_grid      += r.net_grid_cost     || 0;
    if (r.buy_price    != null) { t.buy_price_sum    += r.buy_price;    t.buy_price_n++;    }
    if (r.feedin_price != null) { t.feedin_price_sum += r.feedin_price; t.feedin_price_n++; }
    t.months += 1;
  }

  const years = Object.keys(yearMap).sort();
  const hasArbitrage = years.some(y => yearMap[y].arbitrage > 0);

  const fullYears = years.filter(y => yearMap[y].months === 12);
  let bestY = null, worstY = null;
  if (fullYears.length > 1) {
    bestY  = fullYears.reduce((a, b) => yearMap[a].produced > yearMap[b].produced ? a : b);
    worstY = fullYears.reduce((a, b) => yearMap[a].produced < yearMap[b].produced ? a : b);
  }

  // r/r: porównanie z poprzednim rokiem na bazie TYCH SAMYCH miesięcy
  // (rok częściowy porównywany jest z tym samym wycinkiem roku poprzedniego)
  function yoy(y, field) {
    const prev = yearMap[String(Number(y) - 1)];
    if (!prev) return null;
    let cur = 0, base = 0, n = 0;
    for (const m of Object.keys(yearMap[y].byMonth)) {
      if (prev.byMonth[m] == null) continue;
      cur  += yearMap[y].byMonth[m][field];
      base += prev.byMonth[m][field];
      n++;
    }
    return (n > 0 && base > 0) ? (cur / base - 1) * 100 : null;
  }
  function yoyCell(v) {
    if (v == null) return '<td style="color:var(--muted)">—</td>';
    const col = v >= 0 ? '#16a34a' : '#dc2626';
    const arr = v >= 0 ? '▲' : '▼';
    return '<td style="color:' + col + '">' + arr + ' ' + fmt(Math.abs(v), 1, '%') + '</td>';
  }

  const head = '<thead><tr>' +
    '<th>Rok</th>' +
    '<th>Produkcja</th>' +
    '<th>Prod. r/r</th>' +
    '<th>Oszcz. r/r</th>' +
    '<th>Uzysk (kWh/kWp)</th>' +
    '<th>Autokons.</th>' +
    '<th>Sprzedane</th>' +
    '<th>Autarkia %</th>' +
    '<th>Oszcz. autokons.</th>' +
    '<th>Przych. sprzedazy</th>' +
    (hasArbitrage ? '<th>Arbitraz bat.</th>' : '') +
    '<th>Lacznie oszcz.</th>' +
    '<th>Sr. cena zakupu</th>' +
    '<th>Sr. RCEm</th>' +
    '<th>Zakup energii</th>' +
    '<th>Koszt netto sieci</th>' +
    '<th>Mies.</th>' +
  '</tr></thead>';

  const rows = years.map(y => {
    const t = yearMap[y];
    const kwhKwp   = systemKwp > 0 ? Math.round(t.produced / systemKwp) : '—';
    const suff     = t.consumed > 0 ? pct(t.sc / t.consumed * 100) : '—';
    const avgBuy   = t.buy_price_n    > 0 ? (t.buy_price_sum    / t.buy_price_n).toFixed(4)    + ' zl' : '—';
    const avgRcem  = t.feedin_price_n > 0 ? (t.feedin_price_sum / t.feedin_price_n).toFixed(4) + ' zl' : '—';
    const total    = t.self_sav + t.feedin + t.arbitrage;
    let note = '';
    if (y === bestY)  note = ' ⬆️';
    if (y === worstY) note = ' ⬇️';
    const partial = t.months < 12 ? ' <span style="font-size:10px;color:var(--muted)">(' + t.months + ' mies.)</span>' : '';
    return '<tr>' +
      '<td>' + y + note + partial + '</td>' +
      '<td>' + kwh(t.produced) + '</td>' +
      yoyCell(yoy(y, 'produced')) +
      yoyCell(yoy(y, 'savings')) +
      '<td>' + kwhKwp + '</td>' +
      '<td>' + kwh(t.sc) + '</td>' +
      '<td>' + kwh(t.exported) + '</td>' +
      '<td>' + suff + '</td>' +
      '<td>' + pln(t.self_sav, 0) + '</td>' +
      '<td>' + pln(t.feedin, 0) + '</td>' +
      (hasArbitrage ? '<td>' + (t.arbitrage > 0 ? pln(t.arbitrage, 0) : '—') + '</td>' : '') +
      '<td><strong>' + pln(total, 0) + '</strong></td>' +
      '<td style="color:var(--muted)">' + avgBuy + '</td>' +
      '<td style="color:var(--muted)">' + avgRcem + '</td>' +
      '<td>' + pln(t.purchase_cost, 0) + '</td>' +
      '<td>' + pln(t.net_grid, 0) + '</td>' +
      '<td style="color:var(--muted)">' + t.months + '</td>' +
    '</tr>';
  }).join('');

  document.getElementById('yearsTbl').innerHTML = head + '<tbody>' + rows + '</tbody>';
  document.getElementById('yearsFoot').textContent =
    years.length + ' lat danych' + (fullYears.length > 1 ? '; strzalki = najlepsza/najgorsza produkcja (pelne lata)' : '');
}

/* -- RCE vs RCEm tab -- */
function renderRceTab(rc) {
  const s = rc.summary || {};
  const months = rc.months || [];
  document.getElementById('rceWarning').style.display = (s.n_months || 0) < 3 ? '' : 'none';

  const recCls = s.recommendation === 'ROZWAŻ RCE' ? 'c-green'
               : s.recommendation === 'ZOSTAŃ PRZY RCEm' ? 'c-blue' : '';
  const cards = [
    { lbl: 'Rekomendacja', val: s.recommendation || '—', sub: s.recommendation_reason || '', cls: recCls },
    { lbl: 'Śr. różnica / mies.', val: pln(s.avg_monthly_diff_pln, 2), sub: 'RCE godzinowa − RCEm', cls: (s.avg_monthly_diff_pln || 0) > 0 ? 'c-green' : '' },
    { lbl: 'Suma różnic', val: pln(s.total_diff_pln, 2), sub: (s.n_months || 0) + ' rozliczonych mies.' },
    { lbl: 'Miesiące na plus', val: (s.months_rce_better || 0) + '/' + (s.n_months || 0), sub: fmt(s.pct_rce_better, 0, '%') + ' czasu RCE lepsza' },
    s.neg_kwh_total != null ? { lbl: 'Eksport w godz. z ceną ujemną', val: kwh(s.neg_kwh_total),
      sub: fmt(s.neg_share_pct_total, 1, '%') + ' eksportu • reguła "ujemna → 0 zł" chroni ' + pln(s.neg_saved_pln_total, 2),
      cls: (s.neg_share_pct_total || 0) > 5 ? '' : 'c-green' } : null,
  ].filter(Boolean);
  document.getElementById('rceKpiCards').innerHTML = cards.map(c =>
    '<div class="card ' + (c.cls || '') + '">' +
      '<div class="lbl">' + c.lbl + '</div>' +
      '<div class="val">' + c.val + '</div>' +
      (c.sub ? '<div class="sub">' + c.sub + '</div>' : '') +
    '</div>'
  ).join('');

  // Wykres: przychód RCEm vs RCE godzinowa
  const lbls = months.map(m => m.month_label + (m.rcem_estimated ? ' *' : ''));
  if (_rceCmpChart) _rceCmpChart.destroy();
  _rceCmpChart = new Chart(document.getElementById('rceCmpChart'), {
    type: 'bar',
    data: {
      labels: lbls,
      datasets: [
        { label: 'RCEm (faktyczne)', data: months.map(m => m.revenue_rcem_pln), backgroundColor: '#94a3b8' },
        { label: 'RCE godzinowa (symulacja)', data: months.map(m => m.revenue_rce_pln), backgroundColor: '#2563eb' },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'bottom' } },
      scales: { y: { beginAtZero: true, title: { display: true, text: 'zł' } } },
    },
  });

  // Tabela miesięczna
  const head = '<thead><tr>' +
    '<th>Miesiąc</th><th>Eksport (kWh)</th><th>Pokrycie</th>' +
    '<th>Cena RCEm</th><th>Śr. RCE ważona</th>' +
    '<th>Przychód RCEm</th><th>Przychód RCE</th><th>Różnica</th>' +
    '<th>Godz. ujemne</th>' +
  '</tr></thead>';
  const rows = months.map(m => {
    const diffCol = m.diff_pln == null ? 'var(--muted)' : (m.diff_pln >= 0 ? '#16a34a' : '#dc2626');
    const est = m.rcem_estimated ? ' <span style="font-size:10px;color:var(--muted)">(szac.)</span>' : '';
    const covWarn = m.coverage_pct < 95 ? ' style="color:#dc2626"' : '';
    const negCell = m.neg_kwh == null ? '—'
      : (m.neg_kwh > 0
         ? '<span style="color:#dc2626">' + kwh(m.neg_kwh) + ' (' + fmt(m.neg_share_pct, 1, '%') + ')</span>'
         : '<span style="color:var(--muted)">0</span>');
    return '<tr>' +
      '<td>' + m.month_label + est + '</td>' +
      '<td>' + kwh(m.matched_kwh) + '</td>' +
      '<td' + covWarn + '>' + fmt(m.coverage_pct, 0, '%') + '</td>' +
      '<td>' + price(m.rcem_price_pln_kwh) + '</td>' +
      '<td>' + price(m.rce_weighted_price_pln_kwh) + '</td>' +
      '<td>' + pln(m.revenue_rcem_pln, 2) + '</td>' +
      '<td>' + pln(m.revenue_rce_pln, 2) + '</td>' +
      '<td style="color:' + diffCol + ';font-weight:600">' + pln(m.diff_pln, 2) + '</td>' +
      '<td>' + negCell + '</td>' +
    '</tr>';
  }).join('');
  document.getElementById('rceTbl').innerHTML = head + '<tbody>' + rows + '</tbody>';
  document.getElementById('rceFoot').textContent =
    (s.note || '') + ' * = RCEm jeszcze nieopublikowana (szacunek wg ostatniej znanej).';

  _lastRceMonths = months;
  renderRceHeatmap(months);
}

/* ─────────────────────────────────────────────────────────────
   v0.17.0 — nowe wykresy
   ───────────────────────────────────────────────────────────── */

/* Wachlarz spłaty: historia + prognoza P50 z pasmem P10–P90 */
function renderFanChart(records, predictions, gross, netInvestment) {
  const ctx = document.getElementById('fanChart');
  if (!ctx) return;
  const histLbls = records.map(r => r.month_label);
  const histVals = records.map(r => r.cumulative_return);
  const preds = (predictions || []).filter(p => p.cumulative_return != null);
  const allLbls = [...histLbls, ...preds.map(p => p.month_label)];
  const nullsH = preds.map(() => null);
  const nullsP = histLbls.map(() => null);
  const bridge = histVals.length ? histVals[histVals.length - 1] : null;
  const mk = vals => { const a = [...nullsP]; if (a.length) a[a.length-1] = bridge; return [...a, ...vals]; };

  const datasets = [
    { label: 'Zwrot (historia)', data: [...histVals, ...nullsH], borderColor: '#2563eb', backgroundColor: 'rgba(37,99,235,.06)', fill: true, tension: .3, pointRadius: 0 },
    { label: 'P50 (sezonowa)', data: mk(preds.map(p => p.cumulative_return)), borderColor: '#2563eb', borderDash: [6,4], fill: false, tension: .3, pointRadius: 0 },
    { label: 'Szybciej (P10)', data: mk(preds.map(p => p.cumulative_fast)), borderColor: 'rgba(22,163,74,.45)', backgroundColor: 'rgba(37,99,235,.12)', fill: '+1', tension: .3, pointRadius: 0, borderWidth: 1 },
    { label: 'Wolniej (P90)', data: mk(preds.map(p => p.cumulative_slow)), borderColor: 'rgba(220,38,38,.45)', fill: false, tension: .3, pointRadius: 0, borderWidth: 1 },
    { label: 'Inwestycja brutto', data: allLbls.map(() => gross), borderColor: '#dc2626', borderDash: [4,4], fill: false, pointRadius: 0 },
  ];
  if (netInvestment != null)
    datasets.push({ label: 'Inwestycja netto', data: allLbls.map(() => netInvestment), borderColor: '#16a34a', borderDash: [4,4], fill: false, pointRadius: 0 });

  if (_fanChart) _fanChart.destroy();
  _fanChart = new Chart(ctx, {
    type: 'line',
    data: { labels: allLbls, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: c => c.raw == null ? null : c.dataset.label + ': ' + Number(c.raw).toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' zl' } },
      },
      scales: {
        x: { ticks: { maxTicksLimit: 24, font: { size: 10 }, maxRotation: 45 } },
        y: { ticks: { callback: v => (v/1000).toFixed(0) + 'k zl', font: { size: 10 } } },
      },
    },
  });
}

/* Oszczędności nominalne vs realne (deflator CPI z backendu) */
function renderCpiRealChart(records) {
  const ctx = document.getElementById('cpiRealChart');
  if (!ctx) return;
  const withSav = records.filter(r => (r.month_savings || 0) > 0);
  const labels = withSav.map(r => r.month_label);
  let cumN = 0, cumR = 0;
  const nominal = withSav.map(r => { cumN += r.month_savings || 0; return Math.round(cumN); });
  const real    = withSav.map(r => { cumR += (r.month_savings || 0) / (r.cpi_deflator || 1); return Math.round(cumR); });
  if (_cpiRealChart) _cpiRealChart.destroy();
  _cpiRealChart = new Chart(ctx, {
    type: 'line',
    data: { labels, datasets: [
      { label: 'Nominalne (zł)', data: nominal, borderColor: '#2563eb', fill: false, tension: .3, pointRadius: 0 },
      { label: 'Realne (dzisiejsze zł, CPI GUS)', data: real, borderColor: '#b45309', borderDash: [5,3], fill: false, tension: .3, pointRadius: 0 },
    ]},
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } } },
      scales: {
        x: { ticks: { maxTicksLimit: 24, font: { size: 9 }, maxRotation: 45 } },
        y: { ticks: { callback: v => v.toLocaleString('pl-PL') + ' zl', font: { size: 10 } } },
      },
    },
  });
}

/* Degradacja: kroczący uzysk 12-mies. */
function renderDegradChart(deg) {
  const ctx = document.getElementById('degradChart');
  if (!ctx) return;
  const badge = document.getElementById('degradBadge');
  if (!deg || !deg.rolling || deg.rolling.length < 2) {
    if (badge) badge.textContent = '(za mało danych — potrzeba ≥13 mies. produkcji)';
    if (_degradChart) { _degradChart.destroy(); _degradChart = null; }
    return;
  }
  if (badge) {
    const parts = [];
    if (deg.trend_pct_per_year != null) parts.push('trend ' + fmt(deg.trend_pct_per_year, 1, '%/rok'));
    if (deg.yoy_delta_pct != null) parts.push('r/r ' + fmt(deg.yoy_delta_pct, 1, '%'));
    badge.textContent = parts.length ? '(' + parts.join(' • ') + ')' : '';
  }
  if (_degradChart) _degradChart.destroy();
  _degradChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: deg.rolling.map(p => p.ym),
      datasets: [{ label: 'Uzysk 12-mies. (kWh/kWp)', data: deg.rolling.map(p => p.yield_12m),
                   borderColor: '#7c3aed', backgroundColor: 'rgba(124,58,237,.07)', fill: true, tension: .3, pointRadius: 2 }],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { maxTicksLimit: 18, font: { size: 9 }, maxRotation: 45 } },
        y: { ticks: { callback: v => v + ' kWh/kWp', font: { size: 10 } } },
      },
    },
  });
}

/* Waterfall miesięczny: autokonsumpcja → sprzedaż → arbitraż → opłaty stałe → netto */
function _populateWaterfallSelect(records) {
  const sel = document.getElementById('waterfallMonth');
  if (!sel) return;
  const candidates = records.filter(r => (r.month_savings || 0) > 0).map(r => r.month_key);
  const prev = sel.value;
  sel.innerHTML = candidates.slice().reverse().map(k => '<option value="' + k + '">' + k + '</option>').join('');
  if (prev && candidates.includes(prev)) sel.value = prev;
}

function redrawWaterfall() {
  const sel = document.getElementById('waterfallMonth');
  const ctx = document.getElementById('waterfallChart');
  if (!sel || !ctx || !sel.value) return;
  const r = _lastRecords.find(x => x.month_key === sel.value);
  if (!r) return;
  const inv = _lastInvoices.find(i => i.month === sel.value);
  const auto = r.self_savings || 0, sell = r.feedin_revenue || 0, arb = r.battery_arbitrage_savings || 0;
  const fixed = inv && inv.fixed_total_net != null ? -(inv.fixed_total_net * 1.23) : null; // netto → brutto
  const steps = [
    { lbl: 'Autokonsumpcja', v: auto, col: 'rgba(37,99,235,.8)' },
    { lbl: 'Sprzedaż', v: sell, col: 'rgba(22,163,74,.8)' },
  ];
  if (arb > 0) steps.push({ lbl: 'Arbitraż bat.', v: arb, col: 'rgba(234,179,8,.85)' });
  if (fixed != null) steps.push({ lbl: 'Opłaty stałe', v: fixed, col: 'rgba(220,38,38,.75)' });
  let run = 0;
  const bars = steps.map(s => { const seg = [run, run + s.v]; run += s.v; return seg; });
  steps.push({ lbl: 'Netto', v: run, col: 'rgba(100,116,139,.85)' });
  bars.push([0, run]);
  if (_waterfallChart) _waterfallChart.destroy();
  _waterfallChart = new Chart(ctx, {
    type: 'bar',
    data: { labels: steps.map(s => s.lbl),
            datasets: [{ data: bars, backgroundColor: steps.map(s => s.col), borderWidth: 0, borderSkipped: false }] },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => {
          const s = steps[c.dataIndex];
          return s.lbl + ': ' + Number(s.v).toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' zl';
        } } },
      },
      scales: { y: { ticks: { callback: v => v + ' zl', font: { size: 10 } } } },
    },
  });
}

/* Sankey przepływu energii (rok) */
function _populateSankeySelect(records) {
  const sel = document.getElementById('sankeyYear');
  if (!sel) return;
  const years = [...new Set(records.map(r => r.month_key.slice(0,4)))].sort().reverse();
  const prev = sel.value;
  sel.innerHTML = ['wszystko', ...years].map(y => '<option value="' + y + '">' + y + '</option>').join('');
  if (prev && (years.includes(prev) || prev === 'wszystko')) sel.value = prev;
}

function redrawSankey() {
  const ctx = document.getElementById('sankeyChart');
  const sel = document.getElementById('sankeyYear');
  if (!ctx || !sel) return;
  if (typeof Chart === 'undefined' || !Chart.registry.controllers.get('sankey')) {
    ctx.parentElement.style.display = 'none';   // plugin nie załadowany (offline)
    return;
  }
  const recs = _lastRecords.filter(r => sel.value === 'wszystko' || r.month_key.startsWith(sel.value));
  const sum = f => Math.round(recs.reduce((a, r) => a + (f(r) || 0), 0));
  const self = sum(r => r.self_consumed_kwh), exp = sum(r => r.exported_kwh), buy = sum(r => r.purchased_kwh);
  if (self + exp + buy === 0) return;
  const flows = [
    { from: 'Produkcja PV', to: 'Autokonsumpcja', flow: self },
    { from: 'Produkcja PV', to: 'Eksport do sieci', flow: exp },
    { from: 'Autokonsumpcja', to: 'Zużycie domu', flow: self },
    { from: 'Zakup z sieci', to: 'Zużycie domu', flow: buy },
  ].filter(f => f.flow > 0);
  if (_sankeyChart) _sankeyChart.destroy();
  _sankeyChart = new Chart(ctx, {
    type: 'sankey',
    data: { datasets: [{
      data: flows,
      colorFrom: c => ({'Produkcja PV':'#f59e0b','Autokonsumpcja':'#2563eb','Zakup z sieci':'#94a3b8'}[c.dataset.data[c.dataIndex].from] || '#64748b'),
      colorTo:   c => ({'Autokonsumpcja':'#2563eb','Eksport do sieci':'#16a34a','Zużycie domu':'#0f766e'}[c.dataset.data[c.dataIndex].to] || '#64748b'),
      colorMode: 'gradient',
      labels: {},
      size: 'max',
    }]},
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { tooltip: { callbacks: { label: c => {
        const d = c.dataset.data[c.dataIndex];
        return d.from + ' → ' + d.to + ': ' + d.flow.toLocaleString('pl-PL') + ' kWh';
      } } } },
    },
  });
}

/* Heatmapa eksport × cena RCE (CSS grid, bez pluginów) */
function redrawRceHeatmap() { renderRceHeatmap(_lastRceMonths); }

function renderRceHeatmap(months) {
  const wrap = document.getElementById('rceHeatmap');
  if (!wrap) return;
  const rows = (months || []).filter(m => m.hours_profile);
  if (!rows.length) { wrap.innerHTML = '<p style="color:var(--muted);font-size:12px">Brak danych godzinowych (pojawią się po następnym odświeżeniu cache).</p>'; return; }
  const metricEl = document.querySelector('input[name="hmMetric"]:checked');
  const metric = metricEl ? metricEl.value : 'price';

  let maxKwh = 0, maxP = 0;
  rows.forEach(m => m.hours_profile.forEach(h => {
    if (h[0] > maxKwh) maxKwh = h[0];
    if (h[1] != null && h[1] > maxP) maxP = h[1];
  }));
  const cell = 'display:inline-block;width:26px;height:20px;border-radius:2px;margin:1px;font-size:8px;vertical-align:middle';
  let html = '<div style="white-space:nowrap;font-size:10px"><span style="display:inline-block;width:62px"></span>';
  for (let h = 0; h < 24; h++) html += '<span style="' + cell + ';text-align:center;color:var(--muted)">' + h + '</span>';
  html += '</div>';
  rows.forEach(m => {
    html += '<div style="white-space:nowrap"><span style="display:inline-block;width:62px;font-size:10px;font-weight:600">' + m.month_label + '</span>';
    m.hours_profile.forEach((hp, h) => {
      const [kwhV, priceV, negV] = hp;
      let bg = 'var(--bg)', title = m.month_label + ' ' + h + ':00 — ';
      if (kwhV > 0) {
        if (metric === 'price' && priceV != null) {
          if (priceV < 0) {
            bg = 'rgba(220,38,38,.85)';                       // cena ujemna
          } else {
            const t = maxP > 0 ? Math.min(priceV / maxP, 1) : 0;
            bg = 'rgba(37,99,235,' + (0.12 + 0.78 * t).toFixed(2) + ')';
          }
        } else if (metric === 'kwh') {
          const t = maxKwh > 0 ? Math.min(kwhV / maxKwh, 1) : 0;
          bg = 'rgba(234,140,8,' + (0.10 + 0.85 * t).toFixed(2) + ')';
        }
        title += kwhV.toFixed(1) + ' kWh, śr. RCE ' + (priceV != null ? priceV.toFixed(0) + ' zł/MWh' : '—')
               + (negV > 0 ? ', w tym ' + negV.toFixed(1) + ' kWh po cenie ujemnej (liczone 0 zł)' : '');
      } else { title += 'brak eksportu'; }
      html += '<span style="' + cell + ';background:' + bg + '" title="' + title.replace(/"/g,'&quot;') + '"></span>';
    });
    html += '</div>';
  });
  wrap.innerHTML = html;
}

/* Depozyt: KPI przedawnienia + wykres saldo/prognoza */
function renderDepositSection(dep, invoices) {
  const kpiWrap = document.getElementById('depKpiCards');
  const note = document.getElementById('depositNote');
  if (!kpiWrap) return;
  if (!dep) { kpiWrap.innerHTML = ''; renderDepositChart(invoices); return; }

  const kpiStyle = 'min-width:150px;padding:12px 16px;background:var(--card);border-radius:var(--radius);box-shadow:var(--shadow);flex:1 1 150px';
  const lbl = t => '<div style="font-size:11px;color:var(--muted);margin-bottom:4px">' + t + '</div>';
  const val = (v, col) => '<div style="font-size:18px;font-weight:700' + (col ? ';color:' + col : '') + '">' + v + '</div>';
  const bal = dep.balance_estimate != null ? dep.balance_estimate : dep.balance_model;
  const expCol = dep.expiring_3m > 0 ? '#e67e22' : '#27ae60';
  const balSub = dep.balance_estimate != null
    ? 'po fakturze ' + (dep.invoice_latest_month || '—') + ': ' + pln(dep.anchor_balance || 0, 2) +
      ' + niezaksięgowane: ' + pln(dep.unposted_accrual || 0, 2)
    : 'model FIFO (brak faktur)';
  kpiWrap.innerHTML =
    '<div style="' + kpiStyle + '">' + lbl('Stan bieżący (estymat)') + val(pln(bal, 2)) +
      '<div style="font-size:10px;color:var(--muted)">' + balSub + '</div></div>' +
    '<div style="' + kpiStyle + '">' + lbl('Traci ważność za 1 mies.') + val(pln(dep.expiring_1m, 2), dep.expiring_1m > 0 ? '#e67e22' : null) + '</div>' +
    '<div style="' + kpiStyle + '">' + lbl('Traci ważność za 3 mies.') + val(pln(dep.expiring_3m, 2), expCol) + '</div>' +
    '<div style="' + kpiStyle + '">' + lbl('Prognoza 12 mies.: zwrot / umorzenie') +
      val(pln(dep.projected_refund_12m, 0) + ' / ' + pln(dep.projected_forfeit_12m, 0)) +
      '<div style="font-size:10px;color:var(--muted)">limit zwrotu ' + fmt(dep.refund_cap_pct, 0, '%') + ' zasilenia mies. (12 mies. od przypisania)</div></div>';

  if (note) note.textContent =
    'Stan bieżący = saldo po ostatniej fakturze (previous − rozliczone) + zasilenia z falownika za miesiące, których Tauron jeszcze nie zaksięgował '
    + '(lag ~' + (dep.posting_lag_months || 2) + ' mies.). '
    + 'Po 12 mies. od przypisania niewykorzystane środki przepadają poza zwrotem do ' + fmt(dep.refund_cap_pct, 0, '%') + ' wartości energii z danego miesiąca (art. 4 ust. 11 ustawy o OZE).';

  renderReconSection(dep);

  // Wykres: saldo modelowe + fakturowe + prognoza + przedawnienia
  const ctxEl = document.getElementById('depositChart');
  if (!ctxEl) return;
  const hist = dep.months || [], fc = dep.forecast || [];
  const labels = [...hist.map(m => m.ym), ...fc.map(f => f.ym)];
  const nullsF = fc.map(() => null);
  const histBal = [...hist.map(m => m.balance), ...nullsF];
  const invBal  = [...hist.map(m => m.invoice_balance), ...nullsF];
  const fcBal   = [...hist.map(() => null), ...fc.map(f => f.balance)];
  if (hist.length && fcBal.length > hist.length) fcBal[hist.length - 1] = hist[hist.length - 1].balance;
  const expiry  = [...hist.map(m => (m.expired_refund + m.expired_forfeit) || null),
                   ...fc.map(f => (f.expired_refund + f.expired_forfeit) || null)];
  if (_depositChart) _depositChart.destroy();
  _depositChart = new Chart(ctxEl, {
    data: { labels, datasets: [
      { type: 'line', label: 'Saldo (model FIFO)', data: histBal, borderColor: '#3182ce', backgroundColor: 'rgba(49,130,206,.08)', fill: true, tension: .3, pointRadius: 0 },
      { type: 'line', label: 'Saldo wg faktur', data: invBal, borderColor: '#0f766e', borderDash: [4,3], fill: false, tension: .3, pointRadius: 3, spanGaps: true },
      { type: 'line', label: 'Prognoza', data: fcBal, borderColor: '#3182ce', borderDash: [6,4], fill: false, tension: .3, pointRadius: 0 },
      { type: 'bar', label: 'Przedawnienie (zwrot+umorzenie)', data: expiry, backgroundColor: 'rgba(220,38,38,.6)' },
    ]},
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } } },
      scales: {
        x: { ticks: { maxTicksLimit: 20, font: { size: 9 }, maxRotation: 45 } },
        y: { ticks: { callback: v => v + ' zł', font: { size: 10 } } },
      },
    },
  });
}

/* Depozyt: rekonsyliacja zasileń — faktury (Tauron) vs falownik (model) */
function renderReconSection(dep) {
  const kpiWrap = document.getElementById('reconKpiCards');
  const tblWrap = document.getElementById('reconTableWrap');
  const note = document.getElementById('reconNote');
  if (!kpiWrap || !tblWrap) return;
  const rec = dep && dep.reconciliation;
  if (!rec || !rec.rows || !rec.rows.length) {
    kpiWrap.innerHTML = ''; tblWrap.innerHTML = '';
    if (note) note.textContent = 'Brak danych do rekonsyliacji — wgraj faktury z saldem depozytu.';
    return;
  }

  const kpiStyle = 'min-width:150px;padding:12px 16px;background:var(--card);border-radius:var(--radius);box-shadow:var(--shadow);flex:1 1 150px';
  const lbl = t => '<div style="font-size:11px;color:var(--muted);margin-bottom:4px">' + t + '</div>';
  const val = (v, col) => '<div style="font-size:18px;font-weight:700' + (col ? ';color:' + col : '') + '">' + v + '</div>';
  const tot = rec.totals || {};
  const diffCol = (tot.diff || 0) > 0 ? '#e67e22' : '#27ae60';
  kpiWrap.innerHTML =
    '<div style="' + kpiStyle + '">' + lbl('Σ z falownika (model)') + val(pln(tot.model, 2)) + '</div>' +
    '<div style="' + kpiStyle + '">' + lbl('Σ z faktur (Tauron)') + val(pln(tot.tauron, 2)) + '</div>' +
    '<div style="' + kpiStyle + '">' + lbl('Różnica skumulowana') +
      val(pln(tot.diff, 2) + (tot.diff_pct != null ? ' (' + (tot.diff_pct > 0 ? '+' : '') + tot.diff_pct.toFixed(1) + '%)' : ''), diffCol) + '</div>' +
    '<div style="' + kpiStyle + '">' + lbl('Lag księgowania Taurona') + val('~' + (dep.posting_lag_months || 2) + ' mies.') + '</div>';

  const fmtD = (v, signed) => v == null ? '—'
    : (signed && v > 0 ? '+' : '') + v.toLocaleString('pl-PL', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' zł';
  let html = '<table><thead><tr>' +
    '<th>Mies. eksportu</th>' +
    '<th title="Zasilenie wyliczone z falownika: eksport × RCEm (×1,23 od 2025-02)">Falownik (model)</th>' +
    '<th title="Zasilenie implikowane z łańcucha faktur, przesunięte o lag księgowania">Faktury (Tauron)</th>' +
    '<th>Różnica</th><th>Różnica %</th>' +
    '</tr></thead><tbody>';
  [...rec.rows].reverse().forEach(r => {
    const pctStyle = (r.diff_pct != null && Math.abs(r.diff_pct) > 10) ? 'color:#e67e22;font-weight:700' : '';
    html += '<tr><td>' + r.ym + '</td>' +
      '<td>' + fmtD(r.model_accrued) + '</td>' +
      '<td>' + (r.tauron_implied == null ? '<span style="color:var(--muted)">jeszcze niezaksięgowane</span>' : fmtD(r.tauron_implied)) + '</td>' +
      '<td>' + fmtD(r.diff, true) + '</td>' +
      '<td style="' + pctStyle + '">' + (r.diff_pct == null ? '—' : (r.diff_pct > 0 ? '+' : '') + r.diff_pct.toFixed(1) + '%') + '</td></tr>';
  });
  html += '</tbody><tfoot><tr style="font-weight:700;border-top:2px solid var(--border)">' +
    '<td>Σ</td><td>' + fmtD(tot.model) + '</td><td>' + fmtD(tot.tauron) + '</td>' +
    '<td>' + fmtD(tot.diff, true) + '</td>' +
    '<td>' + (tot.diff_pct != null ? (tot.diff_pct > 0 ? '+' : '') + tot.diff_pct.toFixed(1) + '%' : '—') + '</td></tr></tfoot></table>';
  tblWrap.innerHTML = html;

  if (note) note.textContent =
    'Wartość z faktur to zasilenie zrekonstruowane z łańcucha sald (previous − saldo po poprzedniej fakturze), dopasowane do miesiąca eksportu '
    + 'przez przesunięcie o wykryty lag księgowania. Wartości surowe — bez kalibracji; różnica % pokazuje, o ile model z falownika odbiega od rozliczeń Taurona.';
}

/* -- RCEm manual override -- */
async function submitOverride() {
  const month = document.getElementById('ovMonth').value;
  const price = parseFloat(document.getElementById('ovPrice').value);
  const msg   = document.getElementById('ovMsg');
  msg.className = '';
  msg.textContent = 'Zapisywanie…';
  if (!month || isNaN(price) || price <= 0 || price > 2) {
    msg.className = 'err'; msg.textContent = 'Podaj poprawny miesiac i cene (0–2 zl/kWh)'; return;
  }
  try {
    const r = await fetch('api/rcem/override', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({month, price}),
    });
    const d = await r.json();
    if (d.ok) {
      msg.className = 'ok';
      msg.textContent = 'Zapisano ' + month + ' = ' + price.toFixed(4) + ' zl/kWh';
      setTimeout(loadData, 2000);
    } else {
      msg.className = 'err'; msg.textContent = 'Blad: ' + (d.error || 'nieznany');
    }
  } catch(e) {
    msg.className = 'err'; msg.textContent = 'Blad polaczenia';
  }
}

/* -- Main data load -- */
async function loadData() {
  try {
    const resp = await fetch('api/data');
    if (resp.status === 202) {
      document.getElementById('updated').textContent = 'Oczekiwanie na pierwsze dane…';
      return;
    }
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const d = await resp.json();
    if (d.status !== 'ok') return;

    document.getElementById('loading').style.display = 'none';
    document.getElementById('content').style.display = '';
    document.getElementById('updated').textContent = 'Aktualizacja: ' + (d.updated_at || '—');

    const badge = document.getElementById('rcemBadge');
    if (badge) {
      const st = d.summary.rcem_scrape_status || 'pending';
      const labels = { ok: 'RCEm OK', pending: 'oczekuje', retrying: 'sprawdzam', error: 'błąd' };
      badge.textContent = labels[st] || st;
      badge.className = 'rcem-badge ' + st;
    }

    const systemKwp = d.summary.specific_yield > 0
      ? d.summary.total_produced_kwh / d.summary.specific_yield
      : 6.72;

    renderCards(d.summary);
    renderLineChart(d.records, d.predictions, d.summary.gross_investment, d.summary.net_investment);
    renderBarChart(d.records);
    renderRcemChart(d.records);
    renderAutarkiaChart(d.records);
    renderProdChart(d.records);
    renderArbitrageChart(d.records);
    renderNetCostChart(d.records);
    renderPriceSpreadChart(d.records);
    renderYieldChart(d.records, systemKwp);
    renderEnergyBalChart(d.records);
    renderYearCompChart(d.records);
    renderProdRankChart(d.records);
    renderHistTable([...d.records].reverse(), d.summary.month_closed, d.invoices || []);
    renderPredTable(d.predictions, d.summary, d.summary.avg_window);
    renderYearsTable(d.records, systemKwp);
    renderInvoicesTab(d.invoices || [], d.tariff_drift, d.records, d.layouts_summary, d.cost_breakdown);
    if (d.tariff_comparison) renderTariffTab(d.tariff_comparison);
    if (d.rce_comparison) renderRceTab(d.rce_comparison);
    // v0.17.0
    _lastRecords = d.records || [];
    _lastInvoices = d.invoices || [];
    renderFanChart(d.records, d.predictions, d.summary.gross_investment, d.summary.net_investment);
    renderCpiRealChart(d.records);
    renderDegradChart(d.degradation);
    _populateWaterfallSelect(d.records);
    redrawWaterfall();
    _populateSankeySelect(d.records);
    try { redrawSankey(); } catch (e) { console.warn('sankey:', e); }
    renderDepositSection(d.deposit, d.invoices || []);
    if (d.version) document.getElementById('appVer').textContent = 'v' + d.version;
  } catch (e) {
    document.getElementById('updated').textContent = 'Blad polaczenia';
    console.error(e);
  }
}

/* -- Invoice upload -- */
async function uploadInvoices() {
  const input = document.getElementById('invoiceFiles');
  const msg   = document.getElementById('invoiceMsg');
  msg.className = ''; msg.textContent = 'Wgrywanie...';
  if (!input.files || input.files.length === 0) {
    msg.className = 'err'; msg.textContent = 'Wybierz przynajmniej jeden plik PDF'; return;
  }
  const fd = new FormData();
  for (const f of input.files) fd.append('files', f);
  try {
    const r = await fetch('api/invoice/upload', {method: 'POST', body: fd});
    const d = await r.json();
    if (d.ok) {
      const ok      = (d.results || []).filter(x => x.ok);
      const stubs   = (d.results || []).filter(x => !x.ok && x.needs_training);
      const hardErr = (d.results || []).filter(x => !x.ok && !x.needs_training);
      let parts = [];
      if (ok.length)      parts.push('Wgrano: ' + ok.map(x => x.month).join(', '));
      if (stubs.length)   parts.push('⚠ Dodano do treningu: ' + stubs.map(x => x.filename).join(', ') + ' — kliknij Trenuj w tabeli poniżej');
      if (hardErr.length) parts.push('Błąd: ' + hardErr.map(x => x.filename + ': ' + x.error).join('; '));
      msg.className = hardErr.length ? 'err' : (stubs.length ? '' : 'ok');
      msg.style.color = stubs.length && !hardErr.length ? '#b7791f' : '';
      msg.textContent = parts.join('; ') || 'Gotowe';
      setTimeout(loadData, 1500);
    } else {
      msg.className = 'err'; msg.textContent = 'Blad: ' + (d.error || 'nieznany');
    }
  } catch(e) {
    msg.className = 'err'; msg.textContent = 'Blad polaczenia';
  }
}

/* ─────────────────────────────────────────────────────────────
   Faktury tab renderer
   ───────────────────────────────────────────────────────────── */
function renderInvoicesTab(invoices, tariffDrift, records, layoutsSummary, costBreakdown) {
  _renderInvKpiCards(invoices, tariffDrift, records);
  _renderDriftBanner(tariffDrift);
  _renderCoverageGrid(invoices, records);
  _renderCostBreakdown(costBreakdown);
  // wykres depozytu rysuje renderDepositSection (fallback: renderDepositChart)
  _renderInvoiceTable(invoices);
  _renderLayoutsPanel(layoutsSummary);
}

/* Cost breakdown: totals table + per-month stacked chart, with netto/brutto toggle */
let _costBreakdownData = null, _costBreakdownMode = 'brutto';

function _setCostBreakdownMode(mode) {
  _costBreakdownMode = mode;
  _renderCostBreakdown(_costBreakdownData);
}

function _renderCostBreakdown(breakdown) {
  _costBreakdownData = breakdown;
  const toggleWrap = document.getElementById('costBreakdownToggle');
  const tableWrap = document.getElementById('costBreakdownTableWrap');
  const note = document.getElementById('costBreakdownNote');
  if (!tableWrap) return;

  if (toggleWrap) {
    const btnClass = mode => 'cost-toggle-btn' + (_costBreakdownMode === mode ? ' active' : '');
    toggleWrap.innerHTML =
      '<button class="' + btnClass('netto') + '" onclick="_setCostBreakdownMode(\'netto\')">Netto</button>' +
      '<button class="' + btnClass('brutto') + '" onclick="_setCostBreakdownMode(\'brutto\')">Brutto</button>';
  }

  if (!breakdown || !breakdown.components || breakdown.components.length === 0) {
    tableWrap.innerHTML = '<p style="color:var(--muted);font-size:13px">Brak danych — wgraj faktury, aby zobaczyć rozbicie kosztów.</p>';
    if (note) note.textContent = '';
    if (_costBreakdownChart) { _costBreakdownChart.destroy(); _costBreakdownChart = null; }
    return;
  }

  const mult = _costBreakdownMode === 'brutto' ? 1.23 : 1.0;
  const grand = breakdown.grand_total_net * mult;

  let rows = '';
  breakdown.components.forEach(c => {
    rows += '<tr><td>' + c.label + '</td>' +
      '<td style="text-align:right">' + pln(c.total_net * mult, 2) + '</td>' +
      '<td style="text-align:right;color:var(--muted)">' + pct(c.share_pct) + '</td></tr>';
  });
  if (_costBreakdownMode === 'netto') {
    // Informational row so netto + VAT reconciles to the brutto total below
    rows += '<tr style="color:var(--muted)"><td>VAT (23%)</td>' +
      '<td style="text-align:right">' + pln(breakdown.grand_total_net * 0.23, 2) + '</td><td></td></tr>';
  }

  tableWrap.innerHTML =
    '<table style="width:100%;border-collapse:collapse;font-size:12px">' +
    '<thead><tr style="border-bottom:2px solid var(--border);font-size:11px">' +
      '<th style="text-align:left">Składnik</th><th style="text-align:right">Kwota</th><th style="text-align:right">% udziału</th>' +
    '</tr></thead><tbody>' + rows + '</tbody>' +
    '<tfoot><tr style="border-top:2px solid var(--border);font-weight:700">' +
      '<td>Razem</td><td style="text-align:right">' + pln(grand, 2) + '</td><td></td>' +
    '</tr></tfoot></table>';

  if (note) {
    note.textContent = breakdown.any_reconstructed
      ? 'Część wartości oszacowana ze stawek × kWh — przelicz z PDF (przycisk „↻ PDF” w tabeli faktur) dla kwot z faktury.'
      : '';
  }

  const ctx = document.getElementById('costBreakdownChart');
  if (!ctx) return;
  if (_costBreakdownChart) _costBreakdownChart.destroy();
  const palette = ['#3182ce','#38a169','#d69e2e','#805ad5','#dd6b20','#319795','#718096','#e53e3e','#0bc5ea','#d53f8c','#a0aec0'];
  const datasets = breakdown.components.map((c, i) => ({
    label: c.label,
    data: breakdown.per_month.series[c.key].map(v => v != null ? v * mult : null),
    backgroundColor: palette[i % palette.length],
  }));
  _costBreakdownChart = new Chart(ctx, {
    type: 'bar',
    data: { labels: breakdown.per_month.labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'top', labels: { boxWidth: 10, font: { size: 10 } } } },
      scales: {
        x: { stacked: true, ticks: { font: { size: 9 }, maxRotation: 45 } },
        y: { stacked: true, ticks: { callback: v => v + ' zł', font: { size: 10 } } },
      },
    },
  });
}

/* KPI summary cards */
function _renderInvKpiCards(invoices, tariffDrift, records) {
  const wrap = document.getElementById('invKpiCards');
  if (!wrap) return;
  const sorted = [...invoices].filter(i => i.month).sort((a,b) => b.month.localeCompare(a.month));
  const latest = sorted[0] || null;

  const totalHistoric = (records || []).filter(r => !r.is_current).length;
  const covered = invoices.filter(i => i.reconciled).length;
  const coveragePct = totalHistoric > 0 ? Math.round(covered / totalHistoric * 100) : 0;
  const hasDrift = tariffDrift && (tariffDrift.peak || tariffDrift.offpeak || tariffDrift.fixed_net);

  const kpiStyle = 'min-width:160px;padding:12px 16px;background:var(--card);border-radius:var(--radius);box-shadow:var(--shadow);flex:1 1 160px';
  const lbl = (t) => '<div style="font-size:11px;color:var(--muted);margin-bottom:4px">' + t + '</div>';
  const val = (v, sml) => '<div style="font-size:18px;font-weight:700">' + v +
    (sml ? '<span style="font-size:12px;font-weight:400;color:var(--muted);margin-left:4px">' + sml + '</span>' : '') +
    '</div>';

  let html = '';
  // Deposit balance
  const depBal = latest ? (latest.deposit_previous != null ? latest.deposit_previous.toFixed(2) + ' zł' : '—') : '—';
  html += '<div style="' + kpiStyle + '">' + lbl('Saldo depozytu') + val(depBal) + '</div>';

  // Last invoice
  const lastInv = latest ? (latest.amount_due != null ? latest.amount_due.toFixed(2) + ' zł' : '—') : '—';
  const lastMon = latest ? latest.month : '—';
  html += '<div style="' + kpiStyle + '">' + lbl('Ostatnia faktura (' + lastMon + ')') + val(lastInv) + '</div>';

  // Coverage
  html += '<div style="' + kpiStyle + '">' + lbl('Pokrycie faktury') +
    val(covered + ' / ' + totalHistoric, coveragePct + '%') + '</div>';

  // Drift status
  const driftTxt = hasDrift ? '⚠ zmiana stawek' : '✓ stawki OK';
  const driftCol = hasDrift ? '#e67e22' : '#27ae60';
  html += '<div style="' + kpiStyle + ';">' + lbl('Status taryfy') +
    '<div style="font-size:15px;font-weight:700;color:' + driftCol + '">' + driftTxt + '</div></div>';

  wrap.innerHTML = html;
}

/* Drift banner */
function _renderDriftBanner(tariffDrift) {
  const banner = document.getElementById('tariffDriftBanner');
  if (!banner) return;
  if (tariffDrift && (tariffDrift.peak || tariffDrift.offpeak || tariffDrift.fixed_net)) {
    let msgs = [];
    if (tariffDrift.peak)      msgs.push('szczyt: skonfig. ' + tariffDrift.peak.configured + ' → faktura ' + tariffDrift.peak.invoice + ' zł/kWh');
    if (tariffDrift.offpeak)   msgs.push('poza szczytem: skonfig. ' + tariffDrift.offpeak.configured + ' → faktura ' + tariffDrift.offpeak.invoice + ' zł/kWh');
    if (tariffDrift.fixed_net) msgs.push('opłaty stałe (net): oczekiwano ' + tariffDrift.fixed_net.expected + ' → faktura ' + tariffDrift.fixed_net.invoice + ' zł/mc');
    // Informational, not actionable: symulacja i Analiza taryf już automatycznie
    // korzystają ze stawek z najnowszej faktury — config.yaml jest tu tylko
    // wartością zapasową (fallback), gdy żadna faktura nie jest jeszcze wgrana.
    banner.innerHTML = 'ℹ Stawki z konfiguracji różnią się od najnowszej faktury: ' + msgs.join('; ') +
      '. Symulacja i Analiza taryf używają już automatycznie stawek z faktury — ' +
      'config.yaml służy tylko jako wartość zapasowa, aktualizacja opcjonalna.';
    banner.style.background = '#e7f3ff';
    banner.style.borderLeft = '4px solid #3182ce';
    banner.style.display = '';
  } else {
    banner.style.display = 'none';
  }
}

/* Coverage grid */
function _renderCoverageGrid(invoices, records) {
  const wrap = document.getElementById('invCoverageGrid');
  if (!wrap) return;

  const PL_MONTHS_SHORT = ['Sty','Lut','Mar','Kwi','Maj','Cze','Lip','Sie','Wrz','Paź','Lis','Gru'];

  // Build set of historic months from records (exclude current/live)
  const historicMonths = new Set((records || []).filter(r => !r.is_current).map(r => r.month_key));
  const invMap = {};
  (invoices || []).forEach(i => { invMap[i.month] = i; });

  // Determine years range
  const allYears = new Set([...(records || []).map(r => r.month_key ? r.month_key.substring(0,4) : null).filter(Boolean)]);
  if (!allYears.size) { wrap.innerHTML = '<p style="color:var(--muted);font-size:13px">Brak danych.</p>'; return; }
  const years = [...allYears].sort();

  const chipBase = 'display:inline-flex;align-items:center;justify-content:center;width:38px;height:28px;border-radius:4px;font-size:10px;font-weight:600;cursor:default;margin:1px;position:relative;';
  let html = '<table style="border-collapse:separate;border-spacing:0;font-size:11px"><tbody>';

  for (const y of years) {
    html += '<tr><td style="padding-right:8px;font-weight:600;font-size:12px;white-space:nowrap">' + y + '</td>';
    for (let m = 1; m <= 12; m++) {
      const mk = y + '-' + String(m).padStart(2,'0');
      const inHistory = historicMonths.has(mk);
      const inv = invMap[mk];
      let bg, color, title;
      if (!inHistory) {
        bg = 'var(--bg)'; color = '#bbb'; title = mk + ': brak danych';
      } else if (!inv) {
        bg = '#e2e8f0'; color = '#555'; title = mk + ': brak faktury';
      } else if (inv.reconciled) {
        bg = '#c6f6d5'; color = '#22543d'; title = mk + ': faktura uzgodniona';
        if (inv.amount_due != null) title += ' • ' + inv.amount_due.toFixed(2) + ' zł';
      } else {
        bg = '#fef3c7'; color = '#92400e'; title = mk + ': faktura wgrana, oczekuje na zamknięcie miesiąca';
      }
      const warn = inv && inv.warnings_count > 0 ? '<span style="position:absolute;top:1px;right:2px;font-size:8px;color:#c0392b">⚠</span>' : '';
      html += '<td><div style="' + chipBase + 'background:' + bg + ';color:' + color + '" title="' + title.replace(/"/g,'&quot;') + '">' +
        PL_MONTHS_SHORT[m-1] + warn + '</div></td>';
    }
    html += '</tr>';
  }
  html += '</tbody></table>';
  wrap.innerHTML = html;
}

/* Deposit trend chart */
function renderDepositChart(invoices) {
  const ctx = document.getElementById('depositChart');
  if (!ctx) return;
  const sorted = [...(invoices||[])].filter(i => i.month).sort((a,b) => a.month.localeCompare(b.month));
  const labels = sorted.map(i => i.month);
  const data   = sorted.map(i => i.deposit_previous != null ? i.deposit_previous : null);
  if (_depositChart) _depositChart.destroy();
  _depositChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: 'Saldo depozytu (zł)',
        data,
        borderColor: '#3182ce',
        backgroundColor: 'rgba(49,130,206,0.08)',
        tension: 0.3,
        fill: true,
        pointRadius: 4,
        spanGaps: false,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { font: { size: 10 } } },
        y: { ticks: { callback: v => v + ' zł' } },
      }
    }
  });
}

/* Invoice table with click-to-expand detail rows */
function _renderInvoiceTable(invoices) {
  const wrap = document.getElementById('invoiceTableWrap');
  if (!wrap) return;
  if (!invoices || invoices.length === 0) {
    wrap.innerHTML = '<p style="color:var(--muted);font-size:13px">Brak wgranych faktur. Wgraj PDF powyżej, aby uzgodnić dane rozliczeniowe.</p>';
    return;
  }

  const n4 = v => v != null ? v.toFixed(4) : '-';
  const n2 = v => v != null ? v.toFixed(2) : '-';
  const n0 = v => v != null ? v.toFixed(0) : '-';

  const sorted = [...invoices].sort((a,b) => {
    // stubs sort before real months; real months sort newest-first
    if (a.is_stub && !b.is_stub) return -1;
    if (!a.is_stub && b.is_stub) return 1;
    const ka = a.month || a.key || '';
    const kb = b.month || b.key || '';
    return kb.localeCompare(ka);
  });

  let rows = '';
  sorted.forEach((inv, idx) => {
    const isStub = inv.is_stub || inv.needs_training;
    let statusCell, monthCell;

    if (isStub) {
      statusCell = '<span style="background:#fed7d7;color:#c0392b;padding:2px 6px;border-radius:3px;font-size:11px;font-weight:600">wymaga treningu</span>';
      const errTip = (inv.parse_error || '').replace(/"/g,'&quot;');
      monthCell = '<span style="color:var(--muted);font-size:11px" title="' + errTip + '">' +
        (inv.filename || inv.key || '?') + '</span>';
    } else {
      const chk = inv.reconciled
        ? '<span style="color:#27ae60;font-weight:700">✓</span>'
        : '<span style="color:#e67e22">…</span>';
      statusCell = chk;
      monthCell = (inv.month || '?') +
        (inv.billing_period_raw ? '<br><span style="font-size:10px;color:var(--muted)">' + inv.billing_period_raw + '</span>' : '');
    }

    const dI  = inv.diff_imported_kwh != null ? (inv.diff_imported_kwh >= 0 ? '+' : '') + inv.diff_imported_kwh.toFixed(0) : '—';
    const dE  = inv.diff_exported_kwh != null ? (inv.diff_exported_kwh >= 0 ? '+' : '') + inv.diff_exported_kwh.toFixed(0) : '—';
    const dIcol = (!isStub && Math.abs(inv.diff_imported_kwh||0) > 5) ? '#c0392b' : 'inherit';
    const dEcol = (!isStub && Math.abs(inv.diff_exported_kwh||0) > 5) ? '#c0392b' : 'inherit';

    let warnCell = '';
    if (!isStub && (inv.warnings_count || 0) > 0) {
      const warnTip = (inv.warnings||[]).join('\n').replace(/"/g,'&quot;');
      warnCell = '<span title="' + warnTip + '" style="cursor:help;color:#c0392b">⚠ ' + inv.warnings_count + '</span>';
    }

    const keyAttr = JSON.stringify(inv.key).replace(/"/g, '&quot;');
    const pdfBtns = inv.has_pdf
      ? '<button onclick="event.stopPropagation();viewInvoicePdf(' + keyAttr + ')" ' +
          'style="font-size:11px;padding:2px 7px;margin-right:3px;background:#718096;color:#fff;border:none;border-radius:3px;cursor:pointer" ' +
          'title="Otwórz oryginalny PDF">PDF</button>' +
        '<button onclick="event.stopPropagation();reparseInvoice(' + keyAttr + ')" ' +
          'style="font-size:11px;padding:2px 7px;margin-right:3px;background:#805ad5;color:#fff;border:none;border-radius:3px;cursor:pointer" ' +
          'title="Przelicz ponownie z zapisanego PDF (np. po poprawce parsera)">↻ PDF</button>'
      : '';
    const actionBtns = pdfBtns +
      '<button onclick="event.stopPropagation();openTrainModal(' + keyAttr + ')" ' +
        'style="font-size:11px;padding:2px 7px;margin-right:3px;background:#3182ce;color:#fff;border:none;border-radius:3px;cursor:pointer" ' +
        'title="Trenuj parser na tym układzie faktury">Trenuj</button>' +
      '<button onclick="event.stopPropagation();removeInvoice(' + keyAttr + ')" ' +
        'style="font-size:11px;padding:2px 7px;background:#e53e3e;color:#fff;border:none;border-radius:3px;cursor:pointer" ' +
        'title="Usuń fakturę i przywróć dane sprzed uzgodnienia">Usuń</button>';

    rows += '<tr onclick="toggleInvoiceDetail(' + idx + ')" style="cursor:pointer' + (isStub ? ';background:#fff5f5' : '') + '">' +
      '<td>' + monthCell + '</td>' +
      '<td style="color:var(--muted)">' + (inv.invoice_number || '—') + '</td>' +
      '<td style="text-align:right">' + (isStub ? '—' : n0(inv.imported_kwh)) + '</td>' +
      '<td style="text-align:right">' + (isStub ? '—' : n0(inv.exported_kwh)) + '</td>' +
      '<td style="text-align:right;color:' + dIcol + '">' + (isStub ? '—' : dI) + '</td>' +
      '<td style="text-align:right;color:' + dEcol + '">' + (isStub ? '—' : dE) + '</td>' +
      '<td style="text-align:right">' + (inv.amount_due != null ? inv.amount_due.toFixed(2) + ' zł' : '—') + '</td>' +
      '<td style="text-align:right">' + (inv.deposit_used != null ? inv.deposit_used.toFixed(2) + ' zł' : '—') + '</td>' +
      '<td style="text-align:right;font-size:11px">' + (isStub ? '—' : n4(inv.peak_gross)) + '</td>' +
      '<td style="text-align:right;font-size:11px">' + (isStub ? '—' : n4(inv.offpeak_gross)) + '</td>' +
      '<td style="text-align:center">' + statusCell + '</td>' +
      '<td style="text-align:center">' + warnCell + '</td>' +
      '<td style="text-align:right;white-space:nowrap">' + actionBtns + '</td>' +
    '</tr>' +
    '<tr id="inv-detail-' + idx + '" style="display:none;background:var(--bg)">' +
      '<td colspan="13" style="padding:10px 14px">' +
        (isStub
          ? '<div style="font-size:12px;color:#c0392b"><b>Błąd parsowania:</b> ' + (inv.parse_error||'').replace(/</g,'&lt;') + '</div>' +
            '<div style="font-size:12px;color:var(--muted);margin-top:4px">Kliknij <b>Trenuj</b>, aby ręcznie uzupełnić dane i nauczyć parser tego układu.</div>'
          : '<div style="display:flex;gap:24px;flex-wrap:wrap;font-size:12px">' +
            '<div><b>Sprzedaż energii (net zł/kWh)</b><br>' +
              'Szczyt: ' + n4(inv.energy_peak_net) + '<br>' +
              'Poza szczytem: ' + n4(inv.energy_offpeak_net) +
            '</div>' +
            '<div><b>Dystrybucja zmienna (net zł/kWh)</b><br>' +
              'Skł. zmienny szczyt: ' + n4(inv.dist_var_peak_net) + '<br>' +
              'Skł. zmienny poza: ' + n4(inv.dist_var_offpeak_net) + '<br>' +
              'Jakościowa: ' + n4(inv.dist_jakosciowa_net) + '<br>' +
              'OZE: ' + n4(inv.dist_oze_net) + '<br>' +
              'Kogeneracja: ' + n4(inv.dist_kogeneracja_net) +
            '</div>' +
            '<div><b>Opłaty stałe (net zł/mc)</b><br>' +
              'Mocowa: ' + n2(inv.fixed_mocowa_net) + '<br>' +
              'Abonament: ' + n2(inv.fixed_abonament_net) + '<br>' +
              'Skł. stały siec.: ' + n2(inv.fixed_stalysieciowy_net) +
              (inv.fixed_total_net != null ? '<br><b>Suma: ' + inv.fixed_total_net.toFixed(2) + ' zł</b>' : '') +
            '</div>' +
            '<div><b>Depozyt prosumencki (zł)</b><br>' +
              'Bieżący okres: ' + n2(inv.deposit_current) + '<br>' +
              'Z poprzednich: ' + n2(inv.deposit_previous) + '<br>' +
              'Rozliczony: ' + n2(inv.deposit_used) +
            '</div>' +
            '<div><b>Import szczyt/poza (kWh)</b><br>' +
              'Szczyt: ' + n0(inv.imported_kwh_peak) + '<br>' +
              'Poza: ' + n0(inv.imported_kwh_offpeak) + '<br>' +
              'Blended gross: ' + n4(inv.blended_gross) + ' zł/kWh<br>' +
              'Śr. cena: ' + n4(inv.avg_price) + ' zł/kWh' +
            '</div>' +
            (inv.warnings && inv.warnings.length > 0
              ? '<div><b style="color:#c0392b">Ostrzeżenia parsera</b><br>' +
                inv.warnings.map(w => '⚠ ' + w).join('<br>') + '</div>'
              : '') +
          '</div>') +
      '</td>' +
    '</tr>' +
    // Correction sub-rows (korekty / noty) nested under the billing month
    ((inv.corrections && inv.corrections.length > 0)
      ? inv.corrections.map(function(cor) {
          const isNota = cor.doc_type === 'nota';
          const badgeColor = isNota ? '#718096' : '#4299e1';
          const badgeLabel = isNota ? 'NOTA' : 'KOREKTA';
          const noPaymentFlag = (isNota && cor.requires_payment === false)
            ? ' <span style="background:#e9d8fd;color:#553c9a;padding:1px 5px;border-radius:2px;font-size:10px">NIE WYMAGA PŁATNOŚCI</span>'
            : '';
          const prevDep = cor.prev_deposit_previous;
          const newDep  = cor.deposit_previous;
          const depChange = (prevDep != null && newDep != null && prevDep !== newDep)
            ? ' · depozyt poprz.: <b>' + prevDep.toFixed(2) + ' → ' + newDep.toFixed(2) + ' zł</b>'
            : '';
          const deltaStr = cor.correction_delta_pln != null
            ? ' · delta: <b>' + (cor.correction_delta_pln >= 0 ? '+' : '') + cor.correction_delta_pln.toFixed(2) + ' zł</b>'
            : '';
          const amountStr = (!isNota && cor.amount_due != null)
            ? ' · do zapłaty (po kor.): ' + cor.amount_due.toFixed(2) + ' zł'
            : '';
          const reasonStr = cor.correction_reason
            ? ' · <em style="color:var(--muted)">' + cor.correction_reason.replace(/</g,'&lt;').substring(0,120) + '</em>'
            : '';
          const corKey = JSON.stringify(cor.key).replace(/"/g,'&quot;');
          const pdfBtn = cor.has_pdf
            ? '<button onclick="event.stopPropagation();viewInvoicePdf(' + corKey + ')" ' +
              'style="font-size:10px;padding:1px 5px;background:#718096;color:#fff;border:none;border-radius:2px;cursor:pointer;margin-left:6px">PDF</button>'
            : '';
          const delBtn = '<button onclick="event.stopPropagation();removeInvoice(' + corKey + ')" ' +
            'style="font-size:10px;padding:1px 5px;background:#e53e3e;color:#fff;border:none;border-radius:2px;cursor:pointer;margin-left:3px">Usuń</button>';
          return '<tr style="background:#f0f7ff">' +
            '<td colspan="13" style="padding:3px 8px 3px 28px;border-left:3px solid ' + badgeColor + ';font-size:11px">' +
              '<span style="background:' + badgeColor + ';color:#fff;padding:1px 5px;border-radius:2px;font-size:10px;font-weight:600">' + badgeLabel + '</span> ' +
              (cor.invoice_number || '—') +
              (cor.corrects_number ? ' <span style="color:var(--muted)">→ koryguje nr ' + cor.corrects_number + '</span>' : '') +
              noPaymentFlag +
              depChange + amountStr + deltaStr + reasonStr +
              pdfBtn + delBtn +
            '</td>' +
          '</tr>';
        }).join('')
      : '');
  });

  wrap.innerHTML =
    '<table style="width:100%;border-collapse:collapse;font-size:12px">' +
    '<thead><tr style="border-bottom:2px solid var(--border);font-size:11px">' +
      '<th style="text-align:left">Miesiąc / plik</th>' +
      '<th style="text-align:left">Nr FV</th>' +
      '<th style="text-align:right">Pobr. kWh</th>' +
      '<th style="text-align:right">Odd. kWh</th>' +
      '<th style="text-align:right">Δ pobr.</th>' +
      '<th style="text-align:right">Δ odd.</th>' +
      '<th style="text-align:right">Do zapłaty</th>' +
      '<th style="text-align:right">Depozyt uzyt.</th>' +
      '<th style="text-align:right">Szczyt zł/kWh</th>' +
      '<th style="text-align:right">Poza szczytem</th>' +
      '<th style="text-align:center">Status</th>' +
      '<th style="text-align:center">⚠</th>' +
      '<th style="text-align:right">Akcje</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table>';
}

function toggleInvoiceDetail(idx) {
  const row = document.getElementById('inv-detail-' + idx);
  if (row) row.style.display = row.style.display === 'none' ? '' : 'none';
}

/* ── Remove invoice ────────────────────────────────────────────────────────── */
async function removeInvoice(key) {
  if (!confirm('Usunąć fakturę "' + key + '"?\nDane miesiąca zostaną przywrócone do stanu sprzed uzgodnienia.')) return;
  try {
    const r = await fetch('api/invoice/remove', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key})
    });
    const d = await r.json();
    if (d.ok) {
      setTimeout(loadData, 800);
    } else {
      alert('Błąd usuwania: ' + (d.error || 'nieznany'));
    }
  } catch(e) {
    alert('Błąd połączenia: ' + e.message);
  }
}

/* ── Stored PDF: view / reparse ────────────────────────────────────────────── */
function viewInvoicePdf(key) {
  window.open('api/invoice/pdf?key=' + encodeURIComponent(key), '_blank');
}

async function reparseInvoice(key) {
  try {
    const r = await fetch('api/invoice/reparse', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key})
    });
    const d = await r.json();
    if (d.ok) {
      setTimeout(loadData, 800);
    } else {
      alert('Błąd przeliczania: ' + (d.error || 'nieznany'));
    }
  } catch(e) {
    alert('Błąd połączenia: ' + e.message);
  }
}

/* ── Train modal ───────────────────────────────────────────────────────────── */
let _trainModal = null;

// Per-field pastel colour palette (cycles if more fields than colours)
const _TX_COLOURS = [
  '#bee3f8','#c6f6d5','#fefcbf','#fed7e2','#e9d8fd','#feebc8',
  '#b2f5ea','#c3dafe','#fbd38d','#d6bcfa','#9ae6b4','#90cdf4',
];

function _txColour(idx) { return _TX_COLOURS[idx % _TX_COLOURS.length]; }

function _ensureTrainModal() {
  if (_trainModal) return _trainModal;
  const overlay = document.createElement('div');
  overlay.id = 'trainOverlay';
  overlay.style.cssText = 'display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:1000;overflow-y:auto;padding:16px';
  overlay.innerHTML =
    '<div style="background:var(--card);border-radius:8px;width:min(1100px, 96vw);margin:0 auto;padding:20px;position:relative">' +
      '<button onclick="closeTrainModal()" style="position:absolute;top:10px;right:12px;font-size:18px;background:none;border:none;cursor:pointer;color:var(--muted)">✕</button>' +
      '<h3 style="margin:0 0 12px;font-size:14px">Trenuj parser — skoryguj dane i naucz nowy układ</h3>' +
      '<div class="train-grid">' +
        '<div style="display:flex;flex-direction:column;gap:6px">' +
          '<div style="font-size:11px;font-weight:600;color:var(--muted)">Surowy tekst PDF <span style="font-weight:400">(najechaj na pole, aby podświetlić)</span></div>' +
          '<pre id="trainRawText" class="train-pane" style="font-size:10px;white-space:pre-wrap;word-break:break-all;background:var(--bg);padding:10px;border-radius:4px;margin:0;line-height:1.5"></pre>' +
        '</div>' +
        '<div style="display:flex;flex-direction:column;gap:4px">' +
          '<div style="font-size:11px;font-weight:600;color:var(--muted);margin-bottom:2px">Pola faktury <span style="font-weight:400">● znalezione &nbsp; ○ brakuje</span></div>' +
          '<div id="trainFieldsWrap" class="train-pane" style="padding-right:4px"></div>' +
        '</div>' +
      '</div>' +
      '<div style="margin-top:12px;display:flex;gap:10px;align-items:center">' +
        '<button id="trainSaveBtn" onclick="submitTrain()" style="background:#3182ce;color:#fff;border:none;padding:7px 16px;border-radius:4px;cursor:pointer;font-size:13px">Zapisz i naucz parser</button>' +
        '<button onclick="closeTrainModal()" style="background:var(--bg);border:1px solid var(--border);padding:7px 12px;border-radius:4px;cursor:pointer;font-size:13px">Anuluj</button>' +
        '<span id="trainMsg" style="font-size:12px;margin-left:8px"></span>' +
      '</div>' +
    '</div>';
  document.body.appendChild(overlay);
  _trainModal = overlay;
  return overlay;
}

let _trainCurrentKey = '';
let _trainCurrentData = {};

async function openTrainModal(key) {
  const overlay = _ensureTrainModal();
  const rawEl = document.getElementById('trainRawText');
  const fieldsEl = document.getElementById('trainFieldsWrap');
  const msg = document.getElementById('trainMsg');
  rawEl.innerHTML = '<span style="color:var(--muted)">Ładowanie…</span>';
  fieldsEl.innerHTML = '';
  msg.textContent = '';
  _trainCurrentKey = key;
  _trainCurrentData = {};
  overlay.style.display = '';

  try {
    const r = await fetch('api/invoice/train_form?key=' + encodeURIComponent(key));
    const d = await r.json();
    if (!d.ok) { rawEl.textContent = 'Błąd: ' + (d.error||'nieznany'); return; }
    _trainCurrentData = d;
    const f      = d.fields || {};
    const spans  = d.spans  || {};
    const rawTxt = d.raw_text || '';

    // ── Field definitions (field_key, label, input type, span_key_override?)
    const FIELDS = [
      ['year',                  'Rok (np. 2025)',              'number', 'billing_period'],
      ['month',                 'Miesiąc (1-12)',              'number', 'billing_period'],
      ['imported_kwh',          'Pobrano z sieci (kWh)',       'number'],
      ['exported_kwh',          'Wprowadzono do sieci (kWh)',  'number'],
      ['imported_kwh_peak',     'Import szczyt (kWh)',         'number', 'imp_peak'],
      ['imported_kwh_offpeak',  'Import poza szczytem (kWh)', 'number', 'imp_offpeak'],
      ['energy_peak_net',       'Energia szczyt net (zł/kWh)','number'],
      ['energy_offpeak_net',    'Energia poza net (zł/kWh)',  'number'],
      ['amount_due_pln',        'Do zapłaty (zł)',             'number', 'amount_due'],
      ['avg_price_pln_kwh',     'Śr. cena (zł/kWh)',          'number', 'avg_price'],
      ['deposit_current_pln',   'Depozyt bieżący (zł)',       'number', 'deposit_current'],
      ['deposit_previous_pln',  'Depozyt poprzednie (zł)',    'number', 'deposit_previous'],
      ['deposit_used_pln',      'Depozyt rozliczony (zł)',    'number', 'deposit_used'],
      ['fixed_mocowa_net',      'Opł. mocowa net (zł/mc)',    'number', 'fixed_mocowa'],
      ['fixed_abonament_net',   'Abonament net (zł/mc)',      'number', 'fixed_abonament'],
      ['fixed_stalysieciowy_net','Skł. stały siec. net (zł/mc)','number','fixed_stalysieciowy'],
      ['invoice_number',        'Nr faktury',                 'text'],
    ];

    // Assign a colour index to each unique span key
    const spanColourIdx = {};
    let colIdx = 0;
    FIELDS.forEach(([fk, , , sk]) => {
      const spanKey = sk || fk;
      if (!(spanKey in spanColourIdx)) spanColourIdx[spanKey] = colIdx++;
    });

    // ── Build annotated raw-text HTML
    rawEl.innerHTML = _buildAnnotatedText(rawTxt, spans, spanColourIdx);

    // ── Build field form
    let html = '<div style="display:flex;flex-direction:column;gap:6px">';
    FIELDS.forEach(([fk, label, type, spanKeyOverride]) => {
      const spanKey = spanKeyOverride || fk;
      const colour  = _txColour(spanColourIdx[spanKey] || 0);
      const span    = spans[spanKey];
      const hasSpan = !!span;
      const hasVal  = f[fk] != null && f[fk] !== '';
      const val     = hasVal ? f[fk] : '';

      // Status dot + snippet
      let dot, inputStyle, snippet = '';
      if (hasSpan) {
        dot = '<span style="color:#276749;font-size:14px" title="Auto-wykryto">●</span>';
        inputStyle = 'border-left:3px solid ' + colour + ';background:#fff';
        const snip = span.text.replace(/\n/g,' ').trim().substring(0, 45);
        snippet = '<small style="color:#276749;font-size:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:block" title="' + snip.replace(/"/g,'&quot;') + '">' + _hesc(snip) + '</small>';
      } else if (hasVal) {
        dot = '<span style="color:#c05621;font-size:14px" title="Wartość z zapisu (nie z parsowania)">●</span>';
        inputStyle = 'border-left:3px solid #f6ad55;background:#fff';
      } else {
        dot = '<span style="color:#c53030;font-size:14px" title="Nie znaleziono">○</span>';
        inputStyle = 'background:#fff5f5';
      }

      html += '<label id="tfl_' + fk + '" class="tf-found" ' +
        'style="font-size:12px;display:flex;flex-direction:column;gap:1px;padding:3px 4px;border-radius:3px" ' +
        'onmouseenter="_trainHover(\'' + fk + '\',\'' + spanKey + '\',true)" ' +
        'onmouseleave="_trainHover(\'' + fk + '\',\'' + spanKey + '\',false)">' +
        '<span style="display:flex;align-items:center;gap:4px;color:var(--muted)">' + dot + ' ' + label + '</span>' +
        snippet +
        '<input id="tf_' + fk + '" type="' + type + '" step="any" ' +
          'value="' + String(val).replace(/"/g,'&quot;') + '" ' +
          'style="padding:3px 5px;border:1px solid var(--border);border-radius:3px;font-size:12px;' + inputStyle + '">' +
      '</label>';
    });
    html += '</div>';
    fieldsEl.innerHTML = html;

  } catch(e) {
    rawEl.textContent = 'Błąd połączenia: ' + e.message;
  }
}

/* Build raw-text HTML with coloured span markers */
function _hesc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function _buildAnnotatedText(text, spans, colourIdx) {
  if (!text) return '<span style="color:var(--muted)">(brak tekstu PDF — wpisz wartości ręcznie)</span>';

  // Build a list of {start, end, spanKey} sorted by start, resolving overlaps
  const intervals = Object.entries(spans)
    .map(([sk, sp]) => ({sk, start: sp.start, end: sp.end}))
    .sort((a,b) => a.start - b.start);

  // Remove overlaps: if interval B starts before A ends, trim A
  const clean = [];
  let lastEnd = 0;
  for (const iv of intervals) {
    if (iv.start < lastEnd) continue; // skip overlapping
    clean.push(iv);
    lastEnd = iv.end;
  }

  // Build HTML
  let html = '';
  let pos = 0;
  for (const iv of clean) {
    if (iv.start > pos) html += _hesc(text.slice(pos, iv.start));
    const bg = _txColour(colourIdx[iv.sk] || 0);
    html += '<span id="ts-' + iv.sk + '" data-field="' + iv.sk + '" class="tx-span" ' +
      'style="background:' + bg + '" ' +
      'onmouseenter="_trainHoverSpan(\'' + iv.sk + '\',true)" ' +
      'onmouseleave="_trainHoverSpan(\'' + iv.sk + '\',false)">' +
      _hesc(text.slice(iv.start, iv.end)) +
      '</span>';
    pos = iv.end;
  }
  if (pos < text.length) html += _hesc(text.slice(pos));
  return html;
}

/* Bidirectional hover — called from form field labels */
function _trainHover(fk, spanKey, on) {
  const lbl = document.getElementById('tfl_' + fk);
  if (lbl) lbl.classList.toggle('tf-active', on);
  const sp = document.getElementById('ts-' + spanKey);
  if (sp) {
    sp.classList.toggle('tx-active', on);
    if (on) sp.scrollIntoView({behavior: 'smooth', block: 'center'});
  }
}

/* Bidirectional hover — called from text spans */
function _trainHoverSpan(spanKey, on) {
  const sp = document.getElementById('ts-' + spanKey);
  if (sp) sp.classList.toggle('tx-active', on);
  // Highlight every form field that shares this span key
  document.querySelectorAll('[id^="tfl_"]').forEach(lbl => {
    // Find the label whose onmouseenter references this spanKey
    const oe = lbl.getAttribute('onmouseenter') || '';
    if (oe.includes("'" + spanKey + "'")) {
      lbl.classList.toggle('tf-active', on);
      if (on) lbl.scrollIntoView({behavior: 'smooth', block: 'nearest'});
    }
  });
}

function closeTrainModal() {
  const overlay = document.getElementById('trainOverlay');
  if (overlay) overlay.style.display = 'none';
}

/* ── Docs modal ─────────────────────────────────────────────────────────── */
const _DOCS_HTML = `
<h2>Co robi dodatek</h2>
<p class="intro-box">PV ROI Tracker śledzi zwrot z inwestycji w instalację fotowoltaiczną na polskim rynku net-billing. Publikuje 42 sensory do Home Assistant przez MQTT Discovery i udostępnia panel ingress z historią ROI, prognozą spłaty, zarządzaniem fakturami Tauron i symulacją rozliczenia RCE.</p>
<table><thead><tr><th>Kiedy</th><th>Co się dzieje</th></tr></thead><tbody>
<tr><td><strong>Pierwszy start</strong></td><td>Pobiera historyczny CSV z Google Sheets, parsuje tabelę przestawną i zapisuje każdy miesiąc od 2023-06 do <code>/data/historic.json</code>. CSV nie jest pobierany ponownie.</td></tr>
<tr><td><strong>Co 30 minut</strong></td><td>Odczytuje bieżące sensory HA, uruchamia obliczenie ROI, publikuje sensory MQTT, odświeża porównanie taryf i symulację RCE.</td></tr>
<tr><td><strong>11.–20. dnia, co 2h (08–22)</strong></td><td>Scrapuje cenę RCEm za poprzedni miesiąc ze strony PSE, uzupełnia <code>historic.json</code>.</td></tr>
<tr><td><strong>1. dzień miesiąca, 06:00 UTC</strong></td><td>Skan korekt RCEm — PSE może zmieniać ceny wstecznie do 12 miesięcy.</td></tr>
<tr><td><strong>Ostatni dzień miesiąca, 23:55 local</strong></td><td>Snapshot bieżącego miesiąca do <code>historic.json</code>; wysyła polskie podsumowanie przez <code>notify.family</code> (jeśli włączone).</td></tr>
<tr><td><strong>16. dnia miesiąca, 12:00 UTC</strong></td><td>Odświeża wskaźnik CPI GUS (ROI skorygowany o inflację).</td></tr>
<tr><td><strong>Codziennie, 02:00 UTC</strong></td><td>Kopia zapasowa plików <code>/data</code> do <code>/share/pv_roi_tracker</code>.</td></tr>
</tbody></table>

<h2>Zakładki panelu</h2>
<h3>📅 Historia miesięczna</h3>
<p>Tabela wszystkich miesięcy: produkcja (kWh), eksport, autokonsumpcja, oszczędności, przychód z odsprzedaży, ROI skumulowany (%). Bieżący miesiąc oznaczony gwiazdką; miesiąc spłaty — zielonym tłem.</p>
<h3>📈 Prognoza spłaty</h3>
<p>Wachlarz spłaty — wykres skumulowanego zwrotu z pasmem niepewności P10–P90. Tabela prognozowanego miesiąca spłaty dla każdego scenariusza. Analiza wrażliwości: jak zmiana parametrów wpływa na termin spłaty.</p>
<h3>📊 Podsumowanie roczne</h3>
<p>Agregacja per rok kalendarzowy z kolumnami rok-do-roku (r/r): produkcja, oszczędności, ROI narastająco.</p>
<h3>📉 Wykresy</h3>
<ul>
<li><strong>Waterfall miesięczny</strong> — składniki oszczędności: autokonsumpcja, odsprzedaż, arbitraż bateryjny</li>
<li><strong>Sankey przepływu energii</strong> — bilans produkcja / eksport / autokonsumpcja / zakup z sieci</li>
<li><strong>Oszczędności nominalne vs realne CPI</strong> — wpływ inflacji na realną wartość oszczędności</li>
<li><strong>Trend degradacji kWh/kWp</strong> — specyficzny uzysk miesięczny z linią trendu</li>
<li><strong>Ranking produkcji miesięcznej</strong> — kolorowane per rok, medale top 3</li>
</ul>
<h3>📈 Analiza taryf</h3>
<p>Porównanie G12w (szczyt/poza szczytem) z taryfą dynamiczną opartą na godzinowych cenach RCE. Wykres 7-dniowy i tabela podsumowania. Stawki referencyjne G12w pochodzą z ostatniej przetworzonej faktury.</p>
<h3>📑 Taryfa</h3>
<p>Ręczne wpisy stawek z datą obowiązywania — wypełniają lukę ogłoszenia taryfy: nowe stawki Tauron wchodzą 1 stycznia, ale faktura potwierdzająca nadchodzi dopiero w lutym. Ręczny override automatycznie ustępuje po wgraniu faktury za dany miesiąc.<br><strong>Priorytet:</strong> baseline (seed) &lt; faktura &lt; override (aktywny tylko gdy data wpisu jest nowsza niż najnowsza faktura).</p>
<h3>⚡ RCE vs RCEm</h3>
<p>Symulacja przychodów z odsprzedaży przy rozliczeniu godzinowym RCE zamiast miesięcznego RCEm: godzinowa energia eksportowana × ceny 15-min RCE z PSE. Ceny ujemne zastępowane przez 0 zł (art. 4b ustawy o OZE). Heatmapa 24h×miesiąc: kiedy eksportujesz vs kiedy ceny są wysokie lub ujemne. Rekomendacja ROZWAŻ RCE / ZOSTAŃ PRZY RCEm / NEUTRALNA po ≥3 rozliczonych miesiącach.</p>

<h2>Faktury</h2>
<h3>Upload i trwałe przechowywanie PDF</h3>
<p>Wgraj plik PDF faktury Tauron przez przycisk w zakładce Faktury. Oryginały są trwale przechowywane w <code>/data/pdfs/</code> obok <code>invoices.json</code>. Każdy wiersz faktury ma przyciski:</p>
<ul>
<li><strong>PDF</strong> — podgląd oryginalnego pliku</li>
<li><strong>↻ PDF</strong> — przeliczenie faktury ponownie z zapisanego pliku (przydatne po poprawce parsera, bez ponownego wgrywania)</li>
</ul>
<h3>Typy dokumentów Tauron</h3>
<table><thead><tr><th>Typ</th><th>Rozpoznanie</th><th>Wpływ na ROI</th></tr></thead><tbody>
<tr><td><strong>FAKTURA VAT</strong></td><td>Rozliczeniowa (domyślna)</td><td>Pełny — stawki, kWh, depozyt</td></tr>
<tr><td><strong>FAKTURA VAT KOREKTA</strong></td><td>Nagłówek <code>FAKTURA VAT KOREKTA NR</code></td><td>Korekta depozytu przez <code>deposit.calculate()</code></td></tr>
<tr><td><strong>NOTA OBCIĄŻENIOWA</strong></td><td>Nagłówek <code>NOTA OBCI…</code></td><td>Tylko zapis i podgląd (brak kWh/stawek)</td></tr>
</tbody></table>
<p>Korekty wyświetlane jako zagnieżdżone pod-wiersze z badge KOREKTA/NOTA, delta PLN i powodem. Sensory MQTT pomijają korekty — bazują wyłącznie na fakturach rozliczeniowych.</p>
<h3>Rozbicie kosztów netto/brutto</h3>
<p>Parser wyciąga realne kwoty PLN per składnik: energia, zmienny sieciowy, jakościowa, OZE, kogeneracja, opłata przejściowa/handlowa, akcyza. Zakładka Faktury pokazuje tabelę sum z % udziału i wykres słupkowy skumulowany z przełącznikiem <strong>Netto / Brutto</strong>. Faktury bez realnych kwot (starsze lub brak kolumny wartości) są liczone ze stawki × kWh — UI sygnalizuje to notatką.</p>
<h3>Depozyt prosumencki (12-miesięczne przedawnienie)</h3>
<p>Czysty rejestr FIFO modeluje depozyt prosumencki: miesięczne akumulacje (= przychód z odsprzedaży), zużycie (z faktur lub szacunek falownika), <strong>12-miesięczne przedawnienie</strong> każdej akumulacji i limit zwrotu (<code>deposit_refund_pct</code>: 20% przy RCEm, 30% przy RCE godzinowym).</p>
<p>Bieżące saldo zakotwiczone na <em>saldzie post-invoice</em> z najnowszej faktury + akumulacje z falownika za miesiące jeszcze nierozliczone przez Tauron (lag księgowania auto-wykrywany: 1–3 miesięcy, domyślnie 2).</p>
<h3>Rekonsyliacja faktura vs falownik</h3>
<p>Tabela w zakładce Faktury: implikowane akumulacje Taurona (odtworzone z łańcucha sald faktur), zestawione miesiąc po miesiącu z modelem (eksport × RCEm ×1,23), różnica w PLN i %. Karty KPI: obie sumy, skumulowana różnica i wykryty lag.</p>
<h3>Najnowsza faktura jako źródło prawdy</h3>
<p>Stawki z chronologicznie najnowszej faktury (wybór po miesiącu rozliczeniowym, nie kolejności wgrywania) są publikowane jako 13 sensorów MQTT (<code>sensor.pv_roi_rate_*</code> / <code>sensor.pv_roi_fixed_*</code>) i czytane przez pakiet <code>energy_simulation.yaml</code> w konfiguracji HA. Wgranie starszej faktury po nowszej <strong>nigdy</strong> nie nadpisuje aktualnych stawek.</p>

<h2>Wzór ROI</h2>
<table><thead><tr><th>Składnik</th><th>Formuła</th></tr></thead><tbody>
<tr><td>Status</td><td><code>dofinansowanie + (autokonsumpcja + odsprzedaż + arbitraż bateryjny)</code></td></tr>
<tr><td>ROI %</td><td><code>status / koszt_brutto × 100</code></td></tr>
<tr><td>Pozostało do spłaty</td><td><code>max(0, koszt_brutto − status)</code></td></tr>
<tr><td>Miesięcy do spłaty</td><td><code>pozostało / średnie_miesięczne_oszczędności</code></td></tr>
</tbody></table>

<h2>Sensory MQTT</h2>
<p>Wszystkie 42 sensory widoczne pod urządzeniem <strong>PV ROI Tracker</strong> w HA → Ustawienia → Urządzenia. Stan <code>unknown</code> dopóki nie zostanie wgrana żadna faktura (dotyczy sensorów stawek).</p>
<table><thead><tr><th>Entity ID</th><th>Opis</th><th>Jedn.</th></tr></thead><tbody>
<tr><td><code>pv_roi_tracker_roi_pct</code></td><td>ROI instalacji PV</td><td>%</td></tr>
<tr><td><code>pv_roi_tracker_payback_years</code></td><td>Czas do spłaty</td><td>lata</td></tr>
<tr><td><code>pv_roi_tracker_payback_date</code></td><td>Data spłaty</td><td>ISO</td></tr>
<tr><td><code>pv_roi_tracker_total_savings</code></td><td>Łączne oszczędności</td><td>PLN</td></tr>
<tr><td><code>pv_roi_tracker_self_consumption_savings</code></td><td>Oszczędności autokonsumpcja</td><td>PLN</td></tr>
<tr><td><code>pv_roi_tracker_feedin_revenue</code></td><td>Przychód z odsprzedaży</td><td>PLN</td></tr>
<tr><td><code>pv_roi_tracker_net_investment</code></td><td>Inwestycja netto (koszt − dotacja)</td><td>PLN</td></tr>
<tr><td><code>pv_roi_tracker_monthly_avg_savings</code></td><td>Śr. miesięczne oszczędności</td><td>PLN</td></tr>
<tr><td><code>pv_roi_tracker_total_produced_kwh</code></td><td>Łączna produkcja</td><td>kWh</td></tr>
<tr><td><code>pv_roi_tracker_total_exported_kwh</code></td><td>Łączny eksport</td><td>kWh</td></tr>
<tr><td><code>pv_roi_tracker_specific_yield</code></td><td>Uzysk specyficzny (życie instalacji)</td><td>kWh/kWp</td></tr>
<tr><td><code>pv_roi_tracker_battery_arbitrage_savings</code></td><td>Oszczędności arbitraż bateryjny</td><td>PLN</td></tr>
<tr><td><code>pv_roi_tracker_net_profit</code></td><td>Zysk netto (ponad inwestycję)</td><td>PLN</td></tr>
<tr><td><code>pv_roi_tracker_current_month_savings</code></td><td>Oszczędności bieżącego miesiąca</td><td>PLN</td></tr>
<tr><td><code>pv_roi_tracker_rcem_scrape_status</code></td><td>Status pobierania RCEm z PSE</td><td>—</td></tr>
<tr><td><code>pv_roi_tracker_projected_month_kwh</code></td><td>Prognoza produkcji (Solcast)</td><td>kWh</td></tr>
<tr><td><code>pv_roi_tracker_projected_month_savings</code></td><td>Prognoza oszczędności miesiąca</td><td>PLN</td></tr>
<tr><td><code>pv_roi_tracker_real_total_savings</code></td><td>Realne oszczędności (deflacja CPI)</td><td>PLN</td></tr>
<tr><td><code>pv_roi_tracker_real_roi_pct</code></td><td>Realny ROI (CPI)</td><td>%</td></tr>
<tr><td><code>pv_roi_tracker_npv</code></td><td>NPV (wartość bieżąca netto)</td><td>PLN</td></tr>
<tr><td><code>pv_roi_tracker_irr_pct</code></td><td>IRR (wewnętrzna stopa zwrotu)</td><td>%</td></tr>
<tr><td><code>pv_roi_tracker_vs_bond_delta</code></td><td>Delta vs obligacja skarbowa</td><td>PLN</td></tr>
<tr><td><code>pv_roi_tracker_cumulative_inflation</code></td><td>Skumulowana inflacja CPI (GUS)</td><td>%</td></tr>
<tr><td><code>pv_roi_tracker_self_consumption_rate</code></td><td>Wskaźnik autokonsumpcji</td><td>%</td></tr>
<tr><td><code>pv_roi_tracker_autarky</code></td><td>Autarkia energetyczna</td><td>%</td></tr>
<tr><td><code>pv_roi_tracker_co2_avoided</code></td><td>Uniknięte emisje CO₂ (KOBiZE)</td><td>kg</td></tr>
<tr><td><code>pv_roi_tracker_yoy_yield_delta</code></td><td>Delta uzysku rok do roku</td><td>%</td></tr>
<tr><td><code>pv_roi_tracker_deposit_balance_est</code></td><td>Szacowane saldo depozytu</td><td>PLN</td></tr>
<tr><td><code>pv_roi_tracker_deposit_expiring_30d</code></td><td>Depozyt wygasający w ciągu 30 dni</td><td>PLN</td></tr>
<tr><td><code>pv_roi_tracker_health</code></td><td>Stan zdrowia dodatku (ok/degraded/error)</td><td>—</td></tr>
<tr><td><code>pv_roi_tracker_rate_energy_peak_net</code></td><td>Stawka energii szczyt netto</td><td>PLN/kWh</td></tr>
<tr><td><code>pv_roi_tracker_rate_energy_offpeak_net</code></td><td>Stawka energii poza szczytem netto</td><td>PLN/kWh</td></tr>
<tr><td><code>pv_roi_tracker_rate_dist_var_peak_net</code></td><td>Zmienny dystrybucyjny szczyt netto</td><td>PLN/kWh</td></tr>
<tr><td><code>pv_roi_tracker_rate_dist_var_offpeak_net</code></td><td>Zmienny dystrybucyjny poza szczytem</td><td>PLN/kWh</td></tr>
<tr><td><code>pv_roi_tracker_rate_jakosciowa_net</code></td><td>Opłata jakościowa netto</td><td>PLN/kWh</td></tr>
<tr><td><code>pv_roi_tracker_rate_oze_net</code></td><td>Opłata OZE netto</td><td>PLN/kWh</td></tr>
<tr><td><code>pv_roi_tracker_rate_kogeneracja_net</code></td><td>Opłata kogeneracyjna netto</td><td>PLN/kWh</td></tr>
<tr><td><code>pv_roi_tracker_fixed_mocowa_net</code></td><td>Opłata mocowa (stała miesięczna)</td><td>PLN</td></tr>
<tr><td><code>pv_roi_tracker_fixed_abonament_net</code></td><td>Abonament (stały miesięczny)</td><td>PLN</td></tr>
<tr><td><code>pv_roi_tracker_fixed_stalysieciowy_net</code></td><td>Stały sieciowy (miesięczny)</td><td>PLN</td></tr>
<tr><td><code>pv_roi_tracker_fixed_total_net</code></td><td>Suma opłat stałych</td><td>PLN</td></tr>
<tr><td><code>pv_roi_tracker_rate_peak_gross</code></td><td>Cena brutto G12w szczyt</td><td>PLN/kWh</td></tr>
<tr><td><code>pv_roi_tracker_rate_offpeak_gross</code></td><td>Cena brutto G12w poza szczytem</td><td>PLN/kWh</td></tr>
</tbody></table>

<h2>Opcje konfiguracji</h2>
<table><thead><tr><th>Opcja</th><th>Domyślnie</th><th>Opis</th></tr></thead><tbody>
<tr><td><code>gross_investment</code></td><td>51 900,00</td><td>Całkowity koszt projektu przed dofinansowaniem (zł)</td></tr>
<tr><td><code>subsidy</code></td><td>28 714,00</td><td>Jednorazowe dofinansowanie rządowe (zł)</td></tr>
<tr><td><code>system_kwp</code></td><td>6,72</td><td>Zainstalowana moc szczytowa (kWp)</td></tr>
<tr><td><code>poll_interval_minutes</code></td><td>30</td><td>Jak często przeliczać i publikować dane (minuty)</td></tr>
<tr><td><code>mqtt_host</code></td><td>core-mosquitto</td><td>Hostname brokera MQTT</td></tr>
<tr><td><code>mqtt_port</code></td><td>1883</td><td>Port TCP brokera MQTT</td></tr>
<tr><td><code>mqtt_user</code> / <code>mqtt_password</code></td><td>(puste)</td><td>Dane logowania MQTT — puste = bez autoryzacji</td></tr>
<tr><td><code>log_level</code></td><td>info</td><td>Poziom logowania: debug / info / warning / error</td></tr>
<tr><td><code>backup_share</code></td><td>/share/pv_roi_tracker</td><td>Cel codziennej kopii zapasowej plików /data</td></tr>
<tr><td><code>discount_rate_real</code></td><td>0,04</td><td>Realna stopa dyskontowa dla NPV (4%)</td></tr>
<tr><td><code>inflation_rate_assumption</code></td><td>0,05</td><td>Zakładana inflacja gdy CPI GUS niedostępne (5%)</td></tr>
<tr><td><code>comparison_yield_rate</code></td><td>0,055</td><td>Stopa zwrotu alternatywnej inwestycji — obligacje (5,5%)</td></tr>
<tr><td><code>battery_roundtrip_efficiency</code></td><td>0,92</td><td>Sprawność bateryjna round-trip dla arbitrażu (92%)</td></tr>
<tr><td><code>monthly_notify</code></td><td>true</td><td>Wysyłaj polskie podsumowanie miesięczne przez notify.family</td></tr>
<tr><td><code>co2_factor_kg_kwh</code></td><td>0,597</td><td>Wskaźnik emisji CO₂ sieci elektrycznej (KOBiZE)</td></tr>
<tr><td><code>deposit_refund_pct</code></td><td>0,20</td><td>Limit zwrotu przedawnionego depozytu: 0,20 (RCEm) / 0,30 (RCE)</td></tr>
</tbody></table>

<h2>Pliki danych</h2>
<table><thead><tr><th>Plik</th><th>Opis</th></tr></thead><tbody>
<tr><td><code>/data/historic.json</code></td><td>Zamrożone rekordy miesięczne (+ kopia .bak)</td></tr>
<tr><td><code>/data/rcem_history.json</code></td><td>Ceny RCEm wg klucza YYYY-MM (PLN/kWh brutto), ostatnie 60 miesięcy</td></tr>
<tr><td><code>/data/rcem_corrections.json</code></td><td>Historia korekt cen PSE (do 12 mies. wstecz)</td></tr>
<tr><td><code>/data/rce_hourly.json</code></td><td>Cache godzinowych cen RCE + zamrożone wyniki RCE-vs-RCEm</td></tr>
<tr><td><code>/data/invoices.json</code></td><td>Przetworzone faktury Tauron (metadane, stawki, kwoty)</td></tr>
<tr><td><code>/data/pdfs/</code></td><td>Oryginalne pliki PDF faktur (trwałe przechowywanie)</td></tr>
<tr><td><code>/data/tariff_config.json</code></td><td>Ręczne wpisy stawek taryfy (zakładka Taryfa)</td></tr>
<tr><td><code>/data/invoice_layouts.json</code></td><td>Wyuczone wzorce parsera faktur</td></tr>
<tr><td><code>/data/cpi_history.json</code></td><td>Łańcuchowy indeks CPI z GUS</td></tr>
</tbody></table>
<p>Wszystkie pliki kopiowane codziennie do <code>/share/pv_roi_tracker</code>.</p>
`;

let _docsModal = null;
function _ensureDocsModal() {
  if (_docsModal) return _docsModal;
  const overlay = document.createElement('div');
  overlay.id = 'docsOverlay';
  overlay.style.cssText = 'display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:1000;overflow-y:auto;padding:16px';
  overlay.addEventListener('click', function(e) { if (e.target === overlay) closeDocsModal(); });
  const card = document.createElement('div');
  card.style.cssText = 'background:var(--card);border-radius:8px;width:min(900px,96vw);margin:0 auto;padding:24px 28px;position:relative;overflow-x:hidden';
  card.innerHTML =
    '<button onclick="closeDocsModal()" title="Zamknij" style="position:absolute;top:10px;right:12px;font-size:18px;background:none;border:none;cursor:pointer;color:var(--muted)">&#x2715;</button>' +
    '<h2 style="margin:0 0 16px;font-size:16px;color:var(--accent);border:none;padding:0">&#128214; Dokumentacja &mdash; PV ROI Tracker</h2>' +
    '<div class="docs-body">' + _DOCS_HTML + '</div>';
  overlay.appendChild(card);
  document.body.appendChild(overlay);
  _docsModal = overlay;
  return overlay;
}
function openDocsModal()  { _ensureDocsModal().style.display = ''; }
function closeDocsModal() {
  const overlay = document.getElementById('docsOverlay');
  if (overlay) overlay.style.display = 'none';
}
document.addEventListener('keydown', function(e) { if (e.key === 'Escape') closeDocsModal(); });

async function submitTrain() {
  const msg = document.getElementById('trainMsg');
  msg.textContent = 'Zapisywanie…';
  const FIELD_KEYS = ['year','month','imported_kwh','exported_kwh','imported_kwh_peak',
    'imported_kwh_offpeak','energy_peak_net','energy_offpeak_net','amount_due_pln',
    'avg_price_pln_kwh','deposit_current_pln','deposit_previous_pln','deposit_used_pln',
    'fixed_mocowa_net','fixed_abonament_net','fixed_stalysieciowy_net','invoice_number'];

  const fields = {};
  FIELD_KEYS.forEach(fk => {
    const el = document.getElementById('tf_' + fk);
    if (!el) return;
    const v = el.value.trim();
    if (v === '') { fields[fk] = null; return; }
    if (el.type === 'number') {
      const n = parseFloat(v.replace(',', '.'));
      fields[fk] = isNaN(n) ? null : n;
    } else {
      fields[fk] = v;
    }
  });

  const year = fields['year'];
  const month = fields['month'];
  if (!year || !month) { msg.textContent = 'Rok i miesiąc są wymagane.'; return; }

  try {
    const r = await fetch('api/invoice/train', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        key: _trainCurrentKey,
        year: parseInt(year),
        month: parseInt(month),
        fields,
        raw_text: _trainCurrentData.raw_text || '',
        filename: _trainCurrentData.filename || '',
      })
    });
    const d = await r.json();
    if (d.ok) {
      const lf = (d.learned_fields || []).length;
      msg.style.color = '#27ae60';
      msg.textContent = 'Zapisano! Nauczono ' + lf + ' wzorców' + (lf ? ': ' + d.learned_fields.join(', ') : '') + '.';
      setTimeout(() => { closeTrainModal(); loadData(); }, 1500);
    } else {
      msg.style.color = '#c0392b';
      msg.textContent = 'Błąd: ' + (d.error || 'nieznany');
    }
  } catch(e) {
    msg.style.color = '#c0392b';
    msg.textContent = 'Błąd połączenia: ' + e.message;
  }
}

/* ── Trained-layouts panel renderer ──────────────────────────────────────── */
function _renderLayoutsPanel(summary) {
  const wrap = document.getElementById('invLayoutsPanel');
  if (!wrap) return;
  if (!summary) { wrap.style.display = 'none'; return; }
  wrap.style.display = '';
  const fc = summary.field_counts || {};
  const fields = Object.keys(fc);
  const exCount = summary.examples || 0;
  if (!fields.length && !exCount) {
    wrap.innerHTML = '<p style="font-size:12px;color:var(--muted)">Brak wytrenowanych układów.</p>';
    return;
  }
  let html = '<div style="font-size:12px">';
  html += '<b>' + exCount + '</b> wytrenowane sesje; wzorce dla pól: ';
  html += fields.map(f => '<span style="background:var(--bg);border:1px solid var(--border);border-radius:3px;padding:1px 6px;margin:0 2px">' + f + ' (' + fc[f] + ')</span>').join('') || '—';
  html += ' <button onclick="clearLayouts()" style="margin-left:12px;font-size:11px;padding:2px 8px;background:#e53e3e;color:#fff;border:none;border-radius:3px;cursor:pointer">Wyczyść wytrenowane układy</button>';
  html += '</div>';
  wrap.innerHTML = html;
}

async function clearLayouts() {
  if (!confirm('Wyczyścić wszystkie wytrenowane wzorce?\nBędziesz musiał(-a) ponownie nauczyć parser nowych układów.')) return;
  try {
    const r = await fetch('api/invoice/layouts/clear', {method:'POST'});
    const d = await r.json();
    if (d.ok) setTimeout(loadData, 500);
    else alert('Błąd: ' + (d.error||'nieznany'));
  } catch(e) { alert('Błąd połączenia'); }
}

/* Invoice debug raw-text view */
async function runInvoiceDebug() {
  const input = document.getElementById('debugPdfFile');
  const out   = document.getElementById('debugOutput');
  if (!input || !input.files || !input.files.length) {
    out.innerHTML = '<p style="color:var(--muted);font-size:13px">Wybierz plik PDF.</p>';
    return;
  }
  out.innerHTML = '<p style="font-size:13px;color:var(--muted)">Parsowanie…</p>';
  const fd = new FormData();
  fd.append('files', input.files[0]);
  try {
    const r = await fetch('api/invoice/debug', {method: 'POST', body: fd});
    const d = await r.json();
    let html = '';
    if (d.warnings && d.warnings.length > 0) {
      html += '<div style="padding:8px 12px;background:#fff3cd;border-left:4px solid #f0ad4e;border-radius:4px;margin-bottom:8px;font-size:12px">' +
        '<b>Ostrzeżenia (' + d.warnings.length + '):</b><br>' +
        d.warnings.map(w => '⚠ ' + w).join('<br>') + '</div>';
    }
    if (!d.ok) {
      html += '<div style="padding:8px 12px;background:#fed7d7;border-left:4px solid #c0392b;border-radius:4px;margin-bottom:8px;font-size:12px">Błąd parsera: ' + (d.error||'nieznany') + '</div>';
    }
    html += '<pre style="font-size:10px;white-space:pre-wrap;word-break:break-all;background:var(--bg);padding:10px;border-radius:4px;max-height:400px;overflow-y:auto">' +
      (d.text || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') + '</pre>';
    out.innerHTML = html;
  } catch(e) {
    out.innerHTML = '<p style="color:#c0392b;font-size:13px">Błąd połączenia: ' + e.message + '</p>';
  }
}

/* -- Pre-fill override month to last month -- */
(function() {
  const d = new Date(); d.setMonth(d.getMonth() - 1);
  const m = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
  document.getElementById('ovMonth').value = m;
  document.getElementById('ovMonth').max   = m;
})();

/* ===================== TARIFF COMPARISON TAB ===================== */
function renderTariffTab(tc) {
  if (!tc) return;
  const s = tc.summary || {};
  const cm = tc.current_month || {};
  const c7d = tc.chart_7d || {};
  const pv = tc.pv_context || {};

  // Warning banner
  const banner = document.getElementById('tariffWarning');
  if (banner) banner.style.display = s.n_months_warning ? '' : 'none';

  /* --- Sekcja 1: TERAZ — status bar --- */
  const dynCheaper = cm.dyn_cheaper_now === 'on';
  const dynSpike   = cm.dyn_spike_now   === 'on';
  const clr  = dynCheaper ? '#27ae60' : '#e74c3c';
  const ico  = dynCheaper ? '🟢' : '🔴';
  const spikeBadge = dynSpike
    ? '<span style="background:#e74c3c;color:#fff;padding:2px 10px;border-radius:4px;font-size:12px">⚡ KOLEC CENOWY</span>'
    : '';
  const pill = (label, val, c) =>
    `<div style="background:#fff;border:2px solid ${c || '#ddd'};border-radius:6px;padding:6px 12px;font-size:13px">${label}: <strong>${val || '—'}</strong></div>`;
  const statusEl = document.getElementById('tariffStatusBar');
  if (statusEl) statusEl.innerHTML =
    pill(ico + ' Dyn. tańsza teraz', dynCheaper ? 'TAK' : 'NIE', clr) +
    pill('G12w teraz', price(cm.g12w_price_now), '#e74c3c') +
    pill('Dynamiczna teraz', price(cm.dyn_price_now), '#2ecc71') +
    pill('Różnica/kWh', (cm.diff_kwh_now > 0 ? '+' : '') + price(cm.diff_kwh_now), clr) +
    spikeBadge;

  /* --- Chart A: 7-day price --- */
  const ctxA = document.getElementById('tariffPriceChart');
  if (ctxA && c7d.labels && c7d.labels.length) {
    if (_tariffPriceChart) { _tariffPriceChart.destroy(); _tariffPriceChart = null; }
    // Reference lines — sourced from the latest parsed invoice when available
    // (tc.summary.effective_peak_gross/effective_offpeak_gross), else the
    // same 1.23/0.63 fallback this chart always used.
    const effPeak    = (tc.summary && tc.summary.effective_peak_gross    != null) ? tc.summary.effective_peak_gross    : 1.23;
    const effOffpeak = (tc.summary && tc.summary.effective_offpeak_gross != null) ? tc.summary.effective_offpeak_gross : 0.63;
    const refPeak   = c7d.labels.map(() => effPeak);
    const refOffpeak= c7d.labels.map(() => effOffpeak);
    _tariffPriceChart = new Chart(ctxA, {
      type: 'line',
      data: { labels: c7d.labels, datasets: [
        { label: 'G12w PLN/kWh', data: c7d.g12w, borderColor: '#e74c3c', backgroundColor: 'rgba(231,76,60,0.05)', borderWidth: 2, stepped: true, pointRadius: 0, fill: false },
        { label: 'Dynamiczna PLN/kWh', data: c7d.dynamic, borderColor: '#2ecc71', backgroundColor: 'rgba(46,204,113,0.05)', borderWidth: 1.5, pointRadius: 0, fill: false },
        { label: '— szczyt G12w (' + effPeak.toFixed(2) + ')',    data: refPeak,    borderColor: 'rgba(231,76,60,0.35)', borderWidth: 1, borderDash: [5,5], pointRadius: 0, fill: false },
        { label: '— pozaszczyt (' + effOffpeak.toFixed(2) + ')',  data: refOffpeak, borderColor: 'rgba(243,156,18,0.35)', borderWidth: 1, borderDash: [5,5], pointRadius: 0, fill: false },
      ]},
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { boxWidth: 10, font: { size: 11 } } } },
        scales: {
          x: { ticks: { maxTicksLimit: 7, font: { size: 10 }, maxRotation: 0 } },
          y: { title: { display: true, text: 'PLN/kWh', font: { size: 11 } }, min: 0 }
        }
      }
    });
  }



  /* --- KPI cards row 1 & 2 --- */
  const recColor = s.recommendation === 'ZMIEŃ' ? '#27ae60' : s.recommendation === 'ZOSTAŃ' ? '#e74c3c' : '#777';
  const kpiCards = [
    { lbl: 'Śr. oszczędność/mies.', val: pln(s.avg_monthly_savings_pln, 0) + ' PLN', sub: 'zmienność ±' + pln(s.savings_stddev_pln, 0), cls: (s.avg_monthly_savings_pln || 0) > 0 ? 'c-green' : '' },
    { lbl: 'Prognoza roczna', val: pln(s.projected_annual_pln, 0) + ' PLN', sub: `pesy ${pln(s.projected_annual_pessimistic,0)} / opty ${pln(s.projected_annual_optimistic,0)}` },
    { lbl: 'Dyn. tańsza % czasu', val: pct(s.pct_dynamic_cheaper), sub: s.months_dynamic_cheaper + '/' + s.months_total + ' mies.' },
    { lbl: 'Rekomendacja', val: s.recommendation || '—', sub: s.recommendation_reason, cls: s.recommendation === 'ZMIEŃ' ? 'c-green' : '' },
    s.payback_impact_months != null ? { lbl: 'Skrócenie paybacku PV', val: (s.payback_impact_months > 0 ? '−' : '+') + Math.abs(s.payback_impact_months).toFixed(1) + ' mies.', cls: (s.payback_impact_months || 0) > 0 ? 'c-green' : '', sub: 'nowa data spłaty: ' + (s.new_payback_date || '—') } : null,
    pv.avg_purchased_kwh ? { lbl: 'PV autokonsumpcja', val: pct(pv.pv_reduces_tariff_benefit_pct), sub: 'PV pochłania wartość zakupu', cls: '' } : null,
  ].filter(Boolean);
  const kpiEl = document.getElementById('tariffKpiCards');
  if (kpiEl) kpiEl.innerHTML = kpiCards.map(c =>
    `<div class="card ${c.cls || ''}" style="min-width:160px"><div class="card-lbl">${c.lbl}</div><div class="card-val">${c.val}</div>${c.sub ? '<div class="card-sub">' + c.sub + '</div>' : ''}</div>`
  ).join('');

  /* --- Chart 1: monthly comparison bars --- */
  const months = tc.months || [];
  const mLabels = months.map(m => m.month_label);
  const g12wData = months.map(m => m.g12w_variable_pln);
  const dynData  = months.map(m => m.dynamic_variable_pln);
  const scData   = months.map(m => m.self_consumption_savings_pln);
  const ctx1 = document.getElementById('tariffCompChart');
  if (ctx1 && months.length) {
    if (_tariffCompChart) { _tariffCompChart.destroy(); _tariffCompChart = null; }
    _tariffCompChart = new Chart(ctx1, {
      type: 'bar',
      data: { labels: mLabels, datasets: [
        { label: 'G12w zmienny PLN', data: g12wData, backgroundColor: 'rgba(52,152,219,0.75)', borderRadius: 3 },
        { label: 'Dynamiczna zmienna PLN', data: dynData, backgroundColor: 'rgba(46,204,113,0.75)', borderRadius: 3 },
        { label: 'Autokonsumpcja PV PLN', data: scData, type: 'line', borderColor: '#f39c12', backgroundColor: 'rgba(243,156,18,0.1)', borderWidth: 2, pointRadius: 3, fill: false, yAxisID: 'y' },
      ]},
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { boxWidth: 10 } }, tooltip: {
          callbacks: { afterBody: (items) => {
            const i = items[0].dataIndex; const m = months[i];
            return ['', `Różnica: ${m.diff_pln > 0 ? '+' : ''}${m.diff_pln} PLN`, `Zakup: ${m.purchased_kwh} kWh`, `Produkcja PV: ${m.produced_kwh} kWh`, `PV offset: ${m.pv_offset_pct}%`];
          }}
        }},
        scales: { x: { ticks: { font: { size: 10 } } }, y: { title: { display: true, text: 'PLN', font: { size: 11 } } } }
      }
    });
  }

  /* --- Chart 2: cumulative savings with confidence --- */
  const ctx2 = document.getElementById('tariffCumChart');
  if (ctx2 && months.length) {
    if (_tariffCumChart) { _tariffCumChart.destroy(); _tariffCumChart = null; }
    let cum = 0, cumPesy = 0, cumOpty = 0;
    const cumBase = [], cumP = [], cumO = [];
    const stdDev = s.savings_stddev_pln || 0;
    for (const m of months) {
      cum += m.diff_pln; cumPesy += m.diff_pln - stdDev; cumOpty += m.diff_pln + stdDev;
      cumBase.push(round2(cum)); cumP.push(round2(cumPesy)); cumO.push(round2(cumOpty));
    }
    _tariffCumChart = new Chart(ctx2, {
      type: 'line',
      data: { labels: mLabels, datasets: [
        { label: 'Optimistyczny', data: cumO, borderColor: 'rgba(46,204,113,0.4)', backgroundColor: 'rgba(46,204,113,0.08)', borderWidth: 1, pointRadius: 0, fill: '+1' },
        { label: 'Bazowy', data: cumBase, borderColor: '#2ecc71', backgroundColor: 'transparent', borderWidth: 2, pointRadius: 3, fill: false },
        { label: 'Pesymistyczny', data: cumP, borderColor: 'rgba(231,76,60,0.4)', backgroundColor: 'rgba(231,76,60,0.08)', borderWidth: 1, pointRadius: 0, fill: '-1' },
      ]},
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { boxWidth: 10 } } },
        scales: { x: { ticks: { font: { size: 10 } } }, y: { title: { display: true, text: 'PLN', font: { size: 11 } } } }
      }
    });
  }

  /* --- Chart 3: seasonal grouped bar --- */
  const ctx3 = document.getElementById('tariffSeasonChart');
  if (ctx3) {
    if (_tariffSeasonChart) { _tariffSeasonChart.destroy(); _tariffSeasonChart = null; }
    const sumMonths = months.filter(m => m.season === 'summer');
    const winMonths = months.filter(m => m.season === 'winter');
    const _savg = (arr, field) => arr.length ? arr.reduce((s, m) => s + m[field], 0) / arr.length : 0;
    _tariffSeasonChart = new Chart(ctx3, {
      type: 'bar',
      data: {
        labels: ['Lato (IV–IX)', 'Zima (X–III)'],
        datasets: [
          { label: 'G12w śr. (PLN)', data: [
            _savg(sumMonths, 'g12w_variable_pln'), _savg(winMonths, 'g12w_variable_pln'),
          ], backgroundColor: 'rgba(52,152,219,0.75)', borderRadius: 3 },
          { label: 'Dyn. śr. (PLN)', data: [
            _savg(sumMonths, 'dynamic_variable_pln'), _savg(winMonths, 'dynamic_variable_pln'),
          ], backgroundColor: 'rgba(46,204,113,0.75)', borderRadius: 3 },
        ]
      },
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { labels: { boxWidth: 10 } } },
        scales: { y: { title: { display: true, text: 'PLN (śr./mies.)', font: { size: 10 } } } }
      }
    });
  }

  /* --- Chart 4: histogram --- */
  const ctx4 = document.getElementById('tariffHistChart');
  const hist = s.histogram || [];
  if (ctx4 && hist.length) {
    if (_tariffHistChart) { _tariffHistChart.destroy(); _tariffHistChart = null; }
    _tariffHistChart = new Chart(ctx4, {
      type: 'bar',
      data: { labels: hist.map(b => b.label), datasets: [{
        label: 'Liczba miesięcy',
        data: hist.map(b => b.count),
        backgroundColor: hist.map(b => b.label.startsWith('<') || b.label.startsWith('−') ? 'rgba(231,76,60,0.7)' : 'rgba(46,204,113,0.7)'),
        borderRadius: 3,
      }]},
      options: { responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { title: { display: true, text: 'Różnica G12w − Dynamiczna (PLN)', font: { size: 10 } }, ticks: { font: { size: 10 } } },
          y: { ticks: { stepSize: 1 }, title: { display: true, text: 'Miesięcy', font: { size: 10 } } }
        }
      }
    });
  }

  /* --- Prediction section --- */
  const predEl = document.getElementById('tariffPredCards');
  if (predEl) {
    const cards = [
      { lbl: 'Pesymistyczny', val: pln(s.projected_annual_pessimistic, 0) + ' PLN/rok', cls: (s.projected_annual_pessimistic || 0) < 0 ? 'c-red' : '' },
      { lbl: 'Bazowy', val: pln(s.projected_annual_pln, 0) + ' PLN/rok', cls: (s.projected_annual_pln || 0) > 0 ? 'c-green' : '' },
      { lbl: 'Optymistyczny', val: pln(s.projected_annual_optimistic, 0) + ' PLN/rok', cls: '' },
    ];
    predEl.innerHTML = cards.map(c =>
      `<div class="card ${c.cls||''}" style="flex:1;min-width:120px"><div class="card-lbl" style="font-size:11px">${c.lbl}</div><div class="card-val">${c.val}</div></div>`
    ).join('');
  }
  const sensiTbl = document.getElementById('tariffSensiTbl');
  if (sensiTbl) {
    const sensi = tc.sensitivity_g12w || [];
    sensiTbl.innerHTML = '<tr><th style="text-align:left;padding:3px 6px;border-bottom:1px solid #ddd">Scenariusz G12w</th><th style="text-align:right;padding:3px 6px;border-bottom:1px solid #ddd">Prognoza roczna PLN</th></tr>' +
      sensi.map(r => `<tr><td style="padding:3px 6px">${r.label}</td><td style="text-align:right;padding:3px 6px;color:${r.projected_annual > 0 ? '#27ae60' : '#e74c3c'}">${r.projected_annual > 0 ? '+' : ''}${pln(r.projected_annual, 0)}</td></tr>`).join('');
  }

  /* --- Monthly table --- */
  const tbl = document.getElementById('tariffMonthTbl');
  if (tbl) {
    const hdr = '<tr style="background:#f0f0f0"><th>Miesiąc</th><th>kWh zakup</th><th>kWh prod.</th><th>G12w PLN</th><th>Dyn PLN</th><th>Różnica</th><th>PV offset%</th></tr>';
    const rows = months.map(m => {
      const diffClr = m.diff_pln > 0 ? 'rgba(46,204,113,0.12)' : (m.diff_pln < 0 ? 'rgba(231,76,60,0.10)' : '');
      return `<tr style="background:${diffClr}"><td style="padding:4px 8px">${m.month_label}${m.is_current ? ' ●' : ''}</td><td style="text-align:right;padding:4px 8px">${kwh(m.purchased_kwh)}</td><td style="text-align:right;padding:4px 8px">${kwh(m.produced_kwh)}</td><td style="text-align:right;padding:4px 8px">${pln(m.g12w_variable_pln, 2)}</td><td style="text-align:right;padding:4px 8px">${pln(m.dynamic_variable_pln, 2)}</td><td style="text-align:right;padding:4px 8px;font-weight:600;color:${m.diff_pln > 0 ? '#27ae60' : '#e74c3c'}">${m.diff_pln > 0 ? '+' : ''}${pln(m.diff_pln, 2)}</td><td style="text-align:right;padding:4px 8px">${pct(m.pv_offset_pct)}</td></tr>`;
    }).join('');
    tbl.innerHTML = hdr + rows;
  }

  /* --- Context text --- */
  const ctxTxt = document.getElementById('tariffContextText');
  if (ctxTxt) {
    ctxTxt.innerHTML = `<p><strong>Skąd pochodzi koszt dynamiczny?</strong><br>
      Koszty dynamicznej akumulowane są co 15 minut przez automatyzację HA: <em>calkowity_koszt_1_kwh_dynamiczna × zużycie_kwartalnie</em>.
      Cena godzinowa pochodzi z integracji <code>rce_pse</code> (PSE/TGE) + składnik handlowy + dystrybucja zmienna WSD + opłaty.</p>
      <p><strong>RCEm ≠ cena zakupu</strong><br>
      RCEm (Rynkowa Cena Energii Miesięczna) to cena, po której prosument sprzedaje nadwyżki PV do sieci (net-billing).
      NIE jest to cena zakupu energii na taryfie dynamicznej. Tutaj RCEm nie jest używany.</p>
      <p>${pv.note || ''}</p>
      <p><em>Dane historyczne: od XII 2024 (kiedy uruchomiono liczniki HA). G12w: z rejestrów add-ona. Dynamiczna: z HA Statistics API.</em></p>`;
  }
}

function round2(v) { return Math.round(v * 100) / 100; }

/* ===================== INTERACTIVE RANGE ANALYSIS ===================== */

let _rangeChart = null;
let _rangeData  = null;  // last fetched {kpis, series}

(function initRangeControls() {
  const today = new Date().toISOString().slice(0, 10);
  const from  = new Date(Date.now() - 365 * 86400000).toISOString().slice(0, 10);
  document.getElementById('tariffTo').value   = today;
  document.getElementById('tariffFrom').value = from;
})();

async function fetchTariffRange() {
  const fromEl   = document.getElementById('tariffFrom');
  const toEl     = document.getElementById('tariffTo');
  const statusEl = document.getElementById('tariffRangeStatus');
  const period   = document.querySelector('input[name=tariffPeriod]:checked')?.value || 'day';
  if (!fromEl || !fromEl.value) return;
  if (statusEl) statusEl.textContent = 'Pobieranie…';
  try {
    const url = `api/tariff_stats?from=${fromEl.value}&period=${period}`;
    const r   = await fetch(url);
    if (!r.ok) { if (statusEl) statusEl.textContent = 'Błąd: ' + r.status; return; }
    _rangeData = await r.json();
    renderRangeKpis(_rangeData.kpis || {}, period);
    renderRangeChart(_rangeData.series || {});
    if (statusEl) {
      const n = (_rangeData.kpis || {}).n_periods || 0;
      statusEl.textContent = `${n} ${period === 'day' ? 'dni' : 'mies.'}`;
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = 'Błąd: ' + e.message;
  }
}

function renderRangeKpis(k, period) {
  const el = document.getElementById('tariffRangeKpis');
  if (!el) return;
  const pStr = period === 'day' ? 'dni' : 'mies.';
  const fmtDiff = v => v == null ? '—' : (v > 0 ? '+' : '') + pln(v, 2) + ' PLN';
  const cards = [
    { lbl: '% czasu Dyn. tańsza',  val: k.pct_dyn_cheaper != null ? pct(k.pct_dyn_cheaper) : '—',
      sub: `${k.n_dyn_cheaper}/${k.n_periods} ${pStr}`, cls: (k.pct_dyn_cheaper||0) >= 50 ? 'c-green' : '' },
    { lbl: 'Śr. różnica/' + pStr,  val: fmtDiff(k.avg_diff_pln),
      sub: 'mediana ' + fmtDiff(k.median_diff_pln),
      cls: (k.avg_diff_pln||0) > 0 ? 'c-green' : '' },
    { lbl: 'Skumulowane oszcz.',   val: fmtDiff(k.cumulative_savings_pln),
      sub: `G12w ${pln(k.g12w_total_pln,0)} vs Dyn ${pln(k.dyn_total_pln,0)} PLN`,
      cls: (k.cumulative_savings_pln||0) > 0 ? 'c-green' : '' },
    k.best_period  ? { lbl: '&#127942; Najlepszy dzień',  val: fmtDiff(k.best_period.diff_pln),  sub: k.best_period.date,  cls: 'c-green' } : null,
    k.worst_period ? { lbl: '&#128308; Najgorszy dzień',  val: fmtDiff(k.worst_period.diff_pln), sub: k.worst_period.date, cls: '' } : null,
    k.longest_dyn_streak > 0 ? { lbl: 'Najdłuższa passa', val: k.longest_dyn_streak + ' ' + pStr,
      sub: 'Dyn. tańsza z rzędu', cls: 'c-green' } : null,
  ].filter(Boolean);
  el.innerHTML = cards.map(c =>
    `<div class="card ${c.cls||''}" style="min-width:140px"><div class="card-lbl">${c.lbl}</div>` +
    `<div class="card-val">${c.val}</div>${c.sub ? '<div class="card-sub">' + c.sub + '</div>' : ''}</div>`
  ).join('');
}

function redrawRangeChart() {
  if (_rangeData) renderRangeChart(_rangeData.series || {});
}

function renderRangeChart(s) {
  const ctx = document.getElementById('tariffRangeChart');
  if (!ctx || !s.labels || !s.labels.length) return;
  if (_rangeChart) { _rangeChart.destroy(); _rangeChart = null; }

  const showG12w = document.getElementById('chkRangeG12w')?.checked;
  const showDyn  = document.getElementById('chkRangeDyn')?.checked;
  const showDiff = document.getElementById('chkRangeDiff')?.checked;
  const showCum  = document.getElementById('chkRangeCum')?.checked;

  const datasets = [];
  if (showG12w) datasets.push({
    label: 'G12w PLN', data: s.g12w,
    type: 'line', borderColor: '#3498db', backgroundColor: 'rgba(52,152,219,0.05)',
    borderWidth: 1.5, pointRadius: 0, fill: false, order: 2,
  });
  if (showDyn) datasets.push({
    label: 'Dynamiczna PLN', data: s.dynamic,
    type: 'line', borderColor: '#27ae60', backgroundColor: 'rgba(39,174,96,0.05)',
    borderWidth: 1.5, pointRadius: 0, fill: false, order: 2,
  });
  if (showDiff) datasets.push({
    label: 'Różnica (G12w−Dyn) PLN', data: s.diff,
    type: 'bar', backgroundColor: s.diff.map(v => v >= 0 ? 'rgba(39,174,96,0.6)' : 'rgba(231,76,60,0.5)'),
    borderWidth: 0, order: 1,
  });
  if (showCum) datasets.push({
    label: 'Skumulowana PLN', data: s.cumulative,
    type: 'line', borderColor: '#9b59b6', backgroundColor: 'transparent',
    borderWidth: 2, pointRadius: 0, fill: false, yAxisID: 'yCum', order: 2,
  });

  const hasCum   = showCum && s.cumulative && s.cumulative.length > 0;
  const maxTicks = s.labels.length > 60 ? 12 : (s.labels.length > 14 ? 8 : s.labels.length);

  _rangeChart = new Chart(ctx, {
    data: { labels: s.labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { labels: { boxWidth: 10, font: { size: 11 } } },
        tooltip: { mode: 'index', intersect: false },
      },
      scales: {
        x: { ticks: { maxTicksLimit: maxTicks, font: { size: 10 }, maxRotation: 0 } },
        y: { title: { display: true, text: 'PLN', font: { size: 11 } } },
        ...(hasCum ? { yCum: {
          position: 'right', grid: { drawOnChartArea: false },
          title: { display: true, text: 'Skum. PLN', font: { size: 10 } },
        }} : {}),
      },
    },
  });
}

/* ===================================================================== */
/* -- Taryfa tab -------------------------------------------------------- */

const _RATE_FIELD_IDS = [
  'peak_gross','offpeak_gross',
  'energy_peak_net','energy_offpeak_net',
  'dist_var_peak_net','dist_var_offpeak_net',
  'dist_jakosciowa_net','dist_oze_net','dist_kogeneracja_net',
  'fixed_abonament_net','fixed_stalysieciowy_net','fixed_mocowa_net',
];

async function loadTaryfaTab() {
  try {
    const [rCfg, rDer] = await Promise.all([
      fetch('api/tariff_config'),
      fetch('api/tariff_config/derived'),
    ]);
    if (!rCfg.ok) throw new Error('HTTP ' + rCfg.status);
    const d = await rCfg.json();
    const dd = rDer.ok ? await rDer.json() : {changes: [], existing_effective_from: []};
    _renderTaryfaBadge(d);
    _renderTaryfaTimeline(d.tariffs || [], dd.changes || [], d);
  } catch(e) {
    document.getElementById('taryfaBadge').textContent = 'Błąd ładowania: ' + e.message;
  }
}

function _renderTaryfaTimeline(tariffs, derived, status) {
  const el = document.getElementById('taryfaList');

  // Build lookup maps
  const manualMap = {};
  tariffs.forEach(t => { manualMap[t.effective_from] = t; });
  // derived is newest-first from API; index by effective_from
  const derivedMap = {};
  derived.forEach(c => { derivedMap[c.effective_from] = c; });
  // first derived entry (oldest) has changed=[] → base row
  const firstDerivedKey = derived.length ? derived[derived.length - 1].effective_from : null;

  // Union of all keys, sorted descending (newest first)
  const allKeys = [...new Set([...Object.keys(manualMap), ...Object.keys(derivedMap)])];
  allKeys.sort((a, b) => b.localeCompare(a));

  if (!allKeys.length) {
    el.innerHTML = '<p style="color:#888;font-size:13px">Brak danych — wgraj faktury lub dodaj wpis ręczny.</p>';
    return;
  }

  // Current month YYYY-MM for "future" detection
  const nowYM = new Date().toISOString().slice(0, 7);

  const rows = allKeys.map(key => {
    const manual = manualMap[key];
    const der    = derivedMap[key];
    const hasManual  = !!manual;
    const hasDerived = !!der;

    // Source chips
    const manualChip = '<span style="font-size:10px;background:#cfe2ff;color:#084298;border-radius:3px;padding:1px 6px">&#9998; ręczny</span>';
    let chips = '';
    if (hasManual && hasDerived) {
      chips = manualChip + ' <span style="font-size:10px;background:#e9ecef;color:#495057;border-radius:3px;padding:1px 6px">&#128203; potwierdzony fakturą</span>';
    } else if (hasManual) {
      const futureTag = key > nowYM
        ? ' <span style="font-size:10px;background:#fff3cd;color:#856404;border-radius:3px;padding:1px 5px">przyszły (czeka)</span>'
        : '';
      chips = manualChip + futureTag;
    } else {
      chips = '<span style="font-size:10px;background:#e9ecef;color:#495057;border-radius:3px;padding:1px 6px">&#128203; z faktury</span>';
    }

    // Active status chip
    let activeChip = '';
    if (key === status.current_effective_from) {
      activeChip = status.is_override_active
        ? ' <span style="font-size:10px;background:#fff3cd;color:#856404;border-radius:3px;padding:1px 6px">&#9889; override aktywny</span>'
        : ' <span style="font-size:10px;background:#d1e7dd;color:#0a3622;border-radius:3px;padding:1px 6px">&#9679; baseline aktywny</span>';
    }

    // Delta description
    let deltaHtml = '';
    if (hasDerived) {
      if (der.changed.length === 0) {
        deltaHtml = '<span style="font-size:11px;color:#888">punkt startowy (pierwsza faktura z danymi)</span>';
      } else {
        deltaHtml = der.changed.map(ch =>
          `<span style="font-size:11px;color:#555"><b>${_esc(ch.field)}</b>: ${ch.from}&#8594;<b>${ch.to}</b></span>`
        ).join(' &nbsp; ');
      }
    } else if (hasManual) {
      // Manual-only: show compact rates summary
      const ratesStr = Object.entries(manual.rates || {})
        .filter(([,v]) => v != null)
        .map(([k,v]) => `<span style="font-size:11px;color:#555">${_esc(k)}: <b>${v}</b></span>`)
        .join(' &nbsp; ');
      deltaHtml = ratesStr || '<span style="font-size:11px;color:#aaa">brak stawek</span>';
    }

    // Note (manual only)
    const noteHtml = hasManual && manual.note
      ? `<span style="font-size:11px;color:#666;font-style:italic">${_esc(manual.note)}</span>`
      : '';

    // Action buttons (manual entries only)
    const btns = hasManual
      ? `<button class="btn" onclick="editTaryfaEntry(${_esc(JSON.stringify(manual))})"
           style="font-size:11px;padding:2px 10px;margin-left:auto">Edytuj</button>
         <button class="btn" onclick="deleteTaryfaEntry('${_esc(key)}')"
           style="font-size:11px;padding:2px 10px;background:#dc3545">Usuń</button>`
      : '';

    const bg   = hasManual ? '#fafafa' : '#f4f6fb';
    const border = hasManual ? '#ddd' : '#dde';

    return `<div style="border:1px solid ${border};border-radius:6px;padding:9px 13px;margin-bottom:7px;background:${bg}">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px">
        <span style="font-weight:700;font-size:13px;min-width:56px">${_esc(key)}</span>
        ${chips}${activeChip}
        ${noteHtml}
        ${btns}
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:5px">${deltaHtml}</div>
    </div>`;
  }).join('');
  el.innerHTML = rows;
}

function _renderTaryfaBadge(d) {
  const el = document.getElementById('taryfaBadge');
  if (d.is_override_active) {
    el.style.background = '#fff3cd'; el.style.borderLeftColor = '#f0ad4e';
    el.innerHTML = '&#9889; <strong>Override AKTYWNY</strong> — ' + _esc(d.active_reason);
  } else if (d.current_effective_from) {
    el.style.background = '#e8f4fd'; el.style.borderLeftColor = '#2196F3';
    el.innerHTML = '&#128209; <strong>Baseline</strong> — ' + _esc(d.active_reason);
  } else {
    el.style.background = '#f8f9fa'; el.style.borderLeftColor = '#6c757d';
    el.innerHTML = '&#128204; <strong>Brak wpisów</strong> — ' + _esc(d.active_reason);
  }
}

function _esc(s) {
  if (typeof s !== 'string') return String(s ?? '');
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function editTaryfaEntry(t) {
  document.getElementById('tfEffectiveFrom').value = t.effective_from || '';
  document.getElementById('tfNote').value = t.note || '';
  _RATE_FIELD_IDS.forEach(k => {
    const el = document.getElementById('tf_' + k);
    if (el) el.value = (t.rates && t.rates[k] != null) ? t.rates[k] : '';
  });
  document.getElementById('taryfaFormTitle').textContent = 'Edytuj wpis: ' + (t.effective_from || '');
  document.getElementById('taryfaForm').scrollIntoView({behavior:'smooth'});
}

function clearTaryfaForm() {
  document.getElementById('tfEffectiveFrom').value = '';
  document.getElementById('tfNote').value = '';
  _RATE_FIELD_IDS.forEach(k => {
    const el = document.getElementById('tf_' + k);
    if (el) el.value = '';
  });
  document.getElementById('taryfaFormTitle').textContent = 'Dodaj / edytuj wpis';
  document.getElementById('taryfaMsg').textContent = '';
}

document.getElementById('taryfaForm').addEventListener('submit', async function(e) {
  e.preventDefault();
  const ef = document.getElementById('tfEffectiveFrom').value.trim();
  if (!ef) { document.getElementById('taryfaMsg').textContent = '⚠ Podaj datę obowiązywania.'; return; }
  const rates = {};
  _RATE_FIELD_IDS.forEach(k => {
    const el = document.getElementById('tf_' + k);
    if (el && el.value !== '') rates[k] = parseFloat(el.value);
  });
  const payload = { effective_from: ef, note: document.getElementById('tfNote').value.trim(), rates };
  const msg = document.getElementById('taryfaMsg');
  msg.textContent = 'Zapisywanie...';
  try {
    const r = await fetch('api/tariff_config', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload) });
    const d = await r.json();
    if (!d.ok) { msg.textContent = '⚠ Błąd: ' + (d.error || r.status); return; }
    msg.style.color = '#28a745';
    msg.textContent = '✓ Zapisano. Publikacja MQTT przy najbliższym pollu (≤30 min).';
    clearTaryfaForm();
    await loadTaryfaTab();
  } catch(ex) {
    msg.style.color = '#dc3545';
    msg.textContent = '⚠ Błąd: ' + ex.message;
  }
});

async function deleteTaryfaEntry(ef) {
  if (!confirm('Usunąć wpis ' + ef + '?')) return;
  try {
    const r = await fetch('api/tariff_config/delete', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({effective_from: ef}) });
    const d = await r.json();
    if (!d.ok) { alert('Błąd: ' + (d.error || r.status)); return; }
    await loadTaryfaTab();
  } catch(ex) { alert('Błąd: ' + ex.message); }
}

/* ===================================================================== */

loadData();
setInterval(loadData, 60000);

</script>
</body>
</html>"""
