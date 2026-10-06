#!/usr/bin/env python3
"""Collect bounded, secret-safe local Phase 7 runtime resource/health samples."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
from typing import Any

import psycopg


SERVICES = ("postgres", "quant-collector", "quant-engine")
APPLICATION_METRIC_KEYS = frozenset({
    "queue_depth", "queue_capacity", "backfill_depth", "backlog_depth",
    "kline_recovery_active", "kline_recovery_pending", "kline_recovery_inflight",
    "kline_recovery_completed", "kline_recovery_failed", "kline_recovery_scanned",
    "kline_recovery_gaps", "kline_recovery_admitted", "kline_recovery_coalesced",
    "kline_recovery_skipped", "kline_reconciliation_runs", "head_cursor", "cursor",
    "lag_cursor", "max_backfill", "max_catch_up", "pending_count", "inflight_count",
    "active_count", "windows_pending", "pending_trades", "accepted_trades",
    "duplicate_trades", "agg_trade_messages", "subscription_acks", "processed_blocks",
    "persisted_events", "replay_ws_trades", "symbols", "rest_responses",
    "collector_asyncio_task_count", "collector_ws_task_count", "collector_ws_connection_count",
    "collector_event_buffer_depth", "collector_event_buffer_capacity", "collector_event_buffer_dropped",
    "collector_ticker_store_count", "collector_closed_kline_store_count",
    "collector_ws_application_queue_depth",
    "phase1_recovery_queue_depth",
    "phase1_recovery_queue_capacity", "phase1_recovery_pending", "phase1_recovery_inflight",
    "phase1_recovery_rerun", "phase1_recovery_retry", "phase1_recovery_failed",
    "phase1_recovery_active", "phase1_recovery_task_count", "phase1_db_writer_pending_batches",
    "phase1_db_writer_active_transactions", "phase3_trade_queue_count", "phase3_trade_queue_depth",
    "phase3_trade_queue_capacity", "phase3_trade_aggregation_windows_pending",
    "phase4_pending_events", "phase4_task_count", "phase6_collector_task_count",
    "phase7_task_count", "phase7_active_source_cycles", "phase7_bitcoin_pending_blocks",
    "phase7_bitcoin_backfill_depth", "phase7_bitcoin_active_rpc", "phase7_bitcoin_rpc_requests_cycle",
    "phase7_bitcoin_last_response_bytes", "phase7_bitcoin_response_bytes_cycle",
    "phase7_bitcoin_active_block_processing",
    "phase7_bitcoin_active_block_height", "phase7_bitcoin_block_event_count",
    "phase7_bitcoin_pending_persistence_chunks", "phase7_ethereum_backfill_depth",
    "phase7_ethereum_pending_blocks", "phase7_ethereum_active_rpc", "phase7_ethereum_rpc_requests_cycle",
    "phase7_ethereum_last_response_bytes", "phase7_ethereum_response_bytes_cycle",
    "phase7_ethereum_active_block_processing",
    "phase7_ethereum_active_block_height", "phase7_ethereum_pending_logs",
    "phase7_ethereum_pending_receipts", "phase7_ethereum_block_event_count",
    "phase7_spot_active_rest_requests", "phase7_spot_response_bytes", "phase7_spot_ws_connected",
    "phase7_spot_ws_application_queue_depth", "phase7_spot_pending_trades",
    "phase7_spot_pending_trade_capacity", "phase7_spot_pending_windows",
    "phase7_spot_aggregation_active",
})
APPLICATION_STATE_KEYS = frozenset({
    "runtime_state", "runtime_stage", "failure_stage", "source_status", "phase7_status",
    "lifecycle", "owner", "phase1_db_writer_mode", "phase7_bitcoin_stage",
    "phase7_ethereum_stage", "phase7_spot_stage", "phase7_spot_ws_buffer_mode",
    "phase7_bitcoin_last_rpc_method", "phase7_ethereum_last_rpc_method",
    "phase7_spot_ws_internal_queue_visibility", "collector_ws_transport_pending_visibility",
})
CGROUP_READ = (
    "printf 'memory_current %s\\n' \"$(cat /sys/fs/cgroup/memory.current)\"; "
    "printf 'memory_peak %s\\n' \"$(cat /sys/fs/cgroup/memory.peak)\"; "
    "printf 'memory_limit %s\\n' \"$(cat /sys/fs/cgroup/memory.max)\"; "
    "grep -E '^(anon|file|kernel|sock|shmem|slab|pagetables|inactive_file|active_file) ' "
    "/sys/fs/cgroup/memory.stat | sed 's/^/stat_/'; "
    "sed 's/^/cpu_/' /sys/fs/cgroup/cpu.stat; "
    "sed 's/^/event_/' /sys/fs/cgroup/memory.events; "
    "printf 'pids_current %s\\n' \"$(cat /sys/fs/cgroup/pids.current)\"; "
    "grep -E '^(VmRSS|VmSize|Threads):' /proc/1/status | sed 's/^/proc_/'; "
    "grep -E '^(Rss|Pss):' /proc/1/smaps_rollup | sed 's/^/smaps_/'"
)


def _run(args: list[str]) -> str:
    result = subprocess.run(args, check=False, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("a bounded acceptance sample could not be collected")
    return result.stdout.strip()


def _container(project: str, service: str) -> str:
    return f"{project}_{service}_1"


def _resource_sample(name: str) -> dict[str, int | None]:
    lines = _run(["docker", "exec", name, "sh", "-c", CGROUP_READ]).splitlines()
    values: dict[str, int] = {}
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            values[parts[0].rstrip(":")] = int(parts[1])
    required = {
        "memory_current", "memory_peak", "memory_limit", "stat_anon", "stat_file",
        "stat_kernel", "stat_sock", "stat_shmem", "stat_slab", "stat_pagetables",
        "stat_inactive_file",
        "event_max", "event_oom", "event_oom_kill", "pids_current",
        "proc_VmRSS", "proc_VmSize", "proc_Threads",
    }
    if not required.issubset(values):
        missing = ",".join(sorted(required - values.keys()))
        raise RuntimeError(f"container cgroup v2 metrics are incomplete: {missing}")
    result = {
        "memory_current": values["memory_current"],
        "memory_peak": values["memory_peak"],
        "memory_limit": values["memory_limit"],
        "events_max": values["event_max"],
        "events_oom": values["event_oom"],
        "events_oom_kill": values["event_oom_kill"],
        "tasks": values["pids_current"],
        "process_rss": values["proc_VmRSS"] * 1024,
        "process_vmsize": values["proc_VmSize"] * 1024,
        "process_threads": values["proc_Threads"],
        "process_pss": values["smaps_Pss"] * 1024 if "smaps_Pss" in values else None,
        "process_smaps_rss": values["smaps_Rss"] * 1024 if "smaps_Rss" in values else None,
        "cpu_usage_usec": values.get("cpu_usage_usec"),
        "cpu_nr_throttled": values.get("cpu_nr_throttled"),
        "cpu_throttled_usec": values.get("cpu_throttled_usec"),
    }
    result.update({key[5:]: value for key, value in values.items() if key.startswith("stat_")})
    return result


def _database_ip(project: str) -> str:
    return _run([
        "docker", "inspect", "--format",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
        _container(project, "postgres"),
    ])


def _database_sample(project: str) -> dict[str, Any]:
    ip = _database_ip(project)
    with psycopg.connect(host=ip, user="quant", dbname="quant", connect_timeout=4) as connection:
        health_rows = connection.execute(
            "SELECT component, status, details, checked_at FROM system_health ORDER BY component"
        ).fetchall()
        checkpoints = connection.execute(
            "SELECT source_id, scope_key, cursor_kind, cursor_value, status "
            "FROM phase7_ingestion_checkpoints ORDER BY source_id, scope_key"
        ).fetchall()
        queues = connection.execute(
            """
            SELECT component,
                   details->>'queue_depth',
                   details->>'backfill_depth',
                   details->>'backlog_depth'
            FROM system_health ORDER BY component
            """
        ).fetchall()
        active_transactions, idle_transactions = connection.execute(
            """
            SELECT count(*) FILTER (WHERE state = 'active'),
                   count(*) FILTER (WHERE state LIKE 'idle in transaction%')
            FROM pg_stat_activity
            WHERE datname = current_database() AND pid <> pg_backend_pid()
            """
        ).fetchone()
    application_metrics = {}
    for component, _status, details, _checked_at in health_rows:
        if not isinstance(details, dict):
            continue
        safe_details = {
            key: value for key, value in details.items()
            if key in APPLICATION_METRIC_KEYS | APPLICATION_STATE_KEYS
            and (value is None or isinstance(value, (str, int, bool, float)))
        }
        if safe_details:
            application_metrics[str(component)] = safe_details
    return {
        "health": {str(component): str(status) for component, status, _details, _checked in health_rows},
        "health_checked_at_utc": {
            str(component): checked_at.astimezone(timezone.utc).isoformat()
            for component, _status, _details, checked_at in health_rows
        },
        "health_age_seconds": {
            str(component): round(max(0.0, (datetime.now(timezone.utc) - checked_at.astimezone(timezone.utc)).total_seconds()), 3)
            for component, _status, _details, checked_at in health_rows
        },
        "application_metrics": application_metrics,
        "checkpoints": {
            f"{source}:{scope}": {"kind": kind, "cursor": cursor, "status": status}
            for source, scope, kind, cursor, status in checkpoints
        },
        "queue_fields": {
            str(component): {"queue_depth": queue, "backfill_depth": backfill, "backlog_depth": backlog}
            for component, queue, backfill, backlog in queues
            if queue is not None or backfill is not None or backlog is not None
        },
        "active_transactions": int(active_transactions or 0),
        "idle_in_transaction": int(idle_transactions or 0),
    }


def _collector_diagnostics_sample(project: str) -> dict[str, Any]:
    raw = _run([
        "docker", "exec", _container(project, "quant-collector"),
        "cat", "/tmp/phase7-collector-diagnostics.json",
    ])
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("non-object diagnostics")
        sampled_at = datetime.fromisoformat(payload["sampled_at_utc"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise RuntimeError("collector diagnostics snapshot is invalid") from None
    safe = {
        key: value for key, value in payload.items()
        if key in APPLICATION_METRIC_KEYS | APPLICATION_STATE_KEYS
        and (value is None or isinstance(value, (str, int, bool, float)))
    }
    safe["snapshot_age_seconds"] = round(
        max(0.0, (datetime.now(timezone.utc) - sampled_at.astimezone(timezone.utc)).total_seconds()), 3
    )
    return safe


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--samples", type=int, required=True)
    parser.add_argument("--interval-seconds", type=int, default=30)
    parser.add_argument("--output", required=True)
    parser.add_argument("--stop-file")
    args = parser.parse_args()
    if not 1 <= args.samples <= 200 or not 1 <= args.interval_seconds <= 60:
        raise SystemExit("sample count/interval is outside the bounded acceptance range")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    summaries: dict[str, dict[str, int]] = {}
    first_db: dict[str, Any] | None = None
    last_db: dict[str, Any] | None = None
    started = time.monotonic()
    with output.open("w", encoding="utf-8") as stream:
        sampled = 0
        for index in range(args.samples):
            if index and args.stop_file and Path(args.stop_file).exists():
                break
            sample: dict[str, Any] = {
                "sampled_at_utc": datetime.now(timezone.utc).isoformat(),
                "resources": {},
            }
            for service in SERVICES:
                name = _container(args.project, service)
                metrics = _resource_sample(name)
                sample["resources"][service] = metrics
                service_summary = summaries.setdefault(service, {})
                for key, value in metrics.items():
                    if value is None:
                        continue
                    service_summary[f"max_{key}"] = max(service_summary.get(f"max_{key}", 0), value)
                    service_summary[f"min_{key}"] = min(service_summary.get(f"min_{key}", value), value)
            db_sample = _database_sample(args.project)
            collector_diagnostics = _collector_diagnostics_sample(args.project)
            sample["collector_diagnostics"] = collector_diagnostics
            sample["database"] = db_sample
            if first_db is None:
                first_db = db_sample
            last_db = db_sample
            stream.write(json.dumps(sample, sort_keys=True, separators=(",", ":")) + "\n")
            stream.flush()
            sampled += 1
            collector_metrics = sample["resources"]["quant-collector"]
            print(json.dumps({
                "sample": index + 1,
                "utc": sample["sampled_at_utc"],
                "collector_mem": collector_metrics["memory_current"],
                "collector_limit": collector_metrics["memory_limit"],
                "collector_mem_pct": round(
                    100 * collector_metrics["memory_current"] / collector_metrics["memory_limit"], 1
                ),
                "collector_anon": collector_metrics["anon"],
                "collector_file": collector_metrics["file"],
                "collector_events_max": collector_metrics["events_max"],
                "collector_events_oom": collector_metrics["events_oom"],
                "collector_events_oom_kill": collector_metrics["events_oom_kill"],
                "collector_process_rss": collector_metrics["process_rss"],
                "collector_process_pss": collector_metrics["process_pss"],
                "engine_mem": sample["resources"]["quant-engine"]["memory_current"],
                "postgres_mem": sample["resources"]["postgres"]["memory_current"],
                "health": db_sample["health"],
                "checkpoint_count": len(db_sample["checkpoints"]),
                "queue_fields": db_sample["queue_fields"],
                "application_metrics": db_sample["application_metrics"],
                "collector_diagnostics": collector_diagnostics,
                "database_active_transactions": db_sample["active_transactions"],
            }, sort_keys=True, separators=(",", ":")), flush=True)
            if index + 1 < args.samples:
                target = started + (index + 1) * args.interval_seconds
                time.sleep(max(0.0, target - time.monotonic()))
    print(json.dumps({
        "samples": sampled,
        "interval_seconds": args.interval_seconds,
        "duration_seconds": round(time.monotonic() - started, 3),
        "output_path": str(output),
        "resource_maxima": summaries,
        "health_first": first_db["health"] if first_db else {},
        "health_last": last_db["health"] if last_db else {},
        "checkpoints_first": first_db["checkpoints"] if first_db else {},
        "checkpoints_last": last_db["checkpoints"] if last_db else {},
    }, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
