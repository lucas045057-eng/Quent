"""Machine-verifiable compatibility closure for Phase 9 Task 0."""

from __future__ import annotations

import argparse
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from typing import Literal

from quant_phase9.canonical import canonical_bytes, canonical_sha256
from quant_phase9.contracts import GitSha, Sha256Hex


SUITE_TESTS = {
    "STAGE1_OUTBOX": (
        "tests/quant_phase9/test_stage1_outbox_gate.py",
        "tests/quant_phase9/test_intake.py",
    ),
    "LIQUIDATION_PARTIAL": ("tests/quant_phase9/test_liquidation_gate.py",),
    "IMMUTABLE_SNAPSHOT": ("tests/quant_phase9/test_snapshot_replay_gate.py",),
    "DETERMINISTIC_JEV": ("tests/quant_phase9/test_jev_gate.py",),
}
SUITE_OUTPUTS = {
    "STAGE1_OUTBOX": "stage1-outbox.json",
    "LIQUIDATION_PARTIAL": "liquidation-partial.json",
    "IMMUTABLE_SNAPSHOT": "immutable-snapshot.json",
    "DETERMINISTIC_JEV": "deterministic-jev.json",
}
COMPATIBILITY_PROTECTED_PATHS = (
    ".gitignore",
    "config/phase9/compatibility-closure-v1.json",
    "migrations/016_phase9_evidence_chain.sql",
    "scripts/run_phase9_compatibility_suite.py",
    "src/quant_phase1/repositories.py",
    "src/quant_phase1/service.py",
    "src/quant_phase6/contract_v1.py",
    "src/quant_phase9/__init__.py",
    "src/quant_phase9/canonical.py",
    "src/quant_phase9/compatibility_gate.py",
    "src/quant_phase9/contracts.py",
    "src/quant_phase9/intake.py",
    "src/quant_phase9/jev.py",
    "src/quant_phase9/jev_persistence.py",
    "src/quant_phase9/liquidation.py",
    "src/quant_phase9/snapshot.py",
    "src/quant_phase9/sources/__init__.py",
    "src/quant_phase9/sources/phase1.py",
    "src/quant_phase9/sources/phase2.py",
    "src/quant_phase9/sources/phase3.py",
    "src/quant_phase9/sources/phase4.py",
    "tests/quant_phase9/test_canonical.py",
    "tests/quant_phase9/test_compatibility_gate.py",
    "tests/quant_phase9/test_contracts.py",
    "tests/quant_phase9/test_intake.py",
    "tests/quant_phase9/test_jev_gate.py",
    "tests/quant_phase9/test_liquidation_gate.py",
    "tests/quant_phase9/test_snapshot_replay_gate.py",
    "tests/quant_phase9/test_stage1_outbox_gate.py",
    "tests/test_phase6_contract_v1.py",
    "tests/test_repository_integration.py",
)

