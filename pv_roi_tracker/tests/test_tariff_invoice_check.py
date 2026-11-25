"""Testy tariff_invoice_check.py — kontrola stawek faktur vs cennik modułu (tolerancja 2%)."""
from datetime import date, datetime

import pytest

from pv_roi_tracker import tariff_invoice_check as tic
from pv_roi_tracker import tariff_job as tj
from pv_roi_tracker import tariff_module as tm
from pv_roi_tracker.tariff_catalog import TariffSettings, load_catalog
from pv_roi_tracker.tariff_module import TariffModule


@pytest.fixture(scope='module')
def cat():
    return load_catalog()


@pytest.fixture
def module(cat):
    return TariffModule(cat)


def _at(y, m, d, h):
    return datetime(y, m, d, h)


def _invoice(module, ym='2026-10', **over):
    """Faktura z pełnym zestawem pól = stawki modułu (dzień 15.), z nadpisaniami `over`."""
    rates = module.legacy_rates(date(2026, 10, 15))
    inv = {f: rates[f] for f in tic.CHECK_FIELDS}
    inv.update(over)
    return {f'{ym}-01': inv}


def _diverged_check(module, pct=1.03):
    """Miesiąc z rozbieżnością `peak_gross` o `pct` względem cennika."""
    peak = module.legacy_rates(date(2026, 10, 15))['peak_gross']
    return tic.check_invoices(module, _invoice(module, peak_gross=peak * pct))


# ── check_invoices ───────────────────────────────────────────────────────────

def test_invoice_equal_to_module_is_ok(module):
    check = tic.check_invoices(module, _invoice(module))
    m = check['2026-10']
    assert m['ok'] is True
    assert m['diverged'] == []
    assert m['tariff'] == 'G12w'
    assert m['period_from'] == '2026-02-01'


def test_peak_gross_three_percent_diverges(module):
    check = _diverged_check(module, 1.03)
    m = check['2026-10']
    assert m['ok'] is False
    assert 'peak_gross' in m['diverged']
    assert m['fields']['peak_gross']['ok'] is False


def test_peak_gross_within_tolerance_is_ok(module):
    check = _diverged_check(module, 1.019)
    assert check['2026-10']['ok'] is True
    assert check['2026-10']['diverged'] == []


def test_month_with_tariff_change_is_skipped(module, cat):
    changed = TariffModule(cat, periods=[{'tariff': 'G12w', 'from': None},
                                         {'tariff': 'G13', 'from': '2026-10-15'}])
    assert tic.check_invoices(changed, _invoice(module)) == {}


def test_tariff_outside_check_set_is_skipped(module, cat):
    other = TariffModule(cat, periods=[{'tariff': 'G13s', 'from': None}])
    assert tic.check_invoices(other, _invoice(module)) == {}


# ── pending_notifications ────────────────────────────────────────────────────

def test_new_divergence_notifies_once(module):
    check = _diverged_check(module)
    msgs, new = tic.pending_notifications(check, {})
    assert len(msgs) == 1
    assert msgs[0].startswith('Faktura 2026-10: stawki (G12w) rozjeżdżają się z cennikiem o więcej niż 2%: peak_gross ')
    assert new == {'2026-10': 'peak_gross'}


def test_repeated_divergence_is_silent(module):
    check = _diverged_check(module)
    _, new = tic.pending_notifications(check, {})
    msgs, new2 = tic.pending_notifications(check, new)
    assert msgs == []
    assert new2 == new


def test_changed_divergence_set_notifies_again(module):
    rates = module.legacy_rates(date(2026, 10, 15))
    check = tic.check_invoices(module, _invoice(
        module, peak_gross=rates['peak_gross'] * 1.03, offpeak_gross=rates['offpeak_gross'] * 1.03))
    msgs, new = tic.pending_notifications(check, {'2026-10': 'peak_gross'})
    assert len(msgs) == 1
    assert new == {'2026-10': 'offpeak_gross,peak_gross'}


def test_return_to_ok_removes_entry(module):
    ok_check = tic.check_invoices(module, _invoice(module))
    msgs, new = tic.pending_notifications(ok_check, {'2026-10': 'peak_gross'})
    assert msgs == []
    assert new == {}


def test_pending_does_not_mutate_input(module):
    notified = {'2026-10': 'peak_gross'}
    tic.pending_notifications(tic.check_invoices(module, _invoice(module)), notified)
    assert notified == {'2026-10': 'peak_gross'}


def test_message_lists_at_most_four_fields():
    fields = {f: {'invoice': 1.0, 'module': 2.0, 'diff_pct': -50.0, 'ok': False} for f in 'abcde'}
    check = {'2026-10': {'tariff': 'G12w', 'period_from': '2026-02-01', 'ok': False,
                         'fields': fields, 'diverged': list('abcde')}}
    (msg,), _ = tic.pending_notifications(check, {})
    assert msg.count(' zł vs cennik ') == 4
    assert msg.endswith('; …')


# ── invoice_confirmed_periods ────────────────────────────────────────────────

def test_confirmed_periods_only_when_all_months_ok(module):
    assert tic.invoice_confirmed_periods(tic.check_invoices(module, _invoice(module))) == ['2026-02-01']
    assert tic.invoice_confirmed_periods(_diverged_check(module)) == []


# ── price_at z invoice_ok ────────────────────────────────────────────────────

