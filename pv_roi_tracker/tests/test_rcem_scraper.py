"""Tests for rcem_scraper.py — parsing of the PSE RCEm table (no network)."""
from __future__ import annotations

import pytest

from pv_roi_tracker import rcem_scraper


def _month(name: str, base: str, corrected: str = '-') -> str:
    return (
        f'<tr><td colspan="4"><b>{name}</b></td></tr>'
        f'<tr><td>RCEm&nbsp;</td><td>{base}</td><td>11.01.2026</td><td>-</td></tr>'
        f'<tr><td>skorygowana RCEm*</td><td>{corrected}</td><td>-</td><td>-</td></tr>'
    )


def _page(*months: str) -> bytes:
    rows = ''.join(months)
    return (f'<table><tr><td>2026</td></tr>'
            f'<tr><td></td><td>cena [zł/MWh]**</td><td>data</td><td>różnica</td></tr>'
            f'{rows}</table>').encode('utf-8')


class _Resp:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self) -> None:
        pass


def _scrape(monkeypatch, content: bytes):
    monkeypatch.setattr(rcem_scraper.requests, 'get', lambda *a, **k: _Resp(content))
    return rcem_scraper._scrape_all_months_full()


@pytest.mark.parametrize('name, month', [
    ('styczeń', 1), ('luty', 2), ('marzec', 3), ('kwiecień', 4), ('maj', 5),
    ('czerwiec', 6), ('lipiec', 7), ('sierpień', 8), ('wrzesień', 9),
    ('październik', 10), ('listopad', 11), ('grudzień', 12),
])
def test_match_month_every_month_name(name, month):
    assert rcem_scraper._match_month(name) == month


def test_august_and_september_are_stored_separately(monkeypatch):
    """'wrzesień' contains 'sie' (abbreviation of sierpień) — must not overwrite August."""
    out = _scrape(monkeypatch, _page(_month('sierpień', '294,53'),
                                       _month('wrzesień', '345,68')))
    assert out['2026-08'][0] == pytest.approx(0.294530 * 1.23, abs=1e-6)
    assert out['2026-09'][0] == pytest.approx(0.345680 * 1.23, abs=1e-6)


# ── drop_bounce_corrections (migracja 0.45.12) ────────────────────────────────

def _write(path, data):
    import json
    path.write_text(json.dumps(data), encoding='utf-8')


def _read(path):
    import json
    return json.loads(path.read_text(encoding='utf-8'))


_BOUNCE = [
    {'price': 0.362272, 'recorded_at': '2026-09-12'},
    {'price': 0.425186, 'recorded_at': '2026-10-11'},
    {'price': 0.362272, 'recorded_at': '2026-10-20'},
]


def test_drop_bounce_removes_month_and_keeps_backup(tmp_path):
    p = tmp_path / 'rcem_corrections.json'
    _write(p, {'2026-08': _BOUNCE, '2026-02': [{'price': 1.0, 'recorded_at': 'x'}]})
    assert rcem_scraper.drop_bounce_corrections('2026-08', 0.425186, p) is True
    assert _read(p) == {'2026-02': [{'price': 1.0, 'recorded_at': 'x'}]}
    assert _read(tmp_path / 'rcem_corrections.json.pre-0.45.12')['2026-08'] == _BOUNCE


def test_drop_bounce_is_idempotent(tmp_path):
    p = tmp_path / 'rcem_corrections.json'
    _write(p, {'2026-08': _BOUNCE})
    assert rcem_scraper.drop_bounce_corrections('2026-08', 0.425186, p) is True
    assert rcem_scraper.drop_bounce_corrections('2026-08', 0.425186, p) is False
    assert _read(tmp_path / 'rcem_corrections.json.pre-0.45.12')['2026-08'] == _BOUNCE


def test_drop_bounce_keeps_genuine_correction(tmp_path):
    """Last price differs from the first — a real PSE correction, not a bounce."""
    genuine = [{'price': 0.4, 'recorded_at': 'a'}, {'price': 0.425186, 'recorded_at': 'b'}]
    p = tmp_path / 'rcem_corrections.json'
    _write(p, {'2026-08': genuine})
    assert rcem_scraper.drop_bounce_corrections('2026-08', 0.425186, p) is False
    assert _read(p) == {'2026-08': genuine}
    assert not (tmp_path / 'rcem_corrections.json.pre-0.45.12').exists()


def test_drop_bounce_missing_file_or_month(tmp_path):
    p = tmp_path / 'rcem_corrections.json'
    assert rcem_scraper.drop_bounce_corrections('2026-08', 0.425186, p) is False
    _write(p, {'2026-02': []})
    assert rcem_scraper.drop_bounce_corrections('2026-08', 0.425186, p) is False


def test_drop_bounce_runs_at_most_once(tmp_path):
    """An existing .pre-0.45.12 backup marks the migration as done."""
    p = tmp_path / 'rcem_corrections.json'
    (tmp_path / 'rcem_corrections.json.pre-0.45.12').write_text('{}', encoding='utf-8')
    _write(p, {'2026-08': _BOUNCE})
    assert rcem_scraper.drop_bounce_corrections('2026-08', 0.425186, p) is False
    assert _read(p) == {'2026-08': _BOUNCE}
    assert _read(tmp_path / 'rcem_corrections.json.pre-0.45.12') == {}
