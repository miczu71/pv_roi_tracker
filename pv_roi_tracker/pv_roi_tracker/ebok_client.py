"""
Client for the TAURON customer portal "Mój TAURON" (moj.tauron.pl, ex-eBOK) — logs in
via Keycloak, lists invoice documents for a payer and downloads their PDFs.

Since 26.10.2026 the old ebok.tauron.pl endpoints (client picker, CSV export, HTML
archive and invoice preview) are gone. The portal is a Sitecore site with a JSON API
(probe of 27.10.2026, docs/ROADMAP_EBOK_MOJ_TAURON.md):
  • Login: Keycloak `kc-form-login` on logowanie.tauron.pl with service=moj.tauron.pl
    (same flow as before, no captcha/OTP observed).
  • Payer: GET /Home/Pages/archiwum-dokumentow carries one
    `data-client-number="<payer>" data-client-checksum="<64 hex>"` div per supply
    point; the checksum of the chosen payer is required by the JSON API
    (`select_payer`, `parse_client_checksum`).
  • Listing: GET /api/sitecore/EbokArchiveDocuments/GetDocuments (rangeFrom/rangeTo
    paging, from/to filter = issue date) → JSON {Status, TotalNumber, Documents}
    (`list_documents`, `parse_documents_json`).
  • PDF: GET the document's `Url` → JSON {Status, Document: <base64 PDF>}
    (`download_pdf`).

Kept dependency-free (only `requests`, already in requirements.txt). All network
calls go through `self._session` (a requests.Session, or any object exposing a
compatible `.get`/`.post`), injected in the constructor so tests can substitute
a fake session without an extra mocking library. Parsing helpers are plain
functions with no I/O, testable directly on captured fixtures.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from html import unescape
from html.parser import HTMLParser
from typing import Callable, Optional
from urllib.parse import urlencode, urljoin

import requests

logger = logging.getLogger(__name__)

LOGIN_URL = 'https://logowanie.tauron.pl/realms/ext/protocol/cas/login'
SERVICE = 'https://moj.tauron.pl'
SYSTEM = 'T18'
USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) pv_roi_tracker/ebok-sync'
REQUEST_TIMEOUT = 45
ARCHIVE_TIMEOUT = 60
RETRY_DELAYS = (10, 30, 90)  # s przed kolejnymi ponowieniami GET po timeoucie / błędzie połączenia
PAGE_SIZE = 100
MAX_PAGES = 10  # safety cap: 1000 dokumentów
# Status dokumentu uznawany za zapłacony; każdy inny = niezapłacony (słownik poznajemy z logów).
PAID_STATUSES = {'Opłacona', 'Rozliczono', 'Zapłacona'}
_PAID_FOLDED = {s.casefold() for s in PAID_STATUSES}


class EbokError(Exception):
    """Base class for eBOK client errors."""


class EbokLoginFailed(EbokError):
    """Wrong username/password."""


class EbokLoginBlocked(EbokError):
    """Tauron temporarily blocked further login attempts for this account."""


class EbokLayoutChanged(EbokError):
    """Expected page structure (login form, JSON payload, ...) not found —
    Tauron likely changed their frontend."""


@dataclass
class EbokDocument:
    """One document from the Mój TAURON archive (GetDocuments)."""
    signature: str                          # DocSignature, e.g. "T/K1/BN111111/0017/26"
    doc_name: str                           # DocKindValue, e.g. "Faktura rzeczywista"
    issued_on: str                          # IssueDateFormatted, "DD.MM.YYYY"
    amount_gross_pln: Optional[float]
    due_date: Optional[date] = None         # PaymentDate — None for docs without one (e.g. noty)
    paid: bool = False                      # Status in PAID_STATUSES
    pdf_url: Optional[str] = None           # absolute URL of GetDocumentImage
    status: str = ''                        # raw Status ("Opłacona", "Rozliczono", ...)
    doc_type: str = ''                      # DocType: FV / FK / NB


@dataclass
class EbokPayment:
    """One row of the payments history (GetPayments); format known only from the E0 probe."""
    date: Optional[date]
    amount: Optional[float]
    type: str                               # "Wpłata" / "Zwrot" / "Odpisanie"
    doc_number: str = ''
    # (DocSignature, PaymentAmount = brutto faktury, PaymentSettlementAmount = rozliczone tą wpłatą)
    settlements: list = field(default_factory=list)


PAYMENT_TYPES = ('Wpłata', 'Zwrot', 'Odpisanie')


class EbokClient:
    """One instance per sync run (or kept alive for the add-on's process lifetime —
    login() is a no-op once already logged in, so reuse across calls is free and
    avoids the disk-cookie-cache pitfall found in the Etap 1 spike: flattening
    `session.cookies` to a plain dict drops cookie-domain scoping and causes a
    redirect loop.  Persisting a session across add-on restarts is not needed —
    a single fresh login is cheap and the sync job runs at most a few times a day.
    """

    def __init__(self, username: str, password: str,
                 session: Optional[requests.Session] = None,
                 sleep: Callable[[float], None] = time.sleep):
        self._username = username
        self._password = password
        self._session = session or requests.Session()
        self._sleep = sleep
        self._logged_in = False
        self._payer_id: Optional[str] = None
        self._checksum: Optional[str] = None

    def _get_retrying(self, *args, **kwargs):
        """session.get z ponowieniami po timeoucie / błędzie połączenia (kolejne
        próby po RETRY_DELAYS; incydent 2026-10-19: jednorazowy ReadTimeout
        przy wyborze płatnika zostawił health `degraded` do następnego joba). Tylko GET —
        POST logowania ponawia wyłącznie login()
        (raz, po zerwanym połączeniu) — powtórki zbliżają konto do blokady Taurona."""
        for delay in RETRY_DELAYS:
            try:
                return self._session.get(*args, **kwargs)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                logger.warning('eBOK: %s — ponawiam za %d s', exc, delay)
                self._sleep(delay)
        return self._session.get(*args, **kwargs)

    # ── login ────────────────────────────────────────────────────────────────

    def login(self) -> None:
        """Log in once; safe to call again (no-op) once already logged in."""
        if self._logged_in:
            return
        payload = {'username': self._username, 'password': self._password, 'credentialId': ''}
        for attempt in (1, 2):
            r = self._get_retrying(LOGIN_URL, params={'service': SERVICE},
                                   headers={'User-Agent': USER_AGENT}, timeout=REQUEST_TIMEOUT)
            m = re.search(r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"', r.text)
            if not m:
                raise EbokLayoutChanged('Nie znaleziono formularza logowania (kc-form-login)')
            action = m.group(1).replace('&amp;', '&')
            try:
                r2 = self._session.post(action, data=payload, headers={'User-Agent': USER_AGENT},
                                         timeout=REQUEST_TIMEOUT, allow_redirects=True)
                break
            except requests.exceptions.ConnectionError as exc:
                # Zerwane połączenie bez odpowiedzi (incydent 2026-10-28: RemoteDisconnected):
                # najpewniej to zamknięte keep-alive, więc jedno ponowienie z nowym formularzem
                # (nie da się wykluczyć, że serwer policzył próbę — dlatego max 2 POST-y).
                # ReadTimeout i kolejne zerwanie → błąd, żeby nie zbliżać konta do blokady Taurona.
                if attempt == 2:
                    raise
                logger.warning('eBOK: %s przy logowaniu — ponawiam za %d s', exc, RETRY_DELAYS[0])
                self._sleep(RETRY_DELAYS[0])
        text = r2.text
        if 'Przekroczono maksymalną liczbę logowań' in text:
            raise EbokLoginBlocked('Tauron zablokował logowania — spróbuj ponownie za 24h')
        if 'id="kc-form-login"' in text:
            raise EbokLoginFailed('Nieprawidłowy login lub hasło eBOK')
        self._logged_in = True
        logger.info('eBOK: zalogowano')

    def _get(self, url: str, timeout: int):
        """GET within the logged-in session. Tauron expires the Keycloak session
        server-side while `_logged_in` stays True, and an expired session answers
        with the login page at status 200 (incident 2026-10-01: strona wyboru
        klienta bez wierszy → mylące "Płatnik nie znaleziony"). On a login
        page: log in again and retry once; still a login page → EbokError."""
        self.login()
        r = self._get_retrying(url, headers={'User-Agent': USER_AGENT}, timeout=timeout)
        if not _is_login_page(r):
            return r
        logger.info('eBOK: sesja wygasła — logowanie ponownie')
        self._logged_in = False
        self.login()
        r = self._get_retrying(url, headers={'User-Agent': USER_AGENT}, timeout=timeout)
        if _is_login_page(r):
            raise EbokError('Sesja eBOK nie utrzymała się po ponownym logowaniu '
                            '(serwer nadal zwraca stronę logowania)')
        return r

    def select_payer(self, payer_id: str) -> None:
        """Reads the client checksum of `payer_id` from the documents archive page;
        it is required by the JSON API."""
        r = self._get(f'{SERVICE}/Home/Pages/archiwum-dokumentow', REQUEST_TIMEOUT)
        if r.status_code != 200:
            raise EbokError(f'Pobranie archiwum dokumentów nie powiodło się (status {r.status_code})')
        if not payer_id:
            clients = parse_clients(r.text)
            if len(clients) != 1:
                raise EbokError('Ustaw ebok_payer_id: na koncie jest '
                                f'{len(clients)} płatników, nie da się wybrać automatycznie')
            payer_id = next(iter(clients))
        checksum = parse_client_checksum(r.text, payer_id)
        if checksum is None:
            raise EbokError(f'Płatnik {payer_id} nie znaleziony na liście punktów')
        self._payer_id = str(payer_id)
        self._checksum = checksum

    # ── listing ──────────────────────────────────────────────────────────────

    def list_documents(self, date_from: date, date_to: date) -> list[EbokDocument]:
        """Documents issued between the dates (inclusive) for the selected payer,
        fetched in pages of PAGE_SIZE (at most MAX_PAGES)."""
        if not self._checksum or not self._payer_id:
            raise EbokError('Nie wybrano płatnika (select_payer)')
        docs: list[EbokDocument] = []
        for page in range(MAX_PAGES):
            range_from = page * PAGE_SIZE + 1
            query = urlencode({
                'rangeFrom': range_from, 'rangeTo': range_from + PAGE_SIZE - 1,
                'clientNumber': self._payer_id, 'clientNumberCheckSum': self._checksum,
                'system': SYSTEM, 'from': date_from.strftime('%d.%m.%Y'),
                'to': date_to.strftime('%d.%m.%Y'), 'docKind': 'All', 'docNumber': '',
            })
            r = self._get(f'{SERVICE}/api/sitecore/EbokArchiveDocuments/GetDocuments?{query}',
                          ARCHIVE_TIMEOUT)
            if r.status_code != 200:
                raise EbokError(f'Pobranie listy dokumentów nie powiodło się (status {r.status_code})')
            data = _json_or_layout_error(r, 'lista dokumentów')
            batch = parse_documents_json(data)
            docs.extend(batch)
            total = data.get('TotalNumber')
            if not isinstance(total, int) or len(docs) >= total:
                break
            if not batch:
                logger.warning('eBOK: pusta strona listy przy %d z %d dok. — lista może być niepełna',
                               len(docs), total)
                break
        else:
            logger.warning('eBOK: lista dokumentów obcięta do %d stron (%d dok.)', MAX_PAGES, len(docs))
        return docs

    def list_payments(self, date_from: date, date_to: date) -> list[EbokPayment]:
        """Payments history between the dates for the selected payer. The payments page
        has its own client checksum (different from the documents archive one)."""
        if not self._payer_id:
            raise EbokError('Nie wybrano płatnika (select_payer)')
        page_html = self._get(f'{SERVICE}/Home/Pages/lista-wplat', REQUEST_TIMEOUT)
        if page_html.status_code != 200:
            raise EbokError(f'Pobranie listy wpłat nie powiodło się (status {page_html.status_code})')
        checksum = parse_client_checksum(page_html.text, self._payer_id)
        if checksum is None:
            raise EbokError('Płatnik nie znaleziony na stronie wpłat')
        payments: list[EbokPayment] = []
        received = 0
        for page in range(MAX_PAGES):
            range_from = page * PAGE_SIZE + 1
            query = urlencode({
                'rangeFrom': range_from, 'rangeTo': range_from + PAGE_SIZE - 1,
                'clientNumber': self._payer_id, 'clientNumberCheckSum': checksum,
                'system': SYSTEM, 'from': date_from.strftime('%d.%m.%Y'),
                'to': date_to.strftime('%d.%m.%Y'), 'docKind': 'All',
                'paymentFrom': '', 'paymentTo': '',
            })
            r = self._get(f'{SERVICE}/api/sitecore/EbokPaymentsHistory/GetPayments?{query}',
                          ARCHIVE_TIMEOUT)
            if r.status_code != 200:
                raise EbokError(f'Pobranie listy wpłat nie powiodło się (status {r.status_code})')
            data = _json_or_layout_error(r, 'lista wpłat')
            rows = data.get('Documents')
            if not isinstance(rows, list):
                raise EbokLayoutChanged('lista wpłat: brak listy Documents w odpowiedzi')
            received += len(rows)
            batch = parse_payments_json(data)
            payments.extend(batch)
            total = data.get('TotalNumber')
            if not isinstance(total, int) or received >= total:
                break
            if not batch:
                logger.warning('eBOK: pusta strona listy wpłat przy %d z %d — lista może być niepełna',
                               received, total)
                break
        else:
            logger.warning('eBOK: lista wpłat obcięta do %d stron (%d wpłat)', MAX_PAGES, len(payments))
        return payments

    def download_pdf(self, pdf_url: str) -> bytes:
        r = self._get(pdf_url, REQUEST_TIMEOUT)
        if r.status_code != 200:
            raise EbokError(f'Pobranie PDF nie powiodło się (status {r.status_code})')
        data = _json_or_layout_error(r, 'PDF')
        try:
            pdf = base64.b64decode(data.get('Document') or '', validate=False)
        except (binascii.Error, ValueError, TypeError) as exc:
            raise EbokError(f'Pobranie PDF: niepoprawne dane base64 ({exc})') from exc
        if not pdf.startswith(b'%PDF'):
            raise EbokError('Pobranie PDF: odpowiedź nie jest plikiem PDF')
        return pdf

    def read_contract(self) -> dict:
        """Umowa z portalu: strona główna (umowa, taryfa, faktura) uzupełniona szczegółami
        punktu poboru (taryfa, moc, rozliczenie, OSD). Błąd szczegółów nie psuje wyniku;
        brak taryfy w obu miejscach = EbokLayoutChanged."""
        contract = parse_home_contract(self._get(f'{SERVICE}/', REQUEST_TIMEOUT).text)
        try:
            umowa = self._get(f'{SERVICE}/Home/Pages/Umowa?'
                              + urlencode({'sid': SYSTEM, 'pp': self._payer_id or ''}),
                              REQUEST_TIMEOUT)
            link = _POINT_LINK_RE.search(umowa.text)
            if link:
                href = link.group(1).replace('&amp;', '&')
                url = href if href.startswith('http') else SERVICE + (href if href.startswith('/') else '/' + href)
                if not url.startswith(SERVICE + '/'):
                    raise EbokError('link punktu poboru poza domeną portalu')
                contract = {**contract, **parse_point_details(self._get(url, REQUEST_TIMEOUT).text)}
            else:
                logger.warning('eBOK: brak linku do punktów poboru na stronie umowy')
        except Exception as exc:
            logger.warning('eBOK: szczegóły punktu poboru niedostępne (%s)', type(exc).__name__)
        if not contract.get('taryfa'):
            raise EbokLayoutChanged('Nie znaleziono taryfy na stronie umowy')
        return contract

    def probe_invoices_page(self, known: set) -> dict:
        """Sonda diagnostyczna (0.55.5, tylko odczyt): strona „Faktury” i jej skrypty z tej samej
        domeny → ścieżki `/api/sitecore/<Kontroler>/<Akcja>`, nazwy atrybutów `data-*` oraz
        sygnatury dokumentów, których nie ma w `known`. Bez numeru płatnika i checksum w wyniku."""
        if not self._payer_id:
            raise EbokError('Nie wybrano płatnika (select_payer)')
        r = self._get(f'{SERVICE}/Home/Pages/Faktury?'
                      + urlencode({'sid': SYSTEM, 'pp': self._payer_id}), REQUEST_TIMEOUT)
        html = r.text or ''
        api_paths = _api_paths(html)
        scripts = []
        page_url = f'{SERVICE}/Home/Pages/Faktury'
        for src in _SCRIPT_SRC_RE.findall(html):
            url = urljoin(page_url, unescape(src))
            if url.startswith(SERVICE + '/') and url not in scripts:
                scripts.append(url)
        fetched = failed = 0
        js_context = _js_contexts(html)   # kod wywołania bywa inline w HTML strony
        expand_context = []
        for inline in _INLINE_SCRIPT_RE.findall(html):
            expand_context.extend(_code_contexts(inline, _EXPAND_KEYWORDS))
        for url in scripts[:MAX_PROBE_SCRIPTS]:
            try:
                js = self._get_retrying(url, headers={'User-Agent': USER_AGENT}, timeout=REQUEST_TIMEOUT)
                if js.status_code != 200 or _is_login_page(js):
                    failed += 1
                    continue
                fetched += 1
                api_paths.update(_api_paths(js.text))
                if len(js_context) < 3:
                    js_context.extend(_js_contexts(js.text))
                if len(expand_context) < 8:
                    expand_context.extend(_code_contexts(js.text, _EXPAND_KEYWORDS))
            except Exception as exc:
                failed += 1
                logger.warning('eBOK: sonda — skrypt niedostępny (%s)', type(exc).__name__)
        signatures = set(_SIGNATURE_RE.findall(html))
        return {'status': r.status_code, 'ok': r.status_code == 200, 'size': len(html),
                'api_paths': sorted(api_paths),
                'data_attrs': sorted(set(_DATA_ATTR_RE.findall(html))),
                'scripts': {'total': len(scripts), 'fetched': fetched, 'failed': failed,
                            'skipped': max(0, len(scripts) - MAX_PROBE_SCRIPTS)},
                'signatures_total': len(signatures),
                'unknown_signatures': sorted(s for s in signatures if not is_known(s, known)),
                'js_context': js_context[:3],
                'expand_context': expand_context[:8],
                'item_skeleton': _html_skeleton(html)}

    def probe_invoice_list(self) -> dict:
        """Sonda E0d (0.55.8, tylko odczyt): jak JS strony Faktury (`loadMoreItems`) — POST
        `LoadMoreInvoicesProducts?sid&pp` z `startingIndex`, `searchTerm`, `PickupMediumType`
        i `parentViewModel` (zmienna JS `serializedViewModel` ze strony, przekazywana bez zmian).
        Zwraca wyłącznie kształt odpowiedzi (`_response_shape`): bez kwot, numerów i płatnika;
        wartość `serializedViewModel` nie opuszcza metody."""
        if not self._payer_id:
            raise EbokError('Nie wybrano płatnika (select_payer)')
        page_url = f'{SERVICE}/Home/Pages/Faktury?' + urlencode({'sid': SYSTEM, 'pp': self._payer_id})
        html = self._get(page_url, REQUEST_TIMEOUT).text or ''
        view_obj = _js_object_var(html, 'serializedViewModel') or None   # pusty {} = brak danych do wysłania
        object_unparsed = view_obj is None and bool(re.search(r'(?<![\w.$])serializedViewModel\s*=\s*\{', html))
        view_text = None if view_obj is not None else _js_string_var(html, 'serializedViewModel')
        medium = _js_string_var(html, 'mediumType')
        if medium is not None and not _MEDIUM_RE.fullmatch(medium):
            medium = None   # nietypowa wartość nie trafia ani do POST-a, ani do diag
        medium_source = 'literal' if medium is not None else None
        if medium is None:
            medium = _search_type_default(html)
            medium_source = 'SearchType' if medium is not None else None
        request = {'view_model_found': view_obj is not None or view_text is not None,
                   'view_model_kind': 'object' if view_obj is not None else ('string' if view_text is not None else None),
                   'view_model_keys': len(view_obj) if view_obj is not None else 0,
                   'view_model_object_unparsed': object_unparsed,
                   'view_model_len': len(view_text or ''),
                   'medium_type_found': medium is not None,
                   'medium_type': medium, 'medium_type_source': medium_source}
        if view_obj is None and view_text is None:
            request['view_model_context'] = _var_context(html, 'serializedViewModel')
            return {'ok': False, 'request': request,
                    'skipped': 'brak (lub nie do sparsowania) serializedViewModel jako obiektu ani literału tekstowego — POST pominięty'}
        if medium is None:
            request['medium_type_context'] = _var_context(html, 'mediumType')
        form = [('startingIndex', 0), ('searchTerm', ''), ('PickupMediumType', medium or '')]
        form += _flatten_params(view_obj, 'parentViewModel') if view_obj is not None \
            else [('parentViewModel', view_text)]
        r = self._session.post(
            f'{SERVICE}/api/sitecore/EbokInvoiceList/LoadMoreInvoicesProducts?'
            + urlencode({'sid': SYSTEM, 'pp': self._payer_id}),
            data=form,
            headers={'User-Agent': USER_AGENT, 'X-Requested-With': 'XMLHttpRequest', 'Referer': page_url},
            timeout=REQUEST_TIMEOUT)
        if _is_login_page(r):
            raise EbokError('Sesja eBOK wygasła przy zapytaniu o listę faktur')
        result_html = ''
        try:
            payload = json.loads(r.text or '')
            rh = payload.get('ResultHtml') if isinstance(payload, dict) else None
            result_html = rh if isinstance(rh, str) else ''
        except (ValueError, RecursionError):
            pass
        code = [c for inline in _INLINE_SCRIPT_RE.findall(result_html)
                for c in _code_contexts(inline, _SOURCE_KEYWORDS)]
        for inline in _INLINE_SCRIPT_RE.findall(html):
            code.extend(_code_contexts(inline, _SOURCE_KEYWORDS))
        return {'status': r.status_code, 'ok': r.status_code == 200, 'request': request,
                'content_type': r.headers.get('Content-Type', ''), 'size': len(r.text or ''),
                'shape': _response_shape(r.text or ''),
                'source_hints': {'view_model': _view_model_hints(view_obj) if view_obj is not None else {},
                                 'page_tokens': _token_hits(html),
                                 'result_html_tokens': _token_hits(result_html),
                                 'result_html_code': [c for c in code
                                                      if any(k in c for k in _SOURCE_KEYWORDS)][:8]}}


_STATUS_WORD_RE = re.compile(
    r'Nieopłacon\w*|Opłacon\w*|Do zapłaty|Po terminie|Rozliczon\w*|Korekt\w*|Faktur\w*|Zaległ\w*', re.I)
_ITEM_START_RE = re.compile(r'<tr\b[^>]*>(?=\s*<td)', re.I)   # wiersz tabeli z komórkami (nie menu `data-item`)
_JS_ANCHOR = 'LoadMoreInvoicesProducts'


def _js_string_var(html: str, name: str) -> Optional[str]:
    """Wartość tekstowa przypisania `<name> = '...'` / `"..."` z JS strony (z dekodowaniem sekwencji
    ucieczki JS); pierwszy niepusty literał, a gdy są tylko puste — pusty. None, gdy zmiennej nie ma,
    nie jest literałem tekstowym albo nie da się jej odkodować (nie wysyłamy zepsutej wartości)."""
    pattern = r'(?<![\w.$])' + re.escape(name) + r'\s*=\s*(["\'])((?:\\.|(?!\1).)*)\1'
    found = None
    for m in re.finditer(pattern, html or '', re.S):
        quote, body = m.groups()
        if quote == "'":
            body = re.sub(r'\\.|"', lambda t: {"\\'": "'", '"': '\\"'}.get(t.group(0), t.group(0)), body)
        try:
            value = json.loads('"' + body + '"')
        except ValueError:
            continue
        if value:
            return value
        found = value
    return found


def _js_object_var(html: str, name: str) -> Optional[dict]:
    """Literał obiektu `<name> = {...}` z JS strony (dopasowanie nawiasów z uwzględnieniem
    cudzysłowów) sparsowany jako JSON. None, gdy zmiennej nie ma, nie jest obiektem JSON
    albo nawiasy się nie domykają."""
    for m in re.finditer(r'(?<![\w.$])' + re.escape(name) + r'\s*=\s*(?=\{)', html or ''):
        start, depth, quote, i = m.end(), 0, None, m.end()
        while i < len(html):
            ch = html[i]
            if quote:
                if ch == '\\':
                    i += 1
                elif ch == quote:
                    quote = None
            elif ch in '"\'':
                quote = ch
            elif ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(html[start:i + 1])
                    except ValueError:
                        break
                    if isinstance(value, dict):
                        return value
                    break
            i += 1
    return None


def _flatten_params(value, prefix: str) -> list:
    """Spłaszczenie jak `jQuery.param`: obiekt → `p[k]`, tablica prymitywów → `p[]`, tablica
    obiektów/tablic → `p[i]`; null → pusty tekst, bool → true/false. Puste tablice nic nie wysyłają."""
    def scalar(v):
        if isinstance(v, float) and v.is_integer():
            v = int(v)   # JS: 1.0 → "1"
        return '' if v is None else ('true' if v is True else 'false' if v is False else str(v))
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            out += _flatten_params(v, f'{prefix}[{k}]')
        return out
    if isinstance(value, list):
        out = []
        for i, v in enumerate(value):
            nested = isinstance(v, (dict, list))
            out += _flatten_params(v, f'{prefix}[{i if nested else ""}]')
        return out
    return [(prefix, scalar(value))]


_MEDIUM_RE = re.compile(r'[A-Za-z]{1,20}')
_SEARCH_TYPE_TAG_RE = re.compile(r'<(select|input)\b(?=[^>]*(?<![-\w])id=["\']SearchType["\'])[^>]*>', re.I)
_VALUE_ATTR_RE = re.compile(r'(?<![-\w])value=["\']([^"\']*)["\']', re.I)


def _search_type_default(html: str) -> Optional[str]:
    """Domyślna wartość kontrolki `#SearchType` (select: opcja `selected` albo pierwsza; input: `value`
    z samego znacznika) — tylko krótkie słowo (rodzaj medium), inaczej None."""
    html = html or ''
    m = _SEARCH_TYPE_TAG_RE.search(html)
    if not m:
        return None
    if m.group(1).lower() == 'input':
        v = _VALUE_ATTR_RE.search(m.group(0))
    else:
        body = html[m.end():].split('</select>', 1)[0]
        opts = re.findall(r'<option\b([^>]*)>', body, re.I)
        pick = next((o for o in opts if re.search(r'\bselected\b', o, re.I)), opts[0] if opts else '')
        v = _VALUE_ATTR_RE.search(pick)
    value = v.group(1) if v else None
    return value if value and _MEDIUM_RE.fullmatch(value) else None


def _var_context(html: str, name: str, width: int = 100) -> str:
    """Fragment kodu wokół pierwszego użycia zmiennej (gdy nie jest literałem tekstowym): treść
    literałów tekstowych zastąpiona „…”, cyfry → 9 — zostaje sama składnia."""
    m = re.search(r'(?<![\w.$])' + re.escape(name), html or '')
    if not m:
        return '<brak zmiennej>'
    # literały zamieniamy na szerokim oknie (żeby cudzysłów na brzegu nie rozparował wzorca), dopiero potem tniemy
    wide = html[max(0, m.start() - 4 * width):m.end() + 4 * width]
    wide = re.sub(r'(["\'])(?:\\.|(?!\1).)*\1', '"…"', wide, flags=re.S)
    k = wide.find(name)
    chunk = wide[max(0, k - width):k + len(name) + width]
    return re.sub(r'\d', '9', re.sub(r'\s+', ' ', chunk))


def _js_contexts(js: str, width: int = 600) -> list:
    """Fragmenty kodu JS wokół wywołania `LoadMoreInvoicesProducts` (jak JS buduje zapytanie).
    Kod, nie dane — ale długie literały (tokeny) i ciągi 5+ cyfr są wycinane."""
    out = []
    for m in re.finditer(_JS_ANCHOR, js or ''):
        chunk = js[max(0, m.start() - width):m.end() + width]
        chunk = re.sub(r'[A-Za-z0-9+/=_-]{32,}', '<token>', chunk)
        out.append(re.sub(r'\s+', ' ', re.sub(r'\d{5,}', '<num>', chunk)))
        if len(out) == 3:
            break
    return out


_EXPAND_KEYWORDS = ('expand-init', 'toggle-details', 'toggle-box', 'check-all-in-pp', 'agreement', 'EbokInvoiceList')
_INLINE_SCRIPT_RE = re.compile(r'<script\b(?![^>]*\bsrc=)[^>]*>(.*?)</script>', re.S | re.I)
_JS_TOKEN_RE = re.compile(   # jednoznaczne (bez ReDoS): literały per rodzaj cudzysłowu, komentarze
    r'''"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`|/\*.*?\*/|//[^\n]*''', re.S)
_KEEP_SELECTOR_RE = re.compile(r'[#.\[][\w\-.#\[\]=]{0,58}')
_KEEP_WORD_RE = re.compile(r'click|change|GET|POST|json|html')
_KEEP_KEBAB_RE = re.compile(r'(?=.*(?:data|agreement|expand|toggle|details|check|invoice|button|box))[a-z][a-z0-9]*(?:-[a-z0-9]+)+')
_MAX_CODE_SCAN = 400_000


def _keep_js_literal(m: 're.Match') -> str:
    """Token JS: komentarze znikają, backticki → „…”; z literałów zostaje tylko to, co opisuje mechanikę
    (selektor bez spacji, nazwa zdarzenia, kebab-case z listy słów kluczowych, ścieżka `/api/sitecore/…`
    bez wartości parametrów); reszta → „…”."""
    tok = m.group(0)
    if tok[0] == '/':
        return ' '
    if tok[0] == '`':
        return '"…"'
    inner = tok[1:-1]
    if len(inner) <= 60 and (_KEEP_SELECTOR_RE.fullmatch(inner) or _KEEP_WORD_RE.fullmatch(inner)
                             or _KEEP_KEBAB_RE.fullmatch(inner)):
        return '"' + inner + '"'
    paths = _api_paths(inner) if len(inner) <= 300 else set()
    if len(paths) == 1:
        return '"' + next(iter(paths)) + '"'
    return '"…"'


def _code_contexts(js: str, keywords: tuple, width: int = 300, limit: int = 8) -> list:
    """Sonda E0h: fragmenty kodu JS wokół słów kluczowych (handler rozwijania punktu poboru).
    Literały tekstowe maskowane przez `_keep_js_literal`, ciągi 32+ znaków → <token>, cyfry → 9."""
    code = _JS_TOKEN_RE.sub(_keep_js_literal, (js or '')[:_MAX_CODE_SCAN])
    code = re.sub(r'[A-Za-z0-9+=_-]{32,}', '<token>', code)
    out, last = [], -10 ** 9
    for m in sorted(re.finditer('|'.join(re.escape(k) for k in keywords), code), key=lambda x: x.start()):
        if m.start() - last < width:
            continue
        last = m.start()
        chunk = code[max(0, m.start() - width):m.end() + width]
        out.append(re.sub(r'\d', '9', re.sub(r'\s+', ' ', chunk)))
        if len(out) >= limit:
            break
    return out


class _ExpandAttrParser(HTMLParser):
    _CLASSES = ('toggle-button', 'toggle-details-button', 'check-all-in-pp-expanded')

    def __init__(self, limit: int):
        super().__init__(convert_charrefs=True)
        self.limit, self.found = limit, []

    def handle_starttag(self, tag, attrs):
        if len(self.found) >= self.limit:
            return
        d = {a: (v or '') for a, v in attrs}
        classes = d.get('class', '').split()
        if 'data-expand-init' not in d and not any(c in classes for c in self._CLASSES):
            return
        shaped = {}
        for name, value in d.items():
            if not name.startswith('data-'):
                continue
            paths = _api_paths(value)
            shaped[re.sub(r'\d', '9', name)] = next(iter(paths)) if len(paths) == 1 else _mask_value_shape(value)
        self.found.append({'tag': tag, 'class': re.sub(r'\d', '9', ' '.join(classes)), 'attrs': shaped})


def _expand_attrs(html: str, limit: int = 6) -> list:
    """Sonda E0h: elementy rozwijania w odpowiedzi listy (`toggle-button`, `data-expand-init`, …):
    nazwy `data-*` i wartości tylko jako maska kształtu albo ścieżka API z samymi nazwami parametrów."""
    parser = _ExpandAttrParser(limit)
    parser.feed(html or '')
    parser.close()
    return parser.found


class _SkeletonParser(HTMLParser):
    """Znaczniki + klasy + nazwy data-*, tekst zastąpiony „…”, poza słowami statusu."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list = []

    def handle_starttag(self, tag, attrs):
        names = [f'.{c}' for a, v in attrs if a == 'class' for c in (v or '').split()]
        names += [f'[{a}]' for a, _ in attrs if a.startswith('data-')]
        self.parts.append('<' + tag + ''.join(names) + '>')

    def handle_endtag(self, tag):
        self.parts.append('</>')

    def handle_data(self, data):
        words = _STATUS_WORD_RE.findall(data)
        if words:
            self.parts.append('"' + ' '.join(words) + '"')
        elif data.strip():
            self.parts.append('…')


def _html_skeleton(html: str, limit: int = 1500) -> str:
    """Szkielet pierwszego wiersza tabeli `<tr><td>` (kształt wiersza listy faktur), bez treści."""
    m = _ITEM_START_RE.search(html or '')
    if not m:
        return '<brak wiersza tabeli>'
    parser = _SkeletonParser()
    parser.feed(html[m.start():m.start() + 3000])
    parser.close()
    return ''.join(parser.parts)[:limit]


_VOID_TAGS = frozenset({'img', 'input', 'br', 'hr', 'meta', 'link', 'source', 'wbr', 'col'})


class _Node:
    __slots__ = ('tag', 'classes', 'data', 'children', 'parent', 'texts', 'dvals')

    def __init__(self, tag, classes, data, parent):
        self.tag, self.classes, self.data, self.parent = tag, classes, data, parent
        self.children: list = []
        self.texts: list = []
        self.dvals: dict = {}

    def sig(self):
        return (self.tag, tuple(self.classes))

    def text(self) -> str:
        return ' '.join(' '.join(self.texts + [c.text() for c in self.children]).split())

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


class _TreeParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node('#root', [], [], None)
        self._cur = self.root

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, [c for a, v in attrs if a == 'class' for c in (v or '').split()],
                     [a for a, _ in attrs if a.startswith('data-')], self._cur)
        if tag in ('dt', 'dd'):   # HTML5: dt/dd domykają się przy kolejnym dt/dd
            n = self._cur
            while n is not self.root and n.tag not in ('dl', 'div', 'section', 'body'):
                if n.tag in ('dt', 'dd'):
                    self._cur = n.parent
                    break
                n = n.parent
        node.parent = self._cur
        node.dvals = {a: (v or '') for a, v in attrs if a.startswith('data-')}
        self._cur.children.append(node)
        for a, v in attrs:   # `value="Zapłać"` na przycisku to etykieta UI, jak tekst
            if a == 'value' and tag == 'input' and v:
                node.texts.append(v)
        if tag not in _VOID_TAGS:
            self._cur = node

    def handle_endtag(self, tag):
        n = self._cur
        while n is not self.root and n.tag != tag:
            n = n.parent
        if n is not self.root:
            self._cur = n.parent

    def handle_data(self, data):
        self._cur.texts.append(data)


