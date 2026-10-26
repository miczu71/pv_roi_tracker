"""Testy tariff_catalog.py — katalog stawek Tauron, strefy, okresy, stałe opłaty."""
from datetime import date, datetime

import pytest

from pv_roi_tracker.tariff_catalog import (
    TariffSettings,
    fixed_monthly,
    load_catalog,
    period_for,
    rates_per_kwh,
    zone_for,
)


@pytest.fixture(scope="module")
def cat():
    return load_catalog()


def z(cat, tariff, y, m, d, h):
    return zone_for(tariff, datetime(y, m, d, h), cat)


# ── strefy ───────────────────────────────────────────────────────────────────

def test_g11_zawsze_all(cat):
    assert z(cat, "G11", 2026, 3, 10, 3) == "all"


def test_g12w_granice_dzien_roboczy(cat):
    # wtorek 2026-03-10
    assert z(cat, "G12w", 2026, 3, 10, 5) == "offpeak"
    assert z(cat, "G12w", 2026, 3, 10, 6) == "peak"
    assert z(cat, "G12w", 2026, 3, 10, 12) == "peak"
    assert z(cat, "G12w", 2026, 3, 10, 13) == "offpeak"
    assert z(cat, "G12w", 2026, 3, 10, 14) == "offpeak"
    assert z(cat, "G12w", 2026, 3, 10, 15) == "peak"
    assert z(cat, "G12w", 2026, 3, 10, 21) == "peak"
    assert z(cat, "G12w", 2026, 3, 10, 22) == "offpeak"


def test_g12_przejscie_przez_polnoc(cat):
    assert z(cat, "G12", 2026, 3, 10, 23) == "night"
    assert z(cat, "G12", 2026, 3, 10, 0) == "night"
    assert z(cat, "G12", 2026, 3, 10, 5) == "night"
    assert z(cat, "G12", 2026, 3, 10, 6) == "day"
    assert z(cat, "G12", 2026, 3, 10, 13) == "night"
    assert z(cat, "G12", 2026, 3, 10, 15) == "day"
    assert z(cat, "G12", 2026, 3, 10, 22) == "night"


def test_g12_weekend_bez_zmian(cat):
    # sobota 2026-03-14 godz. 10 nadal "day"
    assert z(cat, "G12", 2026, 3, 14, 10) == "day"


def test_g12w_weekend_cala_doba_offpeak(cat):
    assert z(cat, "G12w", 2026, 3, 14, 10) == "offpeak"
    assert z(cat, "G12w", 2026, 3, 15, 18) == "offpeak"


def test_g13_lato_i_zima(cat):
    # lato (lipiec): poranny 7-13, wieczorny 19-22
    assert z(cat, "G13", 2026, 7, 8, 6) == "other"
    assert z(cat, "G13", 2026, 7, 8, 7) == "morning_peak"
    assert z(cat, "G13", 2026, 7, 8, 13) == "other"
    assert z(cat, "G13", 2026, 7, 8, 18) == "other"
    assert z(cat, "G13", 2026, 7, 8, 19) == "evening_peak"
    assert z(cat, "G13", 2026, 7, 8, 22) == "other"
    # zima (styczeń): wieczorny 16-21
    assert z(cat, "G13", 2026, 1, 14, 16) == "evening_peak"
    assert z(cat, "G13", 2026, 1, 14, 19) == "evening_peak"
    assert z(cat, "G13", 2026, 1, 14, 21) == "other"
    # granica sezonu: marzec zima, kwiecień lato
    assert z(cat, "G13", 2026, 3, 17, 16) == "evening_peak"
    assert z(cat, "G13", 2026, 4, 14, 16) == "other"


def test_g13_weekend_i_swieto_other(cat):
    assert z(cat, "G13", 2026, 3, 14, 8) == "other"   # sobota
    assert z(cat, "G13", 2026, 1, 1, 8) == "other"    # Nowy Rok (czwartek)


def test_swieta_1_listopada_zalezne_od_roku(cat):
    # 1 listopada w dzień roboczy w różnych latach: 2024 piątek, 2027 poniedziałek (święto w obu)
    assert z(cat, "G12w", 2024, 11, 1, 10) == "offpeak"
    assert z(cat, "G12w", 2027, 11, 1, 10) == "offpeak"
    assert z(cat, "G12w", 2025, 11, 1, 10) == "offpeak"   # sobota
    # to samo święto w roku, w którym wypada w środę (2023) — dzień roboczy bez święta byłby peak
    assert z(cat, "G12w", 2023, 11, 1, 10) == "offpeak"
    # dzień wcześniej (czwartek 2024-10-31) normalny dzień roboczy
    assert z(cat, "G12w", 2024, 10, 31, 10) == "peak"


