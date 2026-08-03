'use strict';
let _barChart = null, _rcemChart = null, _autarkiaChart = null, _prodChart = null, _arbitrageChart = null, _netCostChart = null, _priceSpreadChart = null, _yieldChart = null, _energyBalChart = null, _yearCompChart = null, _prodRankChart = null, _depositChart = null, _costBreakdownChart = null;
let _tariffPriceChart = null, _tariffCompChart = null, _tariffCumChart = null, _tariffSeasonChart = null, _tariffHistChart = null;
let _rceCmpChart = null;
let _fanChart = null, _waterfallChart = null, _sankeyChart = null, _cpiRealChart = null, _degradChart = null;
let _billChart = null, _co2Chart = null, _rateTrendChart = null;
let _batMonthlyChart = null, _batCumChart = null, _batCfgLoaded = false;
let _forecastChart = null;
let _lastRecords = [], _lastInvoices = [], _lastRceMonths = [], _lastRateTrend = null, _lastSummary = null;

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
  const TABS = ['hist','pred','years','charts','invoices','tariff','taryfa','rce','battery','forecast'];
  TABS.forEach(t => {
    document.getElementById('tab-' + t).style.display = (t === name) ? '' : 'none';
  });
  document.querySelectorAll('.tab-btn').forEach((b, i) =>
    b.classList.toggle('active', TABS[i] === name)
  );
  if (name === 'rce' && _rceCmpChart) _rceCmpChart.resize();
  if (name === 'battery') [_batMonthlyChart, _batCumChart].forEach(c => c && c.resize());
  if (name === 'charts') {
    [_rcemChart, _autarkiaChart, _prodChart, _arbitrageChart, _netCostChart,
     _priceSpreadChart, _yieldChart, _energyBalChart, _yearCompChart, _prodRankChart,
     _cpiRealChart, _degradChart, _waterfallChart, _sankeyChart,
     _billChart, _co2Chart].forEach(c => c && c.resize());
  }
  if (name === 'invoices' && _depositChart) _depositChart.resize();
  if (name === 'invoices' && _costBreakdownChart) _costBreakdownChart.resize();
  if (name === 'invoices' && _rateTrendChart) _rateTrendChart.resize();
  if (name === 'forecast' && _forecastChart) _forecastChart.resize();
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
    s.co2_avoided_kg != null && s.co2_avoided_kg > 0 ? { lbl: 'CO₂ unikniete', val: s.co2_avoided_kg >= 1000 ? fmt(s.co2_avoided_kg / 1000, 2, 't') : fmt(s.co2_avoided_kg, 0, 'kg'), sub: (() => { const trees = Math.round(s.co2_avoided_kg / 21); const km = Math.round(s.co2_avoided_kg / 0.21); return 'KOBiZE  •  ≈' + trees + ' drzew  •  ≈' + km + ' km autem'; })(), cls: 'c-green' } : null,
    s.yoy_yield_delta_pct != null ? { lbl: 'Produkcja r/r', val: fmt(s.yoy_yield_delta_pct, 1, '%'), sub: 'te same miesiące rok do roku', cls: s.yoy_yield_delta_pct >= 0 ? 'c-green' : '' } : null,
    // v0.27.0: rachunek bez PV vs z PV
    s.bill_comparison && s.bill_comparison.total_saved > 0 ? { lbl: 'Zaoszcz. na rachunku', val: pln(s.bill_comparison.total_saved), sub: 'łącznie gdyby nie było PV  •  śr. o ' + fmt(s.bill_comparison.avg_savings_pct, 0, '%') + ' taniej', cls: 'c-green' } : null,
    s.bill_comparison && s.bill_comparison.total_with_pv != null ? { lbl: 'Rachunek z PV', val: pln(s.bill_comparison.total_with_pv), sub: 'suma opłacona Tauronowi za cały okres', cls: '' } : null,
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

