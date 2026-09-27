"""
Rok do roku (YoY) — kafel „Rok do roku” na stronie głównej: MTD (te same
elapsed dni bieżącego miesiąca vs rok temu) i YTD (od 1.01 do dziś vs tę samą
liczbę dni rok temu), plus projekcja tempa roku i serie do wykresu „wyścigu”
narastająco. Patrz docs/ROADMAP_YOY.md dla pełnego kontekstu i backtestu.

Cała logika tu jest czysta (bez I/O, bez HA) — surowe kWh (LTS) i ceny
(historic records / rce_hourly) dostarcza main.py; to trzyma moduł
testowalnym bez sieci ani Supervisora.

MTD: „te same dni” = pełne dni kalendarzowe od 1. do WCZORAJ (nie licząc
dzisiejszego niedokończonego dnia) — identycznie po obu stronach roku,
niezależnie o jakiej porze dnia main.py akurat odpytuje HA. `days_elapsed`
= today.day - 1; gdy dziś jest 1. dzień miesiąca okno jest puste (0 dni) —
build_yoy_payload zwraca wtedy None.

Dwie metody wyceny eksportu BIEŻĄCEGO (niezamkniętego) miesiąca, wybierane
przełącznikiem w UI (domyślnie 'estimate' — patrz DEFAULT_METHOD):
  'estimate'   — skorygowana średnia godzinowych cen RCE, patrz
                 rce_hourly.estimate_current_month_feedin_price().
  'prior_year' — realna RCEm z tego samego miesiąca kalendarzowego rok temu.
                 Empirycznie GORSZA (backtest na 24 zamkniętych miesiącach:
                 śr. błąd bezwzględny 22% vs 15% dla 'estimate', pojedyncze
                 pudła do -50% na zmianach trendu cen r/r) — ceny energii w
                 Polsce rosną z roku na rok, więc zeszłoroczna cena
                 systematycznie nie nadąża. Zostawiona jako opcja na życzenie
                 usera, nie jako domyślna.
Obie metody dotyczą WYŁĄCZNIE ceny eksportu bieżącego miesiąca; ceny okna
'prev' (to samo okno rok temu) oraz wszystkich zamkniętych miesięcy w YTD
zawsze pochodzą z realnych, już rozliczonych rekordów — nigdy nie są
szacowane.
"""
from __future__ import annotations

import calendar
from dataclasses import asdict, dataclass
from datetime import date
from typing import Optional

from .models import MonthlyRecord
from .tariff_analysis import _month_label

METHODS = ('estimate', 'prior_year')
DEFAULT_METHOD = 'estimate'

_MONTHS_PL = ['', 'Sty', 'Lut', 'Mar', 'Kwi', 'Maj', 'Cze',
              'Lip', 'Sie', 'Wrz', 'Paź', 'Lis', 'Gru']


