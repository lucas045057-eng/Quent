#!/usr/bin/env python3
"""Run a deterministic, isolated A/B/C Collector memory-capacity review."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
SCRIPTS = ROOT / "scripts"
RESULTS_ROOT = Path(os.environ.get("PHASE7_CAPACITY_RESULTS", "/tmp/quant-phase7-capacity-review"))
APP_IMAGE = "quant-phase7:phase7-a091cdd9b-clean-30d189485f40"
APP_IMAGE_ID = "sha256:e381b5c453f0362cc372a3a80e5459a0668cb7d7732f0b834d3f3bf9fa391f2c"
POSTGRES_IMAGE = "postgres:16-alpine"
POSTGRES_IMAGE_ID = "sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
GROUP_CAPS_MIB = (256, 320, 384)
MEASUREMENT_SAMPLES = 121
QUIESCENCE_SAMPLES = 13
SAMPLE_INTERVAL_SECONDS = 10
WARMUP_MAX_SAMPLES = 200
MAX_WARMUP_SECONDS = 1_990
MAX_CHECKPOINT_WAIT_SECONDS = 1_800
COLLECTOR_LIFETIME_SECONDS = 3_600
BASE_HEIGHT = 968_443
BTC_FINAL_CURSOR = BASE_HEIGHT + 143
ETH_FINAL_CURSOR = 20_000_000 + 119
SPOT_FINAL_CURSOR = 1_000


def _run(args: list[str], *, check: bool = True, timeout: int = 30) -> str:
    result = subprocess.run(args, cwd=ROOT, check=False, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {args[0]} {args[1] if len(args) > 1 else ''}")
    return result.stdout.strip()


def _image_info(image: str) -> tuple[str, str]:
    image_id = _run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    user = ""
    if image == APP_IMAGE:
        user = _run(["docker", "image", "inspect", "--format", "{{.Config.User}}", image])
    return image_id, user


def _package_manifest_from_image(image: str) -> dict[str, str]:
    code = """import hashlib, importlib, json, pathlib
