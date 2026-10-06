from __future__ import annotations

import pytest

import scripts.run_data_layer_failure_matrix as failure_matrix
from scripts.run_data_layer_failure_matrix import (
    APP_MEMORY_LIMIT,
    POSTGRES_MEMORY_LIMIT,
    network_create_args,
    postgres_container_args,
    validate_restart_cycles,
    worker_container_args,
)


def test_failure_matrix_network_is_internal_and_owner_labeled():
    args = network_create_args("quant-dlfm-test-net")

    assert args[:3] == ["docker", "network", "create"]
    assert "--internal" in args
    assert "quant.owner=data-layer-v1-failure-matrix" in args


def test_failure_matrix_postgres_is_ephemeral_private_and_resource_bounded():
    args = postgres_container_args(
        container_name="quant-dlfm-test-pg",
        network_name="quant-dlfm-test-net",
        image="postgres:16.15-bookworm",
    )

    assert "--memory=768m" in args
    assert "--memory-swap=768m" in args
    assert "--cpus=1" in args
    assert "--network" in args and "quant-dlfm-test-net" in args
    assert "--tmpfs" in args
    assert "--publish" not in args
    assert not any("volume" in arg.lower() for arg in args)
    assert POSTGRES_MEMORY_LIMIT == "768m"


def test_restart_worker_is_paper_mode_network_isolated_and_collector_bounded():
    args = worker_container_args(
        container_name="quant-dlfm-test-worker-1",
        network_name="quant-dlfm-test-net",
        image="quant-data-layer-replay-v1:test",
        tests_mount="/repo/tests",
        cycle=1,
    )

    assert "--network" in args and "quant-dlfm-test-net" in args
    assert "--memory=256m" in args
    assert "--memory-swap=256m" in args
    assert "--read-only" in args
    assert "max-file=2" in args
    assert "max-file=1" not in args
    assert "TRADING_MODE=paper" in args
    assert "FAILURE_MATRIX_CYCLE=1" in args
    assert "type=bind,src=/repo/tests,dst=/app/tests,readonly" in args
    assert not any("API_KEY=" in arg or "PASSWORD=" in arg for arg in args)
    assert APP_MEMORY_LIMIT == "256m"


def test_restart_validation_requires_monotonic_cursors_dedup_and_recovery_each_cycle():
    cycles = [
        {
            "cycle": cycle,
            "cursor_before": before,
            "cursor_after": after,
            "event_count": count,
            "distinct_event_count": count,
            "migration_first_count": 15 if cycle == 1 else 0,
            "migration_repeat_count": 0,
            "ws_connect_attempts": 2,
            "ws_subscription_count": 4,
            "ws_ack_count": 2,
            "health_recovered": True,
            "queue_clean": True,
        }
        for cycle, before, after, count in (
            (1, None, 862, 12),
            (2, 862, 874, 24),
            (3, 874, 886, 36),
        )
    ]

    result = validate_restart_cycles(cycles)

    assert result["status"] == "PASS"
    assert result["cycle_count"] == 3
    assert result["final_cursor"] == 886


def test_restart_validation_rejects_cursor_regression_and_duplicate_identity():
    cycles = [
        {
            "cycle": cycle,
            "cursor_before": before,
            "cursor_after": after,
            "event_count": count,
            "distinct_event_count": count,
            "migration_first_count": 15 if cycle == 1 else 0,
            "migration_repeat_count": 0,
            "ws_connect_attempts": 2,
            "ws_subscription_count": 4,
            "ws_ack_count": 2,
            "health_recovered": True,
            "queue_clean": True,
        }
        for cycle, before, after, count in (
            (1, None, 862, 12),
            (2, 862, 874, 24),
            (3, 874, 886, 36),
        )
    ]
    cycles[1]["cursor_after"] = 11
    cycles[1]["distinct_event_count"] = 23

    with pytest.raises(ValueError, match="cursor|identity|migration|reconnect|health|queue"):
        validate_restart_cycles(cycles)


def test_restart_validation_rejects_migration_reapplication_after_fresh_bootstrap():
    cycles = [
        {
            "cycle": cycle,
            "cursor_before": before,
            "cursor_after": after,
            "event_count": count,
            "distinct_event_count": count,
            "migration_first_count": first_count,
            "migration_repeat_count": 0,
            "ws_connect_attempts": 2,
            "ws_subscription_count": 4,
            "ws_ack_count": 2,
            "health_recovered": True,
            "queue_clean": True,
        }
        for cycle, before, after, count, first_count in (
            (1, None, 862, 12, 15),
            (2, 862, 874, 24, 1),
            (3, 874, 886, 36, 0),
        )
    ]

    with pytest.raises(ValueError, match="migration"):
        validate_restart_cycles(cycles)


