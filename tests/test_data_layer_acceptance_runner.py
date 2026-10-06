from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import uuid

import pytest

from scripts.data_layer_acceptance import (
    ACCEPTANCE_DURATION_SECONDS,
    ACCEPTANCE_SAMPLE_INTERVAL_SECONDS,
    BoundedJsonlWriter,
    RunDeadline,
    RuntimeTelemetry,
    acceptance_project_name,
    acceptance_gates_pass,
    build_failure_matrix,
    build_acceptance_gate_results,
    derive_phase_statuses,
    derive_phase_degradation_evidence,
    derive_optional_source_configuration,
    evaluate_hard_stop,
    find_numeric_cursor_rollbacks,
    load_public_source_config,
    HostCpuSampler,
    parse_cgroup_resource_sample,
    parse_host_cpu_counters,
    phase_table_activity,
    render_compose_model,
    run_sampling_window,
    validate_compose_model,
    validate_public_rpc_endpoint,
)


def test_acceptance_deadline_is_fixed_and_uses_monotonic_remaining_time():
    class Clock:
        value = 12.5

        def __call__(self):
            return self.value

    clock = Clock()
    deadline = RunDeadline(clock=clock)

    assert deadline.duration_seconds == ACCEPTANCE_DURATION_SECONDS == 900
    assert deadline.remaining_seconds() == 900
    clock.value += 17.25
    assert deadline.remaining_seconds() == pytest.approx(882.75)
    clock.value += 1_000
    assert deadline.remaining_seconds() == 0


def test_sample_interval_is_ten_seconds_and_never_starts_a_sample_at_the_deadline():
    from scripts.data_layer_acceptance import scheduled_sample_offsets

    offsets = scheduled_sample_offsets()

    assert ACCEPTANCE_SAMPLE_INTERVAL_SECONDS == 10
    assert len(offsets) == 90
    assert offsets[0] == 0
    assert offsets[-1] == 890


def test_sampling_window_runs_to_fixed_deadline_without_a_deadline_sample():
    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()
    deadline = RunDeadline(clock=clock)
    samples = []

    result = run_sampling_window(
        lambda sequence, offset: samples.append((sequence, offset, clock.value)),
        hard_stop=lambda: None,
        deadline=deadline,
        sleep=lambda seconds: setattr(clock, "value", clock.value + seconds),
    )

    assert result["status"] == "COMPLETED"
    assert result["sample_count"] == 90
    assert result["missed_intervals"] == 0
    assert clock.value == 900
    assert samples[0] == (1, 0, 0.0)
    assert samples[-1] == (90, 890, 890.0)


def test_sampling_window_stops_immediately_after_hard_safety_condition():
    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()
    deadline = RunDeadline(clock=clock)
    samples = []

    def sample(sequence, offset):
        samples.append((sequence, offset))
        clock.value = float(offset)

    result = run_sampling_window(
        sample,
        hard_stop=lambda: "OOM_KILL_DELTA" if len(samples) == 3 else None,
        deadline=deadline,
        sleep=lambda seconds: setattr(clock, "value", clock.value + seconds),
    )

    assert result["status"] == "HARD_STOP"
    assert result["stop_reason"] == "OOM_KILL_DELTA"
    assert result["sample_count"] == 3
    assert samples == [(1, 0), (2, 10), (3, 20)]


def test_sampling_window_honors_safe_interrupt_and_records_missed_intervals():
    class Clock:
        value = 0.0

        def __call__(self):
            return self.value

    clock = Clock()
    deadline = RunDeadline(clock=clock)
    samples = []
    interrupted = False

    def sample(sequence, offset):
        nonlocal interrupted
        samples.append((sequence, offset))
        clock.value += 12
        interrupted = sequence == 2

    result = run_sampling_window(
        sample,
        hard_stop=lambda: None,
        deadline=deadline,
        sleep=lambda seconds: setattr(clock, "value", clock.value + seconds),
        stop_requested=lambda: interrupted,
    )

    assert result["status"] == "INTERRUPTED"
    assert result["sample_count"] == 2
    assert result["missed_intervals"] == 2
    assert samples == [(1, 0), (2, 20)]


def test_numeric_cursor_rollback_is_detected_without_recording_cursor_identity():
    rolled_back = find_numeric_cursor_rollbacks(
        {"bitcoin:mainnet": "100", "ethereum:mainnet": "0x20", "opaque": "abc"},
        {"bitcoin:mainnet": "99", "ethereum:mainnet": "0x21", "opaque": "abb"},
    )

    assert len(rolled_back) == 1
    assert rolled_back[0] != "bitcoin:mainnet"
    assert find_numeric_cursor_rollbacks({"x": "abc"}, {"x": "abb"}) == []


def test_hard_stop_contract_covers_disk_oom_cursor_and_secret_conditions():
    safe_events = {"quant-collector": {"oom": 0, "oom_kill": 0}}
    assert evaluate_hard_stop(
        disk_free_ratio=0.15,
        previous_memory_events=safe_events,
        current_memory_events=safe_events,
    ) is None
    assert evaluate_hard_stop(
        disk_free_ratio=0.149,
        previous_memory_events=safe_events,
        current_memory_events=safe_events,
    ) == "DISK_FREE_BELOW_15_PERCENT"
    assert evaluate_hard_stop(
        disk_free_ratio=0.5,
        previous_memory_events=safe_events,
        current_memory_events={"quant-collector": {"oom": 1, "oom_kill": 0}},
    ) == "OOM_EVENT_DELTA"
    assert evaluate_hard_stop(
        disk_free_ratio=0.5,
        previous_memory_events=safe_events,
        current_memory_events=safe_events,
        cursor_rollback_ids=("opaque-id",),
    ) == "NUMERIC_CURSOR_ROLLBACK"
    assert evaluate_hard_stop(
        disk_free_ratio=0.5,
        previous_memory_events=safe_events,
        current_memory_events=safe_events,
        secret_leak_found=True,
    ) == "SECRET_LEAK_DETECTED"


