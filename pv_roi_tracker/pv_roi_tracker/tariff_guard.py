"""
Strażnik cenników: raz w tygodniu porównuje zbiór linków na stronach TD i URE z poprzednim przebiegiem.

Nowy link = prawdopodobny nowy dokument (taryfa/decyzja) → komunikat i wpis w `new_documents` (baner w zakładce
Taryfa). Pierwszy przebieg strony tylko zapisuje stan (baseline). Bez parsowania PDF.
Stan: `{'last_run', 'pages': {id: {'links', 'checked', 'error'}}, 'new_documents': [{'page','url','seen'}]}`.
"""
from __future__ import annotations

import copy
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .battery_store import _atomic_write

logger = logging.getLogger(__name__)

PAGES = {
    'td': {'url': 'https://www.tauron-dystrybucja.pl/uslugi-dystrybucyjne/dla-konsumenta',
           'needles': ('/aktualna-taryfa/', '/archiwum-taryf/')},
    'ure': {'url': 'https://bip.ure.gov.pl/bip/taryfy-i-inne-decyzje-b/energia-elektryczna',
            'needles': ('taryf',)},
}
_TIMEOUT_S = 20
_HEADERS = {'User-Agent': 'Mozilla/5.0'}
_MAX_URLS_IN_MSG = 3


def extract_links(html: str, base_url: str, needles) -> list:
    """Absolutne, unikalne, posortowane linki, których `href` zawiera jedną z `needles` (bez wielkości liter)."""
    low = [n.lower() for n in needles]
    out: set = set()
    for a in BeautifulSoup(html or '', 'html.parser').find_all('a', href=True):
        href = a['href'].strip()
        if href and any(n in href.lower() for n in low):
            url = urljoin(base_url, href)
            if url.lower().startswith(('http://', 'https://')):
                out.add(url)
    return sorted(out)


def _http_fetch(url: str) -> str:
    resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT_S)
    if resp.status_code != 200:
        raise requests.HTTPError(f'HTTP {resp.status_code}')
    return resp.text


def run_guard(state: dict, fetch: Optional[Callable] = None, now: Optional[datetime] = None) -> tuple:
    """`(nowy_state, komunikaty)`; nie mutuje `state`. Wyjątki inne niż `requests.RequestException` propagują."""
    fetch = fetch or _http_fetch
    stamp = (now or datetime.now()).isoformat(timespec='seconds')
    new = copy.deepcopy(state or {})
    pages = new.setdefault('pages', {})
    docs = new.setdefault('new_documents', [])
    msgs: list = []
    for pid, spec in PAGES.items():
        prev = pages.get(pid)
        try:
            links = extract_links(fetch(spec['url']), spec['url'], spec['needles'])
        except requests.RequestException as exc:
            logger.warning('Strażnik cenników: %s nie odpowiada (%s)', pid, exc)
            entry = dict(prev) if prev else {'links': []}
            entry.update(checked=stamp, error=str(exc)[:200])
            pages[pid] = entry
            continue
        pages[pid] = {'links': links, 'checked': stamp, 'error': None}
        if not (prev and prev.get('links')):   # brak wpisu albo pusta lista (błąd przy 1. przebiegu) = baseline
            continue
        added = [u for u in links if u not in set(prev.get('links') or [])]
        if not added:
            continue
        known = {d['url'] for d in docs}
        docs.extend({'page': pid, 'url': u, 'seen': stamp} for u in added if u not in known)
        msgs.append(f"Cennik taryf: nowe dokumenty na stronie {pid.upper()}: {len(added)} — "
                    f"{', '.join(added[:_MAX_URLS_IN_MSG])}. Sprawdź i dopisz okres w zakładce Taryfa.")
    new['last_run'] = stamp
    return new, msgs


def acknowledge(state: dict) -> dict:
    """Kopia stanu z wyczyszczonym `new_documents`."""
    new = copy.deepcopy(state or {})
    new['new_documents'] = []
    return new


def load_state(path) -> dict:
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning('Plik stanu strażnika cenników uszkodzony: %s', path)
        return {}
    if not isinstance(data, dict):
        logger.warning('Plik stanu strażnika cenników uszkodzony: %s', path)
        return {}
    return data


def save_state(state: dict, path) -> None:
    _atomic_write(Path(path), state)
