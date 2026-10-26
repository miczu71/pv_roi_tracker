"""Testy tariff_settings_store — ustawienia symulacji taryf (E4)."""
import json

import pytest

from pv_roi_tracker import tariff_settings_store as ts
from pv_roi_tracker.tariff_catalog import TariffSettings


def test_brak_pliku_daje_defaults(tmp_path):
    assert ts.load(tmp_path / 'nie_ma.json') == ts.DEFAULTS


def test_uszkodzony_json_daje_defaults(tmp_path, caplog):
    p = tmp_path / 's.json'
    p.write_text('{nie json')
    with caplog.at_level('WARNING'):
        assert ts.load(p) == ts.DEFAULTS
    assert any(r.levelname == 'WARNING' for r in caplog.records)


def test_round_trip(tmp_path):
    p = tmp_path / 's.json'
    s = ts.validate({'phases': 1, 'capacity_bracket': 'lt500',
                     'sales_fee_net': 12.5, 'recommend_threshold_pln': 250})
    ts.save(s, p)
    assert ts.load(p) == s
    assert json.loads(p.read_text())['phases'] == 1


@pytest.mark.parametrize('raw', [
    {'phases': 2},
    {'capacity_bracket': 'x'},
    {'sales_fee_net': -1},
    {'sales_fee_net': 201},
    {'recommend_threshold_pln': 6000},
    {'recommend_threshold_pln': -1},
    {'sales_fee_net': 'abc'},
])
def test_walidacja_odrzuca(raw):
    with pytest.raises(ValueError):
        ts.validate(raw)


def test_nieznane_klucze_ignorowane_brakujace_z_defaults():
    out = ts.validate({'phases': 1, 'foo': 'bar'})
    assert 'foo' not in out
    assert out['phases'] == 1
    assert out['capacity_bracket'] == ts.DEFAULTS['capacity_bracket']
    assert isinstance(out['sales_fee_net'], float)


def test_to_catalog_settings():
    s = ts.to_catalog_settings({'phases': 1, 'capacity_bracket': '500_1200',
                                'sales_fee_net': 5.0, 'recommend_threshold_pln': 10})
    assert s == TariffSettings(phases=1, capacity_bracket='500_1200', sales_fee_net=5.0)


def test_sales_fee_g13s_domyslnie_none():
    assert ts.DEFAULTS['sales_fee_g13s_net'] is None
    assert ts.validate({})['sales_fee_g13s_net'] is None


@pytest.mark.parametrize("raw", [None, '', 'null'])
def test_sales_fee_g13s_pusta_wartosc_to_none(raw):
    if raw == 'null':
        raw = None
    assert ts.validate({'sales_fee_g13s_net': raw})['sales_fee_g13s_net'] is None


def test_sales_fee_g13s_wartosc_przechodzi():
    assert ts.validate({'sales_fee_g13s_net': 19.1})['sales_fee_g13s_net'] == pytest.approx(19.1)


def test_sales_fee_g13s_zakres():
    with pytest.raises(ValueError):
        ts.validate({'sales_fee_g13s_net': 500})


def test_sales_fee_g13s_do_catalog_settings():
    s = ts.to_catalog_settings(ts.validate({'sales_fee_g13s_net': 10}))
    assert s.sales_fee_g13s_net == pytest.approx(10.0)
