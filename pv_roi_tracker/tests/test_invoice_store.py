"""Tests for invoice_store.py — PDF persistence is the focus here (the rest
of the store's behaviour is exercised indirectly via test_invoice_parser.py
fixtures and main.py's reconcile flow)."""
import pytest
from pathlib import Path

from pv_roi_tracker import invoice_store
from pv_roi_tracker.invoice_parser import InvoiceData


def _data(year=2025, month=10, imported=300.0, exported=101.0, warnings=None) -> InvoiceData:
    return InvoiceData(
        year=year, month=month,
        imported_kwh=imported, exported_kwh=exported,
        imported_kwh_peak=None, imported_kwh_offpeak=None,
        exported_kwh_peak=None, exported_kwh_offpeak=None,
        energy_peak_net=None, energy_offpeak_net=None,
        dist_var_peak_net=None, dist_var_offpeak_net=None,
        dist_jakosciowa_net=None, dist_oze_net=None, dist_kogeneracja_net=None,
        fixed_mocowa_net=None, fixed_abonament_net=None, fixed_stalysieciowy_net=None,
        warnings=warnings or [],
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


# ── needs_training derivation from parser warnings (docs/AUDIT_2026_08_10.md) ─

def test_upsert_flags_needs_training_on_missing_field_warning(store_path):
    """A warning reporting a field the parser genuinely could not find
    ('… nie znaleziony', no fallback) must flip needs_training on — this is
    the exact 2024-01 case: 5 such warnings (missing mocowa/abonament/stały
    sieciowy/jakościowa/fixed_total_net) left the invoice silently excluded
    from layout training and understated cost_breakdown for that month."""
    data = _data(warnings=[
        'stawka jakościowa nie znaleziona (użyto 0)',
        'opłata mocowa nie znaleziona',
        'abonament nie znaleziony',
        'składnik stały sieciowy nie znaleziony',
        'fixed_total_net nieobliczony — brakuje co najmniej jednej opłaty stałej',
    ])
    key = invoice_store.upsert(data, path=store_path)
    rec = invoice_store.get(key, store_path)
    assert rec['needs_training'] is True


def test_upsert_does_not_flag_training_for_plausibility_or_fallback_warnings(store_path):
    """A '(użyto 0)' fallback warning alone, or a 'walidacja:' plausibility
    check on a value the parser DID find (e.g. avg_price looking high in a
    low-import month), must not trigger needs_training — these aren't parse
    failures and flagging them would just be noise in the training queue."""
    data = _data(warnings=[
        'stawka jakościowa nie znaleziona (użyto 0)',
        'walidacja: średnia cena 3.5000 zł/kWh poza rozsądnym zakresem [0.10, 3.00]',
    ])
    key = invoice_store.upsert(data, path=store_path)
    rec = invoice_store.get(key, store_path)
    assert rec['needs_training'] is False


def test_upsert_no_warnings_needs_training_false(store_path):
    key = invoice_store.upsert(_data(), path=store_path)
    rec = invoice_store.get(key, store_path)
    assert rec['needs_training'] is False


# ── korekty bez numeru: deterministyczny klucz, wybór łańcuchowy, migracja ───

def _kor(month=11, year=2023, *, number=None, prev_dep=None, dep_prev=None, amount=None):
    d = _data(year=year, month=month)
    d.doc_type = 'korekta'
    d.invoice_number = number
    d.prev_deposit_previous_pln = prev_dep
    d.deposit_previous_pln = dep_prev
    d.deposit_used_pln = dep_prev
    d.amount_due_pln = amount
    return d


def test_numberless_korekta_same_document_overwrites_same_key(store_path):
    k1 = invoice_store.upsert(_kor(), filename='T_K1_1000002_24.pdf', path=store_path)
    k2 = invoice_store.upsert(_kor(), filename='ebok_T-K1-1000002-24.pdf', path=store_path)
    assert k1 == k2
    assert len([k for k in invoice_store.load(store_path) if '~kor~' in k]) == 1


def test_numberless_korekty_of_different_documents_get_different_keys(store_path):
    k1 = invoice_store.upsert(_kor(), filename='ebok_T-K1-1000001-24.pdf', path=store_path)
    k2 = invoice_store.upsert(_kor(), filename='ebok_T-K1-1000002-24.pdf', path=store_path)
    assert k1 != k2


def _store_chain(store_path):
    """2023-11: faktura + starsza korekta (50 → 48) i późniejsza (48 → 52).
    Klucze timestampowe, starsza sortuje się NA KOŃCU (jak po imporcie 19.10.2026)."""
    base = _data(year=2023, month=11)
    base.deposit_previous_pln = 50.00
    base.deposit_used_pln = 50.00
    base.amount_due_pln = 1000.00
    invoice_store.upsert(base, filename='fv.pdf', path=store_path)
    doc = invoice_store._load_document(store_path)
    older = invoice_store._to_record(_kor(prev_dep=50.00, dep_prev=48.00, amount=1010.00), 'ebok_T-K1-1000001-24.pdf', False)
    later = invoice_store._to_record(_kor(prev_dep=48.00, dep_prev=52.00, amount=1005.00), 'T_K1_1000002_24.pdf', False)
    doc['invoices']['2023-11~kor~1790601115'] = later
    doc['invoices']['2023-11~kor~1792388893'] = older
    invoice_store._save_document(doc, store_path)


def test_effective_by_month_picks_latest_correction_in_chain_not_last_key(store_path):
    _store_chain(store_path)
    eff = invoice_store.effective_by_month(invoice_store.load(store_path))
    assert eff['2023-11']['deposit_previous_pln'] == pytest.approx(52.00)
    assert eff['2023-11']['amount_due_pln'] == pytest.approx(1005.00)


def test_effective_by_month_falls_back_to_last_key_without_chain(store_path):
    base = _data(year=2024, month=3)
    invoice_store.upsert(base, filename='fv.pdf', path=store_path)
    invoice_store.upsert(_kor(3, 2024, number='T/K1/A/1/24', dep_prev=1.0, amount=10.0), path=store_path)
    invoice_store.upsert(_kor(3, 2024, number='T/K1/B/2/24', dep_prev=2.0, amount=20.0), path=store_path)
    eff = invoice_store.effective_by_month(invoice_store.load(store_path))
    assert eff['2024-03']['amount_due_pln'] == pytest.approx(20.0)


def test_dedupe_corrections_merges_copies_keeps_one_and_is_idempotent(store_path):
    invoice_store.upsert(_data(year=2024, month=12), filename='fv.pdf', path=store_path)
    doc = invoice_store._load_document(store_path)
    for i, name in enumerate(['T_K1_1000003_25.pdf', 'T_K1_1000003_25.pdf', 'ebok_T-K1-1000003-25.pdf']):
        rec = invoice_store._to_record(_kor(12, 2024, prev_dep=1.0, dep_prev=2.0, amount=3.0), name, False,
                                       pdf_path=None)
        rec['parsed_at'] = f'2026-10-19T07:0{i}:00+00:00'
        doc['invoices'][f'2024-12~kor~17900000{i}'] = rec
    invoice_store._save_document(doc, store_path)

    result = invoice_store.dedupe_corrections(store_path)
    kors = [k for k in invoice_store.load(store_path) if '~kor~' in k]
    assert kors == ['2024-12~kor~T_K1_1000003_25']   # najnowszy parsed_at, klucz kanoniczny
    assert len(result['removed']) == 2
    assert store_path.with_suffix('.json.pre-dedupe').exists()
    assert '2024-12' in invoice_store.load(store_path)             # faktura nietknięta

    again = invoice_store.dedupe_corrections(store_path)
    assert again['removed'] == [] and again['skipped'] is True


def test_dedupe_corrections_keeps_numbered_record_over_numberless_copy(store_path):
    invoice_store.upsert(_data(year=2024, month=12), filename='fv.pdf', path=store_path)
    invoice_store.upsert(_kor(12, 2024, number='T/K1/1000004/25', dep_prev=2.0), filename='T_K1_1000004_25.pdf',
                         path=store_path)
    doc = invoice_store._load_document(store_path)
    doc['invoices']['2024-12~kor~179000009'] = invoice_store._to_record(
        _kor(12, 2024, dep_prev=2.0), 'ebok_T-K1-1000004-25.pdf', False)
    invoice_store._save_document(doc, store_path)
    invoice_store.dedupe_corrections(store_path)
    assert [k for k in invoice_store.load(store_path) if '~kor~' in k] == ['2024-12~kor~T/K1/1000004/25']


def test_dedupe_corrections_does_not_merge_generic_filenames(store_path):
    invoice_store.upsert(_data(year=2024, month=5), filename='fv.pdf', path=store_path)
    doc = invoice_store._load_document(store_path)
    doc['invoices']['2024-05~kor~1790000001'] = invoice_store._to_record(
        _kor(5, 2024, prev_dep=1.0, dep_prev=2.0, amount=3.0), 'scan.pdf', False)
    doc['invoices']['2024-05~kor~1790000002'] = invoice_store._to_record(
        _kor(5, 2024, prev_dep=5.0, dep_prev=6.0, amount=7.0), 'scan.pdf', False)
    invoice_store._save_document(doc, store_path)
    assert invoice_store.dedupe_corrections(store_path)['removed'] == []
    assert len([k for k in invoice_store.load(store_path) if '~kor~' in k]) == 2


def test_dedupe_corrections_does_not_merge_when_values_differ(store_path):
    invoice_store.upsert(_data(year=2024, month=5), filename='fv.pdf', path=store_path)
    doc = invoice_store._load_document(store_path)
    doc['invoices']['2024-05~kor~1790000001'] = invoice_store._to_record(
        _kor(5, 2024, prev_dep=1.0, dep_prev=2.0, amount=3.0), 'T_K1_1000009_24.pdf', False)
    doc['invoices']['2024-05~kor~1790000002'] = invoice_store._to_record(
        _kor(5, 2024, prev_dep=1.0, dep_prev=2.5, amount=3.5), 'ebok_T-K1-1000009-24.pdf', False)
    invoice_store._save_document(doc, store_path)
    assert invoice_store.dedupe_corrections(store_path)['removed'] == []


def test_dedupe_corrections_moves_pdf_with_rekeyed_record_and_removes_dupe_pdf(store_path):
    invoice_store.upsert(_data(year=2024, month=5), filename='fv.pdf', path=store_path)
    doc = invoice_store._load_document(store_path)
    for key, name, parsed in (('2024-05~kor~1790000001', 'T_K1_1000009_24.pdf', '2026-06-01T00:00:00+00:00'),
                              ('2024-05~kor~1790000002', 'ebok_T-K1-1000009-24.pdf', '2026-10-19T07:00:00+00:00')):
        pdf = invoice_store.save_pdf(key, f'pdf-{key}'.encode(), store_path)
        rec = invoice_store._to_record(_kor(5, 2024, prev_dep=1.0, dep_prev=2.0, amount=3.0), name, False,
                                       pdf_path=pdf)
        rec['parsed_at'] = parsed
        doc['invoices'][key] = rec
    invoice_store._save_document(doc, store_path)

    invoice_store.dedupe_corrections(store_path)
    kors = {k: v for k, v in invoice_store.load(store_path).items() if '~kor~' in k}
    assert list(kors) == ['2024-05~kor~T_K1_1000009_24']
    new_pdf = Path(kors['2024-05~kor~T_K1_1000009_24']['pdf_path'])
    assert new_pdf.read_bytes() == b'pdf-2024-05~kor~1790000002'
    assert not invoice_store.pdf_path_for('2024-05~kor~1790000001', store_path).exists()
    # ponowny import tego samego dokumentu nadpisuje kanoniczny klucz zamiast dokładać kopię
    invoice_store.upsert(_kor(5, 2024, prev_dep=1.0, dep_prev=2.0, amount=3.0),
                         filename='ebok_T-K1-1000009-24.pdf', path=store_path)
    assert len([k for k in invoice_store.load(store_path) if '~kor~' in k]) == 1


def test_numberless_korekta_with_generic_filename_keeps_timestamp_key(store_path):
    k = invoice_store.upsert(_kor(), filename='scan.pdf', path=store_path)
    assert k.split('~kor~')[1].isdigit()


def test_effective_by_month_ignores_chain_when_deposit_missing(store_path):
    base = _data(year=2024, month=7)
    invoice_store.upsert(base, filename='fv.pdf', path=store_path)
    doc = invoice_store._load_document(store_path)
    a = invoice_store._to_record(_kor(7, 2024, prev_dep=1.0, dep_prev=2.0, amount=10.0), 'T_K1_1000011_24.pdf', False)
    b = invoice_store._to_record(_kor(7, 2024, prev_dep=2.0, dep_prev=None, amount=20.0), 'T_K1_1000012_24.pdf', False)
    doc['invoices']['2024-07~kor~1790000001'] = a
    doc['invoices']['2024-07~kor~1790000002'] = b
    invoice_store._save_document(doc, store_path)
    eff = invoice_store.effective_by_month(invoice_store.load(store_path))
    assert eff['2024-07']['amount_due_pln'] == pytest.approx(20.0)   # dawna reguła: ostatni klucz