/* -- v0.27.0: Rachunek bez PV vs z PV -- */
function renderBillChart(records) {
  const ctx = document.getElementById('billChart');
  if (!ctx) return;
  if (_billChart) { _billChart.destroy(); _billChart = null; }
  const valid = records.filter(r => r.bill_without_pv != null && r.bill_with_pv != null);
  if (!valid.length) { ctx.getContext('2d'); return; }
  const recent = valid.slice(-30);
  const labels     = recent.map(r => r.month_label);
  const withoutPV  = recent.map(r => r.bill_without_pv);
  const withPV     = recent.map(r => r.bill_with_pv);
  const saved      = recent.map(r => Math.max(0, r.bill_without_pv - r.bill_with_pv));
  _billChart = new Chart(ctx.getContext('2d'), {
    data: {
      labels,
      datasets: [
        { type: 'bar', label: 'Rachunek bez PV', data: withoutPV,
          backgroundColor: 'rgba(239,68,68,0.55)', borderColor: 'rgba(239,68,68,0.9)',
          borderWidth: 1, order: 2 },
        { type: 'bar', label: 'Rachunek z PV', data: withPV,
          backgroundColor: 'rgba(59,130,246,0.55)', borderColor: 'rgba(59,130,246,0.9)',
          borderWidth: 1, order: 2 },
        { type: 'line', label: 'Zaoszcz. (zł)', data: saved,
          borderColor: 'rgba(22,163,74,1)', backgroundColor: 'rgba(22,163,74,0.15)',
          borderWidth: 2, pointRadius: 2, fill: false, tension: 0.3, order: 1 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { font: { size: 11 } } },
        tooltip: { callbacks: {
          label: c => c.dataset.label + ': ' + c.raw.toLocaleString('pl-PL', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' zł',
        }},
      },
      scales: {
        x: { ticks: { font: { size: 10 }, maxRotation: 45 } },
        y: { ticks: { font: { size: 10 }, callback: v => v.toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' zł' } },
      },
    },
  });
}


/* -- v0.27.0: Skumulowane uniknięte CO₂ -- */
function renderCo2Chart(records, co2Factor) {
  const ctx = document.getElementById('co2Chart');
  if (!ctx) return;
  if (_co2Chart) { _co2Chart.destroy(); _co2Chart = null; }
  const factor = co2Factor || 0.597;
  const withProd = records.filter(r => (r.produced_kwh || 0) > 0);
  if (!withProd.length) return;
  let cum = 0;
  const labels = [], dataKg = [], dataTrees = [];
  withProd.forEach(r => {
    cum += (r.produced_kwh || 0) * factor;
    labels.push(r.month_label);
    dataKg.push(Math.round(cum));
    dataTrees.push(+(cum / 21).toFixed(1));
  });
  _co2Chart = new Chart(ctx.getContext('2d'), {
    data: {
      labels,
      datasets: [
        { type: 'line', label: 'CO₂ uniknięte (kg, skum.)', data: dataKg,
          borderColor: 'rgba(22,163,74,1)', backgroundColor: 'rgba(22,163,74,0.1)',
          borderWidth: 2, pointRadius: 2, fill: true, tension: 0.3, yAxisID: 'yCo2' },
        { type: 'line', label: 'Ekwiwalent drzew (szt.)', data: dataTrees,
          borderColor: 'rgba(134,239,172,1)', backgroundColor: 'transparent',
          borderWidth: 1.5, pointRadius: 0, borderDash: [4, 3], tension: 0.3, yAxisID: 'yTrees' },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { font: { size: 11 } } },
        tooltip: { callbacks: {
          label: c => {
            if (c.datasetIndex === 0)
              return 'CO₂: ' + c.raw.toLocaleString('pl-PL') + ' kg (' + (c.raw / 1000).toFixed(2) + ' t)';
            return 'Drzewa: ≈' + c.raw.toLocaleString('pl-PL') + ' szt.';
          },
        }},
      },
      scales: {
        x: { ticks: { font: { size: 10 }, maxRotation: 45 } },
        yCo2: { position: 'left', ticks: { font: { size: 10 }, callback: v => v >= 1000 ? (v/1000).toFixed(1) + ' t' : v + ' kg' } },
        yTrees: { position: 'right', grid: { drawOnChartArea: false },
                  ticks: { font: { size: 10 }, callback: v => Math.round(v) + ' drzew' } },
      },
    },
  });
}


