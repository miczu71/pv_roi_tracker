"""Flask web UI — serves the ROI dashboard on the add-on ingress port."""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime
from typing import Optional

from dateutil.relativedelta import relativedelta
from flask import Flask, Response, jsonify

from .models import MonthlyRecord
from .roi import RoiResult

app = Flask(__name__)
log = logging.getLogger(__name__)

_lock = threading.Lock()
_state: dict = {
    'result': None,
    'records': [],
    'rcem_price': None,
    'updated_at': None,
}

_MONTHS_PL = ['', 'Sty', 'Lut', 'Mar', 'Kwi', 'Maj', 'Cze',
               'Lip', 'Sie', 'Wrz', 'Paź', 'Lis', 'Gru']


def update_state(result: RoiResult, records: list[MonthlyRecord],
                 rcem_price: Optional[float]) -> None:
    with _lock:
        _state['result'] = result
        _state['records'] = list(records)
        _state['rcem_price'] = rcem_price
        _state['updated_at'] = datetime.now().isoformat(timespec='seconds')


def _month_label(year: int, month: int) -> str:
    return f"{year}-{_MONTHS_PL[month]}"


def _build_predictions(result: RoiResult) -> list[dict]:
    avg = result.monthly_avg_savings
    if not avg or avg <= 0 or result.remaining_to_recover <= 0:
        return []
    today = date.today()
    cursor = today.replace(day=1) + relativedelta(months=1)
    remaining = result.remaining_to_recover
    cumulative = result.total_return
    rows: list[dict] = []
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
        updated_at = _state['updated_at']

    if result is None:
        return jsonify({'status': 'loading'}), 202

    today = date.today()
    current_ym = (today.year, today.month)

    cumulative = result.subsidy
    records_out = []
    for r in sorted(records, key=lambda x: (x.year, x.month)):
        if (r.year, r.month) > current_ym:
            continue  # skip empty future months imported from the CSV
        month_savings = (r.self_consumed_savings_pln or 0.0) + (r.feedin_revenue_pln or 0.0)
        cumulative += month_savings
        # A month can't be 'confirmed' if its feed-in price is still unknown
        rcem_status = r.rcem_status
        if rcem_status == 'confirmed' and r.feedin_price_pln_kwh is None:
            rcem_status = 'pending'
        records_out.append({
            'month_label': _month_label(r.year, r.month),
            'is_current': (r.year, r.month) == current_ym,
            'produced_kwh': r.produced_kwh,
            'exported_kwh': r.exported_kwh,
            'self_consumed_kwh': r.self_consumed_kwh,
            'buy_price': r.buy_price_pln_kwh,
            'feedin_price': r.feedin_price_pln_kwh,
            'self_savings': r.self_consumed_savings_pln,
            'feedin_revenue': r.feedin_revenue_pln,
            'month_savings': round(month_savings, 2),
            'cumulative_return': round(cumulative, 2),
            'roi_pct': round(cumulative / result.gross_investment * 100, 2),
            'rcem_status': rcem_status,
        })

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
        },
        'records': records_out,
        'predictions': _build_predictions(result),
    })


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
a { color: var(--accent); }

header {
  background: linear-gradient(135deg, #1e3a5f 0%, #2563eb 100%);
  color: #fff; padding: 14px 24px;
  display: flex; align-items: center; justify-content: space-between;
}
header h1 { font-size: 17px; font-weight: 700; }
#updated { font-size: 11px; opacity: .75; }

main { max-width: 1600px; margin: 0 auto; padding: 18px 16px; }

.loading { text-align: center; padding: 60px 0; color: var(--muted); font-size: 15px; }

/* ── Summary cards ── */
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 10px; margin-bottom: 18px; }
.card { background: var(--card); border-radius: var(--radius); padding: 14px 16px; box-shadow: var(--shadow); }
.card .lbl { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: .6px; margin-bottom: 5px; }
.card .val { font-size: 21px; font-weight: 700; line-height: 1.1; }
.card .sub { font-size: 11px; color: var(--muted); margin-top: 4px; }
.c-blue  .val { color: var(--accent); }
.c-green .val { color: var(--green); }

