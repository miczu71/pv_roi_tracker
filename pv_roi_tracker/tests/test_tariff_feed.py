"""Testy payloadu encji taryfy (feed_payload / feed_changed)."""
from datetime import datetime

import pytest

from pv_roi_tracker import tariff_catalog as tc
from pv_roi_tracker.tariff_module import TariffModule, feed_changed, feed_payload


@pytest.fixture(scope='module')
def catalog():
    return tc.load_catalog()


def test_g12w_zgodne_z_price_at(catalog):
    m = TariffModule(catalog)
    now = datetime(2026, 10, 26, 12, 30, 45)
    p = feed_payload(m, now)
    pt = m.price_at(datetime(2026, 10, 26, 12), 'G12w')
    assert p['state'] == pt.zone == 'peak'
    assert p['price'] == round(pt.gross, 4)
    a = p['attributes']
    assert a['tariff'] == 'G12w' and a['is_peak'] is True and a['gross_per_kwh'] == p['price']
    assert a['next_change'] == m.next_change(now) == {'at': '2026-10-26T13', 'zone': 'offpeak'}
    assert a['period_from'] == pt.period_from and a['confidence'] == pt.confidence
    assert a['generated_at'].startswith('2026-10-26T12:30:45')
    assert len(a['schedule']) == 48
    assert a['schedule'][0]['hour'] == '2026-10-26T12'
    assert set(a['schedule'][0]) == {'hour', 'tariff', 'zone', 'is_peak', 'level', 'gross', 'net', 'confidence'}


@pytest.mark.parametrize('day,expect', [
    (datetime(2026, 7, 15), {'cheap', 'mid', 'expensive'}),
    (datetime(2026, 1, 15), {'cheap', 'mid', 'expensive'}),
])
def test_g13_poziomy_w_harmonogramie(catalog, day, expect):
    m = TariffModule(catalog, periods=[{'tariff': 'G13', 'from': None}])
    p = feed_payload(m, day)
    levels = {e['level'] for e in p['attributes']['schedule']}
    assert levels == expect
    assert p['attributes']['level'] in expect and p['attributes']['tariff'] == 'G13'


def test_okres_assumed_confidence(catalog):
    m = TariffModule(catalog)
    p = feed_payload(m, datetime(2099, 1, 5, 10))
    assert p['attributes']['confidence'] == 'assumed'
    assert {e['confidence'] for e in p['attributes']['schedule']} == {'assumed'}


def test_feed_changed_ignoruje_generated_at(catalog):
    m = TariffModule(catalog)
    a = feed_payload(m, datetime(2026, 10, 26, 12, 5))
    b = feed_payload(m, datetime(2026, 10, 26, 12, 40))
    c = feed_payload(m, datetime(2026, 10, 26, 13, 5))
    assert feed_changed(a, None) is True
    assert feed_changed(b, a) is False
    assert feed_changed(c, a) is True


def _step(pub, last, m, now):
    from pv_roi_tracker.tariff_module import tariff_feed_step
    return tariff_feed_step(pub, last, m, now)


def test_feed_step_dedupe_i_ponowienie(catalog):
    from unittest.mock import MagicMock
    m = TariffModule(catalog)
    pub = MagicMock()
    pub.publish_tariff.side_effect = [False, True, True]
    last = {'payload': None, 'ok': True}
    res, last = _step(pub, last, m, datetime(2026, 10, 26, 12, 5))
    assert res is False and last['payload'] is None and last['ok'] is False
    res, last = _step(pub, last, m, datetime(2026, 10, 26, 12, 10))      # ponowienie po porażce
    assert res is True and last['ok'] is True and last['payload'] is not None
    res, last = _step(pub, last, m, datetime(2026, 10, 26, 12, 40))      # ta sama godzina
    assert res is None and pub.publish_tariff.call_count == 2
    res, last = _step(pub, last, m, datetime(2026, 10, 26, 13, 5))       # zmiana godziny
    assert res is True and pub.publish_tariff.call_count == 3


def test_feed_step_rozlaczony_zachowuje_poprzedni_stan(catalog):
    from unittest.mock import MagicMock
    m = TariffModule(catalog)
    pub = MagicMock()
    pub.publish_tariff.return_value = True
    res, last = _step(pub, {'payload': None, 'ok': True}, m, datetime(2026, 10, 26, 12))
    prev = last['payload']
    pub.publish_tariff.return_value = False
    res, last = _step(pub, last, m, datetime(2026, 10, 26, 13))
    assert res is False and last['payload'] is prev and last['ok'] is False
