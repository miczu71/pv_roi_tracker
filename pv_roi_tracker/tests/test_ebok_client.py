"""
Unit tests for ebok_client.py.

Parsing helpers (HTML/JSON) are pure functions tested here on synthetic
fixtures (tests/fixtures/ebok_*) with fabricated payer, amounts and invoice
numbers — no real personal data is committed to this (public) repo.

Login/network behaviour is tested with a small FakeSession stand-in (no
requests_mock/responses dependency — this add-on pins its dependencies
deliberately, see requirements.txt).
"""
from __future__ import annotations

import base64
import json
from datetime import date
from pathlib import Path

import pytest
import requests

from pv_roi_tracker.ebok_client import (
    EbokClient,
    EbokError,
    EbokLoginBlocked,
    EbokLoginFailed,
    EbokLayoutChanged,
    PAID_STATUSES,
    is_known,
    known_signatures,
    parse_client_checksum,
    parse_documents_json,
)

_FIX = Path(__file__).parent / 'fixtures'
_ARCHIWUM_HTML = (_FIX / 'ebok_archiwum.html').read_text(encoding='utf-8')
_DOCS_JSON = json.loads((_FIX / 'ebok_documents.json').read_text(encoding='utf-8'))
_PDF_JSON = (_FIX / 'ebok_pdf.json').read_text(encoding='utf-8')
_PAYER = '11111111'
_CHECKSUM = 'a' * 64


# ── Fake HTTP session (no extra test dependency) ────────────────────────────

class _FakeResponse:
    def __init__(self, text: str = '', content: bytes = b'', status_code: int = 200):
        self.text = text
        self.content = content if content else text.encode('utf-8')
        self.status_code = status_code
        self.url = ''
        self.headers: dict = {}


class FakeSession:
    """Records requests and serves canned responses keyed by a URL substring
    match, in registration order (first match wins)."""

    def __init__(self):
        self.calls: list = []
        self._responses: list = []  # list of (substring, response)

    def when(self, url_substring: str, response: _FakeResponse):
        self._responses.append((url_substring, response))
        return self

    def _respond(self, method: str, url: str, **kwargs):
        self.calls.append((method, url, kwargs))
        for substring, resp in self._responses:
            if substring in url:
                return resp
        raise AssertionError(f'FakeSession: no canned response for {method} {url}')

    def get(self, url, **kwargs):
        return self._respond('GET', url, **kwargs)

    def post(self, url, **kwargs):
        return self._respond('POST', url, **kwargs)


_LOGIN_FORM_HTML = (
    '<form id="kc-form-login" action="https://logowanie.tauron.pl/action?x=1">'
)
_LOGGED_IN_HTML = '<html>witaj</html>'
_BLOCKED_HTML = 'Przekroczono maksymalną liczbę logowań.'


def _client_with_successful_login() -> tuple[EbokClient, FakeSession]:
    session = FakeSession()
    session.when('logowanie.tauron.pl/realms/ext/protocol/cas/login',
                 _FakeResponse(text=_LOGIN_FORM_HTML))
    session.when('logowanie.tauron.pl/action', _FakeResponse(text=_LOGGED_IN_HTML))
    client = EbokClient('user@example.com', 'secret', session=session)
    return client, session


# ── login() ──────────────────────────────────────────────────────────────────

def test_login_success_marks_client_logged_in():
    client, _ = _client_with_successful_login()
    client.login()
    assert client._logged_in is True


def test_login_is_a_noop_once_logged_in():
    client, session = _client_with_successful_login()
    client.login()
    calls_after_first = len(session.calls)
    client.login()  # should not issue any new requests
    assert len(session.calls) == calls_after_first


def test_login_wrong_credentials_raises():
    session = FakeSession()
    session.when('logowanie.tauron.pl/realms/ext/protocol/cas/login',
                 _FakeResponse(text=_LOGIN_FORM_HTML))
    session.when('logowanie.tauron.pl/action', _FakeResponse(text=_LOGIN_FORM_HTML))
    client = EbokClient('user@example.com', 'wrong', session=session)
    with pytest.raises(EbokLoginFailed):
        client.login()