def test_wielkanoc_poniedzialek(cat):
    # Wielkanoc 2026: 5 kwietnia, Poniedziałek Wielkanocny 6.04
    assert z(cat, "G12w", 2026, 4, 6, 10) == "offpeak"
    assert z(cat, "G13", 2026, 4, 6, 8) == "other"
    assert z(cat, "G12w", 2026, 4, 7, 10) == "peak"


def test_nieznana_taryfa(cat):
    with pytest.raises(ValueError):
        zone_for("G99", datetime(2026, 3, 10, 10), cat)


# ── okresy ───────────────────────────────────────────────────────────────────

def test_okres_2025(cat):
    p = period_for(cat, date(2025, 6, 1))
    assert p["confirmed"] is False
    assert p["energy_net"]["G12w"]["peak"] == pytest.approx(0.7591)   # faktury I–VI: 0,7641 z akcyzą


def test_okres_2026_01_dziedziczy_z_override(cat):
    jan = period_for(cat, date(2026, 1, 15))
    feb = period_for(cat, date(2026, 2, 15))
    assert jan["quality_net"] == 0.0331
    assert feb["quality_net"] == 0.0332
    assert jan["confirmed"] is True
    assert jan["energy_net"] == feb["energy_net"]
    assert jan["valid_to"] == "2026-01-31"


def test_okres_granice_dat(cat):
    assert period_for(cat, date(2026, 1, 31))["quality_net"] == 0.0331
    assert period_for(cat, date(2026, 2, 1))["quality_net"] == 0.0332
    assert period_for(cat, date(2025, 12, 31))["confirmed"] is False


def test_okres_brak(cat):
    with pytest.raises(LookupError):
        period_for(cat, date(2024, 12, 31))
    with pytest.raises(LookupError):
        period_for(cat, date(2027, 1, 1))


def test_okres_zwraca_kopie_i_overrides_gleboko(cat):
    p = period_for(cat, date(2026, 3, 1), overrides={"energy_net": {"G11": {"all": 0.9}}})
    assert p["energy_net"]["G11"]["all"] == 0.9
    assert p["energy_net"]["G12"]["day"] == 0.5430  # reszta nietknięta
    # oryginał bez zmian
    assert period_for(cat, date(2026, 3, 1))["energy_net"]["G11"]["all"] == 0.4970
    p["energy_net"]["G11"]["all"] = 123
    assert period_for(cat, date(2026, 3, 1))["energy_net"]["G11"]["all"] == 0.4970


# ── stawki ───────────────────────────────────────────────────────────────────

def test_rates_g12w_2026_03(cat):
    p = period_for(cat, date(2026, 3, 10))
    r = rates_per_kwh("G12w", p, "peak")
    assert r["energy"] == pytest.approx(0.6220)
    assert r["dist"] == pytest.approx(0.3298)
    assert r["fees"] == pytest.approx(0.0332 + 0.0073 + 0.0030 + 0.005)
    r2 = rates_per_kwh("G12w", p, "offpeak")
    assert r2["energy"] + r2["dist"] + r2["fees"] == pytest.approx(0.5127)


def test_rates_null_to_valueerror(cat):
    p = period_for(cat, date(2026, 3, 10), overrides={"energy_net": {"G12w": {"peak": None}}})
    with pytest.raises(ValueError):
        rates_per_kwh("G12w", p, "peak")


def test_rates_nieznana_strefa(cat):
    p = period_for(cat, date(2026, 3, 10))
    with pytest.raises(ValueError):
        rates_per_kwh("G12w", p, "nonsens")


# ── stałe ────────────────────────────────────────────────────────────────────

def test_fixed_domyslne(cat):
    p = period_for(cat, date(2026, 3, 10))
    f = fixed_monthly(p, TariffSettings())
    assert f["network"] == 10.86
    assert f["subscription_dist"] == 4.56
    assert f["capacity"] == 24.05
    assert f["transitional"] == 0.0
    assert f["sales_fee"] == 0.0


def test_fixed_2025_wg_faktur(cat):
    # faktury I–VI 2025: 14,90 (sieciowa + abonament, bez mocowej); VII–XII: 30,91 (z mocową, bez przejściowej)
    s = TariffSettings(phases=3, capacity_bracket="gt2800")
    h1 = fixed_monthly(period_for(cat, date(2025, 3, 15)), s)
    h2 = fixed_monthly(period_for(cat, date(2025, 8, 15)), s)
    assert sum(h1.values()) - h1["sales_fee"] == pytest.approx(14.90)
    assert h1["capacity"] == 0 and h1["transitional"] == 0
    assert sum(h2.values()) - h2["sales_fee"] == pytest.approx(30.91)
    assert h2["transitional"] == 0