def _node_skeleton(node: '_Node') -> str:
    head = '<' + node.tag + ''.join('.' + re.sub(r'\d', '9', c) for c in node.classes) + ''.join('[' + re.sub(r'\d', '9', a) + ']' for a in node.data) + '>'
    own = ' '.join(' '.join(node.texts).split())
    words = _STATUS_WORD_RE.findall(own)
    body = ('"' + ' '.join(words) + '"') if words else ('…' if own else '')
    return head + body + ''.join(_node_skeleton(c) for c in node.children) + '</>'


def _mask_value_shape(text: str, limit: int = 40) -> str:
    """Cyfry → 9, litery → a; reszta (separatory, spacje) bez zmian. Nie niesie wartości."""
    out = ''.join('9' if ch.isdigit() else 'a' if ch.isalpha() else ch for ch in ' '.join(text.split()))
    return out[:limit]


def _invoice_block_shape(html: str, limit: int = 1500) -> Optional[dict]:
    """Sonda E0f: szkielet powtarzającego się bloku faktury w układzie `dl/dt/dd` (bez `<tr>`).
    Blok = najniższy przodek pierwszego `dt` powtórzony u rodzeństwa (a gdy jest jeden —
    przodek z tekstem „Zapłać”, w ostateczności rodzic `dl`). Dla każdego `dt`: etykieta UI
    (tylko bez cyfr i ≤40 znaków), zamaskowany kształt wartości i element, w którym leży."""
    parser = _TreeParser()
    parser.feed(html or '')
    parser.close()
    dts = [n for n in parser.root.walk() if n.tag == 'dt']
    if not dts:
        return None
    block = None
    node = dts[0].parent
    while node is not None and node is not parser.root:
        if node.parent is not None and sum(1 for s in node.parent.children if s.sig() == node.sig()) >= 2:
            block = node
            break
        node = node.parent
    if block is None:
        node = dts[0].parent
        while node is not None and node is not parser.root:
            if re.search(r'zapłać', node.text(), re.I):
                block = node
                break
            node = node.parent
    if block is None:
        block = dts[0].parent
    count = sum(1 for s in block.parent.children if s.sig() == block.sig()) if block.parent else 1
    fields = []
    for dt in (n for n in block.walk() if n.tag == 'dt'):
        label = dt.text()
        siblings = dt.parent.children
        dd = next((s for s in siblings[siblings.index(dt) + 1:] if s.tag in ('dd', 'dt')), None)
        dd = dd if dd is not None and dd.tag == 'dd' else None
        path, cur = 'dd', dd
        while cur is not None and len(cur.children) == 1 and not ' '.join(cur.texts).strip():
            cur = cur.children[0]
            path += '>' + cur.tag
        fields.append({'label': label if len(label) <= 40 and not re.search(r'\d', label) else 'str',
                       'shape': _mask_value_shape(dd.text()) if dd is not None else None,
                       'in': path if dd is not None else None})
    return {'blocks': count, 'children': len(list(block.walk())) - 1, 'fields': fields,
            'skeleton': _node_skeleton(block)[:limit]}