@dataclass
class PeriodMetrics:
    savings_pln: Optional[float] = None
    self_savings_pln: Optional[float] = None
    feedin_revenue_pln: Optional[float] = None
    arbitrage_pln: Optional[float] = None
    produced_kwh: Optional[float] = None
    specific_yield: Optional[float] = None
    self_consumed_kwh: Optional[float] = None
    exported_kwh: Optional[float] = None
    purchased_kwh: Optional[float] = None
    consumed_kwh: Optional[float] = None
    self_consumption_rate_pct: Optional[float] = None   # self_consumed / produced
    autarky_pct: Optional[float] = None                 # self_consumed / consumed
    buy_price: Optional[float] = None
    feedin_price: Optional[float] = None
    net_grid_cost_pln: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def build_period_metrics(
    produced_kwh: Optional[float],
    exported_kwh: Optional[float],
    imported_kwh: Optional[float],
    buy_price: Optional[float],
    feedin_price: Optional[float],
    arbitrage_pln: float = 0.0,
    system_kwp: Optional[float] = None,
) -> PeriodMetrics:
    """Zbuduj metryki jednej strony (cur/prev) jednego okna z surowych kWh i cen.

    self_consumed_kwh/consumed_kwh zawsze wyliczane tak samo jak w
    live_reader._build_record (brak niezależnego licznika całego domu na tej
    instalacji) — patrz balance.py.
    """
    self_consumed = (max(0.0, produced_kwh - exported_kwh)
                      if (produced_kwh is not None and exported_kwh is not None) else None)
    consumed = (self_consumed + imported_kwh
                if (self_consumed is not None and imported_kwh is not None) else None)
    self_savings = (round(self_consumed * buy_price, 2)
                     if (self_consumed is not None and buy_price is not None) else None)
    feedin_revenue = (round(exported_kwh * feedin_price, 2)
                        if (exported_kwh is not None and feedin_price is not None) else None)
    arb = round(arbitrage_pln, 2) if arbitrage_pln is not None else 0.0
    savings = (round((self_savings or 0.0) + (feedin_revenue or 0.0) + arb, 2)
               if (self_savings is not None or feedin_revenue is not None) else None)
    purchase_cost = (round(imported_kwh * buy_price, 2)
                       if (imported_kwh is not None and buy_price is not None) else None)
    net_grid_cost = (round((purchase_cost or 0.0) - (feedin_revenue or 0.0), 2)
                       if (purchase_cost is not None or feedin_revenue is not None) else None)
    sc_rate = (round(self_consumed / produced_kwh * 100, 1)
                if (self_consumed is not None and produced_kwh) else None)
    autarky = (round(self_consumed / consumed * 100, 1)
                if (self_consumed is not None and consumed) else None)
    specific_yield = (round(produced_kwh / system_kwp, 1)
                        if (produced_kwh is not None and system_kwp) else None)

    return PeriodMetrics(
        savings_pln=savings, self_savings_pln=self_savings, feedin_revenue_pln=feedin_revenue,
        arbitrage_pln=arb, produced_kwh=produced_kwh, specific_yield=specific_yield,
        self_consumed_kwh=self_consumed, exported_kwh=exported_kwh, purchased_kwh=imported_kwh,
        consumed_kwh=consumed, self_consumption_rate_pct=sc_rate, autarky_pct=autarky,
        buy_price=buy_price, feedin_price=feedin_price, net_grid_cost_pln=net_grid_cost,
    )


def metrics_from_record(rec: MonthlyRecord, system_kwp: Optional[float] = None) -> PeriodMetrics:
    """PeriodMetrics z już policzonego, zamkniętego MonthlyRecord (bez przeliczania —
    tylko przepakowanie pól rekordu do wspólnego kształtu używanego w tym module)."""
    savings = None
    if (rec.self_consumed_savings_pln is not None or rec.feedin_revenue_pln is not None
            or rec.battery_arbitrage_savings_pln is not None):
        savings = round((rec.self_consumed_savings_pln or 0.0) + (rec.feedin_revenue_pln or 0.0)
                        + (rec.battery_arbitrage_savings_pln or 0.0), 2)
    purchase_cost = (round(rec.purchased_kwh * rec.buy_price_pln_kwh, 2)
                       if (rec.purchased_kwh is not None and rec.buy_price_pln_kwh is not None) else None)
    net_grid_cost = (round((purchase_cost or 0.0) - (rec.feedin_revenue_pln or 0.0), 2)
                       if (purchase_cost is not None or rec.feedin_revenue_pln is not None) else None)
    sc_rate = (round(rec.self_consumed_kwh / rec.produced_kwh * 100, 1)
                if (rec.self_consumed_kwh is not None and rec.produced_kwh) else None)
    autarky = (round(rec.self_consumed_kwh / rec.consumed_kwh * 100, 1)
                if (rec.self_consumed_kwh is not None and rec.consumed_kwh) else None)
    specific_yield = (rec.specific_yield if rec.specific_yield is not None
                       else (round(rec.produced_kwh / system_kwp, 1)
                             if (rec.produced_kwh is not None and system_kwp) else None))

    return PeriodMetrics(
        savings_pln=savings, self_savings_pln=rec.self_consumed_savings_pln,
        feedin_revenue_pln=rec.feedin_revenue_pln,
        arbitrage_pln=rec.battery_arbitrage_savings_pln or 0.0,
        produced_kwh=rec.produced_kwh, specific_yield=specific_yield,
        self_consumed_kwh=rec.self_consumed_kwh, exported_kwh=rec.exported_kwh,
        purchased_kwh=rec.purchased_kwh, consumed_kwh=rec.consumed_kwh,
        self_consumption_rate_pct=sc_rate, autarky_pct=autarky,
        buy_price=rec.buy_price_pln_kwh, feedin_price=rec.feedin_price_pln_kwh,
        net_grid_cost_pln=net_grid_cost,
    )