def test_okres_2025_dzieli_sie_na_dwa_pol_roczne(cat):
    h1 = period_for(cat, date(2025, 6, 30))
    h2 = period_for(cat, date(2025, 7, 1))
    assert h1["valid_to"] == "2025-06-30" and h2["valid_from"] == "2025-07-01"
    assert h1["confirmed"] is False and h2["confirmed"] is False
    assert h1["energy_net"]["G12"] == h2["energy_net"]["G12"]   # inne taryfy niż G12w bez zmian względem II połowy


def test_fixed_1faza_i_przejsciowa(cat):
    p = period_for(cat, date(2025, 6, 1))
    p["fixed_net"]["capacity_by_annual_kwh"] = {"lt500": 2.86, "500_1200": 6.86, "1200_2800": 11.44, "gt2800": 16.01}
    p["fixed_net"]["transitional_by_annual_kwh"] = {"lt500": 0.02, "500_1200": 0.1, "gt1200": 0.33}
    f = fixed_monthly(p, TariffSettings(phases=1, capacity_bracket="1200_2800", sales_fee_net=5.0))
    assert f["network"] == 7.02
    assert f["capacity"] == 11.44
    assert f["transitional"] == 0.33   # 1200_2800 -> gt1200
    assert f["sales_fee"] == 5.0
    f2 = fixed_monthly(p, TariffSettings(capacity_bracket="lt500"))
    assert f2["transitional"] == 0.02
    f3 = fixed_monthly(p, TariffSettings(capacity_bracket="500_1200"))
    assert f3["transitional"] == 0.10
    f4 = fixed_monthly(p, TariffSettings(capacity_bracket="gt2800"))
    assert f4["transitional"] == 0.33


def test_okres_2025_granica_09_30_10_01(cat):
    q3 = period_for(cat, date(2025, 9, 30))
    q4 = period_for(cat, date(2025, 10, 1))
    assert q3["energy_net"]["G12w"]["offpeak"] == pytest.approx(0.50)   # faktury VII–IX: 0,505 płasko z akcyzą
    assert q4["energy_net"]["G12w"]["offpeak"] == pytest.approx(0.430)  # faktura X: 0,761/0,435 z akcyzą
    assert q4["valid_from"] == "2025-10-01" and q4["valid_to"] == "2025-10-31"
    assert q4["excise_net"] == 0.005 and q4["confirmed"] is False


def test_okres_2025_listopad_grudzien_plasko(cat):
    nov = period_for(cat, date(2025, 11, 1))
    assert nov["valid_from"] == "2025-11-01" and nov["valid_to"] == "2025-12-31"
    assert nov["energy_net"]["G12w"] == {"peak": 0.50, "offpeak": 0.50}   # faktury XI–XII: 0,505 płasko z akcyzą


def test_rates_brak_akcyzy_to_valueerror(cat):
    p = period_for(cat, date(2026, 3, 10), overrides={"excise_net": None})
    with pytest.raises(ValueError):
        rates_per_kwh("G12w", p, "peak")



# ── G13s (E1: oferta „Tanie godziny”) ────────────────────────────────────────

G13S_ENERGY = {
    'summer_workday': (0.2700, 0.7042, 0.5000),
    'summer_dayoff': (0.1080, 0.2817, 0.5000),
    'winter_workday': (0.5500, 0.7042, 0.4900),
    'winter_dayoff': (0.3300, 0.4225, 0.4900),
}
G13S_DIST = {
    'summer_workday': (0.1000, 0.2842, 0.1094),
    'summer_dayoff': (0.0400, 0.1176, 0.1094),
    'winter_workday': (0.1999, 0.3332, 0.1094),
    'winter_dayoff': (0.1200, 0.1960, 0.1094),
}


def test_g13s_granice_lato(cat):
    assert z(cat, "G13s", 2026, 7, 15, 8) == "summer_workday_peak"
    assert z(cat, "G13s", 2026, 7, 15, 9) == "summer_workday_offpeak"
    assert z(cat, "G13s", 2026, 7, 15, 16) == "summer_workday_offpeak"
    assert z(cat, "G13s", 2026, 7, 15, 17) == "summer_workday_peak"
    assert z(cat, "G13s", 2026, 7, 15, 20) == "summer_workday_peak"
    assert z(cat, "G13s", 2026, 7, 15, 21) == "summer_workday_night"
    assert z(cat, "G13s", 2026, 7, 15, 6) == "summer_workday_night"
    assert z(cat, "G13s", 2026, 7, 15, 7) == "summer_workday_peak"


