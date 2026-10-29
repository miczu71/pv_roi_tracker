"""Testy /api/tariff_config* po scaleniu w moduł taryf (katalog + tariff_overrides.json)."""
import pytest

from pv_roi_tracker import tariff_overrides, web


@pytest.fixture
def client(tmp_path, monkeypatch):
    web.app.config['TESTING'] = True
    monkeypatch.setattr(web, '_tariff_overrides_path', tmp_path / 'ov.json')
    monkeypatch.setattr(web, '_invoice_path', None)
    return web.app.test_client()


def test_get_returns_catalog_entry_first(client):
    j = client.get('/api/tariff_config').get_json()
    assert j['tariffs'][0]['source'] == 'catalog'
    assert j['tariffs'][0]['note'] == 'Katalog (dokument URE/TD)'
    assert 'peak_gross' in j['tariffs'][0]['rates']
    assert j['is_override_active'] is False
    assert j['current_effective_from'] == j['tariffs'][0]['effective_from']


def test_post_get_delete_roundtrip(client, tmp_path):
    r = client.post('/api/tariff_config', json={
        'effective_from': '2027-01', 'note': 'TD 2027',
        'rates': {'peak_gross': 1.31, 'offpeak_gross': 0.67}})
    assert r.status_code == 200 and r.get_json() == {'ok': True}
    stored = tariff_overrides.load(tmp_path / 'ov.json')
    assert stored[0]['from'] == '2027-01-01' and stored[0]['note'] == 'TD 2027'

    manual = [t for t in client.get('/api/tariff_config').get_json()['tariffs'] if t['source'] == 'manual']
    assert manual[0]['effective_from'] == '2027-01'
    assert manual[0]['rates']['peak_gross'] == pytest.approx(1.31, abs=1e-4)
    assert client.get('/api/tariff_config/derived').get_json()['existing_effective_from'] == ['2027-01']

    r = client.post('/api/tariff_config/delete', json={'effective_from': '2027-01'})
    assert r.status_code == 200
    assert tariff_overrides.load(tmp_path / 'ov.json') == []


def test_post_validation_errors_are_400(client):
    assert client.post('/api/tariff_config', json={'effective_from': '2027', 'rates': {}}).status_code == 400
    assert client.post('/api/tariff_config', json={
        'effective_from': '2027-01', 'rates': {'foo': 1}}).status_code == 400
    assert client.post('/api/tariff_config', json={
        'effective_from': '2027-01', 'rates': {'peak_gross': -1}}).status_code == 400
    assert client.post('/api/tariff_config', json={
        'effective_from': '2027-01', 'rates': {'peak_gross': 0.1}}).status_code == 400
    assert client.post('/api/tariff_config/delete', json={}).status_code == 400


def test_not_configured_is_500(client, monkeypatch):
    monkeypatch.setattr(web, '_tariff_overrides_path', None)
    assert client.post('/api/tariff_config', json={'effective_from': '2027-01', 'rates': {}}).status_code == 500
    assert client.post('/api/tariff_config/delete', json={'effective_from': '2027-01'}).status_code == 500
