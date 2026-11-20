"""Testy sc_reprice.py — przeliczenie historii ceną strefy godziny (simulate/apply)."""
from __future__ import annotations

import json

import pytest

from pv_roi_tracker import historic_store, sc_reprice
from pv_roi_tracker.models import MonthlyRecord

H1, H2 = '2026-06-10T10', '2026-06-10T11'


def _price(hour):
    return {H1: 1.0, H2: 2.0}.get(hour)


def _fetch_ok(y, m):
    # sc_h: H1 = 4, H2 = 6  (suma 10)
    return {H1: 10.0, H2: 10.0}, {H1: 6.0, H2: 4.0}


def _fetch_empty(y, m):
    return {}, {}


def _rec(month=6, produced=100.0, sc=None, pln=50.0, tag=None):
    return MonthlyRecord(year=2026, month=month, produced_kwh=produced, exported_kwh=40.0,
                         self_consumed_kwh=sc, self_consumed_savings_pln=pln,
                         buy_price_pln_kwh=0.8, tariff='G13', sc_pricing=tag)


def _by_ym(report):
    return {m['ym']: m for m in report['months']}


def test_no_invoice_month_uses_zone_pricing():
    rep = sc_reprice.simulate([_rec(sc=10.0)], fetch_hourly=_fetch_ok, price_at=_price)
    m = _by_ym(rep)['2026-06']
    assert m['method'] == 'hourly_zone'
    assert m['new_pln'] == pytest.approx(4 * 1.0 + 6 * 2.0)
    assert m['old_pln'] == pytest.approx(50.0)
    assert m['delta_pln'] == pytest.approx(16.0 - 50.0)
    assert m['frozen'] is False
    assert rep['total_delta_pln'] == pytest.approx(-34.0)


def test_invoice_month_keeps_kwh_and_scales_shape():
    # kWh z faktury 20 (kształt godzinowy 4/6 sumuje się do 10 -> factor 2)
    rep = sc_reprice.simulate([_rec(sc=20.0)], reconciled_months={(2026, 6)},
                              fetch_hourly=_fetch_ok, price_at=_price)
    m = _by_ym(rep)['2026-06']
    assert m['method'] == 'hourly_zone_invoice'
    assert m['frozen'] is True
    assert m['kwh'] == pytest.approx(20.0)
    assert m['new_pln'] == pytest.approx((4 * 1.0 + 6 * 2.0) * 2.0)


def test_invoice_month_zero_shape_unchanged_monthly_avg():
    def fetch(y, m):
        return {H1: 5.0}, {H1: 5.0}
    rep = sc_reprice.simulate([_rec(sc=20.0)], reconciled_months={(2026, 6)},
                              fetch_hourly=fetch, price_at=_price)
    m = _by_ym(rep)['2026-06']
    assert m['method'] == 'monthly_avg'
    assert m['new_pln'] == pytest.approx(50.0)


def test_invoice_month_missing_price_unchanged_monthly_avg():
    rep = sc_reprice.simulate([_rec(sc=20.0)], reconciled_months={(2026, 6)},
                              fetch_hourly=_fetch_ok, price_at=lambda h: None)
    m = _by_ym(rep)['2026-06']
    assert m['method'] == 'monthly_avg'
    assert m['new_pln'] == pytest.approx(50.0)


def test_no_lts_month_unchanged_monthly_avg():
    rep = sc_reprice.simulate([_rec(sc=10.0)], fetch_hourly=_fetch_empty, price_at=_price)
    m = _by_ym(rep)['2026-06']
    assert m['method'] == 'monthly_avg'
    assert m['new_pln'] == pytest.approx(50.0)
    assert m['delta_pln'] == pytest.approx(0.0)


def test_missing_price_hour_unchanged_monthly_avg():
    rep = sc_reprice.simulate([_rec(sc=10.0)], fetch_hourly=_fetch_ok, price_at=lambda h: None)
    assert _by_ym(rep)['2026-06']['method'] == 'monthly_avg'


def test_placeholder_untouched_and_no_flag(tmp_path):
    path = tmp_path / 'historic.json'
    ph = MonthlyRecord(year=2026, month=9)
    historic_store.save([ph, _rec(sc=10.0)], path)
    rep = sc_reprice.simulate(historic_store.load(path), fetch_hourly=_fetch_ok, price_at=_price)
    assert '2026-09' not in _by_ym(rep)
    assert '2026-09' in rep['unchanged']
    sc_reprice.apply(path, fetch_hourly=_fetch_ok, price_at=_price)
    docs = {m['month']: m for m in json.loads(path.read_text())['months']}
    assert docs[9]['sc_pricing'] is None
    assert docs[9]['self_consumed_savings_pln'] is None