_UI_LABELS = frozenset({
    'zapłać', 'numer', 'typ', 'faktura', 'data wystawienia', 'termin płatności', 'kwota do zapłaty',
    'informacje dodatkowe', 'należność', 'nota odsetkowa', 'łącznie', 'do zapłaty', 'po terminie',
    'szczegóły', 'pobierz', 'nazwa', 'status'})


def _node_depth(node: '_Node') -> int:
    d = 0
    while node.parent is not None:
        node, d = node.parent, d + 1
    return d


def _leaf_fields(block: '_Node', limit: int = 60) -> list:
    """Liście tekstowe bloku: ścieżka tag.klasa (cyfry → 9), zamaskowany kształt, etykieta tylko
    gdy tekst to znane słowo UI (lista stała — nigdy adres, nazwa ani numer)."""
    out = []
    for n in block.walk():
        own = ' '.join(' '.join(n.texts).split())
        if not own:
            continue
        chain, cur = [], n
        while cur is not None and cur is not block.parent:
            chain.append(cur.tag + ''.join('.' + re.sub(r'\d', '9', c) for c in cur.classes))
            cur = cur.parent
        out.append({'path': '>'.join(reversed(chain))[:120], 'shape': _mask_value_shape(own),
                    'label': own if own.lower() in _UI_LABELS else None})
        if len(out) >= limit:
            break
    return out