/* -- v0.27.0: Trend stawek jednostkowych z faktur -- */
function renderRateTrendChart(rateTrend) {
  const section = document.getElementById('rateTrendSection');
  const ctx     = document.getElementById('rateTrendChart');
  if (!ctx) return;
  if (_rateTrendChart) { _rateTrendChart.destroy(); _rateTrendChart = null; }

  if (!rateTrend || !rateTrend.rates_per_month || !rateTrend.rates_per_month.length) {
    if (section) section.style.display = 'none';
    return;
  }
  if (section) section.style.display = '';

  const months = rateTrend.rates_per_month;
  const labels = months.map(m => m.ym);

  const FIELDS = [
    { key: 'energy_peak_net',      label: 'Energia szczyt',        color: 'rgba(239,68,68,0.85)' },
    { key: 'energy_offpeak_net',   label: 'Energia poza szczytem', color: 'rgba(251,146,60,0.85)' },
    { key: 'dist_var_peak_net',    label: 'Siec zm. szczyt',       color: 'rgba(59,130,246,0.85)' },
    { key: 'dist_var_offpeak_net', label: 'Siec zm. poza szczytem',color: 'rgba(147,197,253,0.85)' },
    { key: 'jakosciowa_net',       label: 'Jakościowa',            color: 'rgba(134,239,172,0.85)' },
    { key: 'oze_net',              label: 'OZE',                   color: 'rgba(167,243,208,0.85)' },
    { key: 'kogeneracja_net',      label: 'Kogeneracja',           color: 'rgba(209,213,219,0.85)' },
    { key: 'effective_gross_per_kwh', label: 'Efektywna all-in (brutto)', color: 'rgba(109,40,217,1)', borderWidth: 2.5, borderDash: [] },
  ];

  const datasets = [];
  FIELDS.forEach(f => {
    const data = months.map(m => m[f.key] ?? null);
    if (data.every(v => v === null)) return;
    datasets.push({
      label: f.label, data,
      borderColor: f.color, backgroundColor: 'transparent',
      borderWidth: f.borderWidth || 1.5,
      borderDash: f.borderDash !== undefined ? f.borderDash : [4, 3],
      pointRadius: 3, tension: 0.3, spanGaps: true,
    });
  });

  // KPI: efektywna cena + r/r
  const kpiEl = document.getElementById('rateTrendKpi');
  if (kpiEl) {
    const eff = rateTrend.latest_effective_gross_per_kwh;
    const yoy = rateTrend.yoy_effective_gross_pct;
    let html = '';
    if (eff != null)
      html += '<span style="font-size:12px;font-weight:600">Efektywna: ' + eff.toFixed(4) + ' zł/kWh</span>';
    if (yoy != null) {
      const cls = yoy > 0 ? 'color:#ef4444' : 'color:#16a34a';
      html += ' <span style="font-size:11px;' + cls + '">' + (yoy > 0 ? '+' : '') + yoy.toFixed(1) + '% r/r</span>';
    }
    kpiEl.innerHTML = html;
  }

  _rateTrendChart = new Chart(ctx.getContext('2d'), {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { labels: { font: { size: 10 }, boxWidth: 12 } },
        tooltip: { callbacks: {
          label: c => c.dataset.label + ': ' + (c.raw != null ? c.raw.toFixed(4) + ' zł/kWh' : '—'),
        }},
      },
      scales: {
        x: { ticks: { font: { size: 10 }, maxRotation: 45 } },
        y: { ticks: { font: { size: 10 }, callback: v => v.toFixed(3) + ' zł' } },
      },
    },
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

    // v0.27.0: badge alertu „poniżej oczekiwań" dla ostatniego zamkniętego miesiąca
    let underBadge = '';
    if (_lastSummary && _lastSummary.underperformance_flag === 'uwaga') {
      // underperformance_pct not in summary per-month — just use global last closed ym
      // but we compare month_key to find the flagged month
      const ym = _lastSummary.underperformance_last_closed_ym;
      if (!ym || ym === r.month_key) {
        const dev = _lastSummary.underperformance_pct;
        const tip = 'Produkcja ' + (dev != null ? dev.toFixed(1) + '% ' : '') +
                    'poniżej oczekiwania sezonowego — sprawdź zabrudzenie / uszkodzenie paneli';
        underBadge = ' <span class="badge badge-uwaga" title="' + tip + '" style="cursor:help">⚠ poniżej</span>';
      }
    }
    const monthLabel = r.month_label + invBadge + underBadge;

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
  renderBalanceDiagnostics(records);
}

/* -- 0.35.3: diagnostyka rozjazdu rodzin produkcji (balance.py) --
   Informacyjna tabela dla miesiecy, dla ktorych zebrano cross_family_produced_kwh
   (rodzina inverter_total_yield). Miesiace rozliczone fakturą są oznaczone —
   dla nich rozjazd nigdy nie jest naprawiany (faktura jest ostateczna), więc
   pokazujemy go, ale bez żadnego badge'a alarmowego. */
