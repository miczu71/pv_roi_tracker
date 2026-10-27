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
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Optional
from urllib.parse import urlencode

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
        POST logowania nigdy nie jest ponawiany, bo powtórki zbliżają konto do
        blokady Taurona."""
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
        r = self._get_retrying(LOGIN_URL, params={'service': SERVICE},
                               headers={'User-Agent': USER_AGENT}, timeout=REQUEST_TIMEOUT)
        m = re.search(r'<form[^>]+id="kc-form-login"[^>]+action="([^"]+)"', r.text)
        if not m:
            raise EbokLayoutChanged('Nie znaleziono formularza logowania (kc-form-login)')
        action = m.group(1).replace('&amp;', '&')
        payload = {'username': self._username, 'password': self._password, 'credentialId': ''}
        r2 = self._session.post(action, data=payload, headers={'User-Agent': USER_AGENT},
                                 timeout=REQUEST_TIMEOUT, allow_redirects=True)
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
            if not batch or not isinstance(total, int) or len(docs) >= total:
                break
        else:
            logger.warning('eBOK: lista dokumentów obcięta do %d stron (%d dok.)', MAX_PAGES, len(docs))
        return docs

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


def parse_client_checksum(html: str, payer_id: str) -> Optional[str]:
    """Checksum (64 hex) of the element carrying `data-client-number="<payer_id>"`
    on the documents archive page; None when the payer is not listed."""
    for tag in _CLIENT_DIV_RE.findall(html or ''):
        number = re.search(r'data-client-number="([^"]*)"', tag)
        checksum = re.search(r'data-client-checksum="([0-9a-fA-F]+)"', tag)
        if number and checksum and number.group(1).strip() == str(payer_id):
            return checksum.group(1)
    return None


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
        if status not in PAID_STATUSES:
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
            paid=status in PAID_STATUSES,
            pdf_url=url,
            status=status,
            doc_type=(row.get('DocType') or '').strip(),
        ))
    for status in sorted(unknown):
        logger.info('eBOK: status dokumentu poza PAID_STATUSES: %r', status)
    return docs


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

