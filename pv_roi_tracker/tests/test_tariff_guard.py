"""Testy tariff_guard.py — strażnik cenników (fetch wstrzykiwany, bez sieci)."""
from datetime import datetime

import pytest
import requests

from pv_roi_tracker import tariff_guard as tg

TD = tg.PAGES['td']['url']
URE = tg.PAGES['ure']['url']
NOW = datetime(2026, 11, 2, 6, 10)


def _html(*hrefs):
    return '<html><body>' + ''.join(f'<a href="{h}">x</a>' for h in hrefs) + '<a>bez href</a></body></html>'


def _fetcher(td=(), ure=()):
    pages = {TD: _html(*td), URE: _html(*ure)}
    return lambda url: pages[url]


# ── extract_links ────────────────────────────────────────────────────────────

def test_extract_relative_to_absolute_filter_unique_sorted():
    html = _html('/b/aktualna-taryfa/2.pdf', '/a/archiwum-taryf/1.pdf', '/b/aktualna-taryfa/2.pdf',
                 '/inne/strona', 'https://x.pl/aktualna-taryfa/z.pdf')
    out = tg.extract_links(html, 'https://www.tauron-dystrybucja.pl/start', tg.PAGES['td']['needles'])
    assert out == ['https://www.tauron-dystrybucja.pl/a/archiwum-taryf/1.pdf',
                   'https://www.tauron-dystrybucja.pl/b/aktualna-taryfa/2.pdf',
                   'https://x.pl/aktualna-taryfa/z.pdf']


def test_extract_needles_case_insensitive():
    assert tg.extract_links(_html('/Taryfa-2027.pdf', '/nic'), 'https://u.pl/', ('taryf',)) == [
        'https://u.pl/Taryfa-2027.pdf']


def test_extract_no_links():
    assert tg.extract_links('<html><body>pusto</body></html>', 'https://u.pl/', ('taryf',)) == []
    assert tg.extract_links('', 'https://u.pl/', ('taryf',)) == []


# ── run_guard ────────────────────────────────────────────────────────────────

def test_baseline_saves_links_without_message():
    state, msgs = tg.run_guard({}, _fetcher(td=['/aktualna-taryfa/a.pdf'], ure=['/taryfa-1']), NOW)
    assert msgs == []
    assert state['new_documents'] == []
    assert state['pages']['td']['links'] == ['https://www.tauron-dystrybucja.pl/aktualna-taryfa/a.pdf']
    assert state['pages']['td']['error'] is None and state['last_run'] == NOW.isoformat(timespec='seconds')


def test_repeat_without_change_is_silent():
    f = _fetcher(td=['/aktualna-taryfa/a.pdf'], ure=['/taryfa-1'])
    s1, _ = tg.run_guard({}, f, NOW)
    s2, msgs = tg.run_guard(s1, f, NOW)
    assert msgs == [] and s2['new_documents'] == []


def test_new_link_gives_message_and_new_document():
    s1, _ = tg.run_guard({}, _fetcher(td=['/aktualna-taryfa/a.pdf'], ure=['/taryfa-1']), NOW)
    s2, msgs = tg.run_guard(s1, _fetcher(td=['/aktualna-taryfa/a.pdf', '/aktualna-taryfa/b.pdf'],
                                         ure=['/taryfa-1']), NOW)
    assert len(msgs) == 1
    assert msgs[0].startswith('Cennik taryf: nowe dokumenty na stronie TD: 1 — ')
    assert 'https://www.tauron-dystrybucja.pl/aktualna-taryfa/b.pdf' in msgs[0]
    assert msgs[0].endswith('Sprawdź i dopisz okres w zakładce Taryfa.')
    assert [d['url'] for d in s2['new_documents']] == ['https://www.tauron-dystrybucja.pl/aktualna-taryfa/b.pdf']
    assert s2['new_documents'][0]['page'] == 'td'
    assert len(s2['pages']['td']['links']) == 2
    assert s1['new_documents'] == []   # wejście nie jest mutowane


def test_message_lists_first_three_urls():
    s1, _ = tg.run_guard({}, _fetcher(ure=['/taryfa-0']), NOW)
    _, msgs = tg.run_guard(s1, _fetcher(ure=[f'/taryfa-{i}' for i in range(6)]), NOW)
    assert 'URE: 5 — ' in msgs[0]
    assert msgs[0].count('https://') == 3