function renderBalanceDiagnostics(records) {
  const rows = records.filter(r => r.cross_family_produced_kwh != null);
  const wrap = document.getElementById('balanceDiagWrap');
  if (!rows.length) { wrap.style.display = 'none'; return; }
  wrap.style.display = '';

  const head = '<thead><tr>' +
    '<th>Miesiąc</th>' +
    '<th title="produced_kwh — rodzina Energy Dashboard (sensor.energy_pv)">Produkcja (Dashboard)</th>' +
    '<th title="cross_family_produced_kwh — rodzina inverter_total_yield">Produkcja (Falownik)</th>' +
    '<th>Δ kWh</th>' +
    '<th>Δ %</th>' +
    '<th>Status</th>' +
  '</tr></thead>';

  let html = '';
  for (const r of rows) {
    const produced = r.produced_kwh || 0;
    const cross = r.cross_family_produced_kwh;
    const diffKwh = r.balance_residual_kwh != null ? r.balance_residual_kwh : Math.abs(produced - cross);
    const diffPct = produced > 0 ? Math.abs(produced - cross) / produced * 100 : 0;
    const status = r.balance_reconciled
      ? '<span class="badge badge-ok" title="Miesiąc rozliczony fakturą — rozjazd jest wyłącznie diagnostyką, nigdy nie jest korygowany">faktura ostateczna</span>'
      : '<span class="badge badge-ok" title="Normalny rozjazd między dwoma niezależnymi licznikami produkcji">na żywo</span>';
    html += '<tr>' +
      '<td>' + r.month_label + '</td>' +
      '<td>' + kwh(produced) + '</td>' +
      '<td>' + kwh(cross) + '</td>' +
      '<td>' + kwh(diffKwh) + '</td>' +
      '<td>' + pct(diffPct) + '</td>' +
      '<td>' + status + '</td>' +
    '</tr>';
  }
  document.getElementById('balanceDiagTbl').innerHTML = head + '<tbody>' + html + '</tbody>';
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

  // v0.33.0: druga karta doradcy — z uwzględnieniem wyższego limitu zwrotu
  // depozytu (30% RCE vs 20% RCEm). Osobna karta, nie podmienia rekomendacji
  // powyżej opartej wyłącznie na różnicy przychodu.
  const adv = rc.advisor;
  const advCards = adv ? [
    { lbl: 'Rekomendacja z uwzgl. depozytu', val: adv.recommendation || '—', sub: adv.recommendation_reason || '',
      cls: adv.recommendation === 'ROZWAŻ RCE' ? 'c-green' : adv.recommendation === 'ZOSTAŃ PRZY RCEm' ? 'c-blue' : '' },
    { lbl: 'Efekt depozytu (limit 30% vs 20%)', val: pln(adv.deposit_refund_delta_annual_pln, 2), sub: 'rocznie, dodatkowy zwrot przy RCE' },
    { lbl: 'Łącznie / mies. (przychód + depozyt)', val: pln(adv.combined_avg_monthly_pln, 2), sub: 'różnica + 1/12 efektu depozytu',
      cls: (adv.combined_avg_monthly_pln || 0) > 0 ? 'c-green' : '' },
  ] : [];
  document.getElementById('rceAdvisorCards').innerHTML = advCards.length ? advCards.map(c =>
    '<div class="card ' + (c.cls || '') + '">' +
      '<div class="lbl">' + c.lbl + '</div>' +
      '<div class="val">' + c.val + '</div>' +
      (c.sub ? '<div class="sub">' + c.sub + '</div>' : '') +
    '</div>'
  ).join('') : '';
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

/* v0.33.0: Prognoza wieloletnia — skumulowany zwrot P10/P50/P90 do końca żywotności */
function renderForecastTab(lf, summary) {
  const ctx = document.getElementById('forecastChart');
  if (!ctx) return;
  const cards = document.getElementById('forecastKpiCards');
  const years = (lf && lf.years) || [];
  if (!years.length) {
    if (cards) cards.innerHTML = '<div class="card"><div class="lbl">Prognoza wieloletnia</div><div class="val">—</div>' +
      '<div class="sub">za mało danych — brak ustalonej daty uruchomienia</div></div>';
    if (_forecastChart) { _forecastChart.destroy(); _forecastChart = null; }
    return;
  }
  const last = years[years.length - 1];
  if (cards) {
    const kpis = [
      { lbl: 'Horyzont prognozy', val: (lf.asset_lifetime_years || years.length) + ' lat', sub: years[0].calendar_year + '–' + last.calendar_year },
      { lbl: 'Zwrot skumulowany P50 (koniec horyzontu)', val: pln(last.cumulative_return_p50), sub: 'ROI ' + pct(last.cumulative_roi_pct_p50), cls: 'c-green' },
      { lbl: 'Pasmo niepewności (koniec horyzontu)', val: pln(last.cumulative_return_p10) + ' – ' + pln(last.cumulative_return_p90), sub: 'P10 (optymistyczne) – P90 (pesymistyczne)' },
    ];
    cards.innerHTML = kpis.map(c =>
      '<div class="card ' + (c.cls || '') + '">' +
        '<div class="lbl">' + c.lbl + '</div>' +
        '<div class="val">' + c.val + '</div>' +
        (c.sub ? '<div class="sub">' + c.sub + '</div>' : '') +
      '</div>'
    ).join('');
  }

  const labels = years.map(y => String(y.calendar_year));
  const gross = summary && summary.gross_investment;
  const netInvestment = summary && summary.net_investment;
  const datasets = [
    { label: 'P50 (sezonowa)', data: years.map(y => y.cumulative_return_p50), borderColor: '#2563eb', borderDash: [6,4], fill: false, tension: .3, pointRadius: 0 },
    { label: 'P10 (optymistyczne)', data: years.map(y => y.cumulative_return_p10), borderColor: 'rgba(22,163,74,.45)', backgroundColor: 'rgba(37,99,235,.12)', fill: '+1', tension: .3, pointRadius: 0, borderWidth: 1 },
    { label: 'P90 (pesymistyczne)', data: years.map(y => y.cumulative_return_p90), borderColor: 'rgba(220,38,38,.45)', fill: false, tension: .3, pointRadius: 0, borderWidth: 1 },
    { label: 'Inwestycja brutto', data: labels.map(() => gross), borderColor: '#dc2626', borderDash: [4,4], fill: false, pointRadius: 0 },
  ];
  if (netInvestment != null)
    datasets.push({ label: 'Inwestycja netto', data: labels.map(() => netInvestment), borderColor: '#16a34a', borderDash: [4,4], fill: false, pointRadius: 0 });

  if (_forecastChart) _forecastChart.destroy();
  _forecastChart = new Chart(ctx, {
    type: 'line',
    data: { labels, datasets },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } },
        tooltip: { callbacks: { label: c => c.raw == null ? null : c.dataset.label + ': ' + Number(c.raw).toLocaleString('pl-PL', {maximumFractionDigits: 0}) + ' zl' } },
      },
      scales: {
        x: { ticks: { maxTicksLimit: 20, font: { size: 10 }, maxRotation: 45 } },
        y: { ticks: { callback: v => (v/1000).toFixed(0) + 'k zl', font: { size: 10 } } },
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
    let html = parts.length ? '(' + parts.join(' • ') + ')' : '';
    if (deg.warranty_flag === 'uwaga') {
      const tip = 'Spadek wydajności szybszy niż zakładana gwarancja producenta';
      html += ' <span class="badge badge-uwaga" title="' + tip + '" style="cursor:help">⚠ szybciej niż zakładana gwarancja</span>';
    }
    badge.innerHTML = html;
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
/* -- Magazyn +5 kWh tab -- */
function renderBatteryTab(bs) {
  const waiting = document.getElementById('batWaiting');
  const content = document.getElementById('batContent');
  if (!bs || !bs.summary) {
    waiting.style.display = '';
    content.style.display = 'none';
    return;
  }
  waiting.style.display = 'none';
  content.style.display = '';
  const s = bs.summary, cfg = bs.config || {};
  document.getElementById('batIntroKwh').textContent = fmt(cfg.usable_kwh, 1);
  document.getElementById('batIntroKw').textContent = fmt(cfg.power_kw, 1);

  const lowCycles = s.cycles_per_month != null && s.cycles_per_month < 9;
  const marginOk  = (s.margin_above_throughput || 0) > 0;
  const cards = [
    { lbl: 'Śr. oszczędność/mies.', val: pln(s.monthly_avg_savings, 2),
      sub: 'okno ' + s.avg_window + ' mies. • symulacja ' + s.months_simulated + ' mies.', cls: 'c-blue' },
    { lbl: 'Zwrot (P50)', val: s.payback_date ? s.payback_date.slice(0, 7) : '> 20 lat',
      sub: (s.payback_years != null ? '~' + fmt(s.payback_years, 1) + ' lat' : '')
        + (s.payback_date_p10 ? ' • P10 ' + s.payback_date_p10.slice(0, 7) : '')
        + (s.payback_date_p90 ? ' • P90 ' + s.payback_date_p90.slice(0, 7) : ' • P90 > 20 lat') },
    { lbl: 'NPV @4% (10 lat)', val: pln(s.npv), sub: 'horyzont gwarancji modułu',
      cls: (s.npv || 0) >= 0 ? 'c-green' : '' },
    s.irr_pct != null ? { lbl: 'IRR', val: pct(s.irr_pct),
      sub: 'roczna stopa zwrotu', cls: s.irr_pct >= 0 ? 'c-green' : '' } : null,
    { lbl: 'Cykle/mies.', val: fmt(s.cycles_per_month, 1),
      sub: (lowCycles ? '&#9888; moduł słabo wykorzystany • ' : '') + 'łącznie ' + fmt(s.cycles_total, 0) },
    { lbl: 'Marża − koszt przerobu', val: fmt(s.margin_above_throughput, 2, 'zł/kWh'),
      sub: 'marża ' + fmt(s.margin_per_kwh, 2) + ' − degradacja ' + fmt(s.throughput_cost_kwh, 2),
      cls: marginOk ? 'c-green' : '' },
    { lbl: 'Retro od ' + (s.retro_first_ym || '—'), val: pln(s.retro_total_savings),
      sub: s.retro_paid_back_ym ? 'moduł spłaciłby się w ' + s.retro_paid_back_ym
        : 'do dziś nie spłaciłby ceny ' + fmt(cfg.module_price_pln, 0, 'zł') },
    bs.s2_avg_monthly != null ? { lbl: 'Taryfa dynamiczna (S2)', val: pln(bs.s2_avg_monthly, 2),
      sub: 'śr./mies. przy cenach godzinowych RCE' } : null,
    { lbl: 'Energia przechwycona', val: kwh(s.total_charged_kwh),
      sub: 'oddane do domu ' + kwh(s.total_discharged_kwh) },
  ].filter(Boolean);
  document.getElementById('batKpiCards').innerHTML = cards.map(c =>
    '<div class="card ' + (c.cls || '') + '">' +
      '<div class="lbl">' + c.lbl + '</div>' +
      '<div class="val">' + c.val + '</div>' +
      (c.sub ? '<div class="sub">' + c.sub + '</div>' : '') +
    '</div>'
  ).join('');

  const rows = (bs.s1_months || []).filter(r => !r.is_current);
  const labels = rows.map(r => r.ym);
  const pvShift = rows.map(r => +(r.savings_pln - (r.arb_savings_pln || 0)).toFixed(2));
  const arb = rows.map(r => +(r.arb_savings_pln || 0).toFixed(2));
  const s2ByYm = {};
  (bs.s2_months || []).forEach(r => { if (!r.is_current) s2ByYm[r.ym] = r.savings_pln; });
  const s2line = labels.map(l => s2ByYm[l] != null ? s2ByYm[l] : null);

  if (_batMonthlyChart) _batMonthlyChart.destroy();
  _batMonthlyChart = new Chart(document.getElementById('batMonthlyChart'), {
    type: 'bar',
    data: { labels, datasets: [
      { label: 'Przesunięcie PV (S1)', data: pvShift, backgroundColor: 'rgba(37,99,235,0.75)', stack: 's1' },
      { label: 'Arbitraż G12w', data: arb, backgroundColor: 'rgba(234,179,8,0.80)', stack: 's1' },
      { label: 'Taryfa dynamiczna (S2)', data: s2line, type: 'line', borderColor: '#dc2626',
        backgroundColor: 'transparent', borderWidth: 2, pointRadius: 2, spanGaps: false },
    ]},
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } } },
      scales: { x: { stacked: true, ticks: { font: { size: 10 } } },
                y: { stacked: true, title: { display: true, text: 'zł/mies.' } } } },
  });

  let cum = 0;
  const cumData = rows.map(r => +(cum += r.savings_pln).toFixed(2));
  const priceLine = labels.map(() => cfg.module_price_pln);
  if (_batCumChart) _batCumChart.destroy();
  _batCumChart = new Chart(document.getElementById('batCumChart'), {
    type: 'line',
    data: { labels, datasets: [
      { label: 'Skumulowane oszczędności', data: cumData, borderColor: '#16a34a',
        backgroundColor: 'rgba(22,163,74,0.12)', fill: true, pointRadius: 0, borderWidth: 2 },
      { label: 'Cena modułu (' + fmt(cfg.module_price_pln, 0, 'zł') + ')', data: priceLine,
        borderColor: '#dc2626', borderDash: [6, 4], pointRadius: 0, borderWidth: 1.5, fill: false },
    ]},
    options: { responsive: true, maintainAspectRatio: false,
      plugins: { legend: { position: 'top', labels: { boxWidth: 12, font: { size: 11 } } } },
      scales: { x: { ticks: { font: { size: 10 } } }, y: { title: { display: true, text: 'zł' } } } },
  });

  const s3 = bs.s3 || {};
  document.getElementById('batSellBox').innerHTML =
    '<b>&#128176; Scenariusz S3 — sprzedaż z magazynu przy wysokim RCE (informacyjny):</b> ' +
    (s3.breakeven_sell_price_gross != null
      ? 'cykl „zmagazynuj nadwyżkę → sprzedaj w godzinie max RCE" zaczyna się opłacać od ceny sprzedaży <b>'
        + fmt(s3.breakeven_sell_price_gross, 2, 'zł/kWh') + '</b> (RCEm + koszt przerobu, po sprawności). '
        + 'Średnie dzienne maksimum RCE z analizowanych miesięcy: <b>'
        + (s3.months && s3.months.length
            ? fmt(s3.months.reduce((a, m) => a + m.avg_daily_max_rce_gross, 0) / s3.months.length, 2, 'zł/kWh') : '—')
        + '</b>; miesiące z dodatnią marżą: <b>' + (s3.profitable_months || 0) + '/' + (s3.months_analyzed || 0)
        + '</b>' + (s3.avg_margin_per_kwh != null ? ' (śr. marża ' + fmt(s3.avg_margin_per_kwh, 2, 'zł/kWh') + ')' : '') + '. '
        + 'Przy rozliczeniu RCEm sprzedaż z magazynu nie ma sensu (sprzedajesz po średniej miesięcznej) — scenariusz dotyczy przejścia na RCE godzinową.'
      : 'brak danych godzinowych RCE (cache add-onu obejmuje okres od 2024-07).');

  const allRows = bs.s1_months || [];
  let html = '<thead><tr><th>Miesiąc</th><th>Ładowanie kWh</th><th>Rozład. kWh</th>' +
    '<th>Szczyt kWh</th><th>Dolina kWh</th><th>Uniknięty zakup zł</th><th>Utracone RCEm zł</th>' +
    '<th>Arbitraż zł</th><th>Oszczędność zł</th><th>S2 dyn. zł</th><th>Pokrycie h</th></tr></thead><tbody>';
  for (let i = allRows.length - 1; i >= 0; i--) {
    const r = allRows[i];
    html += '<tr' + (r.is_current ? ' class="cur"' : '') + '><td>' + r.ym
      + (r.rcem_estimated ? ' <span class="proj-hint" title="RCEm szacowane (średnia znanych miesięcy)">≈</span>' : '') + '</td>' +
      '<td>' + fmt(r.charged_kwh + (r.arb_charged_kwh || 0), 1) + '</td>' +
      '<td>' + fmt(r.discharged_kwh + (r.arb_discharged_kwh || 0), 1) + '</td>' +
      '<td>' + fmt(r.disch_peak_kwh, 1) + '</td>' +
      '<td>' + fmt(r.disch_offpeak_kwh, 1) + '</td>' +
      '<td>' + fmt(r.avoided_buy_pln, 2) + '</td>' +
      '<td>' + fmt(r.foregone_rcem_pln, 2) + '</td>' +
      '<td>' + fmt(r.arb_savings_pln, 2) + '</td>' +
      '<td><b>' + fmt(r.savings_pln, 2) + '</b></td>' +
      '<td>' + (s2ByYm[r.ym] != null ? fmt(s2ByYm[r.ym], 2) : '—') + '</td>' +
      '<td>' + r.hours + '</td></tr>';
  }
  document.getElementById('batTbl').innerHTML = html + '</tbody>';
  document.getElementById('batFoot').textContent =
    'Ceny stref z zakładki Taryfa (tariff_config), RCEm z historii add-onu; „≈" = miesiąc bez opublikowanej RCEm. ' +
    'S2 liczone tylko dla miesięcy z cenami godzinowymi RCE (od 2024-07).';

  if (!_batCfgLoaded) {
    document.getElementById('batPrice').value  = cfg.module_price_pln;
    document.getElementById('batKwh').value    = cfg.usable_kwh;
    document.getElementById('batKw').value     = cfg.power_kw;
    document.getElementById('batEff').value    = cfg.roundtrip_eff;
    document.getElementById('batCycles').value = cfg.lifetime_cycles;
    document.getElementById('batStart').value  = cfg.start_month;
    document.getElementById('batArb').checked  = !!cfg.arbitrage_enabled;
    document.getElementById('batDynDist').value = cfg.dynamic_dist_gross;
    _batCfgLoaded = true;
  }
}

