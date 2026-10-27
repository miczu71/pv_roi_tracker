"""Testy tariff_job.py — payload symulacji taryf: 12 mies., ranking, rozjazd z fakturami."""
import json
from datetime import date

import pytest

from pv_roi_tracker import tariff_battery
from pv_roi_tracker import tariff_job as tj
from pv_roi_tracker.battery_sim import BatteryConfig
from pv_roi_tracker.tariff_catalog import TariffSettings, load_catalog

TODAY = date(2026, 10, 20)
L12 = [f"2025-{m:02d}" for m in (10, 11, 12)] + [f"2026-{m:02d}" for m in range(1, 10)]


@pytest.fixture(scope="module")
def cat():
    return load_catalog()


def _entry(gross, net=None, **extra):
    d = {"gross": gross, "net": net if net is not None else gross / 1.23, "kwh": 100.0}
    d.update(extra)
    return d


def _fake_months(per_tariff, months):
    """per_tariff: {taryfa: callable(ym)->dict}; zwraca {ym: {taryfa: wpis}}."""
    return {ym: {t: f(ym) for t, f in per_tariff.items()} for ym in months}


def _patch_range(monkeypatch, per_tariff):
    def fake(from_ym, to_ym, *a, **k):
        yms = []
        y, m = int(from_ym[:4]), int(from_ym[5:7])
        while (y, m) <= (int(to_ym[:4]), int(to_ym[5:7])):
            yms.append(f"{y:04d}-{m:02d}")
            m += 1
            if m > 12:
                y, m = y + 1, 1
        return _fake_months(per_tariff, yms)
    monkeypatch.setattr(tj, "simulate_range", fake)


def _base(g11=120.0, g12=110.0, g12w=100.0, g13=130.0):
    return {
        "G11": lambda ym: _entry(g11), "G12": lambda ym: _entry(g12),
        "G12w": lambda ym: _entry(g12w), "G13": lambda ym: _entry(g13),
        "dyn_a": lambda ym: _entry(90.0, reliable=True),
        "dyn_b": lambda ym: _entry(95.0, reliable=True),
    }


def run(cat, invoices=None, imp=None):
    return tj.compute_payload(imp or {}, cat, TariffSettings(), {}, {}, invoices or {}, TODAY)


# ── import_series ────────────────────────────────────────────────────────────

def test_import_series_bierze_indeks_1():
    assert tj.import_series({"2026-03-10T10": (1.0, 2.5), "2026-03-10T11": (0.0, None)}) == {
        "2026-03-10T10": 2.5}


# ── 12 miesięcy i ranking ────────────────────────────────────────────────────

def test_last12_pelne_miesiace_i_ranking(monkeypatch, cat):
    pt = _base()
    pt["dyn_a"] = pt["dyn_b"] = lambda ym: {"available": False, "reason": "x"}
    _patch_range(monkeypatch, pt)
    p = run(cat)
    assert p["last12"]["from"] == "2025-10" and p["last12"]["to"] == "2026-09"
    assert p["last12"]["gross"]["G11"] == pytest.approx(12 * 120.0)
    assert p["last12"]["gross"]["G12w"] == pytest.approx(12 * 100.0)
    assert p["last12"]["net"]["G12w"] == pytest.approx(12 * 100.0 / 1.23)
    assert p["last12"]["cheapest"] == "G12w"
    assert p["last12"]["saving_vs_g12w"] == 0.0


def test_najtansza_i_oszczednosc(monkeypatch, cat):
    _patch_range(monkeypatch, _base(g12=80.0))
    l12 = run(cat)["last12"]
    # dyn_a (90) i dyn_b (95) są wiarygodne i dostępne, ale G12 (80) jest tańsza
    assert l12["cheapest"] == "G12"
    assert l12["saving_vs_g12w"] == pytest.approx(12 * 20.0)


