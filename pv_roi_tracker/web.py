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

from .models import MonthlyRecord
from .roi import RoiResult

app = Flask(__name__)
log = logging.getLogger(__name__)

_lock = threading.Lock()
_rcem_override_callback = None
_historic_patch_callback = None
_invoice_reconcile_callback = None
_invoice_remove_callback = None
_invoice_train_callback = None
_invoice_path = None
_layouts_path = None
_tariff_peak    = 1.23
_tariff_offpeak = 0.63


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


def set_tariff_config(peak: float, offpeak: float) -> None:
    global _tariff_peak, _tariff_offpeak
    _tariff_peak, _tariff_offpeak = peak, offpeak


_state: dict = {
    'result': None,
    'records': [],
    'rcem_price': None,
    'month_closed': False,
    'rcem_scrape_status': None,
    'updated_at': None,
}

_MONTHS_PL = ['', 'Sty', 'Lut', 'Mar', 'Kwi', 'Maj', 'Cze',
               'Lip', 'Sie', 'Wrz', 'Paź', 'Lis', 'Gru']


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


def _month_label(year: int, month: int) -> str:
    return f"{year}-{_MONTHS_PL[month]}"


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

    remaining = result.remaining_to_recover
    cumulative = result.total_return
    while remaining > 0 and len(rows) < 120:
        cumulative += avg
        remaining -= avg
        rows.append({
            'month_label': _month_label(cursor.year, cursor.month),
            'projected_savings': round(avg, 2),
            'cumulative_return': round(cumulative, 2),
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
            'feedin_corrections': corrections.get(month_key) or None,
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
        },
        'records': records_out,
        'predictions': _build_predictions(result),
        'invoices': _build_invoices_data(records),
        'tariff_drift': _build_tariff_drift(),
        'layouts_summary': _build_layouts_summary(),
    })


def _build_invoices_data(records):
    if _invoice_path is None:
        return []
    try:
        from . import invoice_store as _istore
        stored = _istore.load(_invoice_path)
    except Exception:
        return []
    out = []
    for key, inv in sorted(stored.items(), key=lambda kv: kv[0]):
        try:
            # Synthetic keys like "unparsed-<ts>-<name>" — month unknown yet
            is_stub = key.startswith('unparsed-')
            diff_imp = diff_exp = None
            if not is_stub:
                try:
                    iy, im = int(key[:4]), int(key[5:])
                    hist_rec = next((r for r in records if r.year == iy and r.month == im), None)
                    if hist_rec:
                        if hist_rec.purchased_kwh is not None and inv.get('imported_kwh') is not None:
                            diff_imp = round(inv['imported_kwh'] - hist_rec.purchased_kwh, 2)
                        if hist_rec.exported_kwh is not None and inv.get('exported_kwh') is not None:
                            diff_exp = round(inv['exported_kwh'] - hist_rec.exported_kwh, 2)
                except (ValueError, TypeError):
                    pass
            inv_warnings = inv.get('warnings', [])
            out.append({
                'key': key,
                'month': key if not is_stub else None,
                'is_stub': is_stub,
                'needs_training': inv.get('needs_training', False),
                'parse_error': inv.get('parse_error'),
                'has_raw_text': bool(inv.get('raw_text')),
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
            })
        except Exception:
            pass
    return out


_FIXED_NET_EXPECTED = 39.47  # per energy_simulation.yaml 2026 Tauron tariff