_SUITE_IDS = tuple(SUITE_TESTS)
_ARTIFACT_SCHEMA = "PHASE9_COMPATIBILITY_SUITE_EVIDENCE_V1"
_GATE_SCHEMA = "PHASE9_COMPATIBILITY_GATE_V1"
_ARTIFACT_FIELDS = (
    "schema", "suite_id", "source_commit", "protected_tree_digest", "command_id",
    "tests_passed", "tests_skipped", "tests_failed", "tests_errors", "artifact_digest",
    "passed", "generated_at",
)
_GATE_FIELDS = (
    "schema", "stage1_outbox_pass", "liquidation_partial_pass", "immutable_snapshot_pass",
    "deterministic_jev_pass", "tests_passed", "tests_skipped", "tests_failed", "tests_errors",
    "source_commit", "protected_tree_digest", "PHASE9_COMPATIBILITY_CLOSURE_PASS", "passed",
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SHA1_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_CLOSURE_MARKER_PATH = "config/phase9/compatibility-closure-v1.json"
_EXPECTED_MARKER = b'{"schema":"PHASE9_COMPATIBILITY_CLOSURE_MARKER_V1","version":"1"}\n'
_MAX_JUNIT_BYTES = 4 * 1024 * 1024
_SUITE_TIMEOUT_SECONDS = 300


class CompatibilityGateError(ValueError):
    """Compatibility evidence or repository state is missing or invalid."""


@dataclass(frozen=True, slots=True)
class CompatibilitySuiteEvidenceV1:
    schema: Literal["PHASE9_COMPATIBILITY_SUITE_EVIDENCE_V1"]
    suite_id: Literal["STAGE1_OUTBOX", "LIQUIDATION_PARTIAL", "IMMUTABLE_SNAPSHOT", "DETERMINISTIC_JEV"]
    source_commit: GitSha
    protected_tree_digest: Sha256Hex
    command_id: str
    tests_passed: int
    tests_skipped: int
    tests_failed: int
    tests_errors: int
    artifact_digest: Sha256Hex
    passed: bool
    generated_at: datetime

    def __post_init__(self) -> None:
        if self.schema != _ARTIFACT_SCHEMA or self.suite_id not in _SUITE_IDS:
            raise CompatibilityGateError("compatibility suite evidence schema or suite_id is invalid")
        if self.command_id != self.suite_id:
            raise CompatibilityGateError("compatibility suite command_id must equal suite_id")
        object.__setattr__(self, "source_commit", GitSha(self.source_commit))
        object.__setattr__(self, "protected_tree_digest", Sha256Hex(self.protected_tree_digest))
        object.__setattr__(self, "artifact_digest", Sha256Hex(self.artifact_digest))
        counts = (self.tests_passed, self.tests_skipped, self.tests_failed, self.tests_errors)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
            raise CompatibilityGateError("compatibility suite counts must be non-negative integers")
        if not isinstance(self.passed, bool):
            raise CompatibilityGateError("compatibility suite passed must be boolean")
        if (not isinstance(self.generated_at, datetime) or self.generated_at.tzinfo is None
                or self.generated_at.utcoffset() != timedelta(0)):
            raise CompatibilityGateError("generated_at must be timezone-aware UTC")
        expected_pass = self.tests_passed > 0 and not any(
            (self.tests_skipped, self.tests_failed, self.tests_errors)
        )
        if self.passed is not expected_pass:
            raise CompatibilityGateError("suite passed must exactly match its test counts")
        unsigned = self._unsigned_dict()
        if canonical_sha256(unsigned) != self.artifact_digest:
            raise CompatibilityGateError("compatibility suite artifact_digest mismatch")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            field.name: getattr(self, field.name)
            for field in fields(self)
            if field.name != "artifact_digest"
        }

    def to_dict(self) -> dict[str, object]:
        if tuple(field.name for field in fields(self)) != _ARTIFACT_FIELDS:
            raise CompatibilityGateError("suite evidence fields do not match the frozen schema")
        return json.loads(canonical_bytes(self))

    @classmethod
    def from_dict(cls, value: object) -> "CompatibilitySuiteEvidenceV1":
        if not isinstance(value, dict) or set(value) != set(_ARTIFACT_FIELDS):
            raise CompatibilityGateError("suite evidence fields are missing or unknown")
        try:
            timestamp = value["generated_at"]
            if not isinstance(timestamp, str):
                raise TypeError("generated_at must be a string")
            generated_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            return cls(
                schema=value["schema"], suite_id=value["suite_id"],
                source_commit=GitSha(value["source_commit"]),
                protected_tree_digest=Sha256Hex(value["protected_tree_digest"]),
                command_id=value["command_id"], tests_passed=value["tests_passed"],
                tests_skipped=value["tests_skipped"], tests_failed=value["tests_failed"],
                tests_errors=value["tests_errors"], artifact_digest=Sha256Hex(value["artifact_digest"]),
                passed=value["passed"], generated_at=generated_at,
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, CompatibilityGateError):
                raise
            raise CompatibilityGateError("compatibility suite evidence has invalid field values") from exc


@dataclass(frozen=True, slots=True)
class Phase9CompatibilityGateResult:
    schema: Literal["PHASE9_COMPATIBILITY_GATE_V1"]
    stage1_outbox_pass: bool
    liquidation_partial_pass: bool
    immutable_snapshot_pass: bool
    deterministic_jev_pass: bool
    tests_passed: int
    tests_skipped: int
    tests_failed: int
    tests_errors: int
    source_commit: GitSha
    protected_tree_digest: Sha256Hex
    PHASE9_COMPATIBILITY_CLOSURE_PASS: bool
    passed: bool

    def to_dict(self) -> dict[str, object]:
        if tuple(field.name for field in fields(self)) != _GATE_FIELDS:
            raise CompatibilityGateError("aggregate gate fields do not match the frozen schema")
        return json.loads(canonical_bytes(self))


def _run_git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), *args], check=False, capture_output=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CompatibilityGateError("required git evidence command could not complete") from exc
    if result.returncode != 0:
        raise CompatibilityGateError("required git evidence command failed")
    return result