def test_cgroup_resource_parser_preserves_cgroup_and_process_metrics():
    metrics = parse_cgroup_resource_sample(
        "memory_current 1000\nmemory_peak 1800\nmemory_limit 4096\n"
        "event_max 2\nevent_oom 0\nevent_oom_kill 0\npids_current 4\n"
        "proc_VmRSS 2\nproc_VmSize 8\nproc_Threads 3\nsmaps_Pss 1\n"
        "cpu_usage_usec 900\ncpu_throttled_usec 10\ncpu_nr_throttled 1\n"
    )

    assert metrics["memory_current_bytes"] == 1000
    assert metrics["memory_peak_bytes"] == 1800
    assert metrics["memory_limit_bytes"] == 4096
    assert metrics["process_rss_bytes"] == 2048
    assert metrics["process_pss_bytes"] == 1024
    assert metrics["cpu_usage_usec"] == 900


def test_host_cpu_sampler_returns_no_fake_first_sample_and_then_busy_percent():
    lines = iter(("cpu 100 0 20 880 0", "cpu 120 0 30 890 0"))
    sampler = HostCpuSampler(reader=lambda: next(lines))

    assert parse_host_cpu_counters("cpu 100 0 20 880 0") == (1000, 880)
    assert sampler.sample_percent() is None
    assert sampler.sample_percent() == 75.0


def test_targeted_window_is_bounded_and_never_looks_like_stage8_acceptance():
    import scripts.data_layer_acceptance as acceptance

    assert acceptance.RunDeadline(duration_seconds=180).duration_seconds == 180
    assert acceptance.scheduled_sample_offsets(180) == tuple(range(0, 180, 10))

    report = acceptance._build_final_report(
        run_id="targeted-1",
        project="quant-dlv1-accept-targeted-1",
        image="quant-data-layer-acceptance:targeted-1",
        configured_keys=(),
        telemetry=None,
        sampling_result={
            "status": "COMPLETED", "sample_count": 18, "missed_intervals": 0,
            "elapsed_seconds": 180.0, "stop_reason": None,
        },
        shutdown={"clean_shutdown": True},
        startup_error=None,
        build_seconds=1.0,
        startup_seconds=1.0,
        proxy_preflight_status="PASS",
        run_mode="TARGETED_DIAGNOSTIC",
    )

    assert report["report_type"] == "DATA_LAYER_V1_TARGETED_RUNTIME_DIAGNOSTIC"
    assert report["acceptance_evaluated"] is False
    assert report["acceptance_status"] == "NOT_EVALUATED_TARGETED"
    fixed_window = next(gate for gate in report["gates"] if gate["gate"] == "fixed_15_minute_window")
    assert fixed_window["status"] == "NOT_EVALUATED"


def test_targeted_cli_is_explicit_and_preserves_default_stage8_duration(monkeypatch):
    import scripts.data_layer_acceptance as acceptance

    calls = []
    monkeypatch.setattr(acceptance, "_run_acceptance", lambda **kwargs: calls.append(kwargs) or 0)

    assert acceptance.main() == 0
    assert calls[-1] == {}
    assert acceptance.main(("--targeted-seconds", "180")) == 0
    assert calls[-1] == {"duration_seconds": 180, "diagnostic_only": True}


def test_phase_table_activity_uses_counter_deltas_not_full_table_scans():
    result = phase_table_activity(
        {
            "market_snapshots": {"n_tup_ins": 5, "n_tup_upd": 7, "n_tup_del": 0},
            "phase8_option_market_snapshots": {"n_tup_ins": 3, "n_tup_upd": 1, "n_tup_del": 0},
        },
        {
            "market_snapshots": {"n_tup_ins": 2, "n_tup_upd": 2, "n_tup_del": 0},
            "phase8_option_market_snapshots": {"n_tup_ins": 3, "n_tup_upd": 0, "n_tup_del": 0},
        },
    )

    assert result[1] == 8
    assert result[8] == 1


def test_runtime_telemetry_streams_safe_measurements_and_hides_raw_cursor_values(tmp_path, monkeypatch):
    import types
    import scripts.data_layer_acceptance as acceptance

    resource = {
        "memory_current_bytes": 10,
        "memory_peak_bytes": 20,
        "memory_limit_bytes": 768 * 1024 * 1024,
        "memory_events_max": 0,
        "memory_events_oom": 0,
        "memory_events_oom_kill": 0,
        "pids_current": 2,
        "process_rss_bytes": 8,
        "process_vmsize_bytes": 30,
        "process_threads": 1,
        "process_pss_bytes": 7,
        "cpu_usage_usec": 4,
        "cpu_throttled_usec": 0,
        "cpu_nr_throttled": 0,
    }
    monkeypatch.setattr(acceptance, "_container_state", lambda _cid: {
        "status": "running", "health": "healthy", "oom_killed": False, "exit_code": 0, "restart_count": 0,
    })
    def resource_for(service):
        return {**resource, "memory_limit_bytes": {
            "postgres": 768 * 1024 * 1024,
            "quant-collector": 256 * 1024 * 1024,
            "quant-engine": 384 * 1024 * 1024,
        }[service]}

    monkeypatch.setattr(acceptance, "_container_resource_sample", resource_for)
    monkeypatch.setattr(acceptance, "_container_log_sample", lambda _cid, _secrets: {
        "status": "AVAILABLE", "bytes": 10, "sampled_tail_bytes": 10, "secret_leak_found": False,
    })
    monkeypatch.setattr(acceptance, "_database_snapshot", lambda *_args, **_kwargs: {
        "database": {"name": "quant", "timezone": "UTC", "size_bytes": 100, "commits": 1, "rollbacks": 0,
                     "tuples_inserted": 1, "tuples_returned": 2, "active_transactions": 0, "idle_in_transaction": 0},
        "migrations": ["001.sql"],
        "health": {},
        "table_stats": {"market_snapshots": {"estimated_rows": 1, "n_tup_ins": 2, "n_tup_upd": 0,
                                               "n_tup_del": 0, "n_dead_tup": 0}},
        "checkpoints": {"count": 1, "sampled_count": 1, "truncated": False, "status_counts": {"AVAILABLE": 1}},
        "_numeric_cursors": {"private-scope-identity": "987654321"},
        "stage1_categories": None,
    })
    monkeypatch.setattr(acceptance, "shutil", types.SimpleNamespace(disk_usage=lambda _path: types.SimpleNamespace(
        total=1000, used=200, free=800,
    )))
    monkeypatch.setattr(acceptance, "HostCpuSampler", lambda: types.SimpleNamespace(sample_percent=lambda: 12.5))

    artifact = tmp_path / "samples.jsonl"
    telemetry = RuntimeTelemetry(
        container_ids={service: service for service in acceptance.SERVICES},
        writer=BoundedJsonlWriter(artifact, secret_values=("987654321",)),
        secret_values=("configured-rpc-key-marker",),
    )
    telemetry.sample(1, 0)

    text = artifact.read_text(encoding="utf-8")
    assert "987654321" not in text
    assert "private-scope-identity" not in text
    assert "configured-rpc-key-marker" not in text
    assert telemetry.last_sample_record["database"]["database"]["timezone"] == "UTC"
    assert telemetry.memory_caps_respected
    assert telemetry.services_stable


