"""Testy GET /api/tariff_hourly, /api/tariff_now, /api/tariff_catalog (E1)."""
from datetime import date, datetime

import pytest

from pv_roi_tracker import price_sources, tariff_sim, web
from pv_roi_tracker.tariff_catalog import TariffSettings, load_catalog

TODAY = date(2026, 10, 26)


class _Cache:
    rdn: dict = {}
    kompas: dict = {}
    eurpln = {'2026-10-25': 4.2}


@pytest.fixture
def client():
    web.app.config['TESTING'] = True
    return web.app.test_client()


@pytest.fixture
def wired(monkeypatch):
    web._tariff_hourly_cache.clear()
    cat = load_catalog()
    inputs = {'import_by_hour': {'2026-03-10T10': 1.0}, 'catalog': cat,
              'settings': TariffSettings(), 'rdn': {}, 'kompas': {}, 'overrides': None,
              'price_cache': _Cache()}
    monkeypatch.setattr(web, '_tariff_inputs_callback', lambda: inputs)
    monkeypatch.setattr(web, '_today', lambda: TODAY)
    monkeypatch.setattr(web, '_now', lambda: datetime(2026, 10, 26, 12, 30))
    return inputs


def test_brak_callbacku_503(client, monkeypatch):
    monkeypatch.setattr(web, '_tariff_inputs_callback', None)
    assert client.get('/api/tariff_hourly?from=2026-03-10&to=2026-03-10').status_code == 503
    assert client.get('/api/tariff_now').status_code == 503
    assert client.get('/api/tariff_catalog').status_code == 503


@pytest.mark.parametrize('qs', [
    'from=2026-3-10&to=2026-03-10', 'from=2026-03-10', 'from=xx&to=2026-03-10',
    'from=2026-03-11&to=2026-03-10', 'from=2025-01-01&to=2026-03-10',
    'from=2026-03-10&to=2026-03-10&tariffs=G99'])
def test_walidacja_400(client, wired, qs):
    r = client.get('/api/tariff_hourly?' + qs)
    assert r.status_code == 400
    assert r.get_json()['ok'] is False and r.get_json()['error']


def test_limit_400_dni_granica(client, wired):
    # 400 dni dozwolone, 401 nie
    assert client.get('/api/tariff_hourly?from=2025-02-01&to=2026-03-07&tariffs=G11').status_code == 200
    assert client.get('/api/tariff_hourly?from=2025-02-01&to=2026-03-08&tariffs=G11').status_code == 400


def test_odpowiedz_i_hours_tylko_do_31_dni(client, wired):
    j = client.get('/api/tariff_hourly?from=2026-03-10&to=2026-03-11&tariffs=G11,G12w').get_json()
    assert j['ok'] is True and j['from'] == '2026-03-10' and j['to'] == '2026-03-11'
    assert set(j['zone_map']) == {'G11', 'G12w'}
    assert len(j['zone_map']['G12w']['cells']['2026-03-10']) == 24
    assert j['cost_by_zone']['G12w']['zones']['peak']['kwh'] == 1.0
    assert len(j['hours']['G11']) == 48
    assert j['cumulative_diff']['days'] == ['2026-03-10', '2026-03-11']
    assert 'tomorrow' not in j or j['tomorrow'] is None
    j2 = client.get('/api/tariff_hourly?from=2026-03-01&to=2026-04-01&tariffs=G11').get_json()
    assert j2['hours'] is None and 'cumulative_diff' not in j2 or j2['cumulative_diff'] is None
    j3 = client.get('/api/tariff_hourly?from=2026-03-01&to=2026-03-31&tariffs=G11').get_json()
    assert j3['hours'] is not None


def test_domyslnie_wszystkie_taryfy(client, wired):
    j = client.get('/api/tariff_hourly?from=2026-05-13&to=2026-05-13').get_json()
    assert set(j['zone_map']) == set(tariff_sim.ALL_TARIFFS)


def test_cache_drugie_wywolanie_nie_liczy(client, wired, monkeypatch):
    calls = []
    orig = tariff_sim.hourly_series
    monkeypatch.setattr(tariff_sim, 'hourly_series', lambda *a, **k: calls.append(1) or orig(*a, **k))
    url = '/api/tariff_hourly?from=2026-03-10&to=2026-03-10&tariffs=G11'
    a = client.get(url).get_json()
    b = client.get(url).get_json()
    assert a == b and len(calls) == 1
    wired['rdn'] = {'2026-03-10T10': 1.0}          # zmiana wersji danych → nowy klucz
    client.get(url)
    assert len(calls) == 2


