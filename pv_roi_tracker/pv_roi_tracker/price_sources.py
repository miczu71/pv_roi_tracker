"""
Źródła cen do symulacji taryf dynamicznych (tylko dane historyczne, bez kluczy API).

  • RDN (TGE Fixing I) — Energy-Charts API, bzn=PL, EUR/MWh (godzinowe do 2025-09,
    później 15-min → średnia godzinowa), przeliczane na PLN/MWh kursem NBP
  • kurs EUR/PLN — NBP tabela A (weekendy/święta: ostatni kurs)
  • strefy S1–S4 (Energetyczny Kompas) — PSE `pdgsz`, `usage_fcst` 0..3

Klucze godzin: `YYYY-MM-DDTHH` w czasie lokalnym Europe/Warsaw. Funkcje sieciowe
przyjmują `http_get(url) -> dict` (testy podają atrapę).
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from .battery_store import _atomic_write

logger = logging.getLogger(__name__)

_TZ = ZoneInfo('Europe/Warsaw')
_TIMEOUT = 40
_NBP_MAX_DAYS = 93
_NBP_LOOKBACK_DAYS = 10     # zapas na weekend/święta przed początkiem zakresu
_ENERGY_CHARTS_URL = 'https://api.energy-charts.info/price?bzn=PL&start={a}&end={b}'
_NBP_URL = 'https://api.nbp.pl/api/exchangerates/rates/a/eur/{a}/{b}/?format=json'
_PSE_URL = 'https://api.raporty.pse.pl/api/pdgsz'
_ZONES = {0: 'S1', 1: 'S2', 2: 'S3', 3: 'S4'}

HttpGet = Callable[[str], dict]


def _default_http_get(url: str) -> dict:
    req = urllib.request.Request(url, headers={'User-Agent': 'pv-roi-tracker', 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode('utf-8'))


def _d(s: str) -> date:
    return date.fromisoformat(s)


def _days(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


# ── NBP ──────────────────────────────────────────────────────────────────────

def fetch_eurpln(date_from: str, date_to: str, http_get: Optional[HttpGet] = None) -> dict:
    """`{YYYY-MM-DD: kurs}` dla każdego dnia zakresu; dni bez tabeli dostają ostatni kurs."""
    get = http_get or _default_http_get
    start, end = _d(date_from), _d(date_to)
    fetch_start = start - timedelta(days=_NBP_LOOKBACK_DAYS)
    rates: dict = {}
    chunk_start = fetch_start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=_NBP_MAX_DAYS - 1), end)
        data = get(_NBP_URL.format(a=chunk_start.isoformat(), b=chunk_end.isoformat()))
        for row in data.get('rates', []):
            rates[row['effectiveDate']] = float(row['mid'])
        chunk_start = chunk_end + timedelta(days=1)
    out: dict = {}
    last = None
    for d in _days(fetch_start, end):
        iso = d.isoformat()
        if iso in rates:
            last = rates[iso]
        if last is not None and d >= start:
            out[iso] = last
    return out


# ── Energy-Charts (RDN) ──────────────────────────────────────────────────────

def fetch_rdn_hourly(date_from: str, date_to: str, http_get: Optional[HttpGet] = None,
                     eurpln: Optional[dict] = None) -> dict:
    """`{YYYY-MM-DDTHH: PLN/MWh}` w czasie lokalnym; 15-min uśrednione do godziny."""
    get = http_get or _default_http_get
    start, end = _d(date_from), _d(date_to)
    rates = eurpln if eurpln is not None else fetch_eurpln(date_from, date_to, get)
    # zapas ±1 dzień: granice doby lokalnej nie pokrywają się z dobą UTC
    url = _ENERGY_CHARTS_URL.format(a=(start - timedelta(days=1)).isoformat(),
                                    b=(end + timedelta(days=1)).isoformat())
    data = get(url)
    buckets: dict = {}
    for ts, price in zip(data.get('unix_seconds', []), data.get('price', [])):
        if price is None:
            continue
        local = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(_TZ)
        if not (start <= local.date() <= end):
            continue
        buckets.setdefault(local.strftime('%Y-%m-%dT%H'), []).append(float(price))
    out: dict = {}
    for key, vals in buckets.items():
        rate = rates.get(key[:10])
        if rate is None:
            logger.warning('price_sources: brak kursu EUR/PLN dla %s, pomijam %s', key[:10], key)
            continue
        out[key] = sum(vals) / len(vals) * rate
    return out


# ── PSE Kompas (strefy S1–S4) ────────────────────────────────────────────────

def fetch_kompas_hourly(date_from: str, date_to: str, http_get: Optional[HttpGet] = None) -> dict:
    """`{YYYY-MM-DDTHH: 'S1'..'S4'}` z PSE pdgsz (tylko wiersze is_active)."""
    get = http_get or _default_http_get
    flt = f"business_date ge '{date_from}' and business_date le '{date_to}' and is_active eq true"
    url = _PSE_URL + '?' + urllib.parse.urlencode({'$filter': flt}, quote_via=urllib.parse.quote)
    out: dict = {}
    seen = set()
    while url and url not in seen:
        seen.add(url)
        data = get(url)
        for row in data.get('value', []):
            active = row.get('is_active')
            if str(active).lower() not in ('true', '1'):
                continue
            zone = _ZONES.get(row.get('usage_fcst'))
            dtime = str(row.get('dtime', ''))
            if zone is None or len(dtime) < 13:
                continue
            out[f'{dtime[:10]}T{dtime[11:13]}'] = zone
        url = data.get('nextLink') or data.get('@odata.nextLink')
    return out


# ── cache ────────────────────────────────────────────────────────────────────

class PriceCache:
    """Cache JSON `{rdn:{}, kompas:{}, eurpln:{}}`; ceny minionych dni są ostateczne."""

    def __init__(self, path):
        self.path = Path(path)
        self.rdn: dict = {}
        self.kompas: dict = {}
        self.eurpln: dict = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception:
            logger.warning('price_sources: cache %s nieczytelny, zaczynam od pustego', self.path)
            return
        self.rdn = dict(data.get('rdn', {}))
        self.kompas = dict(data.get('kompas', {}))
        self.eurpln = dict(data.get('eurpln', {}))

    def save(self) -> None:
        try:
            _atomic_write(self.path, {'rdn': self.rdn, 'kompas': self.kompas,
                                      'eurpln': self.eurpln})
        except Exception:
            logger.exception('price_sources: zapis cache %s nie powiódł się', self.path)

    @staticmethod
    def _missing(days: list, present: set) -> Optional[tuple]:
        miss = [d for d in days if d.isoformat() not in present]
        return (miss[0].isoformat(), miss[-1].isoformat()) if miss else None

    def ensure(self, date_from: str, date_to: str, http_get: Optional[HttpGet] = None,
               today: Optional[date] = None) -> None:
        """Dociąga brakujące dni < dziś; dzień bieżący nie jest cache'owany. Błąd sieci → warning."""
        today = today or date.today()
        last = min(_d(date_to), today - timedelta(days=1))
        days = list(_days(_d(date_from), last))
        if not days:
            return
        have_rdn = {k[:10] for k in self.rdn}
        have_kompas = {k[:10] for k in self.kompas}

        rng = self._missing(days, set(self.eurpln))
        if rng:
            try:
                self.eurpln.update(fetch_eurpln(*rng, http_get=http_get))
            except Exception as e:
                logger.warning('price_sources: kurs EUR/PLN %s..%s niedostępny: %s', *rng, e)
        rng = self._missing(days, have_rdn)
        if rng:
            try:
                self.rdn.update(fetch_rdn_hourly(*rng, http_get=http_get, eurpln=self.eurpln))
            except Exception as e:
                logger.warning('price_sources: RDN %s..%s niedostępne: %s', *rng, e)
        rng = self._missing(days, have_kompas)
        if rng:
            try:
                self.kompas.update(fetch_kompas_hourly(*rng, http_get=http_get))
            except Exception as e:
                logger.warning('price_sources: Kompas %s..%s niedostępny: %s', *rng, e)
        self.save()