def resolve_phase9_compatibility_closure_commit(repo_root: Path) -> GitSha:
    if not isinstance(repo_root, Path):
        raise TypeError("repo_root must be pathlib.Path")
    result = _run_git(
        repo_root, "log", "--follow", "--diff-filter=A", "--format=%H", "--", _CLOSURE_MARKER_PATH
    )
    try:
        candidates = result.stdout.decode("ascii", errors="strict").splitlines()
        if len(candidates) != 1:
            raise CompatibilityGateError("closure marker must have exactly one first-add commit")
        commit = GitSha(candidates[0])
    except (UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, CompatibilityGateError):
            raise
        raise CompatibilityGateError("closure first-add commit is malformed") from exc
    _run_git(repo_root, "cat-file", "-e", f"{commit}^{{commit}}")
    if _run_git(repo_root, "show", f"{commit}:{_CLOSURE_MARKER_PATH}").stdout != _EXPECTED_MARKER:
        raise CompatibilityGateError("closure marker bytes at first-add commit do not match the frozen marker")
    if _run_git(repo_root, "show", f"HEAD:{_CLOSURE_MARKER_PATH}").stdout != _EXPECTED_MARKER:
        raise CompatibilityGateError("closure marker bytes at HEAD do not match the frozen marker")
    try:
        ancestry = subprocess.run(
            ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", str(commit), "HEAD"],
            check=False, capture_output=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CompatibilityGateError("closure ancestry check could not complete") from exc
    if ancestry.returncode != 0:
        raise CompatibilityGateError("closure first-add commit is not an ancestor of HEAD")
    return commit


def ensure_protected_tree_clean(repo_root: Path) -> None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "diff", "--quiet", "HEAD", "--", *COMPATIBILITY_PROTECTED_PATHS],
            check=False, capture_output=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CompatibilityGateError("protected-tree cleanliness check could not complete") from exc
    if result.returncode != 0:
        raise CompatibilityGateError("compatibility-protected paths have staged or unstaged changes")


def protected_tree_digest(repo_root: Path) -> Sha256Hex:
    ensure_protected_tree_clean(repo_root)
    result = _run_git(repo_root, "ls-tree", "-r", "-z", "HEAD", "--", *COMPATIBILITY_PROTECTED_PATHS)
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    try:
        records = result.stdout.split(b"\0")
        for record in records:
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, object_sha = metadata.decode("ascii", errors="strict").split(" ")
            path = raw_path.decode("utf-8", errors="strict")
            if path in seen or path not in COMPATIBILITY_PROTECTED_PATHS:
                raise CompatibilityGateError("protected tree contains a duplicate or unexpected path")
            if mode not in {"100644", "100755"} or object_type != "blob":
                raise CompatibilityGateError("protected tree entry metadata is malformed")
            if re.fullmatch(r"[0-9a-f]{40}", object_sha) is None:
                raise CompatibilityGateError("protected tree object id is malformed")
            seen.add(path)
            entries.append({"path": path, "mode": mode, "object_type": object_type, "object_sha": object_sha})
    except (UnicodeDecodeError, ValueError) as exc:
        if isinstance(exc, CompatibilityGateError):
            raise
        raise CompatibilityGateError("protected tree listing is malformed") from exc
    if seen != set(COMPATIBILITY_PROTECTED_PATHS):
        raise CompatibilityGateError("one or more compatibility-protected paths are not tracked at HEAD")
    entries.sort(key=lambda entry: entry["path"])
    return canonical_sha256(entries)


def _fixed_artifact_directory(repo_root: Path, artifact_dir: Path) -> Path:
    root = repo_root.resolve()
    candidate = artifact_dir if artifact_dir.is_absolute() else root / artifact_dir
    expected = root / "artifacts/phase9/compatibility"
    if candidate.absolute() != expected:
        raise CompatibilityGateError("suite evidence must use the fixed artifact directory")
    if any(path.is_symlink() for path in (
        root / "artifacts", root / "artifacts/phase9", expected,
    )):
        raise CompatibilityGateError("suite artifact directory must not traverse symlinks")
    return expected


def _read_suite_evidence(path: Path) -> CompatibilitySuiteEvidenceV1:
    try:
        if path.is_symlink():
            raise CompatibilityGateError("suite evidence file must not be a symlink")
        if not path.is_file():
            raise CompatibilityGateError("suite evidence must be a regular file")
        raw = path.read_bytes()
        if len(raw) > 65_536:
            raise CompatibilityGateError("suite evidence exceeds 64 KiB")
        value = json.loads(raw)
        record = CompatibilitySuiteEvidenceV1.from_dict(value)
        if canonical_bytes(record) + b"\n" != raw:
            raise CompatibilityGateError("suite evidence is not canonical JSON")
        return record
    except (OSError, json.JSONDecodeError) as exc:
        raise CompatibilityGateError("suite evidence is missing or invalid JSON") from exc


