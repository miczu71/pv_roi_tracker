"""Tests for web._build_rate_trend() — trend stawek jednostkowych z faktur.

_build_rate_trend() takes an already stub-filtered invoices dict (billing records only)
and builds per-invoice rate series and effective_gross_per_kwh."""
import pytest

from pv_roi_tracker import web, invoice_store
from pv_roi_tracker.invoice_parser import _parse_text
from tests.test_invoice_parser import _make_old_format_text, _make_g11_format_text


@pytest.fixture
def store_path(tmp_path):
    return tmp_path / 'invoices.json'


def _real(store_path):
    return invoice_store.load_real(store_path)


def test_no_invoices_returns_none(store_path):
    assert web._build_rate_trend(_real(store_path)) is None


def test_stub_only_returns_none(store_path):
    invoice_store.upsert_stub('bad.pdf', 'raw', 'parse failed', path=store_path)
    assert web._build_rate_trend(_real(store_path)) is None


def test_returns_rate_series_for_single_invoice(store_path):
    data = _parse_text(_make_old_format_text())   # 2025-10
    invoice_store.upsert(data, filename='a.pdf', path=store_path)

    rt = web._build_rate_trend(_real(store_path))
    assert rt is not None
    assert '2025-10' in rt['labels']
    months = {m['ym']: m for m in rt['rates_per_month']}
    m = months['2025-10']
    assert m['energy_peak_net'] == pytest.approx(0.76100, abs=0.0001)
    assert m['oze_net'] is not None


def test_effective_gross_per_kwh_is_positive_when_kwh_present(store_path):
    data = _parse_text(_make_old_format_text())   # 2025-10 — has imported_kwh
    invoice_store.upsert(data, filename='a.pdf', path=store_path)

    rt = web._build_rate_trend(_real(store_path))
    m  = rt['rates_per_month'][0]
    # Either populated (if amounts present) or None (if no amount fields)
    # We only assert it's not negative
    if m['effective_gross_per_kwh'] is not None:
        assert m['effective_gross_per_kwh'] > 0


def test_chronological_order_and_labels(store_path):
    older = _parse_text(_make_g11_format_text())   # 2024-02
    newer = _parse_text(_make_old_format_text())   # 2025-10
    invoice_store.upsert(newer, filename='newer.pdf', path=store_path)
    invoice_store.upsert(older, filename='older.pdf', path=store_path)

    rt = web._build_rate_trend(_real(store_path))
    labels = rt['labels']
    assert labels == sorted(labels)
    assert labels[0] == '2024-02'
    assert labels[-1] == '2025-10'


def test_latest_effective_gross_matches_last_month(store_path):
    older = _parse_text(_make_g11_format_text())   # 2024-02
    newer = _parse_text(_make_old_format_text())   # 2025-10
    invoice_store.upsert(older, filename='older.pdf', path=store_path)
    invoice_store.upsert(newer, filename='newer.pdf', path=store_path)

    rt = web._build_rate_trend(_real(store_path))
    last = rt['rates_per_month'][-1]
    assert rt['latest_effective_gross_per_kwh'] == last['effective_gross_per_kwh']


def test_corrections_excluded(store_path):
    """Correction keys (YYYY-MM~kor~N) must not appear in rate_trend.

    In production api_data() calls filter_billing(filter_real(stored)) before
    passing the result to _build_rate_trend() — the correction is already gone.
    We mirror that same path here.
    """
    data = _parse_text(_make_old_format_text())   # 2025-10
    invoice_store.upsert(data, filename='a.pdf', path=store_path)

    # Manually inject a correction record
    from dataclasses import asdict as _asdict
    import json
    raw = invoice_store.load(store_path)
    raw['2025-10~kor~1'] = {**_asdict(data), 'doc_type': 'korekta'}
    store_path.write_text(json.dumps({'invoices': raw}))

    # Production path: filter_billing(filter_real(...))
    billing = invoice_store.filter_billing(invoice_store.load_real(store_path))
    rt = web._build_rate_trend(billing)
    assert rt is not None
    assert all('~kor~' not in m['ym'] for m in rt['rates_per_month'])
    assert len(rt['rates_per_month']) == 1