def aggregate_metrics(items: list[PeriodMetrics], system_kwp: Optional[float] = None) -> PeriodMetrics:
    """Zsumuj kilka PeriodMetrics (np. miesiące YTD). kWh i zł sumowane wprost;
    ceny/% przeliczone na nowo z sum (ważona średnia), nie uśrednione z
    już-uśrednionych miesięcznych wartości."""
    def _sum(attr: str) -> Optional[float]:
        vals = [getattr(it, attr) for it in items if getattr(it, attr) is not None]
        return round(sum(vals), 2) if vals else None

    produced = _sum('produced_kwh')
    exported = _sum('exported_kwh')
    imported = _sum('purchased_kwh')
    self_consumed = _sum('self_consumed_kwh')
    consumed = _sum('consumed_kwh')
    self_savings = _sum('self_savings_pln')
    feedin_revenue = _sum('feedin_revenue_pln')
    arbitrage = _sum('arbitrage_pln') or 0.0
    savings = _sum('savings_pln')
    net_grid_cost = _sum('net_grid_cost_pln')

    buy_num = sum((it.purchased_kwh or 0.0) * (it.buy_price or 0.0)
                  for it in items if it.purchased_kwh is not None and it.buy_price is not None)
    buy_den = sum(it.purchased_kwh or 0.0
                  for it in items if it.purchased_kwh is not None and it.buy_price is not None)
    buy_price = round(buy_num / buy_den, 4) if buy_den > 0 else None

    feedin_num = sum((it.exported_kwh or 0.0) * (it.feedin_price or 0.0)
                     for it in items if it.exported_kwh is not None and it.feedin_price is not None)
    feedin_den = sum(it.exported_kwh or 0.0
                     for it in items if it.exported_kwh is not None and it.feedin_price is not None)
    feedin_price = round(feedin_num / feedin_den, 4) if feedin_den > 0 else None

    sc_rate = round(self_consumed / produced * 100, 1) if (self_consumed is not None and produced) else None
    autarky = round(self_consumed / consumed * 100, 1) if (self_consumed is not None and consumed) else None
    specific_yield = round(produced / system_kwp, 1) if (produced is not None and system_kwp) else None

    return PeriodMetrics(
        savings_pln=savings, self_savings_pln=self_savings, feedin_revenue_pln=feedin_revenue,
        arbitrage_pln=arbitrage, produced_kwh=produced, specific_yield=specific_yield,
        self_consumed_kwh=self_consumed, exported_kwh=exported, purchased_kwh=imported,
        consumed_kwh=consumed, self_consumption_rate_pct=sc_rate, autarky_pct=autarky,
        buy_price=buy_price, feedin_price=feedin_price, net_grid_cost_pln=net_grid_cost,
    )


