"""Disposable Docker runner for deterministic Phase 6 Replay V2 resource arms."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import subprocess
import time
from typing import Any
import uuid


ROOT = Path(__file__).resolve().parents[1]
IMAGE = "quant-phase7:replay-v2-current"
IMAGE_INSPECT = ["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"]
POSTGRES_IMAGE = "postgres:16-alpine"
POSTGRES_IMAGE_INSPECT = ["docker", "image", "inspect", POSTGRES_IMAGE, "--format", "{{.Id}}"]
REQUIRED_MARKERS = (
    "collector-replay-ready.json",
    "engine-bootstrap.json",
    "engine-phase2-cycle.json",
    "engine-phase2-cycle-runtime.json",
    "phase2-input-consumed.json",
)
TABLE_COUNTS_SQL = """
SELECT COALESCE(json_object_agg(table_name, row_count), '{}'::json)
FROM (
  SELECT table_name,
         (xpath('/row/c/text()', query_to_xml(
            format('SELECT count(*) AS c FROM public.%I', table_name), false, true, ''
         )))[1]::text::bigint AS row_count
  FROM information_schema.tables
  WHERE table_schema='public' AND table_type='BASE TABLE'
) AS counts
"""
DB_STATS_SQL = """
SELECT json_build_object(
  'database', current_database(),
  'size_bytes', pg_database_size(current_database()),
  'connections', (SELECT count(*) FROM pg_stat_activity WHERE datname=current_database()),
  'database_stats', (SELECT to_jsonb(s) FROM pg_stat_database AS s WHERE datname=current_database()),
  'checkpoint_stats', (SELECT to_jsonb(c) FROM pg_stat_bgwriter AS c)
)::text
"""
PHASE6_HEALTH_SQL = """
SELECT COALESCE(json_object_agg(component, json_build_object(
  'status', status, 'details', details, 'checked_at', checked_at
)), '{}'::json)::text
FROM system_health WHERE component LIKE 'phase6-%'
"""
PHASE45_SEMANTICS_SQL = """
SELECT json_build_object(
  'phase4_stage1', json_build_object(
    'rows', (SELECT count(*) FROM stage1_phase4_enrichment),
    'liquidation_statuses', (SELECT COALESCE(json_object_agg(status, n), '{}'::json)
      FROM (SELECT liquidation_status AS status, count(*) AS n FROM stage1_phase4_enrichment GROUP BY 1) q),
    'long_short_statuses', (SELECT COALESCE(json_object_agg(status, n), '{}'::json)
      FROM (SELECT long_short_status AS status, count(*) AS n FROM stage1_phase4_enrichment GROUP BY 1) q),
    'basis_statuses', (SELECT COALESCE(json_object_agg(status, n), '{}'::json)
      FROM (SELECT basis_status AS status, count(*) AS n FROM stage1_phase4_enrichment GROUP BY 1) q),
    'liquidation_observations', (SELECT count(*) FROM liquidation_events),
    'long_short_observations', (SELECT count(*) FROM long_short_observations),
    'basis_observations', (SELECT count(*) FROM basis_snapshots)
  ),
  'phase5_context_health', (SELECT json_build_object('status', status, 'details', details)
    FROM system_health WHERE component='phase5-context')
)::text
"""


class DockerError(RuntimeError):
    pass


def _run(args: list[str], *, timeout: int = 60, check: bool = True) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise DockerError(
            f"command failed ({result.returncode}): {args[0]} {args[1] if len(args) > 1 else ''}\n"
            f"stdout={result.stdout[-2000:]}\nstderr={result.stderr[-2000:]}"
        )
    return result.stdout.strip()


def _json_marker(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _container_state(name: str) -> dict[str, Any]:
    raw = _run(["docker", "inspect", name, "--format", "{{json .State}}"])
    return json.loads(raw)


def _cgroup_memory(name: str) -> dict[str, Any]:
    script = (
        "for f in memory.current memory.peak memory.stat memory.events memory.pressure; do "
        "printf '%s\\n' \"===$f===\"; cat /sys/fs/cgroup/$f 2>/dev/null || true; done"
    )
    output = _run(["docker", "exec", name, "sh", "-c", script], check=False)
    section = ""
    result: dict[str, Any] = {}
    for line in output.splitlines():
        if line.startswith("===") and line.endswith("==="):
            section = line[3:-3]
        elif section:
            if section in {"memory.stat", "memory.events"}:
                key, _, value = line.partition(" ")
                if value:
                    result.setdefault(section, {})[key] = value
            elif section == "memory.pressure":
                result.setdefault(section, []).append(line)
            else:
                result[section] = line
    return result


def _docker_stats(names: list[str]) -> dict[str, Any]:
    if not names:
        return {}
    output = _run([
        "docker", "stats", "--no-stream", "--format",
        "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.BlockIO}}", *names,
    ], check=False)
    result = {}
    for line in output.splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            result[parts[0]] = {"cpu": parts[1], "memory": parts[2], "block_io": parts[3]}
    return result


def _psql(container: str, sql: str) -> str:
    return _run(["docker", "exec", container, "psql", "-U", "quant", "-d", "quant", "-t", "-A", "-c", sql])


def _db_stats(container: str) -> dict[str, Any]:
    return json.loads(_psql(container, DB_STATS_SQL))


def _row_counts(container: str) -> dict[str, int]:
    value = json.loads(_psql(container, TABLE_COUNTS_SQL))
    return {key: int(count) for key, count in value.items()}


def _phase6_health(container: str) -> dict[str, Any]:
    return json.loads(_psql(container, PHASE6_HEALTH_SQL))


def _phase45_semantics(container: str) -> dict[str, Any]:
    return json.loads(_psql(container, PHASE45_SEMANTICS_SQL))


def _sample(path: Path, arm: str, cycle: int, names: list[str], pg_name: str, started: float,
            *, include_db: bool = True) -> dict[str, Any]:
    sample: dict[str, Any] = {
        "utc": datetime.now(timezone.utc).isoformat(),
        "arm": arm,
        "cycle": cycle,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "docker_stats": _docker_stats(names),
        "cgroup_memory": {name: _cgroup_memory(name) for name in names},
    }
    if include_db:
        query_started = time.monotonic()
        sample["postgres_stats"] = _db_stats(pg_name)
        sample["postgres_stats_query_roundtrip_ms"] = round((time.monotonic() - query_started) * 1000, 3)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(sample, sort_keys=True, separators=(",", ":")) + "\n")
    return sample


def _wait_marker(path: Path, name: str, timeout: float, sample_callback) -> Any:
    deadline = time.monotonic() + timeout
    next_sample = 0.0
    while time.monotonic() < deadline:
        value = _json_marker(path / name)
        if value is not None:
            return value
        if time.monotonic() >= next_sample:
            sample_callback()
            next_sample = time.monotonic() + 1.0
        time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for {name}")


def _logs_to_file(container: str, path: Path) -> None:
    output = _run(["docker", "logs", container], check=False, timeout=30)
    path.write_text(output + "\n", encoding="utf-8")


def _stop_parallel(names: list[str], grace: int = 10) -> dict[str, str]:
    def stop(name: str) -> tuple[str, str]:
        output = _run(["docker", "stop", "--time", str(grace), name], timeout=grace + 20, check=False)
        return name, output
    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        return dict(pool.map(stop, names))


def _summarize_samples(path: Path) -> dict[str, Any]:
    samples = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    by_name: dict[str, list[int]] = {}
    stat_series: dict[str, dict[str, list[int]]] = {}
    pg_peaks: dict[str, dict[str, int]] = {}
    event_series: dict[str, dict[str, list[int]]] = {}
    cpu_series: dict[str, list[float]] = {}
    for sample in samples:
        for name, stats in sample.get("docker_stats", {}).items():
            try:
                cpu_series.setdefault(name, []).append(float(str(stats.get("cpu", "")).rstrip("%")))
            except ValueError:
                pass
        for name, cg in sample.get("cgroup_memory", {}).items():
            current = cg.get("memory.current")
            if current and current.isdigit():
                by_name.setdefault(name, []).append(int(current))
            peak = cg.get("memory.peak")
            if peak and peak.isdigit():
                pg_peaks.setdefault(name, {})["memory_peak_bytes"] = max(
                    int(peak), pg_peaks.get(name, {}).get("memory_peak_bytes", 0)
                )
            for key in ("anon", "file", "shmem"):
                raw = cg.get("memory.stat", {}).get(key)
                if raw and str(raw).isdigit():
                    stat_series.setdefault(name, {}).setdefault(key, []).append(int(raw))
            events = cg.get("memory.events", {})
            if events:
                parsed = {key: int(value) for key, value in events.items() if str(value).isdigit()}
                for key, value in parsed.items():
                    event_series.setdefault(name, {}).setdefault(key, []).append(value)
    db_samples = [sample["postgres_stats"] for sample in samples if "postgres_stats" in sample]
    db_deltas: dict[str, Any] = {}
    if len(db_samples) >= 2:
        for section in ("database_stats", "checkpoint_stats"):
            first = db_samples[0].get(section) or {}
            last = db_samples[-1].get(section) or {}
            db_deltas[section] = {
                key: last[key] - first.get(key, last[key])
                for key in last
                if isinstance(last[key], (int, float))
                and isinstance(first.get(key, last[key]), (int, float))
            }
    query_roundtrips = sorted(
        sample["postgres_stats_query_roundtrip_ms"] for sample in samples
        if "postgres_stats_query_roundtrip_ms" in sample
    )
    p95_query = query_roundtrips[min(len(query_roundtrips) - 1, int(len(query_roundtrips) * 0.95))] if query_roundtrips else None
    return {
        "sample_count": len(samples),
        "per_container_memory_current_bytes": {
            name: {"min": min(values), "max": max(values), "last": values[-1]}
            for name, values in by_name.items() if values
        },
        "per_container_cgroup_memory_peak_bytes": pg_peaks,
        "per_container_memory_stat_peak_bytes": {
            name: {key: max(values) for key, values in stats.items() if values}
            for name, stats in stat_series.items()
        },
        "per_container_memory_events_delta": {
            name: {key: values[-1] - values[0] for key, values in events.items() if values}
            for name, events in event_series.items()
        },
        "postgres_stats_query_roundtrip_ms": {
            "p50": round(statistics.median(query_roundtrips), 3) if query_roundtrips else None,
            "p95": round(p95_query, 3) if p95_query is not None else None,
            "note": "docker exec + psql end-to-end diagnostic round-trip, not server-only SQL latency",
        },
        "postgres_counter_deltas": db_deltas,
        "docker_cpu_percent_samples": {
            name: {"max": max(values), "mean": round(statistics.mean(values), 3)}
            for name, values in cpu_series.items() if values
        },
        "docker_stats_last": samples[-1].get("docker_stats", {}) if samples else {},
    }


def _health_gate(health: dict[str, Any]) -> dict[str, Any]:
    expected = {
        "phase6-news-ingestion": "NOT_AVAILABLE",
        "phase6-macro-ingestion": "NOT_AVAILABLE",
        "phase6-unlock-ingestion": "NOT_AVAILABLE",
        "phase6-ai-provider": "NOT_AVAILABLE",
    }
    observed = {name: health.get(name, {}).get("status") for name in expected}
    details = {name: health.get(name, {}).get("details", {}) for name in expected}
    provider_not_configured = details.get("phase6-ai-provider", {}).get("status") == "NOT_CONFIGURED"
    source_reasons = all(details.get(name, {}).get("reason_code") == "SOURCE_NOT_CONFIGURED"
                         for name in list(expected)[:3])
    persistence_available = health.get("phase6-persistence", {}).get("status") == "AVAILABLE"
    active_engine = health.get("phase6-ai-runtime", {}).get("details", {}).get("lifecycle") == "ACTIVE"
    return {
        "expected_statuses": expected,
        "observed_statuses": observed,
        "source_reasons_not_configured": source_reasons,
        "ai_provider_not_configured": provider_not_configured,
        "engine_runtime_active": active_engine,
        "persistence_available_observed": persistence_available,
        "pass": observed == expected and source_reasons and provider_not_configured
                and active_engine and persistence_available,
    }


def _compare(reference: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    left = reference["logical_evidence"]
    right = candidate["logical_evidence"]
    provenance_fields = ("git_sha", "image_id", "postgres_image_id", "dataset_sha256")
    provenance_differences = {
        field: {"reference": reference.get(field), "candidate": candidate.get(field)}
        for field in provenance_fields
        if reference.get(field) != candidate.get(field)
    }
    fields = (
        "dataset_sha256", "phase1_input_event_total", "phase1_event_type_counts",
        "phase2_fixture_event_count", "phase2_event_type_counts", "oi_observations",
        "funding_observations", "reset_count", "collector_logical_rows", "engine_bootstrap",
        "phase2_cycle", "db_row_counts",
        "phase45_semantics",
    )
    differences = {field: {"reference": left.get(field), "candidate": right.get(field)}
                   for field in fields if left.get(field) != right.get(field)}
    return {
        "reproducible": not differences and not provenance_differences,
        "compared_provenance_fields": list(provenance_fields),
        "provenance_differences": provenance_differences,
        "compared_fields": list(fields),
        "differences": differences,
    }


def _run_arm(arm: str, cycles: int, compare_to: Path | None) -> dict[str, Any]:
    results_root = Path(os.environ.get("REPLAY_V2_RESULTS_BASE", "/tmp/phase6-replay-v2"))
    results_dir = results_root / f"{arm}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    if results_dir.exists():
        raise RuntimeError(f"result directory already exists: {results_dir}")
    results_dir.mkdir(parents=True)
    os.chmod(results_dir, 0o777)
    docker_id = _run(IMAGE_INSPECT)
    postgres_image_id = _run(POSTGRES_IMAGE_INSPECT)
    git_sha = _run(["git", "-C", str(ROOT), "rev-parse", "HEAD"])
    worktree = _run(["git", "-C", str(ROOT), "status", "--porcelain"])
    non_report_changes = [
        line for line in worktree.splitlines()
        if line.split(maxsplit=1)[-1].strip(' "') != "PHASE_6_RUNTIME_INTEGRATION_REPORT.md"
    ]
    if non_report_changes:
        raise RuntimeError(f"uncommitted source/test changes invalidate replay baseline: {non_report_changes}")

    postgres_cap = "1g" if arm == "1g" else "768m"
    unique = uuid.uuid4().hex[:7]
    prefix = f"q6v2-{arm}-{unique}"
    network = prefix + "-net"
    pg_name = prefix + "-pg"
    containers: list[str] = []
    all_samples_path = results_dir / "runtime-samples.jsonl"
    started = time.monotonic()
    summaries: list[dict[str, Any]] = []
    baseline_size = 0
    try:
        _run(["docker", "network", "create", "--internal", network])
        _run([
            "docker", "run", "-d", "--name", pg_name, "--network", network,
            "--memory", postgres_cap, "--memory-swap", postgres_cap, "--cpus", "0.5",
            "-e", "POSTGRES_DB=quant", "-e", "POSTGRES_USER=quant", "-e", "POSTGRES_PASSWORD=quant",
            postgres_image_id,
        ])
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            state = _container_state(pg_name)
            if not state.get("Running", False):
                raise DockerError(f"disposable PostgreSQL exited: {state}")
            ready = _run([
                "docker", "exec", pg_name, "psql", "-U", "quant", "-d", "quant",
                "-t", "-A", "-c", "SELECT 1",
            ], check=False)
            if ready == "1":
                break
            time.sleep(1)
        else:
            raise TimeoutError("disposable PostgreSQL did not become ready")
        baseline_size = int(_psql(pg_name, "SELECT pg_database_size('quant')"))

        for cycle in range(1, cycles + 1):
            cycle_dir = results_dir / f"cycle-{cycle}"
            cycle_dir.mkdir()
            os.chmod(cycle_dir, 0o777)
            collector = f"{prefix}-c{cycle}"
            engine = f"{prefix}-e{cycle}"
            cycle_started = time.monotonic()
            dsn = f"postgresql://quant:quant@{pg_name}:5432/quant"
            volume = f"{str(ROOT / 'tests')}:/workspace/tests:ro"
            cycle_volume = f"{str(cycle_dir)}:/run-results"
            common = [
                "docker", "run", "-d", "--network", network,
                "-v", volume, "-v", cycle_volume,
                "-e", f"POSTGRES_DSN={dsn}", "-e", "REPLAY_RESULTS_DIR=/run-results",
                "-e", "TRADING_MODE=paper", "-e", "PHASE7_BITCOIN_RPC_ENABLED=0",
                "-e", "PHASE7_ETHEREUM_RPC_ENABLED=0",
            ]
            _run([
                *common[:3], "--name", collector, "--memory", "256m", "--memory-swap", "256m",
                "--cpus", "1.0", *common[3:], "--entrypoint", "python", IMAGE,
                "/workspace/tests/phase6_replay_v2_runner.py", "--role", "collector",
            ])
            containers.append(collector)
            sample_callback = lambda: _sample(
                all_samples_path, arm, cycle,
                [pg_name, collector, *([engine] if engine in containers else [])],
                pg_name, started,
            )
            collector_bootstrap = _wait_marker(cycle_dir, "collector-bootstrap.json", 180, sample_callback)
            _run([
                *common[:3], "--name", engine, "--memory", "384m", "--memory-swap", "384m",
                "--cpus", "1.0", *common[3:], "--entrypoint", "python", IMAGE,
                "/workspace/tests/phase6_replay_v2_runner.py", "--role", "engine",
            ])
            containers.append(engine)
            marker_values = {}
            for name in REQUIRED_MARKERS:
                try:
                    marker_values[name] = _wait_marker(cycle_dir, name, 240, sample_callback)
                except TimeoutError:
                    _logs_to_file(collector, cycle_dir / "collector-timeout.log")
                    _logs_to_file(engine, cycle_dir / "engine-timeout.log")
                    raise
            health = _phase6_health(pg_name)
            health_gate = _health_gate(health)
            if not health_gate["pass"]:
                raise RuntimeError(f"Phase6 runtime health gate failed: {health_gate}")
            phase45_semantics = _phase45_semantics(pg_name)
            phase4_statuses = phase45_semantics["phase4_stage1"]
            if any(
                phase4_statuses[key] != {"NOT_AVAILABLE": phase4_statuses["rows"]}
                for key in ("liquidation_statuses", "long_short_statuses", "basis_statuses")
            ):
                raise RuntimeError("Phase4 unavailable semantics were not preserved")
            phase5_status = (phase45_semantics.get("phase5_context_health") or {}).get("details", {}).get("phase5_status")
            if phase5_status == "ERROR":
                raise RuntimeError("Phase5 context processing returned ERROR")
            cycle_elapsed = time.monotonic() - cycle_started
            observation_deadline = time.monotonic() + 5
            while time.monotonic() < observation_deadline:
                sample_callback()
                time.sleep(1)

            db_before_stop = _db_stats(pg_name)
            rows_before_stop = _row_counts(pg_name)
            phase6_before_stop = _phase6_health(pg_name)
            stop_output = _stop_parallel([collector, engine], grace=10)
            c_state = _container_state(collector)
            e_state = _container_state(engine)
            _logs_to_file(collector, cycle_dir / "collector.log")
            _logs_to_file(engine, cycle_dir / "engine.log")
            collector_shutdown = _json_marker(cycle_dir / "collector-shutdown.json")
            engine_shutdown = _json_marker(cycle_dir / "engine-shutdown.json")
            if not collector_shutdown or not engine_shutdown:
                raise RuntimeError("clean shutdown marker missing")
            if (c_state.get("ExitCode") != 0 or e_state.get("ExitCode") != 0
                    or c_state.get("OOMKilled") or e_state.get("OOMKilled")
                    or collector_shutdown.get("remaining_async_tasks")
                    or engine_shutdown.get("remaining_async_tasks")):
                raise RuntimeError("loaded Collector/Engine stop failed clean acceptance")
            rows_after_stop = _row_counts(pg_name)
            summaries.append({
                "cycle": cycle,
                "runtime_seconds": round(cycle_elapsed, 3),
                "collector_bootstrap": collector_bootstrap,
                "collector_replay": marker_values["collector-replay-ready.json"],
                "engine_bootstrap": marker_values["engine-bootstrap.json"],
                "engine_phase2_cycle": marker_values["engine-phase2-cycle.json"],
                "engine_phase2_cycle_runtime": marker_values["engine-phase2-cycle-runtime.json"],
                "phase2_input": marker_values["phase2-input-consumed.json"],
                "phase6_health_gate": health_gate,
                "phase6_health_rows_pre_stop": phase6_before_stop,
                "phase45_semantics": phase45_semantics,
                "postgres_stats_pre_stop": db_before_stop,
                "db_row_counts_pre_stop": rows_before_stop,
                "db_row_counts_post_stop": rows_after_stop,
                "stop_output": stop_output,
                "collector_state": {key: c_state.get(key) for key in ("Status", "ExitCode", "OOMKilled", "Error")},
                "engine_state": {key: e_state.get(key) for key in ("Status", "ExitCode", "OOMKilled", "Error")},
                "collector_shutdown": collector_shutdown,
                "engine_shutdown": engine_shutdown,
            })
            for name in (collector, engine):
                _run(["docker", "rm", name])
                containers.remove(name)

        reclaim_samples = []
        for _ in range(10):
            reclaim_samples.append(_sample(all_samples_path, arm, cycles, [pg_name], pg_name, started))
            time.sleep(1)
        final_stats = _db_stats(pg_name)
        final_rows = _row_counts(pg_name)
        manifest = json.loads((ROOT / "tests/fixtures/replays/phase6-replay-v2-manifest.json").read_text())
        expected_rows = manifest["expected_logical_rows"].get("per_three_cycle_resource_arm", {})
        expected_row_differences = {
            table: {"expected": count, "actual": final_rows.get(table)}
            for table, count in expected_rows.items()
            if final_rows.get(table) != count
        }
        expected_rows_gate = {
            "applicable": cycles == 3,
            "pass": cycles != 3 or not expected_row_differences,
            "compared_tables": sorted(expected_rows) if cycles == 3 else [],
            "differences": expected_row_differences if cycles == 3 else {},
        }
        if not expected_rows_gate["pass"]:
            raise RuntimeError(f"REPLAY_V2_EXPECTED_LOGICAL_ROWS_MISMATCH: {expected_row_differences}")
        last = summaries[-1]
        p2 = last["phase2_input"]
        collector_data = last["collector_replay"]
        logical_evidence = {
            "dataset_sha256": manifest["dataset_sha256"],
            "phase1_input_event_total": collector_data["dataset_event_total"],
            "phase1_event_type_counts": manifest["phase1_cassette"]["event_type_counts"],
            "phase2_fixture_event_count": p2["fixture_rows"],
            "phase2_event_type_counts": p2["event_type_counts"],
            "oi_observations": p2["open_interest_observations"],
            "funding_observations": p2["funding_observations"],
            "reset_count": collector_data["ws_reset_count"],
            "collector_logical_rows": collector_data,
            # Keep runtime measurements in the arm report, but exclude them
            # from deterministic logical equality across otherwise identical runs.
            "engine_bootstrap": {
                key: last["engine_bootstrap"][key]
                for key in (
                    "symbols", "available", "persisted",
                    "phase5_enabled", "phase6_enabled",
                )
                if key in last["engine_bootstrap"]
            },
            "phase2_cycle": last["engine_phase2_cycle"],
            "phase45_semantics": last["phase45_semantics"],
            "db_row_counts": final_rows,
        }
        logical_collector = logical_evidence["collector_logical_rows"]
        logical_evidence["collector_logical_rows"] = {
            key: logical_collector[key]
            for key in (
                "dataset_event_total", "ws_reset_count", "uta_expected", "uta_consumed",
                "phase3_expected", "phase3_consumed", "selected_symbol_count",
                "rest_instruments", "rest_tickers", "rest_klines", "dropped_closed_bars",
                "phase3_persistence_observed", "paper_mode",
            )
        }
        comparison = None
        if compare_to:
            reference = json.loads(compare_to.read_text(encoding="utf-8"))
            comparison = _compare(reference, {
                "git_sha": git_sha,
                "image_id": docker_id,
                "postgres_image_id": postgres_image_id,
                "dataset_sha256": manifest["dataset_sha256"],
                "logical_evidence": logical_evidence,
            })
        result = {
            "arm": arm,
            "git_sha": git_sha,
            "image": IMAGE,
            "image_id": docker_id,
            "postgres_image": POSTGRES_IMAGE,
            "postgres_image_id": postgres_image_id,
            "dataset_sha256": manifest["dataset_sha256"],
            "postgres_memory_cap": postgres_cap,
            "collector_memory_cap": "256m",
            "engine_memory_cap": "384m",
            "postgres_cpus": "0.5",
            "collector_engine_cpus": "1.0 each",
            "internal_network_no_egress": True,
            "published_ports": [],
            "cycles": summaries,
            "performance": {
                "engine_bootstrap_seconds": [cycle["engine_bootstrap"].get("runtime_seconds") for cycle in summaries],
                "collector_db_batch_persist_seconds": [cycle["collector_bootstrap"].get("persist_duration_seconds") for cycle in summaries],
                "engine_phase2_cycle_seconds": [cycle["engine_phase2_cycle_runtime"].get("duration_seconds") for cycle in summaries],
                "db_stats_roundtrip_latency_ms": _summarize_samples(all_samples_path)["postgres_stats_query_roundtrip_ms"],
                "note": "DB stats query latency includes docker exec + psql diagnostic overhead; not server-only SQL latency",
            },
            "logical_evidence": logical_evidence,
            "expected_logical_rows_gate": expected_rows_gate,
            "reproducibility_comparison": comparison,
            "baseline_database_size_bytes": baseline_size,
            "final_database_size_bytes": final_stats["size_bytes"],
            "database_growth_bytes": final_stats["size_bytes"] - baseline_size,
            "final_postgres_stats": final_stats,
            "final_db_row_counts": final_rows,
            "runtime_sample_summary": _summarize_samples(all_samples_path),
            "post_replay_reclaim_last_sample": reclaim_samples[-1] if reclaim_samples else None,
            "result_directory": str(results_dir),
            "total_elapsed_seconds": round(time.monotonic() - started, 3),
        }
        result_path = results_dir / "summary.json"
        result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({
            "arm": arm,
            "image_id": docker_id,
            "git_sha": git_sha,
            "dataset_sha256": manifest["dataset_sha256"],
            "result_directory": str(results_dir),
            "database_growth_bytes": result["database_growth_bytes"],
            "rows": final_rows,
            "reproducibility_comparison": comparison,
            "summary": str(result_path),
        }, sort_keys=True))
        if comparison is not None and not comparison["reproducible"]:
            raise RuntimeError("REPLAY_V2_REPRODUCIBLE=false; stop before resource comparison")
        return result
    finally:
        for name in list(containers):
            _logs_to_file(name, results_dir / f"{name}.log")
            _run(["docker", "stop", "--time", "10", name], check=False, timeout=30)
            _run(["docker", "rm", "-f", name], check=False, timeout=30)
        _logs_to_file(pg_name, results_dir / "postgres.log")
        _run(["docker", "stop", "--time", "10", pg_name], check=False, timeout=30)
        _run(["docker", "rm", "-f", pg_name], check=False, timeout=30)
        _run(["docker", "network", "rm", network], check=False, timeout=30)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=("smoke", "768a", "768b", "1g"), required=True)
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--compare-to", type=Path)
    args = parser.parse_args()
    cycles = 1 if args.arm == "smoke" else args.cycles
    if cycles < 1 or cycles > 3:
        raise SystemExit("cycles must be between 1 and 3")
    _run_arm(args.arm, cycles, args.compare_to)


if __name__ == "__main__":
    main()
