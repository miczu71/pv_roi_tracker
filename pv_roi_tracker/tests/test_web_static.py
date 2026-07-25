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
