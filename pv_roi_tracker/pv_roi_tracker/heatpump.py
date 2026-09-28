"""
Pompa ciepła × PV — prawdziwy koszt grzania i CWU.

Moduł czysty (bez I/O) — godzinowe dane liczników, ceny sieci per miesiąc i
RCEm per miesiąc dostarcza wywołujący (main.py), tak jak battery_sim.py dla
symulacji magazynu. Cache/pobieranie: heatpump_store.py + live_reader.

Model godzinowy:
  1. Z liczników domu (Energy Dashboard) wyliczamy, ile w tej godzinie dom
     wziął z PV, z baterii i z sieci (`hourly_flows`).
  2. Pompie przypisujemy TAKI SAM miks źródeł co całemu domowi w tej
     godzinie, proporcjonalnie do jej zużycia względem całkowitego
     zużycia domu (`attribute_heatpump`) — patrz ROADMAP_HEATPUMP.md §2.
     Jeśli zużycie pompy w danej godzinie przewyższa policzone zużycie domu
     (niespójność liczników — inny tor pomiarowy), nadwyżka trafia do sieci
     i godzina jest flagowana jako anomalia.
  3. Bateria ma WŁASNY koszt jednostkowy śledzony metodą średniej ważonej
     (`BatteryPool`) — ładowanie z PV kosztuje utraconą sprzedaż po RCEm,
     ładowanie z sieci kosztuje cenę strefy tej godziny; rozładowanie
     "sprzedaje" energię pompie po aktualnej średniej jednostkowej.
  4. Koszt pompy w tej godzinie liczony w DWÓCH wersjach:
       - gotówkowej: PV = 0 zł, płaci się tylko za kWh z sieci (w cenie
         strefy tej godziny) i za kWh z baterii pochodzące z sieci,
       - ekonomicznej: każda kWh ma cenę — z sieci jak wyżej, z PV/baterii
         po RCEm miesiąca (utracona sprzedaż).

Bez licznika ciepła — brak COP i zł/kWh ciepła. Porównanie sezonów idzie
przez stopniodnie (HDD, baza `hdd_base`), nie przez uzysk cieplny.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from .battery_sim import is_peak

_SEASON_START_MONTH = 9  # wrzesień — początek sezonu grzewczego


# ── Klucze godzinowe 'YYYY-MM-DDTHH' ──────────────────────────────────────────

def _date_of(hour_key: str) -> date:
    return date(int(hour_key[0:4]), int(hour_key[5:7]), int(hour_key[8:10]))


def _hour_of(hour_key: str) -> int:
    return int(hour_key[11:13])


def season_start_year(d: date) -> int:
    """Rok początku sezonu grzewczego (IX–VIII) zawierającego `d`."""
    return d.year if d.month >= _SEASON_START_MONTH else d.year - 1


def season_key(start_year: int) -> str:
    return f'{start_year}-{start_year + 1}'


def season_label(start_year: int) -> str:
    return f'{start_year}/{str(start_year + 1)[-2:]}'


def season_cutoff_date(start_year: int, today: date) -> date:
    """Data w sezonie `start_year` odpowiadająca 'tej samej dacie' co `today`
    (do uczciwego porównania sezonu w toku z sezonami zamkniętymi)."""
    try:
        if today.month >= _SEASON_START_MONTH:
            return date(start_year, today.month, today.day)
        return date(start_year + 1, today.month, today.day)
    except ValueError:
        # 29 lutego bez przestępnego roku docelowego
        return date(start_year + 1, today.month, today.day - 1)


# ── Bateria: koszt jednostkowy metodą średniej ważonej ────────────────────────

@dataclass
class BatteryPool:
    """Śledzi średni koszt jednostkowy (got./ekon.) energii aktualnie w baterii.

    Start pusty (2023-06 = początek historii licznika pompy) — pierwsze dni
    symulacji lekko zaniżają koszt baterii, efekt znika po pierwszym cyklu.
    """
    energy_kwh: float = 0.0
    unit_cash: float = 0.0
    unit_econ: float = 0.0

    def charge(self, from_pv: float, from_grid: float,
               grid_price: Optional[float], pv_price: Optional[float],
               efficiency: float) -> None:
        total_in = from_pv + from_grid
        if total_in <= 0:
            return
        stored = total_in * efficiency
        cash_cost = from_grid * (grid_price or 0.0)
        econ_cost = from_grid * (grid_price or 0.0) + from_pv * (pv_price if pv_price is not None
                                                                   else (grid_price or 0.0))
        new_energy = self.energy_kwh + stored
        if new_energy <= 0:
            return
        self.unit_cash = (self.unit_cash * self.energy_kwh + cash_cost) / new_energy
        self.unit_econ = (self.unit_econ * self.energy_kwh + econ_cost) / new_energy
        self.energy_kwh = new_energy

    def unit_costs(self) -> tuple[float, float]:
        return self.unit_cash, self.unit_econ

    def drain(self, kwh: float) -> float:
        """Zmniejsz pulę o `kwh` (koszt liczy wywołujący z unit_costs() SPRZED
        wywołania — metoda średniego kosztu nie zmienia ceny jednostkowej przy
        rozładowaniu). Zwraca faktycznie pobrane kWh (obcięte do dostępnych)."""
        take = min(max(kwh, 0.0), self.energy_kwh)
        self.energy_kwh -= take
        return take


# ── Bilans godziny ─────────────────────────────────────────────────────────────

def hourly_flows(h: dict) -> dict:
    """Z surowych liczników godziny wylicz, ile dom wziął z PV/baterii/sieci.

    `h`: dict z kluczami produced/exported/imported/batt_charge/batt_discharge
    (kWh, może brakować kluczy → traktowane jako 0).
    """
    produced = h.get('produced') or 0.0
    exported = h.get('exported') or 0.0
    imported = h.get('imported') or 0.0
    batt_charge = h.get('batt_charge') or 0.0
    batt_discharge = h.get('batt_discharge') or 0.0

    pv_net = max(0.0, produced - exported)
    charge_from_pv = min(batt_charge, pv_net)
    charge_from_grid = max(0.0, batt_charge - charge_from_pv)

    pv_to_load = max(0.0, pv_net - charge_from_pv)
    grid_to_load = max(0.0, imported - charge_from_grid)
    load = pv_to_load + batt_discharge + grid_to_load

    return {
        'load': load,
        'pv_to_load': pv_to_load,
        'batt_to_load': batt_discharge,
        'grid_to_load': grid_to_load,
        'charge_from_pv': charge_from_pv,
        'charge_from_grid': charge_from_grid,
    }


def attribute_heatpump(flows: dict, hp_kwh: float) -> dict:
    """Przypisz pompie taki sam miks źródeł co całemu domowi (proporcjonalnie).

    Zwraca hp_pv/hp_batt/hp_grid (kWh) + `anomaly` (True gdy hp_kwh > load —
    licznik pompy poza torem pomiarowym licznika domu w tej godzinie; nadwyżka
    dolicza się do hp_grid)."""
    load = flows['load']
    hp_kwh = max(0.0, hp_kwh)
    if load <= 0 or hp_kwh <= 0:
        share = 0.0
    else:
        share = min(1.0, hp_kwh / load)
    hp_pv = flows['pv_to_load'] * share
    hp_batt = flows['batt_to_load'] * share
    hp_grid = flows['grid_to_load'] * share
    overflow = max(0.0, hp_kwh - (hp_pv + hp_batt + hp_grid))
    hp_grid += overflow
    return {'hp_pv': hp_pv, 'hp_batt': hp_batt, 'hp_grid': hp_grid, 'anomaly': overflow > 1e-9}


def grid_price_for_hour(d: date, hour: int, rates: Optional[tuple]) -> Optional[float]:
    """Cena sieci (PLN/kWh brutto) danej godziny z (peak_gross, offpeak_gross).

    offpeak_gross None → taryfa jednostrefowa (G11, flat = peak_gross)."""
    if rates is None:
        return None
    peak_gross, offpeak_gross = rates
    if offpeak_gross is None:
        return peak_gross
    return peak_gross if is_peak(datetime(d.year, d.month, d.day, hour)) else offpeak_gross


def mode_split(hp_kwh: float, heat_h: float, dhw_h: float) -> dict:
    """Rozbij kWh pompy na grzanie/CWU/inne wg udziału godzin w trybie.

    heat_h/dhw_h: minuty-jako-godziny w danej godzinie zegarowej (0..1 zwykle,
    obcinane do [0,1] — czujnik bywa zaszumiony); oba 0 mimo hp_kwh > 0 →
    „inne" (postój sprężarki, cyrkulacja, antyzamarzanie)."""
    if hp_kwh <= 0:
        return {'heating': 0.0, 'dhw': 0.0, 'other': 0.0}
    heat_h = min(max(heat_h, 0.0), 1.0)
    dhw_h = min(max(dhw_h, 0.0), 1.0)
    total_h = heat_h + dhw_h
    if total_h <= 0:
        return {'heating': 0.0, 'dhw': 0.0, 'other': hp_kwh}
    return {
        'heating': hp_kwh * (heat_h / total_h),
        'dhw': hp_kwh * (dhw_h / total_h),
        'other': 0.0,
    }