async function saveBatteryConfig() {
  const msg = document.getElementById('batMsg');
  msg.className = ''; msg.textContent = 'Przeliczanie…';
  const payload = {
    module_price_pln:  parseFloat(document.getElementById('batPrice').value),
    usable_kwh:        parseFloat(document.getElementById('batKwh').value),
    power_kw:          parseFloat(document.getElementById('batKw').value),
    roundtrip_eff:     parseFloat(document.getElementById('batEff').value),
    lifetime_cycles:   parseFloat(document.getElementById('batCycles').value),
    start_month:       document.getElementById('batStart').value,
    arbitrage_enabled: document.getElementById('batArb').checked,
    dynamic_dist_gross: parseFloat(document.getElementById('batDynDist').value),
  };
  try {
    const r = await fetch('api/battery_config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!d.ok) throw new Error(d.error || 'HTTP ' + r.status);
    msg.className = 'ok'; msg.textContent = 'Zapisano i przeliczono ✓';
    _batCfgLoaded = false;
    await loadData();
  } catch (e) {
    msg.className = 'err'; msg.textContent = 'Błąd: ' + e.message;
  }
}

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
    renderInvoicesTab(d.invoices || [], d.tariff_drift, d.records, d.layouts_summary, d.cost_breakdown, d.rate_trend);
    if (d.tariff_comparison) renderTariffTab(d.tariff_comparison);
    if (d.rce_comparison) renderRceTab(d.rce_comparison);
    renderBatteryTab(d.battery_sim);
    renderForecastTab(d.lifetime_forecast, d.summary);
    // v0.17.0
    _lastRecords = d.records || [];
    _lastInvoices = d.invoices || [];
    _lastRateTrend = d.rate_trend || null;
    _lastSummary = d.summary;
    renderFanChart(d.records, d.predictions, d.summary.gross_investment, d.summary.net_investment);
    renderCpiRealChart(d.records);
    renderDegradChart(d.degradation);
    _populateWaterfallSelect(d.records);
    redrawWaterfall();
    _populateSankeySelect(d.records);
    try { redrawSankey(); } catch (e) { console.warn('sankey:', e); }
    renderDepositSection(d.deposit, d.invoices || []);
    // v0.27.0: nowe wykresy
    renderBillChart(d.records);
    renderCo2Chart(d.records, d.summary.co2_factor_kg_kwh || 0.597);
    renderRateTrendChart(d.rate_trend);
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
function renderInvoicesTab(invoices, tariffDrift, records, layoutsSummary, costBreakdown, rateTrend) {
  _renderInvKpiCards(invoices, tariffDrift, records);
  _renderDriftBanner(tariffDrift);
  _renderCoverageGrid(invoices, records);
  _renderCostBreakdown(costBreakdown);
  renderRateTrendChart(rateTrend);
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
<li><strong>Trend degradacji kWh/kWp</strong> — specyficzny uzysk miesięczny z linią trendu; badge ostrzega, gdy trend spadkowy jest szybszy niż zakładana gwarancja producenta (<code>panel_degradation_pct_year</code>)</li>
<li><strong>Ranking produkcji miesięcznej</strong> — kolorowane per rok, medale top 3</li>
</ul>
<h3>📈 Analiza taryf</h3>
<p>Porównanie G12w (szczyt/poza szczytem) z taryfą dynamiczną opartą na godzinowych cenach RCE. Wykres 7-dniowy i tabela podsumowania. Stawki referencyjne G12w pochodzą z ostatniej przetworzonej faktury.</p>
<h3>📑 Taryfa</h3>
<p>Ręczne wpisy stawek z datą obowiązywania — wypełniają lukę ogłoszenia taryfy: nowe stawki Tauron wchodzą 1 stycznia, ale faktura potwierdzająca nadchodzi dopiero w lutym. Ręczny override automatycznie ustępuje po wgraniu faktury za dany miesiąc.<br><strong>Priorytet:</strong> baseline (seed) &lt; faktura &lt; override (aktywny tylko gdy data wpisu jest nowsza niż najnowsza faktura).</p>
<h3>⚡ RCE vs RCEm</h3>
<p>Symulacja przychodów z odsprzedaży przy rozliczeniu godzinowym RCE zamiast miesięcznego RCEm: godzinowa energia eksportowana × ceny 15-min RCE z PSE. Ceny ujemne zastępowane przez 0 zł (art. 4b ustawy o OZE). Heatmapa 24h×miesiąc: kiedy eksportujesz vs kiedy ceny są wysokie lub ujemne. Rekomendacja ROZWAŻ RCE / ZOSTAŃ PRZY RCEm / NEUTRALNA po ≥3 rozliczonych miesiącach.</p>
<p><strong>Druga karta — rekomendacja z uwzględnieniem depozytu:</strong> przejście na RCE godzinową podnosi też limit zwrotu depozytu (30% zamiast 20%). Druga karta łączy różnicę przychodu z rocznym efektem tego wyższego limitu (<code>deposit.calculate(refund_cap=0.30)</code> vs <code>0.20</code>) w jedną liczbę PLN/mies. — te same progi ±10 PLN co rekomendacja podstawowa, ale osobno, bo opiera się na dodatkowym założeniu (przyszłe zwroty depozytu).</p>
<h3>📈 Prognoza 25 lat</h3>
<p>Skumulowany zwrot (subsydium + oszczędności) rok po roku do końca zakładanej żywotności instalacji (<code>asset_lifetime_years</code>, domyślnie 25 lat), z pasmem niepewności P10/P50/P90 — ten sam mechanizm co wachlarz spłaty w zakładce Prognoza spłaty. Degradacja paneli (<code>panel_degradation_pct_year</code>) obniża prognozowaną przyszłą produkcję rok po roku. Historia (lata już przeszłe) to rzeczywiste dane, nie prognoza — pasmo niepewności dotyczy wyłącznie przyszłości.</p>

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