def test_phase_status_keeps_partial_and_unconfigured_ai_distinct():
    statuses = derive_phase_statuses(
        health={
            "phase8-options": {"status": "NOT_AVAILABLE", "details": {"phase8_status": "PARTIAL"}},
            "phase6-ai": {"status": "NOT_AVAILABLE", "details": {"phase6_ai_status": "NOT_CONFIGURED"}},
        },
        phase_activity={},
        table_stats={"phase8_option_market_snapshots": {"n_tup_ins": 0}},
    )

    assert statuses["phase8"] == "PARTIAL"
    assert statuses["phase6_ai"] == "NOT_CONFIGURED"


def test_phase5_and_phase6_data_status_are_not_rewritten_by_health_heartbeat_age():
    health = {
        "phase5-context": {
            "status": "NOT_AVAILABLE",
            "age_seconds": 240,
            "details": {"phase5_status": "PARTIAL"},
        },
        "phase6-news-ingestion": {
            "status": "NOT_AVAILABLE",
            "age_seconds": 900,
            "details": {"phase6_status": "NOT_AVAILABLE"},
        },
    }

    statuses = derive_phase_statuses(health=health, phase_activity={}, table_stats={})

    assert statuses["phase5"] == "PARTIAL"
    assert statuses["phase6"] == "NOT_AVAILABLE"


def test_phase6_source_not_configured_is_not_misreported_as_stale():
    statuses = derive_phase_statuses(
        health={
            "phase6-news-ingestion": {
                "status": "NOT_AVAILABLE",
                "age_seconds": 900,
                "details": {
                    "phase6_status": "NOT_AVAILABLE",
                    "reason_code": "SOURCE_NOT_CONFIGURED",
                    "source_count": 0,
                    "configured": False,
                },
            },
        },
        phase_activity={},
        table_stats={},
    )

    assert statuses["phase6"] == "NOT_CONFIGURED"


def test_phase6_source_without_explicit_unconfigured_evidence_is_not_promoted():
    statuses = derive_phase_statuses(
        health={
            "phase6-news-ingestion": {
                "status": "NOT_AVAILABLE",
                "age_seconds": 10,
                "details": {
                    "phase6_status": "NOT_AVAILABLE",
                    "reason_code": "SOURCE_NOT_CONFIGURED",
                    "source_count": 0,
                    "configured": True,
                },
            },
        },
        phase_activity={},
        table_stats={},
    )

    assert statuses["phase6"] == "NOT_AVAILABLE"


def test_phase7_available_source_does_not_hide_another_source_degradation():
    statuses = derive_phase_statuses(
        health={
            "phase7-bitcoin-rpc": {
                "status": "NOT_AVAILABLE",
                "age_seconds": 5,
                "details": {
                    "phase7_status": "PARTIAL",
                    "runtime_state": "DEGRADED",
                    "data_quality": "PARTIAL",
                    "reason": "NETWORK_TIMEOUT",
                },
            },
            "phase7-ethereum-rpc": {
                "status": "AVAILABLE",
                "age_seconds": 1,
                "details": {"phase7_status": "AVAILABLE"},
            },
        },
        phase_activity={7: 3},
        table_stats={"phase7_ingestion_checkpoints": {"n_tup_upd": 3}},
    )

    assert statuses["phase7"] == "PARTIAL"


def test_phase7_three_consecutive_failures_remain_error_even_when_eth_is_usable():
    health = {
        "bitcoin_rpc": {
            "status": "ERROR",
            "age_seconds": 5,
            "details": {
                "phase7_status": "ERROR",
                "data_quality": "PARTIAL",
                "error_category": "NETWORK",
                "reason": "TRANSPORT_FAILURE",
                "consecutive_failures": 3,
            },
        },
        "ethereum_rpc": {
            "status": "AVAILABLE",
            "age_seconds": 1,
            "details": {"phase7_status": "AVAILABLE"},
        },
        "binance_spot": {
            "status": "ERROR",
            "age_seconds": 5,
            "details": {
                "phase7_status": "ERROR",
                "data_quality": "PARTIAL",
                "error_category": "NETWORK",
                "reason": "NETWORK_TIMEOUT",
                "consecutive_failures": 5,
            },
        },
    }

    statuses = derive_phase_statuses(
        health=health,
        phase_activity={7: 3},
        table_stats={"phase7_ingestion_checkpoints": {"n_tup_upd": 3}},
    )
    evidence = derive_phase_degradation_evidence(health)

    assert statuses["phase7"] == "PARTIAL"
    assert evidence["phase7"] is True


def test_phase5_partial_requires_explicit_output_and_missing_evidence_counts():
    valid = {
        "phase5-context": {
            "status": "NOT_AVAILABLE",
            "details": {
                "phase5_status": "PARTIAL",
                "data_quality": "PARTIAL",
                "reason": "PARTIAL_CONTEXT_OUTPUTS",
                "partial_output_count": 12,
                "missing_evidence_count": 9,
            },
        },
    }
    invalid = {
        "phase5-context": {
            "status": "NOT_AVAILABLE",
            "details": {
                "phase5_status": "PARTIAL",
                "data_quality": "PARTIAL",
                "reason": "PARTIAL_CONTEXT_OUTPUTS",
            },
        },
    }

    assert derive_phase_degradation_evidence(valid)["phase5"] is True
    assert derive_phase_degradation_evidence(invalid)["phase5"] is False


