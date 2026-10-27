"""Testy tariff_grid_charge.py — wariant C: planowanie ładowania z sieci (DP po dobie)."""
import math
import time
from datetime import datetime

import pytest

from pv_roi_tracker import tariff_battery as tb
from pv_roi_tracker import tariff_grid_charge as gc
from pv_roi_tracker.battery_sim import BatteryConfig, throughput_cost_kwh
from pv_roi_tracker.tariff_catalog import load_catalog, zone_for

CFG = BatteryConfig()
EFF = math.sqrt(CFG.roundtrip_eff)
RANGE = CFG.usable_kwh * (1 - 0.16)
WEAR = throughput_cost_kwh(CFG)
ZERO = [0.0] * 24
ONE = [1.0] * 24


@pytest.fixture(scope="module")
def cat():
    return load_catalog()


def _two_zone(cheap_hours, cheap=0.64, dear=1.18):
    return [cheap if h in cheap_hours else dear for h in range(24)]


G12W_CHEAP = set(range(0, 6)) | {13, 14} | {22, 23}


def test_a_maly_spread_brak_dzialania():
    res = gc.solve_day([1.0] * 24, ZERO, ONE, 0.3, CFG)
    assert res["savings_pln"] == 0 and res["grid_charged_kwh"] == 0
    # różnica mniejsza niż straty + zużycie
    prices = [0.9 if h < 12 else 1.0 for h in range(24)]
    res = gc.solve_day(prices, ZERO, ONE, 0.3, CFG)
    assert res["savings_pln"] == 0 and res["grid_charged_kwh"] == 0


def test_b_dwie_strefy_g12w_like():
    prices = _two_zone(G12W_CHEAP)
    res = gc.solve_day(prices, ZERO, ONE, 0.3, CFG)
    assert res["savings_pln"] > 0
    assert res["charge_hours"] and res["discharge_hours"]
    assert set(res["charge_hours"]) <= G12W_CHEAP
    assert not (set(res["discharge_hours"]) & G12W_CHEAP)
    assert res["discharged_kwh"] <= (res["grid_charged_kwh"] + res["pv_charged_kwh"]) * EFF * EFF + 1e-6
    assert res["discharged_kwh"] <= 2.0 * RANGE * EFF + 1e-6


def test_c_g13s_zima_roboczy(cat):
    fn = tb.make_price_fn("G13s", cat, {}, {})
    prices = [fn(f"2026-01-14T{h:02d}")[0] for h in range(24)]
    res = gc.solve_day(prices, ZERO, ONE, 0.3, CFG)
    cheap = set(range(0, 7)) | set(range(10, 15)) | {21, 22, 23}
    dear = set(range(7, 10)) | set(range(15, 21))
    assert res["savings_pln"] > 0
    assert set(res["charge_hours"]) <= cheap
    assert set(res["discharge_hours"]) <= dear


def test_d_g12w_dolina_szczyt(cat):
    fn = tb.make_price_fn("G12w", cat, {}, {})
    prices = [fn(f"2026-01-14T{h:02d}")[0] for h in range(24)]
    res = gc.solve_day(prices, ZERO, ONE, 0.3, CFG)
    zones = [zone_for("G12w", datetime(2026, 1, 14, h), cat) for h in range(24)]
    assert res["savings_pln"] > 0
    assert all(zones[h] == "offpeak" for h in res["charge_hours"])
    assert all(zones[h] != "offpeak" for h in res["discharge_hours"])


def _cycles_prices():
    # 3 doliny po 2 h (0,2 zł) i 3 szczyty po 2 h (2,0 zł), reszta 1,0
    p = [1.0] * 24
    for base in (0, 8, 16):
        p[base] = p[base + 1] = 0.2
        p[base + 2] = p[base + 3] = 2.0
    return p