def test_login_blocked_raises():
    session = FakeSession()
    session.when('logowanie.tauron.pl/realms/ext/protocol/cas/login',
                 _FakeResponse(text=_LOGIN_FORM_HTML))
    session.when('logowanie.tauron.pl/action', _FakeResponse(text=_BLOCKED_HTML))
    client = EbokClient('user@example.com', 'secret', session=session)
    with pytest.raises(EbokLoginBlocked):
        client.login()


def test_login_missing_form_raises_layout_changed():
    from pv_roi_tracker.ebok_client import EbokLayoutChanged
    session = FakeSession()
    session.when('logowanie.tauron.pl/realms/ext/protocol/cas/login',
                 _FakeResponse(text='<html>coś innego</html>'))
    client = EbokClient('user@example.com', 'secret', session=session)
    with pytest.raises(EbokLayoutChanged):
        client.login()


# ── wygasła sesja (incydent 2026-10-01) ─────────────────────────────────────

class _SequenceSession(FakeSession):
    """Jak FakeSession, ale dla danego podciągu URL zwraca kolejne odpowiedzi z listy."""

    def __init__(self):
        super().__init__()
        self._sequences: dict = {}

    def when_seq(self, url_substring: str, responses: list):
        self._sequences[url_substring] = list(responses)
        return self

    def _respond(self, method, url, **kwargs):
        for substring, seq in self._sequences.items():
            if substring in url and seq:
                self.calls.append((method, url, kwargs))
                return seq.pop(0) if len(seq) > 1 else seq[0]
        return super()._respond(method, url, **kwargs)


_LANDING_HTML = _ARCHIWUM_HTML


def _seq_client() -> tuple[EbokClient, _SequenceSession]:
    session = _SequenceSession()
    session.when('logowanie.tauron.pl/realms/ext/protocol/cas/login',
                 _FakeResponse(text=_LOGIN_FORM_HTML))
    session.when('logowanie.tauron.pl/action', _FakeResponse(text=_LOGGED_IN_HTML))
    return EbokClient('user@example.com', 'secret', session=session), session


def test_expired_session_relogs_and_retries_once():
    client, session = _seq_client()
    session.when_seq('/archiwum-dokumentow', [_FakeResponse(text=_LOGIN_FORM_HTML),
                                              _FakeResponse(text=_ARCHIWUM_HTML)])
    client.login()
    client.select_payer(_PAYER)
    assert client._checksum == _CHECKSUM
    posts = [c for c in session.calls if c[0] == 'POST']
    assert len(posts) == 2  # logowanie startowe + ponowne po wygaśnięciu sesji


def test_session_still_expired_after_relogin_raises_clear_error():
    client, session = _seq_client()
    session.when_seq('/archiwum-dokumentow', [_FakeResponse(text=_LOGIN_FORM_HTML)])
    with pytest.raises(EbokError, match='Sesja eBOK'):
        client.select_payer(_PAYER)


def test_select_payer_unknown_payer_raises():
    client, session = _seq_client()
    session.when('/archiwum-dokumentow', _FakeResponse(text=_ARCHIWUM_HTML))
    with pytest.raises(EbokError, match='nie znaleziony'):
        client.select_payer('00000000')


def test_select_payer_uses_service_url_and_picks_matching_checksum():
    client, session = _seq_client()
    session.when('/archiwum-dokumentow', _FakeResponse(text=_ARCHIWUM_HTML))
    client.select_payer('22222222')
    assert client._checksum == 'b' * 64
    gets = [c[1] for c in session.calls if c[0] == 'GET' and 'archiwum-dokumentow' in c[1]]
    assert gets == ['https://moj.tauron.pl/Home/Pages/archiwum-dokumentow']


# ── ponawianie przy timeoucie / błędzie połączenia ──────────────────────────

class _FlakyGetSession(_SequenceSession):
    """Rzuca `exc` przy pierwszych `fail_times` GET-ach pasujących do `substring`."""

    def __init__(self, substring: str, exc: Exception, fail_times: int):
        super().__init__()
        self._flaky = [substring, exc, fail_times]

    def get(self, url, **kwargs):
        substring, exc, left = self._flaky
        if substring in url and left > 0:
            self._flaky[2] -= 1
            self.calls.append(('GET', url, kwargs))
            raise exc
        return super().get(url, **kwargs)


