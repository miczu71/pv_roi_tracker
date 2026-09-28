"""
Unit tests for invoice_ingest.py — the PDF-ingest logic shared by the manual
upload endpoint (web.invoice_upload) and the eBOK auto-import job.

Uses monkeypatch on parse_invoice/parse_invoice_debug (module-level functions
imported into invoice_ingest) rather than real PDFs, so these tests run
everywhere without needing fixture files.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pv_roi_tracker import invoice_ingest
from pv_roi_tracker.invoice_parser import InvoiceParseError


def _fake_invoice_data(year=2026, month=8, warnings=None):
    return SimpleNamespace(
        year=year, month=month, doc_type='rozliczeniowa',
        imported_kwh=100.0, exported_kwh=200.0,
        peak_gross=0.6, offpeak_gross=0.4,
        amount_due_pln=63.3, deposit_used_pln=57.57,
        correction_delta_pln=None,
        warnings=warnings or [],
    )


def test_ingest_pdfs_success_path(monkeypatch, tmp_path):
    data = _fake_invoice_data()
    monkeypatch.setattr(invoice_ingest, 'parse_invoice', lambda pdf_bytes: data)
    result = invoice_ingest.ingest_pdfs([('faktura.pdf', b'%PDF-fake')], tmp_path / 'invoices.json')

    assert len(result['parsed_list']) == 1
    assert result['parsed_list'][0] is data
    assert result['pdf_bytes_map'] == {'faktura.pdf': b'%PDF-fake'}
    assert result['raw_texts'] == {}  # no warnings → no raw text stored
    assert result['results'] == [{
        'filename': 'faktura.pdf', 'month': '2026-08', 'doc_type': 'rozliczeniowa',
        'imported_kwh': 100.0, 'exported_kwh': 200.0,
        'peak_gross': 0.6, 'offpeak_gross': 0.4,
        'amount_due': 63.3, 'deposit_used': 57.57,
        'correction_delta_pln': None, 'warnings': [], 'ok': True,
    }]


def test_ingest_pdfs_stores_raw_text_when_warnings_present(monkeypatch, tmp_path):
    data = _fake_invoice_data(warnings=['pole X nieznalezione'])
    monkeypatch.setattr(invoice_ingest, 'parse_invoice', lambda pdf_bytes: data)
    monkeypatch.setattr(invoice_ingest, 'parse_invoice_debug',
                        lambda pdf_bytes: {'text': 'surowy tekst faktury'})
    result = invoice_ingest.ingest_pdfs([('faktura.pdf', b'%PDF-fake')], tmp_path / 'invoices.json')
    assert result['raw_texts'] == {'faktura.pdf': 'surowy tekst faktury'}


def test_ingest_pdfs_parse_error_creates_stub(monkeypatch, tmp_path):
    invoice_path = tmp_path / 'invoices.json'

    def _raise(pdf_bytes):
        raise InvoiceParseError('miesiąc nierozpoznany')

    monkeypatch.setattr(invoice_ingest, 'parse_invoice', _raise)
    monkeypatch.setattr(invoice_ingest, 'parse_invoice_debug',
                        lambda pdf_bytes: {'text': 'nieczytelny tekst'})
    result = invoice_ingest.ingest_pdfs([('zly.pdf', b'%PDF-broken')], invoice_path)

    assert result['parsed_list'] == []
    assert len(result['results']) == 1
    r = result['results'][0]
    assert r['ok'] is False
    assert r['needs_training'] is True
    assert r['stub_key'] is not None

    # the stub was actually persisted (same path invoice_upload uses)
    from pv_roi_tracker import invoice_store
    stored = invoice_store.load(invoice_path)
    assert r['stub_key'] in stored
    assert stored[r['stub_key']]['needs_training'] is True


def test_ingest_pdfs_unexpected_error_is_reported_without_stub(monkeypatch, tmp_path):
    def _raise(pdf_bytes):
        raise RuntimeError('coś poszło nie tak')

    monkeypatch.setattr(invoice_ingest, 'parse_invoice', _raise)
    result = invoice_ingest.ingest_pdfs([('zly.pdf', b'garbage')], tmp_path / 'invoices.json')

    assert result['parsed_list'] == []
    r = result['results'][0]
    assert r['ok'] is False
    assert 'needs_training' not in r
    assert 'coś poszło nie tak' in r['error']


def test_ingest_pdfs_multiple_files_mixed_outcomes(monkeypatch, tmp_path):
    good = _fake_invoice_data()

    def _parse(pdf_bytes):
        if pdf_bytes == b'good':
            return good
        raise InvoiceParseError('zły plik')

    monkeypatch.setattr(invoice_ingest, 'parse_invoice', _parse)
    monkeypatch.setattr(invoice_ingest, 'parse_invoice_debug', lambda pdf_bytes: {'text': ''})
    result = invoice_ingest.ingest_pdfs(
        [('ok.pdf', b'good'), ('bad.pdf', b'zly')], tmp_path / 'invoices.json'
    )
    assert len(result['parsed_list']) == 1
    assert sum(1 for r in result['results'] if r.get('ok')) == 1
    assert sum(1 for r in result['results'] if r.get('needs_training')) == 1
