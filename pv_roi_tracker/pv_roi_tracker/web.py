"""Flask web UI — serves the ROI dashboard on the add-on ingress port."""
from __future__ import annotations

import calendar
import csv
import io
import logging
import re
import threading
from datetime import date, datetime
from math import ceil
from typing import Optional

from dateutil.relativedelta import relativedelta
from flask import Flask, Response, jsonify, request

from .models import MonthlyRecord
from .roi import RoiResult

app = Flask(__name__)
log = logging.getLogger(__name__)

_lock = threading.Lock()
_rcem_override_callback = None


def set_rcem_override_callback(fn) -> None:
    global _rcem_override_callback
    _rcem_override_callback = fn



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


def _build_sensitivity(result: RoiResult) -> dict:
    avg = result.monthly_avg_savings
    if not avg or avg <= 0:
        return {}
    today = date.today()
    out: dict = {}
    for label, factor in [('pessimistic', 0.85), ('base', 1.0), ('optimistic', 1.15)]:
        adj = avg * factor
        if result.remaining_to_recover <= 0:
            out[label] = {'avg': round(adj, 2), 'months': 0, 'payback_date': None}
        else:
            months = result.remaining_to_recover / adj
            out[label] = {
                'avg': round(adj, 2),
                'months': round(months, 1),
                'payback_date': (today + relativedelta(months=ceil(months))).isoformat(),
            }
    return out


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
        month_savings = (r.self_consumed_savings_pln or 0.0) + (r.feedin_revenue_pln or 0.0)
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
        },
        'records': records_out,
        'predictions': _build_predictions(result),
        'sensitivity': _build_sensitivity(result),
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
    <div class="charts2">
      <div class="chart-wrap sm">
        <h3>Historia ceny RCEm (zl/kWh)</h3>
        <canvas id="rcemChart"></canvas>
      </div>
    </div>
    <div class="tabs">
      <button class="tab-btn active" onclick="showTab('hist')">Historia miesieczna</button>
      <button class="tab-btn"        onclick="showTab('pred')">Prognoza splaty</button>
      <button class="tab-btn"        onclick="showTab('years')">Podsumowanie roczne</button>
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
let _lineChart = null, _barChart = null, _rcemChart = null;

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
  ['hist','pred','years'].forEach(t => {
    document.getElementById('tab-' + t).style.display = (t === name) ? '' : 'none';
  });
  document.querySelectorAll('.tab-btn').forEach((b, i) =>
    b.classList.toggle('active', ['hist','pred','years'][i] === name)
  );
}

