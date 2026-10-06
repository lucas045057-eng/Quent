import pytest


def events(tmp_path):
    from dashboard.backend.config import DashboardConfig
    from dashboard.backend.db import DatabaseReader
    from dashboard.backend.files import FileCatalog
    from dashboard.backend.events import EventReader
    scope = tmp_path/'artifacts/phase9'
    scope.mkdir(parents=True,exist_ok=True)
    (scope/'paper.log').write_text('2026-09-01T00:00:00Z INFO paper heartbeat observed\n'
        '2026-09-01T00:00:01Z ERROR funding password=fixture-credential-value provider failed\n'
        'plain line with https://example.com/path?api_key=fixture-credential-value\n')
    return EventReader(FileCatalog(tmp_path),DatabaseReader(DashboardConfig(root=tmp_path)))


def test_real_log_tail_has_unknown_level_and_no_secrets(tmp_path):
    data = events(tmp_path).list()['data']['items']
    assert len(data) == 3
    assert 'credential-value' not in str(data)
    assert any(row['level'] == 'UNKNOWN' for row in data)
    assert all(row['source'] == 'artifacts/phase9/paper.log' for row in data)


def test_pem_header_before_last_200_line_cut_cannot_leak_body(tmp_path):
    from dashboard.backend.config import DashboardConfig
    from dashboard.backend.db import DatabaseReader
    from dashboard.backend.events import EventReader
    from dashboard.backend.files import FileCatalog

    scope = tmp_path/'artifacts/phase9'
    scope.mkdir(parents=True)
    lines = ['2026-09-01T00:00:00Z ERROR signer -----BEGIN PRIVATE KEY-----',
        'fixture-truncated-window-private-body']
    lines.extend(f'2026-09-01T00:00:{i % 60:02d}Z INFO paper harmless event {i}' for i in range(198))
    lines.append('-----END PRIVATE KEY-----')
    (scope/'long.log').write_text('\n'.join(lines)+'\n')
    reader = EventReader(FileCatalog(tmp_path),DatabaseReader(DashboardConfig(root=tmp_path)))

    result = reader.list(q='fixture-truncated-window-private-body',limit=200)
    assert result['data']['items'] == []
    assert 'fixture-truncated-window-private-body' not in str(reader.list(limit=200))


def test_byte_tail_start_inside_private_body_is_suppressed_until_footer(tmp_path):
    from dashboard.backend.config import DashboardConfig
    from dashboard.backend.db import DatabaseReader
    from dashboard.backend.events import EventReader
    from dashboard.backend.files import FileCatalog

    scope = tmp_path/'artifacts/phase9'
    scope.mkdir(parents=True)
    body = 'fixture-byte-boundary-private-material-0123456789\n' * 1400
    (scope/'byte-tail.log').write_text(
        '2026-09-01T00:00:00Z ERROR signer -----BEGIN PRIVATE KEY-----\n'
        + body + '-----END PRIVATE KEY-----\n'
        + '2026-09-01T00:01:00Z INFO paper recovered\n')
    reader = EventReader(FileCatalog(tmp_path),DatabaseReader(DashboardConfig(root=tmp_path)))

    result = reader.list(limit=200)
    assert 'fixture-byte-boundary-private-material' not in str(result)
    assert any(row['module'] == 'paper' and row['message'] == 'recovered' for row in result['data']['items'])


@pytest.mark.parametrize('filters,count', [({'level':'ERROR'},1),({'module':'paper'},1),({'q':'heartbeat'},1),({'q':'no-match'},0)])
def test_log_level_module_and_query_filters(tmp_path,filters,count):
    assert len(events(tmp_path).list(**filters)['data']['items']) == count


def test_all_extra_api_endpoints_have_real_empty_states(tmp_path):
    from fastapi.testclient import TestClient
    from dashboard.backend.config import DashboardConfig
    from dashboard.backend.app import create_app
    api = TestClient(create_app(DashboardConfig(root=tmp_path)),base_url='http://127.0.0.1:3000')
    for route in ('decisions','backtests','logs','events'):
        body = api.get('/api/'+route).json()
        assert body['schema'] == 'DASHBOARD_API_V1'
        assert body['data']['items'] == []