def fetch_ahead(day: date, cache: 'PriceCache', http_get: Optional[HttpGet] = None) -> dict:
    """Ceny RDN i strefy Kompasu dla dnia bieżącego/przyszłego — BEZ zapisu do cache na dysku.

    Ceny dnia bieżącego i przyszłych nie są ostateczne, więc nie trafiają do `PriceCache`.
    Kurs: `cache.eurpln[day]`, w razie braku ostatni znany kurs (`eurpln_fallback: True`);
    bez żadnego kursu `rdn_available: False`. Błąd sieci / pusta odpowiedź RDN → `rdn_available:
    False` z treścią błędu w `reason`. Kompas pobierany osobno — jego brak nie blokuje RDN.
    """
    iso = day.isoformat()
    out = {'rdn': {}, 'kompas': {}, 'eurpln_fallback': False, 'eurpln_used': None,
           'rdn_available': False, 'reason': None}

    try:
        out['kompas'] = fetch_kompas_hourly(iso, iso, http_get=http_get)
    except Exception as e:
        logger.warning('price_sources: Kompas %s niedostępny: %s', iso, e)

    rate = cache.eurpln.get(iso)
    if rate is None and cache.eurpln:
        rate = cache.eurpln[max(cache.eurpln)]
        out['eurpln_fallback'] = True
    if rate is None:
        out['reason'] = f'brak kursu EUR/PLN w cache dla {iso}'
        return out
    out['eurpln_used'] = float(rate)
    try:
        rdn = fetch_rdn_hourly(iso, iso, http_get=http_get, eurpln={iso: float(rate)})
    except Exception as e:
        out['reason'] = f'RDN {iso} niedostępne: {e}'
        return out
    if not rdn:
        out['reason'] = f'brak notowań RDN dla {iso} (jeszcze nieopublikowane?)'
        return out
    out['rdn'] = rdn
    out['rdn_available'] = True
    return out
