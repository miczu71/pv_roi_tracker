"""Testy tariff_explain.py (T6) — wiersze cennika, rozbicie, porównania."""
from datetime import date, datetime

import pytest

from pv_roi_tracker import tariff_explain as te
from pv_roi_tracker import tariff_invoice_check as tic
from pv_roi_tracker.tariff_catalog import load_catalog
from pv_roi_tracker.tariff_module import TariffModule

IMPORT = {'2026-03-10T10': 2.0, '2026-03-10T23': 1.0, '2026-03-11T08': 4.0}


@pytest.fixture(scope='module')
def cat():
    return load_catalog()


@pytest.fixture
def module(cat):
    return TariffModule(cat, default_tariff='G12w')


def _hand(module, keys, tariff='G12w'):
    """Ręcznie: Σ kWh × składniki godziny (netto) i Σ stałych miesiąca."""
    var = {k: 0.0 for k in te.VARIABLE_KEYS}
    for key, kwh in keys.items():
        pp = module.price_at(datetime.strptime(key, '%Y-%m-%dT%H'), tariff)
        for c in te.VARIABLE_KEYS:
            var[c] += kwh * pp.components[c]
    return var


# ── breakdown ────────────────────────────────────────────────────────────────

def test_hour_g12w_zgodny_z_price_at(module):
    b = te.breakdown(module, IMPORT, {}, tariff='G12w', at='2026-03-10T10')
    pp = module.price_at(datetime(2026, 3, 10, 10), 'G12w')
    assert b['scope'] == 'hour' and b['kwh'] == 1.0
    assert b['total_gross'] == pytest.approx(pp.gross, abs=1e-5)
    assert b['total_gross'] == pytest.approx(1.2304, abs=5e-5)
    assert all(c['kind'] != 'fixed' for c in b['components'])
    off = te.breakdown(module, IMPORT, {}, tariff='G12w', at='2026-03-10T23')
    assert off['total_gross'] == pytest.approx(0.6306, abs=5e-5)


def test_suma_skladnikow_rowna_total_i_wylaczenie_zmienia_total(module):
    b = te.breakdown(module, IMPORT, {}, month='2026-03')
    inc = [c for c in b['components'] if c['included'] and c['kind'] != 'vat']
    assert sum(c['net'] for c in inc) == pytest.approx(b['total_net'], abs=1e-4)
    assert sum(c['gross'] for c in inc) == pytest.approx(b['total_gross'], abs=1e-4)
    vat = next(c for c in b['components'] if c['key'] == 'vat')
    assert vat['gross'] == pytest.approx(b['total_gross'] - b['total_net'], abs=1e-4)
    keys = [k for k in te.ALL_KEYS if k != 'excise']
    less = te.breakdown(module, IMPORT, {}, month='2026-03', include=','.join(keys))
    assert less['total_net'] < b['total_net']
    assert next(c for c in less['components'] if c['key'] == 'excise')['included'] is False


def test_basis_net_bez_vat(module):
    b = te.breakdown(module, IMPORT, {}, month='2026-03', basis='net')
    assert all(c['key'] != 'vat' for c in b['components'])
    assert b['basis'] == 'net'


def test_month_zgodny_z_recznym_wyliczeniem(module):
    b = te.breakdown(module, IMPORT, {}, month='2026-03')
    var = _hand(module, IMPORT)
    fixed = module.fixed(date(2026, 3, 15), 'G12w')
    for c in b['components']:
        if c['kind'] == 'variable':
            assert c['net'] == pytest.approx(var[c['key']], abs=1e-5)
        elif c['kind'] == 'fixed':
            assert c['net'] == pytest.approx(fixed[c['key']], abs=1e-5)
    total = sum(var.values()) + sum(fixed.values())
    assert b['total_net'] == pytest.approx(total, abs=1e-4)
    assert b['kwh'] == 7.0
    assert b['per_kwh_gross'] == pytest.approx(total * 1.23 / 7.0, abs=1e-4)


