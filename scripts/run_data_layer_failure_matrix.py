#!/usr/bin/env python3
"""Run three isolated, process-level Data Layer restart cycles."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
MANIFEST = TESTS / "fixtures/data_layer_replay_v1/manifest.json"
APP_IMAGE = "quant-data-layer-replay-v1:8cadf8d"
POSTGRES_IMAGE = "postgres:16.15-bookworm"
OWNER = "data-layer-v1-failure-matrix"
POSTGRES_MEMORY_LIMIT = "768m"
APP_MEMORY_LIMIT = "256m"
MAX_RUN_SECONDS = 360


def network_create_args(network_name: str, *, run_id: str | None = None) -> list[str]:
    return [
        "docker", "network", "create", "--internal", "--driver", "bridge",
        "--label", f"quant.owner={OWNER}", "--label", f"quant.run_id={run_id or network_name}",
        network_name,
    ]


def postgres_container_args(
    *, container_name: str, network_name: str, image: str, run_id: str | None = None,
) -> list[str]:
    identity = run_id or container_name
    return [
        "docker", "create", "--name", container_name,
        "--label", f"quant.owner={OWNER}", "--label", f"quant.run_id={identity}",
        "--network", network_name, "--network-alias", "quant-postgres",
        f"--memory={POSTGRES_MEMORY_LIMIT}", f"--memory-swap={POSTGRES_MEMORY_LIMIT}", "--cpus=1",
        "--tmpfs", "/var/lib/postgresql/data:rw,noexec,nosuid,size=512m",
        "--log-driver", "local", "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
        "--health-cmd", "pg_isready -U quant -d quant",
        "--health-interval", "2s", "--health-timeout", "2s", "--health-retries", "45",
        "--env", "POSTGRES_DB=quant", "--env", "POSTGRES_USER=quant",
        "--env", "POSTGRES_HOST_AUTH_METHOD=trust", "--env", "TZ=UTC",
        image, "postgres", "-c", "timezone=UTC",
    ]


def worker_container_args(
    *,
    container_name: str,
    network_name: str,
    image: str,
    tests_mount: str,
    cycle: int,
    run_id: str | None = None,
) -> list[str]:
    if cycle not in {1, 2, 3}:
        raise ValueError("failure matrix requires exactly three restart cycles")
    return [
        "docker", "create", "--name", container_name,
        "--label", f"quant.owner={OWNER}", "--label", f"quant.run_id={run_id or container_name}",
        "--network", network_name,
        f"--memory={APP_MEMORY_LIMIT}", f"--memory-swap={APP_MEMORY_LIMIT}", "--cpus=1",
        "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,size=32m",
        "--log-driver", "local", "--log-opt", "max-size=2m", "--log-opt", "max-file=2",
        "--mount", f"type=bind,src={tests_mount},dst=/app/tests,readonly",
        "--env", "TRADING_MODE=paper",
        "--env", "POSTGRES_DSN=postgresql://quant@quant-postgres:5432/quant",
        "--env", f"FAILURE_MATRIX_CYCLE={cycle}",
        "--env", "PYTHONUNBUFFERED=1",
        "--entrypoint", "python", image, "/app/tests/data_layer_restart_worker.py",
    ]


def validate_restart_cycles(cycles: list[dict[str, Any]]) -> dict[str, Any]:
    if len(cycles) != 3 or [row.get("cycle") for row in cycles] != [1, 2, 3]:
        raise ValueError("exactly three ordered restart cycles are required")
    previous_cursor = None
    for row in cycles:
        before = row.get("cursor_before")
        after = row.get("cursor_after")
        if isinstance(after, bool) or not isinstance(after, int):
            raise ValueError("cursor result must be an integer")
        if before is not None and (isinstance(before, bool) or not isinstance(before, int) or after <= before):
            raise ValueError("cursor regression or non-advancement detected")
        if previous_cursor is not None and before != previous_cursor:
            raise ValueError("durable cursor was not restored by the next process")
        if row.get("event_count") != row.get("distinct_event_count"):
            raise ValueError("canonical event identity duplicates detected")
        if row.get("migration_repeat_count") != 0:
            raise ValueError("migration repeat was not idempotent")
        expected_migrations = 15 if row["cycle"] == 1 else 0
        if row.get("migration_first_count") != expected_migrations:
            raise ValueError("migration first-run count changed after the fresh bootstrap")
        if row.get("ws_connect_attempts", 0) < 2 or row.get("ws_subscription_count") != 4:
            raise ValueError("WebSocket reconnect/resubscribe contract failed")
        if row.get("ws_ack_count") != 2 or not row.get("health_recovered"):
            raise ValueError("WebSocket subscription or health recovery failed")
        if not row.get("queue_clean"):
            raise ValueError("process-local queue or queue age did not reset")
        previous_cursor = after
    return {"status": "PASS", "cycle_count": 3, "final_cursor": previous_cursor}


def _run(args: list[str], *, timeout: int = 30, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False)
    if check and result.returncode:
        safe_command = " ".join(args[:3])
        safe_error = _safe_diagnostic(result.stderr or result.stdout or "no diagnostic")
        raise RuntimeError(f"owned local test resource command failed: {safe_command}; {safe_error}")
    return result


def _safe_diagnostic(value: str) -> str:
    text = re.sub(
        r"(?i)(api-key|authorization|password|token)(\s*[:=]\s*)[^\s,]+",
        r"\1\2[REDACTED]",
        value,
    )
    text = re.sub(r"(?:https?|wss?)://[^\s\"'<>]+", "[URL_REDACTED]", text)
    return "\n".join(text.splitlines()[-8:])[:1_000]


def _worker_failure_message(*, cycle: int, exit_code: str, logs: str) -> str:
    return (
        f"restart worker {cycle} exited with code {exit_code}; "
        f"bounded diagnostic: {_safe_diagnostic(logs or 'no container output')}"
    )


def _combine_output_streams(stdout: str, stderr: str) -> str:
    return "\n".join(value for value in (stdout, stderr) if value)


def _existing_container_names() -> set[str]:
    output = _run(["docker", "ps", "-a", "--format", "{{.Names}}"])
    return set(output.stdout.splitlines())


def _existing_network_names() -> set[str]:
    output = _run(["docker", "network", "ls", "--format", "{{.Name}}"])
    return set(output.stdout.splitlines())


def _wait_postgres(container_name: str, timeout_seconds: int = 90) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = _run([
            "docker", "inspect", "--format",
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
            container_name,
        ]).stdout.strip()
        if state == "healthy":
            return
        if state in {"exited", "dead"}:
            raise RuntimeError("isolated PostgreSQL test container exited before becoming healthy")
        time.sleep(1)
    raise TimeoutError("isolated PostgreSQL was not healthy within 90 seconds")


def _container_identity(name: str) -> tuple[str | None, str | None]:
    result = _run([
        "docker", "inspect", "--format",
        '{{.Id}}|{{index .Config.Labels "quant.owner"}}', name,
    ], check=False)
    if result.returncode:
        return None, None
    fields = result.stdout.strip().split("|", 1)
    return (fields[0], fields[1]) if len(fields) == 2 else (None, None)


def _remove_owned_container(name: str, expected_id: str) -> None:
    actual_id, owner = _container_identity(name)
    if actual_id is None:
        return
    if actual_id != expected_id or owner != OWNER:
        raise RuntimeError("cleanup refused: container identity or owner label changed")
    _run(["docker", "rm", "-f", name])


def _remove_owned_network(name: str, expected_id: str) -> None:
    result = _run([
        "docker", "network", "inspect", "--format",
        '{{.Id}}|{{index .Labels "quant.owner"}}', name,
    ], check=False)
    if result.returncode:
        return
    fields = result.stdout.strip().split("|", 1)
    if len(fields) != 2 or fields[0] != expected_id or fields[1] != OWNER:
        raise RuntimeError("cleanup refused: network identity or owner label changed")
    _run(["docker", "network", "rm", name])


def _verify_pinned_inputs() -> dict[str, str]:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    app_id = _run(["docker", "image", "inspect", "--format", "{{.Id}}", APP_IMAGE]).stdout.strip()
    postgres_id = _run(["docker", "image", "inspect", "--format", "{{.Id}}", POSTGRES_IMAGE]).stdout.strip()
    if app_id != manifest["collector_image_digest"] or app_id != manifest["engine_image_digest"]:
        raise RuntimeError("pinned application image differs from the Task 6 replay checkpoint")
    if postgres_id != manifest["postgres_image_digest"]:
        raise RuntimeError("pinned PostgreSQL image differs from the Task 6 replay checkpoint")
    _package_hashes, _image_package_hashes = _load_replay_helpers()

    if _image_package_hashes(APP_IMAGE) != _package_hashes(ROOT):
        raise RuntimeError("application image package does not match the checked-out source")
    return {"app_image_id": app_id, "postgres_image_id": postgres_id}


def _load_replay_helpers():
    # Executing this file by path places scripts/ rather than the repository
    # root on sys.path. Resolve the sibling project package explicitly.
    root_text = str(ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    from scripts.run_data_layer_replay_v1 import _image_package_hashes, _package_hashes

    return _package_hashes, _image_package_hashes


def run_restart_cycles() -> dict[str, Any]:
    disk = shutil.disk_usage("/")
    if disk.free / disk.total < 0.15:
        raise RuntimeError("WSL storage headroom is below the 15% safety floor")
    images = _verify_pinned_inputs()
    run_id = uuid4().hex[:10]
    network = f"quant-dlfm-{run_id}-net"
    postgres = f"quant-dlfm-{run_id}-pg"
    workers: dict[str, str] = {}
    if network in _existing_network_names() or postgres in _existing_container_names():
        raise RuntimeError("generated isolated test resource name already exists")

    network_id = _run(network_create_args(network, run_id=run_id)).stdout.strip()
    postgres_id = None
    try:
        postgres_id = _run(postgres_container_args(
            container_name=postgres, network_name=network, image=POSTGRES_IMAGE, run_id=run_id,
        )).stdout.strip()
        _run(["docker", "start", postgres])
        _wait_postgres(postgres)

        cycles: list[dict[str, Any]] = []
        for cycle in (1, 2, 3):
            worker = f"quant-dlfm-{run_id}-worker-{cycle}"
            if worker in _existing_container_names():
                raise RuntimeError("generated worker name already exists; refusing to reuse it")
            worker_id = _run(worker_container_args(
                container_name=worker, network_name=network, image=APP_IMAGE,
                tests_mount=str(TESTS), cycle=cycle, run_id=run_id,
            )).stdout.strip()
            workers[worker] = worker_id
            _run(["docker", "start", worker])
            wait_result = _run(["docker", "wait", worker], timeout=MAX_RUN_SECONDS)
            exit_code = wait_result.stdout.strip()
            log_result = _run(["docker", "logs", "--tail", "20", worker], check=False)
            logs = _combine_output_streams(log_result.stdout, log_result.stderr)
            if exit_code != "0":
                raise RuntimeError(_worker_failure_message(cycle=cycle, exit_code=exit_code, logs=logs))
            payload = next((line for line in reversed(logs.splitlines()) if line.startswith("{")), None)
            if payload is None:
                raise RuntimeError(f"restart worker {cycle} did not emit its bounded result record")
            summary = json.loads(payload)
            cycles.append(summary)
            _remove_owned_container(worker, worker_id)
            del workers[worker]

        disposition = validate_restart_cycles(cycles)
        return {
            **disposition,
            "cycles": cycles,
            "images": images,
            "postgres_memory_limit": POSTGRES_MEMORY_LIMIT,
            "collector_memory_limit": APP_MEMORY_LIMIT,
            "filesystem_free_bytes_before": disk.free,
            "external_network": "DISABLED_INTERNAL_NETWORK",
            "paper_mode": True,
        }
    finally:
        for worker_name, worker_id in list(workers.items()):
            _remove_owned_container(worker_name, worker_id)
        if postgres_id is not None:
            _remove_owned_container(postgres, postgres_id)
        _remove_owned_network(network, network_id)


def main() -> int:
    result = run_restart_cycles()
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
