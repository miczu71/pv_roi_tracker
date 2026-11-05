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
    RETRY_DELAYS,
    is_known,
    known_signatures,
    parse_client_checksum,
    parse_documents_json,
    parse_home_contract,
    parse_payments_json,
    parse_point_details,
    parse_unpaid_invoices_html,
    parse_unpaid_source,
)

_FIX = Path(__file__).parent / 'fixtures'
_ARCHIWUM_HTML = (_FIX / 'ebok_archiwum.html').read_text(encoding='utf-8')
_DOCS_JSON = json.loads((_FIX / 'ebok_documents.json').read_text(encoding='utf-8'))
_PAYMENTS_JSON = json.loads((_FIX / 'ebok_payments.json').read_text(encoding='utf-8'))
_PDF_JSON = (_FIX / 'ebok_pdf.json').read_text(encoding='utf-8')
_HOME_HTML = (_FIX / 'ebok_home.html').read_text(encoding='utf-8')
_PUNKT_HTML = (_FIX / 'ebok_punkt.html').read_text(encoding='utf-8')
_UMOWA_HTML = '<a href="/Home/Pages/punkty-poboru?sid=T18&amp;pp=11111111">Punkty poboru</a>'
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


def test_login_post_is_not_retried_after_read_timeout():
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



class _DropOnPost(FakeSession):
    """POST logowania zrywa połączenie `drops` razy (RemoteDisconnected), potem działa."""
    def __init__(self, drops: int, exc=None):
        super().__init__()
        self._drops = drops
        self._exc = exc or requests.exceptions.ConnectionError(
            'Connection aborted.', 'RemoteDisconnected')
        self._forms = 0

    def get(self, url, **kwargs):
        if 'cas/login' in url:  # każdy formularz ma inny action (świeży token)
            self._forms += 1
            self.calls.append(('GET', url, kwargs))
            return _FakeResponse(text=_LOGIN_FORM_HTML.replace('x=1', f'x={self._forms}'))
        return super().get(url, **kwargs)

    def post(self, url, **kwargs):
        if self._drops > 0:
            self._drops -= 1
            self.calls.append(('POST', url, kwargs))
            raise self._exc
        return super().post(url, **kwargs)


def _drop_on_post_client(drops: int):
    session = _DropOnPost(drops)
    session.when('logowanie.tauron.pl/realms/ext/protocol/cas/login',
                 _FakeResponse(text=_LOGIN_FORM_HTML))
    session.when('logowanie.tauron.pl/action', _FakeResponse(text=_LOGGED_IN_HTML))
    sleeps: list = []
    client = EbokClient('user@example.com', 'secret', session=session, sleep=sleeps.append)
    return client, session, sleeps


def test_login_post_retried_once_after_dropped_connection():
    """Zerwane połączenie bez odpowiedzi = serwer nie przetworzył logowania → jedno
    ponowienie z nowym formularzem (świeży action)."""
    client, session, sleeps = _drop_on_post_client(drops=1)
    client.login()
    assert client._logged_in is True
    methods = [c[0] for c in session.calls]
    assert methods == ['GET', 'POST', 'GET', 'POST']
    assert sleeps == [RETRY_DELAYS[0]]
    posts = [c[1] for c in session.calls if c[0] == 'POST']
    assert posts == ['https://logowanie.tauron.pl/action?x=1', 'https://logowanie.tauron.pl/action?x=2']


def test_login_post_gives_up_after_second_dropped_connection():
    client, session, sleeps = _drop_on_post_client(drops=2)
    with pytest.raises(requests.exceptions.ConnectionError):
        client.login()
    assert [c[0] for c in session.calls].count('POST') == 2  # nigdy więcej niż 2
    assert client._logged_in is False


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
    session.when('/Home/Pages/Faktury', _FakeResponse(text='<html></html>'))   # brak źródła nierozliczonych
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


def test_list_documents_empty_page_before_total_warns(caplog):
    client, session = _selected_client()
    body = json.dumps({'Status': 'OK', 'TotalNumber': 5, 'Documents': []})
    session.when('GetDocuments', _FakeResponse(text=body))
    assert client.list_documents(date(2015, 1, 1), date(2026, 10, 27)) == []
    assert 'niepełna' in caplog.text


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


def test_download_pdf_error_status_logs_masked_shape(caplog):
    client, session = _client_with_successful_login()
    body = json.dumps({'Status': 'Error', 'Message': 'Dokument SECRET-123 niedostepny',
                       'Document': None, 'Extra': {'a': 1}})
    session.when('GetDocumentImage', _FakeResponse(text=body))
    url = ('https://moj.tauron.pl/api/sitecore/EbokArchiveDocuments/GetDocumentImage'
           '?clientNumber=CLIENT42&clientNumberCheckSum=' + 'ab' * 32
           + '&system=T18&docNumber=DOCNUM9&docType=FV&sodId=SOD7&docMode=1')
    with caplog.at_level('WARNING'):
        with pytest.raises(EbokError):
            client.download_pdf(url)
    text = caplog.text
    assert 'docType=FV' in text and 'docMode=1' in text
    assert 'Status' in text and "'Error'" in text
    assert 'Message' in text and 'Document' in text and 'Extra' in text
    for secret in ('SECRET-123', 'CLIENT42', 'DOCNUM9', 'SOD7', 'ab' * 32):
        assert secret not in text


def test_download_pdf_ok_status_without_pdf_logs_shape(caplog):
    client, session = _client_with_successful_login()
    body = json.dumps({'Status': 'OK', 'Document': ''})
    session.when('GetDocumentImage', _FakeResponse(text=body))
    with caplog.at_level('WARNING'):
        with pytest.raises(EbokError):
            client.download_pdf('https://moj.tauron.pl/api/x/GetDocumentImage?docType=FV&docMode=1')
    assert 'Document=str[0]' in caplog.text and 'docType=FV' in caplog.text


def test_download_pdf_non_json_logs_length_only(caplog):
    client, session = _client_with_successful_login()
    session.when('GetDocumentImage', _FakeResponse(text='<html>tajne 555</html>'))
    with caplog.at_level('WARNING'):
        with pytest.raises(EbokError):
            client.download_pdf('https://moj.tauron.pl/api/x/GetDocumentImage?docType=FV&docMode=2')
    assert 'docMode=2' in caplog.text and 'tajne' not in caplog.text


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


def test_parsed_documents_feed_payment_reminders(tmp_path):
    """Kontrakt klient ↔ payment_reminders: niezapłacona faktura z fixture'a daje
    przypomnienie i aktywną płatność, opłacone dokumenty są pomijane."""
    from pv_roi_tracker.payment_reminders import (
        apply_refresh, load_payment_state, select_active_payment)
    docs = parse_documents_json(_DOCS_JSON)
    unpaid = next(d for d in docs if not d.paid)
    path = tmp_path / 'payment_state.json'
    today = unpaid.due_date

    notes = apply_refresh(docs, path, today)

    assert [n['signature'] for n in notes] == [unpaid.signature]
    active = select_active_payment(load_payment_state(path), today)
    assert active['signature'] == unpaid.signature


def test_paid_status_comparison_ignores_case_and_spaces():
    data = {'Documents': [{'DocSignature': 'T/K1/BN111111/0001/26', 'Status': ' OPŁACONA '}]}
    assert parse_documents_json(data)[0].paid is True


def test_select_payer_empty_with_several_clients_raises():
    client, session = _seq_client()
    session.when('/archiwum-dokumentow', _FakeResponse(text=_ARCHIWUM_HTML))
    with pytest.raises(EbokError, match='ebok_payer_id'):
        client.select_payer('')


def test_select_payer_empty_picks_sole_client():
    html = '<div data-client-number="11111111" data-client-checksum="' + 'a' * 64 + '" data-system="T18"></div>'
    client, session = _seq_client()
    session.when('/archiwum-dokumentow', _FakeResponse(text=html))
    client.select_payer('')
    assert client._payer_id == '11111111' and client._checksum == 'a' * 64


# ── kontrakt: parse_home_contract / parse_point_details / read_contract ─────

def test_parse_home_contract_reads_dl_horizontal_pairs():
    assert parse_home_contract(_HOME_HTML) == {
        'umowa': 'UMOWA-TEST-1', 'taryfa': 'G12W', 'faktura': 'Elektroniczna'}


