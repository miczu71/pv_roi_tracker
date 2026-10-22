"""Testy tariff_sim.py — symulacja kosztu tego samego poboru w kilku taryfach."""
import copy

import pytest

from pv_roi_tracker.tariff_catalog import TariffSettings, load_catalog
from pv_roi_tracker.tariff_sim import simulate_month, simulate_range

VAT = 1.23
FEES26 = 0.0332 + 0.0073 + 0.0030 + 0.005   # w tym akcyza
# stałe 2026-02+ (3 fazy, gt2800): 10.86 + 4.56 + 24.05
FIXED26 = 10.86 + 4.56 + 24.05


@pytest.fixture(scope="module")
def cat():
    return load_catalog()


def test_regresja_g12w_10_i_14(cat):
    # wtorek 2026-03-10: 10:00 peak, 14:00 offpeak
    imp = {"2026-03-10T10": 1.0, "2026-03-10T14": 1.0}
    r = simulate_month("2026-03", imp, cat)["G12w"]
    peak = 0.6220 + 0.3298 + FEES26
    off = 0.4130 + 0.0512 + FEES26
    assert peak == pytest.approx(1.0003, abs=1e-4)
    assert off == pytest.approx(0.5127, abs=1e-4)
    assert r["energy"] == pytest.approx(0.6220 + 0.4130)
    assert r["dist"] == pytest.approx(0.3298 + 0.0512)
    assert r["fees"] == pytest.approx(2 * FEES26)
    assert r["fixed"] == pytest.approx(FIXED26)
    assert r["net"] == pytest.approx(peak + off + FIXED26)
    assert r["gross"] == pytest.approx(r["net"] * VAT)
    assert r["vat"] == pytest.approx(r["gross"] - r["net"])
    assert r["kwh"] == pytest.approx(2.0)
    assert r["confirmed"] is True
    assert r["coverage_pct"] == 100
    assert r["missing_kwh"] == 0
    assert peak * VAT == pytest.approx(1.2304, abs=1e-4)   # faktury: 1,23 zł brutto


def test_taryfy_klucze_i_g11(cat):
    out = simulate_month("2026-03", {"2026-03-10T10": 2.0}, cat)
    assert set(out) == {"G11", "G12", "G12w", "G13", "dyn_a", "dyn_b"}
    g11 = out["G11"]
    assert g11["energy"] == pytest.approx(2 * 0.4970)
    assert g11["dist"] == pytest.approx(2 * 0.2464)


def test_pobor_poza_miesiacem_ignorowany(cat):
    out = simulate_month("2026-03", {"2026-03-10T10": 1.0, "2026-04-10T10": 5.0}, cat)
    assert out["G11"]["kwh"] == pytest.approx(1.0)


def test_brak_poboru_same_stale(cat):
    r = simulate_month("2026-03", {}, cat)["G11"]
    assert r["kwh"] == 0
    assert r["fixed"] == pytest.approx(FIXED26)
    assert r["coverage_pct"] == 100


def test_settings_wplywaja_na_stale(cat):
    s = TariffSettings(phases=1, capacity_bracket="lt500", sales_fee_net=3.0)
    r = simulate_month("2026-03", {}, cat, settings=s)["G12"]
    assert r["fixed"] == pytest.approx(7.38 + 4.56 + 4.29 + 3.0)


def test_g13_przelom_lato_zima(cat):
    # 16:00 w dniu roboczym: marzec (zima) = evening_peak, kwiecień (lato) = other
    mar = simulate_month("2026-03", {"2026-03-17T16": 1.0}, cat)["G13"]
    apr = simulate_month("2026-04", {"2026-04-14T16": 1.0}, cat)["G13"]
    assert mar["energy"] == pytest.approx(0.7780)
    assert apr["energy"] == pytest.approx(0.4210)
    assert mar["dist"] == pytest.approx(0.3898)
    assert apr["dist"] == pytest.approx(0.0392)


def test_styczen_vs_luty_jakosciowa(cat):
    jan = simulate_month("2026-01", {"2026-01-14T10": 1.0}, cat)["G11"]
    feb = simulate_month("2026-02", {"2026-02-12T10": 1.0}, cat)["G11"]
    assert jan["fees"] == pytest.approx(0.0331 + 0.0073 + 0.0030 + 0.005)
    assert feb["fees"] == pytest.approx(0.0332 + 0.0073 + 0.0030 + 0.005)