def test_niedostepna_taryfa_w_ktoryms_miesiacu_poza_rankingiem(monkeypatch, cat):
    pt = _base()
    pt["dyn_a"] = lambda ym: ({"available": False, "reason": "x"} if ym == "2026-01"
                              else _entry(10.0, reliable=True))
    _patch_range(monkeypatch, pt)
    l12 = run(cat)["last12"]
    assert l12["gross"]["dyn_a"] is None and l12["net"]["dyn_a"] is None
    assert l12["cheapest"] == "dyn_b"


def test_niewiarygodna_taryfa_ma_sume_none(monkeypatch, cat):
    pt = _base()
    pt["dyn_b"] = lambda ym: _entry(1.0, reliable=(ym != "2026-05"))
    _patch_range(monkeypatch, pt)
    l12 = run(cat)["last12"]
    assert l12["gross"]["dyn_b"] is None
    assert l12["cheapest"] == "dyn_a"


def test_miesiac_niedostepny_zostaje_w_months(monkeypatch, cat):
    pt = _base()
    pt["dyn_a"] = lambda ym: {"available": False, "reason": "x"}
    _patch_range(monkeypatch, pt)
    p = run(cat)
    assert p["months"]["2025-10"]["dyn_a"] == {"available": False, "reason": "x"}
    assert p["tariffs"] == ["G11", "G12", "G12w", "G13", "G13s", "dyn_a", "dyn_b"]
    assert p["from"] == "2025-01" and p["to"] == "2026-10"   # katalog zaczyna się od 2025-01


# ── bieżący miesiąc ──────────────────────────────────────────────────────────

def test_biezacy_miesiac_czesciowy(cat):
    imp = {"2026-10-05T10": 1.0, "2026-10-12T14": 2.0, "2026-09-05T10": 100.0}
    p = run(cat, imp=imp)
    cur = p["current_month"]
    assert cur["ym"] == "2026-10"
    assert cur["gross"]["G12w"] == pytest.approx(p["months"]["2026-10"]["G12w"]["gross"])
    assert p["months"]["2026-10"]["G12w"]["kwh"] == pytest.approx(3.0)
    assert cur["gross"]["dyn_a"] is not None   # dyn_a dostępna od 2026-04


def test_biezacy_miesiac_none_gdy_niedostepna(monkeypatch, cat):
    pt = _base()
    pt["dyn_b"] = lambda ym: {"available": False, "reason": "x"}
    _patch_range(monkeypatch, pt)
    assert run(cat)["current_month"]["gross"]["dyn_b"] is None


# ── rozjazd z fakturami ──────────────────────────────────────────────────────

def _inv(total_net, **over):
    inv = {"energy_amount_net": total_net - 30.0, "dist_var_amount_net": 10.0,
           "dist_jakosciowa_amount_net": 5.0, "dist_oze_amount_net": 5.0,
           "dist_kogeneracja_amount_net": 5.0, "fixed_total_net": 5.0,
           "imported_kwh": 100.0}
    inv.update(over)
    return inv


def test_rozjazd_ok_i_nie_ok(monkeypatch, cat):
    _patch_range(monkeypatch, _base(g12w=123.0))     # net = 100.0
    inv = {"2026-02": _inv(102.0), "2026-03": _inv(110.0)}
    p = run(cat, invoices=inv)
    r = p["reconciliation"]
    assert r["2026-02"]["sim_net"] == pytest.approx(100.0)
    assert r["2026-02"]["invoice_net"] == pytest.approx(102.0)
    assert r["2026-02"]["diff_pct"] == pytest.approx(100 * (100 - 102) / 102)
    assert r["2026-02"]["ok"] is True
    assert r["2026-03"]["ok"] is False
    assert r["2026-03"]["kwh_sim"] == 100.0 and r["2026-03"]["kwh_invoice"] == 100.0
    s = p["reconciliation_summary"]
    assert s["months"] == 2 and s["ok_months"] == 1
    assert s["max_abs_diff_pct"] == pytest.approx(abs(100 * (100 - 110) / 110))