def test_phase7_parser_error_is_not_downgraded_by_another_available_source():
    statuses = derive_phase_statuses(
        health={
            "binance_spot": {
                "status": "ERROR",
                "details": {
                    "phase7_status": "ERROR",
                    "error_category": "PARSER",
                    "reason": "SCHEMA_MISMATCH",
                },
            },
            "ethereum_rpc": {"status": "AVAILABLE", "details": {"phase7_status": "AVAILABLE"}},
        },
        phase_activity={7: 3},
        table_stats={"phase7_ingestion_checkpoints": {"n_tup_upd": 3}},
    )

    assert statuses["phase7"] == "ERROR"


def test_phase4_long_short_schema_failure_cannot_become_accepted_degradation():
    health = {
        "phase4-long_short": {
            "status": "NOT_AVAILABLE",
            "age_seconds": 1,
            "details": {
                "phase4_status": "PARTIAL",
                "data_quality": "PARTIAL",
                "reason": "LONG_SHORT_PROVIDER_DEGRADED",
                "error_category": "SCHEMA_ERROR",
                "provider": "bitget",
                "schema_stage": "response_validation",
            },
        },
        "phase4-liquidation": {
            "status": "NOT_AVAILABLE",
            "age_seconds": 1,
            "details": {
                "phase4_status": "PARTIAL",
                "data_quality": "PARTIAL",
                "reason": "LIQUIDATION_GAP_NO_BACKFILL",
                "gap_detected": True,
                "gap_reason": "LIQUIDATION_GAP_NO_BACKFILL",
                "gap_watermark_received_at": "2026-09-27T00:00:00+00:00",
                "gap_watermark_event_timestamp": "2026-09-27T00:00:00+00:00",
            },
        },
    }

    statuses = derive_phase_statuses(health=health, phase_activity={}, table_stats={})
    evidence = derive_phase_degradation_evidence(health)

    assert statuses["phase4"] == "ERROR"
    assert evidence["phase4"] is False


def test_phase4_explicit_network_failure_with_usable_child_is_partial():
    health = {
        "phase4-long_short": {
            "status": "NOT_AVAILABLE",
            "age_seconds": 1,
            "details": {
                "phase4_status": "PARTIAL",
                "data_quality": "PARTIAL",
                "reason": "LONG_SHORT_PROVIDER_DEGRADED",
                "error_category": "NETWORK_ERROR",
                "provider": "bitget",
                "endpoint": "bitget_classic_v2_long_short",
                "usable_source_count": 1,
            },
        },
    }

    assert derive_phase_statuses(health=health, phase_activity={}, table_stats={})["phase4"] == "PARTIAL"
    assert derive_phase_degradation_evidence(health)["phase4"] is True


def test_phase8_activity_never_substitutes_for_health_or_configuration_evidence():
    statuses = derive_phase_statuses(
        health={},
        phase_activity={8: 20},
        table_stats={"phase8_option_context_snapshots": {"n_tup_ins": 20}},
    )

    assert statuses["phase8"] == "UNPROVEN"


def test_phase8_explicitly_disabled_is_distinct_from_missing_health():
    health = {
        "quant-phase8-options": {
            "status": "NOT_AVAILABLE",
            "age_seconds": 1,
            "details": {
                "configured": False,
                "phase8_status": "NOT_CONFIGURED",
                "runtime_state": "DISABLED",
                "reason": "DISABLED",
            },
        },
    }

    statuses = derive_phase_statuses(health=health, phase_activity={}, table_stats={})
    configuration = derive_optional_source_configuration(health)

    assert statuses["phase8"] == "NOT_CONFIGURED"
    assert configuration["phase8"] is False


def test_phase8_configured_without_health_remains_unproven_and_fails_closed():
    statuses = derive_phase_statuses(
        health={},
        phase_activity={},
        table_stats={"phase8_option_context_snapshots": {"n_tup_ins": 0}},
    )

    assert statuses["phase8"] == "UNPROVEN"


def test_phase8_optional_disabled_gate_requires_explicit_configuration_evidence():
    gates = build_acceptance_gate_results(
        window_status="COMPLETED",
        missed_intervals=0,
        migrations_match=True,
        database_identity_utc=True,
        services_stable=True,
        core_health_fresh=True,
        phase1_market_activity=True,
        phase1_stage1_activity=True,
        resource_measurements_complete=True,
        memory_caps_respected=True,
        cursor_observability_complete=True,
        phase_statuses={
            f"phase{phase}": "AVAILABLE" for phase in range(2, 9)
        } | {"phase8": "NOT_CONFIGURED", "phase6_ai": "NOT_CONFIGURED"},
        optional_source_configuration={"phase8": False, "phase6": True, "phase6_ai": False},
        minimum_disk_free_ratio=0.5,
        hard_stop_reason=None,
        logs_audited=True,
        secret_leak_found=False,
        clean_shutdown=True,
    )

    assert next(row for row in gates if row["gate"] == "phase8")["status"] == "PASS"


def test_degradation_evidence_requires_reason_and_partial_data_quality():
    verified = derive_phase_degradation_evidence({
        "phase4-liquidation": {
            "status": "NOT_AVAILABLE",
            "details": {
                "phase4_status": "PARTIAL",
                "data_quality": "PARTIAL",
                "reason": "LIQUIDATION_GAP_AWAITING_FRESH_EVENT",
                "gap_detected": True,
                "gap_reason": "LIQUIDATION_GAP_NO_BACKFILL",
                "gap_watermark_received_at": "2026-09-27T00:00:00+00:00",
                "gap_watermark_event_timestamp": "2026-09-27T00:00:00+00:00",
            },
        },
    })
    missing_reason = derive_phase_degradation_evidence({
        "phase4-liquidation": {
            "status": "NOT_AVAILABLE",
            "details": {"phase4_status": "PARTIAL", "data_quality": "PARTIAL"},
        },
    })

    assert verified["phase4"] is True
    assert missing_reason["phase4"] is False


