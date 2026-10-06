from __future__ import annotations

from argparse import Namespace
import gzip
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

from scripts import build_data_layer_replay_v1_fixture as fixture_builder
from scripts.run_data_layer_replay_v1 import (
    _format_report,
    _collector_shutdown_ready,
    _engine_shutdown_ready,
    _migrations_match,
    _phase7_failure_message,
    app_container_args,
    network_create_args,
    postgres_container_args,
)


def test_replay_postgres_is_private_bounded_and_ephemeral():
    network_args = network_create_args(network_name="quant-dlr-test-net")
    args = postgres_container_args(
        container_name="quant-dlr-test-pg",
        network_name="quant-dlr-test-net",
        image="postgres:16.15-bookworm",
    )

    assert "--network" in args
    assert "--internal" in network_args
    assert "quant.owner=data-layer-v1-replay" in network_args
    assert "quant-dlr-test-net" in args
    assert "--memory=768m" in args
    assert "--cpus=1" in args
    assert "--publish" not in args
    assert "--tmpfs" in args
    assert "--label" in args
    assert not any("0.0.0.0" in arg for arg in args)
    assert not any("password=" in arg.lower() for arg in args)


def test_replay_application_has_paper_mode_fixed_caps_and_no_external_network():
    collector = app_container_args(
        container_name="quant-dlr-test-collector",
        network_name="quant-dlr-test-net",
        image="quant-app:test",
        role="collector",
        dsn="postgresql://quant@quant-postgres:5432/quant",
        tests_mount="/workspace/tests",
        scripts_mount="/workspace/scripts",
    )
    engine = app_container_args(
        container_name="quant-dlr-test-engine",
        network_name="quant-dlr-test-net",
        image="quant-app:test",
        role="engine",
        dsn="postgresql://quant@quant-postgres:5432/quant",
        tests_mount="/workspace/tests",
        scripts_mount="/workspace/scripts",
    )

    assert "--memory=256m" in collector
    assert "--memory=384m" in engine
    assert all("--network" in args and "quant-dlr-test-net" in args for args in (collector, engine))
    assert all("TRADING_MODE=paper" in args for args in (collector, engine))
    assert all("PHASE7_RESOURCE_REPLAY_V3=1" in args for args in (collector, engine))
    assert "REPLAY_COLLECTOR_MAX_LIFETIME_SECONDS=390" in collector
    assert not any("REPLAY_COLLECTOR_MAX_LIFETIME_SECONDS=" in arg for arg in engine)
    assert all(
        any("postgresql://quant@quant-postgres:5432/quant" in arg for arg in args)
        for args in (collector, engine)
    )
    assert all("--network=host" not in args for args in (collector, engine))
    assert all("/workspace/scripts:/app/scripts:ro" in args for args in (collector, engine))
    assert all(not any("API_KEY=" in arg or "PASSWORD=" in arg for arg in args) for args in (collector, engine))


def test_manifest_build_pins_existing_phase7_memory_fixture():
    records = fixture_builder.build_records()
    raw = b"".join(fixture_builder._canonical_json(record) for record in records)
    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    args = Namespace(
        git_sha="8cadf8dc0da0d77f65f47e4936845e3f1186f919",
        collector_image_digest="sha256:" + "1" * 64,
        engine_image_digest="sha256:" + "1" * 64,
        postgres_image_digest="sha256:" + "2" * 64,
    )

    manifest = fixture_builder._write_manifest(args, raw, compressed, len(records))
    components = manifest["replay_components_sha256"]

    assert "scripts/phase7_collector_memory_profile.py" in components
    assert all((fixture_builder.ROOT / name).is_file() for name in components)


def test_phase8_replay_container_is_internal_and_memory_bounded():
    args = app_container_args(
        container_name="quant-dlr-test-phase8",
        network_name="quant-dlr-test-net",
        image="quant-app:test",
        role="phase8",
        dsn="postgresql://quant@quant-postgres:5432/quant",
        tests_mount="/workspace/tests",
        scripts_mount="/workspace/scripts",
    )

    assert "--memory=384m" in args
    assert "--memory-swap=384m" in args
    assert "quant-dlr-test-net" in args
    assert "TRADING_MODE=paper" in args
    assert "--network=host" not in args
    assert "/workspace/scripts:/app/scripts:ro" in args


def test_replay_report_row_includes_phase8_helper_memory_column():
    manifest = SimpleNamespace(
        replay_version="DATA_LAYER_REPLAY_V1",
        git_sha="test-sha",
        collector_image_digest="app-image",
        engine_image_digest="app-image",
        postgres_image_digest="postgres-image",
        postgres_server_version="16.15",
        migration_version=15,
        dataset_sha256="dataset-sha",
        fixture=SimpleNamespace(record_count=1, compressed_bytes=1, uncompressed_bytes=1),
    )
    outcome = {
        "run": 1,
        "collector": {"duration_seconds": 1, "peak_memory_bytes_observed": 256},
        "engine": {"peak_memory_bytes_observed": 384},
        "phase8_role": {"peak_memory_bytes_observed": 512},
        "database": {"database_size_bytes": 1, "table_counts": {}, "bitcoin_large_block_event_count": 0},
        "phase8": {"persisted_counts": {"contexts": 2}, "persisted_sha256": "sha", "context_statuses": {}, "idempotency_pass": True},
        "collector_markers": {},
        "engine_markers": {},
    }
    outcome["database"].update({
        "stable_table_sha256": {},
        "migration_versions": list(range(1, 16)),
        "chain_event_counts": {},
        "cursors": [],
    })

    report = _format_report(
        manifest,
        [outcome],
        SimpleNamespace(equal=True, reference_sha256="sha", differences=()),
        {"filesystem_free_ratio": 0.9, "filesystem_free_bytes": 900},
        {"filesystem_free_ratio": 0.9, "filesystem_free_bytes": 900, "inode_use_percent": "1%"},
    )

    assert "Phase 8 helper observed peak bytes" in report
    assert "| 1 | 1 | 256 | 384 | 512 | 1 | 0 | 0 | 2 |" in report