def test_cache_maks_32_wpisy(client, wired):
    for d in range(1, 29):
        client.get(f'/api/tariff_hourly?from=2026-03-{d:02d}&to=2026-03-{d:02d}&tariffs=G11')
    for d in range(1, 10):
        client.get(f'/api/tariff_hourly?from=2026-04-{d:02d}&to=2026-04-{d:02d}&tariffs=G11')
    assert len(web._tariff_hourly_cache) == 32


def test_jutro_bez_rdn_niedostepne_i_bez_cache(client, wired, monkeypatch):
    calls = []

    def fake_ahead(day, cache, http_get=None):
        calls.append(day)
        return {'rdn': {}, 'kompas': {}, 'eurpln_fallback': False, 'eurpln_used': 4.2,
                'rdn_available': False, 'reason': 'RDN niedostępne: test'}

    monkeypatch.setattr(price_sources, 'fetch_ahead', fake_ahead)
    url = '/api/tariff_hourly?from=2026-10-27&to=2026-10-27&tariffs=G12w,dyn_a'
    j = client.get(url).get_json()
    assert j['ok'] is True
    assert j['tomorrow']['rdn_available'] is False and 'test' in j['tomorrow']['reason']
    assert j['zone_map']['dyn_a']['days'] == []
    assert len(j['zone_map']['G12w']['cells']['2026-10-27']) == 24
    assert j['cost_by_zone']['G12w']['zones'] == {}          # kWh = 0, bez kosztów
    client.get(url)
    assert len(calls) == 2                                     # nie cache'owane


def test_jutro_z_rdn(client, wired, monkeypatch):
    rdn = {f'2026-10-27T{h:02d}': 500.0 for h in range(24)}
    monkeypatch.setattr(price_sources, 'fetch_ahead', lambda day, cache, http_get=None: {
        'rdn': rdn, 'kompas': {}, 'eurpln_fallback': True, 'eurpln_used': 4.2,
        'rdn_available': True, 'reason': None})
    j = client.get('/api/tariff_hourly?from=2026-10-27&to=2026-10-27&tariffs=dyn_a').get_json()
    assert j['tomorrow']['rdn_available'] is True and j['tomorrow']['eurpln_fallback'] is True
    cell = j['zone_map']['dyn_a']['cells']['2026-10-27'][10]
    assert cell['net_per_kwh'] == pytest.approx(0.5 + 0.0892 + 0.3298 + 0.0332 + 0.0073 + 0.0030 + 0.005)


def test_zakres_konczacy_sie_dzis_dociaga_rdn_dnia_biezacego_i_cache_10min(client, wired, monkeypatch):
    calls = []
    rdn = {f'2026-10-26T{h:02d}': 400.0 for h in range(24)}

    def fake_ahead(day, cache, http_get=None):
        calls.append(day)
        return {'rdn': rdn, 'kompas': {}, 'eurpln_fallback': False, 'eurpln_used': 4.2,
                'rdn_available': True, 'reason': None}

    monkeypatch.setattr(price_sources, 'fetch_ahead', fake_ahead)
    url = '/api/tariff_hourly?from=2026-10-20&to=2026-10-26&tariffs=G12w,dyn_a'
    j = client.get(url).get_json()
    assert calls == [TODAY]                                    # tylko dziś; jutra nie dotyczy
    assert 'tomorrow' not in j
    cell = j['zone_map']['dyn_a']['cells']['2026-10-26'][10]
    assert cell['net_per_kwh'] is not None
    client.get(url)
    assert len(calls) == 1                                     # drugi odczyt z cache (kubełek 10 min)


def test_tariff_now(client, wired):
    j = client.get('/api/tariff_now').get_json()
    assert j['ok'] is True and j['now'] == '2026-10-26T12' and j['tariff'] == 'G12w'
    assert j['zone'] == 'peak'                       # poniedziałek 12:30
    assert j['net_per_kwh'] == pytest.approx(0.6220 + 0.3298 + 0.0332 + 0.0073 + 0.0030 + 0.005)
    assert j['gross_per_kwh'] == pytest.approx(j['net_per_kwh'] * 1.23)
    assert j['next_change'] == {'at': '2026-10-26T13', 'zone': 'offpeak'}


def test_tariff_catalog(client, wired):
    j = client.get('/api/tariff_catalog').get_json()
    assert j['ok'] is True and j['meta']['vat'] == 1.23
    assert 'G12w' in j['zones']
    assert len(j['periods']) == 6 and all('inherits' not in p for p in j['periods'])