def test_optional_phase6_sources_require_explicit_disabled_configuration():
    health = {
        f"phase6-{kind}-ingestion": {
            "details": {
                "configured": False,
                "source_count": 0,
                "reason_code": "SOURCE_NOT_CONFIGURED",
            },
        }
        for kind in ("news", "macro", "unlock")
    }
    health["phase6-ai-worker"] = {"details": {"provider_status": "NOT_CONFIGURED"}}
    health["phase6-ai-runtime"] = {"details": {"reason_code": "NOT_CONFIGURED"}}
    health["phase6-ai-budget"] = {"details": {}}

    assert derive_optional_source_configuration(health) == {"phase6": False, "phase6_ai": False, "phase8": None}
    incomplete = {key: value for key, value in health.items() if key != "phase6-unlock-ingestion"}
    assert derive_optional_source_configuration(incomplete)["phase6"] is None
    health["phase6-news-ingestion"]["details"]["configured"] = True
    assert derive_optional_source_configuration(health)["phase6"] is True


def test_unconfigured_phase6_provider_is_not_available_only_when_configured():
    unconfigured = derive_phase_statuses(
        health={
            "phase6-ai-worker": {
                "status": "NOT_AVAILABLE",
                "age_seconds": 10,
                "details": {"provider_status": "NOT_CONFIGURED"},
            },
            "phase6-ai-runtime": {
                "status": "AVAILABLE",
                "age_seconds": 10,
                "details": {"reason_code": "NOT_CONFIGURED"},
            },
        },
        phase_activity={},
        table_stats={},
    )
    configured_but_unavailable = derive_phase_statuses(
        health={
            "phase6-ai-worker": {
                "status": "NOT_AVAILABLE",
                "age_seconds": 10,
                "details": {"provider_status": "CONFIGURED"},
            },
        },
        phase_activity={},
        table_stats={},
    )

    assert unconfigured["phase6_ai"] == "NOT_CONFIGURED"
    assert configured_but_unavailable["phase6_ai"] == "NOT_AVAILABLE"


def test_phase6_ai_without_health_evidence_fails_closed_as_not_exposed():
    statuses = derive_phase_statuses(health={}, phase_activity={}, table_stats={})

    assert statuses["phase6_ai"] == "NOT_EXPOSED"


def test_safe_health_details_keep_diagnostic_enums_and_timestamps_only():
    import scripts.data_layer_acceptance as acceptance

    details = acceptance.safe_health_details({
        "reason": "REST_ERROR:AdapterSchemaError",
        "failure_stage": "TIP_RPC",
        "phase5_status": "PARTIAL",
        "data_quality": "PARTIAL",
                "gap_count": 2,
        "gap_watermark_received_at": "2026-09-26T15:20:18.757767+00:00",
        "consecutive_failures": 1,
        "error_category": "NETWORK",
        "last_fetched_at": "2026-09-26T15:20:18.757767+00:00",
        "cursor": 12345,
        "rpc_url": "https://provider.invalid/credential-path",
        "payload": "must-not-be-copied",
        "worker_health": {
            "chain": {
                "status": "DEGRADED",
                "interval_seconds": 300,
                "last_success_at": "2026-09-27T00:00:00+00:00",
                "credential": "must-not-be-copied",
            },
            "unknown-worker": {"status": "AVAILABLE"},
        },
    })

    assert details == {
        "reason": "REST_ERROR:AdapterSchemaError",
        "failure_stage": "TIP_RPC",
        "phase5_status": "PARTIAL",
        "data_quality": "PARTIAL",
        "gap_count": 2,
        "gap_watermark_received_at": "2026-09-26T15:20:18.757767+00:00",
        "consecutive_failures": 1,
        "error_category": "NETWORK",
        "last_fetched_at": "2026-09-26T15:20:18.757767+00:00",
        "cursor": 12345,
        "worker_health": {
            "chain": {
                "status": "DEGRADED",
                "interval_seconds": 300,
                "last_success_at": "2026-09-27T00:00:00+00:00",
            },
        },
    }
    assert "provider.invalid" not in repr(details)
    assert "must-not-be-copied" not in repr(details)


def test_container_log_sample_uses_bounded_docker_cli_and_scans_secrets(monkeypatch):
    import scripts.data_layer_acceptance as acceptance

    calls = []

    def fake_docker_output(args, **kwargs):
        calls.append((args, kwargs))
        return "recent log line\napi-key: sentinel-secret"

    monkeypatch.setattr(acceptance, "_docker_output", fake_docker_output)

    result = acceptance._container_log_sample("container-id", ("sentinel-secret",))

    assert calls == [(
        ["logs", "--tail", str(acceptance.MAX_LOG_TAIL_LINES), "container-id"],
        {
            "timeout_seconds": acceptance.LOG_COMMAND_TIMEOUT_SECONDS,
            "max_output_bytes": acceptance.MAX_LOG_SAMPLE_BYTES,
            "merge_stderr": True,
        },
    )]
    assert result == {
        "status": "AVAILABLE",
        "bytes": len("recent log line\napi-key: sentinel-secret"),
        "sampled_tail_bytes": len("recent log line\napi-key: sentinel-secret"),
        "secret_leak_found": True,
    }


