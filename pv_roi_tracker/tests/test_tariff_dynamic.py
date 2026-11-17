"""Testy encji dynamicznych cen (tariff_dynamic: dyn_a_price / dyn_b_price)."""
import copy
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from pv_roi_tracker import tariff_catalog as tc
from pv_roi_tracker.tariff_dynamic import (
    dynamic_changed,
    dynamic_feed_payload,
    inputs_with_ahead,
    tariff_dynamic_step,
)
from pv_roi_tracker.tariff_sim import hourly_series

BASELINE = 'G12w'
NOW = datetime(2026, 5, 13, 10, 30, 45)
KEY = '2026-05-13T10'
KOMPAS = {KEY: 'S2'}
RDN = {KEY: 400.0}


@pytest.fixture(scope='module')
def catalog():
    return tc.load_catalog()


def _payloads(catalog, rdn=None, kompas=None, now=NOW, overrides=None):
    return dynamic_feed_payload(catalog, RDN if rdn is None else rdn,
                                KOMPAS if kompas is None else kompas, overrides, BASELINE, now)


def test_cena_rowna_wierszowi_hourly_series(catalog):
    p = _payloads(catalog)
    s = hourly_series(date(2026, 5, 13), date(2026, 5, 15), {}, catalog,
                      tariffs=('dyn_a', 'dyn_b'), rdn=RDN, kompas=KOMPAS, dist_tariff=BASELINE)
    for name in ('dyn_a', 'dyn_b'):
        row = next(r for r in s[name]['hours'] if r['key'] == KEY)
        assert p[name]['price'] == round(row['gross_per_kwh'], 4)
        a = p[name]['attributes']
        assert a['tariff'] == name and a['zone'] == row['zone']
        assert a['net_per_kwh'] == pytest.approx(row['net_per_kwh'], abs=1e-4)
        assert a['components'] == row['components']
        assert a['generated_at'] == '2026-05-13T10:30:45'


def test_dyn_a_confidence_rdn_i_dyn_b_kompas(catalog):
    p = _payloads(catalog)
    assert p['dyn_a']['attributes']['confidence'] == 'rdn'
    assert p['dyn_b']['attributes']['confidence'] == 'kompas'
    assert p['dyn_b']['attributes']['zone'] == 'S2'


def test_fallback_7_dni_wstecz(catalog):
    p = _payloads(catalog, rdn={'2026-05-06T10': 300.0})
    a = p['dyn_a']['attributes']
    assert a['confidence'] == 'rdn_fallback_7d'
    assert p['dyn_a']['price'] is not None
    assert a['schedule'][0]['confidence'] == 'rdn_fallback_7d'


def test_brak_rdn_i_fallbacku_cena_none(catalog):
    p = _payloads(catalog, rdn={})
    assert p['dyn_a']['price'] is None
    assert p['dyn_a']['attributes']['confidence'] == 'unavailable'
    assert p['dyn_a']['attributes']['schedule'][0]['gross'] is None


def test_brak_strefy_kompasu_dyn_b_cena_none(catalog):
    p = _payloads(catalog, kompas={})
    assert p['dyn_b']['price'] is None
    assert p['dyn_b']['attributes']['confidence'] == 'unavailable'
    assert p['dyn_b']['attributes']['zone'] is None
    assert p['dyn_a']['price'] is not None      # dyn_a nie zależy od Kompasu


def test_taryfa_przed_valid_from_niedostepna(catalog):
    # dyn_a od 2026-04-01, dyn_b od 2026-01-01 — grudzień 2025 jest przed obiema
    p = _payloads(catalog, rdn=RDN, kompas=KOMPAS, now=datetime(2025, 12, 15, 12))
    for n in ('dyn_a', 'dyn_b'):
        assert p[n]['price'] is None
        assert p[n]['attributes']['confidence'] == 'unavailable'
        assert p[n]['attributes']['schedule'] == []


def test_dyn_b_przed_valid_from_w_marcu_bez_kompasu_48_pustych(catalog):
    # dyn_b jest dostępne od 2026-01-01: bez strefy Kompasu harmonogram ma 48 pozycji bez ceny
    p = _payloads(catalog, rdn={}, kompas={}, now=datetime(2026, 3, 10, 12))
    sch = p['dyn_b']['attributes']['schedule']
    assert len(sch) == 48 and all(e['gross'] is None and e['zone'] is None for e in sch)