def _build_tariff_drift():
    if _invoice_path is None:
        return None
    try:
        from . import invoice_store as _istore
        stored = _istore.load(_invoice_path)
        if not stored:
            return None
        latest = stored[max(stored)]
        drift = {}
        pk = latest.get('peak_gross')
        op = latest.get('offpeak_gross')
        if pk is not None and abs(pk - _tariff_peak) > 0.02:
            drift['peak'] = {'configured': _tariff_peak, 'invoice': round(pk, 4)}
        if op is not None and abs(op - _tariff_offpeak) > 0.02:
            drift['offpeak'] = {'configured': _tariff_offpeak, 'invoice': round(op, 4)}
        ft = latest.get('fixed_total_net')
        if ft is not None and abs(ft - _FIXED_NET_EXPECTED) > 0.50:
            drift['fixed_net'] = {'expected': _FIXED_NET_EXPECTED, 'invoice': round(ft, 2)}
        return drift or None
    except Exception:
        return None


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
    raw_texts: dict = {}  # fname → raw text (for warnings-aware storage)
    for f in files:
        fname = f.filename or 'upload'
        pdf_bytes = f.read()
        try:
            data = parse_invoice(pdf_bytes)
            data._filename = fname  # type: ignore[attr-defined]
            parsed_list.append(data)
            if data.warnings:
                # Store raw text so Train can work later
                debug = parse_invoice_debug(pdf_bytes)
                raw_texts[fname] = debug.get('text', '')
            results.append({'filename': fname, 'month': f'{data.year}-{data.month:02d}',
                             'imported_kwh': data.imported_kwh, 'exported_kwh': data.exported_kwh,
                             'peak_gross': data.peak_gross, 'offpeak_gross': data.offpeak_gross,
                             'amount_due': data.amount_due_pln, 'deposit_used': data.deposit_used_pln,
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
                    stub_key = _is.upsert_stub(fname, raw_text, error_msg, _invoice_path)
                except Exception:
                    log.exception('Failed to store stub for %s', fname)
            results.append({'filename': fname, 'ok': False, 'needs_training': True,
                             'error': error_msg, 'stub_key': stub_key})
        except Exception as exc:
            log.exception('Invoice parse error: %s', fname)
            results.append({'filename': fname, 'ok': False, 'error': str(exc)})
    if parsed_list:
        try:
            _invoice_reconcile_callback(parsed_list, raw_texts)
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
        if raw_text:
            try:
                from .invoice_parser import _parse_text, InvoiceParseError
                parsed = _parse_text(raw_text)
                from dataclasses import asdict
                fields = asdict(parsed)
            except Exception:
                fields = {}
        # Fall back to stored values for any field not parsed
        for fk in ['year', 'month', 'imported_kwh', 'exported_kwh', 'imported_kwh_peak',
                   'imported_kwh_offpeak', 'energy_peak_net', 'energy_offpeak_net',
                   'amount_due_pln', 'avg_price_pln_kwh', 'deposit_current_pln',
                   'deposit_previous_pln', 'deposit_used_pln', 'fixed_mocowa_net',
                   'fixed_abonament_net', 'fixed_stalysieciowy_net', 'invoice_number']:
            if fields.get(fk) is None and rec.get(fk) is not None:
                fields[fk] = rec[fk]
        return jsonify({'ok': True, 'key': key, 'raw_text': raw_text,
                        'filename': rec.get('filename', ''), 'fields': fields})
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
@media (max-width: 900px) { .charts { grid-template-columns: 1fr; } }
.chart-wrap    { background: var(--card); border-radius: var(--radius); padding: 16px; box-shadow: var(--shadow); height: 280px; position: relative; }
.chart-wrap.sm { height: 200px; }
.chart-wrap h3 { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 8px; }

/* -- Tabs -- */
.tabs { display: flex; gap: 3px; }
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

</style>
</head>
<body>
<header>
  <h1>&#9728;&#65039; PV ROI Tracker</h1>
  <a href="api/export/csv" class="csv-btn" download>&#8595; Eksportuj CSV</a>
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
    </div>
    <div class="tab-panel">
      <div id="tab-hist">
        <div class="tbl-wrap"><table id="histTbl"></table></div>
        <div class="tbl-foot" id="histFoot"></div>
      </div>
      <div id="tab-pred" style="display:none">
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
          <div class="charts2">
            <div class="chart-wrap" id="prodRankWrap" style="height:720px">
              <h3>Produkcja miesięczna — ranking (najlepsza → najgorsza)</h3>
              <canvas id="prodRankChart"></canvas>
            </div>
          </div>
        </div>
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
        <!-- Deposit trend chart -->
        <div style="margin-bottom:16px">
          <h4 style="margin:0 0 8px;font-size:13px;font-weight:600">Saldo depozytu prosumenckiego (zł)</h4>
          <div class="chart-wrap" style="max-width:680px;height:200px">
            <canvas id="depositChart"></canvas>
          </div>
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
let _lineChart = null, _barChart = null, _rcemChart = null, _autarkiaChart = null, _prodChart = null, _arbitrageChart = null, _netCostChart = null, _priceSpreadChart = null, _yieldChart = null, _energyBalChart = null, _yearCompChart = null, _prodRankChart = null, _depositChart = null;

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
  ['hist','pred','years','charts','invoices'].forEach(t => {
    document.getElementById('tab-' + t).style.display = (t === name) ? '' : 'none';
  });
  document.querySelectorAll('.tab-btn').forEach((b, i) =>
    b.classList.toggle('active', ['hist','pred','years','charts','invoices'][i] === name)
  );
  if (name === 'charts') {
    [_rcemChart, _autarkiaChart, _prodChart, _arbitrageChart, _netCostChart,
     _priceSpreadChart, _yieldChart, _energyBalChart, _yearCompChart, _prodRankChart].forEach(c => c && c.resize());
  }
  if (name === 'invoices' && _depositChart) _depositChart.resize();
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
function renderProdRankChart(records) {
  const PL_M = ['Sty','Lut','Mar','Kwi','Maj','Cze','Lip','Sie','Wrz','Paź','Lis','Gru'];
  const seasonColor = [
    'rgba(100,116,139,0.70)', // Sty
    'rgba(100,116,139,0.70)', // Lut
    'rgba(37,99,235,0.65)',   // Mar
    'rgba(37,99,235,0.75)',   // Kwi
    'rgba(234,179,8,0.80)',   // Maj
    'rgba(234,88,12,0.85)',   // Cze
    'rgba(234,88,12,0.85)',   // Lip
    'rgba(234,179,8,0.80)',   // Sie
    'rgba(37,99,235,0.70)',   // Wrz
    'rgba(37,99,235,0.60)',   // Paź
    'rgba(100,116,139,0.65)', // Lis
    'rgba(100,116,139,0.60)', // Gru
  ];

  const ranked = records
    .filter(r => r.produced_kwh != null && !r.is_current)
    .sort((a, b) => b.produced_kwh - a.produced_kwh);

  const labels = ranked.map(r => r.month_label);
  const values = ranked.map(r => Math.round(r.produced_kwh * 10) / 10);
  const colors = ranked.map(r => {
    const mi = PL_M.indexOf(r.month_label.slice(5));
    return mi >= 0 ? seasonColor[mi] : 'rgba(37,99,235,0.75)';
  });

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
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: { label: c => 'Produkcja: ' + Number(c.raw).toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' kWh' } },
      },
      scales: {
        x: { beginAtZero: true, ticks: { callback: v => v.toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' kWh', font: { size: 10 } } },
        y: { ticks: { font: { size: 10 } } },
      },
    },
  });
}