def _flaky_client(fail_times: int, exc=None):
    session = _FlakyGetSession('/archiwum-dokumentow', exc or requests.exceptions.ReadTimeout('timed out'),
                               fail_times)
    session.when('logowanie.tauron.pl/realms/ext/protocol/cas/login',
                 _FakeResponse(text=_LOGIN_FORM_HTML))
    session.when('logowanie.tauron.pl/action', _FakeResponse(text=_LOGGED_IN_HTML))
    session.when('/archiwum-dokumentow', _FakeResponse(text=_ARCHIWUM_HTML))
    sleeps: list = []
    client = EbokClient('user@example.com', 'secret', session=session, sleep=sleeps.append)
    return client, session, sleeps


def test_get_retries_after_timeout_with_growing_delays():
    client, session, sleeps = _flaky_client(fail_times=2)
    client.select_payer(_PAYER)
    assert sleeps == [10, 30]


def test_get_retries_after_connection_error():
    client, _, sleeps = _flaky_client(
        fail_times=1, exc=requests.exceptions.ConnectionError('reset'))
    client.select_payer(_PAYER)
    assert sleeps == [10]


def test_get_gives_up_after_three_retries():
    client, session, sleeps = _flaky_client(fail_times=99)
    with pytest.raises(requests.exceptions.ReadTimeout):
        client.select_payer(_PAYER)
    assert sleeps == [10, 30, 90]
    attempts = [c for c in session.calls if c[0] == 'GET' and '/archiwum-dokumentow' in c[1]]
    assert len(attempts) == 4  # próba + 3 ponowienia


def test_login_post_is_never_retried():
    """Ponowny POST logowania po timeoucie zbliża do blokady konta przez Tauron."""
    class _TimeoutOnPost(FakeSession):
        def post(self, url, **kwargs):
            self.calls.append(('POST', url, kwargs))
            raise requests.exceptions.ReadTimeout('timed out')

    session = _TimeoutOnPost()
    session.when('logowanie.tauron.pl/realms/ext/protocol/cas/login',
                 _FakeResponse(text=_LOGIN_FORM_HTML))
    sleeps: list = []
    client = EbokClient('user@example.com', 'secret', session=session, sleep=sleeps.append)
    with pytest.raises(requests.exceptions.ReadTimeout):
        client.login()
    assert [c[0] for c in session.calls].count('POST') == 1
    assert sleeps == []



# ── parse_client_checksum() ─────────────────────────────────────────────────

def test_parse_client_checksum_picks_row_for_payer():
    assert parse_client_checksum(_ARCHIWUM_HTML, '11111111') == 'a' * 64
    assert parse_client_checksum(_ARCHIWUM_HTML, '22222222') == 'b' * 64
    assert parse_client_checksum(_ARCHIWUM_HTML, '33333333') is None


def test_parse_client_checksum_tolerates_attribute_order():
    html = '<div data-system="T18" data-client-checksum="%s" data-client-number="11111111">' % ('c' * 64)
    assert parse_client_checksum(html, '11111111') == 'c' * 64


# ── parse_documents_json() ──────────────────────────────────────────────────

def test_parse_documents_json_maps_fields():
    docs = parse_documents_json(_DOCS_JSON)
    assert [d.signature for d in docs] == [
        'T/K1/BN111111/0015/26', 'T/K1/BN111111/0016/26', 'T/K1/BN111111/0017/26']
    fv, fk, unpaid = docs
    assert fv.paid is True and fv.doc_type == 'FV' and fv.status == 'Opłacona'
    assert fv.amount_gross_pln == pytest.approx(100.5)
    assert fv.due_date == date(2026, 8, 19) and fv.issued_on == '05.08.2026'
    assert fv.doc_name == 'Faktura rzeczywista'
    # względny Url dołączany do SERVICE, bezwzględny bez zmian
    assert fv.pdf_url == 'https://moj.tauron.pl/api/sitecore/EbokArchiveDocuments/GetDocumentImage?id=1&mode=1'
    assert fk.pdf_url == 'https://moj.tauron.pl/api/sitecore/EbokArchiveDocuments/GetDocumentImage?id=2&mode=1'
    assert fk.paid is True and fk.doc_type == 'FK' and fk.due_date is None
    assert fk.amount_gross_pln == pytest.approx(-12.34)