def test_harmonogram_48_godzin_od_biezacej_pelnej(catalog):
    p = _payloads(catalog)
    sch = p['dyn_b']['attributes']['schedule']
    assert len(sch) == 48
    assert sch[0]['hour'] == '2026-05-13T10'
    assert sch[-1]['hour'] == (NOW.replace(minute=0, second=0) + timedelta(hours=47)).strftime('%Y-%m-%dT%H')
    assert set(sch[0]) == {'hour', 'zone', 'gross', 'net', 'confidence'}


def test_harmonogram_zaokragla_do_5_miejsc(catalog):
    p = _payloads(catalog)
    for e in p['dyn_a']['attributes']['schedule']:
        if e['gross'] is not None:
            assert e['gross'] == round(e['gross'], 5) and e['net'] == round(e['net'], 5)


def test_dynamic_changed_ignoruje_generated_at(catalog):
    a = _payloads(catalog, now=datetime(2026, 5, 13, 10, 5))
    b = _payloads(catalog, now=datetime(2026, 5, 13, 10, 40))
    c = _payloads(catalog, now=datetime(2026, 5, 13, 11, 5))
    assert dynamic_changed(a, None) is True
    assert dynamic_changed(b, a) is False
    assert dynamic_changed(c, a) is True


def _step(pub, last, inputs, now):
    return tariff_dynamic_step(pub, last, inputs, now)


def _inputs(catalog, rdn=None, kompas=None):
    return {'catalog': catalog, 'rdn': RDN if rdn is None else rdn,
            'kompas': KOMPAS if kompas is None else kompas, 'overrides': None, 'baseline': BASELINE}


def test_krok_publikuje_tylko_przy_zmianie_i_ponawia(catalog):
    pub = MagicMock()
    pub.publish_tariff_dynamic.side_effect = [False, True, True]
    inp = _inputs(catalog)
    last = {'payload': None, 'ok': True}
    res, last = _step(pub, last, inp, datetime(2026, 5, 13, 10, 5))
    assert res is False and last['payload'] is None and last['ok'] is False
    res, last = _step(pub, last, inp, datetime(2026, 5, 13, 10, 10))     # ponowienie po porażce
    assert res is True and last['ok'] is True and last['payload'] is not None
    res, last = _step(pub, last, inp, datetime(2026, 5, 13, 10, 40))     # ta sama godzina
    assert res is None and pub.publish_tariff_dynamic.call_count == 2
    res, last = _step(pub, last, inp, datetime(2026, 5, 13, 11, 5))      # zmiana godziny
    assert res is True and pub.publish_tariff_dynamic.call_count == 3


def test_krok_rozlaczony_zachowuje_poprzedni_stan(catalog):
    pub = MagicMock()
    pub.publish_tariff_dynamic.return_value = True
    inp = _inputs(catalog)
    _, last = _step(pub, {'payload': None, 'ok': True}, inp, datetime(2026, 5, 13, 10))
    prev = last['payload']
    pub.publish_tariff_dynamic.return_value = False
    res, last = _step(pub, last, inp, datetime(2026, 5, 13, 11))
    assert res is False and last['payload'] is prev and last['ok'] is False


def test_krok_nie_polyka_valueerror_z_katalogu(catalog):
    bad = copy.deepcopy(catalog)
    bad['dynamic_sales']['margin_net_per_kwh'] = None
    pub = MagicMock()
    with pytest.raises(ValueError):
        _step(pub, {'payload': None, 'ok': True}, _inputs(bad), NOW)
    pub.publish_tariff_dynamic.assert_not_called()


def test_unavailable_payloads_bez_ceny():
    from pv_roi_tracker.tariff_dynamic import unavailable_payloads
    p = unavailable_payloads(datetime(2026, 11, 9, 12, 30))
    for n in ('dyn_a', 'dyn_b'):
        assert p[n]['price'] is None
        assert p[n]['attributes']['confidence'] == 'unavailable'
        assert p[n]['attributes']['schedule'] == []


class _Cache:
    rdn = {'2026-05-06T10': 300.0}
    kompas: dict = {}


def _fake_ahead(rdn_today=True):
    def fn(day, cache):
        iso = day.isoformat()
        if day == NOW.date():
            return {'rdn': {KEY: 400.0} if rdn_today else {}, 'kompas': {KEY: 'S2'},
                    'rdn_available': rdn_today, 'reason': None if rdn_today else 'brak RDN'}
        return {'rdn': {f'{iso}T10': 500.0}, 'kompas': {}, 'rdn_available': True, 'reason': None}
    return fn