# ── Przebieg godzinowy (pełna historia) ────────────────────────────────────────

def run_hours(
    hours: dict[str, dict],
    rates_by_month: dict[str, tuple],
    rcem_by_month: dict[str, float],
    battery_efficiency: float = 0.92,
) -> list[dict]:
    """Przelicz WSZYSTKIE godziny chronologicznie (pula baterii ma pamięć).

    Zwraca listę wzbogaconych rekordów godzinowych (jeden dict na klucz), do
    dalszej agregacji przez `aggregate_months`/`aggregate_seasons`."""
    pool = BatteryPool()
    out: list[dict] = []
    for hk in sorted(hours):
        h = hours[hk]
        d = _date_of(hk)
        hour = _hour_of(hk)
        ym = hk[:7]
        hp_kwh = h.get('hp_kwh') or 0.0

        flows = hourly_flows(h)
        grid_price = grid_price_for_hour(d, hour, rates_by_month.get(ym))
        pv_price = rcem_by_month.get(ym)

        pool.charge(flows['charge_from_pv'], flows['charge_from_grid'],
                   grid_price, pv_price, battery_efficiency)
        unit_cash, unit_econ = pool.unit_costs()
        pool.drain(flows['batt_to_load'])

        attrib = attribute_heatpump(flows, hp_kwh)
        modes = mode_split(hp_kwh, h.get('heat_h') or 0.0, h.get('dhw_h') or 0.0)

        hp_batt_cash = attrib['hp_batt'] * unit_cash
        hp_batt_econ = attrib['hp_batt'] * unit_econ
        cash = attrib['hp_grid'] * (grid_price or 0.0) + hp_batt_cash
        econ = (attrib['hp_grid'] * (grid_price or 0.0) + hp_batt_econ
               + attrib['hp_pv'] * (pv_price if pv_price is not None else (grid_price or 0.0)))
        has_price = grid_price is not None

        out.append({
            'hour_key': hk, 'date': d, 'hour': hour, 'ym': ym,
            'season_start_year': season_start_year(d),
            'hp_kwh': hp_kwh, **modes,
            'hp_pv': attrib['hp_pv'], 'hp_batt': attrib['hp_batt'], 'hp_grid': attrib['hp_grid'],
            'hp_grid_peak': attrib['hp_grid'] if is_peak(datetime(d.year, d.month, d.day, hour)) else 0.0,
            'hp_grid_offpeak': attrib['hp_grid'] if not is_peak(datetime(d.year, d.month, d.day, hour)) else 0.0,
            'cash_pln': cash if has_price else 0.0,
            'econ_pln': econ if has_price else 0.0,
            'has_price': has_price,
            'anomaly': attrib['anomaly'],
            't_out': h.get('t_out'),
        })
    return out