def test_parse_documents_json_unknown_status_is_unpaid_and_logged(caplog):
    with caplog.at_level('INFO'):
        docs = parse_documents_json(_DOCS_JSON)
    unpaid = docs[2]
    assert unpaid.paid is False and unpaid.status == 'Do zapłaty'
    assert unpaid.due_date == date(2026, 10, 19)  # data ISO
    assert 'Do zapłaty' in ' '.join(r.getMessage() for r in caplog.records)
    assert not any('Opłacona' in r.getMessage() for r in caplog.records)


def test_paid_statuses():
    assert PAID_STATUSES == {'Opłacona', 'Rozliczono', 'Zapłacona'}


def test_parse_documents_json_missing_values_are_none():
    raw = {'Status': 'OK', 'Documents': [{'DocSignature': 'X/1', 'DocType': 'NB', 'Status': 'Rozliczono',
                                          'AmountGross': None, 'PaymentDate': '---', 'Url': None}]}
    d = parse_documents_json(raw)[0]
    assert d.amount_gross_pln is None and d.due_date is None and d.pdf_url is None


def test_parse_documents_json_skips_rows_without_signature():
    assert parse_documents_json({'Documents': [{'DocSignature': ''}, {}]}) == []


# ── list_documents() / download_pdf() wiring ────────────────────────────────

def _selected_client():
    client, session = _client_with_successful_login()
    session.when('/archiwum-dokumentow', _FakeResponse(text=_ARCHIWUM_HTML))
    client.select_payer(_PAYER)
    return client, session


def test_list_documents_returns_parsed_rows_and_sends_filters():
    client, session = _selected_client()
    session.when('GetDocuments', _FakeResponse(text=json.dumps(_DOCS_JSON)))
    docs = client.list_documents(date(2015, 1, 1), date(2026, 10, 27))
    assert len(docs) == 3
    url = [c[1] for c in session.calls if 'GetDocuments' in c[1]][0]
    for part in ('https://moj.tauron.pl/api/sitecore/EbokArchiveDocuments/GetDocuments',
                 'rangeFrom=1', 'rangeTo=100', f'clientNumber={_PAYER}',
                 f'clientNumberCheckSum={_CHECKSUM}', 'system=T18',
                 'from=01.01.2015', 'to=27.10.2026', 'docKind=All'):
        assert part in url


def test_list_documents_paginates_until_total():
    client, session = _selected_client()

    def page(first, last, total):
        return json.dumps({'Status': 'OK', 'TotalNumber': total, 'Documents': [
            {'DocSignature': f'T/K1/BN111111/{i:04d}/26', 'DocType': 'FV', 'Status': 'Opłacona'}
            for i in range(first, last + 1)]})

    session.when('rangeFrom=1&', _FakeResponse(text=page(1, 100, 250)))
    session.when('rangeFrom=101&', _FakeResponse(text=page(101, 200, 250)))
    session.when('rangeFrom=201&', _FakeResponse(text=page(201, 250, 250)))
    docs = client.list_documents(date(2015, 1, 1), date(2026, 10, 27))
    assert len(docs) == 250
    urls = [c[1] for c in session.calls if 'GetDocuments' in c[1]]
    assert len(urls) == 3 and 'rangeFrom=201&rangeTo=300' in urls[2]


def test_list_documents_page_limit(monkeypatch, caplog):
    client, session = _selected_client()
    body = json.dumps({'Status': 'OK', 'TotalNumber': 99999, 'Documents': [
        {'DocSignature': 'T/K1/BN111111/0001/26', 'DocType': 'FV', 'Status': 'Opłacona'}]})
    session.when('GetDocuments', _FakeResponse(text=body))
    client.list_documents(date(2015, 1, 1), date(2026, 10, 27))
    assert len([c for c in session.calls if 'GetDocuments' in c[1]]) == 10
    assert 'obcięta' in caplog.text


