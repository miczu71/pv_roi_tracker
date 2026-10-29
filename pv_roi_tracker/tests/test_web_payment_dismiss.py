"""Test POST /api/ebok/payment-status/dismiss (zignorowanie rozbieżności wpłat)."""
import pytest

from pv_roi_tracker import web


@pytest.fixture
def client():
    return web.app.test_client()


def test_dismiss_wola_callback_z_sygnatura(client, monkeypatch):
    calls = []
    monkeypatch.setattr(web, '_ebok_payment_dismiss_callback', lambda sig: calls.append(sig) or True)
    r = client.post('/api/ebok/payment-status/dismiss', json={'signature': 'T/K1/BN111111/0015/26'})
    assert r.status_code == 200 and r.get_json() == {'ok': True}
    assert calls == ['T/K1/BN111111/0015/26']


def test_dismiss_nieznana_rozbieznosc_404(client, monkeypatch):
    monkeypatch.setattr(web, '_ebok_payment_dismiss_callback', lambda sig: False)
    r = client.post('/api/ebok/payment-status/dismiss', json={'signature': 'X'})
    assert r.status_code == 404 and r.get_json()['ok'] is False


def test_dismiss_bez_sygnatury_400(client, monkeypatch):
    monkeypatch.setattr(web, '_ebok_payment_dismiss_callback', lambda sig: True)
    assert client.post('/api/ebok/payment-status/dismiss', json={}).status_code == 400


@pytest.mark.parametrize('body', [{'signature': 123}, ['x'], {'signature': '  '}])
def test_dismiss_zle_wejscie_400(client, monkeypatch, body):
    monkeypatch.setattr(web, '_ebok_payment_dismiss_callback', lambda sig: True)
    assert client.post('/api/ebok/payment-status/dismiss', json=body).status_code == 400


def test_dismiss_bez_callbacku_503(client, monkeypatch):
    monkeypatch.setattr(web, '_ebok_payment_dismiss_callback', None)
    assert client.post('/api/ebok/payment-status/dismiss', json={'signature': 'X'}).status_code == 503