def test_acceptance_gates_require_phase1_live_data_and_all_enabled_phases():
    gates = build_acceptance_gate_results(
        window_status="COMPLETED",
        missed_intervals=0,
        migrations_match=True,
        database_identity_utc=True,
        services_stable=True,
        core_health_fresh=True,
        phase1_market_activity=True,
        phase1_stage1_activity=True,
        resource_measurements_complete=True,
        memory_caps_respected=True,
        cursor_observability_complete=True,
        phase_statuses={
            "phase2": "AVAILABLE", "phase3": "AVAILABLE", "phase4": "AVAILABLE",
            "phase5": "AVAILABLE", "phase6": "AVAILABLE", "phase7": "AVAILABLE",
            "phase8": "PARTIAL", "phase6_ai": "NOT_CONFIGURED",
        },
        phase_degradation_evidence={"phase8": True},
        optional_source_configuration={"phase6_ai": False},
        minimum_disk_free_ratio=0.5,
        hard_stop_reason=None,
        logs_audited=True,
        secret_leak_found=False,
        clean_shutdown=True,
    )

    assert acceptance_gates_pass(gates)
    assert next(row for row in gates if row["gate"] == "phase6_ai_provider")["status"] == "PASS"
    assert next(row for row in gates if row["gate"] == "phase8")["status"] == "PASS_WITH_DEGRADATION"

    gates = build_acceptance_gate_results(
        window_status="COMPLETED",
        missed_intervals=0,
        migrations_match=True,
        database_identity_utc=True,
        services_stable=True,
        core_health_fresh=True,
        phase1_market_activity=True,
        phase1_stage1_activity=True,
        resource_measurements_complete=True,
        memory_caps_respected=True,
        cursor_observability_complete=True,
        phase_statuses={
            "phase2": "AVAILABLE", "phase3": "AVAILABLE", "phase4": "AVAILABLE",
            "phase5": "AVAILABLE", "phase6": "NOT_CONFIGURED", "phase7": "AVAILABLE",
            "phase8": "AVAILABLE", "phase6_ai": "NOT_CONFIGURED",
        },
        optional_source_configuration={"phase6": False, "phase6_ai": False},
        minimum_disk_free_ratio=0.5,
        hard_stop_reason=None,
        logs_audited=True,
        secret_leak_found=False,
        clean_shutdown=True,
    )
    assert acceptance_gates_pass(gates)
    assert next(row for row in gates if row["gate"] == "phase6")["status"] == "PASS"
    assert next(row for row in gates if row["gate"] == "phase6_ai_provider")["status"] == "PASS"

    gates = build_acceptance_gate_results(
        window_status="COMPLETED",
        missed_intervals=0,
        migrations_match=True,
        database_identity_utc=True,
        services_stable=True,
        core_health_fresh=True,
        phase1_market_activity=True,
        phase1_stage1_activity=True,
        resource_measurements_complete=True,
        memory_caps_respected=True,
        cursor_observability_complete=True,
        phase_statuses={
            "phase2": "AVAILABLE", "phase3": "AVAILABLE", "phase4": "AVAILABLE",
            "phase5": "AVAILABLE", "phase6": "NOT_CONFIGURED", "phase7": "AVAILABLE",
            "phase8": "PARTIAL", "phase6_ai": "AVAILABLE",
        },
        optional_source_configuration={"phase6": True},
        phase_degradation_evidence={"phase8": False},
        minimum_disk_free_ratio=0.5,
        hard_stop_reason=None,
        logs_audited=True,
        secret_leak_found=False,
        clean_shutdown=True,
    )
    assert not acceptance_gates_pass(gates)
    assert next(row for row in gates if row["gate"] == "phase6")["status"] == "FAIL"
    assert next(row for row in gates if row["gate"] == "phase8")["status"] == "FAIL"

    gates = build_acceptance_gate_results(
        window_status="COMPLETED",
        missed_intervals=0,
        migrations_match=True,
        database_identity_utc=True,
        services_stable=True,
        core_health_fresh=True,
        phase1_market_activity=False,
        phase1_stage1_activity=True,
        resource_measurements_complete=True,
        memory_caps_respected=True,
        cursor_observability_complete=True,
        phase_statuses={"phase2": "NOT_EXPOSED"},
        minimum_disk_free_ratio=0.5,
        hard_stop_reason=None,
        logs_audited=True,
        secret_leak_found=False,
        clean_shutdown=True,
    )
    assert not acceptance_gates_pass(gates)
    assert next(row for row in gates if row["gate"] == "phase2")["status"] == "NOT_EXPOSED"


def test_source_env_loader_whitelists_public_inputs_and_never_overrides_paper_or_isolated_db(tmp_path):
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "TRADING_MODE=live\n"
        "POSTGRES_DSN=postgresql://prod.invalid/quant\n"
        "BITGET_REST_BASE_URL=https://api.bitget.com\n"
        "PHASE7_BITCOIN_RPC_ENABLED=1\n"
        "PHASE7_BITCOIN_RPC_URL=https://btc.rpc.example/rpc/secret-path\n"
        "PHASE7_BITCOIN_RPC_AUTH_MODE=api_key_header\n"
        "PHASE7_BITCOIN_RPC_API_KEY=sentinel-rpc-key-never-log\n"
        "EXCHANGE_PRIVATE_API_KEY=sentinel-private-key-never-pass\n",
        encoding="utf-8",
    )

    config = load_public_source_config(env_file)

    assert config["BITGET_REST_BASE_URL"] == "https://api.bitget.com"
    assert config["PHASE7_BITCOIN_RPC_API_KEY"] == "sentinel-rpc-key-never-log"
    assert "TRADING_MODE" not in config
    assert "POSTGRES_DSN" not in config
    assert "EXCHANGE_PRIVATE_API_KEY" not in config
    assert "sentinel-private-key-never-pass" not in repr(config)


def test_env_loader_handles_quoted_values_without_interpreting_shell_syntax(tmp_path):
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        'BITGET_REST_BASE_URL="https://api.bitget.com"\n'
        'PHASE7_BITCOIN_RPC_API_KEY="value with spaces # retained"\n'
        'EXCHANGE_PRIVATE_API_KEY="ignored"\n',
        encoding="utf-8",
    )

    config = load_public_source_config(env_file)

    assert config["BITGET_REST_BASE_URL"] == "https://api.bitget.com"
    assert config["PHASE7_BITCOIN_RPC_API_KEY"] == "value with spaces # retained"
    assert "EXCHANGE_PRIVATE_API_KEY" not in config


def test_rpc_endpoint_validation_rejects_non_global_resolution_without_echoing_url():
    endpoint = "https://rpc.example.invalid/key-path-that-is-secret"

    def private_resolver(*_args, **_kwargs):
        return [(None, None, None, None, ("10.2.3.4", 443))]

    with pytest.raises(ValueError) as error:
        validate_public_rpc_endpoint(endpoint, resolver=private_resolver)

    assert str(error.value) == "RPC_ENDPOINT_NOT_PUBLIC"
    assert "key-path-that-is-secret" not in str(error.value)