/* -- History table -- */
function renderHistTable(records, monthClosed, invoices) {
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
    if (!yearTotals[y]) yearTotals[y] = {produced: 0, exported: 0, sc: 0, self_sav: 0, feedin: 0, arbitrage: 0, total: 0, purchase: 0, net_grid: 0, consumed: 0, peak_kw: 0, offpeak_kw: 0, peak_kw_count: 0};
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
  }

  function yearSummaryRow(y) {
    const t = yearTotals[y];
    const suff = t.consumed > 0 ? pct(t.sc / t.consumed * 100) : '—';
    const pkCell  = t.peak_kw_count > 0 ? kwh(t.peak_kw)    : '—';
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

    const pkCell  = r.purchased_kwh_peak    != null ? kwh(r.purchased_kwh_peak)    : '—';
    const opkCell = r.purchased_kwh_offpeak != null ? kwh(r.purchased_kwh_offpeak) : '—';

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
    };
    const t = yearMap[y];
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

  const head = '<thead><tr>' +
    '<th>Rok</th>' +
    '<th>Produkcja</th>' +
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
    renderInvoicesTab(d.invoices || [], d.tariff_drift, d.records, d.layouts_summary);
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
function renderInvoicesTab(invoices, tariffDrift, records, layoutsSummary) {
  _renderInvKpiCards(invoices, tariffDrift, records);
  _renderDriftBanner(tariffDrift);
  _renderCoverageGrid(invoices, records);
  renderDepositChart(invoices);
  _renderInvoiceTable(invoices);
  _renderLayoutsPanel(layoutsSummary);
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
    banner.innerHTML = '⚠ Wykryto zmianę stawek: ' + msgs.join('; ') +
      '. Zaktualizuj <b>tariff_peak_price / tariff_offpeak_price</b> w konfiguracji dodatku (jeśli dotyczy).';
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
    const actionBtns =
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
    '</tr>';
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

/* ── Train modal ───────────────────────────────────────────────────────────── */
let _trainModal = null;

function _ensureTrainModal() {
  if (_trainModal) return _trainModal;
  const overlay = document.createElement('div');
  overlay.id = 'trainOverlay';
  overlay.style.cssText = 'display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:1000;overflow-y:auto;padding:24px';
  overlay.innerHTML =
    '<div style="background:var(--card);border-radius:8px;max-width:900px;margin:0 auto;padding:24px;position:relative">' +
      '<button onclick="closeTrainModal()" style="position:absolute;top:12px;right:14px;font-size:18px;background:none;border:none;cursor:pointer;color:var(--muted)">✕</button>' +
      '<h3 style="margin:0 0 14px;font-size:15px">Trenuj parser — uzupełnij dane faktury</h3>' +
      '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">' +
        '<div>' +
          '<div style="font-size:12px;font-weight:600;margin-bottom:4px;color:var(--muted)">Surowy tekst PDF</div>' +
          '<pre id="trainRawText" style="font-size:10px;white-space:pre-wrap;word-break:break-all;background:var(--bg);padding:10px;border-radius:4px;height:400px;overflow-y:auto;margin:0"></pre>' +
        '</div>' +
        '<div id="trainFieldsWrap" style="overflow-y:auto;height:440px"></div>' +
      '</div>' +
      '<div style="margin-top:14px;display:flex;gap:10px;align-items:center">' +
        '<button id="trainSaveBtn" onclick="submitTrain()" style="background:#3182ce;color:#fff;border:none;padding:8px 18px;border-radius:4px;cursor:pointer;font-size:13px">Zapisz i naucz parser</button>' +
        '<button onclick="closeTrainModal()" style="background:var(--bg);border:1px solid var(--border);padding:8px 14px;border-radius:4px;cursor:pointer;font-size:13px">Anuluj</button>' +
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
  rawEl.textContent = 'Ładowanie…';
  fieldsEl.innerHTML = '';
  msg.textContent = '';
  _trainCurrentKey = key;
  _trainCurrentData = {};
  overlay.style.display = '';

  try {
    const r = await fetch('api/invoice/train_form?key=' + encodeURIComponent(key));
    const d = await r.json();
    if (!d.ok) { rawEl.textContent = 'Błąd: ' + (d.error||'nieznany'); return; }
    rawEl.textContent = d.raw_text || '(brak tekstu PDF — wpisz wartości ręcznie)';
    _trainCurrentData = d;
    const f = d.fields || {};

    const FIELDS = [
      ['year',             'Rok (np. 2025)',           'number'],
      ['month',            'Miesiąc (1-12)',            'number'],
      ['imported_kwh',     'Pobrano z sieci (kWh)',     'number'],
      ['exported_kwh',     'Wprowadzono do sieci (kWh)','number'],
      ['imported_kwh_peak','Import szczyt (kWh)',       'number'],
      ['imported_kwh_offpeak','Import poza szczytem (kWh)','number'],
      ['energy_peak_net',  'Energia szczyt net (zł/kWh)','number'],
      ['energy_offpeak_net','Energia poza net (zł/kWh)', 'number'],
      ['amount_due_pln',   'Do zapłaty (zł)',           'number'],
      ['avg_price_pln_kwh','Śr. cena (zł/kWh)',         'number'],
      ['deposit_current_pln','Depozyt bieżący (zł)',    'number'],
      ['deposit_previous_pln','Depozyt poprzednie (zł)','number'],
      ['deposit_used_pln', 'Depozyt rozliczony (zł)',   'number'],
      ['fixed_mocowa_net', 'Opł. mocowa net (zł/mc)',   'number'],
      ['fixed_abonament_net','Abonament net (zł/mc)',   'number'],
      ['fixed_stalysieciowy_net','Skł. stały siec. net (zł/mc)','number'],
      ['invoice_number',   'Nr faktury',                'text'],
    ];

    let html = '<div style="display:flex;flex-direction:column;gap:8px">';
    FIELDS.forEach(([fk, label, type]) => {
      const val = f[fk] != null ? f[fk] : '';
      html += '<label style="font-size:12px;display:flex;flex-direction:column;gap:2px">' +
        '<span style="color:var(--muted)">' + label + '</span>' +
        '<input id="tf_' + fk + '" type="' + type + '" step="any" value="' + String(val).replace(/"/g,'&quot;') + '" ' +
          'style="padding:4px 6px;border:1px solid var(--border);border-radius:3px;font-size:12px">' +
      '</label>';
    });
    html += '</div>';
    fieldsEl.innerHTML = html;
  } catch(e) {
    rawEl.textContent = 'Błąd połączenia: ' + e.message;
  }
}

function closeTrainModal() {
  const overlay = document.getElementById('trainOverlay');
  if (overlay) overlay.style.display = 'none';
}

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

loadData();
setInterval(loadData, 60000);

</script>
</body>
</html>"""