def test_invoice_ok_upgrades_unconfirmed_only(cat):
    pf = TariffModule(cat).price_at(_at(2025, 8, 12, 12)).period_from
    upgraded = TariffModule(cat, invoice_ok={pf})
    assert upgraded.price_at(_at(2025, 8, 12, 12)).confidence == 'invoice'


def test_invoice_ok_keeps_document(cat):
    m = TariffModule(cat, invoice_ok={'2026-02-01'})
    assert m.price_at(_at(2026, 10, 29, 12)).confidence == 'document'


def test_build_passes_invoice_ok(cat):
    m = tm.build(None, None, None, catalog=cat, invoice_ok={'2026-02-01'})
    assert m.invoice_ok == {'2026-02-01'}


# ── compute_payload i zapis powiadomień ──────────────────────────────────────

def test_compute_payload_carries_invoice_keys(cat, module):
    inv = {'2026-10-01': {f: module.legacy_rates(date(2026, 10, 15))[f] for f in tic.CHECK_FIELDS}}
    p = tj.compute_payload({}, cat, TariffSettings(), {}, {}, inv, date(2026, 10, 20))
    assert p['invoice_check']['2026-10']['ok'] is True
    assert p['invoice_confirmed_periods'] == ['2026-02-01']


def test_load_notified_missing_file_is_empty(tmp_path):
    assert tic.load_notified(tmp_path / 'brak.json') == {}


def test_load_notified_corrupt_file_is_empty(tmp_path):
    path = tmp_path / 'uszkodzony.json'
    path.write_text('{nie json', encoding='utf-8')
    assert tic.load_notified(path) == {}


def test_save_and_load_notified_roundtrip(tmp_path):
    path = tmp_path / 'powiadomienia.json'
    tic.save_notified({'2026-10': 'peak_gross'}, path)
    assert tic.load_notified(path) == {'2026-10': 'peak_gross'}


# ── porządki T3 (T4) ─────────────────────────────────────────────────────────

def test_bad_invoice_field_value_is_skipped_not_fatal(module):
    check = tic.check_invoices(module, _invoice(module, peak_gross='abc', offpeak_gross=[1]))
    m = check['2026-10']
    assert 'peak_gross' not in m['fields'] and 'offpeak_gross' not in m['fields']
    assert 'energy_peak_net' in m['fields'] and m['ok'] is True


def test_pending_drops_notified_months_missing_from_check(module):
    msgs, new = tic.pending_notifications(tic.check_invoices(module, _invoice(module)),
                                          {'2026-10': 'peak_gross', '2025-01': 'peak_gross'})
    assert msgs == []
    assert new == {}


# ── G13 (trzy strefy, okres faktury) ─────────────────────────────────────────

_G13_PERIODS = [{'tariff': 'G12w', 'from': None}, {'tariff': 'G13', 'from': '2026-11-03'}]


def _g13_invoice(module, period='03.11.2026 - 18.11.2026', **over):
    """Faktura G13 = stawki katalogu okresu 5 (energia z akcyzą), stała sieciowa proporcjonalnie do dni."""
    inv = {'tariff': 'G13', 'billing_period_raw': period,
           'energy_morning_net': 0.4718, 'energy_evening_net': 0.783, 'energy_other_net': 0.426,
           'dist_var_morning_net': 0.2203, 'dist_var_evening_net': 0.3898, 'dist_var_other_net': 0.0392,
           'morning_gross': 0.9048, 'evening_gross': 1.496, 'other_gross': 0.6257,
           'dist_jakosciowa_net': 0.0332, 'dist_oze_net': 0.0073, 'dist_kogeneracja_net': 0.003,
           'fixed_stalysieciowy_net': 5.79}
    inv.update(over)
    return {'2026-11': inv}


@pytest.fixture
def g13_module(cat):
    return TariffModule(cat, periods=_G13_PERIODS)


def test_g13_partial_month_invoice_matches_catalog(g13_module):
    m = tic.check_invoices(g13_module, _g13_invoice(g13_module))['2026-11']
    assert m['ok'] is True
    assert m['tariff'] == 'G13'
    assert m['period_from'] == '2026-02-01'
    assert m['diverged'] == []
    assert 'fixed_stalysieciowy_net' in m['fields']


def test_g13_diverged_zone_rate(g13_module):
    m = tic.check_invoices(g13_module, _g13_invoice(g13_module, energy_evening_net=0.90))['2026-11']
    assert m['ok'] is False
    assert m['diverged'] == ['energy_evening_net']


def test_g13_fixed_charge_is_prorated_by_days(g13_module):
    full = tic.check_invoices(g13_module, _g13_invoice(g13_module, fixed_stalysieciowy_net=10.86))['2026-11']
    assert full['diverged'] == ['fixed_stalysieciowy_net']


def test_g13_period_spanning_tariff_change_is_skipped(g13_module):
    inv = _g13_invoice(g13_module, period='01.11.2026 - 18.11.2026')
    assert tic.check_invoices(g13_module, inv) == {}


def test_g13_invoice_without_period_uses_whole_month(g13_module):
    inv = _g13_invoice(g13_module, period=None)
    assert tic.check_invoices(g13_module, inv) == {}      # 1.11 jest jeszcze G12w


def test_g13_zone_field_with_bad_value_is_ignored(g13_module):
    m = tic.check_invoices(g13_module, _g13_invoice(g13_module, energy_other_net='x'))['2026-11']
    assert 'energy_other_net' not in m['fields']
    assert m['ok'] is True


def test_g12w_invoice_still_checked_with_two_zone_path(module):
    assert tic.check_invoices(module, _invoice(module))['2026-10']['tariff'] == 'G12w'