def test_2025_niepotwierdzony_i_przejsciowa(cat):
    r = simulate_month("2025-06", {"2025-06-10T10": 1.0}, cat)["G11"]
    assert r["confirmed"] is False
    assert r["energy"] == pytest.approx(0.50)
    assert r["fixed"] == pytest.approx(10.34 + 4.56)   # faktury I–VI 2025: bez mocowej i przejściowej
    h2 = simulate_month("2025-08", {"2025-08-12T10": 1.0}, cat)["G11"]
    assert h2["fixed"] == pytest.approx(10.34 + 4.56 + 16.01)   # VII–XII: z mocową, bez przejściowej


def test_2025_energia_nizsza_z_ceny_strefy_i_maksymalnej(cat):
    # I–IX 2025: min(cena strefy z pkt 5.1a; 0,50); Q4 2025: płasko 0,50 (zgodność z fakturami)
    def energy(tariff, ts):
        return simulate_month(ts[:7], {ts: 1.0}, cat)[tariff]["energy"]
    assert energy("G12w", "2025-08-12T10") == pytest.approx(0.50)     # wtorek, szczyt
    assert energy("G12w", "2025-08-12T14") == pytest.approx(0.4620)   # pozaszczyt
    assert energy("G12", "2025-08-12T23") == pytest.approx(0.4620)
    assert energy("G13", "2025-08-12T14") == pytest.approx(0.50)
    assert energy("G12w", "2025-11-12T14") == pytest.approx(0.50)     # Q4: płasko 0,50 wg faktur
    assert energy("G13", "2025-11-12T14") == pytest.approx(0.50)
    assert energy("G11", "2025-11-12T14") == pytest.approx(0.50)


# ── dyn_a ────────────────────────────────────────────────────────────────────

MARGIN = 0.0892
D = "2026-04-14"   # wtorek


def test_dyn_a_srednia_wazona_i_strefa_g12w(cat):
    # 10:00 peak G12w (1 kWh, RDN 400), 14:00 offpeak (3 kWh, RDN 200)
    rdn = {f"{D}T10": 400.0, f"{D}T14": 200.0}
    imp = {f"{D}T10": 1.0, f"{D}T14": 3.0}
    r = simulate_month("2026-04", imp, cat, rdn=rdn)["dyn_a"]
    avg = (1 * 0.4 + 3 * 0.2) / 4 + MARGIN
    assert r["energy"] == pytest.approx(4 * avg)
    assert r["dist"] == pytest.approx(0.3298 + 3 * 0.0512)
    assert r["fees"] == pytest.approx(4 * FEES26)
    assert r["fixed"] == pytest.approx(FIXED26)
    assert r["coverage_pct"] == 100
    assert r["reliable"] is True
    assert r["price_fallback_kwh"] == 0


def test_dyn_a_ujemne_godziny_obnizaja_srednia(cat):
    # godzina -100 PLN/MWh (1 kWh) i 300 (1 kWh): średnia = 0,1 + marża, bez podłogi godzinowej
    rdn = {f"{D}T13": -100.0, f"{D}T14": 300.0}
    imp = {f"{D}T13": 1.0, f"{D}T14": 1.0}
    r = simulate_month("2026-04", imp, cat, rdn=rdn)["dyn_a"]
    assert r["energy"] == pytest.approx(2 * (0.1 + MARGIN))


def test_dyn_a_podloga_dotyczy_sredniej_miesiecznej(cat):
    # średnia = -0,2 + 0,0892 < 0 -> koszt energii wg podłogi 0,0050 zł/kWh
    r = simulate_month("2026-04", {f"{D}T13": 2.0}, cat, rdn={f"{D}T13": -200.0})["dyn_a"]
    assert r["energy"] == pytest.approx(2 * 0.0050)
    # średnia dodatnia powyżej podłogi nie jest zmieniana
    r2 = simulate_month("2026-04", {f"{D}T13": 2.0}, cat, rdn={f"{D}T13": 100.0})["dyn_a"]
    assert r2["energy"] == pytest.approx(2 * (0.1 + MARGIN))


