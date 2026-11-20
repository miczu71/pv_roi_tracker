"""Trasy /api/historic/simulate-sc-reprice i apply-sc-reprice (callbacki wstrzykiwane z main)."""
import pytest

from pv_roi_tracker import web


@pytest.fixture
def client():
    web.app.config['TESTING'] = True
    return web.app.test_client()


@pytest.fixture(autouse=True)
def _reset_callbacks():
    yield
    web._simulate_sc_reprice_callback = None
    web._apply_sc_reprice_callback = None


@pytest.mark.parametrize('route,setter', [
    ('simulate-sc-reprice', web.set_simulate_sc_reprice_callback),
    ('apply-sc-reprice', web.set_apply_sc_reprice_callback),
])
class TestRoutes:
    def test_503_without_callback(self, client, route, setter):
        assert client.post(f'/api/historic/{route}').status_code == 503

    def test_200_with_callback(self, client, route, setter):
        setter(lambda: {'months': [{'ym': '2026-07'}], 'total_delta_pln': 1.5})
        resp = client.post(f'/api/historic/{route}')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['ok'] is True
        assert body['months'][0]['ym'] == '2026-07'

    def test_500_on_exception(self, client, route, setter):
        def _boom():
            raise RuntimeError('LTS unreachable')
        setter(_boom)
        resp = client.post(f'/api/historic/{route}')
        assert resp.status_code == 500
        assert resp.get_json()['ok'] is False
