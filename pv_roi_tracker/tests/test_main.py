"""Tests for main.py's pure, module-level helpers.

main() itself wires together apscheduler/MQTT/Flask/HA and isn't unit-tested
directly (no test_main.py existed before this file) — but the decision logic
for the v0.31.0 missed-month-close catch-up (previous_month / month_present)
is deliberately kept as plain module-level functions so it can be tested in
isolation, without booting the rest of the add-on.
"""
from datetime import date

from pv_roi_tracker.main import (
    previous_month, month_present, month_has_data,
    _months_since_commissioning, _heal_month_if_needed, _heal_action,
    _format_heatpump_push_line, _ebok_missing_documents, MAX_NEW_PER_SYNC,
    _baseline_tariff, _periods_peak_fn,
)
from pv_roi_tracker.models import MonthlyRecord


# ── previous_month ────────────────────────────────────────────────────────────

def test_previous_month_normal():
    assert previous_month(date(2026, 7, 25)) == (2026, 6)


def test_previous_month_january_wraps_to_prior_december():
    assert previous_month(date(2026, 1, 15)) == (2025, 12)


def test_previous_month_february():
    assert previous_month(date(2026, 2, 1)) == (2026, 1)


# ── month_present ─────────────────────────────────────────────────────────────

def _rec(year, month):
    return MonthlyRecord(year=year, month=month, produced_kwh=500.0)


def test_month_present_true_when_found():
    records = [_rec(2026, 5), _rec(2026, 6)]
    assert month_present(records, 2026, 6) is True


def test_month_present_false_when_missing():
    """Reproduces the B3 scenario: month-close never fired for June, so the
    month is entirely absent from historic.json (not even a zero row)."""
    records = [_rec(2026, 5)]
    assert month_present(records, 2026, 6) is False


def test_month_present_false_on_empty_records():
    assert month_present([], 2026, 6) is False


def test_month_present_distinguishes_year():
    """A same-numbered month in a different year must not count as present —
    guards against a year/month field mix-up in the caller."""
    records = [_rec(2025, 6)]
    assert month_present(records, 2026, 6) is False


# ── month_has_data ────────────────────────────────────────────────────────────

def _placeholder(year, month):
    """A data-less row — same shape as the ones a CSV import pre-seeds for
    future calendar months. See historic_store.has_energy_data."""
    return MonthlyRecord(year=year, month=month, produced_kwh=None)


def test_month_has_data_true_for_real_record():
    records = [_rec(2026, 7)]
    assert month_has_data(records, 2026, 7) is True


def test_month_has_data_false_for_placeholder():
    """Reproduces the 2026-08-01 incident: month_present() would say True here
    (the row exists), but month_has_data() must say False — this is exactly
    the distinction that should have triggered the startup catch-up backfill
    for July 2026 instead of silently accepting the empty row."""
    records = [_placeholder(2026, 7)]
    assert month_present(records, 2026, 7) is True
    assert month_has_data(records, 2026, 7) is False


def test_month_has_data_false_when_missing_entirely():
    assert month_has_data([], 2026, 7) is False


def test_month_has_data_false_for_zero_production():
    records = [MonthlyRecord(year=2026, month=7, produced_kwh=0.0)]
    assert month_has_data(records, 2026, 7) is False


# ── _months_since_commissioning (v0.35.0 multi-month healer scan) ────────────

def test_months_since_commissioning_spans_full_range():
    records = [_rec(2026, 3), _rec(2026, 4), _rec(2026, 5)]
    months = _months_since_commissioning(records, date(2026, 7, 15))
    assert months == [(2026, 3), (2026, 4), (2026, 5), (2026, 6)]


def test_months_since_commissioning_wraps_year_boundary():
    records = [_rec(2025, 11)]
    months = _months_since_commissioning(records, date(2026, 2, 1))
    assert months == [(2025, 11), (2025, 12), (2026, 1)]


def test_months_since_commissioning_ignores_placeholders():
    """A future data-less placeholder must not push the scan window forward."""
    records = [_rec(2026, 5), _placeholder(2026, 12)]
    months = _months_since_commissioning(records, date(2026, 7, 1))
    assert months == [(2026, 5), (2026, 6)]