# ── Agregacja ───────────────────────────────────────────────────────────────────

def _new_bucket() -> dict:
    return {
        'kwh_heating': 0.0, 'kwh_dhw': 0.0, 'kwh_other': 0.0, 'kwh_total': 0.0,
        'kwh_pv': 0.0, 'kwh_batt': 0.0, 'kwh_grid_peak': 0.0, 'kwh_grid_offpeak': 0.0,
        'cash_pln': 0.0, 'econ_pln': 0.0, 'hdd': 0.0, 'temp_hours': 0,
        'anomaly_hours': 0, 'hours_no_price': 0, 'hours': 0,
    }


def _add_hour(bucket: dict, r: dict, hdd_base: float) -> None:
    bucket['kwh_heating'] += r['heating']
    bucket['kwh_dhw'] += r['dhw']
    bucket['kwh_other'] += r['other']
    bucket['kwh_total'] += r['hp_kwh']
    bucket['kwh_pv'] += r['hp_pv']
    bucket['kwh_batt'] += r['hp_batt']
    bucket['kwh_grid_peak'] += r['hp_grid_peak']
    bucket['kwh_grid_offpeak'] += r['hp_grid_offpeak']
    bucket['cash_pln'] += r['cash_pln']
    bucket['econ_pln'] += r['econ_pln']
    if r['t_out'] is not None:
        bucket['hdd'] += max(0.0, hdd_base - r['t_out']) / 24.0
        bucket['temp_hours'] += 1
    if r['anomaly']:
        bucket['anomaly_hours'] += 1
    if not r['has_price']:
        bucket['hours_no_price'] += 1
    bucket['hours'] += 1