out = {}
for name in ('quant_phase1', 'quant_phase2', 'quant_phase3', 'quant_phase4', 'quant_phase5', 'quant_phase6', 'quant_phase7'):
    root = pathlib.Path(importlib.import_module(name).__file__).parent
    for path in root.rglob('*.py'):
        out[name + '/' + path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
print(json.dumps(out, sort_keys=True, separators=(',', ':')))
"""
    output = _run(["docker", "run", "--rm", "--entrypoint", "python", image, "-c", code], timeout=60)
    return json.loads(output)


def _local_package_manifest() -> dict[str, str]:
    result = {}
    for package in sorted((ROOT / "src").glob("quant_phase[1-7]")):
        for path in package.rglob("*.py"):
            relative = f"{package.name}/{path.relative_to(package).as_posix()}"
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _verify_image_and_source() -> dict[str, Any]:
    app_id, app_user = _image_info(APP_IMAGE)
    postgres_id, _ = _image_info(POSTGRES_IMAGE)
    if app_id != APP_IMAGE_ID or postgres_id != POSTGRES_IMAGE_ID:
        raise RuntimeError("pinned runtime image identity differs from the reviewed image IDs")
    image_manifest = _package_manifest_from_image(APP_IMAGE)
    local_manifest = _local_package_manifest()
    if image_manifest != local_manifest:
        mismatches = sorted(set(image_manifest) ^ set(local_manifest))
        mismatches += sorted(
            name for name in set(image_manifest) & set(local_manifest)
            if image_manifest[name] != local_manifest[name]
        )
        raise RuntimeError(f"application image package source mismatch: {','.join(mismatches[:8])}")
    return {
        "app_image": APP_IMAGE,
        "app_image_id": app_id,
        "app_image_user": app_user,
        "postgres_image": POSTGRES_IMAGE,
        "postgres_image_id": postgres_id,
        "package_python_file_count": len(image_manifest),
        "package_source_sha256": hashlib.sha256(
            json.dumps(image_manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _docker_container_exists(name: str) -> bool:
    return bool(_run(["docker", "ps", "-a", "--format", "{{.Names}}"], check=True).splitlines().count(name))


def _wait_container_health(name: str, timeout_seconds: int = 120) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = _run(["docker", "inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", name])
        if status == "healthy":
            return
        state = _run(["docker", "inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", name])
        if state.startswith("exited") or state.startswith("dead"):
            raise RuntimeError(f"PostgreSQL container did not become healthy: {state}")
        time.sleep(2)
    raise TimeoutError("isolated PostgreSQL did not become healthy within the bound")


def _wait_for_file(path: Path, *, timeout_seconds: int, process_name: str) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        time.sleep(2)
    raise TimeoutError(f"{process_name} did not produce its bounded readiness marker")


def _container_state(name: str) -> dict[str, Any]:
    raw = _run(["docker", "inspect", "--format", "{{json .State}}", name], check=False)
    if not raw:
        return {"present": False}
    state = json.loads(raw)
    return {
        "present": True,
        "status": state.get("Status"),
        "running": state.get("Running"),
        "exit_code": state.get("ExitCode"),
        "oom_killed": state.get("OOMKilled"),
        "error": state.get("Error") or None,
    }


def _capture_sanitized_logs(name: str, destination: Path) -> bool:
    result = subprocess.run(
        ["docker", "logs", "--tail", "100", name],
        cwd=ROOT, check=False, capture_output=True, text=True, timeout=15,
    )
    if result.returncode:
        return False
    content = result.stdout + result.stderr
    content = re.sub(
        r"(?i)(api-key|authorization|password|token)(\s*[:=]\s*)[^\s,]+",
        r"\1\2[REDACTED]", content,
    )
    content = re.sub(r"https?://[^\s\"'<>]+", "[URL_REDACTED]", content)
    destination.write_text(content, encoding="utf-8")
    return True


def _wait_for_checkpoints(host: str, timeout_seconds: int) -> dict[str, Any]:
    import psycopg

    expected = {
        "btc_core_rpc:BITCOIN": BTC_FINAL_CURSOR,
        "ethereum_rpc:ETHEREUM": ETH_FINAL_CURSOR,
        "binance_spot:BTCUSDT": SPOT_FINAL_CURSOR,
        "binance_spot:ETHUSDT": SPOT_FINAL_CURSOR,
    }
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        with psycopg.connect(host=host, user="quant", dbname="quant", connect_timeout=3) as connection:
            rows = connection.execute(
                "SELECT source_id, scope_key, cursor_value, status FROM phase7_ingestion_checkpoints "
                "WHERE (source_id, scope_key) IN ((%s,%s),(%s,%s),(%s,%s),(%s,%s)) "
                "ORDER BY source_id,scope_key",
                ("btc_core_rpc", "BITCOIN", "ethereum_rpc", "ETHEREUM",
                 "binance_spot", "BTCUSDT", "binance_spot", "ETHUSDT"),
            ).fetchall()
        last = {f"{source}:{scope}": {"cursor": cursor, "status": status} for source, scope, cursor, status in rows}
        if all(
            key in last and int(last[key]["cursor"]) >= target and last[key]["status"] == "AVAILABLE"
            for key, target in expected.items()
        ):
            return last
        time.sleep(2)
    raise TimeoutError("deterministic Phase 7 input cursors did not reach the frozen end state")


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[rank]


def _slope_per_minute(points: list[tuple[float, float]]) -> float | None:
    if len(points) < 2:
        return None
    mean_x = statistics.mean(x for x, _ in points)
    mean_y = statistics.mean(y for _, y in points)
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    if denominator == 0:
        return None
    slope_per_second = sum((x - mean_x) * (y - mean_y) for x, y in points) / denominator
    return slope_per_second * 60


def _read_samples(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _memory_series(samples: list[dict[str, Any]], service: str, key: str) -> list[tuple[float, float]]:
    result = []
    for sample in samples:
        value = sample.get("resources", {}).get(service, {}).get(key)
        timestamp = sample.get("sampled_at_utc")
        if isinstance(value, (int, float)) and timestamp:
            point = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
            result.append((point, float(value)))
    return result


def _summarize_window(path: Path, *, expected_samples: int | None = None) -> dict[str, Any]:
    samples = _read_samples(path)
    if expected_samples is not None and len(samples) != expected_samples:
        raise RuntimeError(f"sample count mismatch for {path.name}: {len(samples)} != {expected_samples}")
    if not samples:
        return {"sample_count": 0, "complete": False}
    start = datetime.fromisoformat(samples[0]["sampled_at_utc"].replace("Z", "+00:00")).timestamp()
    first5 = [
        row for row in samples
        if datetime.fromisoformat(row["sampled_at_utc"].replace("Z", "+00:00")).timestamp() <= start + 300
    ]
    end = datetime.fromisoformat(samples[-1]["sampled_at_utc"].replace("Z", "+00:00")).timestamp()
    last5 = [
        row for row in samples
        if datetime.fromisoformat(row["sampled_at_utc"].replace("Z", "+00:00")).timestamp() >= end - 300
    ]
    metrics = (
        "memory_current", "memory_peak", "anon", "file", "kernel", "sock", "shmem",
        "slab", "pagetables", "process_rss", "process_pss", "process_smaps_rss",
        "process_vmsize", "process_threads", "tasks",
    )
    collector = [row["resources"]["quant-collector"] for row in samples]
    summary: dict[str, Any] = {
        "sample_count": len(samples),
        "duration_seconds": round(end - start, 3),
        "first5_minutes_samples": len(first5),
        "last5_minutes_samples": len(last5),
        "first5_minutes": {},
        "last5_minutes": {},
        "events_final": {
            "max": collector[-1]["events_max"],
            "oom": collector[-1]["events_oom"],
            "oom_kill": collector[-1]["events_oom_kill"],
        },
        "memory_current_slope_mib_per_min_last5": None,
        "anon_slope_mib_per_min_last5": None,
        "queue_slopes_per_min_last5": {},
        "checkpoint_first": samples[0].get("database", {}).get("checkpoints", {}),
        "checkpoint_last": samples[-1].get("database", {}).get("checkpoints", {}),
        "health_first": samples[0].get("database", {}).get("health", {}),
        "health_last": samples[-1].get("database", {}).get("health", {}),
        "application_metrics_last": samples[-1].get("database", {}).get("application_metrics", {}),
    }
    for bucket_name, bucket in (("first5_minutes", first5), ("last5_minutes", last5)):
        for key in metrics:
            values = [
                float(row["resources"]["quant-collector"][key])
                for row in bucket
                if isinstance(row["resources"]["quant-collector"].get(key), (int, float))
            ]
            if values:
                summary[bucket_name][key] = {
                    "median": statistics.median(values),
                    "p95": _percentile(values, 0.95),
                    "peak": max(values),
                }
        effective = [
            max(0, int(row["resources"]["quant-collector"]["memory_current"])
                - int(row["resources"]["quant-collector"].get("inactive_file", 0)))
            for row in bucket
        ]
        if effective:
            summary[bucket_name]["effective_working_set_after_inactive_file"] = {
                "median": statistics.median(effective),
                "p95": _percentile(effective, 0.95),
                "peak": max(effective),
            }
    for key in metrics:
        values = [
            float(row[key])
            for row in collector
            if isinstance(row.get(key), (int, float))
        ]
        if values:
            summary.setdefault("whole_window_peak", {})[key] = max(values)
    for key in ("memory_current", "anon"):
        points = _memory_series(samples, "quant-collector", key)
        last5_points = points[-len(last5):] if last5 else []
        summary[f"{key}_slope_mib_per_min_last5"] = (
            _slope_per_minute(last5_points) / (1024 * 1024)
            if _slope_per_minute(last5_points) is not None else None
        )
    effective_points = []
    for sample in samples:
        resource = sample["resources"]["quant-collector"]
        timestamp = datetime.fromisoformat(sample["sampled_at_utc"].replace("Z", "+00:00")).timestamp()
        effective_points.append((timestamp, max(0, resource["memory_current"] - resource.get("inactive_file", 0))))
    effective_slope = _slope_per_minute(effective_points[-len(last5):]) if last5 else None
    summary["effective_working_set_slope_mib_per_min_last5"] = (
        effective_slope / (1024 * 1024) if effective_slope is not None else None
    )

    queue_points: dict[str, list[tuple[float, float]]] = {}
    for sample in samples:
        timestamp = datetime.fromisoformat(sample["sampled_at_utc"].replace("Z", "+00:00")).timestamp()
        app = sample.get("database", {}).get("application_metrics", {})
        for component, details in app.items():
            for key, value in details.items():
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    continue
                if "queue" in key or key.startswith("kline_recovery_") or "backfill" in key or "backlog" in key:
                    queue_points.setdefault(f"{component}.{key}", []).append((timestamp, float(value)))
    for name, points in queue_points.items():
        last5_points = points[-len(last5):] if last5 else []
        slope = _slope_per_minute(last5_points)
        if slope is not None:
            summary["queue_slopes_per_min_last5"][name] = slope
    summary["cpu_usage_core_seconds"] = {
        service: round(
            (samples[-1]["resources"][service]["cpu_usage_usec"]
             - samples[0]["resources"][service]["cpu_usage_usec"]) / 1_000_000,
            3,
        )
        for service in ("postgres", "quant-collector", "quant-engine")
        if isinstance(samples[-1]["resources"][service].get("cpu_usage_usec"), int)
        and isinstance(samples[0]["resources"][service].get("cpu_usage_usec"), int)
    }
    return summary


def _canonical(value: Any) -> Any:
    from decimal import Decimal

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _semantic_hash(connection, name: str, query: str) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    with connection.cursor(name=f"capacity_{name}") as cursor:
        cursor.itersize = 1_000
        cursor.execute(query)
        while rows := cursor.fetchmany(1_000):
            for row in rows:
                encoded = json.dumps(_canonical(row), ensure_ascii=True, separators=(",", ":")).encode()
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
                count += 1
    return {"rows": count, "sha256": digest.hexdigest()}


def _database_snapshot(host: str) -> dict[str, Any]:
    import psycopg
    from psycopg import sql

    semantic_queries = {
        "bitcoin_events": "SELECT event_id,chain,block_number,block_hash,tx_hash,event_index,event_index_kind,asset_id,amount_raw,decimals,amount_normalized,status,finality_status,event_time,observed_at,fetched_at,processed_at,source_id,source_hash FROM phase7_onchain_transfer_events WHERE chain='BITCOIN' ORDER BY event_id",
        "ethereum_events": "SELECT event_id,chain,block_number,block_hash,tx_hash,tx_index,event_index,event_index_kind,asset_id,contract_address,amount_raw,decimals,amount_normalized,status,finality_status,event_time,observed_at,fetched_at,processed_at,source_id,source_hash FROM phase7_onchain_transfer_events WHERE chain='ETHEREUM' ORDER BY event_id",
        "spot_windows": "SELECT exchange,symbol,market_kind,timeframe,window_open,window_close,aggregation_version,base_volume,quote_volume,buy_volume,sell_volume,unknown_volume,delta,cvd,trade_count,directional_trade_count,event_time_first,event_time_last,cursor_first,cursor_last,sample_count,source_count,available_count,missing_count,coverage_ratio,status,reason,source_reference,normalization_version,processed_at,created_at FROM phase7_spot_flow_windows ORDER BY exchange,symbol,market_kind,timeframe,window_open,aggregation_version",
        "checkpoints": "SELECT source_id,scope_kind,scope_key,cursor_kind,cursor_value,last_observed_cursor,last_finalized_cursor,last_block_hash,parser_version,schema_version,status,reason FROM phase7_ingestion_checkpoints ORDER BY source_id,scope_kind,scope_key",
    }
    with psycopg.connect(host=host, user="quant", dbname="quant", connect_timeout=5) as connection:
        table_names = [row[0] for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).fetchall()]
        table_counts = {
            name: connection.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name))).fetchone()[0]
            for name in table_names
        }
        database_size = connection.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        health = connection.execute("SELECT component,status FROM system_health ORDER BY component").fetchall()
        semantic = {
            name: _semantic_hash(connection, name, query)
            for name, query in semantic_queries.items()
            if connection.execute("SELECT to_regclass(%s)", (query.split(" FROM ")[1].split()[0],)).fetchone()[0]
        }
    return {
        "database_size_bytes": database_size,
        "table_counts": table_counts,
        "semantic_outputs": semantic,
        "health": {str(component): str(status) for component, status in health},
    }


def _container_network_ip(name: str) -> str:
    return _run([
        "docker", "inspect", "--format",
        "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name,
    ])


def _start_monitor(
    project: str,
    output: Path,
    log_path: Path,
    *,
    samples: int,
    stop_file: Path | None = None,
) -> tuple[subprocess.Popen, Any]:
    command = [
        sys.executable, str(SCRIPTS / "phase7_acceptance_monitor.py"),
        "--project", project,
        "--samples", str(samples),
        "--interval-seconds", str(SAMPLE_INTERVAL_SECONDS),
        "--output", str(output),
    ]
    if stop_file is not None:
        command.extend(("--stop-file", str(stop_file)))
    log = log_path.open("w", encoding="utf-8")
    return subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT), log


def _wait_monitor(process: subprocess.Popen, log, *, label: str, timeout_seconds: int) -> int:
    started = time.monotonic()
    next_update = 60
    while process.poll() is None:
        elapsed = int(time.monotonic() - started)
        if elapsed >= next_update:
            print(json.dumps({"stage": label, "elapsed_seconds": elapsed}, separators=(",", ":")), flush=True)
            next_update += 60
        if elapsed > timeout_seconds:
            process.terminate()
            process.wait(timeout=10)
            log.close()
            raise TimeoutError(f"bounded monitor exceeded time limit: {label}")
        time.sleep(2)
    return_code = process.returncode
    log.close()
    return return_code


def _wait_for_marker(path: Path, containers: list[str], *, timeout_seconds: int, label: str) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        for name in containers:
            state = _container_state(name)
            if state.get("present") and not state.get("running"):
                raise RuntimeError(f"container exited before {label}: {name}={state}")
        time.sleep(2)
    raise TimeoutError(f"bounded readiness marker not observed: {label}")


def _wait_warmup_monitor(
    process: subprocess.Popen,
    log,
    host: str,
    stop_file: Path,
    containers: list[str],
    *,
    timeout_seconds: int,
    label: str,
) -> dict[str, Any]:
    expected = {
        "btc_core_rpc:BITCOIN": BTC_FINAL_CURSOR,
        "ethereum_rpc:ETHEREUM": ETH_FINAL_CURSOR,
        "binance_spot:BTCUSDT": SPOT_FINAL_CURSOR,
        "binance_spot:ETHUSDT": SPOT_FINAL_CURSOR,
    }
    started = time.monotonic()
    next_update = 60
    last: dict[str, Any] = {}
    try:
        while time.monotonic() - started < timeout_seconds:
            if process.poll() is not None:
                raise RuntimeError(f"warmup monitor exited early ({process.returncode}); evidence retained")
            states = {name: _container_state(name) for name in containers}
            stopped = [name for name, state in states.items() if not state.get("running")]
            if stopped:
                raise RuntimeError(f"runtime container stopped during deterministic catch-up: {stopped}")
            try:
                import psycopg

                with psycopg.connect(host=host, user="quant", dbname="quant", connect_timeout=3) as connection:
                    rows = connection.execute(
                        "SELECT source_id,scope_key,cursor_value,status FROM phase7_ingestion_checkpoints "
                        "WHERE (source_id,scope_key) IN ((%s,%s),(%s,%s),(%s,%s),(%s,%s)) "
                        "ORDER BY source_id,scope_key",
                        ("btc_core_rpc", "BITCOIN", "ethereum_rpc", "ETHEREUM",
                         "binance_spot", "BTCUSDT", "binance_spot", "ETHUSDT"),
                    ).fetchall()
                last = {f"{source}:{scope}": {"cursor": cursor, "status": status} for source, scope, cursor, status in rows}
                if all(
                    key in last and int(last[key]["cursor"]) >= target and last[key]["status"] == "AVAILABLE"
                    for key, target in expected.items()
                ):
                    stop_file.touch()
                    break
            except Exception as exc:
                # Startup may race the first schema creation; only the exception class is recorded.
                if type(exc).__name__ not in {"OperationalError", "UndefinedTable"}:
                    raise
            elapsed = int(time.monotonic() - started)
            if elapsed >= next_update:
                print(json.dumps({
                    "stage": label, "elapsed_seconds": elapsed,
                    "checkpoints": last,
                }, separators=(",", ":")), flush=True)
                next_update += 60
            time.sleep(2)
        else:
            raise TimeoutError("Phase 7 deterministic catch-up exceeded the configured bound")
        if not stop_file.exists():
            raise RuntimeError("warmup ended without the frozen final cursors")
        return_code = process.wait(timeout=20)
        if return_code != 0:
            raise RuntimeError(f"warmup monitor returned {return_code}; evidence retained")
        return last
    finally:
        log.close()


def _run_one_group(cap_mib: int, run_id: str, image_info: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    project = f"p7-cap-{cap_mib}-{run_id}"
    network = f"{project}-internal"
    volume = f"{project}-pgdata"
    names = {
        "postgres": f"{project}_postgres_1",
        "collector": f"{project}_quant-collector_1",
        "engine": f"{project}_quant-engine_1",
    }
    result_dir = RESULTS_ROOT / project
    result_dir.mkdir(parents=True, exist_ok=False)
    result_dir.chmod(0o777)
    output_dir = result_dir / "runtime"
    output_dir.mkdir(mode=0o777)
    output_dir.chmod(0o777)
    group: dict[str, Any] = {
        "cap_mib": cap_mib,
        "project": project,
        "network": network,
        "volume": volume,
        "container_names": names,
        "app_image_id": image_info["app_image_id"],
        "postgres_image_id": image_info["postgres_image_id"],
        "dataset_sha256": identity["dataset_sha256"],
        "dataset_generator": identity["generator"],
        "runtime_config": {
            "trading_mode": "paper",
            "collector_cap_mib": cap_mib,
            "engine_cap_mib": 384,
            "postgres_cap_mib": 768,
            "phase1_through_phase6": "frozen Replay V2 runner settings",
            "phase7": "Bitcoin mainnet-shaped deterministic fixed tip; Ethereum chainId=1 deterministic fixed finalized range; Binance Spot deterministic aggTrade one-shot stream",
            "external_network": "disabled via Docker internal network",
            "api_key": "not supplied",
            "trading_api": "not used",
        },
        "result_dir": str(result_dir),
        "status": "IN_PROGRESS",
        "input_access": "INTERNAL_NETWORK_ONLY; synthetic fixed replay boundaries; no external network",
    }
    created_network = False
    created_volume = False
    monitor_process = None
    monitor_log = None
    group_started = time.monotonic()
    try:
        if _docker_container_exists(names["postgres"]):
            raise RuntimeError(f"refusing to reuse pre-existing container: {names['postgres']}")
        _run(["docker", "network", "create", "--internal", "--driver", "bridge",
              "--label", "project=quant", "--label", "purpose=phase7-capacity-review", network])
        created_network = True
        _run(["docker", "volume", "create", "--label", "project=quant",
              "--label", "purpose=phase7-capacity-review", volume])
        created_volume = True
        postgres_command = [
            "docker", "run", "--detach", "--name", names["postgres"],
            "--network", network, "--network-alias", "postgres", "--memory", "768m",
            "--health-cmd", "pg_isready -U quant -d quant", "--health-interval", "5s",
            "--health-timeout", "3s", "--health-retries", "20",
            "--label", "project=quant", "--label", "purpose=phase7-capacity-review",
            "--log-opt", "max-size=10m", "--log-opt", "max-file=2",
            "--env", "POSTGRES_DB=quant", "--env", "POSTGRES_USER=quant",
            "--env", "POSTGRES_HOST_AUTH_METHOD=trust", "--env", "TZ=UTC",
            "--mount", f"type=volume,src={volume},dst=/var/lib/postgresql/data",
            POSTGRES_IMAGE,
        ]
        _run(postgres_command, timeout=60)
        _wait_container_health(names["postgres"])
        _run(["docker", "exec", names["postgres"], "sh", "-c",
              "test -r /sys/fs/cgroup/memory.current && test -r /sys/fs/cgroup/memory.stat"])
        _run(["docker", "run", "--rm", "--network", network, "--entrypoint", "python",
              "--mount", f"type=bind,src={TESTS},dst=/workspace/tests,readonly",
              "--mount", f"type=bind,src={SCRIPTS},dst=/workspace/scripts,readonly",
              "--mount", f"type=bind,src={output_dir},dst=/run-results",
              "--env", "TRADING_MODE=paper", "--env", "PHASE7_RESOURCE_REPLAY_V3=1",
              "--env", "POSTGRES_DSN=postgresql://quant@postgres:5432/quant",
              "--env", "REPLAY_RESULTS_DIR=/run-results",
              "--env", "RESOURCE_REPLAY_V3_SCRIPTS=/workspace/scripts",
              APP_IMAGE, "-c", "import socket; s=socket.create_connection(('postgres',5432),3); s.close()"],
             timeout=20)

        common_app_args = [
            "--detach", "--network", network,
            "--log-opt", "max-size=10m", "--log-opt", "max-file=2",
            "--label", "project=quant", "--label", "purpose=phase7-capacity-review",
            "--mount", f"type=bind,src={TESTS},dst=/workspace/tests,readonly",
            "--mount", f"type=bind,src={SCRIPTS},dst=/workspace/scripts,readonly",
            "--mount", f"type=bind,src={output_dir},dst=/run-results",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m",
            "--env", "TRADING_MODE=paper",
            "--env", "PHASE7_RESOURCE_REPLAY_V3=1",
            "--env", "REPLAY_COLLECTOR_MAX_LIFETIME_SECONDS=3600",
            "--env", "POSTGRES_DSN=postgresql://quant@postgres:5432/quant",
            "--env", "REPLAY_RESULTS_DIR=/run-results",
            "--env", "RESOURCE_REPLAY_V3_SCRIPTS=/workspace/scripts",
        ]
        _run(["docker", "run", *common_app_args, "--name", names["collector"],
              "--memory", f"{cap_mib}m", APP_IMAGE,
              "python", "/workspace/tests/phase7_resource_replay_v3_runner.py", "--role", "collector"], timeout=60)
        group["runtime_started_utc"] = datetime.now(timezone.utc).isoformat()
        group["collector_limit_bytes"] = cap_mib * 1024 * 1024
        _wait_for_marker(
            output_dir / "collector-replay-ready.json", [names["collector"]],
            timeout_seconds=600, label="Phase 1-6 Collector lifecycle readiness",
        )
        _wait_for_marker(
            output_dir / "collector-bootstrap.json", [names["collector"]],
            timeout_seconds=10, label="Phase 1 database bootstrap",
        )

        engine_args = [
            "docker", "run", *common_app_args, "--name", names["engine"],
            "--memory", "384m", APP_IMAGE,
            "python", "/workspace/tests/phase7_resource_replay_v3_runner.py", "--role", "engine",
        ]
        _run(engine_args, timeout=60)
        _wait_for_marker(
            output_dir / "engine-phase2-cycle-runtime.json", [names["collector"], names["engine"]],
            timeout_seconds=300, label="Engine Phase 2 lifecycle readiness",
        )

        pg_ip = _container_network_ip(names["postgres"])
        warm_samples = output_dir / "warmup-metrics.jsonl"
        warm_log_path = result_dir / "warmup-monitor.log"
        warm_stop = result_dir / "stop-warmup-monitor"
        monitor_process, monitor_log = _start_monitor(
            project, warm_samples, warm_log_path,
            samples=WARMUP_MAX_SAMPLES, stop_file=warm_stop,
        )
        warm_checkpoints = _wait_warmup_monitor(
            monitor_process, monitor_log, pg_ip, warm_stop,
            [names["postgres"], names["collector"], names["engine"]],
            timeout_seconds=MAX_WARMUP_SECONDS,
            label=f"GROUP_{cap_mib}_PHASE7_CATCHUP",
        )
        monitor_process = None
        monitor_log = None
        group["warmup_checkpoints_final"] = warm_checkpoints
        group["warmup_metrics"] = _summarize_window(warm_samples)

        group["data_after_catchup"] = _database_snapshot(pg_ip)
        for name in names.values():
            state = _container_state(name)
            if not state.get("running") or state.get("oom_killed"):
                raise RuntimeError(f"runtime not healthy at steady-state start: {name}={state}")
        group["steady_started_utc"] = datetime.now(timezone.utc).isoformat()
        steady_samples = output_dir / "steady-20m-metrics.jsonl"
        steady_log_path = result_dir / "steady-monitor.log"
        monitor_process, monitor_log = _start_monitor(
            project, steady_samples, steady_log_path, samples=MEASUREMENT_SAMPLES,
        )
        return_code = _wait_monitor(
            monitor_process, monitor_log, label=f"GROUP_{cap_mib}_STEADY_20M",
            timeout_seconds=1_350,
        )
        monitor_process = None
        monitor_log = None
        if return_code != 0:
            raise RuntimeError(f"20-minute steady-state monitor returned {return_code}")
        group["steady_ended_utc"] = datetime.now(timezone.utc).isoformat()
        group["steady_metrics"] = _summarize_window(steady_samples, expected_samples=MEASUREMENT_SAMPLES)
        group["data_before_quiescence"] = _database_snapshot(pg_ip)

        group["quiescence_started_utc"] = datetime.now(timezone.utc).isoformat()
        (result_dir / "deterministic-inputs-drained").touch()
        quiescence_samples = output_dir / "quiescence-120s-metrics.jsonl"
        quiescence_log_path = result_dir / "quiescence-monitor.log"
        monitor_process, monitor_log = _start_monitor(
            project, quiescence_samples, quiescence_log_path,
            samples=QUIESCENCE_SAMPLES,
        )
        return_code = _wait_monitor(
            monitor_process, monitor_log, label=f"GROUP_{cap_mib}_QUIESCENCE_120S",
            timeout_seconds=180,
        )
        monitor_process = None
        monitor_log = None
        if return_code != 0:
            raise RuntimeError(f"quiescence monitor returned {return_code}")
        group["quiescence_ended_utc"] = datetime.now(timezone.utc).isoformat()
        group["quiescence_metrics"] = _summarize_window(
            quiescence_samples, expected_samples=QUIESCENCE_SAMPLES,
        )
        group["data_after_quiescence"] = _database_snapshot(pg_ip)
        group["quiescence_business_unchanged"] = (
            group["data_before_quiescence"]["table_counts"] == group["data_after_quiescence"]["table_counts"]
            and group["data_before_quiescence"]["semantic_outputs"] == group["data_after_quiescence"]["semantic_outputs"]
        )
        group["container_state_before_shutdown"] = {key: _container_state(value) for key, value in names.items()}
        group["docker_stats_before_shutdown"] = _run([
            "docker", "stats", "--no-stream", "--format",
            "{{.Name}} {{.CPUPerc}} {{.MemUsage}} {{.MemPerc}}", *names.values(),
        ])
        group["status"] = "PASS" if group["quiescence_business_unchanged"] else "QUIESCENCE_CHANGED"
    except Exception as exc:
        group["status"] = "INCOMPLETE"
        group["failure_type"] = type(exc).__name__
        group["failure_reason"] = str(exc)[:500]
        group["container_state_on_failure"] = {
            key: _container_state(value) for key, value in names.items()
        }
        group["sanitized_failure_logs"] = {
            key: str(result_dir / f"{key}-failure.log")
            for key, name in names.items()
            if _capture_sanitized_logs(name, result_dir / f"{key}-failure.log")
        }
        if monitor_process is not None and monitor_process.poll() is None:
            monitor_process.terminate()
            try:
                monitor_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                monitor_process.kill()
                monitor_process.wait(timeout=5)
        if monitor_log is not None:
            monitor_log.close()
        try:
            if _docker_container_exists(names["postgres"]):
                pg_ip = _container_network_ip(names["postgres"])
                if pg_ip:
                    try:
                        group["data_at_failure"] = _database_snapshot(pg_ip)
                    except Exception:
                        group["data_at_failure"] = {"status": "NOT_READABLE"}
        except Exception:
            pass
    finally:
        for key in ("collector", "engine", "postgres"):
            name = names[key]
            if _docker_container_exists(name):
                stop_result = _run(["docker", "stop", "--time", "20", name], check=False, timeout=30)
                group.setdefault("stop_results", {})[key] = stop_result
                group.setdefault("stopped_container_states", {})[key] = _container_state(name)
                if key == "collector":
                    shutdown_marker = output_dir / "collector-shutdown.json"
                    if shutdown_marker.exists():
                        group["collector_shutdown"] = json.loads(shutdown_marker.read_text(encoding="utf-8"))
                if key == "engine":
                    shutdown_marker = output_dir / "engine-shutdown.json"
                    if shutdown_marker.exists():
                        group["engine_shutdown"] = json.loads(shutdown_marker.read_text(encoding="utf-8"))
                _run(["docker", "rm", name], check=False, timeout=30)
        if created_network:
            _run(["docker", "network", "rm", network], check=False)
        group["volume_preserved"] = created_volume and bool(
            _run(["docker", "volume", "inspect", volume], check=False)
        )
        group["container_state_after_shutdown"] = {
            key: _container_state(value) for key, value in names.items()
        }
        group["runtime_duration_seconds"] = round(time.monotonic() - group_started, 3)
        (result_dir / "group-result.json").write_text(
            json.dumps(group, sort_keys=True, indent=2, ensure_ascii=True) + "\n", encoding="utf-8",
        )
        print(json.dumps({
            "stage": f"GROUP_{cap_mib}_COMPLETE",
            "status": group["status"],
            "result_dir": str(result_dir),
            "failure_type": group.get("failure_type"),
        }, separators=(",", ":")), flush=True)
    return group


def _business_equality(groups: list[dict[str, Any]]) -> dict[str, Any]:
    complete = [group for group in groups if group.get("status") == "PASS" and "data_after_quiescence" in group]
    if len(complete) != len(GROUP_CAPS_MIB):
        return {"all_three_groups_complete": False, "equal": False}
    reference = complete[0]["data_after_quiescence"]
    equal = all(
        group["data_after_quiescence"]["table_counts"] == reference["table_counts"]
        and group["data_after_quiescence"]["semantic_outputs"] == reference["semantic_outputs"]
        for group in complete[1:]
    )
    return {
        "all_three_groups_complete": True,
        "equal": equal,
        "semantic_outputs": reference["semantic_outputs"],
        "table_counts": reference["table_counts"],
    }


def _resource_decision(groups: list[dict[str, Any]], business: dict[str, Any]) -> dict[str, Any]:
    if len(groups) != 3 or any(group.get("status") != "PASS" for group in groups):
        failed = [
            {"cap_mib": group.get("cap_mib"), "failure_type": group.get("failure_type"),
             "failure_reason": group.get("failure_reason")}
            for group in groups if group.get("status") != "PASS"
        ]
        return {"decision": "PHASE7_RESOURCE_ACCEPTANCE_BLOCKED", "root_cause": failed or "GROUP_COUNT_INCOMPLETE"}
    if not business.get("equal"):
        return {"decision": "PHASE7_RESOURCE_ACCEPTANCE_BLOCKED", "root_cause": "A_B_C_BUSINESS_OUTPUTS_DIFFER"}

    cap_a = next(group for group in groups if group["cap_mib"] == 256)
    cap_b = next(group for group in groups if group["cap_mib"] == 320)
    cap_c = next(group for group in groups if group["cap_mib"] == 384)
    a_metrics = cap_a["steady_metrics"]
    a_events = a_metrics["events_final"]
    last5 = a_metrics["last5_minutes"]
    current_p95_mib = last5["memory_current"]["p95"] / (1024 * 1024)
    effective_p95_mib = last5["effective_working_set_after_inactive_file"]["p95"] / (1024 * 1024)
    anon_p95_mib = last5["anon"]["p95"] / (1024 * 1024)
    limit_mib = 256
    pressure_gate_mib = limit_mib * 0.95
    anon_slope = abs(a_metrics.get("anon_slope_mib_per_min_last5") or 0)
    effective_slope = abs(a_metrics.get("effective_working_set_slope_mib_per_min_last5") or 0)
    plateau = effective_slope <= 0.25 and anon_slope <= 0.25
    oom_free = all(a_events[key] == 0 for key in ("max", "oom", "oom_kill"))
    group_plateaus = all(
        abs(group["steady_metrics"].get("effective_working_set_slope_mib_per_min_last5") or 0) <= 0.25
        and abs(group["steady_metrics"].get("anon_slope_mib_per_min_last5") or 0) <= 0.25
        and all(group["steady_metrics"]["events_final"][key] == 0 for key in ("max", "oom", "oom_kill"))
        for group in groups
    )
    if not group_plateaus:
        return {
            "decision": "PHASE7_RESOURCE_ACCEPTANCE_BLOCKED",
            "root_cause": "ONE_OR_MORE_CAP_GROUPS_DID_NOT_REACH_A_MEMORY_PLATEAU_OR_HAD_MEMORY_EVENTS",
        }
    if effective_p95_mib >= pressure_gate_mib and oom_free and plateau:
        return {
            "decision": "COLLECTOR_256M_CAPACITY_INSUFFICIENT=true",
            "secondary_status": "COLLECTOR_RESOURCE_BUDGET_REVIEW_REQUIRED=true",
            "root_cause": "256_MIB_STEADY_P95_AT_OR_ABOVE_95_PERCENT_WITH_STABLE_A_B_C_OUTPUTS",
            "collector_256m_current_p95_mib": current_p95_mib,
            "collector_256m_effective_p95_mib": effective_p95_mib,
            "collector_256m_anon_p95_mib": anon_p95_mib,
            "collector_320m_last5_p95_mib": cap_b["steady_metrics"]["last5_minutes"]["memory_current"]["p95"] / (1024 * 1024),
            "collector_384m_last5_p95_mib": cap_c["steady_metrics"]["last5_minutes"]["memory_current"]["p95"] / (1024 * 1024),
        }
    if effective_p95_mib < pressure_gate_mib and oom_free and plateau:
        return {
            "decision": "PHASE7_RESOURCE_BLOCKER_CLEARED=true",
            "root_cause": "256_MIB_CONTROLLED_WORKLOAD_BELOW_95_PERCENT_AND_PLATEAUED",
            "collector_256m_current_p95_mib": current_p95_mib,
            "collector_256m_effective_p95_mib": effective_p95_mib,
            "collector_256m_anon_p95_mib": anon_p95_mib,
            "required_next_gate": "RUN_AT_LEAST_10_MINUTES_REAL_D_GROUP_AT_FORMAL_256_MIB_BEFORE_CLAIMING_CLEARANCE",
        }
    return {
        "decision": "PHASE7_RESOURCE_ACCEPTANCE_BLOCKED",
        "root_cause": "256_MIB_CRITERIA_NOT_MET_WITHOUT_SUFFICIENT_EVIDENCE_FOR_CAPACITY_CONCLUSION",
    }


def main() -> int:
    import argparse
    import uuid

    parser = argparse.ArgumentParser()
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--caps", default="256,320,384")
    args = parser.parse_args()
    if os.environ.get("TRADING_MODE", "paper").lower() != "paper":
        raise SystemExit("resource replay is paper-only")

    branch = _run(["git", "branch", "--show-current"])
    git_sha = _run(["git", "rev-parse", "HEAD"])
    status = _run(["git", "status", "--porcelain=v1"])
    if branch != "phase7":
        raise RuntimeError(f"expected phase7 branch, found {branch}")
    if status:
        raise RuntimeError("worktree must be clean before controlled resource groups start")
    source_changes = _run([
        "git", "diff", "--name-only", "c156ae3f48083a51eba9dc37721feb3de250082d..HEAD", "--", "src",
    ])
    if source_changes:
        raise RuntimeError("production source differs from the reviewed c156ae3 baseline")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "c156ae3f48083a51eba9dc37721feb3de250082d", "HEAD"],
        cwd=ROOT, check=False,
    )
    if ancestor.returncode != 0:
        raise RuntimeError("reviewed production baseline is not an ancestor of HEAD")
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".env.local"], cwd=ROOT, check=False,
    ).returncode == 0
    if not ignored:
        raise RuntimeError(".env.local is not ignored; refusing to proceed")
    docker_info = _run(["docker", "info", "--format", "{{.ServerVersion}} {{.CgroupVersion}}"], timeout=30)
    image_info = _verify_image_and_source()

    sys.path.insert(0, str(TESTS))
    from phase7_resource_replay_v3_runner import dataset_identity

    identity = dataset_identity()
    if identity["phase1_input_rows"] != 84_660 or identity["phase2_input_rows"] != 603:
        raise RuntimeError("frozen deterministic baseline count changed")
    preflight = {
        "branch": branch,
        "git_sha": git_sha,
        "production_source_baseline": "c156ae3f48083a51eba9dc37721feb3de250082d",
        "production_source_changes_after_baseline": [],
        "worktree_clean": True,
        "env_local_ignored": True,
        "docker_server_cgroup": docker_info,
        "images": image_info,
        "dataset": identity,
        "mode": "paper",
        "formal_collector_cap_mib": 256,
        "diagnostic_caps_mib": [320, 384],
        "postgres_cap_mib": 768,
        "engine_cap_mib": 384,
        "steady_samples": MEASUREMENT_SAMPLES,
        "steady_interval_seconds": SAMPLE_INTERVAL_SECONDS,
        "steady_duration_seconds": (MEASUREMENT_SAMPLES - 1) * SAMPLE_INTERVAL_SECONDS,
        "quiescence_samples": QUIESCENCE_SAMPLES,
        "quiescence_duration_seconds": (QUIESCENCE_SAMPLES - 1) * SAMPLE_INTERVAL_SECONDS,
        "external_network": "DISABLED (Docker internal network only)",
        "private_api_or_trading": "NOT USED",
    }
    if args.preflight_only:
        print(json.dumps(preflight, sort_keys=True, indent=2))
        return 0

    caps = tuple(int(value) for value in args.caps.split(",") if value)
    if caps != GROUP_CAPS_MIB:
        raise RuntimeError("formal review must run the exact 256,320,384 MiB cap sequence")
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    RESULTS_ROOT.chmod(0o755)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + uuid.uuid4().hex[:6]
    report: dict[str, Any] = {"preflight": preflight, "groups": []}
    for cap in caps:
        group = _run_one_group(cap, run_id, image_info, identity)
        report["groups"].append(group)
    business = _business_equality(report["groups"])
    report["business_equality"] = business
    report["decision"] = _resource_decision(report["groups"], business)
    report["run_finished_utc"] = datetime.now(timezone.utc).isoformat()
    report_path = RESULTS_ROOT / f"capacity-review-{run_id}.json"
    report_path.write_text(json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "decision": report["decision"],
        "git_sha": git_sha,
        "dataset_sha256": identity["dataset_sha256"],
        "groups": [{
            "cap_mib": group["cap_mib"], "status": group["status"],
            "steady_duration_seconds": group.get("steady_metrics", {}).get("duration_seconds"),
            "result_dir": group["result_dir"],
        } for group in report["groups"]],
        "business_equal": business.get("equal"),
        "report_path": str(report_path),
    }, sort_keys=True, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