def test_day_stale_prorata(module):
    b = te.breakdown(module, IMPORT, {}, day='2026-03-10')
    fixed = module.fixed(date(2026, 3, 10), 'G12w')
    net = next(c for c in b['components'] if c['key'] == 'network')
    assert net['net'] == pytest.approx(fixed['network'] / 31, abs=1e-5)
    assert net['rate_net'] == pytest.approx(fixed['network'], abs=1e-5)
    assert b['kwh'] == 3.0


def test_brak_poboru_stale_nadal_liczone(module):
    b = te.breakdown(module, {}, {}, month='2026-03')
    assert b['kwh'] == 0 and b['per_kwh_gross'] is None and b['note']
    assert b['total_net'] == pytest.approx(sum(module.fixed(date(2026, 3, 15), 'G12w').values()), abs=1e-4)


def test_invoice_scope(module):
    inv = {'2026-03': {'invoice_number': 'F/1/2026'}}
    b = te.breakdown(module, IMPORT, inv, invoice='2026-03')
    assert b['scope'] == 'invoice' and 'F/1/2026' in b['label']
    assert b['total_net'] == te.breakdown(module, IMPORT, {}, month='2026-03')['total_net']


@pytest.mark.parametrize('kw', [
    {}, {'at': '2026-03-10T10', 'day': '2026-03-10'}, {'at': '2026-03-10'}, {'day': 'x'}, {'month': '2026-13'},
    {'invoice': 'brak'}, {'month': '2026-03', 'tariff': 'G99'}, {'month': '2026-03', 'include': 'foo'},
    {'month': '2026-03', 'basis': 'x'}, {'month': '2021-03'}])
def test_breakdown_bledy(module, kw):
    with pytest.raises(ValueError):
        te.breakdown(module, IMPORT, {}, **kw)


# ── compare ──────────────────────────────────────────────────────────────────

def test_compare_tariff_diff_to_a_minus_b(module):
    c = te.compare_tariff(module, IMPORT, a='G12w', b='G13', scope='month', at='2026-03')
    assert c['diff'] == pytest.approx(c['a']['total'] - c['b']['total'], abs=1e-5)
    assert c['diff_pct'] == pytest.approx(100 * c['diff'] / c['b']['total'], abs=1e-4)
    assert {z['zone'] for z in c['zones']['a']} == {'peak', 'offpeak'}
    assert {z['zone'] for z in c['zones']['b']} == {'morning_peak', 'evening_peak', 'other'}
    net = te.compare_tariff(module, IMPORT, a='G12w', b='G13', scope='month', at='2026-03', basis='net')
    assert net['a']['total'] < c['a']['total']


def test_compare_tariff_hour_i_year(module):
    h = te.compare_tariff(module, IMPORT, a='G12w', b='G11', scope='hour', at='2026-03-10T10')
    assert h['a']['total'] == pytest.approx(1.2304, abs=5e-5)
    y = te.compare_tariff(module, IMPORT, a='G12w', b='G11', scope='year', at='2026')
    m = te.compare_tariff(module, IMPORT, a='G12w', b='G11', scope='month', at='2026-03')
    fixed_year = sum(sum(module.fixed(date(2026, mm, 15), 'G12w').values()) for mm in range(1, 13))
    fixed_month = sum(module.fixed(date(2026, 3, 15), 'G12w').values())
    assert y['a']['total'] - m['a']['total'] == pytest.approx((fixed_year - fixed_month) * 1.23, abs=1e-3)