def test_parse_home_contract_missing_field_has_no_key():
    html = '<dl class="dl-horizontal"><dt>Taryfa</dt><dd>G11</dd></dl>'
    assert parse_home_contract(html) == {'taryfa': 'G11'}


def test_parse_home_contract_ignores_other_dl_and_empty_page():
    assert parse_home_contract('<dl><dt>Taryfa</dt><dd>G11</dd></dl>') == {}
    assert parse_home_contract('') == {}


def test_parse_point_details_reads_four_labels_only():
    assert parse_point_details(_PUNKT_HTML) == {
        'taryfa': 'G12W', 'moc_umowna': '5 kW',
        'rozliczenie': 'co 1 mies.', 'osd': 'OSD Test'}


def test_parse_point_details_skips_unknown_labels():
    html = '<dt>Numer licznika</dt><dd>X1</dd><dt>Taryfa</dt><dd>G13</dd>'
    assert parse_point_details(html) == {'taryfa': 'G13'}


def _contract_client(umowa_html: str = _UMOWA_HTML, punkt=None, home_html: str = _HOME_HTML):
    client, session = _client_with_successful_login()
    session.when('moj.tauron.pl/Home/Pages/Umowa', _FakeResponse(text=umowa_html))
    session.when('moj.tauron.pl/Home/Pages/punkty-poboru',
                 punkt if punkt is not None else _FakeResponse(text=_PUNKT_HTML))
    session.when('moj.tauron.pl/', _FakeResponse(text=home_html))
    client._payer_id = _PAYER
    return client, session


def test_read_contract_merges_home_and_point_details_details_win():
    client, session = _contract_client()
    contract = client.read_contract()
    assert contract == {'umowa': 'UMOWA-TEST-1', 'taryfa': 'G12W', 'faktura': 'Elektroniczna',
                        'moc_umowna': '5 kW', 'rozliczenie': 'co 1 mies.', 'osd': 'OSD Test'}
    umowa = [c[1] for c in session.calls if 'Home/Pages/Umowa' in c[1]]
    assert umowa == [f'https://moj.tauron.pl/Home/Pages/Umowa?sid=T18&pp={_PAYER}']
    punkt = [c[1] for c in session.calls if 'punkty-poboru' in c[1]]
    assert punkt == ['https://moj.tauron.pl/Home/Pages/punkty-poboru?sid=T18&pp=11111111']


def test_read_contract_details_taryfa_overrides_home():
    punkt = _FakeResponse(text='<dt>Taryfa</dt><dd>G13</dd>')
    client, _ = _contract_client(punkt=punkt)
    assert client.read_contract()['taryfa'] == 'G13'


def test_read_contract_point_details_error_keeps_home_result(caplog):
    client, session = _contract_client()
    original = session._respond

    def _respond(method, url, **kwargs):
        if 'punkty-poboru' in url:
            raise ValueError('boom szczegółów')
        return original(method, url, **kwargs)
    session._respond = _respond
    with caplog.at_level('WARNING'):
        contract = client.read_contract()
    assert contract == {'umowa': 'UMOWA-TEST-1', 'taryfa': 'G12W', 'faktura': 'Elektroniczna'}
    assert any(r.levelname == 'WARNING' for r in caplog.records)


def test_read_contract_without_taryfa_anywhere_raises_layout_changed():
    client, _ = _contract_client(umowa_html='<p>brak linku</p>', home_html='<p>pusto</p>')
    with pytest.raises(EbokLayoutChanged, match='taryfy'):
        client.read_contract()


# ── parse_payments_json() / list_payments() ─────────────────────────────────

def test_parse_payments_json_maps_fields():
    payments = parse_payments_json(_PAYMENTS_JSON)
    assert len(payments) == 5
    p = payments[1]
    assert (p.date, p.amount, p.type, p.doc_number) == (date(2026, 8, 15), 60.5, 'Wpłata', 'WPL/0002/26')
    assert p.settlements == [('T/K1/BN111111/0015/26', 100.5, 60.5)]
    assert len(payments[3].settlements) == 2
    assert payments[2].type == 'Zwrot'


def test_parse_payments_json_row_without_details_has_no_settlements():
    assert parse_payments_json(_PAYMENTS_JSON)[4].settlements == []


def test_parse_payments_json_unknown_type_logged_once_and_kept(caplog):
    data = {'Documents': [
        {'Amount': 1, 'Date': '01.01.2026', 'Type': 'Korekta', 'DocNumber': 'A'},
        {'Amount': 2, 'Date': '02.01.2026', 'Type': 'Korekta', 'DocNumber': 'B'},
        {'Amount': 3, 'Date': '03.01.2026', 'Type': 'Wpłata', 'DocNumber': 'C'},
    ]}
    with caplog.at_level('INFO'):
        payments = parse_payments_json(data)
    assert len(payments) == 3
    assert caplog.text.count('Korekta') == 1
    assert 'Wpłata' not in caplog.text


def test_parse_payments_json_garbage_gives_empty_list():
    for junk in (None, {}, [], 'x', {'Documents': None}, {'Documents': 'x'}, {'Documents': [None, 5, 'a']}):
        assert parse_payments_json(junk) == []


def test_parse_payments_json_tolerates_bad_fields():
    data = {'Documents': [{'Type': 'Wpłata', 'Date': 'nie data', 'Amount': 'abc',
                           'Details': [{'PaymentAmount': 5}, 'x', {'DocSignature': 'S/1'}]}]}
    (p,) = parse_payments_json(data)
    assert p.date is None and p.amount is None
    assert p.settlements == [('S/1', None, None)]


_PAY_CHECKSUM = 'c' * 64
_LISTA_WPLAT_HTML = (f'<div class="client" data-client-number="{_PAYER}" '
                     f'data-client-checksum="{_PAY_CHECKSUM}" data-system="T18">Miasto A</div>')


def _payments_client():
    client, session = _selected_client()
    session.when('/lista-wplat', _FakeResponse(text=_LISTA_WPLAT_HTML))
    return client, session


def test_list_payments_uses_own_checksum_and_url():
    client, session = _payments_client()
    session.when('GetPayments', _FakeResponse(text=json.dumps(_PAYMENTS_JSON)))
    payments = client.list_payments(date(2025, 10, 1), date(2026, 10, 27))
    assert len(payments) == 5
    url = [c[1] for c in session.calls if 'GetPayments' in c[1]][0]
    for part in ('https://moj.tauron.pl/api/sitecore/EbokPaymentsHistory/GetPayments',
                 'rangeFrom=1', 'rangeTo=100', f'clientNumber={_PAYER}',
                 f'clientNumberCheckSum={_PAY_CHECKSUM}', 'system=T18',
                 'from=01.10.2025', 'to=27.10.2026', 'docKind=All', 'paymentFrom=', 'paymentTo='):
        assert part in url
    assert _CHECKSUM not in url


def test_list_payments_paginates_until_total():
    client, session = _payments_client()

    def page(first, last, total):
        return json.dumps({'Status': 'OK', 'TotalNumber': total, 'Documents': [
            {'Amount': 1, 'Date': '01.01.2026', 'Type': 'Wpłata', 'DocNumber': str(i)}
            for i in range(first, last + 1)]})

    session.when('rangeFrom=1&', _FakeResponse(text=page(1, 100, 150)))
    session.when('rangeFrom=101&', _FakeResponse(text=page(101, 150, 150)))
    assert len(client.list_payments(date(2025, 10, 1), date(2026, 10, 27))) == 150
    assert len([c for c in session.calls if 'GetPayments' in c[1]]) == 2


def test_list_payments_without_payer_raises():
    client, _ = _client_with_successful_login()
    with pytest.raises(EbokError):
        client.list_payments(date(2025, 10, 1), date(2026, 10, 27))


def test_list_payments_payer_missing_on_page_raises():
    client, session = _selected_client()
    session.when('/lista-wplat', _FakeResponse(text='<div>pusto</div>'))
    with pytest.raises(EbokError):
        client.list_payments(date(2025, 10, 1), date(2026, 10, 27))