def decompose_effects(prev: PeriodMetrics, cur: PeriodMetrics) -> Optional[dict]:
    """Addytywne rozbicie Δ oszczędności na 5 przyczyn — wyprowadzenie w
    docs/ROADMAP_YOY.md. Suma pięciu efektów == cur.savings_pln - prev.savings_pln
    DOKŁADNIE (tożsamość algebraiczna, sprawdzona testem), o ile obie strony
    mają komplet danych produkcji/eksportu/cen. None gdy czegoś brakuje —
    nigdy nie zgadujemy połowicznego rozbicia."""
    required = (prev.produced_kwh, cur.produced_kwh, prev.self_consumed_kwh, cur.self_consumed_kwh,
                prev.exported_kwh, cur.exported_kwh, prev.buy_price, cur.buy_price,
                prev.feedin_price, cur.feedin_price)
    if any(v is None for v in required):
        return None

    p0, p1 = prev.produced_kwh, cur.produced_kwh
    sc0, sc1 = prev.self_consumed_kwh, cur.self_consumed_kwh
    ex0, ex1 = prev.exported_kwh, cur.exported_kwh
    buy0, buy1 = prev.buy_price, cur.buy_price
    fi0, fi1 = prev.feedin_price, cur.feedin_price
    arb0, arb1 = prev.arbitrage_pln or 0.0, cur.arbitrage_pln or 0.0

    s0 = sc0 / p0 if p0 else 0.0
    s1 = sc1 / p1 if p1 else 0.0
    blended0 = s0 * buy0 + (1 - s0) * fi0  # "zeszłoroczna" cena mieszana za 1 kWh produkcji

    production_effect       = (p1 - p0) * blended0
    self_consumption_effect = (s1 - s0) * p1 * (buy0 - fi0)
    buy_price_effect        = sc1 * (buy1 - buy0)
    feedin_price_effect     = ex1 * (fi1 - fi0)
    arbitrage_effect        = arb1 - arb0

    return {
        'production':       round(production_effect, 2),
        'self_consumption': round(self_consumption_effect, 2),
        'buy_price':        round(buy_price_effect, 2),
        'feedin_price':     round(feedin_price_effect, 2),
        'arbitrage':        round(arbitrage_effect, 2),
    }


def build_period(label: str, prev: PeriodMetrics, cur: PeriodMetrics,
                 flags: Optional[dict] = None) -> dict:
    delta = (round(cur.savings_pln - prev.savings_pln, 2)
             if (cur.savings_pln is not None and prev.savings_pln is not None) else None)
    delta_pct = (round(delta / prev.savings_pln * 100, 1)
                 if (delta is not None and prev.savings_pln) else None)
    return {
        'label': label,
        'cur': cur.to_dict(),
        'prev': prev.to_dict(),
        'delta_savings_pln': delta,
        'delta_savings_pct': delta_pct,
        'effects': decompose_effects(prev, cur),
        'flags': flags or {},
    }


def pair_closed_months(records: list[MonthlyRecord], today: date) -> tuple[list, list]:
    """Miesiące zamknięte tego roku (styczeń .. przed bieżącym), sparowane z
    tym samym miesiącem rok wcześniej. Zwraca (pary [(cur_rec, prev_rec)],
    lista 'YYYY-MM' miesięcy tego roku bez pary — brak danych rok temu)."""
    by_ym = {(r.year, r.month): r for r in records}
    pairs: list = []
    unpaired: list = []
    for m in range(1, today.month):
        cur_rec = by_ym.get((today.year, m))
        if cur_rec is None or not (cur_rec.produced_kwh or 0) > 0:
            continue
        prev_rec = by_ym.get((today.year - 1, m))
        if prev_rec is None or not (prev_rec.produced_kwh or 0) > 0:
            unpaired.append(f'{today.year}-{m:02d}')
            continue
        pairs.append((cur_rec, prev_rec))
    return pairs, unpaired