def test_acceptance_compose_has_only_bounded_paper_services_and_no_host_ports():
    env = {
        "DATA_LAYER_ACCEPTANCE_IMAGE": "quant-data-layer-acceptance:test",
        "PHASE7_BITCOIN_RPC_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_URL": "https://btc.rpc.example/key-path",
        "PHASE7_BITCOIN_RPC_AUTH_MODE": "api_key_header",
        "PHASE7_BITCOIN_RPC_API_KEY": "sentinel-compose-key-never-print",
        "PHASE7_ETHEREUM_RPC_ENABLED": "0",
    }
    project = acceptance_project_name(uuid.UUID("12345678-1234-5678-1234-567812345678"))

    model = render_compose_model(project, env=env)
    validate_compose_model(model)

    assert set(model["services"]) == {"postgres", "quant-collector", "quant-engine"}
    assert model["services"]["postgres"]["mem_limit"] == "768m"
    assert model["services"]["quant-collector"]["mem_limit"] == "256m"
    assert model["services"]["quant-engine"]["mem_limit"] == "384m"
    assert "ports" not in model["services"]["postgres"]
    assert "ports" not in model["services"]["quant-collector"]
    assert "ports" not in model["services"]["quant-engine"]
    assert model["services"]["quant-collector"]["environment"]["TRADING_MODE"] == "paper"
    assert model["services"]["quant-engine"]["environment"]["TRADING_MODE"] == "paper"
    assert "sentinel-compose-key-never-print" in json.dumps(model)
    assert project in model["volumes"]["postgres_data"]["name"]


def test_compose_validation_rejects_proxy_environment():
    env = {
        "DATA_LAYER_ACCEPTANCE_IMAGE": "quant-data-layer-acceptance:test",
        "PHASE7_BITCOIN_RPC_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_URL": "https://btc.rpc.example/key-path",
        "PHASE7_BITCOIN_RPC_AUTH_MODE": "api_key_header",
        "PHASE7_BITCOIN_RPC_API_KEY": "sentinel-compose-key-never-print",
        "PHASE7_ETHEREUM_RPC_ENABLED": "0",
    }
    project = acceptance_project_name(uuid.UUID("12345678-1234-5678-1234-567812345678"))
    model = render_compose_model(project, env=env)
    model["services"]["quant-collector"]["environment"]["HTTPS_PROXY"] = "http://proxy.invalid"

    with pytest.raises(ValueError, match="PROXY_ENV_FORBIDDEN"):
        validate_compose_model(model)


def test_docker_proxy_preflight_rejects_host_proxy_environment_without_echo(monkeypatch, tmp_path):
    import scripts.data_layer_acceptance as acceptance

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "ftp_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)


    proxy_value = "http://host-proxy-secret.invalid"
    monkeypatch.setenv("HTTPS_PROXY", proxy_value)
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    monkeypatch.setattr(
        acceptance,
        "_docker_output",
        lambda *_args, **_kwargs: "<no value>|<no value>",
    )

    with pytest.raises(acceptance.AcceptanceError, match="PROXY_CONFIGURATION_PRESENT") as error:
        acceptance._docker_proxy_preflight()
    assert proxy_value not in str(error.value)


@pytest.mark.parametrize("override", ("DOCKER_HOST", "DOCKER_CONTEXT"))
def test_docker_preflight_rejects_remote_daemon_override_before_docker_info(
    monkeypatch, tmp_path, override
):
    import scripts.data_layer_acceptance as acceptance

    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "ftp_proxy", "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    if override == "DOCKER_HOST":
        monkeypatch.setenv("DOCKER_HOST", "ssh://docker-host.invalid")
    else:
        monkeypatch.setenv("DOCKER_CONTEXT", "remote-context")
    calls = []

    def docker_output(args, **_kwargs):
        calls.append(args)
        if args[:2] == ["context", "inspect"]:
            return "ssh://docker-context.invalid"
        return "<no value>|<no value>"

    monkeypatch.setattr(acceptance, "_docker_output", docker_output)
    with pytest.raises(acceptance.AcceptanceError, match="REMOTE_DOCKER_DAEMON_FORBIDDEN"):
        acceptance._docker_proxy_preflight()
    if override == "DOCKER_HOST":
        assert calls == []
@pytest.mark.parametrize("proxy_source", ("client", "daemon"))
def test_docker_proxy_preflight_rejects_docker_proxy_configuration_without_echo(
    monkeypatch, tmp_path, proxy_source
):
    import scripts.data_layer_acceptance as acceptance

    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "ftp_proxy", "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    proxy_value = "http://docker-proxy-secret.invalid"
    daemon_status = "<no value>|<no value>"
    if proxy_source == "client":
        client_config = {"proxies": {"default": {"httpsProxy": proxy_value}}}
        (tmp_path / "config.json").write_text(json.dumps(client_config), encoding="utf-8")
    else:
        daemon_status = f"{proxy_value}|<no value>"
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    def docker_output(args, **_kwargs):
        if args[:2] == ["context", "inspect"]:
            return "unix:///var/run/docker.sock"
        return daemon_status
    monkeypatch.setattr(acceptance, "_docker_output", docker_output)

    with pytest.raises(acceptance.AcceptanceError, match="PROXY_CONFIGURATION_PRESENT") as error:
        acceptance._docker_proxy_preflight()
    assert proxy_value not in str(error.value)

def test_final_report_records_proxy_preflight_without_proxy_details():
    from scripts.data_layer_acceptance import _build_final_report

    report = _build_final_report(
        run_id="run-1",
        project="quant-dlv1-accept-run-1",
        image="quant-data-layer-acceptance:run-1",
        configured_keys=("BITGET_REST_BASE_URL",),
        telemetry=None,
        sampling_result={"status": "INTERRUPTED", "sample_count": 0, "missed_intervals": 0, "elapsed_seconds": 0.0, "stop_reason": "INTERRUPT_REQUESTED"},
        shutdown={"clean_shutdown": False},
        startup_error=None,
        build_seconds=None,
        startup_seconds=None,
        proxy_preflight_status="PASS",
    )
    assert report["network"] == {
        "egress_scope": "PUBLIC_ONLY",
        "proxy_used": False,
        "proxy_preflight": "PASS",
    }