def test_list_payments_status_not_ok_raises_layout_changed():
    client, session = _payments_client()
    session.when('GetPayments', _FakeResponse(text=json.dumps({'Status': 'Error'})))
    with pytest.raises(EbokLayoutChanged):
        client.list_payments(date(2025, 10, 1), date(2026, 10, 27))


def test_list_payments_missing_documents_list_raises_layout_changed():
    client, session = _payments_client()
    session.when('GetPayments', _FakeResponse(text=json.dumps({'Status': 'OK', 'TotalNumber': 0})))
    with pytest.raises(EbokLayoutChanged):
        client.list_payments(date(2025, 10, 1), date(2026, 10, 27))


# ── probe_invoices_page() (sonda diagnostyczna 0.55.5) ───────────────────────

_FAKTURY_HTML = (_FIX / 'ebok_faktury.html').read_text(encoding='utf-8')
_FAKTURY_JS = (_FIX / 'ebok_faktury.js').read_text(encoding='utf-8')


def _probe_client():
    client, session = _seq_client()
    session.when('/Home/Pages/Faktury', _FakeResponse(text=_FAKTURY_HTML))
    session.when('/assets/invoices.js', _FakeResponse(text=_FAKTURY_JS))
    client._payer_id = _PAYER
    return client, session


def test_probe_invoices_page_finds_api_paths_and_unknown_signatures():
    client, session = _probe_client()
    out = client.probe_invoices_page(known={'T_K1_XX000000_0018_26'})
    assert out['status'] == 200
    assert out['size'] == len(_FAKTURY_HTML)
    assert out['api_paths'] == ['/api/sitecore/EbokInvoices/GetPdf',
                                '/api/sitecore/EbokInvoices/GetUnpaid?clientNumber&rangeFrom',
                                '/api/sitecore/EbokInvoices/GetUnpaid?x']
    assert out['unknown_signatures'] == ['T/K1/XX000000/0020/26']
    assert 'data-client-checksum' in out['data_attrs']


def test_probe_invoices_page_fetches_only_own_domain_scripts():
    client, session = _probe_client()
    client.probe_invoices_page(known=set())
    urls = [c[1] for c in session.calls if c[0] == 'GET']
    assert any(u == 'https://moj.tauron.pl/assets/invoices.js' for u in urls)
    assert not any('cdn.example.com' in u for u in urls)


def test_probe_invoices_page_never_leaks_payer_or_checksum():
    client, session = _probe_client()
    out = client.probe_invoices_page(known=set())
    text = json.dumps(out, ensure_ascii=False)
    assert _PAYER not in text
    assert 'c' * 64 not in text


def test_probe_invoices_page_script_failure_does_not_break_probe():
    client, session = _probe_client()
    session._responses.insert(0, ('/assets/invoices.js', _FakeResponse(text='', status_code=500)))
    out = client.probe_invoices_page(known=set())
    assert out['status'] == 200
    assert '/api/sitecore/EbokInvoices/GetUnpaid?clientNumber&rangeFrom' in out['api_paths']  # z atrybutu HTML


def test_probe_invoices_page_matches_old_three_segment_signature():
    client, session = _probe_client()
    session._responses.insert(0, ('/Home/Pages/Faktury', _FakeResponse(
        text='<td>T/K1/0143787/24</td><td>T/K1/XX000000/0020/26</td>')))
    out = client.probe_invoices_page(known=set())
    assert out['unknown_signatures'] == ['T/K1/0143787/24', 'T/K1/XX000000/0020/26']
    assert out['signatures_total'] == 2


def test_probe_invoices_page_reports_script_stats():
    client, session = _probe_client()
    session._responses.insert(0, ('/assets/invoices.js', _FakeResponse(text='', status_code=500)))
    out = client.probe_invoices_page(known=set())
    assert out['scripts'] == {'total': 1, 'fetched': 0, 'failed': 1, 'skipped': 0}


def test_probe_invoices_page_requires_selected_payer():
    client, session = _probe_client()
    client._payer_id = None
    with pytest.raises(EbokError, match='płatnika'):
        client.probe_invoices_page(known=set())


def test_probe_invoices_page_expired_session_raises():
    client, session = _seq_client()
    session.when_seq('/Home/Pages/Faktury', [_FakeResponse(text=_LOGIN_FORM_HTML)])
    client._payer_id = _PAYER
    with pytest.raises(EbokError, match='Sesja eBOK'):
        client.probe_invoices_page(known=set())


# ── E0b: kontekst JS, szkielet wiersza, kształt LoadMoreInvoicesProducts (0.55.6) ──

_ITEM_HTML = ('<table class="list"><tr class="head"><th>Numer</th></tr><tr class="row paid" data-item="1">'
              '<td class="no">T/K1/XX000000/0020/26</td><td class="amt">123,45 zł</td>'
              '<td><em class="st">Nieopłacona</em></td></tr></table>')


def test_probe_invoices_page_finds_inline_script_context():
    client, session = _probe_client()
    session._responses.insert(0, ('/Home/Pages/Faktury', _FakeResponse(
        text="<script>$.get('/api/sitecore/EbokInvoiceList/LoadMoreInvoicesProducts?pp=' + pp + '&page=' + n)</script>")))
    out = client.probe_invoices_page(known=set())
    assert any("'&page=' + n" in c for c in out['js_context'])
    assert out['item_skeleton'] == '<brak wiersza tabeli>'


def test_probe_invoices_page_returns_js_context_and_item_skeleton():
    client, session = _probe_client()
    session._responses.insert(0, ('/assets/invoices.js', _FakeResponse(
        text="$.get('/api/sitecore/EbokInvoiceList/LoadMoreInvoicesProducts?pp=' + pp + '&page=' + n + '&k=" + 'c' * 64 + "');")))
    session._responses.insert(0, ('/Home/Pages/Faktury', _FakeResponse(
        text=_ITEM_HTML + '<script src="/assets/invoices.js"></script>')))
    out = client.probe_invoices_page(known=set())
    assert len(out['js_context']) == 1 and "'&page=' + n" in out['js_context'][0]
    assert 'c' * 64 not in json.dumps(out)
    skel = out['item_skeleton']
    assert skel.startswith('<tr.row.paid[data-item]>') and '"Nieopłacona"' in skel
    assert '123' not in skel and 'XX000000' not in skel


_VM = 'AAAA{\\"Payer\\":\\"' + _PAYER + '\\"}BBBB'
_LIST_PAGE = ("<script>var mediumType = 'Energy'; var serializedViewModel = '" + _VM + "';"
              " var other = 1;</script>")


def _list_client(post_body):
    client, session = _probe_client()
    session._responses.insert(0, ('/Home/Pages/Faktury', _FakeResponse(text=_LIST_PAGE)))
    session.when('EbokInvoiceList/LoadMoreInvoicesProducts', post_body)
    return client, session


def test_probe_invoice_list_posts_like_page_js_and_hides_view_model():
    body = json.dumps({'ResultHtml': _ITEM_HTML * 2, 'TotalCount': 3, 'ReturnedCount': 2})
    client, session = _list_client(_FakeResponse(text=body))
    out = client.probe_invoice_list()
    post = [c for c in session.calls if c[0] == 'POST' and 'LoadMoreInvoicesProducts' in c[1]]
    assert len(post) == 1
    _, url, kw = post[0]
    assert 'sid=T18' in url and f'pp={_PAYER}' in url
    data = dict(kw['data'])
    assert data['startingIndex'] == 0 and data['searchTerm'] == ''
    assert data['PickupMediumType'] == 'Energy'
    assert data['parentViewModel'] == 'AAAA{"Payer":"' + _PAYER + '"}BBBB'   # odkodowane z JS
    assert out['request'] == {'view_model_found': True, 'view_model_kind': 'string', 'view_model_keys': 0,
                              'view_model_object_unparsed': False,
                              'view_model_len': len(data['parentViewModel']), 'medium_type_found': True,
                              'medium_type': 'Energy', 'medium_type_source': 'literal'}
    tree = out['shape']['tree']
    assert tree['TotalCount'] == 3 and tree['ReturnedCount'] == 2
    assert tree['ResultHtml']['rows'] == 2 and tree['ResultHtml']['signatures'] == 1
    assert tree['ResultHtml']['html'].startswith('<tr.row.paid[data-item]>')
    text = json.dumps(out, ensure_ascii=False)
    assert _PAYER not in text and 'AAAA' not in text and 'XX000000' not in text and '123' not in text