def test_months_since_commissioning_empty_when_no_data():
    assert _months_since_commissioning([_placeholder(2026, 1)], date(2026, 7, 1)) == []


# ── _heal_month_if_needed ─────────────────────────────────────────────────────

def _balanced_rec(year, month):
    """March 2026 figures — dashboard-family produced within normal
    cross-family drift of the template-family figure (see balance.py)."""
    return MonthlyRecord(year=year, month=month, produced_kwh=643.98, exported_kwh=307.0,
                         self_consumed_kwh=336.98, consumed_kwh=670.88, purchased_kwh=35.0,
                         cross_family_produced_kwh=685.53)


def test_heal_month_if_needed_none_for_missing_key():
    assert _heal_month_if_needed({}, 2026, 7) == ('no_data', 'brak danych')


def test_heal_month_if_needed_none_for_placeholder():
    by_key = {(2026, 7): _placeholder(2026, 7)}
    assert _heal_month_if_needed(by_key, 2026, 7) == ('no_data', 'brak danych')


def test_heal_month_if_needed_none_when_balanced():
    by_key = {(2026, 7): _balanced_rec(2026, 7)}
    assert _heal_month_if_needed(by_key, 2026, 7) is None


def test_heal_month_if_needed_flags_balance_breach():
    """A month whose two production-tracking families diverge well beyond
    the normal ~0.6-6.5% drift must be flagged for repair even though the
    row isn't a data-less placeholder."""
    broken = MonthlyRecord(year=2026, month=7, produced_kwh=500.0, exported_kwh=430.00,
                           self_consumed_kwh=70.0, consumed_kwh=154.32, purchased_kwh=84.32,
                           cross_family_produced_kwh=650.0)
    by_key = {(2026, 7): broken}
    reason = _heal_month_if_needed(by_key, 2026, 7)
    assert reason is not None
    code, detail = reason
    assert code == 'balance_breach'
    assert 'bilans' in detail


# ── _heal_action (0.35.2 — invoice-reconciled months are never auto-rebuilt) ──

def test_heal_action_ok_when_no_reason():
    assert _heal_action(None, reconciled=False) == 'ok'
    assert _heal_action(None, reconciled=True) == 'ok'


def test_heal_action_heals_no_data_even_when_reconciled():
    """An empty reconciled row has nothing final to lose — heal it either
    way; reconcile_pending_invoices() reapplies the billed fields right
    after, same startup sequence."""
    reason = ('no_data', 'brak danych')
    assert _heal_action(reason, reconciled=False) == 'heal'
    assert _heal_action(reason, reconciled=True) == 'heal'


def test_heal_action_heals_balance_breach_when_not_reconciled():
    reason = ('balance_breach', 'niespójny bilans')
    assert _heal_action(reason, reconciled=False) == 'heal'


def test_heal_action_skips_balance_breach_when_reconciled():
    """The actual regression this covers: 0.35.1's rebase populated
    cross_family_produced_kwh on invoice-reconciled months, which armed the
    pre-existing balance-breach healer to silently overwrite
    produced_kwh/battery_*/specific_yield on every restart — undoing the
    'invoice is always final' guarantee rebase.py just shipped. A reconciled
    month with a balance breach must be skipped, not rebuilt."""
    reason = ('balance_breach', 'niespójny bilans')
    assert _heal_action(reason, reconciled=True) == 'skip_reconciled'


# ── _format_heatpump_push_line (Etap 3: linia w pushu miesięcznym) ────────────

def _hp_month(**overrides):
    base = {'kwh_total': 151.6, 'kwh_heating': 70.7, 'kwh_dhw': 75.0,
            'cash_pln': 56.23, 'econ_pln': 67.87, 'pv_battery_coverage_pct': 53.3}
    base.update(overrides)
    return base


