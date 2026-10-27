"""Tests for the v0.32.0 static-asset split — web.py used to serve one giant
embedded HTML/JS/CSS string; now it reads static/{index.html,app.js,app.css}
and static/vendor/*.js from disk once at import time and serves them via
dedicated routes with explicit Cache-Control headers (see CLAUDE.md's mobile
WebView caching convention — the HTML shell and /api/* must never be cached,
versioned static assets are safe to cache forever).
"""
import pytest

from pv_roi_tracker import web


@pytest.fixture
def client():
    web.app.config['TESTING'] = True
    return web.app.test_client()


def test_index_returns_html_with_no_store(client):
    resp = client.get('/')
    assert resp.status_code == 200
    assert resp.headers['Content-Type'] == 'text/html; charset=utf-8'
    assert resp.headers['Cache-Control'] == 'no-store'
    assert b'<!DOCTYPE html>' in resp.data


def test_index_has_version_substituted_not_placeholder(client):
    resp = client.get('/')
    body = resp.data.decode('utf-8')
    assert '{{VERSION}}' not in body
    assert f'app.js?v={web.__version__}' in body
    assert f'app.css?v={web.__version__}' in body
    assert f'vendor/chart.umd.min.js?v={web.__version__}' in body
    assert f'vendor/chartjs-chart-sankey.min.js?v={web.__version__}' in body


def test_index_references_are_relative_not_absolute():
    """Ingress serves the add-on under a dynamic path prefix
    (/api/hassio_ingress/<token>/...) — any leading-slash asset reference
    would 404 under that prefix. Guard against reintroducing one."""
    body = web._INDEX_HTML
    for needle in ('src="/app.js', 'src="/vendor/', 'href="/app.css'):
        assert needle not in body, f'found ingress-breaking absolute path: {needle}'


def test_app_js_served_with_immutable_cache(client):
    resp = client.get('/app.js')
    assert resp.status_code == 200
    assert resp.headers['Content-Type'] == 'application/javascript; charset=utf-8'
    assert resp.headers['Cache-Control'] == 'public, max-age=31536000, immutable'
    assert b"'use strict'" in resp.data


def test_app_css_served_with_immutable_cache(client):
    resp = client.get('/app.css')
    assert resp.status_code == 200
    assert resp.headers['Content-Type'] == 'text/css; charset=utf-8'
    assert resp.headers['Cache-Control'] == 'public, max-age=31536000, immutable'
    assert b':root' in resp.data


@pytest.mark.parametrize('filename', ['chart.umd.min.js', 'chartjs-chart-sankey.min.js'])
def test_vendor_files_served_with_immutable_cache(client, filename):
    resp = client.get(f'/vendor/{filename}')
    assert resp.status_code == 200
    assert resp.headers['Cache-Control'] == 'public, max-age=31536000, immutable'
    assert len(resp.data) > 1000


def test_vendor_unknown_file_returns_404(client):
    resp = client.get('/vendor/does-not-exist.js')
    assert resp.status_code == 404


def test_api_data_has_no_store(client):
    """Even the 202-loading response before any poll has run must never be cached."""
    resp = client.get('/api/data')
    assert resp.headers['Cache-Control'] == 'no-store'


def test_no_duplicated_charset_in_content_type(client):
    """Regression guard: Werkzeug appends '; charset=utf-8' to text/* mimetypes
    unconditionally, so passing an already-charset-qualified mimetype string
    (the pre-0.32.0 pattern) produced 'text/html; charset=utf-8; charset=utf-8'."""
    for path in ('/', '/app.css'):
        ct = client.get(path).headers['Content-Type']
        assert ct.count('charset=') == 1, f'{path} Content-Type duplicated charset: {ct}'


def _static(name):
    from pathlib import Path
    return (Path(web.__file__).parent / 'static' / name).read_text(encoding='utf-8')


def test_tsim_tab_wired(client):
    html = client.get('/').data.decode('utf-8')
    assert 'id="tab-tsim"' in html
    js = _static('app.js')
    # przycisk zakładki renderowany z ROUTES (slug symulacja -> panel tsim)
    assert "['symulacja',  'tsim'" in js
    assert 'loadTsimTab' in js and "name === 'tsim'" in js


def test_tariff_analysis_tab_removed(client):
    html = client.get('/').data.decode('utf-8')
    assert 'id="tab-tariff"' not in html
    js = _static('app.js')
    for needle in ('renderTariffTab', 'fetchTariffRange', 'api/tariff_stats'):
        assert needle not in js, needle


