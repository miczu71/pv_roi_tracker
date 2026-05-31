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
_invoice_path = None
_tariff_peak   = 1.23
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

    # ── Invoice data for UI ───────────────────────────────────────────────────
    invoices_out = []
    tariff_drift = None
    fixed_fee_drift = None
    if _invoice_path is not None:
        from . import invoice_store as _istore
        stored = _istore.load(_invoice_path)
        for mk, inv in sorted(stored.items()):
            # kWh diff vs what's in historic records for that month
            try:
                iy, im = int(mk[:4]), int(mk[5:])
                hist_rec = next((r for r in records if r.year == iy and r.month == im), None)
                diff_imp = None
                diff_exp = None
                if hist_rec:
                    if hist_rec.purchased_kwh is not None and inv.get('imported_kwh') is not None:
                        diff_imp = round(inv['imported_kwh'] - hist_rec.purchased_kwh, 2)
                    if hist_rec.exported_kwh is not None and inv.get('exported_kwh') is not None:
                        diff_exp = round(inv['exported_kwh'] - hist_rec.exported_kwh, 2)
                invoices_out.append({
                    'month': mk,
                    'amount_due': inv.get('amount_due_pln'),
                    'deposit_previous': inv.get('deposit_previous_pln'),
                    'deposit_used': inv.get('deposit_used_pln'),
                    'imported_kwh': inv.get('imported_kwh'),
                    'exported_kwh': inv.get('exported_kwh'),
                    'diff_imported_kwh': diff_imp,
                    'diff_exported_kwh': diff_exp,
                    'peak_gross': inv.get('peak_gross'),
                    'offpeak_gross': inv.get('offpeak_gross'),
                    'fixed_total_net': inv.get('fixed_total_net'),
                    'avg_price': inv.get('avg_price_pln_kwh'),
                    'reconciled': inv.get('reconciled', False),
                    'invoice_number': inv.get('invoice_number'),
                })
            except Exception:
                pass
        # Tariff drift: compare latest invoice's gross rates vs config
        if stored:
            latest = stored[max(stored)]
            pk = latest.get('peak_gross')
            op = latest.get('offpeak_gross')
            if pk is not None and abs(pk - _tariff_peak) > 0.02:
                tariff_drift = {'peak': {'configured': _tariff_peak, 'invoice': round(pk, 4)}}
            if op is not None and abs(op - _tariff_offpeak) > 0.02:
                tariff_drift = tariff_drift or {}
                tariff_drift['offpeak'] = {'configured': _tariff_offpeak, 'invoice': round(op, 4)}
            # Fixed fee drift vs HA energy_simulation.yaml known value (39.47 net from 2026 tariff)
            _KNOWN_FIXED_NET = 39.47
            fn = latest.get('fixed_total_net')
            if fn is not None and abs(fn - _KNOWN_FIXED_NET) > 0.5:
                fixed_fee_drift = {'yaml_fixed_net': _KNOWN_FIXED_NET, 'invoice_fixed_net': round(fn, 2)}

    return jsonify({
        'status': 'ok',
        'updated_at': updated_at,
        'invoices': invoices_out,
        'tariff_drift': tariff_drift,
        'fixed_fee_drift': fixed_fee_drift,
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
    })


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