def _repeated_block_shape(html: str, count: int, limit: int = 3000) -> Optional[dict]:
    """Sonda E0g: blok faktury = najwyższa grupa rodzeństwa o tej samej sygnaturze tag+klasy
    i liczności == `count` (ReturnedCount); przy kilku grupach na tej samej głębokości
    wygrywa ta z tekstem „Zapłać”. Zwraca szkielet i liście; bez wartości."""
    if not isinstance(count, int) or isinstance(count, bool) or count < 2:
        return None
    parser = _TreeParser()
    parser.feed(html or '')
    parser.close()
    best = None   # (priorytet, głębokość, pierwszy węzeł grupy)
    for parent in parser.root.walk():
        groups: dict = {}
        for c in parent.children:
            groups.setdefault(c.sig(), []).append(c)
        for members in groups.values():
            if len(members) != count:
                continue
            pay = any(re.search(r'zapłać', m.text(), re.I) for m in members)
            key = (0 if pay else 1, _node_depth(members[0]))
            if best is None or key < best[0]:
                best = (key, members[0])
    if best is None:
        return {'no_group': True, 'count': count}   # ReturnedCount nie zgadza się z żadną grupą rodzeństwa
    block = best[1]
    return {'blocks': count, 'children': len(list(block.walk())) - 1,
            'leaves': _leaf_fields(block), 'skeleton': _node_skeleton(block)[:limit]}