def evaluate_phase9_compatibility_gates(
    *, repo_root: Path, artifact_dir: Path
) -> Phase9CompatibilityGateResult:
    artifact_dir = _fixed_artifact_directory(repo_root, artifact_dir)
    ensure_protected_tree_clean(repo_root)
    source_commit = resolve_phase9_compatibility_closure_commit(repo_root)
    tree_digest = protected_tree_digest(repo_root)
    records: dict[str, CompatibilitySuiteEvidenceV1] = {}
    for suite_id in _SUITE_IDS:
        path = artifact_dir / SUITE_OUTPUTS[suite_id]
        record = _read_suite_evidence(path)
        if record.suite_id != suite_id or record.command_id != suite_id:
            raise CompatibilityGateError("suite evidence command/suite binding mismatch")
        if record.source_commit != source_commit:
            raise CompatibilityGateError("suite evidence source_commit differs from closure first-add commit")
        if record.protected_tree_digest != tree_digest:
            raise CompatibilityGateError("suite evidence protected_tree_digest differs from HEAD")
        records[suite_id] = record

    suite_passes = {suite_id: records[suite_id].passed for suite_id in _SUITE_IDS}
    passed = all(suite_passes.values())
    totals = tuple(
        sum(getattr(records[suite_id], name) for suite_id in _SUITE_IDS)
        for name in ("tests_passed", "tests_skipped", "tests_failed", "tests_errors")
    )
    return Phase9CompatibilityGateResult(
        schema=_GATE_SCHEMA,
        stage1_outbox_pass=suite_passes["STAGE1_OUTBOX"],
        liquidation_partial_pass=suite_passes["LIQUIDATION_PARTIAL"],
        immutable_snapshot_pass=suite_passes["IMMUTABLE_SNAPSHOT"],
        deterministic_jev_pass=suite_passes["DETERMINISTIC_JEV"],
        tests_passed=totals[0], tests_skipped=totals[1], tests_failed=totals[2], tests_errors=totals[3],
        source_commit=source_commit, protected_tree_digest=tree_digest,
        PHASE9_COMPATIBILITY_CLOSURE_PASS=passed, passed=passed,
    )


