"""Tests for the v0.35.0 /api/historic/simulate-rebase and /api/historic/apply-rebase
routes — thin Flask wrappers around rebase.simulate()/apply() dispatched
through the callback-injection pattern already used by /api/historic/reread-month.
"""
import pytest

from pv_roi_tracker import web


@pytest.fixture
def client():
    web.app.config['TESTING'] = True
    return web.app.test_client()


@pytest.fixture(autouse=True)
def _reset_callbacks():
    """Callbacks are module globals set by main() at boot — clear them after
    each test so one test can't leak its stub into another."""
    yield
    web._simulate_rebase_callback = None
    web._apply_rebase_callback = None


def test_simulate_rebase_503_when_not_initialized(client):
    web._simulate_rebase_callback = None
    resp = client.post('/api/historic/simulate-rebase')
    assert resp.status_code == 503


def test_simulate_rebase_returns_callback_report(client):
    fake_report = {'months': [{'ym': '2026-07', 'before': {}, 'after': {}, 'delta': {}}],
                   'unavailable': [], 'still_broken': [],
                   'roi_before': {'roi_pct': 80.0}, 'roi_after': {'roi_pct': 81.0}}
    web.set_simulate_rebase_callback(lambda: fake_report)
    resp = client.post('/api/historic/simulate-rebase')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['ok'] is True
    assert body['months'][0]['ym'] == '2026-07'
    assert body['roi_after']['roi_pct'] == 81.0


def test_simulate_rebase_500_on_callback_exception(client):
    def _boom():
        raise RuntimeError('LTS unreachable')
    web.set_simulate_rebase_callback(_boom)
    resp = client.post('/api/historic/simulate-rebase')
    assert resp.status_code == 500
    assert resp.get_json()['ok'] is False


def test_apply_rebase_503_when_not_initialized(client):
    web._apply_rebase_callback = None
    resp = client.post('/api/historic/apply-rebase')
    assert resp.status_code == 503


def test_apply_rebase_returns_callback_report(client):
    fake_report = {'months': [], 'unavailable': [], 'still_broken': [],
                   'roi_before': {}, 'roi_after': {}, 'snapshot_path': '/data/historic.pre-rebase-x.json'}
    web.set_apply_rebase_callback(lambda: fake_report)
    resp = client.post('/api/historic/apply-rebase')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['ok'] is True
    assert body['snapshot_path'] == '/data/historic.pre-rebase-x.json'


def test_apply_rebase_500_on_callback_exception(client):
    def _boom():
        raise OSError('disk full')
    web.set_apply_rebase_callback(_boom)
    resp = client.post('/api/historic/apply-rebase')
    assert resp.status_code == 500
    assert resp.get_json()['ok'] is False