def build_ytd_period(
    closed_pairs: list,
    mtd_period: Optional[dict],
    system_kwp: Optional[float],
    label: str,
    flags: Optional[dict] = None,
) -> dict:
    """YTD = suma zamkniętych sparowanych miesięcy + okno MTD (ten sam obiekt
    Period co w mtd[method]) — patrz docs/ROADMAP_YOY.md. Efekty YTD = suma
    efektów miesięcznych (ceny różnią się per miesiąc, więc liczenie na sumach
    rocznych dałoby inny — błędny — wynik niż suma efektów policzonych osobno
    dla każdego miesiąca)."""
    cur_items = [metrics_from_record(c, system_kwp) for c, _ in closed_pairs]
    prev_items = [metrics_from_record(p, system_kwp) for _, p in closed_pairs]

    effects_sum = {k: 0.0 for k in ('production', 'self_consumption', 'buy_price', 'feedin_price', 'arbitrage')}
    any_effects = False
    for c, p in closed_pairs:
        eff = decompose_effects(metrics_from_record(p, system_kwp), metrics_from_record(c, system_kwp))
        if eff:
            any_effects = True
            for k in effects_sum:
                effects_sum[k] += eff[k]

    if mtd_period is not None:
        cur_items.append(PeriodMetrics(**mtd_period['cur']))
        prev_items.append(PeriodMetrics(**mtd_period['prev']))
        if mtd_period.get('effects'):
            any_effects = True
            for k in effects_sum:
                effects_sum[k] += mtd_period['effects'][k]

    cur_agg = aggregate_metrics(cur_items, system_kwp)
    prev_agg = aggregate_metrics(prev_items, system_kwp)
    delta = (round(cur_agg.savings_pln - prev_agg.savings_pln, 2)
             if (cur_agg.savings_pln is not None and prev_agg.savings_pln is not None) else None)
    delta_pct = (round(delta / prev_agg.savings_pln * 100, 1)
                 if (delta is not None and prev_agg.savings_pln) else None)

    return {
        'label': label,
        'cur': cur_agg.to_dict(),
        'prev': prev_agg.to_dict(),
        'delta_savings_pln': delta,
        'delta_savings_pct': delta_pct,
        'effects': {k: round(v, 2) for k, v in effects_sum.items()} if any_effects else None,
        'flags': flags or {},
    }


def build_projection(
    ytd_cur_savings: Optional[float], ytd_prev_savings: Optional[float],
    ytd_cur_produced: Optional[float], ytd_prev_produced: Optional[float],
    prev_full_year_savings: Optional[float], prev_full_year_produced: Optional[float],
) -> Optional[dict]:
    """Projekcja końca roku = pełny rok poprzedni × (YTD teraz / YTD rok temu).
    Orientacyjna — zakłada, że reszta roku powtórzy dotychczasowe tempo r/r;
    None gdy brak pełnego poprzedniego roku (12 miesięcy) albo YTD roku
    poprzedniego wynosi 0 (dzielenie niezdefiniowane)."""
    if not ytd_prev_savings or ytd_cur_savings is None or prev_full_year_savings is None:
        return None
    pace = ytd_cur_savings / ytd_prev_savings
    savings_proj = round(prev_full_year_savings * pace, 2)
    produced_proj = None
    if ytd_prev_produced and ytd_cur_produced is not None and prev_full_year_produced is not None:
        produced_proj = round(prev_full_year_produced * (ytd_cur_produced / ytd_prev_produced), 1)
    return {
        'savings_pln': savings_proj,
        'produced_kwh': produced_proj,
        'prev_full_savings_pln': round(prev_full_year_savings, 2),
        'prev_full_produced_kwh': round(prev_full_year_produced, 1) if prev_full_year_produced is not None else None,
        'gap_savings_pln': round(savings_proj - prev_full_year_savings, 2),
        'pace_pct': round(pace * 100, 1),
    }


def build_race_series(records: list[MonthlyRecord], today: date, mtd_by_method: dict) -> dict:
    """Serie do wykresu „wyścigu” narastająco. `prev` (rok ubiegły, pełny) i
    `cur_closed` (rok bieżący, tylko zamknięte miesiące) nie zależą od metody
    wyceny eksportu; `cur_today` (jeden dodatkowy punkt — bieżący, niedomknięty
    miesiąc) ma osobną wartość per metoda, bo dotyczy tego samego okna MTD,
    którego cena eksportu zależy od wyboru w UI. Front dokleja `cur_today
    [wybrana metoda]` jako ostatni punkt za `cur_closed`, żeby dorysować linię
    bieżącego roku aż do „dziś”."""
    by_ym = {(r.year, r.month): r for r in records}

    def _series(year: int, months: range) -> dict:
        savings: list = []
        produced: list = []
        net_grid: list = []
        cum_s = cum_p = cum_n = 0.0
        for m in months:
            rec = by_ym.get((year, m))
            if rec is None or not (rec.produced_kwh or 0) > 0:
                savings.append(None)
                produced.append(None)
                net_grid.append(None)
                continue
            met = metrics_from_record(rec)
            cum_s += met.savings_pln or 0.0
            cum_p += met.produced_kwh or 0.0
            cum_n += met.net_grid_cost_pln or 0.0
            savings.append(round(cum_s, 2))
            produced.append(round(cum_p, 1))
            net_grid.append(round(cum_n, 2))
        return {'savings_pln': savings, 'produced_kwh': produced, 'net_grid_cost_pln': net_grid}

    prev_series = _series(today.year - 1, range(1, 13))
    closed_series = _series(today.year, range(1, today.month))  # pusta gdy today.month == 1

    base_s = next((v for v in reversed(closed_series['savings_pln']) if v is not None), 0.0)
    base_p = next((v for v in reversed(closed_series['produced_kwh']) if v is not None), 0.0)
    base_n = next((v for v in reversed(closed_series['net_grid_cost_pln']) if v is not None), 0.0)

    cur_today: dict = {}
    for method, period in mtd_by_method.items():
        if period is None:
            cur_today[method] = None
            continue
        cur = period['cur']
        cur_today[method] = {
            'savings_pln': round(base_s + (cur.get('savings_pln') or 0.0), 2),
            'produced_kwh': round(base_p + (cur.get('produced_kwh') or 0.0), 1),
            'net_grid_cost_pln': round(base_n + (cur.get('net_grid_cost_pln') or 0.0), 2),
        }

    return {
        'cur_year': today.year,
        'prev_year': today.year - 1,
        'prev': prev_series,
        'cur_closed': closed_series,
        'cur_today': cur_today,
    }