def test_miesiac_niepotwierdzony_poza_podsumowaniem(monkeypatch, cat):
    per = {t: (lambda ym, t=t: _entry(123.0, confirmed=not ym.startswith("2025")))
           for t in ("G11", "G12", "G12w", "G13")}
    _patch_range(monkeypatch, per)
    inv = {"2025-06": _inv(10.0), "2026-02": _inv(102.0)}
    p = run(cat, invoices=inv)
    r = p["reconciliation"]
    assert r["2025-06"]["confirmed"] is False and r["2026-02"]["confirmed"] is True
    s = p["reconciliation_summary"]
    assert s["months"] == 1 and s["ok_months"] == 1 and s["unconfirmed_months"] == 1
    assert s["max_abs_diff_pct"] == pytest.approx(abs(100 * (100 - 102) / 102))


def test_faktura_z_polem_none_pomijana(monkeypatch, cat):
    _patch_range(monkeypatch, _base())
    p = run(cat, invoices={"2026-02": _inv(100.0, dist_oze_amount_net=None)})
    assert p["reconciliation"] == {}


def test_brak_faktur(cat):
    p = run(cat)
    assert p["reconciliation"] == {}
    assert p["reconciliation_summary"] == {"months": 0, "ok_months": 0, "unconfirmed_months": 0, "max_abs_diff_pct": None}


def test_settings_w_payloadzie(cat):
    p = tj.compute_payload({}, cat, TariffSettings(phases=1, capacity_bracket="lt500",
                                                   sales_fee_net=2.0), {}, {}, {}, TODAY)
    assert p["settings"] == {"phases": 1, "capacity_bracket": "lt500", "sales_fee_net": 2.0,
                             "sales_fee_g13s_net": None}


def test_g13s_w_payloadzie_bez_bledu(cat):
    p = tj.compute_payload({}, cat, TariffSettings(sales_fee_g13s_net=12.0), {}, {}, {}, TODAY)
    assert "G13s" in p["tariffs"]
    assert p["months"]["2025-12"]["G13s"]["available"] is False
    assert p["settings"]["sales_fee_g13s_net"] == 12.0
    assert "G13s" in p["last12"]["gross"]


# ── zapis / odczyt ───────────────────────────────────────────────────────────

def test_save_load_round_trip(tmp_path, cat):
    p = run(cat)
    path = tmp_path / "sub" / "tariff_sim.json"
    tj.save_payload(p, path)
    assert tj.load_payload(path) == json.loads(json.dumps(p))
    assert not list(path.parent.glob("*.tmp"))


