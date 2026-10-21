"""Testy price_sources.py — atrapy http_get, bez sieci."""
import json
from datetime import date

import pytest

from pv_roi_tracker import price_sources as ps


def _unix(y, m, d, h, mi=0):
    from datetime import datetime, timezone
    return int(datetime(y, m, d, h, mi, tzinfo=timezone.utc).timestamp())


# ── NBP ──────────────────────────────────────────────────────────────────────

def test_eurpln_wypelnia_weekendy():
    calls = []

    def fake(url):
        calls.append(url)
        # 2025-01-15 śr, 16 czw, 17 pt, 18 sob, 19 nd, 20 pn
        return {"rates": [
            {"effectiveDate": "2025-01-14", "mid": 4.2},
            {"effectiveDate": "2025-01-15", "mid": 4.3},
            {"effectiveDate": "2025-01-17", "mid": 4.4},
            {"effectiveDate": "2025-01-20", "mid": 4.5},
        ]}

    r = ps.fetch_eurpln("2025-01-16", "2025-01-20", http_get=fake)
    assert r["2025-01-16"] == 4.3   # czwartek bez tabeli -> ostatni kurs
    assert r["2025-01-17"] == 4.4
    assert r["2025-01-18"] == 4.4
    assert r["2025-01-19"] == 4.4
    assert r["2025-01-20"] == 4.5
    assert set(r) == {f"2025-01-{d}" for d in (16, 17, 18, 19, 20)}


def test_eurpln_dzieli_zakres_ponad_93_dni():
    urls = []

    def fake(url):
        urls.append(url)
        import re
        a, b = re.search(r"/eur/(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})/", url).groups()
        from datetime import timedelta
        da, db = date.fromisoformat(a), date.fromisoformat(b)
        assert (db - da).days + 1 <= 93
        rows, d = [], da
        while d <= db:
            if d.weekday() < 5:
                rows.append({"effectiveDate": d.isoformat(), "mid": 4.0})
            d += timedelta(days=1)
        return {"rates": rows}

    r = ps.fetch_eurpln("2025-01-01", "2025-12-31", http_get=fake)
    assert len(urls) >= 4
    assert len(r) == 365
    assert all(v == 4.0 for v in r.values())


# ── Energy-Charts ────────────────────────────────────────────────────────────

def _router(ec=None, nbp=None):
    def fake(url):
        if "energy-charts" in url:
            return ec
        if "nbp.pl" in url:
            return nbp
        raise AssertionError(url)
    return fake


def test_rdn_15min_do_godziny_i_przeliczenie():
    # 2025-01-15 12:00 UTC = 13:00 lokalnie (zima, UTC+1)
    secs = [_unix(2025, 1, 15, 12, m) for m in (0, 15, 30, 45)]
    ec = {"unix_seconds": secs, "price": [100.0, 200.0, 300.0, 400.0]}
    nbp = {"rates": [{"effectiveDate": "2025-01-15", "mid": 4.0}]}
    r = ps.fetch_rdn_hourly("2025-01-15", "2025-01-15", http_get=_router(ec, nbp))
    assert r == {"2025-01-15T13": pytest.approx(250.0 * 4.0)}


def test_rdn_godzinowe_lato_czas_lokalny():
    # 2025-07-01 10:00 UTC = 12:00 lokalnie (UTC+2)
    ec = {"unix_seconds": [_unix(2025, 7, 1, 10)], "price": [50.0]}
    nbp = {"rates": [{"effectiveDate": "2025-07-01", "mid": 4.25}]}
    r = ps.fetch_rdn_hourly("2025-07-01", "2025-07-01", http_get=_router(ec, nbp))
    assert r == {"2025-07-01T12": pytest.approx(212.5)}


def test_rdn_pomija_none_i_kurs_weekendowy():
    # sobota 2025-01-18, kurs z piątku
    ec = {"unix_seconds": [_unix(2025, 1, 18, 9), _unix(2025, 1, 18, 10)],
          "price": [None, 100.0]}
    nbp = {"rates": [{"effectiveDate": "2025-01-17", "mid": 4.4}]}
    r = ps.fetch_rdn_hourly("2025-01-18", "2025-01-18", http_get=_router(ec, nbp))
    assert r == {"2025-01-18T11": pytest.approx(440.0)}


# ── PSE kompas ───────────────────────────────────────────────────────────────