def test_compare_period_dwa_okresy(module):
    p = te.compare_period(module, tariff='G12w', zone='peak', date_from='2025-12-01', date_to='2026-02-15')
    assert [x['from'] for x in p['periods']] == ['2025-11-01', '2026-01-01', '2026-02-01']
    p2 = te.compare_period(module, tariff='G12w', zone='peak', date_from='2025-06-01', date_to='2026-03-01')
    assert len(p2['periods']) >= 2
    first, last = p2['periods'][0], p2['periods'][-1]
    assert first['components']['energy']['net'] != last['components']['energy']['net']
    assert te.compare_period(module, tariff='G12w')['zone'] == 'peak'
    with pytest.raises(ValueError):
        te.compare_period(module, tariff='G12w', zone='zzz')


def _inv(module, **over):
    rates = module.legacy_rates(date(2026, 10, 15))
    inv = {f: rates[f] for f in tic.CHECK_FIELDS}
    inv.update(over, invoice_number='T-1')
    return {'2026-10': inv}


def test_compare_invoice_match_diff_no_invoice(module):
    ok = te.compare_invoice(module, _inv(module), month='2026-10')
    assert ok['status'] == 'match' and ok['invoice_no'] == 'T-1' and ok['tolerance_pct'] == tic.TOLERANCE_PCT
    assert {r['status'] for r in ok['rows']} == {'ok'}
    peak = module.legacy_rates(date(2026, 10, 15))['energy_peak_net']
    bad = te.compare_invoice(module, _inv(module, energy_peak_net=peak * 1.1), month='2026-10')
    assert bad['status'] == 'diff'
    row = next(r for r in bad['rows'] if r['key'] == 'energy' and r['zone'] == 'peak')
    assert row['status'] == 'diff' and row['diff_pct'] == pytest.approx(10.0, abs=1e-3)
    none = te.compare_invoice(module, {}, month='2026-10')
    assert none['status'] == 'no_invoice' and none['rows'] == [] and none['invoice_no'] is None
    partial = _inv(module)
    del partial['2026-10']['dist_oze_net']
    miss = te.compare_invoice(module, partial, month='2026-10')
    assert next(r for r in miss['rows'] if r['key'] == 'oze')['status'] == 'missing'


# ── rates ────────────────────────────────────────────────────────────────────

def test_rates_filtry(module):
    r = te.rates_rows(module, {}, tariff='G12w', zone='peak', component='energy')
    assert r['count'] == len(r['rows']) > 3 and not r['truncated']
    assert {(x['tariff'], x['zone'], x['component']) for x in r['rows']} == {('G12w', 'peak', 'energy')}
    row = next(x for x in r['rows'] if x['period_from'] == '2026-02-01')
    assert row['net'] == 0.622 and row['gross'] == pytest.approx(0.622 * 1.23, abs=1e-5)
    assert row['confidence'] == 'document' and row['source_key'] == 'A, B, E, G'
    assert row['source_url'].startswith('https://')
    cur = te.rates_rows(module, {}, tariff='G12w', date_from='2026-03-01', date_to='2026-03-05')
    assert {x['period_from'] for x in cur['rows']} == {'2026-02-01'}
    assert all(x['source_key'] and 'A' in x['source_key']
               for x in te.rates_rows(module, {}, source='A', tariff='G11')['rows'])
    assert all(x['confidence'] == 'unconfirmed'
               for x in te.rates_rows(module, {}, confidence='unconfirmed')['rows'])


def test_rates_q_i_skalary_bez_strefy(module):
    r = te.rates_rows(module, {}, tariff='G11', q='MOCOWA')
    assert r['rows'] and all(x['component'] == 'capacity' and x['zone'] is None for x in r['rows'])
    z = te.rates_rows(module, {}, tariff='G12', q='night')
    assert z['rows'] and all(x['zone'] == 'night' for x in z['rows'])


def test_rates_null_to_null_nie_zero(cat):
    import copy
    c = copy.deepcopy(cat)
    c['periods'][-1]['energy_net']['G11']['all'] = None
    c['periods'][-1]['oze_net'] = None
    r = te.rates_rows(TariffModule(c), {}, tariff='G11', date_from='2026-03-01')
    energy = next(x for x in r['rows'] if x['component'] == 'energy')
    oze = next(x for x in r['rows'] if x['component'] == 'oze')
    assert energy['net'] is None and energy['gross'] is None and oze['net'] is None


