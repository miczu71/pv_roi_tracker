"""Testy GET/POST /api/tariff_settings (E4)."""
import pytest

from pv_roi_tracker import web


@pytest.fixture
def client():
    web.app.config['TESTING'] = True
    return web.app.test_client()


@pytest.fixture
def wired(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(web, '_tariff_settings_path', tmp_path / 'ts.json')
    monkeypatch.setattr(web, '_tariff_settings_callback', calls.append)
    return calls


def test_get_defaults(client, wired):
    j = client.get('/api/tariff_settings').get_json()
    assert j['ok'] is True and j['settings']['phases'] == 3


def test_post_ok_wywoluje_callback(client, wired):
    r = client.post('/api/tariff_settings', json={
        'phases': 1, 'capacity_bracket': 'lt500', 'sales_fee_net': 5,
        'recommend_threshold_pln': 200})
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] is True and j['recomputing'] is True
    assert j['settings']['phases'] == 1
    assert wired == [j['settings']]


def test_post_zly_400(client, wired):
    r = client.post('/api/tariff_settings', json={'phases': 2})
    assert r.status_code == 400
    assert r.get_json()['ok'] is False and 'faz' in r.get_json()['error']
    assert wired == []


def test_nieskonfigurowane_500(client, monkeypatch):
    monkeypatch.setattr(web, '_tariff_settings_path', None)
    monkeypatch.setattr(web, '_tariff_settings_callback', None)
    assert client.post('/api/tariff_settings', json={}).status_code == 500
    assert client.get('/api/tariff_settings').status_code == 500


@pytest.mark.parametrize('body', ['[1, 2]', '', '{zepsuty'])
def test_post_nie_obiekt_400(client, wired, body):
    r = client.post('/api/tariff_settings', data=body, content_type='application/json')
    assert r.status_code == 400
    assert wired == []


def test_post_czesciowy_zachowuje_zapisane_ustawienia(client, wired, tmp_path):
    from pv_roi_tracker import tariff_settings_store as tss
    tss.save({**tss.DEFAULTS, 'sales_fee_net': 12.0, 'recommend_threshold_pln': 321.0},
             tmp_path / 'ts.json')
    r = client.post('/api/tariff_settings', json={'phases': 1})
    assert r.status_code == 200
    assert wired[0]['phases'] == 1
    assert wired[0]['sales_fee_net'] == 12.0 and wired[0]['recommend_threshold_pln'] == 321.0
