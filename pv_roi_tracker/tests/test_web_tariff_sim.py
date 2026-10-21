"""Test GET /api/tariff_sim — brak payloadu to 200 z data: None, nie 500."""
import pytest

from pv_roi_tracker import web


@pytest.fixture
def client():
    return web.app.test_client()


@pytest.fixture(autouse=True)
def _reset():
    web.update_tariff_sim(None)
    yield
    web.update_tariff_sim(None)


def test_brak_payloadu(client):
    r = client.get('/api/tariff_sim')
    assert r.status_code == 200
    assert r.get_json() == {'ok': True, 'data': None}


def test_z_payloadem(client, tmp_path, monkeypatch):
    monkeypatch.setattr(web, '_tariff_settings_path', tmp_path / 'ts.json')
    payload = {'to': '2026-10', 'last12': {'cheapest': 'G12w',
                                           'gross': {'G12w': 1000.0, 'G13': 500.0}}}
    web.update_tariff_sim(payload)
    r = client.get('/api/tariff_sim')
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] is True and j['data'] == payload
    assert j['recommendation']['A']['best'] == 'G13'
    assert j['recommendation']['A']['switch'] is True
    assert j['recommendation']['B'] is None
    assert j['settings']['phases'] == 3