def test_main_rejects_remote_docker_before_any_daemon_operation(monkeypatch, tmp_path):
    import scripts.data_layer_acceptance as acceptance

    for name in (
        "HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY",
        "http_proxy", "https_proxy", "ftp_proxy", "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DOCKER_HOST", "ssh://docker-host.invalid")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setenv("DOCKER_CONFIG", str(tmp_path))
    monkeypatch.setattr(acceptance, "ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(acceptance, "_git_secret_file_preflight", lambda: None)
    monkeypatch.setattr(acceptance, "load_public_source_config", lambda: {})
    monkeypatch.setattr(acceptance, "_preflight_public_config", lambda _config: None)
    monkeypatch.setattr(acceptance, "render_compose_model", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(acceptance, "validate_compose_model", lambda _model: None)
    collision_calls = []
    monkeypatch.setattr(
        acceptance,
        "_assert_no_project_collision",
        lambda project: collision_calls.append(project),
    )
    docker_calls = []

    def docker_output(args, **_kwargs):
        docker_calls.append(args)
        return "local"

    monkeypatch.setattr(acceptance, "_docker_output", docker_output)
    assert acceptance.main() == 1
    assert collision_calls == []
    assert docker_calls == []
    report_files = list(tmp_path.glob("*/report.json"))
    assert len(report_files) == 1
    report = json.loads(report_files[0].read_text(encoding="utf-8"))
    assert report["network"]["proxy_preflight"] == "FAIL"
    assert report["network"]["proxy_used"] is False


def test_compose_validation_rejects_wrong_caps_private_keys_or_unbounded_logs():
    model = {
        "services": {
            "postgres": {
                "mem_limit": "768m",
                "memswap_limit": "768m",
                "restart": "no",
                "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
                "environment": {"POSTGRES_DB": "quant"},
                "volumes": ["project_data:/var/lib/postgresql/data"],
                "labels": {"quant.owner": "data-layer-v1-acceptance", "quant.project": "fixture", "quant.run_id": "test"},
            },
            "quant-collector": {
                "mem_limit": "256m",
                "memswap_limit": "256m",
                "restart": "no",
                "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
                "environment": {
                    "TRADING_MODE": "paper",
                    "POSTGRES_DSN": "postgresql://quant@postgres:5432/quant",
                    "EXCHANGE_PRIVATE_API_KEY": "must-not-exist",
                },
                "labels": {"quant.owner": "data-layer-v1-acceptance", "quant.project": "fixture", "quant.run_id": "test"},
            },
            "quant-engine": {
                "mem_limit": "384m",
                "memswap_limit": "384m",
                "restart": "no",
                "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}},
                "environment": {
                    "TRADING_MODE": "paper",
                    "POSTGRES_DSN": "postgresql://quant@postgres:5432/quant",
                },
                "labels": {"quant.owner": "data-layer-v1-acceptance", "quant.project": "fixture", "quant.run_id": "test"},
            },
        },
        "volumes": {"postgres_data": {"name": "project_data", "labels": {"quant.owner": "data-layer-v1-acceptance", "quant.project": "fixture", "quant.run_id": "test"}}},
        "networks": {"default": {"labels": {"quant.owner": "data-layer-v1-acceptance", "quant.project": "fixture", "quant.run_id": "test"}}},
    }

    with pytest.raises(ValueError, match="PRIVATE_ENV_NOT_ALLOWED"):
        validate_compose_model(model)

    model["services"]["quant-collector"]["environment"].pop("EXCHANGE_PRIVATE_API_KEY")
    model["services"]["quant-collector"]["logging"]["options"].pop("max-file")
    with pytest.raises(ValueError, match="LOG_ROTATION_REQUIRED"):
        validate_compose_model(model)


def test_failure_matrix_preserves_soft_source_failures_and_unexposed_metrics():
    rows = build_failure_matrix(
        source_statuses={
            "bitget_public": "AVAILABLE",
            "phase7_bitcoin": "RATE_LIMITED",
            "phase6_ai": "NOT_CONFIGURED",
        },
        unexposed_metrics=("websocket_transport_pending",),
    )

    by_name = {row["source"]: row for row in rows}
    assert by_name["bitget_public"]["result"] == "PASS"
    assert by_name["phase7_bitcoin"]["result"] == "FAIL"
    assert by_name["phase7_bitcoin"]["observed_status"] == "RATE_LIMITED"
    assert by_name["phase6_ai"]["result"] == "NOT_CONFIGURED"
    assert by_name["websocket_transport_pending"]["result"] == "NOT_EXPOSED"
    assert all("payload" not in row for row in rows)


def test_bounded_writer_rejects_oversize_records_and_secret_values(tmp_path):
    artifact = tmp_path / "samples.jsonl"
    writer = BoundedJsonlWriter(
        artifact,
        max_record_bytes=128,
        max_artifact_bytes=256,
        secret_values=("secret-marker-never-write",),
    )

    writer.write({"status": "AVAILABLE", "sample": 1})
    assert artifact.stat().st_size < 256

    with pytest.raises(ValueError, match="ARTIFACT_RECORD_TOO_LARGE"):
        writer.write({"diagnostic": "x" * 200})
    with pytest.raises(ValueError, match="SECRET_LEAK_BLOCKED"):
        writer.write({"diagnostic": "secret-marker-never-write"})
    assert "secret-marker-never-write" not in artifact.read_text(encoding="utf-8")


def test_runtime_artifact_has_hard_total_size_limit(tmp_path):
    artifact = tmp_path / "samples.jsonl"
    writer = BoundedJsonlWriter(
        artifact,
        max_record_bytes=100,
        max_artifact_bytes=25,
    )

    with pytest.raises(ValueError, match="ARTIFACT_TOTAL_SIZE_LIMIT"):
        writer.write({"status": "AVAILABLE", "sequence": 123456789})
    assert not artifact.exists() or artifact.stat().st_size == 0


def test_generated_compose_project_name_is_unique_and_does_not_use_fixed_names():
    first = acceptance_project_name(uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
    second = acceptance_project_name(uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"))

    assert first == "quant-dlv1-accept-aaaaaaaaaaaa"
    assert second == "quant-dlv1-accept-bbbbbbbbbbbb"
    assert first != second


def test_project_cleanup_refuses_resources_not_owned_by_generated_project():
    from scripts.data_layer_acceptance import validate_owned_project_resources

    project = "quant-dlv1-accept-aaaaaaaaaaaa"
    resources = [
        {"name": f"{project}-postgres-1", "project": project, "owner": "data-layer-v1-acceptance"},
        {"name": f"{project}_postgres_data", "project": project, "owner": "data-layer-v1-acceptance"},
    ]

    assert validate_owned_project_resources(project, resources) is True
    with pytest.raises(ValueError, match="CLEANUP_OWNERSHIP_MISMATCH"):
        validate_owned_project_resources(
            project,
            resources + [{"name": "quant-postgres", "project": "user-project", "owner": ""}],
        )