def test_inputs_with_ahead_scala_cache_dzis_i_jutro():
    rdn, kompas, problems = inputs_with_ahead(_Cache(), NOW, _fake_ahead())
    assert rdn[KEY] == 400.0 and rdn['2026-05-14T10'] == 500.0 and rdn['2026-05-06T10'] == 300.0
    assert kompas[KEY] == 'S2' and problems == []
    assert _Cache.rdn == {'2026-05-06T10': 300.0}      # cache nietknięty


def test_inputs_with_ahead_brak_rdn_na_dzis_to_problem(catalog):
    _rdn, _kompas, problems = inputs_with_ahead(_Cache(), NOW, _fake_ahead(rdn_today=False))
    assert problems == ['brak RDN']


def test_cena_teraz_z_rdn_dnia_a_nie_z_fallbacku(catalog):
    rdn, kompas, _ = inputs_with_ahead(_Cache(), NOW, _fake_ahead())
    p = dynamic_feed_payload(catalog, rdn, kompas, None, BASELINE, NOW)
    assert p['dyn_a']['attributes']['confidence'] == 'rdn'
    assert p['dyn_b']['attributes']['confidence'] == 'kompas'


def test_przelom_miesiaca_48_godzin(catalog):
    now = datetime(2026, 5, 31, 23, 30)
    p = dynamic_feed_payload(catalog, {}, {}, None, BASELINE, now)
    sched = p['dyn_a']['attributes']['schedule']
    assert len(sched) == 48
    assert sched[0]['hour'] == '2026-05-31T23' and sched[1]['hour'] == '2026-06-01T00'


def test_granica_valid_from_dyn_a_bez_ceny_i_bez_wyjatku(catalog):
    now = datetime(2026, 3, 31, 23, 10)
    p = dynamic_feed_payload(catalog, {'2026-03-31T23': 400.0}, {}, None, BASELINE, now)
    assert p['dyn_a']['price'] is None
    assert p['dyn_a']['attributes']['confidence'] == 'unavailable'


def test_components_podstawa_netto(catalog):
    assert _payloads(catalog)['dyn_a']['attributes']['components_basis'] == 'net'


# ── ciągłość po końcu katalogu i korekty ręczne (F3/F4) ──────────────────────

def test_feed_dyn_po_koncu_katalogu_ma_ceny_assumed(catalog):
    now = datetime(2026, 12, 30, 1, 30)
    keys = [(now.replace(minute=0) + timedelta(hours=i)).strftime('%Y-%m-%dT%H') for i in range(48)]
    rdn = {k: 400.0 for k in keys}
    kompas = {k: 'S2' for k in keys}
    p = dynamic_feed_payload(catalog, rdn, kompas, None, BASELINE, now)
    sched = p['dyn_a']['attributes']['schedule']
    assert p['dyn_a']['price'] is not None and p['dyn_b']['price'] is not None
    assert all(r['gross'] is not None for r in sched)
    assert [r['confidence'] for r in sched if r['hour'][:10] <= '2026-12-31'] == \
        ['rdn'] * sum(1 for r in sched if r['hour'][:10] <= '2026-12-31')
    assert all(r['confidence'] == 'assumed' for r in sched if r['hour'][:10] >= '2027-01-01')
    assert p['dyn_a']['attributes']['confidence'] == 'rdn'


def test_feed_dyn_w_calosci_po_koncu_katalogu_confidence_assumed(catalog):
    now = datetime(2027, 1, 4, 10, 15)
    key = '2027-01-04T10'
    p = dynamic_feed_payload(catalog, {key: 400.0}, {key: 'S2'}, None, BASELINE, now)
    assert p['dyn_a']['attributes']['confidence'] == 'assumed'
    assert p['dyn_a']['price'] is not None


def test_feed_dyn_przed_katalogiem_nadal_lookup_error(catalog):
    with pytest.raises(LookupError):
        dynamic_feed_payload(catalog, {}, {}, None, BASELINE, datetime(2024, 11, 3, 10))


def test_korekta_dystrybucji_zmienia_cene_dyn_b(catalog):
    key = '2026-11-10T12'
    now = datetime(2026, 11, 10, 12, 5)
    base = dynamic_feed_payload(catalog, {key: 400.0}, {key: 'S2'}, [], BASELINE, now)
    ovs = [{'from': '2026-11-01', 'to': None, 'note': 't',
            'patch': {'dist_var_net': {'G12w': {'offpeak': 0.30, 'peak': 0.30}}}}]
    cor = dynamic_feed_payload(catalog, {key: 400.0}, {key: 'S2'}, ovs, BASELINE, now)
    assert cor['dyn_a']['price'] != base['dyn_a']['price']
