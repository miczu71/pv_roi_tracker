"""
Moduł taryf — jedno miejsce odpowiedzi na pytanie „jaka taryfa, strefa i cena obowiązują o godzinie t”.

Warstwy danych (od najniższej): katalog `tariffs/tauron.json` (dokumenty URE/TD) → ręczne korekty
(`overrides`, wpisy z umowy). Taryfa na dany dzień pochodzi z historii okresów (`tariff_history`),
a bez niej z `default_tariff`. Czysta logika bez I/O; wczytanie plików robi wywołujący.

Reguły:
* dzień po końcu katalogu dostaje OSTATNI okres z pewnością `assumed` (widoczne w UI i w encjach),
* dzień przed początkiem katalogu to `LookupError` — cena nie jest zgadywana wstecz,
* `null` w katalogu nie jest cicho zamieniane na zero (wyjątek z `tariff_catalog`),
* strefy mogą być wersjonowane kluczem katalogu `zones_history` (lista `{valid_from, zones}`).
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from . import tariff_catalog as tc
from . import tariff_history
from .tariff_zones import is_peak_zone

logger = logging.getLogger(__name__)
_legacy_warned: set = set()

DEFAULT_TARIFF = 'G12w'

# pewność ceny: manual (korekta z umowy) > document (okres potwierdzony w dokumencie)
# > unconfirmed (okres w katalogu niepotwierdzony) > assumed (dzień po końcu katalogu)
CONFIDENCES = ('manual', 'invoice', 'document', 'unconfirmed', 'assumed')

# widok dwustrefowy dla odbiorców, którzy znają tylko „szczyt/poza szczytem” (ROI, bateria, pompa ciepła)
_TWO_ZONE_VIEW = {
    'G11': ('all', 'all'),
    'G12': ('day', 'night'),
    'G12w': ('peak', 'offpeak'),
    'G13': ('evening_peak', 'other'),     # świadome uproszczenie: szczyt = najdroższa strefa
}


@dataclass(frozen=True)
class PricePoint:
    hour: str                   # 'YYYY-MM-DDTHH' (czas lokalny)
    tariff: str
    zone: str
    is_peak: bool
    level: str                  # cheap | mid | expensive | flat — pozycja ceny strefy wśród stref taryfy
    components: dict            # składniki netto zł/kWh: energy, dist, quality, oze, cogeneration, excise
    net: float
    gross: float
    period_from: str            # początek okresu cennika, z którego pochodzi cena
    confidence: str
    source: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


class TariffModule:
    def __init__(self, catalog: dict, periods: Optional[list] = None,
                 overrides: Optional[list] = None, default_tariff: str = DEFAULT_TARIFF,
                 settings: Optional[tc.TariffSettings] = None, invoice_ok: Optional[set] = None):
        self.catalog = catalog
        self.periods = periods or []
        self.overrides = overrides or []
        self.default_tariff = default_tariff
        self.settings = settings or tc.TariffSettings()
        self.invoice_ok = set(invoice_ok or ())
        self.vat = float(catalog['_meta']['vat'])
        self._entries = sorted(catalog['periods'], key=lambda p: p['valid_from'])
        self._info_cache: dict = {}

    # ── taryfa i okres ───────────────────────────────────────────────────────

    def tariff_on(self, day: date) -> str:
        return (tariff_history.tariff_on(self.periods, day) if self.periods else None) \
            or self.default_tariff

    def _period_info(self, day: date) -> tuple:
        """(okres po korektach, początek okresu, przyjęty?, źródło korekty|None) — cache per dzień."""
        hit = self._info_cache.get(day)
        if hit is not None:
            return hit
        last = max(self._entries, key=lambda p: p['valid_to'])
        assumed = day.isoformat() > last['valid_to']
        period = tc.period_for(self.catalog, date.fromisoformat(last['valid_to']) if assumed else day)
        manual = None
        for ov in self.overrides:
            start, end = ov.get('from'), ov.get('to')
            if (start is None or start <= day.isoformat()) and (end is None or day.isoformat() <= end):
                period = tc._deep_merge(period, ov.get('patch') or {})
                manual = ov.get('note') or 'ręczna korekta'
        info = (period, period['valid_from'], assumed, manual)
        self._info_cache[day] = info
        return info

    def _catalog_for(self, day: date) -> dict:
        """Katalog ze strefami obowiązującymi w dniu (`zones_history`; brak historii = `zones`)."""
        hist = [h for h in self.catalog.get('zones_history', []) if h['valid_from'] <= day.isoformat()]
        if not hist:
            return self.catalog
        return {**self.catalog, 'zones': max(hist, key=lambda h: h['valid_from'])['zones']}

    # ── strefa i cena ────────────────────────────────────────────────────────

    def zone_at(self, dt: datetime, tariff: Optional[str] = None) -> str:
        t = tariff or self.tariff_on(dt.date())
        return tc.zone_for(t, dt, self._catalog_for(dt.date()))

    def _level(self, tariff: str, period: dict, zone: str) -> str:
        gross = {}
        for z in period['energy_net'][tariff]:
            try:
                r = tc.rates_per_kwh(tariff, period, z)
            except ValueError:
                continue
            gross[z] = r['energy'] + r['dist'] + r['fees']
        values = set(round(v, 6) for v in gross.values())
        if len(values) <= 1:
            return 'flat'
        mine = round(gross[zone], 6)
        if mine == min(values):
            return 'cheap'
        if mine == max(values):
            return 'expensive'
        return 'mid'

    def price_at(self, dt: datetime, tariff: Optional[str] = None) -> PricePoint:
        day = dt.date()
        t = tariff or self.tariff_on(day)
        period, period_from, assumed, manual = self._period_info(day)
        zone = self.zone_at(dt, t)
        components = {
            'energy': tc._zone_value(period['energy_net'], t, zone, 'energy_net'),
            'dist': tc._zone_value(period['dist_var_net'], t, zone, 'dist_var_net'),
            'quality': tc._scalar(period, 'quality_net'),
            'oze': tc._scalar(period, 'oze_net'),
            'cogeneration': tc._scalar(period, 'cogeneration_net'),
            'excise': tc._scalar(period, 'excise_net'),
        }
        net = sum(components.values())
        if manual:
            confidence = 'manual'
        elif assumed:
            confidence = 'assumed'
        elif period.get('confirmed') and t not in (period.get('unconfirmed_tariffs') or []):
            confidence = 'document'
        elif period_from in self.invoice_ok:
            confidence = 'invoice'
        else:
            confidence = 'unconfirmed'
        return PricePoint(
            hour=dt.strftime('%Y-%m-%dT%H'), tariff=t, zone=zone, is_peak=is_peak_zone(t, zone),
            level=self._level(t, period, zone), components=components, net=net,
            gross=net * self.vat, period_from=period_from, confidence=confidence, source=manual)

    def schedule(self, start: datetime, hours: int, tariff: Optional[str] = None) -> list:
        """Godzina po godzinie od `start` (obcięte do pełnej godziny), `hours` punktów."""
        first = start.replace(minute=0, second=0, microsecond=0)
        return [self.price_at(first + timedelta(hours=i), tariff) for i in range(hours)]

    def next_change(self, dt: datetime, horizon_hours: int = 48,
                    tariff: Optional[str] = None) -> Optional[dict]:
        now = dt.replace(minute=0, second=0, microsecond=0)
        zone = self.zone_at(now, tariff)
        for i in range(1, horizon_hours + 1):
            t = now + timedelta(hours=i)
            z = self.zone_at(t, tariff)
            if z != zone:
                return {'at': t.strftime('%Y-%m-%dT%H'), 'zone': z}
        return None

    # ── opłaty stałe ─────────────────────────────────────────────────────────

    def fixed(self, day: date, tariff: Optional[str] = None) -> dict:
        period = self._period_info(day)[0]
        return tc.fixed_monthly(period, self.settings, tariff or self.tariff_on(day))

    # ── widok dla odbiorców dwustrefowych (ROI, bateria, pompa ciepła) ───────

    def legacy_rates_or_empty(self, day: date) -> dict:
        """`legacy_rates`, a `{}` gdy dzień jest sprzed katalogu albo taryfa nie ma widoku dwustrefowego."""
        try:
            return self.legacy_rates(day)
        except LookupError:
            return {}
        except ValueError as exc:
            if str(exc) not in _legacy_warned:      # raz na przyczynę, nie co cykl
                _legacy_warned.add(str(exc))
                logger.warning('tariff_module: legacy_rates(%s) niedostępne — %s', day, exc)
            return {}

    def legacy_rates(self, day: date, tariff: Optional[str] = None) -> dict:
        """Trzynaście pól stawek w formacie dawnego `tariff_config` / faktury (zł netto, brutto z VAT)."""
        t = tariff or self.tariff_on(day)
        if t not in _TWO_ZONE_VIEW:
            raise ValueError(f'{t}: brak widoku dwustrefowego (peak/offpeak)')
        peak_z, off_z = _TWO_ZONE_VIEW[t]
        period = self._period_info(day)[0]
        peak = tc.rates_per_kwh(t, period, peak_z)
        off = tc.rates_per_kwh(t, period, off_z)
        fixed = self.fixed(day, t)
        mocowa, abon, siec = fixed['capacity'], fixed['subscription_dist'], fixed['network']
        return {
            'energy_peak_net': peak['energy'], 'energy_offpeak_net': off['energy'],
            'dist_var_peak_net': peak['dist'], 'dist_var_offpeak_net': off['dist'],
            'dist_jakosciowa_net': tc._scalar(period, 'quality_net'),
            'dist_oze_net': tc._scalar(period, 'oze_net'),
            'dist_kogeneracja_net': tc._scalar(period, 'cogeneration_net'),
            'fixed_mocowa_net': mocowa, 'fixed_abonament_net': abon, 'fixed_stalysieciowy_net': siec,
            'peak_gross': (peak['energy'] + peak['dist'] + peak['fees']) * self.vat,
            'offpeak_gross': (off['energy'] + off['dist'] + off['fees']) * self.vat,
            'fixed_total_net': mocowa + abon + siec,
        }


def build(periods_path, current_override, overrides_path, settings_path=None, catalog=None,
          invoice_ok=None) -> TariffModule:
    """Moduł złożony z plików add-onu: historia taryf (z override opcji), korekty ręczne, ustawienia.

    Brakująca ścieżka (None) albo plik = puste dane / ustawienia domyślne."""
    from . import tariff_overrides, tariff_settings_store
    periods = tariff_history.with_override(
        tariff_history.load(periods_path) if periods_path else [], current_override, date.today())
    settings = (tariff_settings_store.to_catalog_settings(tariff_settings_store.load(settings_path))
                if settings_path else None)
    overrides = tariff_overrides.load(overrides_path) if overrides_path else []
    return TariffModule(catalog or tc.load_catalog(), periods, overrides, settings=settings,
                        invoice_ok=invoice_ok)


# ── encje MQTT (sensor.pv_roi_tracker_pv_tariff_zone / _price) ───────────────

def feed_payload(module: TariffModule, now: datetime) -> dict:
    """Payload encji taryfy: `{state, price, attributes}`; harmonogram 48 h od bieżącej pełnej godziny."""
    hour = now.replace(minute=0, second=0, microsecond=0)
    points = module.schedule(hour, 48)
    cur = points[0]
    return {
        'state': cur.zone,
        'price': round(cur.gross, 4),
        'attributes': {
            'tariff': cur.tariff,
            'is_peak': cur.is_peak,
            'level': cur.level,
            'gross_per_kwh': round(cur.gross, 4),
            'confidence': cur.confidence,
            'next_change': module.next_change(hour),
            'period_from': cur.period_from,
            'generated_at': now.isoformat(timespec='seconds'),
            'schedule': [{'hour': p.hour, 'tariff': p.tariff, 'zone': p.zone, 'is_peak': p.is_peak,
                          'level': p.level, 'gross': round(p.gross, 5), 'net': round(p.net, 5),
                          'confidence': p.confidence} for p in points],
        },
    }


def feed_changed(payload: dict, last: Optional[dict]) -> bool:
    """Czy payload różni się od ostatnio opublikowanego (poza `generated_at`); brak `last` = tak."""
    if last is None:
        return True

    def strip(p: dict) -> dict:
        return {**p, 'attributes': {k: v for k, v in p['attributes'].items() if k != 'generated_at'}}
    return strip(payload) != strip(last)


def tariff_feed_step(pub, last: dict, module: TariffModule, now: datetime) -> tuple:
    """Jeden krok publikacji encji taryfy. `last` = `{'payload', 'ok'}`.

    Zwraca `(wynik, nowe_last)`: wynik True = opublikowano, False = publikacja nieudana
    (rozłączony MQTT), None = pominięto (bez zmian i poprzednia była udana).
    Ostatni payload zapamiętywany dopiero po udanej publikacji."""
    payload = feed_payload(module, now)
    if last['ok'] and not feed_changed(payload, last['payload']):
        return None, last
    ok = bool(pub.publish_tariff(payload))
    return ok, {'payload': payload if ok else last['payload'], 'ok': ok}