def test_same_new_link_twice_no_duplicate():
    s1, _ = tg.run_guard({}, _fetcher(ure=['/taryfa-1']), NOW)
    s2, _ = tg.run_guard(s1, _fetcher(ure=['/taryfa-1', '/taryfa-2']), NOW)
    s3, _ = tg.run_guard(s2, _fetcher(ure=['/taryfa-1']), NOW)           # link znika…
    s4, msgs = tg.run_guard(s3, _fetcher(ure=['/taryfa-1', '/taryfa-2']), NOW)   # …i wraca
    assert len(msgs) == 1
    assert [d['url'] for d in s4['new_documents']] == ['https://bip.ure.gov.pl/taryfa-2']


def test_vanished_link_is_silent():
    s1, _ = tg.run_guard({}, _fetcher(ure=['/taryfa-1', '/taryfa-2']), NOW)
    s2, msgs = tg.run_guard(s1, _fetcher(ure=['/taryfa-1']), NOW)
    assert msgs == [] and s2['pages']['ure']['links'] == ['https://bip.ure.gov.pl/taryfa-1']


def test_fetch_error_sets_error_keeps_links_no_message():
    s1, _ = tg.run_guard({}, _fetcher(td=['/aktualna-taryfa/a.pdf'], ure=['/taryfa-1']), NOW)

    def fetch(url):
        if url == TD:
            raise requests.ConnectionError('x' * 300)
        return _html('/taryfa-1')
    s2, msgs = tg.run_guard(s1, fetch, NOW)
    assert msgs == []
    assert s2['pages']['td']['links'] == s1['pages']['td']['links']
    assert s2['pages']['td']['error'] == 'x' * 200
    assert s2['pages']['ure']['error'] is None


def test_http_error_status_is_error(monkeypatch):
    class R:
        status_code = 503
        text = ''
    monkeypatch.setattr(tg.requests, 'get', lambda *a, **k: R())
    state, msgs = tg.run_guard({}, None, NOW)
    assert msgs == [] and state['pages']['td']['error'] == 'HTTP 503'


def test_error_on_first_run_then_success_is_baseline():
    def boom(url):
        raise requests.Timeout('timeout')
    s1, _ = tg.run_guard({}, boom, NOW)
    s2, msgs = tg.run_guard(s1, _fetcher(td=['/aktualna-taryfa/a.pdf']), NOW)
    assert msgs == [] and s2['new_documents'] == []


def test_other_exceptions_propagate():
    def boom(url):
        raise RuntimeError('bug')
    with pytest.raises(RuntimeError):
        tg.run_guard({}, boom, NOW)


# ── acknowledge / stan ───────────────────────────────────────────────────────

def test_acknowledge_clears_without_mutating():
    state = {'pages': {}, 'new_documents': [{'page': 'td', 'url': 'u', 'seen': 's'}]}
    out = tg.acknowledge(state)
    assert out['new_documents'] == [] and len(state['new_documents']) == 1


def test_load_state_missing_and_corrupt(tmp_path):
    assert tg.load_state(tmp_path / 'nie.json') == {}
    bad = tmp_path / 'bad.json'
    bad.write_text('{nie json')
    assert tg.load_state(bad) == {}
    lst = tmp_path / 'list.json'
    lst.write_text('[1]')
    assert tg.load_state(lst) == {}


def test_save_and_load_roundtrip(tmp_path):
    state = {'last_run': 'x', 'pages': {'td': {'links': ['a'], 'checked': 'x', 'error': None}}, 'new_documents': []}
    tg.save_state(state, tmp_path / 's.json')
    assert tg.load_state(tmp_path / 's.json') == state


def test_extract_links_odrzuca_schematy_inne_niz_http():
    from pv_roi_tracker.tariff_guard import extract_links
    html = '<a href="javascript:alert(1)//taryfa">x</a><a href="/a/taryfa.pdf">y</a>'
    assert extract_links(html, 'https://x.pl/', ('taryf',)) == ['https://x.pl/a/taryfa.pdf']