@app.route('/api/invoice/upload', methods=['POST'])
def invoice_upload():
    if _invoice_reconcile_callback is None:
        return jsonify({'ok': False, 'error': 'not initialized'}), 503

    files = request.files.getlist('files') or request.files.getlist('file')
    if not files:
        return jsonify({'ok': False, 'error': 'no file(s) attached (field name: files)'}), 400

    from .invoice_parser import parse_invoice, InvoiceParseError

    results = []
    parsed_list = []
    for f in files:
        fname = f.filename or 'upload'
        try:
            pdf_bytes = f.read()
            data = parse_invoice(pdf_bytes)
            data._filename = fname  # type: ignore[attr-defined]  # carried to callback
            parsed_list.append(data)
            results.append({
                'filename': fname,
                'month': f'{data.year}-{data.month:02d}',
                'imported_kwh': data.imported_kwh,
                'exported_kwh': data.exported_kwh,
                'peak_gross': data.peak_gross,
                'offpeak_gross': data.offpeak_gross,
                'amount_due': data.amount_due_pln,
                'deposit_previous': data.deposit_previous_pln,
                'deposit_used': data.deposit_used_pln,
                'fixed_total_net': data.fixed_total_net,
                'ok': True,
            })
        except InvoiceParseError as exc:
            results.append({'filename': fname, 'ok': False, 'error': str(exc)})
        except Exception as exc:
            log.exception('Invoice parse error: %s', fname)
            results.append({'filename': fname, 'ok': False, 'error': str(exc)})

    if parsed_list:
        try:
            _invoice_reconcile_callback(parsed_list)
        except Exception as exc:
            log.exception('Invoice reconcile callback failed')
            return jsonify({'ok': False, 'error': str(exc), 'results': results}), 500

    return jsonify({'ok': True, 'results': results})


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
    </div>
    <!-- ── Faktury Tauron ─────────────────────────────────────────────────── -->
    <div class="override-wrap" style="margin-top:18px" id="invoiceSection">
      <h3>Faktury Tauron</h3>
      <div id="tariffDriftBanner" style="display:none;margin-bottom:8px;padding:8px 12px;background:#fff3cd;border-left:4px solid #f0ad4e;border-radius:4px;font-size:13px"></div>
      <div id="fixedFeeDriftBanner" style="display:none;margin-bottom:8px;padding:8px 12px;background:#cce5ff;border-left:4px solid #004085;border-radius:4px;font-size:13px"></div>
      <div class="override-form" style="align-items:flex-start;gap:12px">
        <label style="display:flex;flex-direction:column;gap:4px;font-size:13px">
          Wgraj faktury PDF (jedna lub wiele):
          <input type="file" id="invoiceFiles" accept=".pdf" multiple style="margin-top:4px">
        </label>
        <button onclick="uploadInvoices()" style="align-self:flex-end">Wgraj i uzgodnij</button>
        <span id="invoiceMsg" style="align-self:flex-end;font-size:12px"></span>
      </div>
      <div id="invoiceTableWrap" style="margin-top:12px;overflow-x:auto"></div>
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
let _lineChart = null, _barChart = null, _rcemChart = null, _autarkiaChart = null, _prodChart = null, _arbitrageChart = null, _netCostChart = null, _priceSpreadChart = null, _yieldChart = null, _energyBalChart = null, _yearCompChart = null, _prodRankChart = null;

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
  ['hist','pred','years','charts'].forEach(t => {
    document.getElementById('tab-' + t).style.display = (t === name) ? '' : 'none';
  });
  document.querySelectorAll('.tab-btn').forEach((b, i) =>
    b.classList.toggle('active', ['hist','pred','years','charts'][i] === name)
  );
  if (name === 'charts') {
    [_rcemChart, _autarkiaChart, _prodChart, _arbitrageChart, _netCostChart,
     _priceSpreadChart, _yieldChart, _energyBalChart, _yearCompChart, _prodRankChart].forEach(c => c && c.resize());
  }
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
function renderHistTable(records, monthClosed) {
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

    const monthLabel = r.month_label;

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

/* -- Invoice upload -- */
async function uploadInvoices() {
  const input = document.getElementById('invoiceFiles');
  const msg   = document.getElementById('invoiceMsg');
  msg.className = ''; msg.textContent = 'Wgrywanie…';
  if (!input.files || input.files.length === 0) {
    msg.className = 'err'; msg.textContent = 'Wybierz przynajmniej jeden plik PDF'; return;
  }
  const fd = new FormData();
  for (const f of input.files) fd.append('files', f);
  try {
    const r = await fetch('api/invoice/upload', {method: 'POST', body: fd});
    const d = await r.json();
    if (d.ok) {
      const ok  = (d.results || []).filter(x => x.ok);
      const err = (d.results || []).filter(x => !x.ok);
      let txt = 'Wgrano: ' + ok.map(x => x.month).join(', ');
      if (err.length) txt += '; błąd: ' + err.map(x => x.filename + ': ' + x.error).join('; ');
      msg.className = err.length ? 'err' : 'ok';
      msg.textContent = txt;
      setTimeout(loadData, 1500);
    } else {
      msg.className = 'err'; msg.textContent = 'Błąd: ' + (d.error || 'nieznany');
    }
  } catch(e) {
    msg.className = 'err'; msg.textContent = 'Błąd połączenia';
  }
}

function renderInvoiceTable(invoices, tariffDrift, fixedFeeDrift) {
  // Drift banners
  const tBanner = document.getElementById('tariffDriftBanner');
  const fBanner = document.getElementById('fixedFeeDriftBanner');
  if (tBanner) {
    if (tariffDrift) {
      let msgs = [];
      if (tariffDrift.peak)    msgs.push('szczyt: skonfig. ' + tariffDrift.peak.configured + ' → faktura ' + tariffDrift.peak.invoice + ' zł/kWh');
      if (tariffDrift.offpeak) msgs.push('poza szczytem: skonfig. ' + tariffDrift.offpeak.configured + ' → faktura ' + tariffDrift.offpeak.invoice + ' zł/kWh');
      tBanner.textContent = '⚠ Zmiana stawek taryfowych: ' + msgs.join('; ') + '. Zaktualizuj tariff_peak_price / tariff_offpeak_price w konfiguracji dodatku.';
      tBanner.style.display = '';
    } else tBanner.style.display = 'none';
  }
  if (fBanner) {
    if (fixedFeeDrift) {
      fBanner.textContent = 'ℹ Opłaty stałe dystrybucji zmieniły się: YAML = ' + fixedFeeDrift.yaml_fixed_net + ' zł/mc net → faktura = ' + fixedFeeDrift.invoice_fixed_net + ' zł/mc net. Zaktualizuj fixed_net w packages/energy_simulation.yaml.';
      fBanner.style.display = '';
    } else fBanner.style.display = 'none';
  }

  const wrap = document.getElementById('invoiceTableWrap');
  if (!wrap) return;
  if (!invoices || invoices.length === 0) {
    wrap.innerHTML = '<p style="color:var(--muted);font-size:13px">Brak wgranych faktur. Wgraj PDF, aby uzgodnić dane rozliczeniowe.</p>';
    return;
  }
  const rows = [...invoices].sort((a,b) => b.month.localeCompare(a.month)).map(inv => {
    const ok = inv.reconciled ? '✅' : '⏳';
    const diffImp = inv.diff_imported_kwh != null ? (inv.diff_imported_kwh > 0 ? '+' : '') + inv.diff_imported_kwh.toFixed(0) : '—';
    const diffExp = inv.diff_exported_kwh != null ? (inv.diff_exported_kwh > 0 ? '+' : '') + inv.diff_exported_kwh.toFixed(0) : '—';
    const depStr = inv.deposit_used != null ? fmt(inv.deposit_used, 2, 'zł') : '—';
    return '<tr>' +
      '<td>' + inv.month + '</td>' +
      '<td>' + (inv.invoice_number || '—') + '</td>' +
      '<td style="text-align:right">' + (inv.imported_kwh != null ? inv.imported_kwh.toFixed(0) : '—') + ' kWh</td>' +
      '<td style="text-align:right">' + (inv.exported_kwh != null ? inv.exported_kwh.toFixed(0) : '—') + ' kWh</td>' +
      '<td style="text-align:right;color:' + (inv.diff_imported_kwh != null && Math.abs(inv.diff_imported_kwh) > 5 ? '#c0392b' : 'inherit') + '">' + diffImp + '</td>' +
      '<td style="text-align:right;color:' + (inv.diff_exported_kwh != null && Math.abs(inv.diff_exported_kwh) > 5 ? '#c0392b' : 'inherit') + '">' + diffExp + '</td>' +
      '<td style="text-align:right">' + (inv.amount_due != null ? fmt(inv.amount_due, 2, 'zł') : '—') + '</td>' +
      '<td style="text-align:right">' + depStr + '</td>' +
      '<td style="text-align:right;color:var(--muted)">' + (inv.peak_gross != null ? inv.peak_gross.toFixed(4) : '—') + '</td>' +
      '<td style="text-align:right;color:var(--muted)">' + (inv.offpeak_gross != null ? inv.offpeak_gross.toFixed(4) : '—') + '</td>' +
      '<td style="text-align:center">' + ok + '</td>' +
    '</tr>';
  }).join('');
  wrap.innerHTML =
    '<table style="width:100%;border-collapse:collapse;font-size:12px">' +
    '<thead><tr style="border-bottom:2px solid var(--border)">' +
      '<th style="text-align:left">Miesiąc</th>' +
      '<th style="text-align:left">Nr faktury</th>' +
      '<th style="text-align:right">Pobrano kWh</th>' +
      '<th style="text-align:right">Oddano kWh</th>' +
      '<th style="text-align:right" title="różnica faktura vs. sensor">Δ pobór</th>' +
      '<th style="text-align:right" title="różnica faktura vs. sensor">Δ oddanie</th>' +
      '<th style="text-align:right">Do zapłaty</th>' +
      '<th style="text-align:right">Depozyt użyty</th>' +
      '<th style="text-align:right">Szczyt zł/kWh</th>' +
      '<th style="text-align:right">Poza szczytem</th>' +
      '<th style="text-align:center">Uzgod.</th>' +
    '</tr></thead>' +
    '<tbody>' + rows + '</tbody></table>';
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
    renderHistTable([...d.records].reverse(), d.summary.month_closed);
    renderPredTable(d.predictions, d.summary, d.summary.avg_window);
    renderYearsTable(d.records, systemKwp);
    renderInvoiceTable(d.invoices || [], d.tariff_drift, d.fixed_fee_drift);
  } catch (e) {
    document.getElementById('updated').textContent = 'Blad polaczenia';
    console.error(e);
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
