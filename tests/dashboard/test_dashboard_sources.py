import json
import subprocess
import sys
from datetime import datetime, timezone

import pytest


@pytest.mark.parametrize('key', ['password','PASSWORD','api_key','access_token','authorization','secret','private_key','dsn'])
def test_recursive_credentials_are_removed(key):
    from dashboard.backend.security import sanitize
    value = sanitize({'safe':{'rows':[{'nested':{key:'credential-value'}}]},'mode':'PAPER'})
    assert 'credential-value' not in json.dumps(value)
    assert value['mode'] == 'PAPER'


@pytest.mark.parametrize('text', [
    'connect postgresql://admin:fixture-credential-value@localhost/quant',
    'password=fixture-credential-value failed', 'api_key: fixture-credential-value',
    'Authorization: Bearer fixture-credential-value',
    'https://example.com/x?token=fixture-credential-value&ok=true',
    '-----BEGIN PRIVATE KEY-----\nfixture-credential-value\n-----END PRIVATE KEY-----',
])
def test_credentials_inside_unstructured_text_are_removed(text):
    from dashboard.backend.security import sanitize
    assert 'credential-value' not in sanitize(text)


@pytest.mark.parametrize('text', [
    '{"password":"fixture-credential-value","api_key":"fixture-credential-value"}',
    'structured log: {"password":"fixture-credential-value"}',
])
def test_quoted_json_credentials_inside_log_text_are_removed(text):
    from dashboard.backend.security import sanitize
    assert 'fixture-credential-value' not in sanitize(text)


def test_json_credentials_in_log_events_cannot_be_searched_or_returned(tmp_path):
    from dashboard.backend.config import DashboardConfig
    from dashboard.backend.db import DatabaseReader
    from dashboard.backend.events import EventReader
    from dashboard.backend.files import FileCatalog

    scope = tmp_path/'artifacts/phase9'
    scope.mkdir(parents=True)
    (scope/'json.log').write_text(
        '2026-09-01T00:00:00Z ERROR engine {"password":"fixture-password-value",'
        '"api_key":"fixture-api-key-value"}\n')
    reader = EventReader(FileCatalog(tmp_path),DatabaseReader(DashboardConfig(root=tmp_path)))

    body = reader.list(limit=200)
    assert 'fixture-password-value' not in str(body)
    assert 'fixture-api-key-value' not in str(body)
    assert reader.list(q='fixture-password-value',limit=200)['data']['items'] == []


def test_file_catalog_limits_and_symlinks(tmp_path):
    from dashboard.backend.files import FileCatalog
    scope = tmp_path/'artifacts/phase9'
    scope.mkdir(parents=True)
    (scope/'report.json').write_text('{"schema":"safe"}')
    outside = tmp_path/'secret.json'
    outside.write_text('{"password":"leak"}')
    (scope/'linked.json').symlink_to(outside)
    (scope/'oversize.json').write_bytes(b'x'*(4*1024*1024+1))
    catalog = FileCatalog(tmp_path)
    assert catalog.scan() == ['artifacts/phase9/report.json']
    assert catalog.read_json('artifacts/phase9/report.json') == {'schema':'safe'}
    for ref in ('../secret.json','secret.json','artifacts/phase9/linked.json','artifacts/phase9/oversize.json'):
        with pytest.raises(ValueError):
            catalog.read_json(ref)


@pytest.mark.parametrize('dsn', ['postgresql://x@remote.example/q','host=/var/run/postgresql dbname=q', 'host=127.0.0.1 hostaddr=8.8.8.8 dbname=q'])
def test_remote_database_config_rejected(tmp_path, dsn):
    from dashboard.backend.config import DashboardConfig
    with pytest.raises(ValueError):
        DashboardConfig(root=tmp_path, dsn=dsn)


def test_config_does_not_read_dotenv(tmp_path, monkeypatch):
    from dashboard.backend.config import DashboardConfig
    monkeypatch.delenv('QUANT_DASHBOARD_DSN', raising=False)
    monkeypatch.setenv('QUANT_DASHBOARD_ROOT', str(tmp_path))
    (tmp_path/'.env').write_text('QUANT_DASHBOARD_DSN=postgresql://secret@remote/q')
    assert DashboardConfig.from_env().dsn is None


def test_fresh_timestamp_and_reused_live_pid_is_not_paper(tmp_path):
    from dashboard.backend.config import DashboardConfig
    from dashboard.backend.paper import PaperReader
    scope = tmp_path/'artifacts/phase9'
    scope.mkdir(parents=True)
    child = subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
    try:
        (scope/'ready.json').write_text(json.dumps({'native_engine':'SandboxExecutionClient',
            'pid':child.pid,'as_of':datetime.now(timezone.utc).isoformat(),'position':{'qty':'2'}}))
        value = PaperReader(DashboardConfig(root=tmp_path)).snapshot()
        assert value['state'] == 'STOPPED'
        assert value['current'] is None
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_actual_module_process_and_ready_file_prove_fresh_paper(tmp_path):
    from dashboard.backend.config import DashboardConfig
    from dashboard.backend.paper import PaperReader
    import time
    package = tmp_path/'quant_nautilus'
    package.mkdir()
    (package/'__init__.py').write_text('')
    (package/'paper.py').write_text('''import json, os, sys, time
from datetime import datetime, timezone
from pathlib import Path
target = Path(sys.argv[sys.argv.index('--ready-file')+1])
while True:
    data = {'pid':os.getpid(),'as_of':datetime.now(timezone.utc).isoformat(),
        'native_engine':'SandboxExecutionClient','network_order_routes':0,'position':{'quantity':'0.25','equity':'10000'},'state':'RECONCILED'}
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(data))
    temporary.replace(target)
    time.sleep(.1)
''')
    scope = tmp_path/'artifacts/phase9'
    scope.mkdir(parents=True)
    ready = scope/'ready.json'
    child = subprocess.Popen([sys.executable,'-m','quant_nautilus.paper','--ready-file',str(ready)],cwd=tmp_path)
    try:
        deadline = time.monotonic()+5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.05)
        assert ready.exists()
        value = PaperReader(DashboardConfig(root=tmp_path)).snapshot()
        assert value['state'] == 'RUNNING'
        assert value['current']['position']['quantity'] == '0.25'
        assert value['sessions'][0]['process_started_at']
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_catalog_file_count_is_bounded(tmp_path):
    from dashboard.backend.files import FileCatalog
    scope = tmp_path/'artifacts/phase9'
    scope.mkdir(parents=True)
    for i in range(270):
        (scope/f'{i:03}.json').write_text('{}')
    assert len(FileCatalog(tmp_path).scan()) == 256