def test_format_heatpump_push_line_includes_all_parts():
    line = _format_heatpump_push_line(_hp_month())
    assert line == ('Pompa ciepła: 152 kWh (grzanie 71 / CWU 75), '
                    'z sieci 56 zł (ekon. 68 zł), PV+bateria pokryły 53%.')


def test_format_heatpump_push_line_handles_missing_coverage():
    line = _format_heatpump_push_line(_hp_month(pv_battery_coverage_pct=None))
    assert line.endswith('(ekon. 68 zł).')
    assert 'pokryły' not in line


def test_format_heatpump_push_line_rounds_to_whole_numbers():
    line = _format_heatpump_push_line(_hp_month(kwh_total=0.4, kwh_heating=0.0, kwh_dhw=0.4))
    assert 'Pompa ciepła: 0 kWh (grzanie 0 / CWU 0)' in line


# ── _ebok_missing_documents (bezpiecznik importu eBOK) ───────────────────────

class _Doc:
    def __init__(self, signature):
        self.signature = signature


def test_ebok_missing_documents_returns_unknown_only():
    from pv_roi_tracker.ebok_client import known_signatures
    known = known_signatures([{'invoice_number': 'T/K1/BN111111/0001/26'}])
    docs = [_Doc('T/K1/BN111111/0001/26'), _Doc('T/K1/BN111111/0002/26')]
    missing, over = _ebok_missing_documents(docs, known)
    assert [d.signature for d in missing] == ['T/K1/BN111111/0002/26'] and over is False


def test_ebok_missing_documents_at_limit_is_allowed():
    docs = [_Doc(f'T/K1/BN111111/{i:04d}/26') for i in range(MAX_NEW_PER_SYNC)]
    missing, over = _ebok_missing_documents(docs, set())
    assert len(missing) == MAX_NEW_PER_SYNC and over is False


def test_ebok_missing_documents_over_limit_flags_overflow():
    assert MAX_NEW_PER_SYNC == 5
    docs = [_Doc(f'T/K1/BN111111/{i:04d}/26') for i in range(MAX_NEW_PER_SYNC + 1)]
    missing, over = _ebok_missing_documents(docs, set())
    assert len(missing) == 6 and over is True


# ── wiring taryfy: _baseline_tariff / _periods_peak_fn ──────────────────────────

def _periods_file(tmp_path, monkeypatch, periods, override=''):
    from pv_roi_tracker import main, tariff_history
    path = tmp_path / 'tariff_periods.json'
    if periods:
        tariff_history.save(periods, path)
    monkeypatch.setattr(main, 'TARIFF_PERIODS_PATH', path)
    monkeypatch.setattr(main, 'CURRENT_TARIFF_OVERRIDE', override)


def test_baseline_tariff_defaults_to_g12w_without_periods(tmp_path, monkeypatch):
    _periods_file(tmp_path, monkeypatch, [])
    assert _baseline_tariff() == 'G12w'


def test_baseline_tariff_uses_last_period(tmp_path, monkeypatch):
    _periods_file(tmp_path, monkeypatch, [{'tariff': 'G12W', 'from': None},
                                          {'tariff': 'G13', 'from': '2026-11-01'}])
    assert _baseline_tariff() == 'G13'


def test_baseline_tariff_override_wins(tmp_path, monkeypatch):
    _periods_file(tmp_path, monkeypatch, [{'tariff': 'G13', 'from': None}], override='G11')
    assert _baseline_tariff() == 'G11'


def test_periods_peak_fn_none_without_periods(tmp_path, monkeypatch):
    _periods_file(tmp_path, monkeypatch, [])
    assert _periods_peak_fn() is None


def test_periods_peak_fn_callable_with_periods(tmp_path, monkeypatch):
    from datetime import datetime
    _periods_file(tmp_path, monkeypatch, [{'tariff': 'G11', 'from': None}])
    fn = _periods_peak_fn()
    assert fn(datetime(2026, 11, 3, 3)) is True  # G11: jedna strefa = szczyt


# ── _refresh_payments (E7: wpłaty nie psują jobu płatności) ──────────────────

