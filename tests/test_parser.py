"""Tests for parser.py — pivot CSV → MonthlyRecord list."""
import pytest
from pv_roi_tracker.parser import parse_csv

# Minimal two-year pivot CSV using Polish month abbreviations and Polish metric labels.
# The ź in Paź and ę/ó/ś in metric labels tests UTF-8 + NFC normalisation.
SAMPLE_CSV = """\
status,inwestycja,dofinansowanie,oszczędności,ile lat do zwrotu,%
wartości,51900,28714,5000,5,21.6

2023,,,,,,,,,,,,,,
,Sty,Lut,Mar,Kwi,Maj,Cze,Lip,Sie,Wrz,Paź,Lis,Gru,SUMA
wyprodukowane,0,0,50,180,450,600,620,580,400,150,10,0,3040
kWh/kWp,0,0,7.4,26.8,66.9,89.3,92.3,86.3,59.5,22.3,1.5,0,452.3
zużyte,300,280,290,260,250,220,210,200,230,270,310,320,2940
kupione,300,280,250,100,0,0,0,0,0,120,300,320,1670
autokonsumpcja,0,0,40,160,250,220,210,200,230,30,10,0,1350
autokonsumpcja oszczędności,0,0,36.8,147.2,230,202.4,193.2,184,211.6,27.6,9.2,0,1242
sprzedane,0,0,10,20,200,380,410,380,170,120,0,0,1690
koszt zakupu,0.92,0.92,0.92,0.92,0.92,0.92,0.92,0.92,0.92,0.92,0.92,0.92,0.92
suma zakupu,276,257.6,230,92,0,0,0,0,0,110.4,276,294.4,1536.4
cena sprzedaży RCEm,0,0,0.40,0.42,0.38,0.41,0.43,0.45,0.39,0.35,0,0,0.38
suma sprzedaży,0,0,4,8.4,76,155.8,176.3,171,66.3,42,0,0,699.8

2024,,,,,,,,,,,,,,
,Sty,Lut,Mar,Kwi,Maj,Cze,Lip,Sie,Wrz,Paź,Lis,Gru,SUMA
wyprodukowane,5,10,80,220,500,650,670,630,450,180,20,5,3420
kWh/kWp,0.7,1.5,11.9,32.7,74.4,96.7,99.7,93.8,67.0,26.8,3.0,0.7,508.9
zużyte,310,290,300,270,260,230,220,210,240,280,320,330,3060
kupione,305,280,240,70,0,0,0,0,0,110,300,325,1630
autokonsumpcja,5,10,60,200,260,230,220,210,240,70,20,5,1530
autokonsumpcja oszczędności,4.6,9.2,55.2,184,239.2,211.6,202.4,193.2,220.8,64.4,18.4,4.6,1407.6
sprzedane,0,0,20,20,240,420,450,420,210,110,0,0,1890
koszt zakupu,0.92,0.92,0.92,0.92,0.92,0.92,0.92,0.92,0.92,0.92,0.92,0.92,0.92
suma zakupu,280.6,257.6,220.8,64.4,0,0,0,0,0,101.2,276,299,1499.6
cena sprzedaży RCEm,0.30,0.32,0.38,0.40,0.39,0.42,0.44,0.46,0.40,0.36,0,0,0.39
suma sprzedaży,0,0,7.6,8,93.6,176.4,198,193.2,84,39.6,0,0,800.4
"""

# CSV with Polish decimal commas to test number parsing
SAMPLE_CSV_COMMAS = """\
2023,,,,,,,,,,,,,,
,Jan,Feb,Mar,Apr,May,Jun,Jul,Aug,Sep,Oct,Nov,Dec,SUMA
wyprodukowane,0,0,"50,5","180,2","450,8","600,1","620,0","580,7","400,3","150,9","10,1",0,"3044,6"
autokonsumpcja oszczędności,0,0,"36,8","147,2","230,0","202,4","193,2","184,0","211,6","27,6","9,2",0,"1242,0"
sprzedane,0,0,10,20,200,380,410,380,170,120,0,0,1690
cena sprzedaży RCEm,0,0,"0,40","0,42","0,38","0,41","0,43","0,45","0,39","0,35",0,0,0
suma sprzedaży,0,0,4,"8,4",76,"155,8","176,3",171,"66,3",42,0,0,0
"""


# ── Structure tests ───────────────────────────────────────────────────────────

def test_parse_returns_records():
    records = parse_csv(SAMPLE_CSV)
    assert len(records) > 0


def test_parse_two_years():
    records = parse_csv(SAMPLE_CSV)
    years = {r.year for r in records}
    assert 2023 in years
    assert 2024 in years


def test_no_suma_double_count():
    records = parse_csv(SAMPLE_CSV)
    assert all(1 <= r.month <= 12 for r in records), "SUMA column must not produce a month=0 record"


def test_sorted_chronologically():
    records = parse_csv(SAMPLE_CSV)
    keys = [(r.year, r.month) for r in records]
    assert keys == sorted(keys)


