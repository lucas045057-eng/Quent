import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest


@pytest.fixture
def launch_config(tmp_path):
    from dashboard.backend.config import DashboardConfig
    frontend = Path(__file__).resolve().parents[2]/'dashboard/frontend'
    shutil.copytree(frontend,tmp_path/'dashboard/frontend')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0))
        port = probe.getsockname()[1]
    return DashboardConfig(root=tmp_path,port=port)


def test_real_dashboard_process_serves_api_and_bundle_and_stops_idempotently(launch_config):
    from dashboard.backend.lifecycle import start, stop, status
    config = launch_config
    first = start(config)
    try:
        assert first['state'] == 'RUNNING'
        assert first['url'] == f'http://127.0.0.1:{config.port}'
        assert start(config)['pid'] == first['pid']
        assert status(config)['pid'] == first['pid']
        for path in ('/','/paper','/decisions','/backtests','/health','/logs'):
            with urlopen(first['url']+path,timeout=3) as response:
                assert response.status == 200
                assert b'Quant Core' in response.read()
        with urlopen(first['url']+'/api/health',timeout=3) as response:
            body = json.load(response)
            assert body['schema'] == 'DASHBOARD_API_V1'
            assert body['data']['process_pid'] == first['pid']
        record = json.loads((config.root/'artifacts/dashboard'/f'service-{config.port}.json').read_text())
        assert record['start_ticks'] and record['instance_id']
        assert 'dsn' not in record and 'password' not in json.dumps(record)
    finally:
        assert stop(config)['state'] == 'STOPPED'
    assert stop(config)['state'] == 'STOPPED'
    assert status(config)['state'] == 'STOPPED'


def test_occupied_port_survives_start_attempt(launch_config):
    from dashboard.backend.lifecycle import start
    with socket.socket() as owner:
        owner.bind(('127.0.0.1',launch_config.port))
        owner.listen()
        with pytest.raises(ValueError,match='PORT_IN_USE'):
            start(launch_config)
        assert owner.getsockname()[1] == launch_config.port


def test_stop_does_not_kill_a_reused_pid(launch_config):
    from dashboard.backend.lifecycle import stop
    child = subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    directory = launch_config.root/'artifacts/dashboard'
    directory.mkdir(parents=True)
    (directory/f'service-{launch_config.port}.json').write_text(json.dumps({
        'pid':child.pid,'start_ticks':1,'instance_id':'00000000-0000-4000-8000-000000000001',
        'root':str(launch_config.root),'port':launch_config.port}))
    try:
        assert stop(launch_config)['state'] == 'NOT_OWNED'
        assert child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_cli_rejects_remote_bind(launch_config):
    result = subprocess.run([sys.executable,'-m','dashboard.backend','serve','--root',str(launch_config.root),
        '--port',str(launch_config.port),'--host','0.0.0.0'],capture_output=True,text=True,timeout=10)
    assert result.returncode != 0
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',launch_config.port))


def test_static_assets_do_not_expose_source_or_traversal(launch_config):
    from fastapi.testclient import TestClient
    from dashboard.backend.app import create_app
    api = TestClient(create_app(launch_config),base_url=f'http://127.0.0.1:{launch_config.port}')
    for path in ('/.env','/src/App.tsx','/build-stamp.json','/api/nonexistent'):
        assert api.get(path).status_code == 404
    assert api.get('/assets/../../.env').status_code == 404


def test_bundle_mismatch_prevents_stale_start(launch_config):
    from dashboard.backend.lifecycle import start, verify_bundle
    verify_bundle(launch_config.root)
    (launch_config.root/'dashboard/frontend/src/App.tsx').write_text('changed without rebuild')
    with pytest.raises(ValueError,match='BUNDLE_SOURCE_MISMATCH'):
        start(launch_config)


def test_real_dashboard_restarts_immediately_after_http_connection(launch_config):
    from dashboard.backend.lifecycle import start, stop
    first = start(launch_config)
    with urlopen(first['url']+'/api/health',timeout=3) as response:
        assert response.status == 200
    stop(launch_config)
    second = start(launch_config)
    try:
        assert second['state'] == 'RUNNING'
        assert second['instance_id'] != first['instance_id']
    finally:
        stop(launch_config)