def test_dyn_a_srednia_zaokraglona_do_5_miejsc(cat):
    r = simulate_month("2026-04", {f"{D}T13": 1.0}, cat, rdn={f"{D}T13": 123.456789})["dyn_a"]
    assert r["energy"] == pytest.approx(round(0.123456789 + MARGIN, 5), abs=1e-12)


def test_dyn_a_niedostepne_przed_2026_04(cat):
    exp = {"available": False,
           "reason": "cennik dynamiczny od 2026-04-01; wcześniejsze wersje nieustalone"}
    r = simulate_month("2026-03", {"2026-03-10T10": 1.0}, cat, rdn={"2026-03-10T10": 400.0})
    assert r["dyn_a"] == exp
    assert r["G12w"]["kwh"] == 1.0
    assert simulate_month("2025-10", {}, cat)["dyn_a"] == exp
    assert "available" not in simulate_month("2026-04", {}, cat)["dyn_a"]


def test_dyn_a_fallback_tydzien_wstecz(cat):
    prev = "2026-04-07"   # -7 dni od D
    imp = {f"{D}T10": 2.0}
    r = simulate_month("2026-04", imp, cat, rdn={f"{prev}T10": 400.0})["dyn_a"]
    assert r["price_fallback_kwh"] == pytest.approx(2.0)
    assert r["missing_kwh"] == 0
    assert r["coverage_pct"] == 100
    assert r["energy"] == pytest.approx(2 * (0.4 + MARGIN))


def test_dyn_a_fallback_przez_granice_miesiaca(cat):
    imp = {"2026-04-02T10": 1.0}
    r = simulate_month("2026-04", imp, cat, rdn={"2026-03-26T10": 400.0})["dyn_a"]
    assert r["price_fallback_kwh"] == pytest.approx(1.0)


def test_dyn_a_brak_ceny_missing_i_niewiarygodne(cat):
    imp = {f"{D}T10": 1.0, f"{D}T11": 9.0}
    r = simulate_month("2026-04", imp, cat, rdn={f"{D}T10": 400.0})["dyn_a"]
    assert r["missing_kwh"] == pytest.approx(9.0)
    assert r["coverage_pct"] == pytest.approx(10.0)
    assert r["reliable"] is False
    assert r["price_fallback_kwh"] == 0
    # niepoliczona godzina nie dolicza się po cichu jako 0 zł energii/dystrybucji
    assert r["energy"] == pytest.approx(0.4 + MARGIN)
    assert r["dist"] == pytest.approx(0.3298)
    assert r["kwh"] == pytest.approx(10.0)


def test_dyn_a_brak_rdn_w_ogole(cat):
    r = simulate_month("2026-04", {f"{D}T10": 1.0}, cat)["dyn_a"]
    assert r["missing_kwh"] == pytest.approx(1.0)
    assert r["coverage_pct"] == 0
    assert r["reliable"] is False
    assert r["energy"] == 0


def test_dyn_a_pokrycie_96_wiarygodne(cat):
    imp = {f"{D}T10": 96.0, f"{D}T11": 4.0}
    r = simulate_month("2026-04", imp, cat, rdn={f"{D}T10": 400.0})["dyn_a"]
    assert r["coverage_pct"] == pytest.approx(96.0)
    assert r["reliable"] is True


def test_dst_jesien_dwie_godziny_w_jednym_kluczu(cat):
    # 2026-10-25: 02:00 występuje dwa razy; wywołujący sumuje obie godziny w jeden klucz
    # (niedziela -> offpeak). Wynik ma być równy sumie kWh obu godzin.
    first, second = 1.5, 2.0
    imp: dict = {}
    for kwh in (first, second):
        imp["2026-10-25T02"] = imp.get("2026-10-25T02", 0.0) + kwh
    assert imp["2026-10-25T02"] == pytest.approx(first + second)
    r = simulate_month("2026-10", imp, cat, rdn={"2026-10-25T02": 300.0})["dyn_a"]
    assert r["kwh"] == pytest.approx(first + second)
    assert r["energy"] == pytest.approx((first + second) * (0.3 + MARGIN))
    assert r["missing_kwh"] == 0