def test_e_limit_cykli():
    prices = _cycles_prices()
    imp = [2.5] * 24
    r2 = gc.solve_day(prices, ZERO, imp, 0.3, CFG, max_cycles=2.0)
    r1 = gc.solve_day(prices, ZERO, imp, 0.3, CFG, max_cycles=1.0)
    r3 = gc.solve_day(prices, ZERO, imp, 0.3, CFG, max_cycles=3.0)
    assert r2["grid_charged_kwh"] * EFF <= 2.0 * RANGE + 1e-6
    assert r1["grid_charged_kwh"] * EFF <= 1.0 * RANGE + 1e-6
    assert r1["savings_pln"] < r2["savings_pln"] < r3["savings_pln"]


def test_f_pv_ma_pierwszenstwo():
    prices = [2.0 if 10 <= h < 14 else 0.64 if h < 3 else 1.0 for h in range(24)]
    exp = [0.5 if h < 2 else 0.0 for h in range(24)]
    imp = [2.5 if 10 <= h < 14 else 0.0 for h in range(24)]
    res = gc.solve_day(prices, exp, imp, 0.2, CFG)
    assert res["pv_charged_kwh"] > 0
    assert res["pv_charged_kwh"] <= 1.0 + 1e-9
    assert res["grid_charged_kwh"] > 0
    # drogie PV (rcem > cena doliny + zużycie): sieć przed PV
    res2 = gc.solve_day(prices, exp, imp, 1.5, CFG)
    assert res2["pv_charged_kwh"] == 0
    assert res2["grid_charged_kwh"] > 0


def test_g_sprawnosc():
    prices = [0.3 if h < 3 else 2.0 if 10 <= h < 12 else 1.0 for h in range(24)]
    imp = [2.5 if 10 <= h < 12 else 0.0 for h in range(24)]
    res = gc.solve_day(prices, ZERO, imp, 0.3, CFG, max_cycles=1.0)
    assert res["discharged_kwh"] == pytest.approx(res["grid_charged_kwh"] * CFG.roundtrip_eff, rel=0.02)
    low = BatteryConfig(roundtrip_eff=0.5)
    p2 = [0.6 if h < 6 else 1.2 for h in range(24)]
    assert gc.solve_day(p2, ZERO, ONE, 0.3, CFG)["grid_charged_kwh"] > 0
    assert gc.solve_day(p2, ZERO, ONE, 0.3, low)["grid_charged_kwh"] == 0


def _balance(res, price_of_dis, price_of_ch, rcem):
    return (res["discharged_kwh"] * price_of_dis - res["grid_charged_kwh"] * price_of_ch
            - res["wear_pln"] - rcem * res["pv_charged_kwh"])


def test_h_zuzycie_i_bilans_bez_pv():
    prices = [0.5 if h < 4 else 1.5 for h in range(24)]
    res = gc.solve_day(prices, ZERO, ONE, 0.25, CFG)
    assert res["grid_charged_kwh"] > 0 and res["pv_charged_kwh"] == 0
    assert res["wear_pln"] == pytest.approx(WEAR * res["grid_charged_kwh"])
    assert res["savings_pln"] == pytest.approx(_balance(res, 1.5, 0.5, 0.25), abs=0.01)


def test_h_bilans_z_pv():
    prices = [0.5 if h < 4 else 1.5 for h in range(24)]
    exp = [0.5 if h in (1, 2) else 0.0 for h in range(24)]
    res = gc.solve_day(prices, exp, ONE, 0.25, CFG)
    assert res["pv_charged_kwh"] > 0 and res["grid_charged_kwh"] > 0
    assert res["wear_pln"] == pytest.approx(WEAR * res["grid_charged_kwh"])
    assert res["savings_pln"] == pytest.approx(_balance(res, 1.5, 0.5, 0.25), abs=0.01)