class _PayDoc:
    def __init__(self, signature, paid=True, gross=80.0):
        self.signature, self.paid, self.amount_gross_pln, self.due_date = signature, paid, gross, None


def _patch_pay_env(monkeypatch, tmp_path):
    from pv_roi_tracker import main as m
    monkeypatch.setattr(m, 'EBOK_PAYMENT_STATE_PATH', tmp_path / 'pay_state.json')
    monkeypatch.setattr(m, 'INVOICE_PATH', tmp_path / 'invoices.json')
    sent, jobs = [], []
    monkeypatch.setattr(m, '_notify_ha', lambda title, msg, target='family': sent.append((title, msg, target)))
    monkeypatch.setattr(m, '_record_job', lambda name, ok, detail='': jobs.append((name, ok, detail)))
    return m, sent, jobs


class _PayClient:
    def __init__(self, payments=None, exc=None):
        self._payments, self._exc, self.calls = payments or [], exc, []

    def list_payments(self, date_from, date_to):
        self.calls.append((date_from, date_to))
        if self._exc:
            raise self._exc
        return self._payments


def test_refresh_payments_error_is_swallowed_and_state_untouched(monkeypatch, tmp_path):
    m, sent, jobs = _patch_pay_env(monkeypatch, tmp_path)
    m._refresh_payments(_PayClient(exc=RuntimeError('boom')), [_PayDoc('S/1')], date(2026, 10, 27))
    assert not m.EBOK_PAYMENT_STATE_PATH.exists() and sent == []
    assert jobs == [('ebok_payment', False, 'odczyt wpłat nie powiódł się')]


def test_refresh_payments_login_blocked_propagates(monkeypatch, tmp_path):
    import pytest
    from pv_roi_tracker.ebok_client import EbokLoginBlocked
    m, _, _ = _patch_pay_env(monkeypatch, tmp_path)
    with pytest.raises(EbokLoginBlocked):
        m._refresh_payments(_PayClient(exc=EbokLoginBlocked('x')), [], date(2026, 10, 27))


def test_refresh_payments_window_is_13_months(monkeypatch, tmp_path):
    m, _, _ = _patch_pay_env(monkeypatch, tmp_path)
    client = _PayClient()
    m._refresh_payments(client, [], date(2026, 10, 27))
    assert client.calls == [(date(2025, 9, 27), date(2026, 10, 27))]
    m._refresh_payments(client, [], date(2026, 3, 31))
    assert client.calls[1] == (date(2025, 2, 28), date(2026, 3, 31))


def test_refresh_payments_result_reaches_payment_status_and_mismatch_pushes_once(monkeypatch, tmp_path):
    from pv_roi_tracker import invoice_store
    from pv_roi_tracker.ebok_client import EbokPayment
    m, sent, jobs = _patch_pay_env(monkeypatch, tmp_path)
    sig = 'T/K1/BN111111/0015/26'
    pay = EbokPayment(date=date(2026, 8, 15), amount=60.5, type='Wpłata', doc_number='X',
                      settlements=[(sig, 100.5, 60.5)])
    monkeypatch.setattr(invoice_store, 'load',
                        lambda path: {'k': {'invoice_number': sig, 'amount_due_pln': 100.5}})
    client = _PayClient([pay])
    docs = [_PayDoc(sig, True, 100.5)]
    m._refresh_payments(client, docs, date(2026, 10, 27))
    m._refresh_payments(client, docs, date(2026, 10, 28))
    assert len(sent) == 1 and sent[0][0] == 'PV ROI Tracker — wpłaty' and sent[0][2] == 'kacper'
    status = m._payment_status_payload(date(2026, 10, 28))
    assert status['last_payment']['date'] == '2026-08-15'
    assert status['mismatches'][0]['signature'] == sig
    assert status['active'] is None and jobs == []


def test_payment_status_payload_without_payments_block(monkeypatch, tmp_path):
    m, _, _ = _patch_pay_env(monkeypatch, tmp_path)
    assert m._payment_status_payload(date(2026, 10, 27)) == {
        'active': None, 'last_payment': None, 'mismatches': []}