def test_dyn_a_confirmed_wymaga_potwierdzonej_oferty(cat):
    imp = {f"{D}T10": 1.0}
    rdn = {f"{D}T10": 400.0}
    assert simulate_month("2026-04", imp, cat, rdn=rdn)["dyn_a"]["confirmed"] is True
    c2 = copy.deepcopy(cat)
    c2["dynamic_sales"]["confirmed"] = False
    assert simulate_month("2026-04", imp, c2, rdn=rdn)["dyn_a"]["confirmed"] is False


# ── dyn_b ────────────────────────────────────────────────────────────────────

def test_dyn_b_g11_i_strefy_s(cat):
    imp = {"2026-03-10T10": 1.0, "2026-03-10T19": 2.0}
    kompas = {"2026-03-10T10": "S1", "2026-03-10T19": "S4"}
    r = simulate_month("2026-03", imp, cat, kompas=kompas)["dyn_b"]
    assert r["energy"] == pytest.approx(3 * 0.5020)       # stała cena oferty, nie G11
    assert r["dist"] == pytest.approx(0.0224 + 2 * 2.3756)
    assert r["fees"] == pytest.approx(3 * FEES26)
    assert r["fixed"] == pytest.approx(FIXED26)
    assert r["confirmed"] is True     # okres 2026-02+ potwierdzony i cena oferty potwierdzona
    assert r["coverage_pct"] == 100
    assert r["reliable"] is True


def test_dyn_b_cena_niezalezna_od_strefy(cat):
    imp = {"2026-03-10T10": 1.0, "2026-03-10T11": 1.0}
    k = {"2026-03-10T10": "S1", "2026-03-10T11": "S3"}
    r = simulate_month("2026-03", imp, cat, kompas=k)["dyn_b"]
    assert r["energy"] == pytest.approx(2 * 0.5020)


def test_dyn_b_niedostepne_przed_2026(cat):
    r = simulate_month("2025-12", {"2025-12-10T10": 1.0}, cat,
                       kompas={"2025-12-10T10": "S1"})["dyn_b"]
    assert r == {"available": False, "reason": "oferta od 2026-01-01"}
    # pozostałe taryfy liczone normalnie
    assert simulate_month("2025-12", {"2025-12-10T10": 1.0}, cat)["G11"]["kwh"] == 1.0


def test_dyn_b_niepotwierdzona_gdy_cena_niepotwierdzona(cat):
    c = copy.deepcopy(cat)
    c["dynamic_sales_b"]["confirmed"] = False
    r = simulate_month("2026-03", {"2026-03-10T10": 1.0}, c,
                       kompas={"2026-03-10T10": "S1"})["dyn_b"]
    assert r["confirmed"] is False


def test_dyn_b_override_ceny_ma_pierwszenstwo(cat):
    ov = {"dynamic_sales_b": {"fixed_price_net_per_kwh": 0.60}}
    r = simulate_month("2026-03", {"2026-03-10T10": 1.0}, cat,
                       kompas={"2026-03-10T10": "S2"}, overrides=ov)["dyn_b"]
    assert r["energy"] == pytest.approx(0.60)
    assert r["dist"] == pytest.approx(0.0893)


def test_dyn_b_brak_strefy(cat):
    imp = {"2026-03-10T10": 1.0, "2026-03-10T11": 1.0}
    r = simulate_month("2026-03", imp, cat, kompas={"2026-03-10T10": "S3"})["dyn_b"]
    assert r["missing_kwh"] == pytest.approx(1.0)
    assert r["coverage_pct"] == pytest.approx(50.0)
    assert r["reliable"] is False


# ── zakres ───────────────────────────────────────────────────────────────────

def test_simulate_range(cat):
    imp = {"2026-01-14T10": 1.0, "2026-02-12T10": 1.0, "2026-03-10T10": 1.0}
    out = simulate_range("2026-01", "2026-03", imp, cat)
    assert list(out) == ["2026-01", "2026-02", "2026-03"]
    assert out["2026-01"]["G11"]["kwh"] == pytest.approx(1.0)
    assert out["2026-02"]["G11"]["fees"] == pytest.approx(FEES26)


def test_simulate_range_przez_rok(cat):
    out = simulate_range("2025-11", "2026-02", {}, cat)
    assert list(out) == ["2025-11", "2025-12", "2026-01", "2026-02"]