def test_i_dst_i_none():
    base = [0.64 if h < 6 else 1.18 for h in range(24)]
    for n in (23, 25):
        prices = (base + [1.18])[:n] if n == 25 else base[:n]
        res = gc.solve_day(prices, [0.0] * n, [1.0] * n, 0.3, CFG)
        assert res["savings_pln"] > 0
        assert all(0 <= i < n for i in res["charge_hours"] + res["discharge_hours"])
    with_none = list(base)
    for i in (3, 4, 15):
        with_none[i] = None
    a = gc.solve_day(with_none, ZERO, ONE, 0.3, CFG)
    keep = [i for i, p in enumerate(with_none) if p is not None]
    b = gc.solve_day([with_none[i] for i in keep], [0.0] * len(keep), [1.0] * len(keep), 0.3, CFG)
    assert a["savings_pln"] == pytest.approx(b["savings_pln"])
    assert [keep.index(i) for i in a["charge_hours"]] == b["charge_hours"]
    assert gc.solve_day([None] * 24, ZERO, ONE, 0.3, CFG)["savings_pln"] == 0


def _keys(day):
    return [f"{day}T{h:02d}" for h in range(24)]


def _pv_hours(days):
    hours = {}
    for d in days:
        for h in range(24):
            hours[f"{d}T{h:02d}"] = (2.5 if h in (13, 14) else 0.0,
                                      2.5 if h in (6, 7, 8, 9, 17, 18, 19) else 0.0)
    return hours


def test_j_variant_c_struktura(cat):
    hours = {}
    for d in ("2026-01-14", "2026-06-06"):
        for k in _keys(d):
            hours[k] = (0.0, 0.5)
    monthly = gc.simulate_variant_c(hours, CFG, cat, {}, {}, {}, "2026-01", "2026-06")
    assert set(monthly) == {"2026-01", "2026-06"}
    assert set(monthly["2026-01"]) == set(gc.GRID_TARIFFS)
    assert set(monthly["2026-01"]["G12w"]) == {"savings_pln", "wear_pln", "grid_charged_kwh",
                                                "pv_charged_kwh", "discharged_kwh"}


def test_j_c_nie_gorsze_niz_b(cat):
    hours = _pv_hours(("2026-01-14", "2026-06-02"))
    rcem = {"2026-01": 0.3, "2026-06": 0.3}
    monthly = gc.simulate_variant_c(hours, CFG, cat, {}, {}, rcem, "2026-01", "2026-06")
    b = tb.simulate_variant_b(hours, CFG, cat, {}, {}, rcem, "2026-01", "2026-06", soc_floor=0.16)
    for ym in monthly:
        for t in gc.GRID_TARIFFS:
            assert monthly[ym][t]["savings_pln"] >= b[ym][t]["savings_pln"] - 1e-9, (ym, t)
    assert any(monthly[ym][t]["pv_charged_kwh"] > 0 for ym in monthly for t in gc.GRID_TARIFFS)


def _synth_hours(month, days, seed, export_scale=1.0):
    import random
    r = random.Random(seed)
    h = {}
    for d in range(1, days + 1):
        for hr in range(24):
            exp = r.uniform(0.5, 2.5) * export_scale if 10 <= hr <= 15 else 0.0
            imp = 0.0 if 10 <= hr <= 15 else r.uniform(0.2, 1.2)
            if d % 3 == 0 and hr >= 16:
                imp = 0.0
            h[f"{month}-{d:02d}T{hr:02d}"] = (exp, imp)
    return h


@pytest.mark.parametrize("month,scale", [("2026-07", 1.0), ("2026-01", 0.15)])
@pytest.mark.parametrize("tariff", ["G12", "G12w", "G13"])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_c_nie_gorsze_niz_ciagle_b_syntetyczne(cat, month, scale, tariff, seed):
    hours = _synth_hours(month, 20, seed, scale)
    rcem = {month: 0.3}
    b = tb.simulate_variant_b(hours, CFG, cat, {}, {}, rcem, month, month,
                              soc_floor=gc.SOC_FLOOR)[month][tariff]["savings_pln"]
    m = gc.simulate_variant_c(hours, CFG, cat, {}, {}, rcem, month, month, tariffs=(tariff,))
    assert m[month][tariff]["savings_pln"] >= b - 1e-6