def _safe_repeated_block_shape(html: str, count):
    try:
        return _repeated_block_shape(html, count)
    except RecursionError:
        return {'error': 'RecursionError'}


def _safe_block_shape(html: str):
    try:
        return _invoice_block_shape(html)
    except RecursionError:
        return {'error': 'RecursionError'}   # głęboko zagnieżdżony HTML nie może zepsuć reszty sondy


def _details_blocks(html: str, limit: int = 3000) -> Optional[dict]:
    """Sonda E0i: ukryte `.row-details-container` (wiersze faktur pod punktem poboru) i wiersze
    z `data-charge-id`. Bez wartości: liście przez `_leaf_fields`, id tylko jako maska kształtu."""
    parser = _TreeParser()
    parser.feed(html or '')
    parser.close()
    nodes = list(parser.root.walk())
    containers = [n for n in nodes if 'row-details-container' in n.classes]
    charge = [n for n in nodes if 'data-charge-id' in n.dvals]
    if not containers and not charge:
        return None
    out: dict = {'containers': len(containers), 'charge_rows': len(charge)}
    if containers:
        first = containers[0]
        out.update({'children': len(list(first.walk())) - 1, 'leaves': _leaf_fields(first),
                    'skeleton': _node_skeleton(first)[:limit]})
    if charge:
        out['charge_id_shapes'] = sorted({_mask_value_shape(n.dvals['data-charge-id']) for n in charge})[:3]
        out['charge_attrs'] = sorted({re.sub(r'\d', '9', a) for n in charge for a in n.data})[:12]
        out['charge_row_skeleton'] = _node_skeleton(charge[0])[:limit]
    return out