def _settings_fragment(html):
    start = html.index('id="tab-settings"')
    return html[start:]


def test_settings_panel_contains_moved_blocks(client):
    html = client.get('/').data.decode('utf-8')
    assert html.count('id="tab-settings"') == 1
    frag = _settings_fragment(html)
    for needle in ('submitOverride', 'saveTsimSettings', 'saveBatteryConfig', 'id="taryfaForm"',
                   'ebokSync()', 'id="invLayoutsPanel"', 'runInvoiceDebug()'):
        assert needle in frag, needle
    # upload PDF zostaje w Fakturach, poza Ustawieniami
    assert 'uploadInvoices()' not in frag
    assert 'uploadInvoices()' in html


def test_settings_ids_unique(client):
    html = client.get('/').data.decode('utf-8')
    for i in ('ovMonth', 'ovPrice', 'ovMsg', 'rcemBadge', 'tsimPhases', 'tsimBracket', 'tsimFee',
              'tsimThreshold', 'tsimMsg', 'batPrice', 'batKwh', 'batKw', 'batEff', 'batCycles',
              'batStart', 'batArb', 'batDynDist', 'batMsg', 'ebokSyncBtn', 'ebokMsg',
              'invLayoutsPanel', 'debugPdfFile', 'debugOutput', 'taryfaFormTitle', 'taryfaForm',
              'taryfaMsg'):
        assert html.count(f'id="{i}"') == 1, i


def test_routes_defined_and_showtab_adapter():
    js = _static('app.js')
    for slug in ('historia', 'prognoza', 'roczne', 'wykresy', 'rce', 'magazyn', 'prognoza25',
                 'pompa', 'symulacja', 'faktury', 'taryfa'):
        assert f"['{slug}'" in js, slug
    assert 'const ROUTES' in js and 'function applyRoute' in js and 'function showTab' in js
    assert "addEventListener('hashchange'" in js
    html = _static('index.html')
    assert 'id="secNav"' in html and 'id="tabsBar"' in html and 'aria-label="Ustawienia"' in html


def test_e3_summary_taryfa_invoices_wired():
    html = _static('index.html')
    js = _static('app.js')
    for i in ('taryfySummary', 'taryfaRates', 'taryfaWeekChart', 'taryfaList', 'taryfaBadge', 'taryfaCennik',
              'invKpiCards', 'depKpiCards', 'depositChart', 'reconKpiCards', 'invCoverageGrid',
              'tariffDriftBanner', 'costBreakdownChart', 'rateTrendChart', 'invoiceTableWrap'):
        assert html.count(f'id="{i}"') == 1, i
    assert html.index('id="invoiceTableWrap"') < html.index('id="costBreakdownChart"') \
        < html.index('id="rateTrendChart"') < html.index('id="depositChart"')
    assert 'function renderTaryfySummary' in js and 'function renderDepositTile' in js
    assert 'stepped: true' in js


def test_e4_tsim_controls_and_charts_ids(client):
    body = client.get('/').data.decode('utf-8')
    for i in ('tsimCtl', 'tsimZoneMap', 'tsimPriceChart', 'tsimZoneCost', 'tsimDiffChart', 'tsimHourlyMsg',
              'tsimMapTariff', 'tsimReco', 'tsimTbl', 'tsimChart', 'tsimBreakMonth', 'tsimBreakTbl', 'tsimBreakMsg',
              'tsimReconSummary', 'tsimReconTbl', 'tsimBtnA', 'tsimBtnB', 'tsimVariantSwitch'):
        assert f'id="{i}"' in body, i
    # przełącznik wariantu mieszka w pasku sterowania
    ctl = body[body.index('id="tsimCtl"'):body.index('id="tsimHourlyMsg"')]
    assert 'id="tsimVariantSwitch"' in ctl


def test_g13s_in_tsim_lists():
    js = _static('app.js')
    all_line = next(l for l in js.splitlines() if l.startswith('const _TSIM_ALL'))
    assert "'G13s'" in all_line
    colors_line = next(l for l in js.splitlines() if l.startswith('const _TSIM_COLORS'))
    assert "G13s: '#0891b2'" in colors_line


def test_e3_tsim_variant_c_plan_best_ids_and_js():
    html = _static('index.html')
    for i in ('tsimBtnC', 'tsimPlan', 'tsimBest', 'tsimFeeG13s'):
        assert html.count(f'id="{i}"') == 1, i
    js = _static('app.js')
    for needle in ('_tsimHasC', '_tsimSpan', 'best_year'):
        assert needle in js, needle