def test_solve_day_soc0_rozladowuje_energie_startowa():
    prices = [1.8] * 24
    prices[18] = 2.0
    exp = [0.0] * 24
    exp[12] = 1.0   # eksport przy wysokim RCEm: ladowanie z PV nieoplacalne
    imp = [0.0] * 24
    imp[18] = 1.0
    r0 = gc.solve_day(prices, exp, imp, 3.0, CFG)
    assert r0["discharged_kwh"] == 0 and r0["end_soc_kwh"] == 0
    r = gc.solve_day(prices, exp, imp, 3.0, CFG, soc0_kwh=2.0)
    assert r["discharge_hours"] == [18]
    assert r["charge_hours"] == []
    assert r["grid_charged_kwh"] == 0 and r["pv_charged_kwh"] == 0
    assert r["discharged_kwh"] > 0
    assert r["savings_pln"] > 0


def test_solve_day_end_value_zostawia_energie():
    prices = [0.5] * 24
    prices[18] = 1.0
    exp = [0.0] * 24
    exp[12] = 1.0
    imp = [0.0] * 24
    imp[18] = 1.0
    # tanie PV (rcem niskie) ladowane w poludnie; bez wartosci koncowej rozladowanie o 18:00
    r_hi = gc.solve_day(prices, exp, imp, 0.05, CFG, end_value=5.0)
    r_lo = gc.solve_day(prices, exp, imp, 0.05, CFG, end_value=0.0)
    assert r_hi["end_soc_kwh"] > 0
    assert r_lo["end_soc_kwh"] == 0
    assert r_lo["discharged_kwh"] > 0


def test_solve_day_wczesne_wyjscie_zachowuje_soc():
    r = gc.solve_day([0.5] * 24, ZERO, ZERO, 0.3, CFG, soc0_kwh=3.0)
    assert r["end_soc_kwh"] == pytest.approx(3.0, abs=0.2)
    assert r["charge_hours"] == r["discharge_hours"] == []


def test_k_wydajnosc(cat):
    hours = {}
    start = datetime(2026, 1, 1)
    for i in range(120):
        d = (start.toordinal() + i)
        day = datetime.fromordinal(d).strftime("%Y-%m-%d")
        for h in range(24):
            hours[f"{day}T{h:02d}"] = (2.5 if 10 <= h < 14 else 0.0, 0.0 if 10 <= h < 14 else 2.5)
    t0 = time.perf_counter()
    monthly = gc.simulate_variant_c(hours, CFG, cat, {}, {}, {}, "2026-01", "2026-12")
    elapsed = time.perf_counter() - t0
    print(f"\nwydajnosc: 120 dob x 4 taryfy = {elapsed:.2f} s")
    assert monthly
    assert elapsed < 60


def test_c_nie_gorsze_niz_b_z_resetem_losowe():
    import random
    from datetime import timedelta
    cat = load_catalog()
    rng = random.Random(7)
    hours = {}
    for i in range(60):
        d = (datetime(2026, 1, 1) + timedelta(days=i * 3)).strftime("%Y-%m-%d")
        for h in range(24):
            exp = rng.uniform(0, 3.0) if 9 <= h <= 15 else 0.0
            hours[f"{d}T{h:02d}"] = (round(exp, 3), round(rng.uniform(0, 2.5), 3))
    yms = sorted({k[:7] for k in hours})
    rcem = {ym: 0.3 for ym in yms}
    mon = gc.simulate_variant_c(hours, CFG, cat, {}, {}, rcem, yms[0], yms[-1])
    days = sorted({k[:10] for k in hours})
    pairs = 0
    for ym in mon:
        for t in gc.GRID_TARIFFS:
            bs = 0.0
            for d in days:
                if d[:7] == ym:
                    hd = {k: v for k, v in hours.items() if k[:10] == d}
                    bs += tb.simulate_variant_b(hd, CFG, cat, {}, {}, rcem, ym, ym,
                                                soc_floor=0.16)[ym][t]["savings_pln"]
            assert mon[ym][t]["savings_pln"] >= bs - 1e-9, (ym, t)
            pairs += 1
    assert pairs >= 24