def test_probe_invoice_list_without_view_model_skips_post_and_reports_masked_context():
    client, session = _probe_client()
    session._responses.insert(0, ('/Home/Pages/Faktury', _FakeResponse(
        text='<script>var serializedViewModel = JSON.parse("Z' + 'Z' * 40 + _PAYER + '");</script>')))
    out = client.probe_invoice_list()
    assert out['ok'] is False and 'POST pominięty' in out['skipped']
    assert not [c for c in session.calls if c[0] == 'POST' and 'LoadMoreInvoicesProducts' in c[1]]
    ctx = out['request']['view_model_context']
    assert 'serializedViewModel' in ctx and 'Z' * 10 not in ctx and _PAYER not in ctx


def test_js_string_var_ignores_longer_names_and_empty_placeholders():
    from pv_roi_tracker.ebok_client import _js_string_var
    html = "window.serializedViewModelOld = 'x'; var serializedViewModel = ''; serializedViewModel = 'real';"
    assert _js_string_var(html, 'serializedViewModel') == 'real'
    assert _js_string_var("var a = 'x\\qz';", 'a') is None   # nieodkodowalne → None, nie surowa wartość


def test_json_shape_plain_text_with_angle_bracket_is_not_html():
    from pv_roi_tracker.ebok_client import _json_shape
    assert _json_shape({'Msg': 'A < B'}) == {'Msg': 'str'}


def test_probe_invoice_list_json_does_not_leak_strings_containing_status_words():
    body = json.dumps({'Status': 'OK', 'T/K1/XX000000/0020/26': {'Name': 'Faktura T/K1/XX000000/0020/26',
                                                                 'Note': 'Opłacona 123,45 zł'}})
    client, session = _list_client(_FakeResponse(text=body))
    out = json.dumps(client.probe_invoice_list(), ensure_ascii=False)
    assert 'XX000000' not in out and '123,45' not in out


def test_probe_invoice_list_html_response_shape():
    client, session = _list_client(_FakeResponse(text=_ITEM_HTML * 2))
    shape = client.probe_invoice_list()['shape']
    assert shape['type'] == 'html' and shape['items'] == 2 and shape['signatures'] == 1
    assert shape['tags']['td'] == 6


def test_probe_invoice_list_login_page_raises():
    client, session = _list_client(_FakeResponse(text=_LOGIN_FORM_HTML))
    with pytest.raises(EbokError, match='wygasła'):
        client.probe_invoice_list()


def test_probe_invoice_list_requires_selected_payer():
    client, session = _probe_client()
    client._payer_id = None
    with pytest.raises(EbokError, match='płatnika'):
        client.probe_invoice_list()


def test_js_context_masks_long_tokens_and_numbers():
    from pv_roi_tracker.ebok_client import _js_contexts
    out = _js_contexts("x='%s'; a(\"LoadMoreInvoicesProducts\", 1234567)" % ('A' * 40))
    assert 'A' * 40 not in out[0] and '1234567' not in out[0]


class _RaisingScriptSession(_SequenceSession):
    def get(self, url, **kwargs):
        if url.endswith('/assets/invoices.js'):
            raise requests.exceptions.ReadTimeout('boom')
        return super().get(url, **kwargs)


def test_probe_invoices_page_script_exception_counts_as_failed():
    session = _RaisingScriptSession()
    session.when('logowanie.tauron.pl/realms/ext/protocol/cas/login', _FakeResponse(text=_LOGIN_FORM_HTML))
    session.when('logowanie.tauron.pl/action', _FakeResponse(text=_LOGGED_IN_HTML))
    session.when('/Home/Pages/Faktury', _FakeResponse(text=_FAKTURY_HTML))
    client = EbokClient('user@example.com', 'secret', session=session)
    client._sleep = lambda s: None
    client._payer_id = _PAYER
    out = client.probe_invoices_page(known=set())
    assert out['scripts'] == {'total': 1, 'fetched': 0, 'failed': 1, 'skipped': 0}


def test_probe_invoices_page_script_login_page_counts_as_failed():
    client, session = _probe_client()
    session._responses.insert(0, ('/assets/invoices.js', _FakeResponse(text=_LOGIN_FORM_HTML)))
    out = client.probe_invoices_page(known=set())
    assert out['scripts']['failed'] == 1


# ── E0e: serializedViewModel jako obiekt JS ──────────────────────────────────

def test_js_object_var_matches_braces_aware_of_strings():
    from pv_roi_tracker.ebok_client import _js_object_var
    html = 'var a = 1; var serializedViewModel = {"A":null,"B":"x } { y \\" z","C":{"D":[1,2]}}; var b = {"q":1};'
    assert _js_object_var(html, 'serializedViewModel') == {'A': None, 'B': 'x } { y " z', 'C': {'D': [1, 2]}}
    assert _js_object_var('var serializedViewModel = {"A":', 'serializedViewModel') is None
    assert _js_object_var('var serializedViewModel = JSON.parse("x");', 'serializedViewModel') is None
    assert _js_object_var('x', 'serializedViewModel') is None


def test_flatten_params_follows_jquery_param():
    from pv_roi_tracker.ebok_client import _flatten_params
    obj = {'N': None, 'S': 'a', 'T': True, 'F': False, 'I': 5, 'P': [1, 'x'],
           'O': [{'k': 'v', 'n': None}], 'D': {'in': 'z'}, 'E': []}
    assert dict(_flatten_params(obj, 'parentViewModel')) == {
        'parentViewModel[N]': '', 'parentViewModel[S]': 'a', 'parentViewModel[T]': 'true',
        'parentViewModel[F]': 'false', 'parentViewModel[I]': '5', 'parentViewModel[P][]': 'x',
        'parentViewModel[O][0][k]': 'v', 'parentViewModel[O][0][n]': '', 'parentViewModel[D][in]': 'z'}
    assert _flatten_params({'P': [1, 'x']}, 'p') == [('p[P][]', '1'), ('p[P][]', 'x')]


_OBJ_VM = '{"Payer":"' + _PAYER + '","Empty":null,"Items":[{"Id":"XX000000"}],"Flag":true}'
_OBJ_PAGE = ('<select id="SearchType"><option value="Energy">E</option><option selected value="Gas">G</option>'
             '</select><script>var mediumType = $("#SearchType").val(); var serializedViewModel = ' + _OBJ_VM
             + '; var currentItemCount = 9;</script>')


def test_probe_invoice_list_posts_flattened_object_view_model_and_hides_values():
    body = json.dumps({'ResultHtml': _ITEM_HTML * 2, 'TotalCount': 3, 'ReturnedCount': 2})
    client, session = _probe_client()
    session._responses.insert(0, ('/Home/Pages/Faktury', _FakeResponse(text=_OBJ_PAGE)))
    session.when('EbokInvoiceList/LoadMoreInvoicesProducts', _FakeResponse(text=body))
    out = client.probe_invoice_list()
    post = [c for c in session.calls if c[0] == 'POST' and 'LoadMoreInvoicesProducts' in c[1]]
    assert len(post) == 1
    data = post[0][2]['data']
    assert isinstance(data, list)
    pairs = dict(data)
    assert pairs['startingIndex'] == 0 and pairs['searchTerm'] == '' and pairs['PickupMediumType'] == 'Gas'
    assert pairs['parentViewModel[Payer]'] == _PAYER and pairs['parentViewModel[Empty]'] == ''
    assert pairs['parentViewModel[Items][0][Id]'] == 'XX000000' and pairs['parentViewModel[Flag]'] == 'true'
    assert out['request']['view_model_kind'] == 'object' and out['request']['view_model_keys'] == 4
    assert out['request']['medium_type'] == 'Gas' and out['request']['medium_type_source'] == 'SearchType'
    assert out['shape']['tree']['ResultHtml']['rows'] == 2
    text = json.dumps(out, ensure_ascii=False)
    assert _PAYER not in text and 'XX000000' not in text


def test_search_type_default_ignores_foreign_value_and_data_id():
    from pv_roi_tracker.ebok_client import _search_type_default
    assert _search_type_default('<input id="SearchType" type="text"><input value="Submit">') is None
    assert _search_type_default('<input data-id="SearchType" value="Evil">') is None
    assert _search_type_default('<input id="SearchType" value="Energy">') == 'Energy'