def parse_junit_counts(path: Path) -> tuple[int, int, int, int]:
    """Return passed, skipped, failed, and error testcase counts from bounded JUnit XML."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise CompatibilityGateError("pytest JUnit report is missing or unreadable") from exc
    if len(raw) > _MAX_JUNIT_BYTES or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise CompatibilityGateError("pytest JUnit report exceeds safety limits")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise CompatibilityGateError("pytest JUnit report is malformed") from exc
    if root.tag not in {"testsuite", "testsuites"}:
        raise CompatibilityGateError("pytest JUnit report has an unexpected root element")
    cases = list(root.iter("testcase"))
    if not cases:
        raise CompatibilityGateError("pytest JUnit report contains no testcases")
    skipped = failed = errors = 0
    for case in cases:
        outcomes = [case.find(tag) is not None for tag in ("skipped", "failure", "error")]
        if sum(outcomes) > 1:
            raise CompatibilityGateError("pytest JUnit testcase has conflicting outcomes")
        skipped += int(outcomes[0])
        failed += int(outcomes[1])
        errors += int(outcomes[2])
    passed = len(cases) - skipped - failed - errors
    return passed, skipped, failed, errors


def _execute_pytest(
    command: list[str], junit_path: Path, timeout_seconds: int, *, cwd: Path
) -> subprocess.CompletedProcess[None]:
    try:
        return subprocess.run(
            [*command[:3], "--junitxml", str(junit_path), *command[3:]],
            check=False, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        # No provider output or pytest output is surfaced; the caller records a bounded error result.
        return subprocess.CompletedProcess(command, 124, b"", b"")


def _write_suite_evidence(
    path: Path, record: CompatibilitySuiteEvidenceV1 | Phase9CompatibilityGateResult
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_bytes(record) + b"\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except OSError as exc:
        raise CompatibilityGateError("could not atomically write suite evidence") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def run_compatibility_suite(
    *, suite_id: str, output: Path, repo_root: Path
) -> CompatibilitySuiteEvidenceV1:
    """Run exactly one fixed compatibility suite and persist its anchored evidence."""
    if suite_id not in SUITE_TESTS:
        raise CompatibilityGateError("unknown compatibility suite_id")
    root = repo_root.resolve()
    artifact_dir = _fixed_artifact_directory(root, root / "artifacts/phase9/compatibility")
    if not output.is_absolute():
        output = root / output
    expected_output = artifact_dir / SUITE_OUTPUTS[suite_id]
    if output.absolute() != expected_output:
        raise CompatibilityGateError("suite output path does not match its fixed artifact destination")
    if output.is_symlink():
        raise CompatibilityGateError("suite output path must not be a symlink")
    output.unlink(missing_ok=True)

    ensure_protected_tree_clean(repo_root)
    closure_commit = resolve_phase9_compatibility_closure_commit(repo_root)
    tree_digest = protected_tree_digest(repo_root)
    head_before = _run_git(root, "rev-parse", "HEAD").stdout.decode("ascii").strip()
    if _SHA1_PATTERN.fullmatch(head_before) is None:
        raise CompatibilityGateError("HEAD is not a full lowercase Git SHA-1")

    command = [sys.executable, "-m", "pytest", "-q", *SUITE_TESTS[suite_id]]
    with tempfile.TemporaryDirectory(prefix="phase9-compatibility-") as temp_dir:
        junit_path = Path(temp_dir) / "pytest-results.xml"
        try:
            process = _execute_pytest(command, junit_path, _SUITE_TIMEOUT_SECONDS, cwd=root)
            counts = parse_junit_counts(junit_path)
            passed_tests, skipped_tests, failed_tests, error_tests = counts
            if process.returncode != 0 and not (skipped_tests or failed_tests or error_tests):
                error_tests = 1
        except CompatibilityGateError:
            passed_tests, skipped_tests, failed_tests, error_tests = 0, 0, 0, 1

    ensure_protected_tree_clean(repo_root)
    head_after = _run_git(repo_root, "rev-parse", "HEAD").stdout.decode("ascii").strip()
    if head_after != head_before or protected_tree_digest(repo_root) != tree_digest:
        raise CompatibilityGateError("protected tree or HEAD changed while the suite ran")

    passed = passed_tests > 0 and not any((skipped_tests, failed_tests, error_tests))
    unsigned = {
        "schema": _ARTIFACT_SCHEMA,
        "suite_id": suite_id,
        "source_commit": closure_commit,
        "protected_tree_digest": tree_digest,
        "command_id": suite_id,
        "tests_passed": passed_tests,
        "tests_skipped": skipped_tests,
        "tests_failed": failed_tests,
        "tests_errors": error_tests,
        "passed": passed,
        "generated_at": datetime.now(timezone.utc),
    }
    record = CompatibilitySuiteEvidenceV1(
        **unsigned,
        artifact_digest=canonical_sha256(unsigned),
    )
    _write_suite_evidence(output, record)
    return record


def suite_main(
    argv: list[str] | None = None,
    *,
    repo_root: Path | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Run one fixed Phase 9 compatibility suite")
    parser.add_argument("--suite", choices=_SUITE_IDS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    root = repo_root or Path(__file__).resolve().parents[2]
    try:
        record = run_compatibility_suite(suite_id=args.suite, output=args.output, repo_root=root)
    except (CompatibilityGateError, OSError, ValueError) as exc:
        print(f"compatibility suite invalid: {type(exc).__name__}", file=sys.stderr)
        return 3
    print(canonical_bytes(record).decode("utf-8"))
    return 0 if record.passed else 2


def main(
    argv: list[str] | None = None,
    *,
    repo_root: Path | None = None,
    artifact_dir: Path | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-pass", action="store_true", required=True)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    root = repo_root or Path(__file__).resolve().parents[2]
    evidence_dir = artifact_dir or root / "artifacts/phase9/compatibility"
    try:
        output = None
        if args.json_output is not None:
            root = root.resolve()
            candidate = args.json_output if args.json_output.is_absolute() else root / args.json_output
            candidate = candidate.absolute()
            base = root / "artifacts/phase9"
            if (not candidate.is_relative_to(base)
                    or candidate.is_relative_to(base / "compatibility")
                    or candidate.suffix != ".json"
                    or any(part == ".." for part in candidate.parts)
                    or any(path.is_symlink() for path in (candidate, *candidate.parents)
                           if path.is_relative_to(root))):
                raise CompatibilityGateError("gate output must be a local Phase9 JSON artifact without symlinks")
            output = candidate
            output.unlink(missing_ok=True)
        result = evaluate_phase9_compatibility_gates(repo_root=root, artifact_dir=evidence_dir)
        if output is not None:
            _write_suite_evidence(output, result)
    except (CompatibilityGateError, OSError, ValueError) as exc:
        print(f"compatibility evidence invalid: {type(exc).__name__}", file=sys.stderr)
        return 3
    print(canonical_bytes(result).decode("utf-8"))
    return 0 if result.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