def test_collector_shutdown_requires_ready_marker_and_is_single_flight(tmp_path):
    assert not _collector_shutdown_ready("collector", tmp_path, False)
    assert not _collector_shutdown_ready("engine", tmp_path, False)
    (tmp_path / "collector-replay-ready.json").write_text("{}", encoding="utf-8")
    assert not _collector_shutdown_ready("collector", tmp_path, False)
    (tmp_path / "phase7-resource-replay-ready.json").write_text(
        '{"block_height":968443,"checkpoint_committed":true,"event_count":12665}',
        encoding="utf-8",
    )
    assert _collector_shutdown_ready("collector", tmp_path, False)
    assert not _collector_shutdown_ready("collector", tmp_path, True)


def test_collector_shutdown_rejects_incomplete_phase7_replay_marker(tmp_path):
    (tmp_path / "collector-replay-ready.json").write_text("{}", encoding="utf-8")
    marker = tmp_path / "phase7-resource-replay-ready.json"
    marker.write_text(
        '{"block_height":968443,"checkpoint_committed":false,"event_count":12665}',
        encoding="utf-8",
    )
    assert not _collector_shutdown_ready("collector", tmp_path, False)
    marker.write_text(
        '{"block_height":968443,"checkpoint_committed":true,"event_count":12664}',
        encoding="utf-8",
    )
    assert not _collector_shutdown_ready("collector", tmp_path, False)


def test_collector_shutdown_stops_promptly_after_safe_phase7_failure_marker(tmp_path):
    (tmp_path / "collector-replay-ready.json").write_text("{}", encoding="utf-8")
    (tmp_path / "phase7-source-failure.json").write_text(
        '{"component":"bitcoin_rpc","exception_type":"TypeError",'
        '"failure_stage":"BLOCK_PERSISTENCE","runtime_stage":"BLOCK_PERSISTENCE","status":"ERROR"}',
        encoding="utf-8",
    )
    assert _collector_shutdown_ready("collector", tmp_path, False)


def test_phase7_failure_report_does_not_echo_untrusted_marker_values():
    message = _phase7_failure_message({
        "component": "bitcoin_rpc",
        "status": "ERROR",
        "exception_type": "TypeError",
        "failure_stage": "BLOCK_PERSISTENCE",
        "runtime_stage": "BLOCK_PERSISTENCE",
        "endpoint": "https://user:secret@private.example/path?token=secret",
    })

    assert message == "Phase 7 Bitcoin replay failed: TypeError at BLOCK_PERSISTENCE (runtime=BLOCK_PERSISTENCE)"
    assert "secret" not in message
    assert _phase7_failure_message({"component": "bitcoin_rpc", "status": "ERROR", "exception_type": "secret"}) is None


def test_engine_shutdown_requires_phase2_cycle_marker_and_is_single_flight(tmp_path):
    assert not _engine_shutdown_ready("engine", tmp_path, False)
    (tmp_path / "engine-phase2-cycle.json").write_text("{}", encoding="utf-8")
    assert _engine_shutdown_ready("engine", tmp_path, False)
    assert not _engine_shutdown_ready("collector", tmp_path, False)
    assert not _engine_shutdown_ready("engine", tmp_path, True)


def test_migration_snapshot_requires_complete_ordered_numbered_set():
    migrations = [f"{number:03d}_migration.sql" for number in range(1, 16)]
    assert _migrations_match(migrations, 15)
    with_repair_marker = [*migrations[:9], "009_phase4_metrics.repair.v1", *migrations[9:]]
    assert _migrations_match(with_repair_marker, 15)
    assert not _migrations_match(migrations[:-1], 15)
    assert not _migrations_match([migrations[1], migrations[0], *migrations[2:]], 15)
    assert not _migrations_match([*migrations, "016_unknown.sql"], 15)


def test_phase8_replay_support_loads_without_pytest_dependency(monkeypatch):
    helper_path = Path(fixture_builder.ROOT) / "tests/data_layer_replay_v1_db_runner.py"
    helper_spec = importlib.util.spec_from_file_location("phase8_db_replay_under_test", helper_path)
    assert helper_spec is not None and helper_spec.loader is not None
    helper = importlib.util.module_from_spec(helper_spec)
    sys.modules[helper_spec.name] = helper
    helper_spec.loader.exec_module(helper)
    monkeypatch.setitem(sys.modules, "pytest", None)

    replay_module = helper._load_phase8_replay_module()

    assert callable(replay_module._settings)
    assert callable(replay_module._replay_once)
    assert "pytest" not in replay_module.__dict__ or replay_module.pytest is not None
    assert helper._migration_versions([("001_phase1_core.sql",), ("015_data_layer_runtime.sql",)]) == [
        "001_phase1_core.sql", "015_data_layer_runtime.sql",
    ]
