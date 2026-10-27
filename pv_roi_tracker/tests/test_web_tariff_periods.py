"""Testy GET/PUT /api/tariff_periods."""
import pytest

from pv_roi_tracker import tariff_history as th
from pv_roi_tracker import web


@pytest.fixture
def client():
    return web.app.test_client()


@pytest.fixture
def wired(tmp_path, monkeypatch):
    path = tmp_path / 'tariff_periods.json'
    saved = []
    monkeypatch.setattr(web, '_tariff_periods_path', path)
    monkeypatch.setattr(web, '_tariff_periods_callback', lambda periods: saved.append(periods))
    monkeypatch.setattr(web, '_tariff_override', '')
    return path, saved


def test_get_brak_pliku(client, wired):
    j = client.get('/api/tariff_periods').get_json()
    assert j == {'ok': True, 'periods': [], 'current_tariff': None, 'override': None}


def test_get_z_okresami_i_override(client, wired, monkeypatch):
    path, _ = wired
    th.save([{'tariff': 'G12w', 'from': None}, {'tariff': 'G13', 'from': '2026-03-01'}], path)
    j = client.get('/api/tariff_periods').get_json()
    assert j['current_tariff'] == 'G13' and j['override'] is None
    assert len(j['periods']) == 2
    monkeypatch.setattr(web, '_tariff_override', 'G11')
    j = client.get('/api/tariff_periods').get_json()
    assert j['current_tariff'] == 'G11' and j['override'] == 'G11'


def test_put_ok_wola_callback(client, wired):
    _, saved = wired
    body = {'periods': [{'tariff': 'g12w', 'from': None}, {'tariff': 'G13', 'from': '2026-03-01'}]}
    r = client.put('/api/tariff_periods', json=body)
    assert r.status_code == 200
    j = r.get_json()
    assert j['ok'] is True
    assert j['periods'] == [{'tariff': 'G12w', 'from': None}, {'tariff': 'G13', 'from': '2026-03-01'}]
    assert saved == [j['periods']]


@pytest.mark.parametrize('body', [
    {'periods': []},
    {'periods': [{'tariff': 'XX', 'from': None}]},
    {'periods': [{'tariff': 'G12w', 'from': None}, {'tariff': 'G13', 'from': 'zle'}]},
    {'nope': 1},
    [1, 2],
])
def test_put_400(client, wired, body):
    _, saved = wired
    r = client.put('/api/tariff_periods', json=body)
    assert r.status_code == 400
    assert r.get_json()['ok'] is False and r.get_json()['error']
    assert saved == []


def test_nie_podlaczone_500(client, monkeypatch):
    monkeypatch.setattr(web, '_tariff_periods_path', None)
    monkeypatch.setattr(web, '_tariff_periods_callback', None)
    assert client.get('/api/tariff_periods').status_code == 500
    assert client.put('/api/tariff_periods', json={'periods': []}).status_code == 500