def test_variant_c_doby_dst(cat):
    hours = {}
    for d, n in (("2026-03-29", 23), ("2026-10-25", 25)):
        for h in range(n):
            hours[f"{d}T{h % 24:02d}" if h < 24 else f"{d}T23"] = (0.0, 1.0)
    for ym, d in (("2026-03", "2026-03-29"), ("2026-10", "2026-10-25")):
        m = gc.simulate_variant_c(hours, CFG, cat, {}, {}, {}, ym, ym)
        assert ym in m


# ── suggest_plans ─────────────────────────────────────────────────────────────

SUG_FIELDS = {"charge_window", "target_soc_pct", "discharge_zones", "saving_vs_b_pln_year",
              "worth_it", "days_total"}


def _month_hours(ym, days, fn):
    return {f"{ym}-{d:02d}T{h:02d}": fn(d, h) for d in range(1, days + 1) for h in range(24)}


def _window_set(w):
    return {(w["start"] + i) % 24 for i in range(((w["end"] - w["start"]) % 24) or 24)}


def _zones_set(zs):
    out = set()
    for z in zs:
        out |= _window_set(z)
    return out


def _profile(tariff, cat, day="2026-01-14"):
    fn = tb.make_price_fn(tariff, cat, {}, {})
    return [fn(f"{day}T{h:02d}")[0] for h in range(24)]


# tani magazyn (niski koszt przerobu): domyślne 0,34 zł/kWh zjada cały spread G12/G13s
CHEAP_CFG = BatteryConfig(module_price_pln=2000.0)


def test_s_duzy_spread_g12(cat):
    hours = _month_hours("2026-01", 31, lambda d, h: (0.0, 0.8))
    res = gc.suggest_plans(hours, CHEAP_CFG, cat, {}, {}, {}, "2026-01", "2026-01", tariffs=("G12",))
    g = res["G12"]["winter_workday"]
    prof = _profile("G12", cat)
    assert g["worth_it"] is True
    win = _window_set(g["charge_window"])
    assert all(prof[h] < max(prof) for h in win)
    assert g["target_soc_pct"] > 16
    assert g["saving_vs_b_pln_year"] > gc.MIN_WORTH_PLN_YEAR
    dz = _zones_set(g["discharge_zones"])
    assert dz and not (dz & win)


def test_s_maly_spread(cat, monkeypatch):
    def flat(tariff, catalog, rdn, kompas, overrides=None, dist_tariff='G12w'):
        return lambda key: (1.0 + (0.001 if int(key[11:13]) < 12 else 0.0), False)
    monkeypatch.setattr(gc, "make_price_fn", flat)
    hours = _month_hours("2026-01", 31, lambda d, h: (0.0, 0.8))
    res = gc.suggest_plans(hours, CFG, cat, {}, {}, {}, "2026-01", "2026-01", tariffs=("G12",))
    g = res["G12"]["winter_workday"]
    assert g["worth_it"] is False
    assert g["charge_window"] is None and g["target_soc_pct"] is None
    assert g["discharge_zones"] == []
    assert g["saving_vs_b_pln_year"] >= 0


def test_s_g13s_zima_okno_bez_szczytu(cat):
    hours = _month_hours("2026-01", 31, lambda d, h: (0.0, 1.0 if h in (7, 8, 9, 15, 16, 17, 18, 19, 20) else 0.1))
    res = gc.suggest_plans(hours, CHEAP_CFG, cat, {}, {}, {}, "2026-01", "2026-01", tariffs=("G13s",))
    g = res["G13s"]["winter_workday"]
    prof = _profile("G13s", cat)
    assert g["worth_it"] is True
    assert not (_window_set(g["charge_window"]) & {h for h in range(24) if prof[h] == max(prof)})