def test_load_uszkodzony_lub_brak(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{nie json")
    assert tj.load_payload(bad) is None
    assert tj.load_payload(tmp_path / "brak.json") is None


# ── wariant B (magazyn przeliczony pod taryfy) ───────────────────────────────

calls: list = []


def _fake_b(savings_for):
    """savings_for(ym, taryfa) -> oszczędność lub None (brak wpisu B)."""
    def fake(hours, cfg, catalog, rdn, kompas, rcem, from_ym, to_ym, overrides=None, soc_floor=0.0):
        calls.append(soc_floor)
        out = {}
        y, m = int(from_ym[:4]), int(from_ym[5:7])
        while (y, m) <= (int(to_ym[:4]), int(to_ym[5:7])):
            ym = f"{y:04d}-{m:02d}"
            for t in tj.TARIFFS:
                sv = savings_for(ym, t)
                if sv is not None:
                    out.setdefault(ym, {})[t] = {"savings_pln": sv, "charged_kwh": 1.0,
                                                 "discharged_kwh": 0.9}
            m += 1
            if m > 12:
                y, m = y + 1, 1
        return out
    return fake


def run_b(cat):
    return tj.compute_payload({}, cat, TariffSettings(), {}, {}, {}, TODAY,
                              hours={"2026-03-10T10": (1.0, 1.0)}, battery_cfg=BatteryConfig(),
                              rcem_by_ym={"2026-03": 0.3})


def test_bez_hours_brak_pol_b(monkeypatch, cat):
    _patch_range(monkeypatch, _base())
    p = run(cat)
    assert "gross_b" not in p["last12"] and "gross_b" not in p["current_month"]
    assert "b" not in p["months"]["2026-03"]["G12w"]
    assert "variant_b_note" not in p


def test_wariant_b_gross_minus_oszczednosc(monkeypatch, cat):
    _patch_range(monkeypatch, _base())
    monkeypatch.setattr(tariff_battery, "simulate_variant_b", _fake_b(lambda ym, t: 5.0))
    p = run_b(cat)
    b = p["months"]["2026-03"]["G12w"]["b"]
    assert b["gross"] == pytest.approx(95.0) and b["savings_pln"] == 5.0
    assert b["charged_kwh"] == 1.0 and b["discharged_kwh"] == 0.9
    l12 = p["last12"]
    assert l12["gross_b"]["G12w"] == pytest.approx(12 * 95.0)
    assert l12["gross_b"]["G11"] == pytest.approx(12 * 115.0)
    assert p["current_month"]["gross_b"]["G12w"] == pytest.approx(95.0)
    assert p["variant_b_note"]


def test_wariant_b_najtansza_i_oszczednosc(monkeypatch, cat):
    _patch_range(monkeypatch, _base())
    # G13 (130) dostaje duzą oszczędność i wygrywa w B
    monkeypatch.setattr(tariff_battery, "simulate_variant_b",
                        _fake_b(lambda ym, t: 50.0 if t == "G13" else 1.0))
    l12 = run_b(cat)["last12"]
    assert l12["cheapest"] == "dyn_a"
    assert l12["cheapest_b"] == "G13"
    assert l12["saving_vs_g12w_b"] == pytest.approx(12 * (99.0 - 80.0))


def test_wariant_b_niedostepna_taryfa_none(monkeypatch, cat):
    pt = _base()
    pt["dyn_a"] = lambda ym: ({"available": False, "reason": "x"} if ym == "2026-01"
                              else _entry(10.0, reliable=True))
    _patch_range(monkeypatch, pt)
    monkeypatch.setattr(tariff_battery, "simulate_variant_b", _fake_b(lambda ym, t: 1.0))
    p = run_b(cat)
    assert p["months"]["2026-01"]["dyn_a"]["b"] is None
    assert p["last12"]["gross_b"]["dyn_a"] is None


def test_wariant_b_brak_wyniku_miesiaca_none(monkeypatch, cat):
    _patch_range(monkeypatch, _base())
    monkeypatch.setattr(tariff_battery, "simulate_variant_b",
                        _fake_b(lambda ym, t: None if ym == "2026-02" else 1.0))
    p = run_b(cat)
    assert p["months"]["2026-02"]["G12w"]["b"] is None
    assert p["last12"]["gross_b"]["G12w"] is None
    assert p["last12"]["cheapest_b"] is None and p["last12"]["saving_vs_g12w_b"] is None


# ── recommend (E4) ───────────────────────────────────────────────────────────

def _reco_payload(gross, gross_b=None):
    l12 = {"gross": gross}
    if gross_b is not None:
        l12["gross_b"] = gross_b
    return {"last12": l12}


def test_recommend_zostan_gdy_g12w_najtansza():
    r = tj.recommend(_reco_payload({"G11": 900.0, "G12w": 800.0, "G13": 850.0}), 100)["A"]
    assert r["best"] == "G12w" and r["switch"] is False and r["saving_pln"] == 0
    assert "G12w" in r["reason"]


def test_recommend_zmiana_gdy_oszczednosc_ponad_progiem():
    r = tj.recommend(_reco_payload({"G12w": 1000.0, "G13": 600.0}), 100)["A"]
    assert r["best"] == "G13" and r["switch"] is True
    assert r["saving_pln"] == pytest.approx(400.0)


def test_recommend_ponizej_progu_nie_przelacza():
    r = tj.recommend(_reco_payload({"G12w": 1000.0, "G13": 950.0}), 100)["A"]
    assert r["best"] == "G13" and r["switch"] is False
    assert r["saving_pln"] == pytest.approx(50.0)


def test_recommend_brak_b_to_none():
    out = tj.recommend(_reco_payload({"G12w": 1000.0, "G13": 600.0}), 100)
    assert out["B"] is None and out["A"] is not None


def test_recommend_b_liczone_z_gross_b():
    out = tj.recommend(_reco_payload({"G12w": 1000.0}, {"G12w": 700.0, "dyn_a": 500.0}), 100)
    assert out["B"]["best"] == "dyn_a" and out["B"]["switch"] is True


def test_recommend_g12w_none_to_none():
    out = tj.recommend(_reco_payload({"G12w": None, "G13": 600.0}), 100)
    assert out["A"] is None


def test_recommend_pomija_taryfy_none():
    r = tj.recommend(_reco_payload({"G12w": 1000.0, "G13": None, "G11": 700.0}), 100)["A"]
    assert r["best"] == "G11"


def test_recommend_brak_last12():
    assert tj.recommend({}, 100) == {"A": None, "B": None, "C": None, "best_year": None}


def test_po_koncu_cennika_liczy_do_ostatniego_miesiaca_i_oznacza(cat):
    imp = {"2026-12-05T10": 1.0}
    p = tj.compute_payload(imp, cat, TariffSettings(), {}, {}, {}, date(2027, 1, 15))
    assert p["to"] == "2026-12"
    assert p["catalog_ends"] == "2026-12"
    assert p["months"]["2026-12"]["G12w"]["kwh"] == pytest.approx(1.0)


def test_w_okresie_cennika_brak_catalog_ends(cat):
    assert run(cat)["catalog_ends"] is None


# ── wariant C, best_year, recommend (E3) ─────────────────────────────────────

def _fake_c(savings_for):
    """savings_for(ym, taryfa) -> oszczędność lub None; zwraca monthly."""
    def fake(hours, cfg, catalog, rdn, kompas, rcem, from_ym, to_ym, *a, **k):
        out = {}
        y, m = int(from_ym[:4]), int(from_ym[5:7])
        while (y, m) <= (int(to_ym[:4]), int(to_ym[5:7])):
            ym = f"{y:04d}-{m:02d}"
            for t in ("G12", "G12w", "G13", "G13s"):
                sv = savings_for(ym, t)
                if sv is not None:
                    out.setdefault(ym, {})[t] = {
                        "savings_pln": sv, "wear_pln": 0.1, "grid_charged_kwh": 2.0,
                        "pv_charged_kwh": 1.0, "discharged_kwh": 2.5}
            m += 1
            if m > 12:
                y, m = y + 1, 1
        return out
    return fake


def _patch_bc(monkeypatch, b=1.0, c=lambda ym, t: 8.0, per_tariff=None):
    from pv_roi_tracker import tariff_grid_charge
    pt = per_tariff or _base()
    pt.setdefault("G13s", lambda ym: _entry(105.0))
    _patch_range(monkeypatch, pt)
    monkeypatch.setattr(tariff_battery, "simulate_variant_b", _fake_b(lambda ym, t: b))
    monkeypatch.setattr(tariff_grid_charge, "simulate_variant_c", _fake_c(c))


def test_b_liczone_z_soc_floor_016(monkeypatch, cat):
    calls.clear()
    _patch_bc(monkeypatch)
    run_b(cat)
    assert calls and all(c == 0.16 for c in calls)


def test_wariant_c_w_months_i_last12(monkeypatch, cat):
    _patch_bc(monkeypatch)
    p = run_b(cat)
    c = p["months"]["2026-03"]["G12w"]["c"]
    assert c["gross"] == pytest.approx(92.0) and c["savings_pln"] == 8.0
    assert c["grid_charged_kwh"] == 2.0 and c["pv_charged_kwh"] == 1.0
    assert c["discharged_kwh"] == 2.5 and c["wear_pln"] == 0.1
    l12 = p["last12"]
    assert l12["gross_c"]["G12w"] == pytest.approx(12 * 92.0)
    assert l12["gross_c"]["G13s"] == pytest.approx(12 * 97.0)
    assert l12["cheapest_c"] == "G12w"
    assert l12["saving_vs_g12w_c"] == 0.0
    assert p["current_month"]["gross_c"]["G12w"] == pytest.approx(92.0)
    assert "G12w" in p["plans"] and p["variant_c_note"]
    assert "16%" in p["variant_b_note"]


def test_wariant_c_none_dla_nie_grid(monkeypatch, cat):
    _patch_bc(monkeypatch)
    p = run_b(cat)
    for t in ("G11", "dyn_a", "dyn_b"):
        assert p["months"]["2026-03"][t]["c"] is None
        assert p["last12"]["gross_c"][t] is None
        assert p["current_month"]["gross_c"][t] is None


def test_wariant_c_brak_wyniku_miesiaca_none(monkeypatch, cat):
    _patch_bc(monkeypatch, c=lambda ym, t: None if ym == "2026-02" else 8.0)
    p = run_b(cat)
    assert p["months"]["2026-02"]["G12w"]["c"] is None
    assert p["last12"]["gross_c"]["G12w"] is None


def test_brak_c_bez_hours(monkeypatch, cat):
    _patch_range(monkeypatch, _base())
    p = run(cat)
    assert "gross_c" not in p["last12"] and "gross_c" not in p["current_month"]
    assert "plans" not in p and "variant_c_note" not in p
    assert "c" not in p["months"]["2026-03"]["G12w"]


def test_best_year_ranking_i_baseline(monkeypatch, cat):
    _patch_bc(monkeypatch, b=1.0, c=lambda ym, t: 30.0 if t == "G13s" else 8.0)
    by = run_b(cat)["best_year"]
    assert by["baseline"] == {"tariff": "G12w", "variant": "A", "gross": pytest.approx(1200.0)}
    gr = [r["gross"] for r in by["rows"]]
    assert gr == sorted(gr)
    assert by["best"] == by["rows"][0]
    assert (by["best"]["tariff"], by["best"]["variant"]) == ("G13s", "C")
    row = next(r for r in by["rows"] if r["tariff"] == "G13s" and r["variant"] == "C")
    assert row["gross"] == pytest.approx(12 * 75.0)
    assert row["saving_pln"] == pytest.approx(1200.0 - 12 * 75.0)
    assert row["confirmed"] is True
    assert not any(r["variant"] == "C" and r["tariff"] in ("G11", "dyn_a", "dyn_b")
                   for r in by["rows"])


def test_best_year_g13s_niepotwierdzony_i_ostrzezenia(monkeypatch, cat):
    pt = _base()
    pt["G13s"] = lambda ym: _entry(105.0, confirmed=False)
    pt["dyn_a"] = lambda ym: _entry(500.0, reliable=False)
    pt["dyn_b"] = lambda ym: _entry(500.0, reliable=False)
    _patch_bc(monkeypatch, c=lambda ym, t: 60.0 if t == "G13s" else 1.0, per_tariff=pt)
    by = run_b(cat)["best_year"]
    assert by["best"]["tariff"] == "G13s" and by["best"]["variant"] == "C"
    assert by["best"]["confirmed"] is False
    assert any("niepotwierdzon" in w for w in by["warnings"])
    assert any("12 miesięcy" in w for w in by["warnings"])


def test_best_year_g12w_c_bez_ostrzezenia_o_zmianie(monkeypatch, cat):
    pt = _base()
    pt["dyn_a"] = lambda ym: _entry(500.0, reliable=False)
    pt["dyn_b"] = lambda ym: _entry(500.0, reliable=False)
    _patch_bc(monkeypatch, c=lambda ym, t: 40.0 if t == "G12w" else 1.0, per_tariff=pt)
    by = run_b(cat)["best_year"]
    assert by["best"]["tariff"] == "G12w" and by["best"]["variant"] == "C"
    assert not any("12 miesięcy" in w for w in by["warnings"])


def test_best_year_none_bez_baseline(monkeypatch, cat):
    pt = _base()
    pt["G12w"] = lambda ym: ({"available": False, "reason": "x"} if ym == "2026-01"
                             else _entry(100.0))
    _patch_range(monkeypatch, pt)
    assert run(cat)["best_year"] is None


def test_recommend_c_i_best_year_powyzej_progu():
    by = {"best": {"tariff": "G13s", "variant": "C", "gross": 600.0,
                   "saving_pln": 400.0, "confirmed": True}}
    p = {"last12": {"gross": {"G12w": 1000.0}, "gross_c": {"G12w": 900.0, "G13s": 600.0}},
         "best_year": by}
    out = tj.recommend(p, 100)
    assert out["C"]["best"] == "G13s" and out["C"]["switch"] is True
    b = out["best_year"]
    assert b["best"] == by["best"] and b["saving_pln"] == 400.0
    assert b["tariff_change"] is True and b["above_threshold"] is True and b["reason"]


def test_recommend_best_year_ponizej_progu_i_bez_zmiany_taryfy():
    by = {"best": {"tariff": "G12w", "variant": "C", "gross": 950.0,
                   "saving_pln": 50.0, "confirmed": True}}
    b = tj.recommend({"best_year": by}, 100)["best_year"]
    assert b["tariff_change"] is False and b["above_threshold"] is False


def test_recommend_best_year_none():
    assert tj.recommend({"best_year": None}, 100)["best_year"] is None
    assert tj.recommend({"best_year": {"best": None}}, 100)["best_year"] is None


# ── baza porównań = bieżąca taryfa (E3) ──────────────────────────────────────

def test_baza_domyslna_g12w_i_aliasy(monkeypatch, cat):
    _patch_range(monkeypatch, _base(g12w=100.0, g13=80.0))
    p = run(cat)
    assert p["baseline_tariff"] == "G12w"
    l12 = p["last12"]
    assert l12["saving_vs_baseline"] == l12["saving_vs_g12w"] == pytest.approx(240.0)
    assert p["best_year"]["baseline"]["tariff"] == "G12w"


def test_baza_g13_ranking_i_oszczednosc(monkeypatch, cat):
    _patch_range(monkeypatch, _base(g12w=100.0, g13=80.0))
    p = tj.compute_payload({}, cat, TariffSettings(), {}, {}, {}, TODAY, baseline="G13")
    assert p["baseline_tariff"] == "G13"
    l12 = p["last12"]
    # najtańsza dyn_a (90*12=1080)? nie: G13 = 960 < dyn_a 1080 -> oszczędność 0
    assert l12["cheapest"] == "G13"
    assert l12["saving_vs_baseline"] == l12["saving_vs_g12w"] == 0.0
    assert p["best_year"]["baseline"] == {"tariff": "G13", "variant": "A", "gross": pytest.approx(960.0)}
    assert p["best_year"]["warnings"] == []   # najlepsza = baza, brak ostrzeżenia o zmianie grupy


def test_baza_g13_oszczednosc_dodatnia(monkeypatch, cat):
    _patch_range(monkeypatch, _base(g12w=50.0, g13=100.0))
    p = tj.compute_payload({}, cat, TariffSettings(), {}, {}, {}, TODAY, baseline="G13")
    assert p["last12"]["cheapest"] == "G12w"
    assert p["last12"]["saving_vs_baseline"] == p["last12"]["saving_vs_g12w"] == pytest.approx(600.0)
    assert p["best_year"]["best"]["saving_pln"] == pytest.approx(600.0)


def test_reconcile_wg_okresow_taryfy(monkeypatch, cat):
    # G13 net 100.0 (gross 123), G12w net 200.0
    per = _base(g12w=246.0, g13=123.0)
    _patch_range(monkeypatch, per)
    periods = [{"tariff": "G12w", "from": None}, {"tariff": "G13", "from": "2026-03-01"}]
    inv = {"2026-02": _inv(200.0), "2026-04": _inv(100.0)}
    p = tj.compute_payload({}, cat, TariffSettings(), {}, {}, inv, TODAY, tariff_periods=periods)
    r = p["reconciliation"]
    assert r["2026-02"]["sim_net"] == pytest.approx(200.0) and r["2026-02"]["ok"] is True
    assert r["2026-04"]["sim_net"] == pytest.approx(100.0) and r["2026-04"]["ok"] is True


def test_reconcile_zmiana_w_srodku_miesiaca_pomijana(monkeypatch, cat):
    _patch_range(monkeypatch, _base())
    periods = [{"tariff": "G12w", "from": None}, {"tariff": "G13", "from": "2026-03-15"}]
    p = tj.compute_payload({}, cat, TariffSettings(), {}, {}, {"2026-03": _inv(100.0)}, TODAY,
                           tariff_periods=periods)
    assert p["reconciliation"] == {}


def test_reconcile_bez_okresow_to_g12w(monkeypatch, cat):
    _patch_range(monkeypatch, _base(g12w=123.0))
    p = tj.compute_payload({}, cat, TariffSettings(), {}, {}, {"2026-02": _inv(100.0)}, TODAY,
                           tariff_periods=[])
    assert p["reconciliation"]["2026-02"]["sim_net"] == pytest.approx(100.0)


def test_recommend_baza_z_payloadu():
    pl = {"baseline_tariff": "G13", "last12": {"gross": {"G12w": 500.0, "G13": 1000.0}}}
    r = tj.recommend(pl, 100)["A"]
    assert r["best"] == "G12w" and r["switch"] is True
    assert r["baseline_tariff"] == "G13" and r["baseline_gross"] == 1000.0 and r["g12w"] == 1000.0
    assert r["saving_pln"] == 500.0


def test_recommend_baza_najtansza_tekst():
    pl = {"baseline_tariff": "G13", "last12": {"gross": {"G12w": 1500.0, "G13": 1000.0}}}
    r = tj.recommend(pl, 100)["A"]
    assert r["best"] == "G13" and r["switch"] is False
    assert r["reason"] == "G13 jest najtańsza"


def test_recommend_stary_payload_bez_baseline_tariff():
    r = tj.recommend({"last12": {"gross": {"G12w": 1000.0, "G13": 600.0}}}, 100)["A"]
    assert r["baseline_tariff"] == "G12w" and r["g12w"] == 1000.0 and r["switch"] is True


def test_recommend_best_year_tekst_z_nazwa_bazy():
    best = {"tariff": "G12w", "variant": "A", "gross": 500.0, "saving_pln": 500.0, "confirmed": True}
    pl = {"baseline_tariff": "G13", "best_year": {"best": best}}
    out = tj.recommend(pl, 100)["best_year"]
    assert "względem G13 A" in out["reason"]
    assert out["tariff_change"] is True
    best2 = {**best, "tariff": "G13"}
    out2 = tj.recommend({"baseline_tariff": "G13", "best_year": {"best": best2}}, 100)["best_year"]
    assert out2["tariff_change"] is False