/* ── Chart ── */
.chart-wrap { background: var(--card); border-radius: var(--radius); padding: 16px; box-shadow: var(--shadow); margin-bottom: 18px; height: 280px; position: relative; }

/* ── Tabs ── */
.tabs { display: flex; gap: 3px; }
.tab-btn {
  padding: 8px 18px; border: none; cursor: pointer; font-size: 12px; font-weight: 600;
  border-radius: var(--radius) var(--radius) 0 0;
  background: #dde3eb; color: var(--muted);
  transition: background .15s;
}
.tab-btn.active { background: var(--card); color: var(--text); box-shadow: 0 -1px 3px rgba(0,0,0,.08); }
.tab-panel {
  background: var(--card); border-radius: 0 var(--radius) var(--radius) var(--radius);
  box-shadow: var(--shadow); overflow: hidden;
}

/* ── Tables ── */
.tbl-wrap { overflow-x: auto; max-height: 520px; overflow-y: auto; }
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
thead th {
  position: sticky; top: 0; z-index: 2;
  background: #f7fafc; padding: 9px 11px;
  text-align: right; font-weight: 600; font-size: 10.5px;
  text-transform: uppercase; letter-spacing: .4px; color: var(--muted);
  border-bottom: 2px solid var(--border); white-space: nowrap;
}
thead th:first-child { text-align: left; }
tbody td { padding: 7px 11px; text-align: right; border-bottom: 1px solid var(--border); white-space: nowrap; }
tbody td:first-child { text-align: left; font-weight: 600; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: #f7fafc; }
tbody tr.cur td { background: #eff6ff; }
tbody tr.cur td:first-child::after { content: "\00a0\2605"; color: var(--accent); font-size: 10px; }
tbody tr.pb td { background: #f0fdf4; color: var(--green); font-weight: 700; }
.tbl-foot { padding: 10px 14px; font-size: 11px; color: var(--muted); border-top: 1px solid var(--border); }

/* ── RCEm status badges ── */
.badge { display: inline-block; padding: 2px 7px; border-radius: 4px; font-size: 10px; font-weight: 700; }
.badge-ok      { background: #dcfce7; color: var(--green); }
.badge-pending { background: #fef3c7; color: var(--yellow); }
.badge-missing { background: #fee2e2; color: var(--red); }
</style>
</head>
<body>
<header>
  <h1>&#9728;&#65039; PV ROI Tracker</h1>
  <span id="updated">Ładowanie&hellip;</span>
</header>
<main>
  <div id="loading" class="loading">Pobieranie danych&hellip;</div>
  <div id="content" style="display:none">
    <div class="cards" id="cards"></div>
    <div class="chart-wrap"><canvas id="chart"></canvas></div>
    <div class="tabs">
      <button class="tab-btn active" onclick="showTab('hist')">Historia miesięczna</button>
      <button class="tab-btn"        onclick="showTab('pred')">Prognoza spłaty</button>
    </div>
    <div class="tab-panel">
      <div id="tab-hist">
        <div class="tbl-wrap"><table id="histTbl"></table></div>
        <div class="tbl-foot" id="histFoot"></div>
      </div>
      <div id="tab-pred" style="display:none">
        <div class="tbl-wrap"><table id="predTbl"></table></div>
        <div class="tbl-foot" id="predFoot"></div>
      </div>
    </div>
  </div>
</main>
<script>
'use strict';
let _chart = null;

/* ── Formatters ── */
function fmt(v, dp, sfx) {
  if (v == null) return '—';
  let s = Number(v).toLocaleString('pl-PL', {minimumFractionDigits: dp, maximumFractionDigits: dp});
  return sfx ? s + ' ' + sfx : s;
}
const pln   = (v, dp=0) => fmt(v, dp, 'zł');
const pct   = (v)       => fmt(v, 2, '%');
const kwh   = (v)       => fmt(v, 1, 'kWh');
const price = (v)       => fmt(v, 4, 'zł/kWh');
const num   = (v, dp=1) => fmt(v, dp);

/* ── Tab switching ── */
function showTab(name) {
  ['hist','pred'].forEach(t => {
    document.getElementById('tab-' + t).style.display = (t === name) ? '' : 'none';
  });
  document.querySelectorAll('.tab-btn').forEach((b, i) =>
    b.classList.toggle('active', (i === 0) === (name === 'hist'))
  );
}

/* ── Summary cards ── */
function renderCards(s) {
  const roi = s.roi_pct;
  const cards = [
    { lbl: 'ROI',              val: pct(roi),                     cls: roi >= 100 ? 'c-green' : 'c-blue' },
    { lbl: 'Łączny zwrot',     val: pln(s.total_return),          sub: `subsydium ${pln(s.subsidy)} + oszcz. ${pln(s.total_savings)}` },
    { lbl: 'Oszczędności',     val: pln(s.total_savings),         sub: `autokons. ${pln(s.self_consumption_savings)} / sprzedaż ${pln(s.feedin_revenue)}` },
    { lbl: 'Pozostało',        val: pln(s.remaining_to_recover),  sub: `inwestycja brutto ${pln(s.gross_investment)}` },
    { lbl: 'ᖪrednia mies.', val: pln(s.monthly_avg_savings), sub: `ost. ${s.avg_window} mies.` },
    { lbl: 'Spłata',           val: s.payback_date || '—',   sub: s.years_to_payback != null ? `za ${num(s.years_to_payback, 1)} lat` : '' },
    { lbl: 'Produkcja łącznie',val: kwh(s.total_produced_kwh),    sub: `uzysk ${num(s.specific_yield, 0)} kWh/kWp` },
    { lbl: 'RCEm bieżący',     val: s.rcem_price != null ? price(s.rcem_price) : '—' },
  ];
  // Fix the broken char from Python raw string
  cards[4].lbl = 'Średnia mies.';
  document.getElementById('cards').innerHTML = cards.map(c =>
    `<div class="card ${c.cls||''}">
       <div class="lbl">${c.lbl}</div>
       <div class="val">${c.val}</div>
       ${c.sub ? `<div class="sub">${c.sub}</div>` : ''}
     </div>`
  ).join('');
}

/* ── Chart ── */
function renderChart(records, predictions, gross) {
  const histLbls = records.map(r => r.month_label);
  const histVals = records.map(r => r.cumulative_return);
  const predLbls = predictions.map(p => p.month_label);
  const predVals = predictions.map(p => p.cumulative_return);

  const allLbls = [...histLbls, ...predLbls];
  const target  = allLbls.map(() => gross);

  // Solid history; dashed predictions (bridged from last hist point)
  const hDs = [...histVals, ...predLbls.map(() => null)];
  const pDs = [...histLbls.map(() => null)];
  if (histVals.length) pDs[histVals.length - 1] = histVals[histVals.length - 1];
  pDs.push(...predVals);

  const ctx = document.getElementById('chart').getContext('2d');
  if (_chart) _chart.destroy();
  const manyPts = allLbls.length > 60;
  _chart = new Chart(ctx, {
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
        tooltip: {
          callbacks: {
            label: ctx => {
              if (ctx.raw == null) return null;
              return ctx.dataset.label + ': ' + Number(ctx.raw).toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' zł';
            }
          }
        }
      },
      scales: {
        x: { ticks: { maxTicksLimit: 24, font: { size: 10 }, maxRotation: 45 } },
        y: { ticks: { callback: v => (v/1000).toFixed(0) + 'k zł', font: { size: 10 } } },
      },
    },
  });
}

/* ── History table ── */
function renderHistTable(records) {
  const head = `<thead><tr>
    <th>Miesiąc</th>
    <th title="kWh wyprodukowane">Produkcja</th>
    <th title="kWh sprzedane">Sprzedane</th>
    <th title="kWh autokonsumpcja">Autokons.</th>
    <th title="PLN/kWh cena zakupu">Cena zakupu</th>
    <th title="PLN/kWh RCEm">RCEm</th>
    <th title="PLN oszczędności autokonsumpcji">Oszcz. autokons.</th>
    <th title="PLN przychód ze sprzedaży">Przych. sprzedaży</th>
    <th title="PLN łącznie w miesiącu">Łącznie mies.</th>
    <th title="PLN kumulatywnie">Kumulatywnie</th>
    <th>ROI</th>
    <th>RCEm status</th>
  </tr></thead>`;

  const badgeMap = { ok: 'badge-ok', pending: 'badge-pending', missing: 'badge-missing', confirmed: 'badge-ok' };

  const rows = records.map(r => {
    const cls = r.is_current ? ' class="cur"' : '';
    const st  = r.rcem_status || 'ok';
    return `<tr${cls}>
      <td>${r.month_label}</td>
      <td>${kwh(r.produced_kwh)}</td>
      <td>${kwh(r.exported_kwh)}</td>
      <td>${kwh(r.self_consumed_kwh)}</td>
      <td>${price(r.buy_price)}</td>
      <td>${price(r.feedin_price)}</td>
      <td>${pln(r.self_savings, 2)}</td>
      <td>${pln(r.feedin_revenue, 2)}</td>
      <td>${pln(r.month_savings, 2)}</td>
      <td>${pln(r.cumulative_return)}</td>
      <td>${pct(r.roi_pct)}</td>
      <td><span class="badge ${badgeMap[st]||'badge-ok'}">${st}</span></td>
    </tr>`;
  }).join('');

  document.getElementById('histTbl').innerHTML  = head + '<tbody>' + rows + '</tbody>';
  document.getElementById('histFoot').textContent = records.length + ' miesięcy danych';
}

/* ── Predictions table ── */
function renderPredTable(predictions, window) {
  if (!predictions.length) {
    document.getElementById('predTbl').innerHTML =
      '<tbody><tr><td colspan="5" style="padding:24px;text-align:center;color:#718096">Inwestycja już spłacona lub brak danych do prognozy.</td></tr></tbody>';
    document.getElementById('predFoot').textContent = '';
    return;
  }
  const head = `<thead><tr>
    <th>Miesiąc</th>
    <th>Proj. oszczędności</th>
    <th>Kumulatywnie</th>
    <th>Pozostało</th>
    <th>ROI</th>
  </tr></thead>`;

  const rows = predictions.map((p, i) => {
    const isPb = p.remaining <= 0;
    const cls  = isPb ? ' class="pb"' : '';
    return `<tr${cls}>
      <td>${p.month_label}${isPb ? ' 🎉' : ''}</td>
      <td>${pln(p.projected_savings, 2)}</td>
      <td>${pln(p.cumulative_return)}</td>
      <td>${pln(Math.max(0, p.remaining))}</td>
      <td>${pct(p.roi_pct)}</td>
    </tr>`;
  }).join('');

  document.getElementById('predTbl').innerHTML  = head + '<tbody>' + rows + '</tbody>';
  document.getElementById('predFoot').textContent =
    'Prognoza oparta na średniej z ostatnich ' + window + ' pełnych miesięcy';
}

/* ── Main data load ── */
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

    document.getElementById('loading').style.display  = 'none';
    document.getElementById('content').style.display  = '';
    document.getElementById('updated').textContent = 'Aktualizacja: ' + (d.updated_at || '—');

    renderCards(d.summary);
    renderChart(d.records, d.predictions, d.summary.gross_investment);
    renderHistTable([...d.records].reverse());
    renderPredTable([...d.predictions].reverse(), d.summary.avg_window);
  } catch (e) {
    document.getElementById('updated').textContent = 'Błąd połączenia';
    console.error(e);
  }
}

loadData();
setInterval(loadData, 60000);
</script>
</body>
</html>"""
