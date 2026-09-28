"""
Unit tests for ebok_client.py.

Parsing helpers (CSV/HTML) are pure functions tested here on synthetic
fixtures with fabricated names/amounts/invoice numbers — no real personal
data is committed to this (public) repo.

Tests against the real fixtures captured during the Etap 1 spike (real name,
address, invoice numbers) live outside the repo and are skipped automatically
when absent, mirroring the pattern in test_invoice_parser.py.

Login/network behaviour is tested with a small FakeSession stand-in (no
requests_mock/responses dependency — this add-on pins its dependencies
deliberately, see requirements.txt).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pv_roi_tracker.ebok_client import (
    EbokClient,
    EbokError,
    EbokLoginBlocked,
    EbokLoginFailed,
    parse_archive_html,
    parse_client_id_for_payer,
    parse_documents_csv,
)

_REAL_FIXTURES_DIR = Path('/data/home/.claude/uploads/ebok_fixtures')


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


# ── list_documents() / download_pdf() wiring ────────────────────────────────

def test_list_documents_returns_parsed_rows():
    client, session = _client_with_successful_login()
    csv_text = (
        'SYGNATURA;NAZWA DOKUMENTU;DATA WYSTAWIENIA;DODATKOWE INFORMACJE;'
        'KWOTA BRUTTO;TERMIN PŁATNOŚCI;KWOTA DO ZAPŁATY;ZAPŁACONA\r\n'
        'T/K1/TEST/0001/26;Faktura rzeczywista;01.01.2026;---;100,50;15.01.2026;---;zapłacona\r\n'
    ).encode('windows-1250')
    session.when('/content/platnosci/csv/', _FakeResponse(content=csv_text))
    docs = client.list_documents(date(2022, 1, 1), date(2027, 1, 1))
    assert len(docs) == 1
    assert docs[0].signature == 'T/K1/TEST/0001/26'
    assert docs[0].amount_gross_pln == pytest.approx(100.50)


def test_list_documents_bad_status_raises():
    client, session = _client_with_successful_login()
    session.when('/content/platnosci/csv/', _FakeResponse(status_code=500))
    with pytest.raises(EbokError):
        client.list_documents(date(2022, 1, 1), date(2027, 1, 1))


def test_download_pdf_returns_bytes():
    client, session = _client_with_successful_login()
    session.when('/podgladFaktury/id/123', _FakeResponse(content=b'%PDF-1.4 fake'))
    assert client.download_pdf('123') == b'%PDF-1.4 fake'


def test_download_pdf_non_pdf_response_raises():
    client, session = _client_with_successful_login()
    session.when('/podgladFaktury/id/123', _FakeResponse(text='<html>error page</html>'))
    with pytest.raises(EbokError):
        client.download_pdf('123')


def test_locate_ids_stops_once_all_found():
    client, session = _client_with_successful_login()
    page1_html = (
        '<a href="/podgladFaktury/id/111"  class="invoicePreviewHelper typ_106 helperLink" >'
        '<i class="icon"></i>T/K1/TEST/0002/26</a>'
    )
    session.when('/archiwumFaktur/page/1/', _FakeResponse(text=page1_html))
    found = client.locate_ids({'T/K1/TEST/0002/26'}, date(2022, 1, 1), date(2027, 1, 1))
    assert found == {'T/K1/TEST/0002/26': '111'}
    # only page 1 requested (plus the initial login GET/POST) — no page/2 call
    assert not any('page/2/' in url for _, url, _ in session.calls)


def test_locate_ids_gives_up_after_empty_page():
    client, session = _client_with_successful_login()
    session.when('/archiwumFaktur/page/', _FakeResponse(text='<html>brak wierszy</html>'))
    found = client.locate_ids({'NIEISTNIEJACY/0000/00'}, date(2022, 1, 1), date(2027, 1, 1))
    assert found == {}


# ── pure parsing helpers ─────────────────────────────────────────────────────

def test_parse_documents_csv_decodes_polish_chars_and_skips_blank_rows():
    csv_bytes = (
        'SYGNATURA;NAZWA DOKUMENTU;DATA WYSTAWIENIA;DODATKOWE INFORMACJE;'
        'KWOTA BRUTTO;TERMIN PŁATNOŚCI;KWOTA DO ZAPŁATY;ZAPŁACONA\r\n'
        'T/K1/TEST/0003/26;Faktura korygująca;05.02.2026;---;-12,34;19.02.2026;---;zapłacona\r\n'
        ';;;;;;;\r\n'
    ).encode('windows-1250', errors='replace')
    docs = parse_documents_csv(csv_bytes)
    assert len(docs) == 1
    assert docs[0].doc_name == 'Faktura korygująca'
    assert docs[0].amount_gross_pln == pytest.approx(-12.34)


def test_parse_documents_csv_handles_dash_amount():
    csv_bytes = (
        'SYGNATURA;NAZWA DOKUMENTU;DATA WYSTAWIENIA;DODATKOWE INFORMACJE;'
        'KWOTA BRUTTO;TERMIN PŁATNOŚCI;KWOTA DO ZAPŁATY;ZAPŁACONA\r\n'
        'T/K1/TEST/0004/26;Faktura korygująca;05.02.2026;---;---;19.02.2026;---;zapłacona\r\n'
    ).encode('windows-1250')
    docs = parse_documents_csv(csv_bytes)
    assert docs[0].amount_gross_pln is None


def test_parse_archive_html_maps_signature_to_numeric_id():
    html = (
        '<a href="/podgladFaktury/id/999888777"  class="invoicePreviewHelper typ_106 helperLink" >'
        '<i class="icon iconArrowSmallest"></i>T/K1/TEST/0005/26</a><br/>'
        '<a href="/podgladFaktury/id/999888777"  class="invoicePreviewHelper typ_106 " >'
        '<i class="icon iconArrowSmallest"></i>Faktura rzeczywista</a>'
    )
    result = parse_archive_html(html)
    assert result == {'T/K1/TEST/0005/26': '999888777'}


def test_parse_archive_html_no_rows_returns_empty_dict():
    assert parse_archive_html('<html>brak faktur</html>') == {}


def test_parse_client_id_for_payer_matches_correct_row():
    html = (
        '<table><tbody>'
        '<tr><td><label>JAN KOWALSKI <br />12345678</label></td>'
        '<td><a href="/wyborKlienta/id/111111">Zobacz</a></td></tr>'
        '<tr><td><label>JAN KOWALSKI <br />87654321</label></td>'
        '<td><a href="/wyborKlienta/id/222222">Zobacz</a></td></tr>'
        '</tbody></table>'
    )
    assert parse_client_id_for_payer(html, '87654321') == '222222'
    assert parse_client_id_for_payer(html, '00000000') is None


# ── Optional regression tests on real spike fixtures (skipped if absent) ────

@pytest.mark.skipif(not _REAL_FIXTURES_DIR.exists(),
                    reason='real eBOK fixtures not available on this machine')
class TestRealFixtures:
    def test_real_csv_has_77_documents(self):
        raw = (_REAL_FIXTURES_DIR / 'archiwum_real.csv').read_bytes()
        docs = parse_documents_csv(raw)
        assert len(docs) == 77
        signatures = [d.signature for d in docs]
        assert len(signatures) == len(set(signatures))  # no duplicates

    def test_real_archive_page_maps_at_least_50_ids(self):
        html = (_REAL_FIXTURES_DIR / 'archiwumFaktur_page1_real.html').read_text(encoding='utf-8')
        mapping = parse_archive_html(html)
        assert len(mapping) == 50

    def test_real_landing_page_resolves_known_payer(self):
        html = (_REAL_FIXTURES_DIR / 'landing_real.html').read_text(encoding='utf-8')
        assert parse_client_id_for_payer(html, '60567872') == '9810070'

    def test_real_sample_invoice_parses_cleanly(self):
        from pv_roi_tracker.invoice_parser import parse_invoice
        pdf_bytes = (_REAL_FIXTURES_DIR / 'sample_invoice_2026-08_real.pdf').read_bytes()
        data = parse_invoice(pdf_bytes)
        assert data.warnings == []
        assert data.year == 2026 and data.month == 8
        assert data.amount_due_pln == pytest.approx(63.3)