def test_cli_runner_resolves_replay_helpers_without_repo_root_on_sys_path(monkeypatch):
    import sys

    root = str(failure_matrix.ROOT)
    monkeypatch.setattr(sys, "path", [item for item in sys.path if item not in {"", root}])

    package_hashes, image_hashes = failure_matrix._load_replay_helpers()

    assert callable(package_hashes)
    assert callable(image_hashes)


def test_runner_failure_diagnostic_is_bounded_and_redacts_authenticated_urls():
    diagnostic = failure_matrix._safe_diagnostic(
        "line one\nline two\nPOST https://user:secret@fixture.invalid/path?token=private\n" +
        "tail " * 500
    )

    assert len(diagnostic) <= 1_000
    assert "secret" not in diagnostic
    assert "private" not in diagnostic
    assert "[URL_REDACTED]" in diagnostic


def test_worker_failure_message_keeps_only_bounded_sanitized_runtime_diagnostics():
    message = failure_matrix._worker_failure_message(
        cycle=1,
        exit_code="1",
        logs="Traceback line\nValueError: https://user:secret@fixture.invalid/path?token=private\n",
    )

    assert "worker 1 exited with code 1" in message
    assert "[URL_REDACTED]" in message
    assert "secret" not in message
    assert "private" not in message


def test_worker_failure_diagnostic_includes_stderr_only_traceback():
    logs = failure_matrix._combine_output_streams("", "RuntimeError: isolated test failure")

    assert "RuntimeError: isolated test failure" in logs


def test_bitcoin_and_ethereum_reorg_reconciliation_window_is_bounded_and_stale():
    from datetime import datetime, timezone

    from quant_phase7.contracts import DataStatus
    from quant_phase7.recovery import CheckpointState, Phase7RecoveryCoordinator

    now = datetime(2026, 9, 26, tzinfo=timezone.utc)
    coordinator = Phase7RecoveryCoordinator(max_catch_up=12, max_backfill=144, max_reorg_window=12)
    for source_id, scope_key, block_hash in (
        ("btc_core_rpc", "BITCOIN", "ab" * 32),
        ("ethereum_rpc", "ETHEREUM_MAINNET", "0x" + "ab" * 32),
    ):
        checkpoint = CheckpointState(
            source_id=source_id,
            scope_kind="CHAIN",
            scope_key=scope_key,
            cursor_kind="BLOCK",
            cursor_value="1000",
            last_observed_cursor="1000",
            last_finalized_cursor="990",
            last_block_hash=block_hash,
            parser_version="phase7-chain-v1",
            schema_version="phase7-onchain-v1",
            status=DataStatus.AVAILABLE,
            reason=None,
            updated_at=now,
        )

        result = coordinator.plan_reorg_window(
            checkpoint,
            new_block_hash=("cd" * 32 if source_id == "btc_core_rpc" else "0x" + "cd" * 32),
        )

        assert result.scan_start_cursor == "989"
        assert result.scan_end_cursor == "1000"
        assert int(result.scan_end_cursor) - int(result.scan_start_cursor) + 1 == 12
        assert result.status is DataStatus.STALE
        assert result.reason == "REORG_WINDOW_REQUIRED"


def test_reorg_lookup_beyond_bounded_block_range_fails_before_database_access():
    from quant_phase7.persistence import Phase7Repository

    class NoQueryConnection:
        def cursor(self):
            raise AssertionError("unbounded reorg query must be rejected before database access")

    repository = Phase7Repository(NoQueryConnection())

    with pytest.raises(ValueError, match="bounded range"):
        repository.load_event_ids_for_block_range("BITCOIN", 100, 2_501)


def test_reorg_event_lookup_reads_only_one_sentinel_beyond_cap_then_fails_closed():
    from quant_phase7.persistence import MAX_REORG_EVENTS, Phase7Repository

    class FakeCursor:
        def __init__(self):
            self.query = ""
            self.params = ()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            self.query = query
            self.params = params

        def fetchall(self):
            return [(f"event-{index}",) for index in range(MAX_REORG_EVENTS + 1)]

    cursor = FakeCursor()

    class FakeConnection:
        def cursor(self):
            return cursor

    repository = Phase7Repository(FakeConnection())
    with pytest.raises(ValueError, match="exceeds bounded cap"):
        repository.load_event_ids_for_block_range("ETHEREUM", 100, 100)

    assert "LIMIT %s" in cursor.query
    assert cursor.params == ("ETHEREUM", 100, 100, MAX_REORG_EVENTS + 1)
