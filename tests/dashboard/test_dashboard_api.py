import json
from pathlib import Path

import pytest


def client(tmp_path, **kwargs):
    from fastapi.testclient import TestClient
    from dashboard.backend.config import DashboardConfig
    from dashboard.backend.app import create_app
    return TestClient(create_app(DashboardConfig(root=tmp_path, **kwargs)), base_url='http://127.0.0.1:3000')


@pytest.mark.parametrize('route', ['health', 'overview', 'positions', 'orders', 'trades', 'paper'])
def test_read_endpoints_are_real_empty_safe_envelopes(tmp_path, route):
    response = client(tmp_path).get('/api/'+route)
    assert response.status_code == 200
    body = response.json()
    assert body['schema'] == 'DASHBOARD_API_V1'
    assert body['observed_at']
    assert 'sources' in body and 'availability' in body
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert response.headers['cache-control'] == 'no-store'


def test_overview_does_not_invent_zero_balances_or_running_core(tmp_path):
    body = client(tmp_path).get('/api/overview').json()['data']
    assert body['live'] == 'DISABLED'
    assert body['jev'] == 'NOT_CONFIGURED'
    assert body['mode'] == 'IDLE'
    assert body['system'] == 'NO_DATA'
    assert body['database'] == 'NOT_CONFIGURED'
    for value in body['metrics'].values():
        assert value is None
    assert client(tmp_path).get('/api/trades').json()['data']['items'] == []


def test_db_error_does_not_expose_dsn(tmp_path):
    response = client(tmp_path, dsn='postgresql://reader:never-show-secret@127.0.0.1:55449/no_db').get('/api/overview')
    assert response.json()['data']['database'] == 'ERROR'
    assert 'never-show-secret' not in response.text
    assert 'postgresql://' not in response.text


@pytest.mark.parametrize('method', ['post','put','patch','delete'])
def test_no_mutating_api_method(tmp_path, method):
    assert getattr(client(tmp_path), method)('/api/paper').status_code == 405


@pytest.mark.parametrize('headers', [
    {'origin':'https://evil.example'}, {'host':'evil.example'},
    {'sec-fetch-site':'cross-site'}, {'origin':'null'},
    {'origin':'http://localhost:9000'}, {'host':'127.0.0.1:9999'},
])
def test_cross_site_and_rebinding_denied(tmp_path, headers):
    assert client(tmp_path).get('/api/health', headers=headers).status_code == 403


def test_validation_error_does_not_echo_sensitive_query(tmp_path):
    response = client(tmp_path).get('/api/positions?limit=password=secret-value')
    assert response.status_code == 422
    assert 'secret-value' not in response.text
    assert client(tmp_path).get('/api/positions?limit=201').status_code == 422


def test_api_exception_is_generic(tmp_path, monkeypatch):
    from dashboard.backend.service import DashboardService
    def bad(*args, **kwargs):
        raise RuntimeError('password=secret-error')
    monkeypatch.setattr(DashboardService, 'overview', bad)
    response = client(tmp_path).get('/api/overview')
    assert response.status_code == 503
    assert 'secret-error' not in response.text


def test_recorded_paper_is_historical_and_not_current(tmp_path):
    target = tmp_path/'artifacts/phase9/paper/initial.json'
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({'native_engine':'SandboxExecutionClient','pid':9999999,
        'as_of':'2026-01-01T00:00:00+00:00','state':'RECONCILED',
        'acceptance_kind':'FIXTURE_DRIVEN_ACCEPTANCE','position':{'qty':'0.1','equity':'10000'},
        'order_states':{'Q-entry':'FILLED','Q-stop':'ACCEPTED'}}))
    api = client(tmp_path)
    paper = api.get('/api/paper').json()['data']
    assert paper['state'] == 'STOPPED'
    assert paper['sessions'][0]['freshness'] == 'STALE'
    assert api.get('/api/positions').json()['data']['items'] == []
    selected = api.get('/api/positions?session='+paper['sessions'][0]['session_id']).json()
    assert selected['availability'] == 'STALE'
    assert selected['data']['items'][0]['qty'] == '0.1'
    assert api.get('/api/overview').json()['data']['metrics']['equity'] is None
