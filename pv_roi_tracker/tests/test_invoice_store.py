"""Tests for invoice_store.py — PDF persistence is the focus here (the rest
of the store's behaviour is exercised indirectly via test_invoice_parser.py
fixtures and main.py's reconcile flow)."""
import pytest
from pathlib import Path

from pv_roi_tracker import invoice_store
from pv_roi_tracker.invoice_parser import InvoiceData


def _data(year=2025, month=10, imported=300.0, exported=101.0) -> InvoiceData:
    return InvoiceData(
        year=year, month=month,
        imported_kwh=imported, exported_kwh=exported,
        imported_kwh_peak=None, imported_kwh_offpeak=None,
        exported_kwh_peak=None, exported_kwh_offpeak=None,
        energy_peak_net=None, energy_offpeak_net=None,
        dist_var_peak_net=None, dist_var_offpeak_net=None,
        dist_jakosciowa_net=None, dist_oze_net=None, dist_kogeneracja_net=None,
        fixed_mocowa_net=None, fixed_abonament_net=None, fixed_stalysieciowy_net=None,
    )


@pytest.fixture
def store_path(tmp_path) -> Path:
    return tmp_path / 'invoices.json'


# ── PDF persistence on upsert ───────────────────────────────────────────────

def test_upsert_with_pdf_bytes_saves_file(store_path):
    key = invoice_store.upsert(_data(), filename='fv.pdf', pdf_bytes=b'%PDF-fake-bytes',
                               path=store_path)
    assert key == '2025-10'
    rec = invoice_store.get(key, store_path)
    assert rec['pdf_path']
    assert Path(rec['pdf_path']).exists()
    assert Path(rec['pdf_path']).read_bytes() == b'%PDF-fake-bytes'


def test_upsert_without_pdf_bytes_no_pdf_path(store_path):
    key = invoice_store.upsert(_data(), filename='fv.pdf', path=store_path)
    rec = invoice_store.get(key, store_path)
    assert 'pdf_path' not in rec


def test_load_pdf_round_trip(store_path):
    key = invoice_store.upsert(_data(), pdf_bytes=b'hello-pdf', path=store_path)
    assert invoice_store.load_pdf(key, store_path) == b'hello-pdf'


def test_load_pdf_missing_returns_none(store_path):
    assert invoice_store.load_pdf('2099-01', store_path) is None


def test_pdf_dir_is_sibling_of_json(store_path):
    invoice_store.upsert(_data(), pdf_bytes=b'x', path=store_path)
    assert invoice_store.pdf_dir(store_path) == store_path.parent / 'pdfs'


# ── PDF persistence on stub (failed parse) ──────────────────────────────────

def test_upsert_stub_with_pdf_bytes(store_path):
    key = invoice_store.upsert_stub('bad.pdf', 'raw text', 'boom', path=store_path,
                                    pdf_bytes=b'%PDF-stub')
    rec = invoice_store.get(key, store_path)
    assert rec['pdf_path']
    assert invoice_store.load_pdf(key, store_path) == b'%PDF-stub'


# ── Remove deletes the PDF too ──────────────────────────────────────────────

def test_remove_deletes_stored_pdf(store_path):
    key = invoice_store.upsert(_data(), pdf_bytes=b'to-be-deleted', path=store_path)
    pdf_file = invoice_store.pdf_path_for(key, store_path)
    assert pdf_file.exists()
    invoice_store.remove(key, store_path)
    assert not pdf_file.exists()
    assert invoice_store.get(key, store_path) is None


def test_remove_without_pdf_does_not_raise(store_path):
    key = invoice_store.upsert(_data(), path=store_path)  # no pdf_bytes
    invoice_store.remove(key, store_path)  # must not raise even though no PDF exists
    assert invoice_store.get(key, store_path) is None
