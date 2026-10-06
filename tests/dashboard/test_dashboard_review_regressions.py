import hashlib
import json
from datetime import datetime, timezone


def test_recent_component_degradation_is_not_masked_by_collector_recovery(tmp_path):
    from dashboard.backend.config import DashboardConfig
    from dashboard.backend.service import DashboardService

    now = datetime.now(timezone.utc).isoformat()
    service = DashboardService(DashboardConfig(root=tmp_path))
    database = {'tables': {'runtime_health_events': [
        {'component': 'phase5-collector', 'state': 'RUNNING', 'created_at': now},
        {'component': 'quant-engine', 'state': 'DEGRADED', 'created_at': now},
    ]}}

    assert service.core_status(database) == 'DEGRADED'


def test_single_component_recovery_does_not_claim_whole_core_is_running(tmp_path):
    from dashboard.backend.config import DashboardConfig
    from dashboard.backend.service import DashboardService

    service = DashboardService(DashboardConfig(root=tmp_path))
    database = {'tables': {'runtime_health_events': [{
        'component': 'phase5-collector', 'state': 'RUNNING',
        'created_at': datetime.now(timezone.utc).isoformat(),
    }]}}

    assert service.core_status(database) == 'PARTIAL'


def test_paper_process_state_is_refreshed_even_while_database_snapshot_is_cached(tmp_path, monkeypatch):
    from dashboard.backend.config import DashboardConfig
    from dashboard.backend.service import DashboardService

    service = DashboardService(DashboardConfig(root=tmp_path))
    database = {'state': 'NOT_CONFIGURED', 'tables': {}, 'warnings': []}
    calls = []
    monkeypatch.setattr(service.database, 'snapshot', lambda: calls.append('database') or database)
    states = iter((
        {'state': 'RUNNING', 'currents': [{'session_id': 'current'}], 'sessions': []},
        {'state': 'STOPPED', 'currents': [], 'sessions': [{'session_id': 'stale'}]},
    ))
    monkeypatch.setattr(service.paper_reader, 'snapshot', lambda: next(states))

    assert service.sources()[1]['state'] == 'RUNNING'
    assert service.sources()[1]['state'] == 'STOPPED'
    assert calls == ['database']


def test_multiline_pem_in_log_is_redacted_as_one_event(tmp_path):
    from dashboard.backend.config import DashboardConfig
    from dashboard.backend.db import DatabaseReader
    from dashboard.backend.events import EventReader
    from dashboard.backend.files import FileCatalog

    scope = tmp_path / 'artifacts' / 'phase9'
    scope.mkdir(parents=True)
    (scope / 'paper.log').write_text(
        '2026-09-01T00:00:00Z ERROR signer signing failed:\n'
        '-----BEGIN PRIVATE KEY-----\n'
        'fixture-private-material-must-not-escape\n'
        '-----END PRIVATE KEY-----\n'
        '2026-09-01T00:00:01Z INFO paper recovered\n'
    )
    reader = EventReader(FileCatalog(tmp_path), DatabaseReader(DashboardConfig(root=tmp_path)))

    result = reader.list()['data']['items']
    rendered = json.dumps(result)
    assert 'fixture-private-material-must-not-escape' not in rendered
    assert 'BEGIN PRIVATE KEY' not in rendered
    assert any(row['module'] == 'paper' and row['message'] == 'recovered' for row in result)
    assert len(result) == 2


def test_truncated_multiline_pem_tail_does_not_expose_body(tmp_path):
    from dashboard.backend.config import DashboardConfig
    from dashboard.backend.db import DatabaseReader
    from dashboard.backend.events import EventReader
    from dashboard.backend.files import FileCatalog

    scope = tmp_path / 'artifacts' / 'phase9'
    scope.mkdir(parents=True)
    (scope / 'partial.log').write_text(
        '2026-09-01T00:00:00Z ERROR signer signing failed:\n'
        '-----BEGIN PRIVATE KEY-----\n'
        'fixture-truncated-private-material\n'
    )
    reader = EventReader(FileCatalog(tmp_path), DatabaseReader(DashboardConfig(root=tmp_path)))

    result = reader.list()['data']['items']
    assert 'fixture-truncated-private-material' not in json.dumps(result)
    assert any('REDACTED_PRIVATE_KEY' in row['message'] for row in result)


def test_null_backtest_metrics_skips_only_malformed_record(tmp_path):
    from dashboard.backend.backtests import BacktestReader
    from dashboard.backend.files import FileCatalog
    from quant_phase9.canonical import canonical_sha256

    scope = tmp_path / 'artifacts' / 'phase9'
    scope.mkdir(parents=True)
    malformed = {
        'schema': 'QUANT_BACKTEST_ACCEPTANCE_V1', 'acceptance_kind': 'FIXTURE_DRIVEN_ACCEPTANCE',
        'passed': True, 'cases': [{'canonical_symbol': 'BTC-USDT-PERP', 'metrics': None}],
    }
    malformed['digest'] = str(canonical_sha256(malformed))
    (scope / 'malformed.json').write_text(json.dumps(malformed))
    valid = {
        'schema': 'QUANT_BACKTEST_ACCEPTANCE_V1', 'acceptance_kind': 'FIXTURE_DRIVEN_ACCEPTANCE',
        'passed': True, 'cases': [{'canonical_symbol': 'ETH-USDT-PERP', 'net_pnl': '-1'}],
    }
    valid['digest'] = str(canonical_sha256(valid))
    (scope / 'valid.json').write_text(json.dumps(valid))

    listing = BacktestReader(FileCatalog(tmp_path)).list()
    assert [row['canonical_symbol'] for row in listing['data']['items']] == ['ETH-USDT-PERP']
    assert 'INVALID_BACKTEST_METRICS' in listing['warnings']
