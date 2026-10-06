#!/usr/bin/env python3
"""Run three bounded, isolated Phase 1–8 deterministic database replays."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
MANIFEST_PATH = TESTS / "fixtures/data_layer_replay_v1/manifest.json"
OWNER = "data-layer-v1-replay"
APPLICATION_MEMORY = {"collector": "256m", "engine": "384m", "phase8": "384m"}
MAX_COLLECTOR_SECONDS = 420
MAX_ENGINE_SECONDS = 180
MAX_RESULT_ARTIFACT_BYTES = 8 * 1024 * 1024
HASH_TABLE_PATTERNS = (
    "kline", "market_snapshot", "market_observation", "open_interest", "funding",
    "screening", "stage1", "universe", "trade_flow", "liquidation", "basis",
    "phase5_", "phase6_", "phase7_", "phase8_", "exchange_instrument", "symbol",
)
VOLATILE_TABLE_NAMES = {"runtime_health_events", "system_health_events"}


def postgres_container_args(
    *, container_name: str, network_name: str, image: str, run_id: str | None = None,
) -> list[str]:
    identity = run_id or container_name
    return [
        "docker", "create", "--name", container_name,
        "--label", f"quant.owner={OWNER}",
        "--label", f"quant.run_id={identity}",
        "--network", network_name, "--network-alias", "quant-postgres",
        "--memory=768m", "--memory-swap=768m", "--cpus=1",
        "--tmpfs", "/var/lib/postgresql/data:rw,noexec,nosuid,size=512m",
        "--log-driver", "local", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
        "--health-cmd", "pg_isready -U quant -d quant",
        "--health-interval", "2s", "--health-timeout", "2s", "--health-retries", "60",
        "--env", "POSTGRES_DB=quant", "--env", "POSTGRES_USER=quant",
        "--env", "POSTGRES_HOST_AUTH_METHOD=trust", "--env", "TZ=UTC",
        image, "postgres", "-c", "timezone=UTC",
    ]


def network_create_args(*, network_name: str) -> list[str]:
    return [
        "docker", "network", "create", "--internal",
        "--label", f"quant.owner={OWNER}", network_name,
    ]


def app_container_args(
    *,
    container_name: str,
    network_name: str,
    image: str,
    role: str,
    dsn: str,
    tests_mount: str,
    scripts_mount: str,
    results_mount: str = "/tmp/quant-dlr-results",
    run_id: str | None = None,
) -> list[str]:
    if role not in APPLICATION_MEMORY:
        raise ValueError("role must be collector, engine, or phase8")
    memory = APPLICATION_MEMORY[role]
    if role == "phase8":
        role_command = ["/app/tests/data_layer_replay_v1_db_runner.py"]
        role_environment: list[str] = []
    else:
        role_command = ["/app/tests/phase7_resource_replay_v3_runner.py", "--role", role]
        role_environment = [
            "--env", "PHASE7_RESOURCE_REPLAY_V3=1",
            "--env", "PHASE7_BITCOIN_RPC_ENABLED=0",
            "--env", "PHASE7_ETHEREUM_RPC_ENABLED=0",
        ]
        if role == "collector":
            role_environment.extend([
                "--env", f"REPLAY_COLLECTOR_MAX_LIFETIME_SECONDS={MAX_COLLECTOR_SECONDS - 30}",
            ])
    return [
        "docker", "create", "--name", container_name,
        "--label", f"quant.owner={OWNER}",
        "--label", f"quant.run_id={run_id or container_name}",
        "--network", network_name,
        f"--memory={memory}", f"--memory-swap={memory}", "--cpus=1",
        "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
        "--log-driver", "local", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
        "--volume", f"{tests_mount}:/app/tests:ro",
        "--volume", f"{scripts_mount}:/app/scripts:ro",
        "--volume", f"{results_mount}:/replay-results:rw",
        "--env", "TRADING_MODE=paper", "--env", "TZ=UTC",
        "--env", f"POSTGRES_DSN={dsn}",
        *role_environment,
        "--env", "REPLAY_RESULTS_DIR=/replay-results",
        "--entrypoint", "python", image, *role_command,
    ]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _run(args: list[str], *, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False)
    if check and result.returncode != 0:
        diagnostic = _sanitize((result.stderr or result.stdout).splitlines()[-15:])
        raise RuntimeError(f"local replay command failed ({result.returncode}): {' '.join(args[:3])}; {diagnostic}")
    return result


def _sanitize(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    text = re.sub(r"(?i)(api-key|authorization|password|token)(\s*[:=]\s*)[^\s,]+", r"\1\2[REDACTED]", text)
    text = re.sub(r"(?:https?|wss?)://[^\s\"'<>]+", "[URL_REDACTED]", text)
    return text[:4_000]


def _docker_image_id(image: str) -> str:
    return _run(["docker", "image", "inspect", "--format", "{{.Id}}", image]).stdout.strip()


def _package_hashes(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for package in ("quant_phase1", "quant_phase2", "quant_phase3", "quant_phase4", "quant_phase5", "quant_phase6", "quant_phase7", "quant_phase8", "quant_data_layer"):
        module = importlib.import_module(package)
        package_root = Path(module.__file__).parent
        for path in sorted(package_root.rglob("*.py")):
            result[f"{package}/{path.relative_to(package_root).as_posix()}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted((root / "migrations").glob("*.sql")):
        result[f"migrations/{path.name}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _image_package_hashes(image: str) -> dict[str, str]:
    code = """import hashlib, importlib, json, pathlib
