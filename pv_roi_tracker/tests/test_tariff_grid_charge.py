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
    monthly, plans = gc.simulate_variant_c(hours, CFG, cat, {}, {}, {}, "2026-01", "2026-06")
    assert set(monthly) == {"2026-01", "2026-06"}
    assert set(monthly["2026-01"]) == set(gc.GRID_TARIFFS)
    assert set(monthly["2026-01"]["G12w"]) == {"savings_pln", "wear_pln", "grid_charged_kwh",
                                                "pv_charged_kwh", "discharged_kwh"}
    assert set(plans) == set(gc.GRID_TARIFFS)
    assert set(plans["G12w"]) == {"winter_workday", "summer_dayoff"}
    w = plans["G13s"]["winter_workday"]
    assert w["days"] == w["days_total"] == 1
    assert w["avg_saving_pln"] == pytest.approx(monthly["2026-01"]["G13s"]["savings_pln"])
    assert w["avg_grid_kwh"] == pytest.approx(monthly["2026-01"]["G13s"]["grid_charged_kwh"])
    assert w["charge_hours"] and w["discharge_hours"]
    assert set(w["charge_hours"]) <= set(range(0, 7)) | set(range(10, 15)) | {21, 22, 23}
    # soboty latem w G12w: cała doba tania, brak spreadu -> brak akcji
    assert plans["G12w"]["summer_dayoff"]["days"] == 0
    assert plans["G12w"]["summer_dayoff"]["charge_hours"] == []


def test_j_progi_50_procent(cat):
    # 2 doby robocze zimą: tylko jedna ma dane (import) wymuszające rozładowanie
    hours = {}
    for k in _keys("2026-01-14"):
        hours[k] = (0.0, 0.5)
    for k in _keys("2026-01-15"):
        hours[k] = (0.0, 0.0)
    for k in _keys("2026-01-16"):
        hours[k] = (0.0, 0.0)
    _, plans = gc.simulate_variant_c(hours, CFG, cat, {}, {}, {}, "2026-01", "2026-01",
                                      tariffs=("G13s",))
    w = plans["G13s"]["winter_workday"]
    assert w["days_total"] == 3 and w["days"] == 1
    assert w["charge_hours"] == [] and w["discharge_hours"] == []  # 1/3 < 50%


def test_j_c_nie_gorsze_niz_b(cat):
    hours = _pv_hours(("2026-01-14", "2026-06-02"))
    rcem = {"2026-01": 0.3, "2026-06": 0.3}
    monthly, _ = gc.simulate_variant_c(hours, CFG, cat, {}, {}, rcem, "2026-01", "2026-06")
    b = tb.simulate_variant_b(hours, CFG, cat, {}, {}, rcem, "2026-01", "2026-06", soc_floor=0.16)
    for ym in monthly:
        for t in gc.GRID_TARIFFS:
            assert monthly[ym][t]["savings_pln"] >= b[ym][t]["savings_pln"] - 1e-9, (ym, t)
    assert any(monthly[ym][t]["pv_charged_kwh"] > 0 for ym in monthly for t in gc.GRID_TARIFFS)


def test_k_wydajnosc(cat):
    hours = {}
    start = datetime(2026, 1, 1)
    for i in range(120):
        d = (start.toordinal() + i)
        day = datetime.fromordinal(d).strftime("%Y-%m-%d")
        for h in range(24):
            hours[f"{day}T{h:02d}"] = (2.5 if 10 <= h < 14 else 0.0, 0.0 if 10 <= h < 14 else 2.5)
    t0 = time.perf_counter()
    monthly, _ = gc.simulate_variant_c(hours, CFG, cat, {}, {}, {}, "2026-01", "2026-12")
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
    mon, _ = gc.simulate_variant_c(hours, CFG, cat, {}, {}, rcem, yms[0], yms[-1])
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
        m, p = gc.simulate_variant_c(hours, CFG, cat, {}, {}, {}, ym, ym)
        assert ym in m and p
