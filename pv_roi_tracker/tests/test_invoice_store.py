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


# ── Correction keying ────────────────────────────────────────────────────────

def _korekta(year=2025, month=11, inv_num='T/K1/BN567872/0009/26') -> InvoiceData:
    base = _data(year, month)
    # Create a copy with doc_type=korekta
    from dataclasses import replace
    return replace(base, doc_type='korekta', invoice_number=inv_num,
                   corrects_number='T/K1/BN567872/0005/26',
                   correction_delta_pln=1.89,
                   deposit_previous_pln=228.90,
                   deposit_used_pln=228.90,
                   amount_due_pln=232.52,
                   prev_deposit_previous_pln=230.79)


def _nota(year=2026, month=3, inv_num='K1NBN567872/025') -> InvoiceData:
    base = _data(year, month, imported=0.0, exported=0.0)
    from dataclasses import replace
    return replace(base, doc_type='nota', invoice_number=inv_num,
                   corrects_number='K1N0474969',
                   correction_delta_pln=125.34,
                   amount_due_pln=125.34,
                   requires_payment=False)


def test_upsert_korekta_gets_kor_key(store_path):
    """Korekty must be stored under a ~kor~ key, not overwriting the billing record."""
    billing_key = invoice_store.upsert(_data(2025, 11), path=store_path)
    kor = _korekta()
    kor_key = invoice_store.upsert(kor, path=store_path)
    assert billing_key == '2025-11'
    assert '~kor~' in kor_key
    assert kor_key.startswith('2025-11~kor~')
    # Both records must coexist
    all_inv = invoice_store.load(store_path)
    assert '2025-11' in all_inv
    assert kor_key in all_inv


def test_upsert_nota_gets_nota_key(store_path):
    nota = _nota()
    nota_key = invoice_store.upsert(nota, path=store_path)
    assert '~nota~' in nota_key
    assert nota_key.startswith('2026-03~nota~')


def test_upsert_korekta_idempotent(store_path):
    """Re-uploading the same korekta PDF must overwrite the same key (dedup)."""
    kor = _korekta()
    key1 = invoice_store.upsert(kor, path=store_path)
    key2 = invoice_store.upsert(kor, path=store_path)
    assert key1 == key2
    assert len([k for k in invoice_store.load(store_path) if '~kor~' in k]) == 1


# ── filter_billing ───────────────────────────────────────────────────────────

def test_filter_billing_excludes_corrections(store_path):
    invoice_store.upsert(_data(2025, 11), path=store_path)
    invoice_store.upsert(_korekta(2025, 11), path=store_path)
    invoice_store.upsert(_nota(2026, 3), path=store_path)
    all_inv = invoice_store.load(store_path)
    billing = invoice_store.filter_billing(all_inv)
    assert '2025-11' in billing
    assert all('~kor~' not in k and '~nota~' not in k for k in billing)
    assert len(billing) == 1


def test_filter_billing_excludes_stubs(store_path):
    invoice_store.upsert_stub('bad.pdf', 'raw', 'error', path=store_path)
    invoice_store.upsert(_data(), path=store_path)
    all_inv = invoice_store.load(store_path)
    billing = invoice_store.filter_billing(all_inv)
    assert all(not k.startswith('unparsed-') for k in billing)


# ── corrections_for ──────────────────────────────────────────────────────────

def test_corrections_for_returns_nested_corrections(store_path):
    invoice_store.upsert(_data(2025, 11), path=store_path)
    invoice_store.upsert(_korekta(2025, 11, 'T/K1/BN567872/0009/26'), path=store_path)
    invoice_store.upsert(_nota(2026, 3), path=store_path)  # different month
    all_inv = invoice_store.load(store_path)
    cors = invoice_store.corrections_for(all_inv, '2025-11')
    assert len(cors) == 1
    assert cors[0]['doc_type'] == 'korekta'
    assert '~kor~' in cors[0]['key']


def test_corrections_for_empty_when_none(store_path):
    invoice_store.upsert(_data(), path=store_path)
    all_inv = invoice_store.load(store_path)
    cors = invoice_store.corrections_for(all_inv, '2025-10')
    assert cors == []


# ── effective_by_month ────────────────────────────────────────────────────────

def test_effective_by_month_overlays_deposit(store_path):
    """effective_by_month must overlay deposit from the korekta onto billing record."""
    base = _data(2025, 11)
    from dataclasses import replace
    base_with_deposit = replace(base, deposit_previous_pln=230.79, deposit_used_pln=230.79,
                                 amount_due_pln=230.63)
    invoice_store.upsert(base_with_deposit, path=store_path)
    invoice_store.upsert(_korekta(2025, 11), path=store_path)
    all_inv = invoice_store.load(store_path)
    eff = invoice_store.effective_by_month(all_inv)
    assert '2025-11' in eff
    assert eff['2025-11']['deposit_previous_pln'] == pytest.approx(228.90, abs=0.01)
    assert eff['2025-11']['deposit_used_pln'] == pytest.approx(228.90, abs=0.01)
    assert eff['2025-11']['amount_due_pln'] == pytest.approx(232.52, abs=0.01)


def test_effective_by_month_no_correction_unchanged(store_path):
    """When no korekta exists, the billing record is returned as-is."""
    invoice_store.upsert(_data(), path=store_path)
    all_inv = invoice_store.load(store_path)
    eff = invoice_store.effective_by_month(all_inv)
    assert '2025-10' in eff
    # Without a korekta, deposit fields stay at their parsed values (None in _data())
    assert eff['2025-10'].get('deposit_previous_pln') is None


def test_effective_by_month_excludes_nota_keys(store_path):
    """Nota records must never appear as top-level keys in effective_by_month output."""
    invoice_store.upsert(_data(2026, 3), path=store_path)
    invoice_store.upsert(_nota(2026, 3), path=store_path)
    all_inv = invoice_store.load(store_path)
    eff = invoice_store.effective_by_month(all_inv)
    assert all('~nota~' not in k for k in eff)


# ── Safe-write regression (same fix as historic_store — see its test file) ──

def test_load_missing_with_bak_present_recovers_from_bak(store_path):
    invoice_store.upsert(_data(), path=store_path)
    bak = store_path.with_suffix('.json.bak')
    bak.write_text(store_path.read_text())
    store_path.unlink()   # simulate a crash that left only the .bak behind

    loaded = invoice_store.load(store_path)
    assert '2025-10' in loaded


def test_save_does_not_delete_target_before_atomic_replace(store_path, monkeypatch):
    """A crash mid-save must never leave invoices.json missing (see
    historic_store's identical test for the full incident writeup)."""
    invoice_store.upsert(_data(), path=store_path)

    original_rename = Path.rename
    seen_missing = []

    def _spy_rename(self, target):
        seen_missing.append(store_path.exists())
        return original_rename(self, target)

    monkeypatch.setattr(Path, 'rename', _spy_rename)
    invoice_store.upsert(_data(year=2025, month=11), path=store_path)

    assert seen_missing == [True]
    assert store_path.with_suffix('.json.bak').exists()