def test_probe_invoice_list_rejects_odd_medium_literal_empty_and_unparsed_object():
    def run(page):
        client, session = _probe_client()
        session._responses.insert(0, ('/Home/Pages/Faktury', _FakeResponse(text=page)))
        session.when('EbokInvoiceList/LoadMoreInvoicesProducts', _FakeResponse(text='{}'))
        return client.probe_invoice_list(), session
    out, s = run("<script>var mediumType = 'x y 123'; var serializedViewModel = {\"A\":\"b\"};</script>")
    assert out['request']['medium_type'] is None and 'x y 123' not in json.dumps(out)
    out, s = run('<script>var serializedViewModel = {};</script>')
    assert out['ok'] is False and not [c for c in s.calls if c[0] == 'POST' and 'LoadMore' in c[1]]
    out, s = run("<script>var serializedViewModel = {'A':'b'};</script>")
    assert out['ok'] is False and out['request']['view_model_object_unparsed'] is True


def test_flatten_params_integral_float_like_js():
    from pv_roi_tracker.ebok_client import _flatten_params
    assert _flatten_params({'a': 1.0, 'b': 1.5}, 'p') == [('p[a]', '1'), ('p[b]', '1.5')]


def test_var_context_masks_literal_cut_by_window_edge():
    from pv_roi_tracker.ebok_client import _var_context
    html = 'x = "' + 'SECRETTEXT' * 12 + '"; serializedViewModel = foo("' + 'LEAKYVALUE' * 12 + '");'
    ctx = _var_context(html, 'serializedViewModel')
    assert 'SECRET' not in ctx and 'LEAKY' not in ctx


_DL_BLOCK = (
    '<div class="inv"><div class="head"><dl><dt>Numer</dt><dd><a href="/x">K1AB000000/037</a></dd>'
    '<dt>Data wystawienia</dt><dd><span>31.10.2026</span></dd>'
    '<dt>Termin płatności</dt><dd>14.11.2026</dd>'
    '<dt>Kwota do zapłaty</dt><dd><span class="amt">95,32 zł</span></dd></dl>'
    '<input type="button" value="Zapłać"></div></div>')


def test_invoice_block_shape_describes_dl_layout_without_values():
    from pv_roi_tracker.ebok_client import _invoice_block_shape
    out = _invoice_block_shape('<div id="itemList">' + _DL_BLOCK * 3 + '</div>')
    assert out['blocks'] == 3
    labels = [f['label'] for f in out['fields']]
    assert labels == ['Numer', 'Data wystawienia', 'Termin płatności', 'Kwota do zapłaty']
    by = {f['label']: f for f in out['fields']}
    assert by['Numer']['shape'] == 'a9aa999999/999'
    assert by['Numer']['in'] == 'dd>a'
    assert by['Kwota do zapłaty']['shape'] == '99,99 aa'
    assert by['Termin płatności']['shape'] == '99.99.9999' and by['Termin płatności']['in'] == 'dd'
    assert out['skeleton'].startswith('<div.inv>')
    text = json.dumps(out, ensure_ascii=False)
    assert '95,32' not in text and '000000' not in text and '2026' not in text


def test_invoice_block_shape_single_block_and_no_dl():
    from pv_roi_tracker.ebok_client import _invoice_block_shape
    one = _invoice_block_shape('<div id="itemList">' + _DL_BLOCK + '</div>')
    assert one['blocks'] == 1 and len(one['fields']) == 4
    assert _invoice_block_shape('<div><p>nic</p></div>') is None


def test_json_shape_html_without_rows_includes_dl_blocks():
    from pv_roi_tracker.ebok_client import _json_shape
    out = _json_shape({'ResultHtml': _DL_BLOCK * 2})
    assert out['ResultHtml']['rows'] == 0 and out['ResultHtml']['dl_blocks']['blocks'] == 2


def test_invoice_block_shape_implied_end_tags_and_class_digits():
    from pv_roi_tracker.ebok_client import _invoice_block_shape, _safe_block_shape
    blk = '<div class="inv-12345"><dl><dt>Numer<dd>K1AB/037<dt>Termin płatności<dd>14.11.2026</dl></div>'
    out = _invoice_block_shape(blk * 2)
    assert [f['label'] for f in out['fields']] == ['Numer', 'Termin płatności']
    assert out['fields'][1]['shape'] == '99.99.9999' and 'inv-99999' in out['skeleton'] and '12345' not in out['skeleton']
    assert _safe_block_shape('<dl><dt>a' + '<li>' * 3000 + '</dl>') is not None


_ROW_BLOCK = (
    '<div class="row item"><div class="col-a"><span class="num">K1AB000000/037</span>'
    '<span class="typ">Faktura</span></div>'
    '<div class="col-b">31.10.2026</div><div class="col-c">ul. Tajna 5</div>'
    '<div class="col-d">14.11.2026</div>'
    '<div class="col-e"><span>95,32 zł</span><input type="button" value="Zapłać"></div>'
    '<dl class="dl-horizontal"><dt>Należność</dt><dd>1,00 zł</dd></dl></div>')


def test_repeated_block_shape_picks_container_with_returned_count_not_first_dt():
    from pv_roi_tracker.ebok_client import _repeated_block_shape
    html = '<div class="sum"><dl><dt>Łącznie</dt><dd>9,99 zł</dd></dl><dl><dt>Nota odsetkowa</dt><dd>0,00 zł</dd></dl></div>' \
           '<div id="itemList">' + _ROW_BLOCK * 3 + '</div>'
    out = _repeated_block_shape(html, 3)
    assert out['blocks'] == 3 and out['skeleton'].startswith('<div.row.item>')
    by_path = {l['path']: l for l in out['leaves']}
    shapes = [l['shape'] for l in out['leaves']]
    assert 'a9aa999999/999' in shapes and '99.99.9999' in shapes and '99,99 aa' in shapes
    assert any(l['label'] == 'Zapłać' for l in out['leaves'])
    assert any(l['label'] == 'Należność' for l in out['leaves'])
    assert not any(l.get('label') == 'ul. Tajna' for l in out['leaves'])
    text = json.dumps(out, ensure_ascii=False)
    assert '95,32' not in text and '000000' not in text and 'Tajna' not in text and '2026' not in text
    assert by_path


def test_repeated_block_shape_no_match_returns_none():
    from pv_roi_tracker.ebok_client import _repeated_block_shape
    assert _repeated_block_shape('<div><p>a</p></div>', 3) == {'no_group': True, 'count': 3}
    assert _repeated_block_shape(_ROW_BLOCK * 2, 0) is None


def test_json_shape_passes_returned_count_to_item_blocks():
    from pv_roi_tracker.ebok_client import _json_shape
    out = _json_shape({'ResultHtml': '<div id="l">' + _ROW_BLOCK * 3 + '</div>', 'TotalCount': 3, 'ReturnedCount': 3})
    assert out['ResultHtml']['item_blocks']['blocks'] == 3


def test_repeated_block_shape_pay_button_in_later_block_only():
    from pv_roi_tracker.ebok_client import _repeated_block_shape
    paid = '<div class="row item"><span>x</span><a class="btn">a</a><a class="btn">b</a></div>'
    html = '<div>' + paid + _ROW_BLOCK + '</div>'
    assert _repeated_block_shape(html, 2)['skeleton'].startswith('<div.row.item>')
    assert _repeated_block_shape(html, 2)['blocks'] == 2


# ── E0h: handler rozwijania punktu (data-expand-init / toggle-details-button) ──

_EXPAND_JS = (
    "var serializedViewModel = {\"Name\":\"Jan Tajny\",\"Id\":1234567890};\n"
    "$('.toggle-details-button').on('click', function () {\n"
    "  var id = $(this).data('agreement-id');\n"
    "  $.get('/api/sitecore/EbokInvoiceList/GetInvoicesForAgreement?pp=" + _PAYER + "&agreementId=' + id, function (r) {\n"
    "    $('.toggle-box').html(r.ResultHtml); var tok = '" + 'A' * 40 + "'; });\n"
    "});")