def _safe_details_blocks(html: str):
    try:
        return _details_blocks(html)
    except RecursionError:
        return {'error': 'RecursionError'}


def _json_shape(value, depth: int = 0, key: str = '', returned: Optional[int] = None):
    if isinstance(value, dict):
        rc = value.get('ReturnedCount')
        return {re.sub(r'\d', '9', str(k)): _json_shape(v, depth + 1, str(k), rc if isinstance(rc, int) else None)
                for k, v in value.items()} if depth < 4 else 'object'
    if isinstance(value, list):
        return {'list': len(value), 'item': _json_shape(value[0], depth + 1) if value and depth < 4 else None}
    if isinstance(value, str):
        if re.search(r'<[A-Za-z]', value):
            return {'html': _html_skeleton(value), 'len': len(value),
                    'rows': len(_ITEM_START_RE.findall(value)),
                    'signatures': len(set(_SIGNATURE_RE.findall(value))),
                    'dl_blocks': _safe_block_shape(value),
                    'item_blocks': _safe_repeated_block_shape(value, returned),
                    'expand_attrs': _expand_attrs(value),
                    'details_blocks': _safe_details_blocks(value),
                    'tags': dict(Counter(re.findall(r'<([a-z][a-z0-9]*)', value, re.I)).most_common(8))}
        return value if _STATUS_WORD_RE.fullmatch(value) else 'str'
    if isinstance(value, int) and not isinstance(value, bool) and key.endswith('Count'):
        return value   # TotalCount / ReturnedCount: liczności, nie dane
    return type(value).__name__