def build_yoy_payload(
    records: list[MonthlyRecord],
    today: date,
    days_elapsed: int,
    mtd_prev_metrics: Optional[PeriodMetrics],
    mtd_cur_metrics_by_method: dict,
    mtd_flags_by_method: dict,
    system_kwp: Optional[float] = None,
) -> Optional[dict]:
    """Złóż cały payload `yoy` z już policzonych składników — main.py robi I/O
    (okna LTS, ceny) i buduje PeriodMetrics przez build_period_metrics(); ta
    funkcja tylko łączy je w MTD/YTD/projekcję/wyścig. Zwraca None gdy dziś
    jest 1. dzień miesiąca (okno MTD puste) albo brak danych okna 'prev'."""
    if days_elapsed <= 0 or mtd_prev_metrics is None:
        return None

    cutoff = today.replace(day=days_elapsed)  # ostatni w pełni policzony dzień
    mtd_label = f'1–{cutoff.day} {_MONTHS_PL[cutoff.month]}'
    mtd_by_method = {
        method: build_period(mtd_label, mtd_prev_metrics, mtd_cur_metrics_by_method[method],
                             flags=mtd_flags_by_method.get(method, {}))
        for method in METHODS
    }

    closed_pairs, unpaired = pair_closed_months(records, today)
    ytd_label = f'1.01–{cutoff.day:02d}.{cutoff.month:02d}'
    ytd_by_method = {
        method: build_ytd_period(closed_pairs, mtd_by_method[method], system_kwp,
                                 label=ytd_label, flags={'unpaired_months': unpaired})
        for method in METHODS
    }

    prev_full_year_recs = [r for r in records
                            if r.year == today.year - 1 and (r.produced_kwh or 0) > 0]
    prev_full_metrics = (aggregate_metrics([metrics_from_record(r, system_kwp) for r in prev_full_year_recs],
                                           system_kwp)
                          if len(prev_full_year_recs) == 12 else None)

    projection_by_method = {}
    for method in METHODS:
        ytd = ytd_by_method[method]
        projection_by_method[method] = (
            build_projection(
                ytd['cur'].get('savings_pln'), ytd['prev'].get('savings_pln'),
                ytd['cur'].get('produced_kwh'), ytd['prev'].get('produced_kwh'),
                prev_full_metrics.savings_pln, prev_full_metrics.produced_kwh,
            ) if prev_full_metrics is not None else None
        )

    race = build_race_series(records, today, mtd_by_method)

    return {
        'as_of': today.isoformat(),
        'days_elapsed': days_elapsed,
        'method_default': DEFAULT_METHOD,
        'methods': list(METHODS),
        'mtd': mtd_by_method,
        'ytd': ytd_by_method,
        'projection': projection_by_method,
        'race': race,
    }
