"""
Encje cen dynamicznych (`dyn_a_price` / `dyn_b_price`) — czysta logika bez I/O.

Cena brutto bieżącej godziny i harmonogram 48 h liczone TĄ SAMĄ funkcją co karta symulacji
(`tariff_sim.hourly_series`), bez własnego wzoru. Wywołujący dostarcza katalog, RDN, Kompas i taryfę bazową.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from . import tariff_catalog, tariff_sim
from .tariff_module import feed_changed

DYN_TARIFFS = ('dyn_a', 'dyn_b')
SCHEDULE_HOURS = 48


def _confidence(tariff: str, row: Optional[dict], assumed_from: Optional[str] = None) -> str:
    """Pewność ceny godziny: brak ceny = 'unavailable'; godzina po końcu katalogu = 'assumed';
    dyn_a z RDN sprzed tygodnia = 'rdn_fallback_7d'."""
    if row is None or row['gross_per_kwh'] is None:
        return 'unavailable'
    if assumed_from and row['key'][:10] >= assumed_from:
        return 'assumed'
    if tariff == 'dyn_b':
        return 'kompas'
    return 'rdn_fallback_7d' if row['fallback'] else 'rdn'


def _payload(tariff: str, entry: dict, keys: list, now: datetime,
             assumed_from: Optional[str] = None) -> dict:
    """Payload jednej encji: `{price, attributes}`; `keys` = 48 kolejnych godzin od bieżącej."""
    rows = {r['key']: r for r in entry['hours']} if entry['available'] else {}
    cur = rows.get(keys[0])
    gross = cur['gross_per_kwh'] if cur else None
    net = cur['net_per_kwh'] if cur else None
    price = round(gross, 4) if gross is not None else None
    schedule = []
    for k in (keys if entry['available'] else []):
        r = rows.get(k)
        g = r['gross_per_kwh'] if r else None
        n = r['net_per_kwh'] if r else None
        schedule.append({
            'hour': k,
            'zone': r['zone'] if r else None,
            'gross': round(g, 5) if g is not None else None,
            'net': round(n, 5) if n is not None else None,
            'confidence': _confidence(tariff, r, assumed_from),
        })
    return {
        'price': price,
        'attributes': {
            'tariff': tariff,
            'zone': cur['zone'] if cur else None,
            'gross_per_kwh': price,
            'net_per_kwh': round(net, 5) if net is not None else None,
            'components': cur['components'] if cur else None,
            'components_basis': 'net',   # składniki netto; `gross_per_kwh` = netto × VAT
            'confidence': _confidence(tariff, cur, assumed_from),
            'generated_at': now.isoformat(timespec='seconds'),
            'schedule': schedule,
        },
    }


def inputs_with_ahead(cache, now: datetime, fetch_ahead) -> tuple:
    """`(rdn, kompas, problemy)`: cache + RDN/Kompas dnia bieżącego i jutrzejszego z `fetch_ahead`.

    `PriceCache` nie przechowuje dnia bieżącego ani przyszłych (ceny nie są ostateczne), więc bez
    tego cena „teraz” byłaby zawsze fallbackiem −7 dni. Bez zapisu na dysk. Brak RDN na dziś trafia
    do `problemy` (jutro bywa nieopublikowane do ok. 14:00 — to nie problem)."""
    rdn, kompas = dict(cache.rdn), dict(cache.kompas)
    problems: list = []
    for i in (0, 1):
        res = fetch_ahead(now.date() + timedelta(days=i), cache)
        rdn.update(res['rdn'])
        kompas.update(res['kompas'])
        if i == 0 and not res['rdn_available']:
            problems.append(res['reason'])
    return rdn, kompas, problems


def _extend_catalog(catalog: dict, first: date, last: date) -> tuple:
    """`(katalog, assumed_from)`: gdy zakres wykracza poza koniec katalogu, kopia katalogu z dodatkowym
    okresem (ostatni okres cennika, `period_for_assumed`) do `last`; `assumed_from` = pierwszy dzień
    przyjęty (ISO) albo None. `tariff_sim` zostaje bez zmian (symulacja kosztów kończy się na katalogu)."""
    end = date.fromisoformat(tariff_catalog.catalog_end(catalog))
    if last <= end:
        return catalog, None
    start = max(first, end + timedelta(days=1))
    period, _ = tariff_catalog.period_for_assumed(catalog, start)
    period = {**period, 'valid_from': start.isoformat(), 'valid_to': last.isoformat()}
    return {**catalog, 'periods': [*catalog['periods'], period]}, start.isoformat()


def dynamic_feed_payload(catalog: dict, rdn: Optional[dict], kompas: Optional[dict],
                         overrides: Optional[dict], baseline: str, now: datetime) -> dict:
    """`{'dyn_a': payload, 'dyn_b': payload}` — cena godziny `now` (obciętej do pełnej) i 48 h harmonogramu."""
    hour = now.replace(minute=0, second=0, microsecond=0)
    keys = [(hour + timedelta(hours=i)).strftime('%Y-%m-%dT%H') for i in range(SCHEDULE_HOURS)]
    day0 = hour.date()
    day_n = (hour + timedelta(hours=SCHEDULE_HOURS - 1)).date()
    catalog, assumed_from = _extend_catalog(catalog, day0, day_n)
    series = tariff_sim.hourly_series(day0, day_n, {}, catalog,
                                      tariffs=DYN_TARIFFS, settings=None, rdn=rdn, kompas=kompas,
                                      overrides=overrides, dist_tariff=baseline)
    return {n: _payload(n, series[n], keys, now, assumed_from) for n in DYN_TARIFFS}


def unavailable_payloads(now: datetime) -> dict:
    """Payloady „brak ceny” (`price None`, confidence 'unavailable') — po błędzie wyceny, by HA nie
    trzymał ostatniej retained ceny jako aktualnej."""
    empty = {'hours': [], 'available': False}
    keys = [now.replace(minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H')]
    return {n: _payload(n, empty, keys, now) for n in DYN_TARIFFS}


def dynamic_changed(payloads: dict, last: Optional[dict]) -> bool:
    """Czy payloady różnią się od ostatnio opublikowanych (poza `generated_at`); brak `last` = tak."""
    return any(feed_changed(payloads[n], (last or {}).get(n)) for n in DYN_TARIFFS)


def tariff_dynamic_step(pub, last: dict, inputs: dict, now: datetime) -> tuple:
    """Jeden krok publikacji encji dynamicznych. `last` = `{'payload', 'ok'}`.

    Zwraca `(wynik, nowe_last)`: True = opublikowano, False = publikacja nieudana (rozłączony MQTT),
    None = pominięto (bez zmian i poprzednia udana). Payload zapamiętywany dopiero po udanej publikacji.
    Wyjątki z `hourly_series` (ValueError z katalogu) nie są tu łapane."""
    payloads = dynamic_feed_payload(inputs['catalog'], inputs['rdn'], inputs['kompas'],
                                    inputs.get('overrides'), inputs['baseline'], now)
    if last['ok'] and not dynamic_changed(payloads, last['payload']):
        return None, last
    ok = bool(pub.publish_tariff_dynamic(payloads))
    return ok, {'payload': payloads if ok else last['payload'], 'ok': ok}
