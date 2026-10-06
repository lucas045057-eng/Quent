#!/usr/bin/env python3
"""Audit Phase 7 acceptance artifacts without ever printing configured secrets."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
BASELINE = "c1accd8934100fac9f6603ad25220d6879cefc7c"
PROJECT = "phase7-acc-fbdda1e-20260925"
COMPOSE = ROOT / "docker-compose.phase7-acceptance.yml"
ENV_FILE = ROOT / ".env.local"
IMAGE = "quant-phase7:phase7-fbdda1ed3-clean-1bde41b028e5"
SENSITIVE_NAME = re.compile(r"(?:API_KEY|PASSWORD|SECRET|TOKEN|_RPC_URL)$", re.IGNORECASE)
SECRET_PATTERNS = (
    re.compile(rb"(?i)\bapi-key\s*[:=]\s*[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(rb"(?i)\bauthorization\s*[:=]\s*(?:bearer|basic)\s+\S{12,}"),
    re.compile(rb"(?i)https?://[^/@\s:]+:[^/@\s]+@"),
)


def _capture(args: list[str], *, env: dict[str, str] | None = None) -> bytes:
    result = subprocess.run(args, cwd=ROOT, env=env, check=False, capture_output=True)
    if result.returncode:
        raise RuntimeError("secret audit input could not be collected")
    return result.stdout


def _configured_secret_values() -> list[bytes]:
    values: list[bytes] = []
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if not separator or not SENSITIVE_NAME.search(name.strip()):
            continue
        value = value.strip().strip('"').strip("'")
        if len(value) >= 8:
            values.append(value.encode("utf-8"))
    return values


def main() -> int:
    try:
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", ".env.local"], cwd=ROOT, check=False,
        ).returncode == 0
        tracked_env = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".env.local"],
            cwd=ROOT, check=False, capture_output=True,
        ).returncode == 0
        if not ENV_FILE.is_file() or not ignored or tracked_env:
            raise RuntimeError("local secret file ignore contract failed")

        paths = _capture(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"])
        surfaces: list[tuple[str, bytes]] = []
        for raw_path in paths.split(b"\0"):
            if not raw_path or raw_path == b".env.local":
                continue
            candidate = ROOT / os.fsdecode(raw_path)
            if candidate.is_file():
                surfaces.append((os.fsdecode(raw_path), candidate.read_bytes()))
        surfaces.append(("git-diff-from-baseline", _capture(["git", "diff", "--binary", BASELINE, "HEAD", "--"])))
        for service in ("postgres", "quant-collector", "quant-engine"):
            name = f"{PROJECT}_{service}_1"
            result = subprocess.run(["docker", "logs", name], check=False, capture_output=True)
            if result.returncode == 0:
                surfaces.append((f"container-log:{service}", result.stdout + result.stderr))
        for path in (
            Path("/tmp/phase7-short-runtime-samples.jsonl"),
            Path("/tmp/phase7-short-monitor-smoke.jsonl"),
        ):
            if path.is_file():
                surfaces.append((str(path), path.read_bytes()))

        # Validate rendered Compose in memory only; it is intentionally never
        # written to a file, printed, or included in the artifact scan.
        child_env = os.environ.copy()
        child_env["PHASE7_ACCEPTANCE_IMAGE"] = IMAGE
        rendered = subprocess.run(
            ["docker-compose", "--env-file", str(ENV_FILE), "-f", str(COMPOSE),
             "-p", PROJECT, "config"],
            cwd=ROOT, env=child_env, check=False, capture_output=True,
        )
        if rendered.returncode:
            raise RuntimeError("rendered Compose validation failed")

        configured_values = _configured_secret_values()
        exact_hits: set[str] = set()
        for value in configured_values:
            for label, surface in surfaces:
                if value in surface:
                    exact_hits.add(label)
        pattern_hits: set[str] = set()
        fixture_hits: set[str] = set()
        pattern_hit_locations: set[str] = set()
        fake_markers = (b"test", b"fake", b"example", b"placeholder", b"dummy", b"redacted")
        fake_test_contexts = (b"fake_endpoint", b"secret_redaction", b"auth_is_sent", b"auth_mode")
        for pattern in SECRET_PATTERNS:
            for label, surface in surfaces:
                for match in pattern.finditer(surface):
                    line_start = surface.rfind(b"\n", 0, match.start()) + 1
                    line_end = surface.find(b"\n", match.end())
                    if line_end < 0:
                        line_end = len(surface)
                    source_line = surface[line_start:line_end].lower()
                    preceding = surface[:line_start]
                    test_functions = re.findall(rb"(?m)^def (test_[A-Za-z0-9_]+)\(", preceding)
                    test_context = test_functions[-1].lower() if test_functions else b""
                    is_fixture = any(marker in source_line for marker in fake_markers)
                    is_fixture = is_fixture or any(marker in test_context for marker in fake_test_contexts)
                    if label.startswith("tests/") and is_fixture:
                        fixture_hits.add(label)
                    else:
                        pattern_hits.add(label)
                        pattern_hit_locations.add(f"{label}:{surface[:line_start].count(b'\n') + 1}")
        leaked = bool(exact_hits or pattern_hits)
        print("ENV_LOCAL=ignored-untracked")
        print("COMPOSE_RENDERED_CONFIG=validated-in-memory-not-emitted")
        print(f"AUDIT_SURFACES={len(surfaces)}")
        print(f"EXACT_CONFIGURED_SECRET_HITS={len(exact_hits)}")
        print("NON_FIXTURE_PATTERN_HIT_SOURCES=" + ",".join(sorted(pattern_hits)))
        print("NON_FIXTURE_PATTERN_HIT_LOCATIONS=" + ",".join(sorted(pattern_hit_locations)))
        print("SYNTHETIC_TEST_FIXTURE_PATTERN_SOURCES=" + ",".join(sorted(fixture_hits)))
        print(f"SECRET_LEAK_FOUND={'true' if leaked else 'false'}")
        return 1 if leaked else 0
    except (OSError, RuntimeError, UnicodeError):
        print("SECRET_AUDIT=INCOMPLETE")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
