"""Bounded local secret and private-payload scan for Phase 9 evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|secret|password|access[_-]?token|refresh[_-]?token|authorization)\b"
    r"\s*[:=]\s*['\"]?([A-Za-z0-9_./+=:-]{16,})"
)
_BEARER = re.compile(r"(?i)\bauthorization\s*[:=]\s*['\"]?bearer\s+([A-Za-z0-9_.-]{16,})")
_CREDENTIAL_URL = re.compile(r"(?i)(?:https?|wss?|postgresql)://[^\s/@:]+:([^\s/@]+)@([^\s/:]+)")
_PRIVATE_PAYLOAD = re.compile(
    r'(?i)["\'](?:account_id|private_account|position_payload|order_payload)["\']\s*:\s*'
    r'["\']?[A-Za-z0-9_-]{8,}'
)
_PLACEHOLDERS = ("example", "placeholder", "dummy", "fixture", "redacted", "changeme", "not-configured")
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_FILES = 5000


def scan_text(text: str, *, synthetic: bool = False, private_context: bool = False) -> tuple[str, ...]:
    """Return categories only; matched values are never retained or logged."""
    findings = []
    for match in _ASSIGNMENT.finditer(text):
        value = match.group(1).lower()
        if synthetic and value.startswith(("http://", "https://", "wss://", "postgresql://")):
            continue
        if not any(marker in value for marker in _PLACEHOLDERS):
            findings.append("CREDENTIAL_ASSIGNMENT")
    for match in _BEARER.finditer(text):
        if not any(marker in match.group(1).lower() for marker in _PLACEHOLDERS):
            findings.append("AUTHORIZATION_BEARER")
    for match in _CREDENTIAL_URL.finditer(text):
        password, host = match.groups()
        if synthetic and (host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".invalid")):
            continue
        if not any(marker in password.lower() for marker in _PLACEHOLDERS):
            findings.append("CREDENTIAL_URL")
    if private_context and _PRIVATE_PAYLOAD.search(text):
        findings.append("PRIVATE_ACCOUNT_ORDER_PAYLOAD")
    return tuple(sorted(set(findings)))


def _selected(path: Path) -> bool:
    value = path.as_posix()
    return (
        value.startswith("src/quant_phase9/")
        or value.startswith(('src/quant_execution/','src/quant_nautilus/','src/quant_features/','src/quant_research/'))
        or (value.startswith("scripts/") and "phase9" in value)
        or value.startswith("tests/quant_phase9/")
        or value.startswith(('tests/quant_execution/','tests/quant_nautilus/','tests/quant_research/'))
        or value.startswith("tests/fixtures/phase9/")
        or value.startswith("policies/phase9")
        or value.startswith("artifacts/phase9/")
        or value == "PHASE_9_IMPLEMENTATION_REPORT.md"
        or value.startswith('docs/NAUTILUS_')
        or (value.startswith("docs/superpowers/") and "phase9" in value)
        or (value.startswith("src/quant_phase6/") and "prompt" in value)
    )


def scan_repository(root: Path) -> dict[str, object]:
    root = root.resolve()
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"],
        capture_output=True, check=True, timeout=30,
    ).stdout.decode("utf-8").split("\0")
    paths = {Path(name) for name in tracked if name and _selected(Path(name))}
    artifacts = root / "artifacts/phase9"
    if artifacts.is_dir():
        paths.update(path.relative_to(root) for path in artifacts.rglob("*") if path.is_file())
    if len(paths) > _MAX_FILES:
        raise ValueError("secret scan file inventory exceeds bound")
    findings: list[dict[str, object]] = []
    for relative in sorted(paths):
        path = (root / relative).resolve()
        if root not in path.parents or not path.is_file():
            raise ValueError("secret scan path escapes repository or is unavailable")
        raw = path.read_bytes()
        if len(raw) > _MAX_FILE_BYTES:
            raise ValueError("secret scan file exceeds byte bound")
        if b"\0" in raw:
            continue
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("secret scan text artifact is invalid UTF-8") from exc
        synthetic = relative.as_posix().startswith("tests/")
        private_context = any(part in {"logs", "prompts"} for part in relative.parts)
        for number, line in enumerate(content.splitlines(), 1):
            for category in scan_text(line, synthetic=synthetic,
                                      private_context=private_context):
                findings.append({"path": relative.as_posix(), "line": number,
                                 "category": category})
    diff = subprocess.run(
        ["git", "-C", str(root), "diff", "--unified=0", "HEAD", "--",
         'src/quant_execution','src/quant_nautilus','src/quant_features','src/quant_research',
         'tests/quant_execution','tests/quant_nautilus','tests/quant_research','docs/NAUTILUS_*',
         "src/quant_phase9", "scripts/run_phase9_acceptance.py",
         "scripts/run_phase9_replay_v1.py", "scripts/run_phase9_test_matrix.py",
         "scripts/phase9_secret_scan.py", "tests/quant_phase9", "policies",
         "PHASE_9_IMPLEMENTATION_REPORT.md"],
        capture_output=True, check=True, timeout=30,
    ).stdout.decode("utf-8")
    diff_path = ""
    for number, line in enumerate(diff.splitlines(), 1):
        if line.startswith("+++ b/"):
            diff_path = line[6:]
            continue
        if line.startswith("+") and not line.startswith("+++"):
            for category in scan_text(
                line[1:], synthetic=diff_path.startswith("tests/"),
                private_context=any(part in {"logs", "prompts"} for part in Path(diff_path).parts),
            ):
                findings.append({"path": "tracked-diff", "line": number,
                                 "category": category})
    return {
        "schema": "PHASE9_SECRET_SCAN_V1", "passed": not findings,
        "secret_leak_found": bool(findings), "findings_count": len(findings),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--require-pass", action="store_true")
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    result = scan_repository(args.root)
    payload = json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
