"""Testy /api/tariff_guard*, /api/tariff_period* (strażnik cenników i formularz nowego okresu)."""
from datetime import date, datetime

import pytest

from pv_roi_tracker import tariff_guard, tariff_overrides, web


@pytest.fixture
def client(tmp_path, monkeypatch):
    web.app.config['TESTING'] = True
    monkeypatch.setattr(web, '_tariff_overrides_path', tmp_path / 'ov.json')
    monkeypatch.setattr(web, '_tariff_guard_path', tmp_path / 'guard.json')
    monkeypatch.setattr(web, '_tariff_periods_path', None)
    monkeypatch.setattr(web, '_tariff_settings_path', None)
    monkeypatch.setattr(web, '_tariff_override', '')
    monkeypatch.setattr(web, '_invoice_path', None)
    return web.app.test_client()


def _fake_now(monkeypatch, year, month, day):
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(year, month, day, 12)
    monkeypatch.setattr(web, 'datetime', FakeDT)


# ── strażnik ─────────────────────────────────────────────────────────────────

def test_guard_get_not_assumed_today(client, monkeypatch):
    _fake_now(monkeypatch, 2026, 10, 29)
    j = client.get('/api/tariff_guard').get_json()
    assert j['ok'] is True and j['state'] == {}
    assert j['assumed'] == {'active': False, 'period_valid_to': None, 'tariff': 'G12w'}


def test_guard_get_assumed_after_catalog_end(client, monkeypatch):
    _fake_now(monkeypatch, 2027, 3, 10)
    j = client.get('/api/tariff_guard').get_json()
    assert j['assumed']['active'] is True
    assert j['assumed']['period_valid_to'] == '2026-12-31'


def test_guard_get_returns_state(client, tmp_path):
    tariff_guard.save_state({'pages': {}, 'new_documents': [{'page': 'td', 'url': 'u', 'seen': 's'}]},
                            tmp_path / 'guard.json')
    assert client.get('/api/tariff_guard').get_json()['state']['new_documents'][0]['url'] == 'u'


def test_guard_ack_clears_new_documents(client, tmp_path):
    tariff_guard.save_state({'pages': {}, 'new_documents': [{'page': 'td', 'url': 'u', 'seen': 's'}]},
                            tmp_path / 'guard.json')
    assert client.post('/api/tariff_guard/ack').get_json() == {'ok': True}
    assert tariff_guard.load_state(tmp_path / 'guard.json')['new_documents'] == []


def test_guard_not_wired_500(client, monkeypatch):
    monkeypatch.setattr(web, '_tariff_guard_path', None)
    assert client.get('/api/tariff_guard').status_code == 500
    assert client.post('/api/tariff_guard/ack').status_code == 500


# ── formularz ────────────────────────────────────────────────────────────────

def test_form_g13_zones_and_values(client):
    j = client.get('/api/tariff_period/form?tariff=G13').get_json()
    assert j['ok'] is True and j['tariff'] == 'G13'
    assert j['zones']['energy_net'] == ['morning_peak', 'evening_peak', 'other']
    assert set(j['zones']['dist_var_net']) == {'morning_peak', 'evening_peak', 'other'}
    assert j['values']['energy_net']['G13']['other'] > 0
    assert set(j['values']['fixed_net']) == {'network_1ph', 'network_3ph', 'subscription_dist'}
    for k in ('quality_net', 'oze_net', 'cogeneration_net', 'excise_net'):
        assert j['values'][k] >= 0
    first = date.fromisoformat(j['from_default'])
    assert first.day == 1 and first > date.today()


def test_form_bad_tariff_400(client):
    assert client.get('/api/tariff_period/form?tariff=G99').status_code == 400
    assert client.get('/api/tariff_period/form').status_code == 400


# ── podgląd ──────────────────────────────────────────────────────────────────

def _patch_g12w(peak=0.9):
    return {'energy_net': {'G12w': {'peak': peak}}}


def test_preview_does_not_write_and_uses_patch(client, tmp_path):
    r = client.post('/api/tariff_period/preview',
                    json={'from': '2027-01-01', 'tariff': 'G12w', 'patch': _patch_g12w(0.9)})
    j = r.get_json()
    assert r.status_code == 200 and j['ok'] is True
    assert [s['label'] for s in j['samples']] == ['wtorek 08:00', 'wtorek 14:00', 'sobota 12:00', 'wtorek 23:00']
    assert j['samples'][0]['zone'] == 'peak' and j['samples'][1]['zone'] == 'offpeak'
    assert j['samples'][0]['dt'].startswith('2027-01-05T08')   # pierwszy wtorek od 2027-01-01
    base = client.post('/api/tariff_period/preview',
                       json={'from': '2027-01-01', 'tariff': 'G12w', 'patch': {}}).get_json()
    assert j['samples'][0]['gross_per_kwh'] > base['samples'][0]['gross_per_kwh']
    assert not (tmp_path / 'ov.json').exists()


def test_preview_g13_samples(client):
    j = client.post('/api/tariff_period/preview',
                    json={'from': '2027-01-01', 'tariff': 'G13', 'patch': {}}).get_json()
    assert [s['label'] for s in j['samples']] == ['wtorek 08:00', 'wtorek 15:00', 'wtorek 20:00', 'sobota 12:00']
    assert j['samples'][0]['zone'] == 'morning_peak'


@pytest.mark.parametrize('body', [
    {'from': '2027-01-01', 'tariff': 'XX', 'patch': {}},
    {'from': '2027-01-15', 'tariff': 'G12w', 'patch': {}},
    {'from': '2027-01-01', 'tariff': 'G12w', 'patch': _patch_g12w(-1)},
])
def test_preview_400(client, body):
    r = client.post('/api/tariff_period/preview', json=body)
    assert r.status_code == 400 and r.get_json()['ok'] is False


# ── zapis ────────────────────────────────────────────────────────────────────

def test_save_writes_entry_and_price_is_manual(client, tmp_path, monkeypatch):
    r = client.post('/api/tariff_period', json={'from': '2027-01-01', 'note': 'TD 2027',
                                                'patch': _patch_g12w(0.9)})
    assert r.status_code == 200 and r.get_json() == {'ok': True}
    stored = tariff_overrides.load(tmp_path / 'ov.json')
    assert stored == [{'from': '2027-01-01', 'to': None, 'note': 'TD 2027', 'patch': _patch_g12w(0.9)}]
    p = web._tariff_module().price_at(datetime(2027, 1, 5, 8))
    assert p.confidence == 'manual'
    assert p.components['energy'] == pytest.approx(0.9)


def test_save_not_first_day_400(client, tmp_path):
    r = client.post('/api/tariff_period', json={'from': '2027-01-15', 'patch': _patch_g12w()})
    assert r.status_code == 400 and '1. dnia' in r.get_json()['error']
    assert not (tmp_path / 'ov.json').exists()


def test_save_negative_rate_400(client, tmp_path):
    r = client.post('/api/tariff_period', json={'from': '2027-01-01', 'patch': _patch_g12w(-0.1)})
    assert r.status_code == 400 and r.get_json()['ok'] is False
    assert not (tmp_path / 'ov.json').exists()


def test_save_bad_json_400(client):
    assert client.post('/api/tariff_period', json={'patch': {}}).status_code == 400


def test_save_not_wired_500(client, monkeypatch):
    monkeypatch.setattr(web, '_tariff_overrides_path', None)
    assert client.post('/api/tariff_period', json={'from': '2027-01-01', 'patch': {}}).status_code == 500
