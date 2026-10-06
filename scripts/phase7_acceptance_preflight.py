#!/usr/bin/env python3
"""Build and validate the local Phase 7 acceptance image without running it."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.phase7-acceptance.yml"
ENV_FILE = ROOT / ".env.local"
EXPECTED_SERVICES = {"postgres", "quant-collector", "quant-engine"}
EXPECTED_MEMORY_LIMITS = ("mem_limit: 768m", "mem_limit: 256m", "mem_limit: 384m")
BUILD_INPUTS = ("Dockerfile", "pyproject.toml", "README.md")


class GateFailure(RuntimeError):
    """A safe-to-report local preflight failure without subprocess output."""


def _run(args: list[str], *, env: dict[str, str] | None = None, check: bool = True) -> str:
    result = subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise GateFailure("a required preflight command failed; captured details were suppressed")
    return result.stdout.strip()


def _compose_command() -> list[str]:
    result = subprocess.run(
        ["docker", "compose", "version"], cwd=ROOT, check=False,
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    raise GateFailure("neither docker compose nor docker-compose is available")


def _source_fingerprint() -> str:
    inputs = list(BUILD_INPUTS)
    for directory in ("src", "migrations", "config"):
        inputs.extend(
            path.relative_to(ROOT).as_posix()
            for path in sorted((ROOT / directory).rglob("*"))
            if path.is_file()
        )
    digest = hashlib.sha256()
    for relative in sorted(set(inputs)):
        path = ROOT / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def main() -> int:
    try:
        if _run(["git", "branch", "--show-current"]) != "phase7":
            raise GateFailure("preflight must run on branch phase7")
        revision = _run(["git", "rev-parse", "HEAD"])
        short_revision = revision[:9]
        env_status = _run(["git", "status", "--porcelain", "--", ".env.local"])
        if env_status or not ENV_FILE.is_file():
            raise GateFailure(".env.local must exist locally and remain untracked")
        _run(["git", "check-ignore", "--quiet", ".env.local"])

        compose = _compose_command()
        compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
        if any(limit not in compose_text for limit in EXPECTED_MEMORY_LIMITS):
            raise GateFailure("acceptance Compose memory limits do not match the approved contract")
        if "ports:" in compose_text:
            raise GateFailure("acceptance Compose must not publish container ports")
        if "PHASE7_BITCOIN_RPC_MAX_RESPONSE_BYTES" not in compose_text:
            raise GateFailure("Bitcoin response budget is not wired into acceptance Compose")
        if "PHASE7_ACCEPTANCE_IMAGE" not in compose_text:
            raise GateFailure("acceptance Compose image variable is missing")
        if "PHASE7_ACCEPTANCE_DIAGNOSTICS_PATH: /tmp/phase7-collector-diagnostics.json" not in compose_text:
            raise GateFailure("collector diagnostics snapshot is not enabled for acceptance")

        fingerprint = _source_fingerprint()
        status = _run(["git", "status", "--porcelain"])
        tree_state = "dirty" if status else "clean"
        image_tag = f"quant-phase7:phase7-{short_revision}-{tree_state}-{fingerprint[:12]}"
        child_env = os.environ.copy()
        child_env["PHASE7_ACCEPTANCE_IMAGE"] = image_tag
        compose_base = compose + ["--env-file", str(ENV_FILE), "-f", str(COMPOSE_FILE)]

        _run(compose_base + ["config", "--quiet"], env=child_env)
        services = set(_run(compose_base + ["config", "--services"], env=child_env).splitlines())
        if services != EXPECTED_SERVICES:
            raise GateFailure("acceptance Compose service set differs from the approved contract")

        _run([
            "docker", "build", "--file", "Dockerfile", "--tag", image_tag,
            "--label", f"org.opencontainers.image.revision={revision}",
            "--label", f"io.quant.source-tree-sha256={fingerprint}", ".",
        ])
        image_id = _run(["docker", "image", "inspect", "--format", "{{.Id}}", image_tag])

        print("COMPOSE_COMMAND=" + " ".join(compose))
        print("COMPOSE_CONFIG=PASS")
        print("COMPOSE_SERVICES=" + ",".join(sorted(services)))
        print("GIT_BRANCH=phase7")
        print("GIT_HEAD=" + revision)
        print("IMAGE_TAG=" + image_tag)
        print("IMAGE_ID=" + image_id)
        print("IMAGE_SOURCE_SHA256=" + fingerprint)
        print("RUNTIME_CONTAINERS_STARTED=NO")
        return 0
    except (GateFailure, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, GateFailure) else "local preflight could not complete"
        print("PHASE7_ACCEPTANCE_PREFLIGHT_BLOCKED=" + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