_GUID_RE = re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')
_HEX64_RE = re.compile(r'(?<![0-9a-zA-Z])[0-9a-fA-F]{64}(?![0-9a-zA-Z])')
_HINT_KEY_RE = re.compile(r'source|check|sum|contractor|guid|payer|client|agreement', re.I)
_SOURCE_KEYWORDS = ('GetInvoices', 'EbokInvoices', 'sourceId', 'checkSum', 'contractorNumber')


def _token_hits(text: str, limit: int = 20) -> list:
    """Sonda E0j: gdzie w tekście stoją GUID-y i 64-znakowe hex-y (kandydaci na `sourceId` /
    `checkSum`): rodzaj, miejsce (`tag[atrybut]` albo `script`/`text`) i liczba. Bez wartości."""
    text = text or ''
    counts: Counter = Counter()
    for kind, rx in (('guid', _GUID_RE), ('hex64', _HEX64_RE)):
        for m in rx.finditer(text):
            lt, gt = text.rfind('<', 0, m.start()), text.rfind('>', 0, m.start())
            where = 'text'
            if lt > gt:   # wewnątrz znacznika → nazwa atrybutu
                tag = re.match(r'<([A-Za-z][\w-]*)', text[lt:])
                attr = re.search(r'([\w:-]+)\s*=\s*["\'][^"\']*$', text[lt:m.start()])
                where = f"{tag.group(1).lower() if tag else '?'}[{attr.group(1).lower() if attr else '?'}]"
            elif re.search(r'<script\b[^>]*>(?:(?!</script>).)*$', text[:m.start()], re.S | re.I):
                where = 'script'
            counts[(kind, re.sub(r'\d', '9', where))] += 1
    return [{'kind': k, 'where': w, 'n': n} for (k, w), n in counts.most_common(limit)]


def _view_model_hints(value, prefix: str = '', out: Optional[dict] = None) -> dict:
    """Sonda E0j: ścieżki kluczy view-modelu przypominające `sourceId`/`checkSum`/numer płatnika
    z zamaskowanym kształtem wartości (cyfry → 9, litery → a). Wartości nie opuszczają funkcji."""
    out = {} if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            path = f'{prefix}.{k}' if prefix else str(k)
            if isinstance(v, (dict, list)):
                _view_model_hints(v, path, out)
            elif _HINT_KEY_RE.search(str(k)) and v not in (None, ''):
                out[re.sub(r'\d', '9', path)] = _mask_value_shape(str(v))
    elif isinstance(value, list):
        for v in value[:3]:
            _view_model_hints(v, prefix + '[]', out)
    return out


def _response_shape(text: str) -> dict:
    """Kształt odpowiedzi: JSON → drzewo kluczy z typami; HTML → szkielet pierwszego `data-item`
    i liczba elementów `data-item`; sygnatury `T/K1/…` tylko jako liczba."""
    try:
        data = json.loads(text)
    except ValueError:
        return {'type': 'html' if '<' in text else 'text',
                'items': len(_ITEM_START_RE.findall(text)),
                'signatures': len(set(_SIGNATURE_RE.findall(text))),
                'tags': dict(Counter(re.findall(r'<([a-z][a-z0-9]*)', text, re.I)).most_common(8)),
                'skeleton': _html_skeleton(text)}
    return {'type': 'json', 'tree': _json_shape(data),
            'signatures': len(set(_SIGNATURE_RE.findall(json.dumps(data, ensure_ascii=False))))}


def _api_paths(text: str) -> set:
    """`/api/sitecore/<Kontroler>/<Akcja>` z tekstu; z zapytania zostają same nazwy parametrów
    (`...?rangeFrom&clientNumber`), nigdy wartości — te niosą płatnika i checksum."""
    found = set()
    for path, query in _API_PATH_RE.findall(text or ''):
        names = sorted(set(re.findall(r'([A-Za-z0-9_]+)=', unescape(query))))
        found.add(path + ('?' + '&'.join(names) if names else ''))
    return found


def _is_login_page(r) -> bool:
    return b'id="kc-form-login"' in (r.content or b'')


def _json_or_layout_error(r, what: str) -> dict:
    """Parsed JSON object with Status == "OK", otherwise EbokLayoutChanged."""
    try:
        data = json.loads(r.text)
    except ValueError as exc:
        raise EbokLayoutChanged(f'Odpowiedź ({what}) nie jest JSON-em — Tauron zmienił API?') from exc
    if not isinstance(data, dict) or data.get('Status') != 'OK':
        status = data.get('Status') if isinstance(data, dict) else type(data).__name__
        raise EbokLayoutChanged(f'Odpowiedź ({what}) ze statusem {status!r} zamiast "OK"')
    return data


# ── pure parsing helpers (no I/O — unit-testable on captured fixtures) ─────────

_CLIENT_DIV_RE = re.compile(r'<[^>]*\bdata-client-number="[^"]*"[^>]*>')


def parse_clients(html: str) -> dict:
    """{payer number: checksum (64 hex)} for every supply point on the documents archive page."""
    clients = {}
    for tag in _CLIENT_DIV_RE.findall(html or ''):
        number = re.search(r'data-client-number="([^"]*)"', tag)
        checksum = re.search(r'data-client-checksum="([0-9a-fA-F]+)"', tag)
        if number and checksum:
            clients[number.group(1).strip()] = checksum.group(1)
    return clients


def parse_client_checksum(html: str, payer_id: str) -> Optional[str]:
    """Checksum of `payer_id` on the documents archive page; None when the payer is not listed."""
    return parse_clients(html).get(str(payer_id))


