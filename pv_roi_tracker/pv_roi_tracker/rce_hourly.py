"""
RCEm vs RCE-godzinowa — symulacja rozliczenia sprzedaży nadwyżek PV.

Net-billing daje prosumentowi dwa tryby rozliczenia eksportu:
  • RCEm — miesięczna średnia cena rynkowa × suma kWh oddanych w miesiącu (tryb obecny)
  • RCE  — cena 15-minutowa × energia oddana w danym okresie (tryb godzinowy)

Moduł symuluje, jaki byłby przychód przy rozliczeniu godzinowym i porównuje
go miesiąc po miesiącu z faktycznym przychodem RCEm.

Źródła cen RCE (w kolejności):
  1. cache /data/rce_hourly.json — ceny minionych dni są ostateczne
  2. integracja HA rce_pse (atrybut `prices` sensora rce_pse_price) — dzień bieżący,
     bez odpytywania PSE
  3. oficjalne REST API PSE (api.raporty.pse.pl/api/rce-pln) — backfill dni
     historycznych, jeden request na miesiąc

Energia eksportowana godzinowo: statystyki długoterminowe HA ('change', period=hour)
dla sensor.power_meter_exported_energy_monthly.

Od 2025-02 depozyt zasilany jest wartością ×1.23 (współczynnik korekcyjny z nowelizacji
ustawy o OZE z 27.11.2024, obowiązuje WSZYSTKICH prosumentów net-billing — w obu trybach
RCEm i RCE) — ta sama reguła co w rcem_scraper._MULTIPLIER_FROM, stosowana też do
symulowanej RCE. Współczynnik nie wpływa więc na ZNAK różnicy RCE−RCEm, tylko na kwoty.

Zmiana RCEm → RCE godzinowa jest jednokierunkowa (decyzja nieodwracalna); przy RCE
godzinowej prosument może wypłacić do 30% depozytu w 12 mies. (RCEm: 20%).
Źródło: lepiej.tauron.pl/zielona-energia/jak-rozliczani-sa-prosumenci-nowelizacja-ustawy-o-oze/

Ceny ujemne: zgodnie z ustawą o OZE (art. 4b, brzmienie od nowelizacji 27.11.2024)
ujemna RCE jest w rozliczeniu prosumenta zastępowana wartością 0 zł — eksport
w godzinach ujemnych nie zasila depozytu, ale też nic nie kosztuje. Symulacja
stosuje tę regułę (p_eff = max(p, 0)) i raportuje skalę zjawiska per miesiąc:
neg_kwh / neg_share_pct / neg_saved_pln.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Optional

import requests

from .rcem_scraper import _MULTIPLIER_FROM

logger = logging.getLogger(__name__)

PSE_API_URL    = 'https://api.raporty.pse.pl/api/rce-pln'
EXPORT_ENTITY  = 'sensor.power_meter_exported_energy_monthly'
RCE_PSE_ENTITY = 'sensor.rce_pse_price'
DEFAULT_CACHE_PATH = Path('/data/rce_hourly.json')

# Pierwszy pełny miesiąc dostępny w API PSE v2 (dane od 2024-06-14).
FIRST_MONTH = '2024-07'

_MONTHS_PL = ['', 'Sty', 'Lut', 'Mar', 'Kwi', 'Maj', 'Cze',
              'Lip', 'Sie', 'Wrz', 'Paź', 'Lis', 'Gru']

# Miesiąc liczymy jako wiarygodny, gdy ≥95% eksportu ma dopasowaną cenę godzinową.
_MIN_COVERAGE_PCT = 95.0

# Wersja formatu cache: bump unieważnia zamrożone wiersze months (ceny zostają).
# v2: clamp cen ujemnych do 0 (art. 4b ustawy o OZE) + pola neg_*.
CACHE_VERSION = 2


def _vat_factor(ym: str) -> float:
    return 1.23 if ym >= _MULTIPLIER_FROM else 1.0


def _month_label(ym: str) -> str:
    return f'{ym[:4]}-{_MONTHS_PL[int(ym[5:7])]}'


# ── Cache ─────────────────────────────────────────────────────────────────────

def _load_cache(path: Path) -> dict:
    if not path.exists():
        return {'prices': {}, 'months': {}}
    try:
        data = json.loads(path.read_text())
        data.setdefault('prices', {})
        data.setdefault('months', {})
        if data.get('v') != CACHE_VERSION:
            # przeliczamy zamrożone miesiące wg nowych reguł; surowe ceny zostają
            logger.info('rce_hourly: cache v%s → v%d — unieważniam zamrożone miesiące',
                        data.get('v'), CACHE_VERSION)
            data['months'] = {}
        return data
    except Exception:
        logger.exception('rce_hourly: nie można wczytać %s — zaczynam od pustego cache', path)
        return {'prices': {}, 'months': {}}


def _save_cache(cache: dict, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix='.tmp')
        with os.fdopen(fd, 'w') as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        logger.exception('rce_hourly: zapis cache %s nie powiódł się', path)


# ── Pobieranie cen ────────────────────────────────────────────────────────────

def rows_to_hourly(rows: list) -> dict:
    """
    Wiersze 15-min PSE/rce_pse [{dtime, rce_pln, ...}] → {YYYY-MM-DDTHH: śr. PLN/MWh}.
    `dtime` to KONIEC okresu w czasie lokalnym, więc początek = dtime − 15 min.
    """
    buckets: dict = {}
    for r in rows:
        dt_raw = r.get('dtime')
        price = r.get('rce_pln')
        if dt_raw is None or price is None:
            continue
        try:
            start = datetime.strptime(dt_raw, '%Y-%m-%d %H:%M:%S') - timedelta(minutes=15)
            buckets.setdefault(start.strftime('%Y-%m-%dT%H'), []).append(float(price))
        except (ValueError, TypeError):
            continue
    return {k: round(mean(v), 2) for k, v in buckets.items()}


def fetch_pse_range(date_from: str, date_to: str) -> list:
    """Pobiera wiersze 15-min z API PSE dla zakresu dat (business_date, włącznie)."""
    rows: list = []
    url = (f"{PSE_API_URL}?$filter=business_date ge '{date_from}' "
           f"and business_date le '{date_to}'&$first=20000")
    for _ in range(10):  # limit stron — bezpiecznik
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        rows.extend(payload.get('value', []))
        url = payload.get('nextLink')
        if not url:
            break
    logger.info('rce_hourly: PSE API %s..%s → %d wierszy', date_from, date_to, len(rows))
    return rows


def fetch_today_from_ha() -> list:
    """Dzisiejsza krzywa RCE z integracji rce_pse (atrybut `prices`) — bez wołania PSE."""
    token = os.environ.get('SUPERVISOR_TOKEN', '')
    if not token:
        return []
    try:
        resp = requests.get(
            f'http://supervisor/core/api/states/{RCE_PSE_ENTITY}',
            headers={'Authorization': f'Bearer {token}'},
            timeout=10,
        )
        resp.raise_for_status()
        prices = resp.json().get('attributes', {}).get('prices') or []
        return prices if isinstance(prices, list) else []
    except Exception as exc:
        logger.warning('rce_hourly: odczyt %s nie powiódł się: %s', RCE_PSE_ENTITY, exc)
        return []


def _ensure_prices(cache: dict, months: list, today: date) -> None:
    """
    Uzupełnia cache['prices'] o brakujące godziny dla podanych miesięcy.
    Dni < dziś: API PSE (jeden ranged request na miesiąc z brakami).
    Dzień dzisiejszy: atrybuty integracji rce_pse w HA.
    """
    prices = cache['prices']
    today_str = today.isoformat()

    for ym in months:
        year, month = int(ym[:4]), int(ym[5:7])
        first = date(year, month, 1)
        last = (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)) - timedelta(days=1)
        # tylko dni już zakończone lub bieżący
        last = min(last, today)
        if first > today:
            continue

        # dni przeszłe bez kompletu 24 godzin w cache
        missing_past = [
            d for d in (first + timedelta(days=i) for i in range((last - first).days + 1))
            if d < today and sum(1 for h in range(24)
                                 if f'{d.isoformat()}T{h:02d}' in prices) < 23  # 23: dzień zmiany czasu
        ]
        if missing_past:
            try:
                rows = fetch_pse_range(missing_past[0].isoformat(), missing_past[-1].isoformat())
                prices.update(rows_to_hourly(rows))
            except Exception as exc:
                logger.warning('rce_hourly: backfill PSE %s nie powiódł się: %s', ym, exc)

    # dzień bieżący — z integracji HA (ceny day-ahead są ostateczne, można cache'ować)
    if any(ym == today_str[:7] for ym in months):
        ha_rows = [r for r in fetch_today_from_ha() if r.get('business_date') == today_str]
        if ha_rows:
            prices.update(rows_to_hourly(ha_rows))


# ── Czysta logika porównania ──────────────────────────────────────────────────

def compare_month(
    ym: str,
    export_hourly: dict,
    prices: dict,
    rcem_price_gross: Optional[float],
    exported_total_kwh: Optional[float],
    rcem_estimated: bool = False,
) -> Optional[dict]:
    """
    Porównanie jednego miesiąca. `export_hourly` = {YYYY-MM-DDTHH: kWh} (tylko ten miesiąc),
    `prices` = {YYYY-MM-DDTHH: PLN/MWh netto}, `rcem_price_gross` = PLN/kWh (jak w rcem_history).
    Zwraca None, gdy brak danych eksportu.
    """
    hours = {h: kwh for h, kwh in export_hourly.items() if h.startswith(ym) and kwh > 0}
    total_export_stats = sum(hours.values())
    if total_export_stats <= 0:
        return None

    factor = _vat_factor(ym)
    matched_kwh = 0.0
    revenue_rce = 0.0
    weighted_price_sum = 0.0
    neg_kwh = 0.0
    neg_saved = 0.0  # o ile reguła "ujemna → 0" podnosi przychód vs surowa RCE
    # profil dobowy: kWh eksportu i suma kWh×cena (surowa) per godzina 0-23
    hod_kwh = [0.0] * 24
    hod_pln = [0.0] * 24
    hod_neg = [0.0] * 24
    for h, kwh in hours.items():
        p = prices.get(h)
        if p is None:
            continue
        matched_kwh += kwh
        p_eff = max(p, 0.0)  # art. 4b ustawy o OZE: RCE ujemna → 0 zł
        if p < 0:
            neg_kwh += kwh
            neg_saved += kwh * (-p) / 1000.0 * factor
        revenue_rce += kwh * p_eff / 1000.0 * factor
        weighted_price_sum += kwh * p_eff / 1000.0
        try:
            hod = int(h[11:13])
            hod_kwh[hod] += kwh
            hod_pln[hod] += kwh * p
            if p < 0:
                hod_neg[hod] += kwh
        except (ValueError, IndexError):
            pass

    coverage = round(matched_kwh / total_export_stats * 100, 1)
    if matched_kwh <= 0:
        return None

    # Średnia RCE ważona profilem eksportu (netto, do porównania z RCEm netto)
    rce_weighted_net = weighted_price_sum / matched_kwh

    out: dict = {
        'ym': ym,
        'month_label': _month_label(ym),
        'exported_kwh': round(exported_total_kwh if exported_total_kwh is not None else total_export_stats, 1),
        'matched_kwh': round(matched_kwh, 1),
        'coverage_pct': coverage,
        'rce_weighted_price_pln_kwh': round(rce_weighted_net * factor, 4),
        'revenue_rce_pln': round(revenue_rce, 2),
        'neg_kwh': round(neg_kwh, 1),
        'neg_share_pct': round(neg_kwh / matched_kwh * 100, 1),
        'neg_saved_pln': round(neg_saved, 2),
        # heatmapa: per godzina [kWh, śr. cena netto PLN/MWh ważona eksportem, kWh ujemne]
        'hours_profile': [
            [round(hod_kwh[i], 1),
             round(hod_pln[i] / hod_kwh[i], 1) if hod_kwh[i] > 0 else None,
             round(hod_neg[i], 1)]
            for i in range(24)
        ],
        'rcem_price_pln_kwh': rcem_price_gross,
        'rcem_estimated': rcem_estimated,
        'revenue_rcem_pln': None,
        'diff_pln': None,
        'rce_better': None,
    }
    if rcem_price_gross is not None:
        # Ta sama baza kWh (matched) — porównanie cen, nie różnic liczników
        revenue_rcem = matched_kwh * rcem_price_gross
        out['revenue_rcem_pln'] = round(revenue_rcem, 2)
        out['diff_pln'] = round(revenue_rce - revenue_rcem, 2)
        out['rce_better'] = out['diff_pln'] > 0
    return out


def build_summary(months_out: list) -> dict:
    """KPI + rekomendacja na podstawie wierszy miesięcznych (tylko zamknięte, z RCEm)."""
    settled = [m for m in months_out
               if m['diff_pln'] is not None and not m['rcem_estimated']
               and m['coverage_pct'] >= _MIN_COVERAGE_PCT]
    diffs = [m['diff_pln'] for m in settled]
    n = len(diffs)
    total_diff = round(sum(diffs), 2)
    avg_diff = round(total_diff / n, 2) if n else 0.0
    n_rce_better = sum(1 for d in diffs if d > 0)
    pct_rce_better = round(n_rce_better / n * 100, 1) if n else 0.0

    # Skala cen ujemnych — po wszystkich miesiącach z danymi (nie tylko rozliczonych)
    with_neg = [m for m in months_out if m.get('neg_kwh') is not None]
    total_neg_kwh = round(sum(m['neg_kwh'] for m in with_neg), 1)
    total_matched = sum(m.get('matched_kwh') or 0.0 for m in with_neg)
    total_neg_saved = round(sum(m.get('neg_saved_pln') or 0.0 for m in with_neg), 2)

    if n < 3:
        recommendation = 'BRAK DANYCH'
        reason = f'Za mało rozliczonych miesięcy ({n}) — potrzeba min. 3'
    elif avg_diff > 10:
        recommendation = 'ROZWAŻ RCE'
        reason = (f'Rozliczenie godzinowe dałoby średnio +{avg_diff:.0f} PLN/mies. '
                  f'({pct_rce_better:.0f}% miesięcy na plus)')
    elif avg_diff < -10:
        recommendation = 'ZOSTAŃ PRZY RCEm'
        reason = f'RCEm korzystniejsza średnio o {abs(avg_diff):.0f} PLN/mies.'
    else:
        recommendation = 'NEUTRALNA'
        reason = f'Różnica {avg_diff:.0f} PLN/mies. — zbyt mała, by uzasadnić zmianę'

    return {
        'n_months': n,
        'total_diff_pln': total_diff,
        'avg_monthly_diff_pln': avg_diff,
        'months_rce_better': n_rce_better,
        'pct_rce_better': pct_rce_better,
        'neg_kwh_total': total_neg_kwh,
        'neg_share_pct_total': round(total_neg_kwh / total_matched * 100, 1) if total_matched else 0.0,
        'neg_saved_pln_total': total_neg_saved,
        'recommendation': recommendation,
        'recommendation_reason': reason,
        'note': ('Symulacja: przychód z tych samych godzinowych kWh eksportu wyceniony '
                 'ceną RCE (15-min, ważoną profilem) vs ceną RCEm. Godziny z ujemną RCE '
                 'liczone po 0 zł (art. 4b ustawy o OZE). UWAGA: przejście na '
                 'RCE godzinową (oświadczenie w Strefie Prosumenta) jest NIEODWRACALNE — '
                 'powrót do RCEm nie jest możliwy. Plus RCE: wypłata do 30% depozytu '
                 'w 12 mies. (RCEm: 20%). Współczynnik 1,23 obowiązuje w obu trybach.'),
    }


# ── Orkiestracja ──────────────────────────────────────────────────────────────

def update_and_compare(
    records: list,
    rcem_history: dict,
    cache_path: Path = DEFAULT_CACHE_PATH,
    get_stats_fn=None,
    today: Optional[date] = None,
) -> dict:
    """
    Główne wejście wołane z pętli poll. Zwraca payload dla zakładki UI:
    {'months': [...], 'summary': {...}, 'first_month': str}.
    Miesiące zamknięte i rozliczone trzymane są w cache i nie są przeliczane.
    """
    if get_stats_fn is None:
        from .live_reader import get_ha_tariff_stats
        get_stats_fn = get_ha_tariff_stats
    if today is None:
        today = date.today()

    cache = _load_cache(cache_path)
    current_ym = today.strftime('%Y-%m')

    by_ym = {f'{r.year}-{r.month:02d}': r for r in records}
    all_months = sorted(ym for ym in by_ym if FIRST_MONTH <= ym <= current_ym)
    if not all_months:
        return {'months': [], 'summary': build_summary([]), 'first_month': FIRST_MONTH}

    pending = [ym for ym in all_months if ym not in cache['months']]
    if pending:
        _ensure_prices(cache, pending, today)
        export_hourly = get_stats_fn([EXPORT_ENTITY], start=f'{pending[0]}-01',
                                     period='hour').get(EXPORT_ENTITY, {})
        for ym in pending:
            rcem = rcem_history.get(ym)
            rcem_estimated = False
            if rcem is None and ym == current_ym:
                # bieżący miesiąc: szacunek wg ostatniej znanej RCEm
                known = sorted(k for k in rcem_history if k < ym)
                if known:
                    rcem = rcem_history[known[-1]]
                    rcem_estimated = True
            rec = by_ym[ym]
            row = compare_month(ym, export_hourly, cache['prices'], rcem,
                                rec.exported_kwh, rcem_estimated=rcem_estimated)
            if row is None:
                continue
            # zamrażamy tylko miesiące zamknięte, rozliczone i z dobrym pokryciem
            if (ym < current_ym and not rcem_estimated and row['diff_pln'] is not None
                    and row['coverage_pct'] >= _MIN_COVERAGE_PCT):
                cache['months'][ym] = row
            else:
                cache.setdefault('_volatile', {})[ym] = row

    _save_cache({'v': CACHE_VERSION, 'prices': cache['prices'], 'months': cache['months']},
                cache_path)

    months_out = sorted(
        list(cache['months'].values()) + list(cache.get('_volatile', {}).values()),
        key=lambda m: m['ym'],
    )
    # deduplikacja (frozen ma pierwszeństwo)
    seen: dict = {}
    for m in months_out:
        if m['ym'] not in seen or m['ym'] in cache['months']:
            seen[m['ym']] = m
    months_out = [seen[k] for k in sorted(seen)]

    return {
        'months': months_out,
        'summary': build_summary(months_out),
        'first_month': FIRST_MONTH,
    }
