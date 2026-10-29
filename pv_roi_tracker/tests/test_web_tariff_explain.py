"""Testy GET /api/tariff/rates, /api/tariff/breakdown, /api/tariff/compare (T6)."""
from datetime import date

import pytest

from pv_roi_tracker import tariff_invoice_check as tic
from pv_roi_tracker import web
from pv_roi_tracker.tariff_catalog import TariffSettings, load_catalog
from pv_roi_tracker.tariff_module import TariffModule


@pytest.fixture
def client():
    web.app.config['TESTING'] = True
    return web.app.test_client()


@pytest.fixture
def wired(monkeypatch, tmp_path):
    cat = load_catalog()
    module = TariffModule(cat, default_tariff='G12w')
    rates = module.legacy_rates(date(2026, 10, 15))
    inv = {f: rates[f] for f in tic.CHECK_FIELDS}
    inv['invoice_number'] = 'T/1'
    path = tmp_path / 'invoices.json'
    path.write_text('{"schema_version": 2, "invoices": {"2026-10": %s}}' % __import__('json').dumps(inv))
    inputs = {'import_by_hour': {'2026-03-10T10': 2.0, '2026-03-10T23': 1.0}, 'catalog': cat,
              'settings': TariffSettings(), 'baseline': 'G12w'}
    monkeypatch.setattr(web, '_tariff_inputs_callback', lambda: inputs)
    monkeypatch.setattr(web, '_tariff_module', lambda: module)
    monkeypatch.setattr(web, '_invoice_path', path)


def test_rates_ok_i_filtry(client, wired):
    j = client.get('/api/tariff/rates?tariff=G12w&zone=peak&component=energy&from=2026-03-01').get_json()
    assert j['ok'] and j['count'] == 1 and j['rows'][0]['net'] == 0.622
    assert client.get('/api/tariff/rates?q=akcyza&tariff=G11').get_json()['count'] >= 1


def test_breakdown_godzina_g12w(client, wired):
    j = client.get('/api/tariff/breakdown?tariff=G12w&at=2026-03-10T10').get_json()
    assert j['ok'] and j['scope'] == 'hour' and j['total_gross'] == pytest.approx(1.2304, abs=5e-5)
    n = client.get('/api/tariff/breakdown?at=2026-03-10T23&basis=net').get_json()
    assert n['tariff'] == 'G12w' and all(c['key'] != 'vat' for c in n['components'])


def test_breakdown_month_i_invoice(client, wired):
    j = client.get('/api/tariff/breakdown?month=2026-03&include=energy,dist').get_json()
    assert j['kwh'] == 3.0
    assert {c['key'] for c in j['components'] if c['included']} == {'energy', 'dist'}
    k = client.get('/api/tariff/breakdown?invoice=2026-10').get_json()
    assert k['ok'] and k['scope'] == 'invoice' and 'T/1' in k['label'] and k['kwh'] == 0


def test_compare_trzy_tryby(client, wired):
    t = client.get('/api/tariff/compare?mode=tariff&a=G12w&b=G13&scope=month&at=2026-03').get_json()
    assert t['ok'] and t['diff'] == pytest.approx(t['a']['total'] - t['b']['total'], abs=1e-5)
    p = client.get('/api/tariff/compare?mode=period&tariff=G12w&zone=peak&from=2025-06-01&to=2026-03-01').get_json()
    assert p['ok'] and len(p['periods']) >= 2
    i = client.get('/api/tariff/compare?mode=invoice&month=2026-10').get_json()
    assert i['status'] == 'match' and i['invoice_no'] == 'T/1'
    assert client.get('/api/tariff/compare?mode=invoice&month=2026-09').get_json()['status'] == 'no_invoice'


@pytest.mark.parametrize('url', [
    '/api/tariff/rates?tariff=G99', '/api/tariff/rates?from=zly', '/api/tariff/rates?confidence=x',
    '/api/tariff/breakdown', '/api/tariff/breakdown?month=2026-03&day=2026-03-10',
    '/api/tariff/breakdown?month=2026-3', '/api/tariff/breakdown?month=2026-03&include=zly',
    '/api/tariff/breakdown?month=2026-03&basis=x', '/api/tariff/breakdown?invoice=nie-ma',
    '/api/tariff/compare', '/api/tariff/compare?mode=x', '/api/tariff/compare?mode=tariff&a=G12w&scope=month',
    '/api/tariff/compare?mode=tariff&a=G12w&b=G13&scope=week&at=1',
    '/api/tariff/compare?mode=tariff&a=G12w&b=G13&scope=month&at=2026-13',
    '/api/tariff/compare?mode=period&tariff=G12w&zone=zzz', '/api/tariff/compare?mode=invoice&month=2026',
])
def test_bledne_parametry_400(client, wired, url):
    r = client.get(url)
    assert r.status_code == 400
    assert r.get_json()['ok'] is False and r.get_json()['error']


def test_brak_callbacku_503(client, monkeypatch):
    monkeypatch.setattr(web, '_tariff_inputs_callback', None)
    assert client.get('/api/tariff/breakdown?month=2026-03').status_code == 503
    assert client.get('/api/tariff/compare?mode=tariff&a=G12w&b=G13&scope=month&at=2026-03').status_code == 503
