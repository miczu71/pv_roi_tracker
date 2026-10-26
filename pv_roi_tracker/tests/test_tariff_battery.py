"""Testy tariff_battery.py — wariant B: magazyn przeliczony pod strefy i ceny każdej taryfy."""
import pytest

from pv_roi_tracker import battery_sim
from pv_roi_tracker import tariff_battery as tb
from pv_roi_tracker.battery_sim import BatteryConfig
from pv_roi_tracker.tariff_catalog import load_catalog, period_for
from datetime import date

MON = "2026-06-01"   # poniedziałek
SAT = "2026-06-06"
VAT = 1.23
FEES = 0.0332 + 0.0073 + 0.003 + 0.005   # w tym akcyza


@pytest.fixture(scope="module")
def cat():
    return load_catalog()


def test_g12w_szczyt_vs_dolina(cat):
    fn = tb.make_price_fn("G12w", cat, {}, {})
    assert fn(f"{MON}T10") == (pytest.approx((0.622 + 0.3298 + FEES) * VAT), True)
    assert fn(f"{MON}T14") == (pytest.approx((0.413 + 0.0512 + FEES) * VAT), False)
    assert fn(f"{SAT}T10") == (pytest.approx((0.413 + 0.0512 + FEES) * VAT), False)


def test_g11_jednakowa_cena_bez_szczytu(cat):
    fn = tb.make_price_fn("G11", cat, {}, {})
    exp = (0.497 + 0.2464 + FEES) * VAT
    assert fn(f"{MON}T10") == (pytest.approx(exp), False)
    assert fn(f"{MON}T03") == (pytest.approx(exp), False)


def test_g13_flaga_szczytu(cat):
    fn = tb.make_price_fn("G13", cat, {}, {})
    assert fn(f"{MON}T08")[1] is True      # morning_peak
    assert fn(f"{MON}T20")[1] is True      # evening_peak (lato 19-22)
    assert fn(f"{MON}T15")[1] is False     # other
    assert fn(f"{MON}T08")[0] == pytest.approx((0.4668 + 0.2203 + FEES) * VAT)


def test_dyn_a_ceny_rdn(cat):
    d = "2026-04-14"
    fn = tb.make_price_fn("dyn_a", cat, {f"{d}T10": 400.0}, {})
    assert fn(f"{d}T10") == (pytest.approx((0.4 + 0.0892 + 0.3298 + FEES) * VAT), False)


def test_dyn_a_fallback_tydzien_wczesniej(cat):
    rdn = {"2026-04-07T12": 300.0}
    fn = tb.make_price_fn("dyn_a", cat, rdn, {})
    p = fn("2026-04-14T12")
    assert p is not None and p[0] == pytest.approx((0.3 + 0.0892 + 0.3298 + FEES) * VAT)
    assert fn("2026-04-14T13") is None


def test_dyn_b_strefa_kompas(cat):
    fn = tb.make_price_fn("dyn_b", cat, {}, {f"{MON}T19": "S4"})
    assert fn(f"{MON}T19") == (pytest.approx((0.502 + 2.3756 + FEES) * VAT), False)
    assert fn(f"{MON}T10") is None


def _hours():
    # eksport rano, import po południu i wieczorem (poniedziałek)
    return {f"{MON}T10": (3.0, 0.0), f"{MON}T11": (3.0, 0.0),
            f"{MON}T18": (0.0, 2.0), f"{MON}T19": (0.0, 2.0),
            "2025-12-10T10": (3.0, 0.0), "2025-12-10T18": (0.0, 2.0),
            "2027-02-01T10": (3.0, 0.0)}


RCEM = {"2026-06": 0.30}