/* -- Summary cards -- */
function renderCards(s) {
  const roi = s.roi_pct;
  const cards = [
    { lbl: 'ROI',               val: pct(roi),                    cls: roi >= 100 ? 'c-green' : 'c-blue' },
    { lbl: 'Laczny zwrot',      val: pln(s.total_return),         sub: 'subsydium ' + pln(s.subsidy) + ' + oszcz. ' + pln(s.total_savings) },
    { lbl: 'Oszczednosci',      val: pln(s.total_savings),        sub: 'autokons. ' + pln(s.self_consumption_savings) + ' / sprzedaz ' + pln(s.feedin_revenue) },
    { lbl: 'Pozostalo',         val: pln(s.remaining_to_recover), sub: 'inwestycja brutto ' + pln(s.gross_investment) },
    { lbl: 'Srednia mies.',     val: pln(s.monthly_avg_savings),  sub: 'ost. ' + s.avg_window + ' mies.' },
    { lbl: 'Splata',            val: s.payback_date || '—',  sub: s.years_to_payback != null ? 'za ' + num(s.years_to_payback, 1) + ' lat' : '' },
    { lbl: 'Produkcja lacznie', val: kwh(s.total_produced_kwh),   sub: 'uzysk ' + num(s.specific_yield, 0) + ' kWh/kWp' },
    s.best_month  ? { lbl: 'Najlepszy miesiac',  val: pln(s.best_month.savings),  sub: s.best_month.label,  cls: 'c-green' } : null,
    s.worst_month ? { lbl: 'Najslabszy miesiac', val: pln(s.worst_month.savings), sub: s.worst_month.label } : null,
    s.solcast_projected_kwh != null ? { lbl: 'Prognoza miesiaca', val: kwh(s.solcast_projected_kwh), sub: 'produkcja + Solcast 7 dni' } : null,
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
function renderLineChart(records, predictions, gross) {
  const histLbls = records.map(r => r.month_label);
  const histVals = records.map(r => r.cumulative_return);
  const predLbls = predictions.map(p => p.month_label);
  const predVals = predictions.map(p => p.cumulative_return || (p.net_profit != null ? gross + p.net_profit : null));

  const allLbls = [...histLbls, ...predLbls];
  const target  = allLbls.map(() => gross);

  const hDs = [...histVals, ...predLbls.map(() => null)];
  const pDs = [...histLbls.map(() => null)];
  if (histVals.length) pDs[histVals.length - 1] = histVals[histVals.length - 1];
  pDs.push(...predVals);

  const ctx = document.getElementById('lineChart').getContext('2d');
  if (_lineChart) _lineChart.destroy();
  const manyPts = allLbls.length > 60;
  _lineChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: allLbls,
      datasets: [
        { label: 'Zwrot (historia)', data: hDs,    borderColor: '#2563eb', backgroundColor: 'rgba(37,99,235,.07)', fill: true,  tension: 0.35, pointRadius: manyPts ? 0 : 3, spanGaps: false },
        { label: 'Zwrot (prognoza)', data: pDs,    borderColor: '#2563eb', borderDash: [6,4],  backgroundColor: 'transparent', fill: false, tension: 0.35, pointRadius: 0, spanGaps: false },
        { label: 'Inwestycja brutto',data: target, borderColor: '#dc2626', borderDash: [4,4],  backgroundColor: 'transparent', fill: false, pointRadius: 0 },
      ],
    },
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
  const nonEmpty = records.filter(r => (r.self_savings || 0) + (r.feedin_revenue || 0) > 0);
  const recent   = nonEmpty.slice(-24);
  const labels   = recent.map(r => r.month_label);
  const autokons = recent.map(r => r.self_savings    || 0);
  const sprzedaz = recent.map(r => r.feedin_revenue  || 0);

  const ctx = document.getElementById('barChart').getContext('2d');
  if (_barChart) _barChart.destroy();
  _barChart = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: 'Autokonsumpcja', data: autokons, backgroundColor: 'rgba(37,99,235,0.75)',  borderColor: '#2563eb', borderWidth: 1 },
        { label: 'Sprzedaz',  data: sprzedaz, backgroundColor: 'rgba(22,163,74,0.75)',  borderColor: '#16a34a', borderWidth: 1 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: {
        legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: c => c.dataset.label + ': ' + Number(c.raw).toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' zl' } }
      },
      scales: {
        x: { ticks: { maxTicksLimit: 24, font: { size: 9 }, maxRotation: 45 } },
        y: { ticks: { callback: v => v.toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' zl', font: { size: 10 } } },
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
    if (!yearTotals[y]) yearTotals[y] = {produced: 0, exported: 0, sc: 0, self_sav: 0, feedin: 0, total: 0, purchase: 0, net_grid: 0, consumed: 0, peak_kw: 0, offpeak_kw: 0, peak_kw_count: 0};
    const t = yearTotals[y];
    t.produced    += r.produced_kwh          || 0;
    t.exported    += r.exported_kwh          || 0;
    t.sc          += r.self_consumed_kwh     || 0;
    t.self_sav    += r.self_savings          || 0;
    t.feedin      += r.feedin_revenue        || 0;
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
      '<td>' + pln(t.self_sav, 0) + '</td>' +
      '<td>' + pln(t.feedin,   0) + '</td>' +
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
function renderPredTable(predictions, sensitivity, window) {
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

  /* Sensitivity table */
  const sw = document.getElementById('sensiWrap');
  if (!sensitivity || !sensitivity.base) {
    sw.style.display = 'none';
    return;
  }
  sw.style.display = '';
  const sHead = '<caption>Analiza wrazliwosci splaty</caption>' +
    '<thead><tr><th>Scenariusz</th><th>Srednia mies.</th><th>Mies. do splaty</th><th>Prognozowana data splaty</th></tr></thead>';
  const sRows = [
    ['Pesymistyczny (−15%)', sensitivity.pessimistic, ''],
    ['Bazowy', sensitivity.base, 'base'],
    ['Optymistyczny (+15%)', sensitivity.optimistic, ''],
  ].map(([label, v, rowCls]) => {
    if (!v) return '';
    const cls = rowCls ? ' class="' + rowCls + '"' : '';
    return '<tr' + cls + '>' +
      '<td>' + label + '</td>' +
      '<td>' + pln(v.avg, 2) + '</td>' +
      '<td>' + (v.months ? num(v.months, 1) : '—') + '</td>' +
      '<td>' + (v.payback_date || 'juz splacone') + '</td>' +
    '</tr>';
  }).join('');
  document.getElementById('sensiTbl').innerHTML = sHead + '<tbody>' + sRows + '</tbody>';
}

/* -- Year-over-year table -- */
function renderYearsTable(records, systemKwp) {
  const yearMap = {};
  for (const r of records) {
    const y = r.month_label.substring(0, 4);
    if (!yearMap[y]) yearMap[y] = { produced: 0, exported: 0, sc: 0, consumed: 0, purchased: 0,
                                    self_sav: 0, feedin: 0, purchase_cost: 0, net_grid: 0, months: 0 };
    const t = yearMap[y];
    t.produced      += r.produced_kwh      || 0;
    t.exported      += r.exported_kwh      || 0;
    t.sc            += r.self_consumed_kwh || 0;
    t.consumed      += r.consumed_kwh      || 0;
    t.self_sav      += r.self_savings      || 0;
    t.feedin        += r.feedin_revenue    || 0;
    t.purchase_cost += r.purchase_cost_pln || 0;
    t.net_grid      += r.net_grid_cost     || 0;
    t.months        += 1;
  }

  const years = Object.keys(yearMap).sort();

  // Find best/worst production years (full years only)
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
    '<th>Lacznie oszcz.</th>' +
    '<th>Zakup energii</th>' +
    '<th>Koszt netto sieci</th>' +
    '<th>Mies.</th>' +
  '</tr></thead>';

  const rows = years.map(y => {
    const t = yearMap[y];
    const kwhKwp = systemKwp > 0 ? Math.round(t.produced / systemKwp) : '—';
    const suff   = t.consumed > 0 ? pct(t.sc / t.consumed * 100) : '—';
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
      '<td>' + pln(t.self_sav + t.feedin, 0) + '</td>' +
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
    renderLineChart(d.records, d.predictions, d.summary.gross_investment);
    renderBarChart(d.records);
    renderRcemChart(d.records);
    renderHistTable([...d.records].reverse(), d.summary.month_closed);
    renderPredTable(d.predictions, d.sensitivity, d.summary.avg_window);
    renderYearsTable(d.records, systemKwp);
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