def test_g13s_granice_zima(cat):
    assert z(cat, "G13s", 2026, 1, 15, 9) == "winter_workday_peak"
    assert z(cat, "G13s", 2026, 1, 15, 10) == "winter_workday_offpeak"
    assert z(cat, "G13s", 2026, 1, 15, 14) == "winter_workday_offpeak"
    assert z(cat, "G13s", 2026, 1, 15, 15) == "winter_workday_peak"
    assert z(cat, "G13s", 2026, 1, 15, 20) == "winter_workday_peak"
    assert z(cat, "G13s", 2026, 1, 15, 21) == "winter_workday_night"


def test_g13s_przelom_sezonow_31_03_01_04(cat):
    assert z(cat, "G13s", 2026, 3, 31, 23) == "winter_workday_night"
    assert z(cat, "G13s", 2026, 3, 31, 12) == "winter_workday_offpeak"
    assert z(cat, "G13s", 2026, 4, 1, 0) == "summer_workday_night"
    assert z(cat, "G13s", 2026, 4, 1, 12) == "summer_workday_offpeak"


def test_g13s_weekend_i_swieto_dayoff(cat):
    assert z(cat, "G13s", 2026, 3, 14, 12) == "winter_dayoff_offpeak"   # sobota
    assert z(cat, "G13s", 2026, 3, 15, 18) == "winter_dayoff_peak"      # niedziela, pasmo 18:00 = szczyt
    assert z(cat, "G13s", 2026, 11, 11, 12) == "winter_dayoff_offpeak"  # święto


def test_g13s_dst_bez_wyjatkow(cat):
    for dt in (datetime(2026, 3, 29, 2), datetime(2026, 3, 29, 3),
               datetime(2026, 10, 25, 2), datetime(2026, 10, 25, 3)):
        assert zone_for("G13s", dt, cat).startswith("winter_dayoff_")


@pytest.mark.parametrize("key", [f"{s}_{d}_{b}" for s in ("summer", "winter")
                                 for d in ("workday", "dayoff") for b in ("offpeak", "peak", "night")])
def test_g13s_stawki_z_tabel(cat, key):
    season_daytype, band = key.rsplit("_", 1)
    p = period_for(cat, date(2026, 3, 10), None)
    r = rates_per_kwh("G13s", p, key)
    idx = ("offpeak", "peak", "night").index(band)
    assert r["energy"] == pytest.approx(G13S_ENERGY[season_daytype][idx])
    assert r["dist"] == pytest.approx(G13S_DIST[season_daytype][idx])
    assert r["fees"] == pytest.approx(0.0332 + 0.0073 + 0.0030 + 0.005)


def test_g13s_fees_styczen_jakosciowa(cat):
    p = period_for(cat, date(2026, 1, 15), None)
    assert rates_per_kwh("G13s", p, "winter_workday_peak")["fees"] == pytest.approx(
        0.0331 + 0.0073 + 0.0030 + 0.005)


def test_g13s_oplata_handlowa_domyslnie_19_10(cat):
    p = period_for(cat, date(2026, 3, 10), None)
    assert fixed_monthly(p, TariffSettings(), "G13s")["sales_fee"] == pytest.approx(19.10)


def test_g13s_oplata_handlowa_nadpisana(cat):
    p = period_for(cat, date(2026, 3, 10), None)
    s = TariffSettings(sales_fee_g13s_net=10.0)
    assert fixed_monthly(p, s, "G13s")["sales_fee"] == pytest.approx(10.0)


def test_g13s_globalna_oplata_nie_sumuje_sie(cat):
    p = period_for(cat, date(2026, 3, 10), None)
    s = TariffSettings(sales_fee_net=5.0)
    assert fixed_monthly(p, s, "G13s")["sales_fee"] == pytest.approx(19.10)
    assert fixed_monthly(p, s, "G12w")["sales_fee"] == pytest.approx(5.0)
    assert fixed_monthly(p, s)["sales_fee"] == pytest.approx(5.0)


def test_g13s_przed_2026_brak_oplaty_valueerror(cat):
    p = period_for(cat, date(2025, 3, 10), None)
    with pytest.raises(ValueError):
        fixed_monthly(p, TariffSettings(), "G13s")