def _finalize_bucket(b: dict) -> dict:
    total = b['kwh_total']
    coverage = (b['kwh_pv'] + b['kwh_batt']) / total * 100.0 if total > 0 else None
    hdd = round(b['hdd'], 1)
    out = {
        'kwh_heating': round(b['kwh_heating'], 1), 'kwh_dhw': round(b['kwh_dhw'], 1),
        'kwh_other': round(b['kwh_other'], 1), 'kwh_total': round(total, 1),
        'kwh_pv': round(b['kwh_pv'], 1), 'kwh_batt': round(b['kwh_batt'], 1),
        'kwh_grid_peak': round(b['kwh_grid_peak'], 1), 'kwh_grid_offpeak': round(b['kwh_grid_offpeak'], 1),
        'cash_pln': round(b['cash_pln'], 2), 'econ_pln': round(b['econ_pln'], 2),
        'hdd': hdd if b['temp_hours'] > 0 else None,
        'pv_battery_coverage_pct': round(coverage, 1) if coverage is not None else None,
        'anomaly_hours': b['anomaly_hours'], 'hours_no_price': b['hours_no_price'], 'hours': b['hours'],
        'cash_pln_per_hdd': round(b['cash_pln'] / hdd, 2) if hdd and hdd > 0 else None,
        'econ_pln_per_hdd': round(b['econ_pln'] / hdd, 2) if hdd and hdd > 0 else None,
        'kwh_heating_per_hdd': round(b['kwh_heating'] / hdd, 2) if hdd and hdd > 0 else None,
    }
    return out


def aggregate_months(rows: list[dict], hdd_base: float = 15.0) -> list[dict]:
    buckets: dict[str, dict] = {}
    for r in rows:
        buckets.setdefault(r['ym'], _new_bucket())
        _add_hour(buckets[r['ym']], r, hdd_base)
    out = []
    for ym in sorted(buckets):
        rec = {'ym': ym, **_finalize_bucket(buckets[ym])}
        out.append(rec)
    return out


def aggregate_seasons(rows: list[dict], today: date, hdd_base: float = 15.0) -> list[dict]:
    """Sezony IX–VIII: agregat pełny (gdy sezon już zamknięty) i 'do tej samej
    daty' (dla uczciwego porównania sezonu w toku z poprzednimi)."""
    start_years = sorted({r['season_start_year'] for r in rows})
    cur_start = season_start_year(today)
    out = []
    for sy in start_years:
        cutoff = season_cutoff_date(sy, today)
        full_bucket = _new_bucket()
        todate_bucket = _new_bucket()
        has_full = False
        for r in rows:
            if r['season_start_year'] != sy:
                continue
            _add_hour(full_bucket, r, hdd_base)
            has_full = True
            if r['date'] <= cutoff:
                _add_hour(todate_bucket, r, hdd_base)
        season_ended = today >= date(sy + 1, _SEASON_START_MONTH, 1)
        out.append({
            'season': season_key(sy), 'label': season_label(sy),
            'is_current': sy == cur_start,
            'full': _finalize_bucket(full_bucket) if (has_full and season_ended) else None,
            'to_date': _finalize_bucket(todate_bucket),
            'cutoff_date': cutoff.isoformat(),
        })
    return out


def compute(
    hours: dict[str, dict],
    rates_by_month: dict[str, tuple],
    rcem_by_month: dict[str, float],
    battery_efficiency: float = 0.92,
    today: Optional[date] = None,
    hdd_base: float = 15.0,
) -> Optional[dict]:
    """Pełny payload zakładki „Pompa ciepła" dla `/api/data`. None gdy brak
    godzin (funkcja wyłączona lub jeszcze nic nie pobrano)."""
    if not hours:
        return None
    if today is None:
        today = date.today()
    rows = run_hours(hours, rates_by_month, rcem_by_month, battery_efficiency)
    months = aggregate_months(rows, hdd_base)
    seasons = aggregate_seasons(rows, today, hdd_base)
    current = next((s for s in seasons if s['is_current']), None)
    return {
        'months': months,
        'seasons': seasons,
        'current_season_to_date': current['to_date'] if current else None,
        'hours_total': len(rows),
        'anomaly_hours_total': sum(1 for r in rows if r['anomaly']),
    }
