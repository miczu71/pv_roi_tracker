"""
Shared PDF-ingest logic for Tauron invoices — used by both the manual upload
endpoint (`web.invoice_upload`) and the eBOK auto-import job (`main.ebok_job`).

Parses each PDF, stores unparseable ones as training stubs (same as a manual
upload would), and returns the pieces the caller passes on to the invoice
reconcile callback plus a per-file summary suitable for JSON/log output.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from .invoice_parser import InvoiceParseError, parse_invoice, parse_invoice_debug

logger = logging.getLogger(__name__)


def ingest_pdfs(files: list, invoice_path: Optional[Path]) -> dict:
    """files: list of (filename, pdf_bytes) tuples.

    Returns a dict with:
      parsed_list:   list[InvoiceData] — ready for the reconcile callback
      raw_texts:     dict[filename, str] — only for parses with warnings
      pdf_bytes_map: dict[filename, bytes]
      results:       list[dict] — one summary per file (ok / needs_training / error)
    """
    from . import invoice_store as _is

    parsed_list = []
    raw_texts: dict = {}
    pdf_bytes_map: dict = {}
    results: list = []

    for fname, pdf_bytes in files:
        pdf_bytes_map[fname] = pdf_bytes
        try:
            data = parse_invoice(pdf_bytes)
            data._filename = fname  # type: ignore[attr-defined]
            parsed_list.append(data)
            if data.warnings:
                debug = parse_invoice_debug(pdf_bytes)
                raw_texts[fname] = debug.get('text', '')
            results.append({'filename': fname, 'month': f'{data.year}-{data.month:02d}',
                             'doc_type': getattr(data, 'doc_type', 'rozliczeniowa'),
                             'imported_kwh': data.imported_kwh, 'exported_kwh': data.exported_kwh,
                             'peak_gross': data.peak_gross, 'offpeak_gross': data.offpeak_gross,
                             'amount_due': data.amount_due_pln, 'deposit_used': data.deposit_used_pln,
                             'correction_delta_pln': getattr(data, 'correction_delta_pln', None),
                             'warnings': data.warnings,
                             'ok': True})
        except InvoiceParseError as exc:
            error_msg = str(exc)
            stub_key = None
            if invoice_path is not None:
                try:
                    debug = parse_invoice_debug(pdf_bytes)
                    raw_text = debug.get('text', '')
                    stub_key = _is.upsert_stub(fname, raw_text, error_msg, invoice_path,
                                               pdf_bytes=pdf_bytes)
                except Exception:
                    logger.exception('Failed to store stub for %s', fname)
            results.append({'filename': fname, 'ok': False, 'needs_training': True,
                             'error': error_msg, 'stub_key': stub_key})
        except Exception as exc:
            logger.exception('Invoice parse error: %s', fname)
            results.append({'filename': fname, 'ok': False, 'error': str(exc)})

    return {'parsed_list': parsed_list, 'raw_texts': raw_texts,
            'pdf_bytes_map': pdf_bytes_map, 'results': results}
