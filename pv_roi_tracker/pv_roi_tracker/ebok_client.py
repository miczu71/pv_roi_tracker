"""
Client for TAURON eBOK (ebok.tauron.pl) — logs in via Keycloak, lists invoice
documents for a payer, and downloads their PDFs.

Findings from the Etap 1 spike (docs/ROADMAP_EBOK_IMPORT.md):
  • Login: Keycloak `kc-form-login` — same flow as PiotrMachowski/Tauron-AMIplus
    connector.py, no captcha/OTP observed.
  • Client selection: GET /wyborKlienta/id/<client_id> (payer → client id mapping
    is read from /wyborKlienta itself, see `find_client_id`).
  • Listing: the CSV export (/content/platnosci/csv/dataOd/.../dataDo/.../type/
    archiwumFaktur) returns the FULL matching history in one request, windows-1250
    encoded, but has no numeric id — only the HTML archive page maps
    SYGNATURA → numeric id (used to download the PDF), max 50 rows/page,
    paginated as /content/platnosci/archiwumFaktur/page/<n>/display/50/...
  • PDF: GET /podgladFaktury/id/<numeric_id> returns the PDF bytes directly.

Kept dependency-free (only `requests`, already in requirements.txt). All network
calls go through `self._session` (a requests.Session, or any object exposing a
compatible `.get`/`.post`), injected in the constructor so tests can substitute
a fake session without an extra mocking library. Parsing helpers are plain
functions with no I/O, testable directly on captured HTML/CSV fixtures.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

MAX_ARCHIVE_ONLY = 5   # bezpiecznik: tyle dokumentów naraz importujemy tylko z archiwum HTML
LOGIN_URL = 'https://logowanie.tauron.pl/realms/ext/protocol/cas/login'
SERVICE = 'https://ebok.tauron.pl'
USER_AGENT = 'Mozilla/5.0 (X11; Linux x86_64) pv_roi_tracker/ebok-sync'
REQUEST_TIMEOUT = 45
ARCHIVE_TIMEOUT = 60
MAX_ARCHIVE_PAGES = 20  # safety cap — well above what 100s of invoices would need at 50/page


class EbokError(Exception):
    """Base class for eBOK client errors."""


class EbokLoginFailed(EbokError):
    """Wrong username/password."""


class EbokLoginBlocked(EbokError):
    """Tauron temporarily blocked further login attempts for this account."""


class EbokLayoutChanged(EbokError):
    """Expected page structure (login form, CSV columns, archive rows, ...) not found —
    Tauron likely changed their frontend."""


@dataclass
class EbokDocument:
    """One row from the eBOK invoice archive CSV."""
    signature: str                          # SYGNATURA, e.g. "T/K1/BN567872/0017/26"
    doc_name: str                           # NAZWA DOKUMENTU, e.g. "Faktura rzeczywista"
    issued_on: str                          # DATA WYSTAWIENIA, "DD.MM.YYYY"
    amount_gross_pln: Optional[float]
    due_date: Optional[date] = None         # TERMIN PŁATNOŚCI — None for docs without one (e.g. noty)
    paid: bool = False                      # ZAPŁACONA — only "zapłacona" counts as paid
    numeric_id: Optional[str] = None        # filled in later by locate_ids()


class EbokClient:
    """One instance per sync run (or kept alive for the add-on's process lifetime —
    login() is a no-op once already logged in, so reuse across calls is free and
    avoids the disk-cookie-cache pitfall found in the Etap 1 spike: flattening
    `session.cookies` to a plain dict drops cookie-domain scoping and causes a
    redirect loop.  Persisting a session across add-on restarts is not needed —
    a single fresh login is cheap and the sync job runs at most a few times a day.
    """

    def __init__(self, username: str, password: str,
                 session: Optional[requests.Session] = None):
        self._username = username
        self._password = password
        self._session = session or requests.Session()
        self._logged_in = False

    # ── login ────────────────────────────────────────────────────────────────

    def login(self) -> None:
        """Log in once; safe to call again (no-op) once already logged in."""
        if self._logged_in:
            return
        r = self._session.get(LOGIN_URL, params={'service': SERVICE},
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
        with the login page at status 200 (incident 2026-10-01: /wyborKlienta
        without client rows → misleading "Płatnik nie znaleziony"). On a login
        page: log in again and retry once; still a login page → EbokError."""
        self.login()
        r = self._session.get(url, headers={'User-Agent': USER_AGENT}, timeout=timeout)
        if not _is_login_page(r):
            return r
        logger.info('eBOK: sesja wygasła — logowanie ponownie')
        self._logged_in = False
        self.login()
        r = self._session.get(url, headers={'User-Agent': USER_AGENT}, timeout=timeout)
        if _is_login_page(r):
            raise EbokError('Sesja eBOK nie utrzymała się po ponownym logowaniu '
                            '(serwer nadal zwraca stronę logowania)')
        return r

    def select_client(self, client_id: str) -> None:
        r = self._get(f'{SERVICE}/wyborKlienta/id/{client_id}', REQUEST_TIMEOUT)
        if r.status_code != 200:
            raise EbokError(f'Wybór klienta {client_id} nie powiódł się (status {r.status_code})')

    def find_client_id(self, payer_id: str) -> Optional[str]:
        """Look up the internal client id for a given payer number (SYGNATURA-style
        'Nr płatnika', e.g. '60567872') from the /wyborKlienta landing page."""
        r = self._get(f'{SERVICE}/wyborKlienta', REQUEST_TIMEOUT)
        if r.status_code != 200:
            raise EbokError(f'Pobranie /wyborKlienta nie powiodło się (status {r.status_code})')
        return parse_client_id_for_payer(r.text, payer_id)

    # ── listing ──────────────────────────────────────────────────────────────

    def list_documents(self, date_from: date, date_to: date) -> list[EbokDocument]:
        """Full invoice history for the currently-selected client via the CSV
        export — one request, no pagination needed."""
        url = (f'{SERVICE}/content/platnosci/csv/dataOd/{date_from.isoformat()}'
               f'/dataDo/{date_to.isoformat()}/type/archiwumFaktur')
        r = self._get(url, ARCHIVE_TIMEOUT)
        if r.status_code != 200:
            raise EbokError(f'Pobranie CSV faktur nie powiodło się (status {r.status_code})')
        docs = parse_documents_csv(r.content)
        if r.content and not docs:
            raise EbokLayoutChanged('CSV faktur nie zawiera rozpoznawalnych wierszy — sprawdź format')
        return docs

    def archive_page_ids(self, date_from: date, date_to: date) -> dict:
        """SYGNATURA → numeric id for the first (newest) archive HTML page. Second
        source next to the CSV: on 2026-10-18 the CSV export returned 8 rows
        (77 in the spike) and omitted a freshly issued invoice that the archive
        page listed."""
        url = (f'{SERVICE}/content/platnosci/archiwumFaktur/page/1/display/50'
               f'/dataOd/{date_from.isoformat()}/dataDo/{date_to.isoformat()}')
        r = self._get(url, ARCHIVE_TIMEOUT)
        if r.status_code != 200:
            raise EbokError(f'Pobranie archiwum (strona 1) nie powiodło się (status {r.status_code})')
        return parse_archive_html(r.text)

    def diagnose(self, needle: str, today: date) -> dict:
        """Read-only probe: czy dokument `needle` (np. "0018/26") jest widoczny w CSV,
        w archiwum HTML (różne zakresy dat) i na innych stronach płatności. Zwraca
        tylko liczby, sygnatury i adresy linków — bez kwot i danych osobowych."""
        def fetch(path: str):
            try:
                r = self._get(f'{SERVICE}{path}', ARCHIVE_TIMEOUT)
                return r.status_code, r
            except Exception as exc:  # timeout itp. — raportujemy, nie przerywamy
                return f'ERR {type(exc).__name__}', None

        tomorrow = today + timedelta(days=1)
        out: dict = {'needle': needle, 'csv': [], 'archive_html': [], 'pages': []}
        for label, d_to in (('dziś+1', tomorrow), ('dziś+30', today + timedelta(days=30))):
            st, r = fetch(f'/content/platnosci/csv/dataOd/2022-01-01/dataDo/{d_to.isoformat()}/type/archiwumFaktur')
            docs = parse_documents_csv(r.content) if r is not None and st == 200 else []
            newest = sorted(docs, key=lambda d: d.issued_on.split('.')[::-1])[-3:]
            out['csv'].append({'dataDo': label, 'status': st, 'rows': len(docs),
                               'has_needle': any(needle in d.signature for d in docs),
                               'newest': [f'{d.issued_on} {d.signature}' for d in newest]})
        for label, d_from in (('365 dni', today - timedelta(days=365)), ('120 dni', today - timedelta(days=120)),
                              ('od 2022', date(2022, 1, 1))):
            st, r = fetch(f'/content/platnosci/archiwumFaktur/page/1/display/50'
                          f'/dataOd/{d_from.isoformat()}/dataDo/{tomorrow.isoformat()}')
            html = r.text if r is not None and st == 200 else ''
            sigs = sorted(parse_archive_html(html))
            out['archive_html'].append({'zakres': label, 'status': st, 'rows': len(sigs),
                                        'has_needle': needle in html, 'newest': sigs[:3]})
        for path in ('/platnosci', '/content/platnosci', '/faktury', '/content/platnosci/faktury',
                     '/content/platnosci/biezace', '/content/platnosci/fakturyDoZaplaty'):
            st, r = fetch(path)
            html = r.text if r is not None and st == 200 else ''
            links = sorted(set(re.findall(r'href="(/[^"#?]*(?:faktur|platnosc|rozlicz)[^"#?]*)"', html, re.I)))[:12]
            out['pages'].append({'path': path, 'status': st, 'has_needle': needle in html, 'links': links})
        return out

    def locate_ids(self, signatures: set, date_from: date, date_to: date) -> dict:
        """Map SYGNATURA → numeric podgladFaktury id by paginating the HTML
        archive page. Stops early once every requested signature is found."""
        found: dict = {}
        remaining = set(signatures)
        page = 1
        while remaining and page <= MAX_ARCHIVE_PAGES:
            url = (f'{SERVICE}/content/platnosci/archiwumFaktur/page/{page}/display/50'
                   f'/dataOd/{date_from.isoformat()}/dataDo/{date_to.isoformat()}')
            r = self._get(url, ARCHIVE_TIMEOUT)
            if r.status_code != 200:
                raise EbokError(f'Pobranie archiwum (strona {page}) nie powiodło się '
                                 f'(status {r.status_code})')
            page_map = parse_archive_html(r.text)
            if not page_map:
                break  # no more pages
            for sig, doc_id in page_map.items():
                if sig in remaining:
                    found[sig] = doc_id
                    remaining.discard(sig)
            page += 1
        return found

    def download_pdf(self, numeric_id: str) -> bytes:
        r = self._get(f'{SERVICE}/podgladFaktury/id/{numeric_id}', REQUEST_TIMEOUT)
        if r.status_code != 200 or not r.content.startswith(b'%PDF'):
            raise EbokError(f'Pobranie PDF id={numeric_id} nie powiodło się '
                             f'(status {r.status_code})')
        return r.content


def _is_login_page(r) -> bool:
    return b'id="kc-form-login"' in (r.content or b'')


# ── pure parsing helpers (no I/O — unit-testable on captured fixtures) ─────────

def parse_documents_csv(raw: bytes) -> list:
    """Parse the eBOK 'archiwumFaktur' CSV export (windows-1250, ';'-delimited)."""
    text = raw.decode('windows-1250', errors='replace')
    reader = csv.DictReader(io.StringIO(text), delimiter=';')
    docs = []
    for row in reader:
        sig = (row.get('SYGNATURA') or '').strip()
        if not sig:
            continue
        amount_raw = (row.get('KWOTA BRUTTO') or '').strip()
        amount = None
        if amount_raw and amount_raw != '---':
            try:
                amount = float(amount_raw.replace(' ', '').replace('\xa0', '').replace(',', '.'))
            except ValueError:
                amount = None
        docs.append(EbokDocument(
            signature=sig,
            doc_name=(row.get('NAZWA DOKUMENTU') or '').strip(),
            issued_on=(row.get('DATA WYSTAWIENIA') or '').strip(),
            amount_gross_pln=amount,
            due_date=_parse_ddmmyyyy((row.get('TERMIN PŁATNOŚCI') or '').strip()),
            paid=(row.get('ZAPŁACONA') or '').strip() == 'zapłacona',
        ))
    return docs


def _parse_ddmmyyyy(raw: str) -> Optional[date]:
    """Parse a "DD.MM.YYYY" cell; None for missing/placeholder ("---") values."""
    if not raw or raw == '---':
        return None
    try:
        return datetime.strptime(raw, '%d.%m.%Y').date()
    except ValueError:
        return None


def known_signatures(records) -> set:
    """Sygnatury już zaimportowane: z `invoice_number` rekordu ORAZ z nazwy pliku
    `ebok_<sygnatura z '/' → '-'>.pdf` — starsze układy PDF nie dają numeru, więc same
    numery nie wystarczały do dedupu (19.10.2026: 32 dokumenty z lat 2023–25 wracały
    do importu przy każdym pełnym CSV)."""
    known = set()
    for rec in records:
        if rec.get('invoice_number'):
            known.add(rec['invoice_number'])
        name = rec.get('filename') or ''
        if name.startswith('ebok_') and name.endswith('.pdf'):
            known.add(name[len('ebok_'):-len('.pdf')])      # forma z '-' zamiast '/'
    return known


def is_known(signature: str, known: set) -> bool:
    return signature in known or signature.replace('/', '-') in known


def find_missing_signatures(csv_signatures: set, archive_ids: dict, known: set) -> tuple:
    """(missing, extra): signatures to import — from the CSV or the archive page —
    and the subset seen only on the archive page (CSV gap)."""
    extra = {sig.strip() for sig in archive_ids
             if sig not in csv_signatures and not is_known(sig.strip(), known)}
    if len(extra) > MAX_ARCHIVE_ONLY:
        # zbyt wiele naraz = raczej niezgodność formatu sygnatur niż nowe faktury
        logger.warning('eBOK: %d dokumentów tylko w archiwum (limit %d) — pomijam',
                       len(extra), MAX_ARCHIVE_ONLY)
        extra = set()
    missing = {sig for sig in csv_signatures if not is_known(sig, known)} | extra
    return missing, extra


_ARCHIVE_ROW_RE = re.compile(
    r'href="/podgladFaktury/id/(\d+)"\s*class="invoicePreviewHelper[^"]*helperLink"[^>]*>'
    r'(?:<i[^>]*></i>)?([^<]+)</a>'
)


def parse_archive_html(html: str) -> dict:
    """Map SYGNATURA → numeric id from one archiwumFaktur HTML page."""
    return {sig.strip(): doc_id for doc_id, sig in _ARCHIVE_ROW_RE.findall(html)}


def parse_client_id_for_payer(html: str, payer_id: str) -> Optional[str]:
    """Find the internal client id for a given 'Nr płatnika' on the
    /wyborKlienta landing page (one <tr> per contract/payer)."""
    for row in html.split('<tr>'):
        m = re.search(r'<label>\s*[^<]*<br\s*/>\s*(\d+)\s*</label>', row)
        idm = re.search(r'/wyborKlienta/id/(\d+)', row)
        if m and idm and m.group(1) == str(payer_id):
            return idm.group(1)
    return None