# ── Value tests ───────────────────────────────────────────────────────────────

def test_produced_kwh_may_2023():
    records = parse_csv(SAMPLE_CSV)
    m = {(r.year, r.month): r for r in records}
    assert m[(2023, 5)].produced_kwh == pytest.approx(450.0)


def test_specific_yield_may_2023():
    records = parse_csv(SAMPLE_CSV)
    m = {(r.year, r.month): r for r in records}
    assert m[(2023, 5)].specific_yield == pytest.approx(66.9)


def test_feedin_price_oct_2023():
    records = parse_csv(SAMPLE_CSV)
    m = {(r.year, r.month): r for r in records}
    assert m[(2023, 10)].feedin_price_pln_kwh == pytest.approx(0.35)


# ── Polish label tests ────────────────────────────────────────────────────────

def test_autokonsumpcja_oszczednosci_parsed():
    """Polish label 'autokonsumpcja oszczędności' must map to self_consumed_savings_pln."""
    records = parse_csv(SAMPLE_CSV)
    m = {(r.year, r.month): r for r in records}
    assert m[(2023, 3)].self_consumed_savings_pln == pytest.approx(36.8)


def test_autokonsumpcja_kwh_distinct_from_savings():
    """'autokonsumpcja' (kWh) and 'autokonsumpcja oszczędności' (zł) must not collide."""
    records = parse_csv(SAMPLE_CSV)
    m = {(r.year, r.month): r for r in records}
    # May: autokonsumpcja=250 kWh, autokonsumpcja oszczędności=230 zł
    assert m[(2023, 5)].self_consumed_kwh == pytest.approx(250.0)
    assert m[(2023, 5)].self_consumed_savings_pln == pytest.approx(230.0)


def test_paz_month_parsed():
    """Polish 'Paź' (October, with ź) must be mapped to month 10."""
    records = parse_csv(SAMPLE_CSV)
    m = {(r.year, r.month): r for r in records}
    assert (2023, 10) in m


# ── Decimal comma + currency suffix tests ────────────────────────────────────

def test_polish_decimal_comma():
    records = parse_csv(SAMPLE_CSV_COMMAS)
    m = {(r.year, r.month): r for r in records}
    assert m[(2023, 3)].produced_kwh == pytest.approx(50.5)
    assert m[(2023, 5)].self_consumed_savings_pln == pytest.approx(230.0)
    assert m[(2023, 3)].feedin_price_pln_kwh == pytest.approx(0.40)


# CSV that matches the actual Google Sheets format: values have "zł" suffix and NBSP thousands
SAMPLE_CSV_PLN_FORMAT = """\
2024,,,,,,,,,,,,,,
,Sty,Lut,Mar,Kwi,Maj,Cze,Lip,Sie,Wrz,Paź,Lis,Gru,SUMA
autokonsumpcja oszczedność,"291,80 zł","273,35 zł","518,06 zł","398,87 zł","481,56 zł","418,61 zł","457,43 zł","385,25 zł","353,97 zł","338,56 zł","182,97 zł","149,14 zł","4\xa0249,56 zł"
koszt zakupu,"1,29 zł","1,29 zł","1,29 zł","1,29 zł","1,29 zł","1,29 zł","1,29 zł","1,15 zł","1,15 zł","1,15 zł","1,15 zł","1,04 zł",
suma zakupu,"2\xa0236,60 zł","1\xa0578,44 zł","392,42 zł","103,20 zł","19,87 zł","14,45 zł","16,38 zł","16,33 zł","105,57 zł","120,87 zł","671,95 zł","701,69 zł","5\xa0977,76 zł"
wyprodukowane,226,211,401,309,373,324,354,335,307,294,159,143,3441
"""


def test_pln_suffix_parsed():
    """Values like '457,43 zł' must be parsed to 457.43."""
    records = parse_csv(SAMPLE_CSV_PLN_FORMAT)
    m = {(r.year, r.month): r for r in records}
    assert m[(2024, 7)].self_consumed_savings_pln == pytest.approx(457.43)
    assert m[(2024, 7)].buy_price_pln_kwh == pytest.approx(1.29)
    assert m[(2024, 7)].purchase_cost_pln == pytest.approx(16.38)


def test_nbsp_thousands_separator_parsed():
    """Values like '2\xa0236,60 zł' must be parsed to 2236.60."""
    records = parse_csv(SAMPLE_CSV_PLN_FORMAT)
    m = {(r.year, r.month): r for r in records}
    assert m[(2024, 1)].purchase_cost_pln == pytest.approx(2236.60)


# ── Robustness tests ──────────────────────────────────────────────────────────

def test_empty_csv_returns_empty_list():
    assert parse_csv("") == []


def test_summary_block_ignored():
    """The first two rows (summary header + values) must not produce any records."""
    records = parse_csv(SAMPLE_CSV)
    # If summary rows were parsed as metric rows they'd have appeared in year None → skipped
    assert all(r.year in (2023, 2024) for r in records)