def test_s_pv_nie_psuje(cat):
    def fn(d, h):
        return (2.0 if h in (11, 12, 13) else 0.0, 1.5 if h in (18, 19, 20, 21) else 0.0)
    hours = _month_hours("2026-06", 30, fn)
    res = gc.suggest_plans(hours, CFG, cat, {}, {}, {"2026-06": 0.3}, "2026-06", "2026-06")
    for t in gc.GRID_TARIFFS:
        for gk, g in res[t].items():
            assert g["saving_vs_b_pln_year"] >= 0
            assert g["worth_it"] == (g["saving_vs_b_pln_year"] >= gc.MIN_WORTH_PLN_YEAR)


def test_s_schemat_i_godziny_bez_ceny(cat):
    # G13s ma ceny dopiero od g13s_from: wcześniejsze godziny są pomijane
    hours = _month_hours("2024-06", 30, lambda d, h: (0.0, 0.5))
    hours.update(_month_hours("2026-01", 31, lambda d, h: (0.0, 0.5)))
    res = gc.suggest_plans(hours, CFG, cat, {}, {}, {}, "2024-06", "2026-01")
    assert set(res) == set(gc.GRID_TARIFFS)
    for t, groups in res.items():
        for gk, g in groups.items():
            assert set(g) == SUG_FIELDS, (t, gk)
            assert isinstance(g["worth_it"], bool) and isinstance(g["days_total"], int)
            assert isinstance(g["saving_vs_b_pln_year"], float) and g["saving_vs_b_pln_year"] >= 0
            assert isinstance(g["discharge_zones"], list)
            if g["worth_it"]:
                assert set(g["charge_window"]) == {"start", "end"}
                assert isinstance(g["target_soc_pct"], int)
            else:
                assert g["charge_window"] is None and g["target_soc_pct"] is None
                assert g["discharge_zones"] == []
    assert "winter_workday" in res["G13s"]


def test_s_simulate_schedule_none_zgodny_z_s1(cat):
    import random
    from pv_roi_tracker.battery_sim import simulate_s1
    r = random.Random(7)
    hours = {f"2026-06-{d:02d}T{h:02d}": (r.uniform(0, 2.5) if 9 <= h <= 15 else 0.0,
                                          0.0 if 9 <= h <= 15 else r.uniform(0, 2.0))
             for d in range(1, 21) for h in range(24)}
    rcem = {"2026-06": 0.31}
    fn = tb.make_price_fn("G12w", cat, {}, {})
    U = CFG.usable_kwh * (1 - gc.SOC_FLOOR)
    recs = [(int(k[11:13]), e, i, fn(k)[0], 0.31, "x") for k, (e, i) in sorted(hours.items())]
    import dataclasses
    cfg_b = dataclasses.replace(CFG, arbitrage_enabled=False, usable_kwh=U)
    ref = sum(row["savings_pln"] for row in simulate_s1(hours, cfg_b, {}, rcem, price_fn=fn))
    assert gc._simulate_schedule(recs, CFG, U, None, None) == pytest.approx(ref, abs=1e-9)


def test_s_wydajnosc_365_dob(cat):
    import random
    r = random.Random(3)
    start = datetime(2025, 2, 1).toordinal()
    hours = {}
    for i in range(365):
        day = datetime.fromordinal(start + i).strftime("%Y-%m-%d")
        for h in range(24):
            hours[f"{day}T{h:02d}"] = (r.uniform(0, 2.5) if 10 <= h < 15 else 0.0,
                                       0.0 if 10 <= h < 15 else r.uniform(0.2, 1.5))
    t0 = time.perf_counter()
    res = gc.suggest_plans(hours, CFG, cat, {}, {}, {}, "2025-02", "2026-01")
    elapsed = time.perf_counter() - t0
    print(f"\nsuggest_plans: 365 dob x 4 taryfy = {elapsed:.2f} s")
    assert res and elapsed < 15