def test_kompas_mapowanie_stref_i_is_active():
    rows = [
        {"dtime": "2025-01-15 08:00", "usage_fcst": 0, "is_active": True},
        {"dtime": "2025-01-15 09:00", "usage_fcst": 1, "is_active": True},
        {"dtime": "2025-01-15 10:00", "usage_fcst": 2, "is_active": True},
        {"dtime": "2025-01-15 11:00", "usage_fcst": 3, "is_active": True},
        {"dtime": "2025-01-15 12:00", "usage_fcst": 3, "is_active": False},
    ]
    urls = []

    def fake(url):
        urls.append(url)
        return {"value": rows}

    r = ps.fetch_kompas_hourly("2025-01-15", "2025-01-15", http_get=fake)
    assert r == {"2025-01-15T08": "S1", "2025-01-15T09": "S2",
                 "2025-01-15T10": "S3", "2025-01-15T11": "S4"}
    assert "pdgsz" in urls[0]
    assert "is_active" in __import__("urllib.parse").parse.unquote(urls[0])
    assert "business_date" in json.dumps(urls[0]) or "business_date" in __import__("urllib.parse").parse.unquote(urls[0])


def test_kompas_stronicowanie():
    pages = [
        {"value": [{"dtime": "2025-01-15 08:00", "usage_fcst": 0, "is_active": True}],
         "nextLink": "https://api.raporty.pse.pl/api/pdgsz?page=2"},
        {"value": [{"dtime": "2025-01-15 09:00", "usage_fcst": 1, "is_active": True}]},
    ]
    it = iter(pages)
    r = ps.fetch_kompas_hourly("2025-01-15", "2025-01-15", http_get=lambda u: next(it))
    assert set(r) == {"2025-01-15T08", "2025-01-15T09"}


# ── cache ────────────────────────────────────────────────────────────────────

def _fetchers(monkeypatch, rdn=None, kompas=None, eur=None, fail=False):
    calls = {"rdn": 0, "kompas": 0, "eur": 0}

    def f_eur(a, b, http_get=None):
        calls["eur"] += 1
        if fail:
            raise OSError("brak sieci")
        return dict(eur or {})

    def f_rdn(a, b, http_get=None, eurpln=None):
        calls["rdn"] += 1
        if fail:
            raise OSError("brak sieci")
        return dict(rdn or {})

    def f_k(a, b, http_get=None):
        calls["kompas"] += 1
        if fail:
            raise OSError("brak sieci")
        return dict(kompas or {})

    monkeypatch.setattr(ps, "fetch_eurpln", f_eur)
    monkeypatch.setattr(ps, "fetch_rdn_hourly", f_rdn)
    monkeypatch.setattr(ps, "fetch_kompas_hourly", f_k)
    return calls


def test_cache_zapis_odczyt_i_dociaganie_brakujacych(tmp_path, monkeypatch):
    path = tmp_path / "prices.json"
    calls = _fetchers(monkeypatch,
                      rdn={"2025-01-15T10": 500.0},
                      kompas={"2025-01-15T10": "S2"},
                      eur={"2025-01-15": 4.3})
    c = ps.PriceCache(path)
    c.ensure("2025-01-15", "2025-01-15", today=date(2025, 2, 1))
    assert path.exists()
    assert c.rdn["2025-01-15T10"] == 500.0
    c2 = ps.PriceCache(path)
    assert c2.kompas == {"2025-01-15T10": "S2"}
    assert c2.eurpln == {"2025-01-15": 4.3}
    # drugie ensure nie odpytuje sieci
    before = dict(calls)
    c2.ensure("2025-01-15", "2025-01-15", today=date(2025, 2, 1))
    assert calls == before


def test_cache_nie_zapisuje_dnia_biezacego(tmp_path, monkeypatch):
    calls = _fetchers(monkeypatch, rdn={"2025-02-01T10": 1.0})
    c = ps.PriceCache(tmp_path / "p.json")
    c.ensure("2025-02-01", "2025-02-01", today=date(2025, 2, 1))
    assert calls == {"rdn": 0, "kompas": 0, "eur": 0}
    assert c.rdn == {}


def test_cache_blad_sieci_nie_kasuje(tmp_path, monkeypatch):
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"rdn": {"2025-01-14T10": 7.0}, "kompas": {}, "eurpln": {}}))
    _fetchers(monkeypatch, fail=True)
    c = ps.PriceCache(path)
    c.ensure("2025-01-15", "2025-01-16", today=date(2025, 2, 1))   # nie rzuca
    assert c.rdn == {"2025-01-14T10": 7.0}
    assert json.loads(path.read_text())["rdn"] == {"2025-01-14T10": 7.0}


def test_cache_uszkodzony_plik(tmp_path):
    path = tmp_path / "p.json"
    path.write_text("{nie json")
    c = ps.PriceCache(path)
    assert c.rdn == {} and c.kompas == {} and c.eurpln == {}