def test_code_contexts_keep_selectors_and_paths_mask_other_literals():
    from pv_roi_tracker.ebok_client import _code_contexts
    out = _code_contexts(_EXPAND_JS, ('toggle-details-button',), width=400)
    assert len(out) == 1
    text = out[0]
    assert '.toggle-details-button' in text and '/api/sitecore/EbokInvoiceList/GetInvoicesForAgreement' in text
    assert 'agreementId' in text            # nazwa parametru zostaje
    for secret in (_PAYER, 'Tajny', '1234567890', 'A' * 40):
        assert secret not in text


def test_code_contexts_limit_and_no_keyword():
    from pv_roi_tracker.ebok_client import _code_contexts
    assert _code_contexts('abc', ('toggle-box',)) == []
    assert len(_code_contexts('toggle-box ' * 20, ('toggle-box',), width=5, limit=4)) == 4


def test_expand_attrs_shows_names_masked_shapes_and_paths_only():
    from pv_roi_tracker.ebok_client import _expand_attrs
    html = ('<a class="toggle-button" data-expand-init="/api/sitecore/EbokInvoiceList/Expand?pp=123456&sid=T18"'
            ' data-target="#box-77">x</a>'
            '<a class="toggle-details-button" data-agreement-id="9988776655">Pokaż szczegóły</a>'
            '<input class="check-all-in-pp-expanded" data-agreement-id="9988776655">')
    out = _expand_attrs(html)
    assert out[0]['class'] == 'toggle-button'
    assert out[0]['attrs']['data-expand-init'] == '/api/sitecore/EbokInvoiceList/Expand?pp&sid'
    assert out[0]['attrs']['data-target'] == '#aaa-99'
    assert out[1]['attrs']['data-agreement-id'] == '9999999999'
    text = json.dumps(out)
    assert '123456' not in text and '9988776655' not in text


def test_expand_attrs_empty_and_limited():
    from pv_roi_tracker.ebok_client import _expand_attrs
    assert _expand_attrs('<div>x</div>') == []
    assert len(_expand_attrs('<a class="toggle-button" data-expand-init="1"></a>' * 20)) == 6


def test_json_shape_html_includes_expand_attrs():
    from pv_roi_tracker.ebok_client import _json_shape
    out = _json_shape({'ResultHtml': '<div><a class="toggle-button" data-expand-init="x">a</a></div>'})
    assert out['ResultHtml']['expand_attrs'][0]['attrs'] == {'data-expand-init': 'a'}


def test_probe_invoices_page_returns_expand_context_from_inline_and_scripts():
    client, session = _probe_client()
    session._responses.insert(0, ('/Home/Pages/Faktury', _FakeResponse(
        text='<script>' + _EXPAND_JS + '</script><script src="/assets/invoices.js"></script>')))
    session._responses.insert(0, ('/assets/invoices.js', _FakeResponse(text="$('.toggle-box').hide();")))
    out = client.probe_invoices_page(known=set())
    joined = ' '.join(out['expand_context'])
    assert 'GetInvoicesForAgreement' in joined and '.toggle-box' in joined
    assert _PAYER not in json.dumps(out) and 'Tajny' not in json.dumps(out)
    assert len(out['expand_context']) <= 8


def test_code_contexts_no_redos_and_masks_comments_backticks_and_loose_literals():
    import time
    from pv_roi_tracker.ebok_client import _code_contexts
    t = time.time()
    _code_contexts("x = 'a" + '\\' * 60 + " toggle-box", ('toggle-box',))
    assert time.time() - t < 1
    js = ("// don't leak Anna Nowak\n/* Jan Kowalski */ var a = `Jan Kowalski ${x}`;\n"
          "var n = 'jan-kowalski'; var s = '.Jan Kowalski, Wroclaw'; var k = 'toggle-box';")
    text = ' '.join(_code_contexts(js, ('toggle-box',), width=400))
    for leak in ('Nowak', 'Kowalski', 'Wroclaw', 'jan-kowalski'):
        assert leak not in text
    assert 'toggle-box' in text


def test_expand_attrs_masks_digits_in_attribute_names():
    from pv_roi_tracker.ebok_client import _expand_attrs
    out = _expand_attrs('<a class="toggle-button" data-agreement-123456="x"></a>')
    assert 'data-agreement-999999' in out[0]['attrs'] and '123456' not in json.dumps(out)


# ── E0i: ukryte row-details-container z wierszami faktur ──

_DETAILS = (
    '<div class="row invoice-row"><div class="col">Punkt</div></div>'
    '<div class="row-details-container"><div class="table-inside"><div class="table-wrapper">'
    '<div class="row" data-charge-id="123456789" data-agreement-id="55"><span>K1NBN000000/037</span>'
    '<span>31.10.2026</span><span>1,31 zł</span><input class="ebok-checkbox-payment" type="checkbox">'
    '<a class="ebok-button-pay-agreement">Zapłać</a></div></div></div></div>')


def test_details_blocks_shape_without_values():
    from pv_roi_tracker.ebok_client import _details_blocks
    out = _details_blocks(_DETAILS * 2)
    assert out['containers'] == 2 and out['charge_rows'] == 2
    assert out['charge_id_shapes'] == ['999999999']
    assert 'data-charge-id' in out['charge_attrs']
    shapes = [l['shape'] for l in out['leaves']]
    assert '99,99 aa' not in shapes and '9,99 aa' in shapes and '99.99.9999' in shapes
    text = json.dumps(out, ensure_ascii=False)
    assert '123456789' not in text and '000000' not in text and '1,31' not in text and '2026' not in text


def test_details_blocks_absent_and_in_json_shape():
    from pv_roi_tracker.ebok_client import _details_blocks, _json_shape
    assert _details_blocks('<div><p>a</p></div>') is None
    out = _json_shape({'ResultHtml': _DETAILS, 'TotalCount': 1, 'ReturnedCount': 1})
    assert out['ResultHtml']['details_blocks']['containers'] == 1


def test_details_blocks_skeleton_masks_digits_in_data_attr_names():
    from pv_roi_tracker.ebok_client import _details_blocks
    html = _DETAILS.replace('data-agreement-id="55"', 'data-agreement-123456="55"')
    out = _details_blocks(html)
    assert '123456' not in json.dumps(out) and 'data-agreement-999999' in out['charge_row_skeleton']


# ── E0j: skąd wziąć sourceId / checkSum (sonda tokenów, bez wartości) ─────────

_GUID = '0a1b2c3d-1111-2222-3333-444455556666'
_HEX = 'ab' * 32


def test_token_hits_reports_tag_attr_and_kind_without_values():
    from pv_roi_tracker.ebok_client import _token_hits
    html = (f'<div class="amount-column" data-source-id="{_GUID}" data-check="{_HEX}"></div>'
            f'<script>var x = "{_GUID}";</script>')
    hits = _token_hits(html)
    assert {'kind': 'guid', 'where': 'div[data-source-id]', 'n': 1} in hits
    assert {'kind': 'hex64', 'where': 'div[data-check]', 'n': 1} in hits
    assert {'kind': 'guid', 'where': 'script', 'n': 1} in hits
    text = json.dumps(hits)
    assert _GUID not in text and _HEX not in text


def test_view_model_hints_lists_matching_paths_with_masked_shape():
    from pv_roi_tracker.ebok_client import _view_model_hints
    vm = {'SourceId': _GUID, 'Other': 'x', 'Nested': {'CheckSum': _HEX, 'Items': [{'ContractorNumber': '123456'}]}}
    hints = _view_model_hints(vm)
    assert hints['SourceId'] == '9a9a9a9a-9999-9999-9999-999999999999'
    assert 'Nested.CheckSum' in hints and 'Nested.Items[].ContractorNumber' in hints
    assert 'Other' not in hints
    assert _GUID not in json.dumps(hints) and _HEX not in json.dumps(hints)