def test_variant_b_g12w_rowna_s1_z_katalogu_arbitraz_wylaczony(cat):
    cfg = BatteryConfig(arbitrage_enabled=True)
    hours = _hours()
    res = tb.simulate_variant_b(hours, cfg, cat, {}, {}, RCEM, "2026-06", "2026-06")
    assert set(res) == {"2026-06"}
    assert {"G11", "G12", "G12w", "G13"} <= set(res["2026-06"])
    p = period_for(cat, date(2026, 6, 1))
    peak = (p["energy_net"]["G12w"]["peak"] + p["dist_var_net"]["G12w"]["peak"] + FEES) * VAT
    off = (p["energy_net"]["G12w"]["offpeak"] + p["dist_var_net"]["G12w"]["offpeak"] + FEES) * VAT
    cfg_noarb = BatteryConfig(arbitrage_enabled=False)
    sub = {k: v for k, v in hours.items() if k.startswith("2026-06")}
    ref = battery_sim.simulate_s1(sub, cfg_noarb, {"2026-06": (peak, off)}, RCEM)[0]
    got = res["2026-06"]["G12w"]
    assert got["savings_pln"] == pytest.approx(ref["savings_pln"])
    assert got["charged_kwh"] == pytest.approx(ref["charged_kwh"])
    assert got["discharged_kwh"] == pytest.approx(ref["discharged_kwh"])
    assert got["savings_pln"] > 0


def test_variant_b_zakres_miesiecy(cat):
    res = tb.simulate_variant_b(_hours(), BatteryConfig(), cat, {}, {}, RCEM,
                                "2025-12", "2026-06")
    assert set(res) == {"2025-12", "2026-06"}


def test_variant_b_dyn_bez_cen_brak_wpisu(cat):
    hours = {f"{MON}T10": (3.0, 0.0), f"{MON}T18": (0.0, 2.0)}
    res = tb.simulate_variant_b(hours, BatteryConfig(), cat, {}, {}, RCEM, "2026-06", "2026-06")
    assert "dyn_a" not in res["2026-06"] and "dyn_b" not in res["2026-06"]
    assert "G11" in res["2026-06"]


def test_blad_katalogu_nie_jest_cicho_pomijany(cat):
    fn = tb.make_price_fn("G12w", cat, {}, {})
    with pytest.raises(LookupError):
        fn("2031-06-01T10")      # poza zakresem cennika


def test_brak_marzy_dyn_a_to_blad(cat):
    import copy
    bad = copy.deepcopy(cat)
    bad["dynamic_sales"]["margin_net_per_kwh"] = None
    with pytest.raises(ValueError):
        tb.make_price_fn("dyn_a", bad, {}, {})


# ── G13s (E1) ────────────────────────────────────────────────────────────────

def test_g13s_price_fn_przed_i_po_dostepnosci(cat):
    fn = tb.make_price_fn("G13s", cat, None, None)
    assert fn("2025-12-31T12") is None
    price = fn("2026-01-05T12")          # poniedziałek, zima, pozaszczyt
    assert price is not None
    assert price[0] == pytest.approx((0.5500 + 0.1999 + 0.0331 + 0.0073 + 0.0030 + 0.005) * 1.23)
    assert price[1] is False


def test_g13s_szczyt_zima_is_peak(cat):
    fn = tb.make_price_fn("G13s", cat, None, None)
    assert fn("2026-01-05T16")[1] is True     # winter_workday_peak
    assert fn("2026-01-05T03")[1] is False    # winter_workday_night


def test_variant_b_soc_floor_zmniejsza_pojemnosc(cat):
    cfg = BatteryConfig()
    hours = {f"{MON}T{h:02d}": (2.5, 0.0) for h in range(8, 14)}
    hours.update({f"{MON}T{h:02d}": (0.0, 2.5) for h in range(17, 21)})
    full = tb.simulate_variant_b(hours, cfg, cat, {}, {}, RCEM, "2026-06", "2026-06")
    default = tb.simulate_variant_b(hours, cfg, cat, {}, {}, RCEM, "2026-06", "2026-06", soc_floor=0.0)
    floor = tb.simulate_variant_b(hours, cfg, cat, {}, {}, RCEM, "2026-06", "2026-06", soc_floor=0.16)
    assert default == full
    assert floor["2026-06"]["G12w"]["charged_kwh"] < full["2026-06"]["G12w"]["charged_kwh"]
    assert floor["2026-06"]["G12w"]["charged_kwh"] == pytest.approx(4.2 / (0.92 ** 0.5))