def test_parse_documents_json_foreign_host_pdf_url_is_dropped(caplog):
    data = {'Documents': [{'DocSignature': 'T/K1/BN111111/0001/26', 'DocType': 'FV',
                           'Status': 'Opłacona', 'Url': 'https://evil.example/x.pdf'}]}
    (doc,) = parse_documents_json(data)
    assert doc.pdf_url is None
    assert 'spoza' in caplog.text


def test_list_documents_status_not_ok_raises_layout_changed():
    client, session = _selected_client()
    session.when('GetDocuments', _FakeResponse(text=json.dumps({'Status': 'Error', 'Documents': []})))
    with pytest.raises(EbokLayoutChanged):
        client.list_documents(date(2015, 1, 1), date(2026, 10, 27))


def test_list_documents_non_json_raises_layout_changed():
    client, session = _selected_client()
    session.when('GetDocuments', _FakeResponse(text='<html>Strona główna</html>'))
    with pytest.raises(EbokLayoutChanged):
        client.list_documents(date(2015, 1, 1), date(2026, 10, 27))


def test_list_documents_bad_http_status_raises():
    client, session = _selected_client()
    session.when('GetDocuments', _FakeResponse(status_code=500))
    with pytest.raises(EbokError):
        client.list_documents(date(2015, 1, 1), date(2026, 10, 27))


def test_list_documents_without_selected_payer_raises():
    client, _ = _client_with_successful_login()
    with pytest.raises(EbokError):
        client.list_documents(date(2015, 1, 1), date(2026, 10, 27))


def test_download_pdf_decodes_base64_document():
    client, session = _client_with_successful_login()
    session.when('GetDocumentImage', _FakeResponse(text=_PDF_JSON))
    pdf = client.download_pdf('https://moj.tauron.pl/api/sitecore/EbokArchiveDocuments/GetDocumentImage?id=1&mode=1')
    assert pdf.startswith(b'%PDF-1.4')
    assert pdf == base64.b64decode(json.loads(_PDF_JSON)['Document'])


def test_download_pdf_non_pdf_payload_raises():
    client, session = _client_with_successful_login()
    body = json.dumps({'Status': 'OK', 'Document': base64.b64encode(b'<html>error</html>').decode()})
    session.when('GetDocumentImage', _FakeResponse(text=body))
    with pytest.raises(EbokError):
        client.download_pdf('https://moj.tauron.pl/api/x/GetDocumentImage?id=1')


def test_download_pdf_non_json_or_bad_status_raises():
    client, session = _client_with_successful_login()
    session.when('GetDocumentImage', _FakeResponse(text='<html>error page</html>'))
    with pytest.raises(EbokError):
        client.download_pdf('https://moj.tauron.pl/api/x/GetDocumentImage?id=1')
    client2, session2 = _client_with_successful_login()
    session2.when('GetDocumentImage', _FakeResponse(status_code=500))
    with pytest.raises(EbokError):
        client2.download_pdf('https://moj.tauron.pl/api/x/GetDocumentImage?id=1')


def test_known_signatures_uses_invoice_number_and_ebok_filename():
    known = known_signatures([
        {'invoice_number': 'T/K1/X/0017/26', 'filename': 'upload.pdf'},
        {'invoice_number': '', 'filename': 'ebok_T-K1-0262015-25.pdf'},
        {'filename': 'recznie.pdf'},
    ])
    assert is_known('T/K1/X/0017/26', known) and is_known('T/K1/0262015/25', known)
    assert is_known('T/K1/0262015/25', known_signatures([{'filename': 'T_K1_0262015_25.pdf'}]))
    assert not is_known('T/K1/X/0018/26', known)


def test_mask_hides_payer_and_email_and_tolerates_empty_payer():
    from pv_roi_tracker.ebok_client import _mask
    assert _mask('płatnik 11111111 a@b.pl', '11111111') == 'płatnik <płatnik> <email>'
    assert _mask('abc', '') == 'abc'