def test_probe_invoice_list_returns_source_hints():
    body = json.dumps({'ResultHtml': f'<div data-source-id="{_GUID}"></div>'
                       "<script>$.get('/api/sitecore/EbokInvoices/GetInvoices?sourceId=' + s)</script>",
                       'TotalCount': 1, 'ReturnedCount': 1})
    client, session = _probe_client()
    session._responses.insert(0, ('/Home/Pages/Faktury', _FakeResponse(
        text=f'<script>var serializedViewModel = {{"SourceId":"{_GUID}","Other":1}};</script>')))
    session.when('EbokInvoiceList/LoadMoreInvoicesProducts', _FakeResponse(text=body))
    out = client.probe_invoice_list()
    hints = out['source_hints']
    assert 'SourceId' in hints['view_model']
    assert {'kind': 'guid', 'where': 'div[data-source-id]', 'n': 1} in hints['result_html_tokens']
    assert any('GetInvoices' in c for c in hints['result_html_code'])
    assert _GUID not in json.dumps(out)


# ── faktury do zapłaty (E1: lista „Faktury” → GetInvoices) ───────────────────

_SRC_GUID = '00000000-0000-0000-0000-000000000000'
_SRC_HEX = 'ab' * 32
_SIG_A = 'T/K1/XX000000/0020/26'
_SIG_B = 'K1NXX000000/037'


def _unpaid_row(sig, issued='01.10.2026', due='15.10.2026', amount='95,32 zł', doc_type='FV', name='Faktura'):
    base = ('/api/sitecore/EbokArchiveDocuments/GetDocumentImage?'
            f'docNumber=x&docType={doc_type}&a=1')
    return (f'<tr><td><a data-base="{base}" href="#"> {sig} </a><div>{name}</div></td>'
            f'<td>{issued}</td><td>-</td><td>{due}</td>'
            f'<td><input data-charge-id="1" data-charge-value="95.32"><div class="amount">{amount}</div>'
            '<a href="/Payment">Zapłać</a></td></tr>')


def _result_html(source_id=_SRC_GUID, checksum=_SRC_HEX):
    return (f'<div data-source-id="{source_id}" data-contractor-number-checksum="{checksum}" '
            'data-api-url="/api/sitecore/EbokInvoices/GetInvoices"></div>')


def test_parse_unpaid_source_ok():
    assert parse_unpaid_source(_result_html()) == {'source_id': _SRC_GUID, 'checksum': _SRC_HEX}


def test_parse_unpaid_source_missing_attribute_is_none():
    assert parse_unpaid_source(f'<div data-source-id="{_SRC_GUID}"></div>') is None
    assert parse_unpaid_source('<div data-contractor-number-checksum="%s"></div>' % _SRC_HEX) is None
    assert parse_unpaid_source('') is None


def test_parse_unpaid_source_bad_format_is_none():
    assert parse_unpaid_source(_result_html(source_id='nie-guid')) is None
    assert parse_unpaid_source(_result_html(checksum='ab' * 10)) is None


def test_parse_unpaid_invoices_html_rows():
    html = ('<table>' + _unpaid_row(_SIG_A) + _unpaid_row(_SIG_B, amount='10,00 zł', due='20.10.2026')
            + '<tr><td><div>Faktura</div></td><td>01.10.2026</td><td>-</td><td>x</td><td></td></tr>'
            + '</table>')
    docs = parse_unpaid_invoices_html(html)
    assert [d.signature for d in docs] == [_SIG_A, _SIG_B]
    a, b = docs
    assert a.doc_name == 'Faktura' and a.issued_on == '01.10.2026'
    assert a.due_date == date(2026, 10, 15) and a.amount_gross_pln == 95.32
    assert a.paid is False and a.status == 'Do zapłaty' and a.doc_type == 'FV'
    assert a.pdf_url.startswith('https://moj.tauron.pl/api/sitecore/EbokArchiveDocuments/GetDocumentImage?')
    assert b.amount_gross_pln == 10.0 and b.due_date == date(2026, 10, 20)


def test_parse_unpaid_invoices_html_foreign_pdf_url_dropped():
    html = _unpaid_row(_SIG_A).replace('/api/sitecore/EbokArchiveDocuments', 'https://evil.example/x')
    assert parse_unpaid_invoices_html(html)[0].pdf_url is None


def test_signature_re_matches_both_formats():
    from pv_roi_tracker.ebok_client import _SIGNATURE_RE, normalize_signature
    assert _SIGNATURE_RE.findall(f'a {_SIG_A} b {_SIG_B} c') == [_SIG_A, _SIG_B]
    assert normalize_signature(_SIG_B) == 'K1NXX000000_037'


def _unpaid_page(first, last, total):
    return _FakeResponse(text=json.dumps({'status': 'OK', 'totalCount': total, 'html': ''.join(
        _unpaid_row(f'T/K1/XX000000/{i:04d}/26') for i in range(first, last + 1))}))


def _unpaid_client(result_html=None):
    client, session = _selected_client()
    session._responses.insert(0, ('/Home/Pages/Faktury', _FakeResponse(text=_LIST_PAGE)))
    body = json.dumps({'ResultHtml': _result_html() if result_html is None else result_html,
                       'TotalCount': 1, 'ReturnedCount': 1})
    session.when('EbokInvoiceList/LoadMoreInvoicesProducts', _FakeResponse(text=body))
    return client, session


def test_list_unpaid_invoices_paginates_five_plus_two():
    client, session = _unpaid_client()
    session.when('skip=0', _unpaid_page(1, 5, 7))
    session.when('skip=5', _unpaid_page(6, 7, 7))
    docs = client.list_unpaid_invoices()
    assert len(docs) == 7
    urls = [c[1] for c in session.calls if 'GetInvoices' in c[1]]
    assert len(urls) == 2
    for part in (f'sourceId={_SRC_GUID}', f'contractorNumber={_PAYER}', f'checkSum={_SRC_HEX}', 'take=5'):
        assert part in urls[0]


def test_list_unpaid_invoices_empty_page_stops_pagination():
    client, session = _unpaid_client()
    session.when('GetInvoices', _FakeResponse(text=json.dumps({'status': 'OK', 'html': '', 'totalCount': 50})))
    assert client.list_unpaid_invoices() == []
    assert len([c for c in session.calls if 'GetInvoices' in c[1]]) == 1


def test_list_unpaid_invoices_without_source_returns_empty():
    client, session = _unpaid_client(result_html='<div>brak</div>')
    assert client.list_unpaid_invoices() == []
    assert not [c for c in session.calls if 'GetInvoices' in c[1]]


def test_list_unpaid_invoices_status_not_ok_raises_layout_changed():
    client, session = _unpaid_client()
    session.when('GetInvoices', _FakeResponse(text=json.dumps({'status': 'Error', 'html': ''})))
    with pytest.raises(EbokLayoutChanged):
        client.list_unpaid_invoices()


def test_list_unpaid_invoices_without_payer_raises():
    client, _ = _seq_client()
    with pytest.raises(EbokError, match='płatnika'):
        client.list_unpaid_invoices()


def _archive_with(session, sigs):
    session.when('GetDocuments', _FakeResponse(text=json.dumps({'Status': 'OK', 'TotalNumber': len(sigs), 'Documents': [
        {'DocSignature': s, 'DocType': 'FV', 'Status': 'Opłacona'} for s in sigs]})))


def test_list_documents_appends_unpaid_and_archive_wins_on_duplicates():
    client, session = _unpaid_client()
    _archive_with(session, [_SIG_A])
    session.when('GetInvoices', _FakeResponse(text=json.dumps({'status': 'OK', 'totalCount': 2, 'html':
                                                              _unpaid_row(_SIG_A) + _unpaid_row(_SIG_B)})))
    docs = client.list_documents(date(2015, 1, 1), date(2026, 10, 27))
    assert [d.signature for d in docs] == [_SIG_A, _SIG_B]
    assert docs[0].paid is True and docs[0].status == 'Opłacona'
    assert docs[1].paid is False


def test_list_documents_unpaid_error_keeps_archive(caplog):
    client, session = _unpaid_client()
    _archive_with(session, [_SIG_A])
    session.when('GetInvoices', _FakeResponse(text=json.dumps({'status': 'Error'})))
    docs = client.list_documents(date(2015, 1, 1), date(2026, 10, 27))
    assert [d.signature for d in docs] == [_SIG_A]
    assert 'EbokLayoutChanged' in caplog.text


def test_list_documents_unpaid_request_exception_keeps_archive(monkeypatch):
    client, session = _unpaid_client()
    _archive_with(session, [_SIG_A])
    monkeypatch.setattr(client, 'list_unpaid_invoices',
                        lambda: (_ for _ in ()).throw(requests.exceptions.ChunkedEncodingError('x')))
    docs = client.list_documents(date(2015, 1, 1), date(2026, 10, 27))
    assert [d.signature for d in docs] == [_SIG_A]


