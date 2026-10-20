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