def test_price_at_none_raises():
    with pytest.raises(ValueError):
        sc_reprice.simulate([_rec(sc=10.0)], fetch_hourly=_fetch_ok, price_at=None)


def test_simulate_does_not_write(tmp_path):
    path = tmp_path / 'historic.json'
    historic_store.save([_rec(sc=10.0)], path)
    before = path.read_text()
    sc_reprice.simulate(historic_store.load(path), fetch_hourly=_fetch_ok, price_at=_price)
    assert path.read_text() == before
    assert list(tmp_path.glob('*.bak-*')) == []


def test_apply_backup_and_only_two_fields_change(tmp_path):
    path = tmp_path / 'historic.json'
    historic_store.save([_rec(sc=10.0), _rec(month=7, sc=20.0)], path)
    original = path.read_text()
    old_doc = json.loads(original)
    rep = sc_reprice.apply(path, reconciled_months={(2026, 7)},
                           fetch_hourly=_fetch_ok, price_at=_price)
    baks = list(tmp_path.glob('historic.json.bak-*'))
    assert len(baks) == 1
    assert baks[0].read_text() == original
    assert rep['backup_path'] == str(baks[0])
    new_doc = json.loads(path.read_text())
    for old, new in zip(old_doc['months'], new_doc['months']):
        o = {k: v for k, v in old.items() if k not in ('self_consumed_savings_pln', 'sc_pricing')}
        n = {k: v for k, v in new.items() if k not in ('self_consumed_savings_pln', 'sc_pricing')}
        assert o == n
        assert new['sc_pricing'] == 'hourly_zone'
    assert new_doc['months'][0]['self_consumed_savings_pln'] == pytest.approx(16.0)
    assert new_doc['months'][1]['self_consumed_savings_pln'] == pytest.approx(32.0)


def test_apply_idempotent(tmp_path):
    path = tmp_path / 'historic.json'
    historic_store.save([_rec(sc=10.0), _rec(month=7, sc=20.0)], path)
    kw = dict(reconciled_months={(2026, 7)}, fetch_hourly=_fetch_ok, price_at=_price)
    sc_reprice.apply(path, **kw)
    first = json.loads(path.read_text())['months']
    sc_reprice.apply(path, **kw)
    second = json.loads(path.read_text())['months']
    assert first == second


def test_apply_twice_same_instant_keeps_both_backups(tmp_path, monkeypatch):
    from datetime import datetime as real_dt

    class _Frozen(real_dt):
        @classmethod
        def now(cls, tz=None):
            return real_dt(2026, 11, 20, 12, 0, 0, 0, tzinfo=tz)

    monkeypatch.setattr(sc_reprice, 'datetime', _Frozen)
    path = tmp_path / 'historic.json'
    historic_store.save([_rec(sc=10.0)], path)
    original = path.read_text()
    kw = dict(fetch_hourly=_fetch_ok, price_at=_price)
    r1 = sc_reprice.apply(path, **kw)
    after_first = path.read_text()
    r2 = sc_reprice.apply(path, **kw)
    assert r1['backup_path'] != r2['backup_path']
    baks = sorted(tmp_path.glob('historic.json.bak-*'))
    assert len(baks) == 2
    from pathlib import Path
    assert Path(r1['backup_path']).read_text() == original
    assert Path(r2['backup_path']).read_text() == after_first


def test_apply_keeps_hourly_zone_tag_when_hourly_data_missing(tmp_path):
    from pv_roi_tracker import sc_reprice, historic_store
    from pv_roi_tracker.models import MonthlyRecord
    path = tmp_path / 'historic.json'
    rec = MonthlyRecord(year=2026, month=5, produced_kwh=100.0, exported_kwh=40.0,
                        self_consumed_kwh=60.0, self_consumed_savings_pln=77.0,
                        sc_pricing='hourly_zone')
    historic_store._save_document({'months': [rec.to_dict()]}, path)
    sc_reprice.apply(path=path, fetch_hourly=lambda y, m: ({}, {}), price_at=lambda h: 1.0)
    saved = historic_store._load_document(path)['months'][0]
    assert saved['sc_pricing'] == 'hourly_zone'
    assert saved['self_consumed_savings_pln'] == 77.0