out = {}
for name in ('quant_phase1','quant_phase2','quant_phase3','quant_phase4','quant_phase5','quant_phase6','quant_phase7','quant_phase8','quant_data_layer'):
 root=pathlib.Path(importlib.import_module(name).__file__).parent
 for p in sorted(root.rglob('*.py')): out[name+'/'+p.relative_to(root).as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
for p in sorted(pathlib.Path('/app/migrations').glob('*.sql')): out['migrations/'+p.name]=hashlib.sha256(p.read_bytes()).hexdigest()
print(json.dumps(out,sort_keys=True,separators=(',',':')))
"""
    output = _run([
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=8m", "--entrypoint", "python", image, "-c", code,
    ], timeout=60).stdout
    return json.loads(output)


def _memory_bytes(value: str) -> int:
    match = re.search(r"([0-9.]+)(B|kB|KB|KiB|MB|MiB|GB|GiB)", value)
    if not match:
        return 0
    amount = float(match.group(1))
    factor = {"B": 1, "kB": 1000, "KB": 1000, "KiB": 1024, "MB": 10**6,
              "MiB": 1024**2, "GB": 10**9, "GiB": 1024**3}[match.group(2)]
    return int(amount * factor)


def _collector_shutdown_ready(role: str, results_path: Path, shutdown_requested: bool) -> bool:
    if role != "collector" or shutdown_requested:
        return False
    if not (results_path / "collector-replay-ready.json").is_file():
        return False
    phase7_marker = results_path / "phase7-resource-replay-ready.json"
    payload = None
    if phase7_marker.is_file():
        try:
            if phase7_marker.stat().st_size > 16 * 1024:
                return False
            payload = json.loads(phase7_marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
    if (
        isinstance(payload, dict)
        and payload.get("block_height") == 968_443
        and payload.get("event_count") == 12_665
        and payload.get("checkpoint_committed") is True
    ):
        return True
    failure_marker = results_path / "phase7-source-failure.json"
    try:
        if failure_marker.stat().st_size > 16 * 1024:
            return False
        failure = json.loads(failure_marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return _phase7_failure_message(failure) is not None


def _phase7_failure_message(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("component") != "bitcoin_rpc" or payload.get("status") != "ERROR":
        return None
    exception_type = payload.get("exception_type")
    failure_stage = payload.get("failure_stage")
    runtime_stage = payload.get("runtime_stage")
    if not isinstance(exception_type, str) or not re.fullmatch(r"[A-Z][A-Za-z0-9]{0,79}", exception_type):
        return None
    if not all(
        isinstance(stage, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", stage)
        for stage in (failure_stage, runtime_stage)
    ):
        return None
    return f"Phase 7 Bitcoin replay failed: {exception_type} at {failure_stage} (runtime={runtime_stage})"


def _engine_shutdown_ready(role: str, results_path: Path, shutdown_requested: bool) -> bool:
    return (
        role == "engine"
        and not shutdown_requested
        and (results_path / "engine-phase2-cycle.json").is_file()
    )


def _migrations_match(migrations: list[str], expected_count: int) -> bool:
    repair_marker = "009_phase4_metrics.repair.v1"
    if migrations.count(repair_marker) > 1:
        return False
    migration_files = [migration for migration in migrations if migration != repair_marker]
    return len(migration_files) == expected_count and all(
        migration.startswith(f"{expected:03d}_")
        for expected, migration in enumerate(migration_files, start=1)
    )


def _run_app_role(
    *, container_name: str, role: str, image: str, network_name: str, dsn: str,
    results_path: Path, run_id: str,
) -> dict[str, Any]:
    results_path.mkdir(parents=True, exist_ok=True)
    results_path.chmod(0o777)
    tests_mount = str(TESTS.resolve())
    scripts_mount = str((ROOT / "scripts").resolve())
    args = app_container_args(
        container_name=container_name,
        network_name=network_name,
        image=image,
        role=role,
        dsn=dsn,
        tests_mount=tests_mount,
        scripts_mount=scripts_mount,
        results_mount=str(results_path.resolve()),
        run_id=run_id,
    )
    created = False
    started_at = time.monotonic()
    samples: list[tuple[int, float]] = []
    shutdown_requested = False
    try:
        _run(args, timeout=60)
        created = True
        _run(["docker", "start", container_name])
        deadline_seconds = MAX_COLLECTOR_SECONDS if role == "collector" else MAX_ENGINE_SECONDS
        deadline = time.monotonic() + deadline_seconds
        state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            state_raw = _run(["docker", "inspect", "--format", "{{json .State}}", container_name]).stdout
            state = json.loads(state_raw)
            stats = _run([
                "docker", "stats", "--no-stream", "--format", "{{.MemUsage}};{{.CPUPerc}}", container_name,
            ], check=False)
            if stats.returncode == 0 and stats.stdout.strip():
                memory_text, _, cpu_text = stats.stdout.strip().partition(";")
                used_text = memory_text.partition(" / ")[0]
                cpu_match = re.search(r"([0-9.]+)%", cpu_text)
                samples.append((_memory_bytes(used_text), float(cpu_match.group(1)) if cpu_match else 0.0))
            if not state.get("Running"):
                break
            if (
                _collector_shutdown_ready(role, results_path, shutdown_requested)
                or _engine_shutdown_ready(role, results_path, shutdown_requested)
            ):
                _run(["docker", "kill", "--signal=SIGTERM", container_name], timeout=15)
                shutdown_requested = True
            time.sleep(1)
        else:
            _run(["docker", "stop", "--time", "10", container_name], check=False, timeout=15)
            raise TimeoutError(f"bounded {role} replay exceeded {deadline_seconds}s")
        exit_code = state.get("ExitCode")
        if exit_code != 0 or state.get("OOMKilled"):
            logs = _run(["docker", "logs", "--tail", "30", container_name], check=False, timeout=20)
            raise RuntimeError(
                f"{role} replay failed: exit={exit_code}, oom={state.get('OOMKilled')}, "
                f"logs={_sanitize(logs.stdout + logs.stderr)}"
            )
        total_bytes = sum(path.stat().st_size for path in results_path.rglob("*") if path.is_file())
        if total_bytes > MAX_RESULT_ARTIFACT_BYTES:
            raise RuntimeError(f"{role} replay result markers exceeded their bounded artifact budget")
        return {
            "role": role,
            "exit_code": exit_code,
            "oom_killed": bool(state.get("OOMKilled")),
            "duration_seconds": round(time.monotonic() - started_at, 3),
            "peak_memory_bytes_observed": max((memory for memory, _ in samples), default=None),
            "peak_cpu_percent_observed": max((cpu for _, cpu in samples), default=None),
            "sample_count": len(samples),
            "result_marker_bytes": total_bytes,
        }
    finally:
        if created:
            _remove_owned_container(container_name, run_id)


def _wait_for_postgres(container_name: str, timeout_seconds: int = 120) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state_raw = _run(["docker", "inspect", "--format", "{{json .State}}", container_name], check=False)
        if state_raw.returncode == 0:
            state = json.loads(state_raw.stdout)
            health = state.get("Health", {}).get("Status")
            if health == "healthy":
                return
            if state.get("Status") in {"exited", "dead"}:
                raise RuntimeError("disposable replay PostgreSQL exited before becoming healthy")
        time.sleep(1)
    raise TimeoutError("disposable replay PostgreSQL did not become healthy within 120 seconds")




def _phase8_replay(dsn: str) -> dict[str, Any]:
    import psycopg
    from quant_phase8.persistence import Phase8Repository

    module_spec = importlib.util.spec_from_file_location("phase8_replay_support", TESTS / "test_phase8_replay.py")
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError("Phase 8 deterministic replay support could not be loaded")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    module_spec.loader.exec_module(module)
    settings = module._settings()
    with psycopg.connect(dsn, autocommit=True) as connection:
        repository = Phase8Repository(connection, settings=settings)
        first = module._replay_once(repository, settings)
        counts_first, digest_first = module._persisted_counts_and_digest(connection)
        second = module._replay_once(repository, settings)
        counts_second, digest_second = module._persisted_counts_and_digest(connection)
        if counts_second != counts_first or digest_second != digest_first:
            raise RuntimeError("Phase 8 persistence replay is not idempotent")
        context_statuses = {
            key: {name: metric.status.value for name, metric in snapshot.metrics.items()}
            for key, snapshot in second["contexts"].items()
        }
    return {
        "persisted_counts": counts_first,
        "persisted_sha256": digest_first,
        "selected_ticker_count": len(first["selected"]),
        "context_statuses": context_statuses,
        "idempotency_pass": True,
    }


def _database_snapshot(dsn: str) -> dict[str, Any]:
    import psycopg
    from psycopg import sql

    with psycopg.connect(dsn) as connection:
        current_database = connection.execute("SELECT current_database()").fetchone()[0]
        timezone_name = connection.execute("SHOW timezone").fetchone()[0]
        postgres_version = connection.execute("SHOW server_version").fetchone()[0]
        database_size = connection.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        table_names = [row[0] for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).fetchall()]
        counts = {
            name: connection.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name))).fetchone()[0]
            for name in table_names
        }
        stable_hashes: dict[str, str] = {}
        for name in table_names:
            if name in VOLATILE_TABLE_NAMES or not any(token in name for token in HASH_TABLE_PATTERNS):
                continue
            digest = hashlib.sha256()
            query = sql.SQL(
                "SELECT (to_jsonb(row_value) - ARRAY['created_at','updated_at'])::text "
                "FROM {} AS row_value ORDER BY 1"
            ).format(sql.Identifier(name))
            with connection.cursor(name=f"replay_hash_{len(stable_hashes)}") as cursor:
                cursor.execute(query)
                while rows := cursor.fetchmany(512):
                    for (serialized,) in rows:
                        digest.update(serialized.encode("utf-8"))
                        digest.update(b"\n")
            stable_hashes[name] = digest.hexdigest()
        migrations = connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        btc_block_count = connection.execute(
            "SELECT count(*) FROM phase7_onchain_transfer_events WHERE chain='BITCOIN' AND block_number=968443"
        ).fetchone()[0]
        chain_counts = connection.execute(
            "SELECT chain,count(*) FROM phase7_onchain_transfer_events GROUP BY chain ORDER BY chain"
        ).fetchall()
        cursors = connection.execute(
            "SELECT source_id,scope_kind,scope_key,cursor_value,status FROM phase7_ingestion_checkpoints "
            "ORDER BY source_id,scope_kind,scope_key"
        ).fetchall()
    return {
        "database": current_database,
        "timezone": timezone_name,
        "postgres_version": postgres_version,
        "database_size_bytes": database_size,
        "table_counts": counts,
        "stable_table_sha256": stable_hashes,
        "migration_versions": [str(row[0]) for row in migrations],
        "bitcoin_large_block_event_count": btc_block_count,
        "chain_event_counts": {str(chain): int(count) for chain, count in chain_counts},
        "cursors": [list(row) for row in cursors],
    }


def _read_markers(path: Path) -> dict[str, Any]:
    markers: dict[str, Any] = {}
    for item in sorted(path.glob("*.json")):
        if item.stat().st_size > 128 * 1024:
            raise RuntimeError("replay readiness marker exceeded its 128 KiB bound")
        markers[item.name] = json.loads(item.read_text(encoding="utf-8"))
    return markers


def _run_one(index: int, *, app_image: str, postgres_image: str, network_name: str, run_id: str,
             results_root: Path, dataset: Any) -> dict[str, Any]:
    container_name = f"quant-dlr1-{run_id}-pg-{index}"
    _run(network_create_args(network_name=network_name))
    container_created = False
    try:
        _run(postgres_container_args(
            container_name=container_name, network_name=network_name, image=postgres_image, run_id=run_id,
        ), timeout=60)
        container_created = True
        _run(["docker", "start", container_name])
        _wait_for_postgres(container_name)
        app_dsn = "postgresql://quant@quant-postgres:5432/quant"
        role_results: dict[str, Any] = {}
        role_markers: dict[str, Any] = {}
        for role in ("collector", "engine", "phase8"):
            role_name = f"quant-dlr1-{run_id}-{index}-{role}"
            marker_dir = results_root / f"run-{index}" / role
            role_results[role] = _run_app_role(
                container_name=role_name,
                role=role,
                image=app_image,
                network_name=network_name,
                dsn=app_dsn,
                results_path=marker_dir,
                run_id=run_id,
            )
            role_markers[role] = _read_markers(marker_dir)
            _remove_owned_container(role_name, run_id)
            if role == "collector" and "phase7-resource-replay-ready.json" not in role_markers[role]:
                failure_message = _phase7_failure_message(
                    role_markers[role].get("phase7-source-failure.json")
                )
                if failure_message is not None:
                    raise RuntimeError(failure_message)

        replay_marker = role_markers["phase8"].get("phase8-db-replay.json")
        if not isinstance(replay_marker, dict):
            raise RuntimeError("Phase 8 replay container did not produce its bounded result marker")
        phase8 = replay_marker.get("phase8")
        snapshot = replay_marker.get("database")
        if not isinstance(phase8, dict) or not isinstance(snapshot, dict):
            raise RuntimeError("Phase 8 replay result marker is incomplete")
        if snapshot["database"] != "quant" or snapshot["timezone"].upper() != "UTC":
            raise RuntimeError("replay database identity or UTC timezone contract failed")
        if not snapshot["postgres_version"].startswith(dataset.manifest.postgres_server_version):
            raise RuntimeError("runtime PostgreSQL server version differs from the frozen manifest")
        if not _migrations_match(snapshot["migration_versions"], dataset.manifest.migration_version):
            raise RuntimeError(
                "replay database migration filenames differ from the frozen manifest: "
                f"expected_count={dataset.manifest.migration_version}, "
                f"actual={snapshot['migration_versions']!r}"
            )
        if snapshot["bitcoin_large_block_event_count"] != 12_665:
            raise RuntimeError(
                "Bitcoin block replay did not persist exactly 12,665 canonical events: "
                f"actual={snapshot['bitcoin_large_block_event_count']}, "
                f"chain_event_counts={snapshot['chain_event_counts']}, "
                f"cursors={snapshot['cursors']}"
            )
        if phase8["persisted_counts"].get("contexts") != 2 or not phase8["idempotency_pass"]:
            raise RuntimeError("Phase 8 replay did not persist both contexts idempotently")
        return {
            "run": index,
            "started_at_utc": _utc_now(),
            "collector": role_results["collector"],
            "engine": role_results["engine"],
            "phase8_role": role_results["phase8"],
            "collector_markers": role_markers["collector"],
            "engine_markers": role_markers["engine"],
            "phase8": phase8,
            "database": snapshot,
        }
    finally:
        if container_created:
            _remove_owned_container(container_name, run_id)
        _remove_owned_network(network_name)


def _remove_owned_container(name: str, run_id: str) -> None:
    inspect = _run(["docker", "inspect", name], check=False)
    if inspect.returncode != 0:
        return
    value = json.loads(inspect.stdout)[0]
    labels = value.get("Config", {}).get("Labels", {})
    if labels.get("quant.owner") != OWNER or labels.get("quant.run_id") != run_id:
        raise RuntimeError("refusing to remove a container not owned by this exact replay run")
    if value.get("State", {}).get("Running"):
        _run(["docker", "stop", "--time", "10", name], check=False, timeout=15)
    _run(["docker", "rm", "-v", name], check=False)


def _remove_owned_network(name: str) -> None:
    inspect = _run(["docker", "network", "inspect", name], check=False)
    if inspect.returncode != 0:
        return
    value = json.loads(inspect.stdout)[0]
    if (value.get("Labels") or {}).get("quant.owner") != OWNER:
        raise RuntimeError("refusing to remove a Docker network not owned by the replay harness")
    removed = _run(["docker", "network", "rm", name], check=False)
    if removed.returncode != 0:
        raise RuntimeError("owned disposable replay network could not be removed")


def _storage_snapshot() -> dict[str, Any]:
    disk = shutil.disk_usage("/")
    df = _run(["df", "-P", "/"]).stdout.splitlines()[-1].split()
    inode = _run(["df", "-Pi", "/"]).stdout.splitlines()[-1].split()
    docker_df = _run(["docker", "system", "df"], check=False).stdout
    return {
        "filesystem_total_bytes": disk.total,
        "filesystem_used_bytes": disk.used,
        "filesystem_free_bytes": disk.free,
        "filesystem_free_ratio": round(disk.free / disk.total, 4),
        "df_filesystem": df[0],
        "inode_use_percent": inode[4],
        "docker_system_df": docker_df,
    }


def _format_report(manifest: Any, outcomes: list[dict[str, Any]], comparison: Any,
                   storage_before: dict[str, Any], storage_after: dict[str, Any]) -> str:
    rows = []
    semantic_runs = []
    for outcome in outcomes:
        db = outcome["database"]
        rows.append(
            f"| {outcome['run']} | {outcome['collector']['duration_seconds']} | "
            f"{outcome['collector']['peak_memory_bytes_observed']} | "
            f"{outcome['engine']['peak_memory_bytes_observed']} | "
            f"{outcome['phase8_role']['peak_memory_bytes_observed']} | "
            f"{db['database_size_bytes']} | {sum(db['table_counts'].values())} | "
            f"{db['bitcoin_large_block_event_count']} | {outcome['phase8']['persisted_counts']['contexts']} |"
        )
        semantic_runs.append({
            "run": outcome["run"],
            "table_counts": db["table_counts"],
            "stable_table_sha256": db["stable_table_sha256"],
            "migrations": db["migration_versions"],
            "chain_event_counts": db["chain_event_counts"],
            "cursors": db["cursors"],
            "phase8": outcome["phase8"],
            "collector_ready_marker": outcome["collector_markers"].get("collector-replay-ready.json", {}),
            "engine_phase2_marker": outcome["engine_markers"].get("engine-phase2-cycle.json", {}),
        })
    semantics_json = json.dumps(semantic_runs, sort_keys=True, separators=(",", ":"))
    return "\n".join((
        "# DATA LAYER V1 DETERMINISTIC REPLAY REPORT",
        "",
        f"- Status: `{'PASS' if comparison.equal else 'FAIL'}`",
        f"- Replay version: `{manifest.replay_version}`",
        f"- Source Git SHA: `{manifest.git_sha}`",
        f"- Collector / Engine image digest: `{manifest.collector_image_digest}` / `{manifest.engine_image_digest}`",
        f"- PostgreSQL image digest: `{manifest.postgres_image_digest}`",
        f"- PostgreSQL server version: `{manifest.postgres_server_version}`",
        f"- Migration version: `{manifest.migration_version}`",
        f"- Dataset SHA-256: `{manifest.dataset_sha256}`",
        f"- Fixture: `{manifest.fixture.record_count}` rows; {manifest.fixture.compressed_bytes} compressed bytes; "
        f"{manifest.fixture.uncompressed_bytes} uncompressed bytes",
        f"- Limits: 2 MiB compressed, 16 MiB uncompressed, 20,000 records",
        f"- Required deterministic comparison SHA-256: `{comparison.reference_sha256}`",
        f"- Outcomes identical: `{str(comparison.equal).lower()}`",
        f"- Difference paths: `{', '.join(comparison.differences) if comparison.differences else 'none'}`",
        "- Network policy: internal Docker network; external egress unavailable; no real provider calls",
        "- Trading mode: paper; private API and order routes disabled",
        "",
        "## Three isolated repetitions",
        "",
        "| Run | Collector seconds | Collector observed peak bytes | Engine observed peak bytes | Phase 8 helper observed peak bytes | Database bytes | Total table rows | BTC large-block events | Phase 8 contexts |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        *rows,
        "",
        "## Compared semantic data",
        "",
        "The compact JSON below contains row counts, stable table digests, migrations, chain cursors, Stage 1-related table counts, the Phase 2 cycle marker, and Phase 8 context quality states. It contains no raw market payloads or container logs.",
        "",
        "```json",
        semantics_json,
        "```",
        "",
        "## Measured storage guard",
        "",
        f"- Before: {storage_before['filesystem_free_ratio']:.1%} free; {storage_before['filesystem_free_bytes']} bytes available.",
        f"- After: {storage_after['filesystem_free_ratio']:.1%} free; {storage_after['filesystem_free_bytes']} bytes available.",
        f"- Inode use: {storage_after['inode_use_percent']}.",
        "- PostgreSQL used a 768 MiB-capped disposable container with tmpfs; no named data volume was created.",
        "- Docker system usage was recorded; no prune, image removal, or broad cleanup was performed.",
        "",
        "## Interpretation and limits",
        "",
        "- Fixture values are synthetic test inputs, not live market or provider evidence.",
        "- Collector/Engine memory values are observed short-replay peaks under the unchanged 256/384 MiB caps; they are not sustained resource acceptance.",
        "- Database size is measured per fresh replay database; it is not extrapolated into long-term retention growth.",
        "- Numeric admission weights/slots and sustained resource thresholds remain provisional unless separately justified by representative saturation and runtime gates.",
        "- Migrations 001–015 were used; no migration was added.",
        "",
    ))


def _assert_registered_stream_coverage(dataset: Any) -> None:
    from quant_data_layer.backpressure import STREAM_CONTRACTS

    required = {
        (int(contract.phase.value.removeprefix("phase")), contract.stream_id)
        for contract in STREAM_CONTRACTS
        if contract.phase is not None
    }
    covered = {(record.phase, record.stream_id) for record in dataset.records}
    if required - covered:
        raise RuntimeError("frozen fixture does not cover the current registered Phase 1–8 stream inventory")
    bitcoin_events = sum(
        record.phase == 7
        and record.payload.get("chain") == "BITCOIN"
        and record.payload.get("block_height") == 968_443
        for record in dataset.records
    )
    if bitcoin_events != 12_665:
        raise RuntimeError("frozen fixture does not contain the required 12,665-event Bitcoin block")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-image", required=True)
    parser.add_argument("--postgres-image", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "DATA_LAYER_V1_REPLAY_REPORT.md")
    args = parser.parse_args()
    from quant_data_layer.replay import compare_replay_outcomes, load_replay_dataset

    storage_before = _storage_snapshot()
    if storage_before["filesystem_free_ratio"] < 0.15:
        raise SystemExit("WSL_STORAGE_PRESSURE=true; replay was not started")
    dataset = load_replay_dataset(MANIFEST_PATH, project_root=ROOT)
    _assert_registered_stream_coverage(dataset)
    head = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
    if head != dataset.manifest.git_sha:
        raise SystemExit("replay manifest Git SHA does not match the checked-out source checkpoint")
    app_digest = _docker_image_id(args.app_image)
    postgres_digest = _docker_image_id(args.postgres_image)
    if app_digest != dataset.manifest.collector_image_digest or app_digest != dataset.manifest.engine_image_digest:
        raise SystemExit("application image digest differs from the frozen replay manifest")
    if postgres_digest != dataset.manifest.postgres_image_digest:
        raise SystemExit("PostgreSQL image digest differs from the frozen replay manifest")
    if _package_hashes(ROOT) != _image_package_hashes(args.app_image):
        raise SystemExit("application image source files do not match the current pinned project source")
    run_id = uuid4().hex[:12]
    storage_root = Path(tempfile.mkdtemp(prefix=f"quant-dlr1-{run_id}-"))
    storage_root.chmod(0o700)
    if storage_root.resolve().parent != Path(tempfile.gettempdir()).resolve():
        raise SystemExit("temporary replay evidence path failed its containment check")
    outcomes: list[dict[str, Any]] = []
    try:
        for index in range(1, 4):
            network_name = f"quant-dlr1-{run_id}-net-{index}"
            outcomes.append(_run_one(
                index,
                app_image=args.app_image,
                postgres_image=args.postgres_image,
                network_name=network_name,
                run_id=run_id,
                results_root=storage_root,
                dataset=dataset,
            ))
    finally:
        shutil.rmtree(storage_root)
    comparable = [
        {
            "table_counts": item["database"]["table_counts"],
            "stable_table_sha256": item["database"]["stable_table_sha256"],
            "migration_versions": item["database"]["migration_versions"],
            "bitcoin_large_block_event_count": item["database"]["bitcoin_large_block_event_count"],
            "chain_event_counts": item["database"]["chain_event_counts"],
            "cursors": item["database"]["cursors"],
            "phase8": {
                "counts": item["phase8"]["persisted_counts"],
                "hash": item["phase8"]["persisted_sha256"],
                "contexts": item["phase8"]["context_statuses"],
                "idempotent": item["phase8"]["idempotency_pass"],
            },
        }
        for item in outcomes
    ]
    comparison = compare_replay_outcomes(comparable)
    storage_after = _storage_snapshot()
    report = _format_report(dataset.manifest, outcomes, comparison, storage_before, storage_after)
    args.output.write_text(report, encoding="utf-8")
    print(json.dumps({
        "status": "PASS" if comparison.equal else "FAIL",
        "runs": len(outcomes),
        "outcome_sha256": comparison.reference_sha256,
        "differences": comparison.differences,
        "report": str(args.output),
        "storage_free_before_ratio": storage_before["filesystem_free_ratio"],
        "storage_free_after_ratio": storage_after["filesystem_free_ratio"],
    }, sort_keys=True, separators=(",", ":")))
    return 0 if comparison.equal else 1


if __name__ == "__main__":
    raise SystemExit(main())
