"""Wycena autokonsumpcji PV godzina po godzinie ceną strefy taryfy tej godziny.

Powód: wycena miesięcznej autokonsumpcji średnią ceną importu (live_reader) spadała w nocy
(tani import obniża średnią) i rosła wieczorem bez produkcji. Tu każda godzina dokłada
kWh × cena swojej strefy, więc suma w miesiącu nigdy nie maleje."""
from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def zone_priced_self_consumption(
    produced_h: dict,
    exported_h: dict,
    price_at: Callable[[str], Optional[float]],
) -> Optional[tuple]:
    """(kWh, PLN) autokonsumpcji z godzin; None gdy brak danych godzinowych lub ceny.

    produced_h/exported_h: {'YYYY-MM-DDTHH': kWh}. Brak godziny w exported_h = eksport 0.
    price_at(hour_key) -> cena brutto PLN/kWh albo None."""
    if not produced_h or not exported_h:
        return None
    kwh = 0.0
    pln = 0.0
    for hour in sorted(produced_h):
        sc = max(0.0, produced_h[hour] - exported_h.get(hour, 0.0))
        if sc <= 0.0:
            continue
        price = price_at(hour)
        if price is None:
            logger.warning('zone_priced_self_consumption: brak ceny dla godziny %s — fallback na średnią miesięczną', hour)
            return None
        kwh += sc
        pln += sc * price
    return round(kwh, 3), round(pln, 2)