def test_list_documents_login_blocked_in_unpaid_propagates(monkeypatch):
    client, session = _unpaid_client()
    _archive_with(session, [_SIG_A])
    monkeypatch.setattr(client, 'list_unpaid_invoices',
                        lambda: (_ for _ in ()).throw(EbokLoginBlocked('x')))
    with pytest.raises(EbokLoginBlocked):
        client.list_documents(date(2015, 1, 1), date(2026, 10, 27))


def test_unpaid_only_document_becomes_active_payment(tmp_path):
    from pv_roi_tracker.payment_reminders import apply_refresh, load_payment_state, select_active_payment
    client, session = _unpaid_client()
    _archive_with(session, [])
    session.when('GetInvoices', _FakeResponse(text=json.dumps({'status': 'OK', 'totalCount': 1, 'html':
                                                              _unpaid_row(_SIG_B)})))
    docs = client.list_documents(date(2015, 1, 1), date(2026, 10, 27))
    path = tmp_path / 'payment_state.json'
    today = docs[0].due_date
    apply_refresh(docs, path, today)
    active = select_active_payment(load_payment_state(path), today)
    assert active['signature'] == _SIG_B


# ── retry listy „do zapłaty” i last_unpaid_error ────────────────────────────

def _unpaid_doc():
    from pv_roi_tracker.ebok_client import EbokDocument
    return EbokDocument(signature=_SIG_B, doc_name='Faktura', issued_on='01.10.2026',
                        amount_gross_pln=95.32, status='Do zapłaty', doc_type='FV')


def _raiser(*excs):
    """list_unpaid_invoices: kolejne wywołania rzucają kolejne wyjątki, a po wyczerpaniu zwraca dokument."""
    calls = []

    def fake():
        calls.append(1)
        if len(calls) <= len(excs):
            raise excs[len(calls) - 1]
        return [_unpaid_doc()]
    return fake, calls


def test_unpaid_retry_succeeds_on_second_attempt(monkeypatch):
    from pv_roi_tracker import ebok_client
    monkeypatch.setattr(ebok_client, 'UNPAID_RETRY_DELAY', 0)
    client, session = _unpaid_client()
    _archive_with(session, [_SIG_A])
    fake, calls = _raiser(requests.exceptions.ConnectionError('x'))
    monkeypatch.setattr(client, 'list_unpaid_invoices', fake)
    docs = client.list_documents(date(2015, 1, 1), date(2026, 10, 27))
    assert [d.signature for d in docs] == [_SIG_A, _SIG_B]
    assert len(calls) == 2
    assert client.last_unpaid_error is None


def test_unpaid_two_failures_keep_archive_and_record_error_type(monkeypatch):
    from pv_roi_tracker import ebok_client
    monkeypatch.setattr(ebok_client, 'UNPAID_RETRY_DELAY', 0)
    client, session = _unpaid_client()
    _archive_with(session, [_SIG_A])
    fake, calls = _raiser(requests.exceptions.ConnectionError('SECRET-host'),
                          requests.exceptions.ConnectionError('SECRET-host'))
    monkeypatch.setattr(client, 'list_unpaid_invoices', fake)
    docs = client.list_documents(date(2015, 1, 1), date(2026, 10, 27))
    assert [d.signature for d in docs] == [_SIG_A]
    assert len(calls) == 2
    assert client.last_unpaid_error == 'ConnectionError'


def test_unpaid_login_blocked_propagates_without_retry(monkeypatch):
    from pv_roi_tracker import ebok_client
    monkeypatch.setattr(ebok_client, 'UNPAID_RETRY_DELAY', 0)
    client, session = _unpaid_client()
    _archive_with(session, [_SIG_A])
    fake, calls = _raiser(EbokLoginBlocked('x'))
    monkeypatch.setattr(client, 'list_unpaid_invoices', fake)
    with pytest.raises(EbokLoginBlocked):
        client.list_documents(date(2015, 1, 1), date(2026, 10, 27))
    assert len(calls) == 1


def test_unpaid_layout_changed_no_retry_records_error(monkeypatch):
    from pv_roi_tracker import ebok_client
    monkeypatch.setattr(ebok_client, 'UNPAID_RETRY_DELAY', 0)
    client, session = _unpaid_client()
    _archive_with(session, [_SIG_A])
    fake, calls = _raiser(EbokLayoutChanged('x'))
    monkeypatch.setattr(client, 'list_unpaid_invoices', fake)
    docs = client.list_documents(date(2015, 1, 1), date(2026, 10, 27))
    assert [d.signature for d in docs] == [_SIG_A]
    assert len(calls) == 1
    assert client.last_unpaid_error == 'EbokLayoutChanged'


# ── download_pdf_any_mode() ─────────────────────────────────────────────────

_ANY_URL = ('https://moj.tauron.pl/api/sitecore/EbokArchiveDocuments/GetDocumentImage'
            '?docNumber=x&docType=FV&docMode={mode}')
_ERR_JSON = json.dumps({'Status': 'Error', 'Document': None})


def _modes(session):
    return [c[1].rsplit('docMode=', 1)[1] for c in session.calls if 'GetDocumentImage' in c[1]]


def test_download_pdf_any_mode_falls_back_to_mode_zero():
    client, session = _client_with_successful_login()
    session.when('docMode=1', _FakeResponse(text=_ERR_JSON))
    session.when('docMode=0', _FakeResponse(text=_PDF_JSON))
    pdf = client.download_pdf_any_mode(_ANY_URL.format(mode=1))
    assert pdf.startswith(b'%PDF')
    assert _modes(session) == ['1', '0']


def test_download_pdf_any_mode_all_fail_raises_after_three_requests():
    client, session = _client_with_successful_login()
    session.when('GetDocumentImage', _FakeResponse(text=_ERR_JSON))
    with pytest.raises(EbokLayoutChanged):
        client.download_pdf_any_mode(_ANY_URL.format(mode=1))
    assert _modes(session) == ['1', '0', '2']


def test_download_pdf_any_mode_skips_original_mode():
    client, session = _client_with_successful_login()
    session.when('GetDocumentImage', _FakeResponse(text=_ERR_JSON))
    with pytest.raises(EbokLayoutChanged):
        client.download_pdf_any_mode(_ANY_URL.format(mode=0))
    assert _modes(session) == ['0', '2']


def test_download_pdf_any_mode_first_success_single_request():
    client, session = _client_with_successful_login()
    session.when('GetDocumentImage', _FakeResponse(text=_PDF_JSON))
    assert client.download_pdf_any_mode(_ANY_URL.format(mode=1)).startswith(b'%PDF')
    assert _modes(session) == ['1']


def test_download_pdf_any_mode_falls_back_on_plain_ebok_error(monkeypatch):
    """Zły docMode często daje JSON OK bez pliku PDF (EbokError, nie EbokLayoutChanged)."""
    from pv_roi_tracker import ebok_client as ec
    calls = []

    def fake(self, url):
        calls.append(url)
        if 'docMode=2' in url:
            return b'%PDF-ok'
        raise ec.EbokError('Pobranie PDF: odpowiedź nie jest plikiem PDF')

    monkeypatch.setattr(ec.EbokClient, 'download_pdf', fake)
    client = ec.EbokClient.__new__(ec.EbokClient)
    out = client.download_pdf_any_mode('https://moj.tauron.pl/api/sitecore/X/Y?docType=FV&docMode=1')
    assert out == b'%PDF-ok' and len(calls) == 3


def test_download_pdf_any_mode_login_blocked_propagates(monkeypatch):
    from pv_roi_tracker import ebok_client as ec
    calls = []

    def fake(self, url):
        calls.append(url)
        raise ec.EbokLoginBlocked('x')

    monkeypatch.setattr(ec.EbokClient, 'download_pdf', fake)
    client = ec.EbokClient.__new__(ec.EbokClient)
    with pytest.raises(ec.EbokLoginBlocked):
        client.download_pdf_any_mode('https://moj.tauron.pl/api/sitecore/X/Y?docType=FV&docMode=1')
    assert len(calls) == 1