def test_rates_limit(module):
    r = te.rates_rows(module, {}, limit=10)
    assert r['count'] == 10 and r['truncated'] is True


def test_rates_korekty_reczne(cat):
    ov = [{'from': '2026-03-01', 'to': None, 'note': 'umowa', 'patch': {'oze_net': 0.01,
           'energy_net': {'G12w': {'peak': 0.9}}}}]
    r = te.rates_rows(TariffModule(cat, overrides=ov), {}, tariff='G12w', confidence='manual')
    got = {(x['component'], x['zone']): x for x in r['rows']}
    assert got[('oze', None)]['net'] == 0.01 and got[('energy', 'peak')]['net'] == 0.9
    assert got[('oze', None)]['period_to'] is None and got[('oze', None)]['note'] == 'umowa'


def test_rates_invoice_no_tylko_gdy_odczytane(cat):
    mod = TariffModule(cat, invoice_ok={'2025-07-01'})
    inv = {'2025-08': {'invoice_number': 'F/8'}, '2025-09': {}, '2025-09~kor~X': {'invoice_number': 'K'}}
    rows = te.rates_rows(mod, inv, tariff='G11', component='oze', confidence='invoice')['rows']
    assert rows and rows[0]['invoice_no'] == 'F/8'
    assert te.rates_rows(mod, {}, tariff='G11', component='oze', confidence='invoice')['rows'][0]['invoice_no'] is None


@pytest.mark.parametrize('kw', [{'tariff': 'G99'}, {'component': 'vat'}, {'confidence': 'x'},
                                {'date_from': 'x'}, {'date_from': '2026-02-01', 'date_to': '2026-01-01'}])
def test_rates_bledy(module, kw):
    with pytest.raises(ValueError):
        te.rates_rows(module, {}, **kw)


@pytest.mark.parametrize('kw', [{'at': '2026-03-10T10'}, {'month': '2026-03'}])
@pytest.mark.parametrize('inc', ['energy,dist,network', 'energy,dist,network,vat', 'energy'])
def test_suma_gross_included_rowna_total_gross(module, kw, inc):
    kw = dict(kw)
    if 'at' in kw:
        inc = ','.join(k for k in inc.split(',') if k not in te.FIXED_KEYS)
    b = te.breakdown(module, IMPORT, {}, tariff='G12w', include=inc, **kw)
    comps = [c for c in b['components'] if c['included'] and c['kind'] != 'vat']
    assert sum(c['gross'] for c in comps) == pytest.approx(b['total_gross'], abs=1e-4)
    vat_on = 'vat' in inc.split(',')
    if vat_on:
        assert b['total_gross'] == pytest.approx(b['total_net'] * 1.23, abs=1e-4)
    else:
        assert b['total_gross'] == pytest.approx(b['total_net'], abs=1e-4)
        assert all(c['gross'] == c['net'] for c in b['components'] if c['kind'] != 'vat')
    if b['kwh']:
        assert b['per_kwh_gross'] == pytest.approx(b['total_gross'] / b['kwh'], abs=1e-4)


def test_compare_bez_vat_total_bez_vat(module):
    on = te.compare_tariff(module, IMPORT, a='G12w', b='G13', scope='month', at='2026-03')
    off = te.compare_tariff(module, IMPORT, a='G12w', b='G13', scope='month', at='2026-03',
                            include=','.join(k for k in te.ALL_KEYS if k != 'vat'))
    assert off['a']['total'] == pytest.approx(on['a']['total'] / 1.23, abs=1e-4)
    assert sum(c['gross'] for c in off['a']['components'] if c['included']) == pytest.approx(
        off['a']['total'], abs=1e-4)