_API_PATH_RE = re.compile(r'(/api/sitecore/[A-Za-z0-9_]+/[A-Za-z0-9_]+)(?:\?([^"\'\s<>]*))?')
_SCRIPT_SRC_RE = re.compile(r'<script[^>]+src="([^"]+)"', re.I)
_DATA_ATTR_RE = re.compile(r'\b(data-[a-z0-9-]+)=', re.I)
_SIGNATURE_RE = re.compile(r'\bT/K1/[A-Za-z0-9]+(?:/\d{4})?/\d{2}\b')
MAX_PROBE_SCRIPTS = 8
_POINT_LINK_RE = re.compile(r'href="([^"]*punkty-poboru[^"]*)"')
_DL_HORIZONTAL_RE = re.compile(r'<dl[^>]*\bdl-horizontal\b[^>]*>(.*?)</dl>', re.S | re.I)
_DT_DD_RE = re.compile(r'<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>', re.S | re.I)
_POINT_LABELS = {'taryfa': 'taryfa', 'moc umowna': 'moc_umowna',
                 'sposób rozliczenia': 'rozliczenie', 'nazwa osd': 'osd'}


def _dt_dd_pairs(html: str):
    """(etykieta, wartość) z par <dt>/<dd>; tagi usunięte, białe znaki zwinięte."""
    for label, value in _DT_DD_RE.findall(html or ''):
        yield (_plain(label), _plain(value))


def _plain(raw: str) -> str:
    return ' '.join(unescape(re.sub(r'<[^>]+>', ' ', raw)).split())


def parse_home_contract(html: str) -> dict:
    """{'umowa', 'taryfa', 'faktura'} z listy <dl class="dl-horizontal"> strony głównej;
    pole, którego brak na stronie, nie trafia do wyniku."""
    out: dict = {}
    for block in _DL_HORIZONTAL_RE.findall(html or ''):
        for label, value in _dt_dd_pairs(block):
            key = label.casefold()
            if key in ('umowa', 'taryfa', 'faktura') and value and key not in out:
                out[key] = value
    return out


def parse_point_details(html: str) -> dict:
    """{'taryfa', 'moc_umowna', 'rozliczenie', 'osd'} ze strony punktu poboru
    (etykiety Taryfa, Moc umowna, Sposób rozliczenia, Nazwa OSD); numer licznika i PPE pomijamy."""
    out: dict = {}
    for label, value in _dt_dd_pairs(html):
        key = _POINT_LABELS.get(label.casefold())
        if key and value and key not in out:
            out[key] = value
    return out


def _parse_date(raw) -> Optional[date]:
    """"DD.MM.YYYY" or ISO ("YYYY-MM-DD", optionally with time); None for missing/"---"."""
    if not raw or not isinstance(raw, str) or raw.strip() in ('', '---'):
        return None
    text = raw.strip()
    for fmt, chunk in (('%d.%m.%Y', text[:10]), ('%Y-%m-%d', text[:10])):
        try:
            return datetime.strptime(chunk, fmt).date()
        except ValueError:
            continue
    return None


def _to_float(raw) -> Optional[float]:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(str(raw).replace(' ', '').replace('\xa0', '').replace(',', '.'))
    except ValueError:
        return None


def _is_paid_status(status: str) -> bool:
    """Porównanie bez wielkości liter i otaczających spacji (słownik portalu jest nieznany)."""
    return status.strip().casefold() in _PAID_FOLDED


def parse_documents_json(data: dict) -> list:
    """Map the GetDocuments payload to EbokDocument objects (rows without a signature are skipped).
    Statuses outside PAID_STATUSES are logged once, to learn the portal's vocabulary."""
    docs = []
    unknown: set = set()
    for row in (data or {}).get('Documents') or []:
        sig = (row.get('DocSignature') or '').strip()
        if not sig:
            continue
        status = (row.get('Status') or '').strip()
        if not _is_paid_status(status):
            unknown.add(status)
        issued = _parse_date(row.get('IssueDateFormatted')) or _parse_date(row.get('IssueDate'))
        url = row.get('Url') or None
        if url and not url.startswith('http'):
            url = SERVICE + (url if url.startswith('/') else '/' + url)
        if url and not url.startswith(SERVICE + '/'):
            logger.warning('eBOK: %s — adres PDF spoza %s, pomijam', sig, SERVICE)
            url = None  # ciasteczka sesji tylko do własnego hosta
        docs.append(EbokDocument(
            signature=sig,
            doc_name=(row.get('DocKindValue') or '').strip(),
            issued_on=issued.strftime('%d.%m.%Y') if issued else '',
            amount_gross_pln=_to_float(row.get('AmountGross')),
            due_date=_parse_date(row.get('PaymentDate')),
            paid=_is_paid_status(status),
            pdf_url=url,
            status=status,
            doc_type=(row.get('DocType') or '').strip(),
        ))
    for status in sorted(unknown):
        logger.info('eBOK: status dokumentu poza PAID_STATUSES: %r', status)
    return docs


def parse_payments_json(data) -> list:
    """Map the GetPayments payload to EbokPayment objects. Tolerant: anything malformed is
    skipped or left None, never raises. Types outside PAYMENT_TYPES are logged once."""
    payments = []
    unknown: set = set()
    rows = data.get('Documents') if isinstance(data, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        ptype = row.get('Type')
        ptype = ptype.strip() if isinstance(ptype, str) else ''
        if ptype not in PAYMENT_TYPES:
            unknown.add(ptype)
        settlements = []
        details = row.get('Details')
        for det in details if isinstance(details, list) else []:
            if not isinstance(det, dict):
                continue
            sig = det.get('DocSignature')
            sig = sig.strip() if isinstance(sig, str) else ''
            if sig:
                settlements.append((sig, _to_float(det.get('PaymentAmount')),
                                    _to_float(det.get('PaymentSettlementAmount'))))
        doc_number = row.get('DocNumber')
        payments.append(EbokPayment(
            date=_parse_date(row.get('Date')),
            amount=_to_float(row.get('Amount')),
            type=ptype,
            doc_number=doc_number.strip() if isinstance(doc_number, str) else '',
            settlements=settlements,
        ))
    for ptype in sorted(unknown):
        logger.info('eBOK: typ wpłaty poza PAYMENT_TYPES: %r', ptype)
    return payments


def normalize_signature(text: str) -> str:
    """'T/K1/0158318/24', 'T-K1-0158318-24' i 'T_K1_0158318_24' → ta sama forma."""
    return re.sub(r'[^A-Za-z0-9]+', '_', text or '').strip('_')


def known_signatures(records) -> set:
    """Znormalizowane sygnatury już zaimportowanych dokumentów: z `invoice_number` ORAZ z
    nazwy pliku (`ebok_<sygnatura>.pdf` albo ręcznie wgrane `T_K1_..._24.pdf`). Starsze
    układy PDF nie dają numeru, więc same numery nie wystarczały do dedupu (19.10.2026:
    dokumenty z lat 2023–25 wracały do importu przy każdym pełnym CSV)."""
    known = set()
    for rec in records:
        if rec.get('invoice_number'):
            known.add(normalize_signature(rec['invoice_number']))
        name = rec.get('filename') or ''
        if name.lower().endswith('.pdf'):
            stem = name[:-len('.pdf')]
            known.add(normalize_signature(stem[len('ebok_'):] if stem.startswith('ebok_') else stem))
    return known


def is_known(signature: str, known: set) -> bool:
    return normalize_signature(signature) in known


def _mask(text: str, payer_id: str) -> str:
    """Wycina numer płatnika i adresy e-mail z fragmentu diagnostycznego."""
    if payer_id:
        text = text.replace(str(payer_id), '<płatnik>')
    return re.sub(r'[\w.+-]+@[\w-]+\.[\w.]+', '<email>', re.sub(r'\s+', ' ', text))

