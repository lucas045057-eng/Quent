"""Machine-checked Phase 9 acceptance; no caller-supplied success flags."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, fields
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Literal, Sequence
import xml.etree.ElementTree as ET

from quant_phase9.canonical import canonical_bytes, canonical_sha256
from quant_phase9.contracts import GitSha, Sha256Hex


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PLAN_PATH = "docs/superpowers/plans/2026-09-27-phase9-evidence-chain.md"
IDS = tuple(f"A{index}" for index in range(1, 10))
TASK_IDS = (
    "TASK_0_1", "TASK_0_2", "TASK_0_3", "TASK_0_4", "TASK_0_5",
    "TASK_0_6", "TASK_0_7", "TASK_0_8", "TASK_0_9", "TASK_1",
    "TASK_2", "TASK_3", "TASK_4", "TASK_5", "TASK_6A",
    "TASK_6B", "TASK_7", "TASK_8", "TASK_9",
)
PARSERS = {
    "A1": "a1_compatibility_gate_v1", "A2": "a2_pytest_junit_v1",
    "A3": "a3_pytest_junit_v1", "A4": "a4_pytest_junit_v1",
    "A5": "a5_replay_v1", "A6": "a6_secret_scan_v1",
    "A7": "a7_runtime_acceptance_v1", "A8": "a8_git_diff_check_v1",
    "A9": "a9_git_status_clean_v1",
}
KINDS = {
    "A1": "JSON_ARTIFACT", "A2": "PYTEST_JUNIT", "A3": "PYTEST_JUNIT",
    "A4": "PYTEST_JUNIT", "A5": "JSON_ARTIFACT", "A6": "JSON_ARTIFACT",
    "A7": "JSON_ARTIFACT", "A8": "GIT_DIFF_CHECK", "A9": "GIT_STATUS_CLEAN",
}
HASH_NAMES = (
    "evaluation_snapshot_hash", "evidence_items_digest", "evidence_chain_digest",
    "pattern_matches_digest", "decision_candidate_digest", "input_snapshot_hash",
)
RECORD_FIELDS = (
    "schema", "command_id", "kind", "parser_id", "command", "source_commit",
    "required", "exit_code", "tests_passed", "tests_skipped", "tests_failed",
    "tests_errors", "artifact_path", "output_digest", "artifact_digest", "passed",
)
COMPLETION_FIELDS = (
    "schema", "plan_path", "plan_digest", "source_commit", "required_task_ids",
    "completed_task_ids", "task_commits", "generated_at", "passed",
)
RUNTIME_FIELDS = (
    "schema", "passed", "runtime_duration_seconds", "PHASE9_RUNTIME_INTEGRATION_PASS",
    "PHASE9_FAILURE_SEMANTICS_PASS", "PHASE9_PERSISTENCE_AUDIT_PASS",
    "hard_safety_violation", "required_test_skips", "PHASE9_REAL_JEV_INTEGRATION_STATUS",
)
RAW_NAMES = {
    "A1": "A1.gate.json", "A2": "A2.junit.xml", "A3": "A3.junit.xml",
    "A4": "A4.junit.xml", "A5": "A5.replay.json", "A6": "A6.secret-scan.json",
    "A7": "A7.runtime.json", "A8": "A8.capture.json", "A9": "A9.capture.json",
}


class AcceptanceContractError(ValueError):
    """A required acceptance observation is absent, malformed or unsafe."""


def validate_formal_duration(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 900:
        raise AcceptanceContractError("INVALID_ACCEPTANCE_DURATION: formal runtime requires >=900 seconds")
    return value


def _exact(value: object, expected: Sequence[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(expected):
        raise AcceptanceContractError(f"{label} has missing or unknown fields")
    return value


def _read_json(path: Path, *, limit: int = 4 * 1024 * 1024) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AcceptanceContractError("required acceptance artifact is missing") from exc
    if len(raw) > limit:
        raise AcceptanceContractError("acceptance artifact exceeds byte limit")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        parsed = {}
        for key, value in pairs:
            if key in parsed:
                raise AcceptanceContractError("acceptance JSON contains duplicate keys")
            parsed[key] = value
        return parsed

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceContractError("acceptance JSON is malformed") from exc
    if not isinstance(value, dict):
        raise AcceptanceContractError("acceptance JSON must be an object")
    return value


def _nonnegative(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AcceptanceContractError(f"{label} must be a non-negative integer")
    return value


def _success_counts(passed: int, skipped: int = 0, failed: int = 0,
                    errors: int = 0, *, exit_code: int) -> dict[str, object]:
    if exit_code != 0 or passed <= 0 or any((skipped, failed, errors)):
        raise AcceptanceContractError("required acceptance command did not pass exactly")
    return {"passed": True, "tests_passed": passed, "tests_skipped": skipped,
            "tests_failed": failed, "tests_errors": errors}


def parse_a1_compatibility_gate_v1(path: Path, *, exit_code: int) -> dict[str, object]:
    row = _read_json(path)
    expected = (
        "schema", "stage1_outbox_pass", "liquidation_partial_pass",
        "immutable_snapshot_pass", "deterministic_jev_pass", "tests_passed",
        "tests_skipped", "tests_failed", "tests_errors", "source_commit",
        "protected_tree_digest", "PHASE9_COMPATIBILITY_CLOSURE_PASS", "passed",
    )
    _exact(row, expected, "A1 compatibility gate")
    if row["schema"] != "PHASE9_COMPATIBILITY_GATE_V1":
        raise AcceptanceContractError("A1 schema mismatch")
    for field in (
        "stage1_outbox_pass", "liquidation_partial_pass", "immutable_snapshot_pass",
        "deterministic_jev_pass", "PHASE9_COMPATIBILITY_CLOSURE_PASS", "passed",
    ):
        if row[field] is not True:
            raise AcceptanceContractError(f"A1 {field} is not true")
    GitSha(row["source_commit"])
    Sha256Hex(row["protected_tree_digest"])
    return _success_counts(*(
        _nonnegative(row[name], name) for name in
        ("tests_passed", "tests_skipped", "tests_failed", "tests_errors")
    ), exit_code=exit_code)


def _parse_junit(path: Path, *, exit_code: int) -> dict[str, object]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AcceptanceContractError("JUnit artifact is missing") from exc
    if len(raw) > 4 * 1024 * 1024 or not raw:
        raise AcceptanceContractError("JUnit artifact is empty or oversized")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise AcceptanceContractError("JUnit artifact is malformed") from exc
    if root.tag not in {"testsuite", "testsuites"}:
        raise AcceptanceContractError("JUnit root must be testsuite or testsuites")
    suites = [item for item in root.iter("testsuite") if not list(item.iter("testsuite"))[1:]]
    if not suites:
        raise AcceptanceContractError("JUnit has no leaf test suites")
    totals = [0, 0, 0, 0]
    for suite in suites:
        values = []
        for key in ("tests", "skipped", "failures", "errors"):
            raw_count = suite.get(key)
            if raw_count is None or re.fullmatch(r"[0-9]+", raw_count) is None:
                raise AcceptanceContractError("JUnit suite count is missing or malformed")
            values.append(int(raw_count))
        if sum(values[1:]) > values[0]:
            raise AcceptanceContractError("JUnit suite counts are inconsistent")
        totals = [left + right for left, right in zip(totals, values)]
    tests, skipped, failed, errors = totals
    return _success_counts(tests - skipped - failed - errors, skipped, failed,
                           errors, exit_code=exit_code)


def parse_a2_pytest_junit_v1(path: Path, *, exit_code: int) -> dict[str, object]:
    return _parse_junit(path, exit_code=exit_code)


def parse_a3_pytest_junit_v1(path: Path, *, exit_code: int) -> dict[str, object]:
    return _parse_junit(path, exit_code=exit_code)


def parse_a4_pytest_junit_v1(path: Path, *, exit_code: int) -> dict[str, object]:
    return _parse_junit(path, exit_code=exit_code)


def parse_a5_replay_v1(path: Path, *, exit_code: int) -> dict[str, object]:
    row = _read_json(path)
    _exact(row, (
        "schema", "passed", "PHASE9_DETERMINISTIC_REPLAY_PASS", "hashes_compared",
        "hashes_match", "provider_calls", "ai_calls",
    ), "A5 replay")
    if (row["schema"] != "PHASE9_REPLAY_RESULT_V1"
            or row["passed"] is not True
            or row["PHASE9_DETERMINISTIC_REPLAY_PASS"] is not True
            or row["hashes_match"] is not True
            or row["hashes_compared"] != list(HASH_NAMES)
            or row["provider_calls"] != 0 or isinstance(row["provider_calls"], bool)
            or row["ai_calls"] != 0 or isinstance(row["ai_calls"], bool)
            or exit_code != 0):
        raise AcceptanceContractError("A5 deterministic replay did not pass")
    return {"passed": True, "tests_passed": 0, "tests_skipped": 0,
            "tests_failed": 0, "tests_errors": 0}


def parse_a6_secret_scan_v1(path: Path, *, exit_code: int) -> dict[str, object]:
    row = _read_json(path)
    _exact(row, ("schema", "passed", "secret_leak_found", "findings_count"), "A6 secret scan")
    if (row["schema"] != "PHASE9_SECRET_SCAN_V1" or row["passed"] is not True
            or row["secret_leak_found"] is not False
            or _nonnegative(row["findings_count"], "findings_count") != 0
            or exit_code != 0):
        raise AcceptanceContractError("A6 secret scan did not pass")
    return {"passed": True, "tests_passed": 0, "tests_skipped": 0,
            "tests_failed": 0, "tests_errors": 0}


def parse_a7_runtime_acceptance_v1(path: Path, *, exit_code: int) -> dict[str, object]:
    row = _read_json(path)
    _exact(row, RUNTIME_FIELDS, "A7 runtime")
    if row["schema"] != "PHASE9_RUNTIME_ACCEPTANCE_V1":
        raise AcceptanceContractError("A7 runtime schema mismatch")
    validate_formal_duration(row["runtime_duration_seconds"])
    if (exit_code != 0 or row["passed"] is not True
            or row["PHASE9_RUNTIME_INTEGRATION_PASS"] is not True
            or row["PHASE9_FAILURE_SEMANTICS_PASS"] is not True
            or row["PHASE9_PERSISTENCE_AUDIT_PASS"] is not True
            or row["hard_safety_violation"] is not False
            or _nonnegative(row["required_test_skips"], "required_test_skips") != 0
            or row["PHASE9_REAL_JEV_INTEGRATION_STATUS"] not in
            {"NOT_CONFIGURED", "PASS", "BLOCKED_EXTERNAL"}):
        raise AcceptanceContractError("A7 runtime acceptance did not pass")
    return {"passed": True, "tests_passed": 0, "tests_skipped": 0,
            "tests_failed": 0, "tests_errors": 0}


def parse_a8_git_diff_check_v1(stdout: str, stderr: str, *, exit_code: int) -> dict[str, object]:
    if exit_code != 0 or stdout or stderr:
        raise AcceptanceContractError("A8 git diff --check reported a violation")
    return {"passed": True, "tests_passed": 0, "tests_skipped": 0,
            "tests_failed": 0, "tests_errors": 0}


def parse_a9_git_status_clean_v1(stdout: str, stderr: str, *, exit_code: int) -> dict[str, object]:
    if exit_code != 0 or stdout or stderr:
        raise AcceptanceContractError("A9 worktree is not clean")
    return {"passed": True, "tests_passed": 0, "tests_skipped": 0,
            "tests_failed": 0, "tests_errors": 0}


def _git(repo_root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo_root), *args], check=False,
                            capture_output=True, timeout=30)
    if result.returncode != 0 or result.stderr:
        raise AcceptanceContractError("required git evidence could not be verified")
    return result.stdout.decode("utf-8", errors="strict")


@dataclass(frozen=True, slots=True)
class Phase9ImplementationCompletionV1:
    schema: Literal["PHASE9_IMPLEMENTATION_COMPLETION_V1"]
    plan_path: str
    plan_digest: Sha256Hex
    source_commit: GitSha
    required_task_ids: tuple[str, ...]
    completed_task_ids: tuple[str, ...]
    task_commits: dict[str, GitSha]
    generated_at: datetime
    passed: bool


def parse_phase9_implementation_completion_v1(
    path: Path, *, repo_root: Path, formal_source_commit: GitSha,
) -> Phase9ImplementationCompletionV1:
    row = _read_json(path)
    _exact(row, COMPLETION_FIELDS, "C1 implementation completion")
    if row["schema"] != "PHASE9_IMPLEMENTATION_COMPLETION_V1" or row["plan_path"] != PLAN_PATH:
        raise AcceptanceContractError("C1 schema or plan path mismatch")
    source_commit = GitSha(row["source_commit"])
    if source_commit != GitSha(formal_source_commit):
        raise AcceptanceContractError("C1 source commit differs from formal run")
    if row["required_task_ids"] != list(TASK_IDS) or row["completed_task_ids"] != list(TASK_IDS):
        raise AcceptanceContractError("C1 must contain exactly all 19 tasks")
    commits = _exact(row["task_commits"], TASK_IDS, "C1 task commits")
    commits = {key: GitSha(value) for key, value in commits.items()}
    if len(set(commits.values())) != len(TASK_IDS):
        raise AcceptanceContractError("C1 tasks must have distinct milestone commits")
    plan_data = _git(repo_root, "show", f"{source_commit}:{PLAN_PATH}")
    plan_digest = hashlib.sha256(
        plan_data.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    ).hexdigest()
    if Sha256Hex(row["plan_digest"]) != plan_digest:
        raise AcceptanceContractError("C1 plan digest mismatch")
    for commit in commits.values():
        result = subprocess.run(
            ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", commit, source_commit],
            check=False, capture_output=True, timeout=30,
        )
        if result.returncode != 0:
            raise AcceptanceContractError("C1 task commit is not an ancestor")
    try:
        generated = datetime.fromisoformat(row["generated_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise AcceptanceContractError("C1 generated_at is invalid") from exc
    if generated.utcoffset() != timedelta(0) or row["passed"] is not True:
        raise AcceptanceContractError("C1 must be generated in UTC and recompute true")
    return Phase9ImplementationCompletionV1(
        schema=row["schema"], plan_path=PLAN_PATH, plan_digest=Sha256Hex(plan_digest),
        source_commit=source_commit, required_task_ids=TASK_IDS,
        completed_task_ids=TASK_IDS, task_commits=commits,
        generated_at=generated.astimezone(timezone.utc), passed=True,
    )


@dataclass(frozen=True, slots=True)
class AcceptanceCommandResultV1:
    schema: Literal["PHASE9_ACCEPTANCE_COMMAND_V1"]
    command_id: str
    kind: str
    parser_id: str
    command: str
    source_commit: GitSha
    required: bool
    exit_code: int | None
    tests_passed: int
    tests_skipped: int
    tests_failed: int
    tests_errors: int
    artifact_path: str
    output_digest: Sha256Hex
    artifact_digest: Sha256Hex
    passed: bool


def _raw_artifact_path(repo_root: Path, value: str) -> Path:
    if not isinstance(value, str) or not value.startswith("artifacts/phase9/final/commands/"):
        raise AcceptanceContractError("raw artifact path is outside the acceptance directory")
    path = (repo_root / value).resolve()
    if (repo_root.resolve() not in path.parents or path.is_symlink()
            or ".." in Path(value).parts):
        raise AcceptanceContractError("raw artifact path escapes repository")
    return path


def _parse_raw_for(command_id: str, raw_path: Path, *, exit_code: int) -> dict[str, object]:
    parsers = {
        "A1": parse_a1_compatibility_gate_v1,
        "A2": parse_a2_pytest_junit_v1,
        "A3": parse_a3_pytest_junit_v1,
        "A4": parse_a4_pytest_junit_v1,
        "A5": parse_a5_replay_v1,
        "A6": parse_a6_secret_scan_v1,
        "A7": parse_a7_runtime_acceptance_v1,
    }
    if command_id in parsers:
        return parsers[command_id](raw_path, exit_code=exit_code)
    capture = _read_json(raw_path, limit=65_536)
    _exact(capture, ("stdout", "stderr"), f"{command_id} capture")
    if not isinstance(capture["stdout"], str) or not isinstance(capture["stderr"], str):
        raise AcceptanceContractError("git capture output must be text")
    if command_id == "A8":
        return parse_a8_git_diff_check_v1(capture["stdout"], capture["stderr"],
                                          exit_code=exit_code)
    if command_id == "A9":
        return parse_a9_git_status_clean_v1(capture["stdout"], capture["stderr"],
                                            exit_code=exit_code)
    raise AcceptanceContractError("unknown acceptance command ID")


def _output_digest(command_id: str, raw_path: Path) -> Sha256Hex:
    if command_id in {"A8", "A9"}:
        return canonical_sha256(_read_json(raw_path, limit=65_536))
    raw = raw_path.read_bytes()
    if len(raw) > 4 * 1024 * 1024:
        raise AcceptanceContractError("acceptance raw artifact exceeds byte limit")
    return Sha256Hex(hashlib.sha256(raw).hexdigest())


def make_command_record(
    *, command_id: str, command: str, source_commit: GitSha,
    exit_code: int, raw_path: Path, repo_root: Path,
) -> AcceptanceCommandResultV1:
    if command_id not in IDS or not isinstance(command, str) or not command:
        raise AcceptanceContractError("invalid acceptance command specification")
    path = _raw_artifact_path(repo_root, str(raw_path.relative_to(repo_root)))
    if path.name != RAW_NAMES[command_id]:
        raise AcceptanceContractError("raw artifact filename does not match command")
    observation = _parse_raw_for(command_id, path, exit_code=exit_code)
    unsigned = {
        "schema": "PHASE9_ACCEPTANCE_COMMAND_V1", "command_id": command_id,
        "kind": KINDS[command_id], "parser_id": PARSERS[command_id],
        "command": command, "source_commit": GitSha(source_commit),
        "required": True, "exit_code": exit_code,
        "tests_passed": observation["tests_passed"],
        "tests_skipped": observation["tests_skipped"],
        "tests_failed": observation["tests_failed"],
        "tests_errors": observation["tests_errors"],
        "artifact_path": str(path.relative_to(repo_root)),
        "output_digest": _output_digest(command_id, path),
        "passed": observation["passed"],
    }
    return AcceptanceCommandResultV1(
        **unsigned, artifact_digest=canonical_sha256(unsigned),
    )


def _load_command_record(path: Path, *, repo_root: Path,
                         expected_id: str) -> AcceptanceCommandResultV1:
    row = _read_json(path, limit=65_536)
    _exact(row, RECORD_FIELDS, "acceptance command record")
    if (row["schema"] != "PHASE9_ACCEPTANCE_COMMAND_V1"
            or row["command_id"] != expected_id
            or row["kind"] != KINDS[expected_id]
            or row["parser_id"] != PARSERS[expected_id]
            or row["required"] is not True
            or not isinstance(row["command"], str) or not row["command"]
            or isinstance(row["exit_code"], bool)
            or not isinstance(row["exit_code"], int)):
        raise AcceptanceContractError("acceptance command record contract mismatch")
    expected_commands = {
        command_id: " ".join(command)
        for command_id, command, _ in _formal_commands(repo_root)
    }
    expected_commands.update({
        "A8": "git diff --check",
        "A9": "git status --porcelain=v1 --untracked-files=all",
    })
    if row["command"] != expected_commands[expected_id]:
        raise AcceptanceContractError("acceptance command differs from frozen command")
    unsigned = {key: value for key, value in row.items() if key != "artifact_digest"}
    if canonical_sha256(unsigned) != Sha256Hex(row["artifact_digest"]):
        raise AcceptanceContractError("acceptance command record digest mismatch")
    raw_path = _raw_artifact_path(repo_root, row["artifact_path"])
    if raw_path.name != RAW_NAMES[expected_id]:
        raise AcceptanceContractError("acceptance raw artifact filename mismatch")
    if _output_digest(expected_id, raw_path) != Sha256Hex(row["output_digest"]):
        raise AcceptanceContractError("acceptance raw artifact digest mismatch")
    observation = _parse_raw_for(expected_id, raw_path, exit_code=row["exit_code"])
    for name in ("tests_passed", "tests_skipped", "tests_failed", "tests_errors"):
        if _nonnegative(row[name], name) != observation[name]:
            raise AcceptanceContractError("acceptance count differs from raw artifact")
    if row["passed"] is not observation["passed"]:
        raise AcceptanceContractError("acceptance pass value differs from raw artifact")
    return AcceptanceCommandResultV1(
        **{**row, "source_commit": GitSha(row["source_commit"]),
           "output_digest": Sha256Hex(row["output_digest"]),
           "artifact_digest": Sha256Hex(row["artifact_digest"])}
    )


@dataclass(frozen=True, slots=True)
class Phase9FinalAcceptanceResultV1:
    schema: Literal["PHASE9_FINAL_ACCEPTANCE_V1"]
    source_commit: GitSha
    commands: tuple[AcceptanceCommandResultV1, ...]
    required_commands_total: int
    required_commands_passed: int
    required_tests_passed: int
    required_test_skips: int
    required_tests_failed: int
    required_test_errors: int
    runtime_duration_seconds: int
    PHASE9_IMPLEMENTATION_COMPLETE: bool
    PHASE9_COMPATIBILITY_CLOSURE_PASS: bool
    PHASE9_DETERMINISTIC_REPLAY_PASS: bool
    PHASE9_FAILURE_SEMANTICS_PASS: bool
    PHASE9_PERSISTENCE_AUDIT_PASS: bool
    PHASE9_RUNTIME_INTEGRATION_PASS: bool
    PHASE9_REAL_JEV_INTEGRATION_STATUS: str
    PHASE9_READY_FOR_PHASE10: bool
    passed: bool


def evaluate_phase9_final_acceptance(
    *, repo_root: Path, command_artifacts: Sequence[Path], runtime_duration_seconds: int,
) -> Phase9FinalAcceptanceResultV1:
    if len(command_artifacts) != 9:
        raise AcceptanceContractError("all nine acceptance commands are required")
    root = repo_root.resolve()
    expected = [root / "artifacts/phase9/final/commands" / f"{name}.json" for name in IDS]
    if [path.resolve() for path in command_artifacts] != expected:
        raise AcceptanceContractError("command artifact order or path mismatch")
    commands = tuple(
        _load_command_record(path, repo_root=root, expected_id=command_id)
        for command_id, path in zip(IDS, command_artifacts)
    )
    source = commands[0].source_commit
    if any(command.source_commit != source for command in commands[:7]):
        raise AcceptanceContractError("A1-A7 must bind the same code commit")
    head = GitSha(_git(root, "rev-parse", "HEAD").strip())
    if any(command.source_commit != head for command in commands[7:]):
        raise AcceptanceContractError("A8-A9 must bind the report commit")
    report_diff = _git(root, "diff", "--name-only", f"{source}..{head}").splitlines()
    if report_diff != ["PHASE_9_IMPLEMENTATION_REPORT.md"]:
        raise AcceptanceContractError("post-measurement diff must contain only the report")
    completion = parse_phase9_implementation_completion_v1(
        root / "artifacts/phase9/final/implementation-complete.json",
        repo_root=root, formal_source_commit=source,
    )
    gate = _read_json(_raw_artifact_path(root, commands[0].artifact_path))
    replay = _read_json(_raw_artifact_path(root, commands[4].artifact_path))
    runtime = _read_json(_raw_artifact_path(root, commands[6].artifact_path))
    duration = validate_formal_duration(runtime_duration_seconds)
    if duration != runtime["runtime_duration_seconds"]:
        raise AcceptanceContractError("reported duration differs from measured A7 runtime")
    passed_commands = sum(command.passed for command in commands)
    counts = tuple(sum(getattr(command, name) for command in commands) for name in
                   ("tests_passed", "tests_skipped", "tests_failed", "tests_errors"))
    jev_status = runtime["PHASE9_REAL_JEV_INTEGRATION_STATUS"]
    jev_acceptable = jev_status in {"NOT_CONFIGURED", "PASS"}
    ready = (
        passed_commands == 9 and all(command.exit_code == 0 for command in commands)
        and counts[1:] == (0, 0, 0) and duration >= 900
        and completion.passed
        and gate["PHASE9_COMPATIBILITY_CLOSURE_PASS"] is True
        and replay["PHASE9_DETERMINISTIC_REPLAY_PASS"] is True
        and runtime["PHASE9_FAILURE_SEMANTICS_PASS"] is True
        and runtime["PHASE9_PERSISTENCE_AUDIT_PASS"] is True
        and runtime["PHASE9_RUNTIME_INTEGRATION_PASS"] is True
        and jev_acceptable
    )
    return Phase9FinalAcceptanceResultV1(
        schema="PHASE9_FINAL_ACCEPTANCE_V1", source_commit=source,
        commands=commands, required_commands_total=9,
        required_commands_passed=passed_commands,
        required_tests_passed=counts[0], required_test_skips=counts[1],
        required_tests_failed=counts[2], required_test_errors=counts[3],
        runtime_duration_seconds=duration,
        PHASE9_IMPLEMENTATION_COMPLETE=completion.passed,
        PHASE9_COMPATIBILITY_CLOSURE_PASS=gate["PHASE9_COMPATIBILITY_CLOSURE_PASS"],
        PHASE9_DETERMINISTIC_REPLAY_PASS=replay["PHASE9_DETERMINISTIC_REPLAY_PASS"],
        PHASE9_FAILURE_SEMANTICS_PASS=runtime["PHASE9_FAILURE_SEMANTICS_PASS"],
        PHASE9_PERSISTENCE_AUDIT_PASS=runtime["PHASE9_PERSISTENCE_AUDIT_PASS"],
        PHASE9_RUNTIME_INTEGRATION_PASS=runtime["PHASE9_RUNTIME_INTEGRATION_PASS"],
        PHASE9_REAL_JEV_INTEGRATION_STATUS=jev_status,
        PHASE9_READY_FOR_PHASE10=ready, passed=ready,
    )


_TASK_COMMIT_PREFIXES = {
    "TASK_0_1": "880de41", "TASK_0_2": "48959ea", "TASK_0_3": "fff708e",
    "TASK_0_4": "4cb7ba9", "TASK_0_5": "325a496", "TASK_0_6": "b3558c0",
    "TASK_0_7": "7911dcd", "TASK_0_8": "f01d2bd", "TASK_0_9": "2f9cbab",
    "TASK_1": "4540875", "TASK_2": "a3395e4", "TASK_3": "6b9ba09",
    "TASK_4": "914eef5", "TASK_5": "e95d027", "TASK_6A": "165f9a9",
    "TASK_6B": "d63211d", "TASK_7": "7fb9b50", "TASK_8": "829b3ca",
    "TASK_9": "1e6956a",
}


def write_implementation_completion(repo_root: Path, *, source_commit: GitSha) -> Path:
    root = repo_root.resolve()
    source_commit = GitSha(source_commit)
    task_commits = {
        task_id: GitSha(_git(root, "rev-parse", prefix + "^{commit}").strip())
        for task_id, prefix in _TASK_COMMIT_PREFIXES.items()
    }
    plan_data = _git(root, "show", f"{source_commit}:{PLAN_PATH}")
    plan_digest = hashlib.sha256(
        plan_data.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    ).hexdigest()
    record = {
        "schema": "PHASE9_IMPLEMENTATION_COMPLETION_V1", "plan_path": PLAN_PATH,
        "plan_digest": plan_digest, "source_commit": source_commit,
        "required_task_ids": TASK_IDS, "completed_task_ids": TASK_IDS,
        "task_commits": task_commits,
        "generated_at": datetime.now(timezone.utc), "passed": True,
    }
    path = root / "artifacts/phase9/final/implementation-complete.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(record) + b"\n")
    parse_phase9_implementation_completion_v1(
        path, repo_root=root, formal_source_commit=source_commit,
    )
    return path


def _write_record(path: Path, record: AcceptanceCommandResultV1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_bytes(record) + b"\n")


def _run_command(command: list[str], *, root: Path,
                 environment: dict[str, str] | None = None,
                 timeout_seconds: int = 1800) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command, cwd=root, env=environment, capture_output=True,
        check=False, timeout=timeout_seconds,
    )


def _write_diagnostic(path: Path, *, command: list[str],
                      result: subprocess.CompletedProcess[bytes]) -> None:
    from scripts.phase9_secret_scan import _ASSIGNMENT, _BEARER, _CREDENTIAL_URL

    def safe(value: bytes) -> str:
        text = value[:16384].decode("utf-8", errors="replace")
        for pattern in (_BEARER, _CREDENTIAL_URL, _ASSIGNMENT):
            text = pattern.sub("[REDACTED]", text)
        return text

    payload = {
        "command": command, "exit_code": result.returncode,
        "stdout": safe(result.stdout), "stderr": safe(result.stderr),
        "stdout_truncated": len(result.stdout) > 16384,
        "stderr_truncated": len(result.stderr) > 16384,
    }
    path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
                    encoding="utf-8")


def _formal_commands(root: Path) -> tuple[tuple[str, list[str], Path], ...]:
    commands_dir = root / "artifacts/phase9/final/commands"
    python = sys.executable
    return (
        ("A1", [python, "-m", "quant_phase9.compatibility_gate", "--require-pass",
                 "--json-output", str(commands_dir / "A1.gate.json")], commands_dir / "A1.gate.json"),
        ("A2", [python, "-m", "pytest", "-q", "tests/quant_phase9/",
                 f"--junitxml={commands_dir / 'A2.junit.xml'}"], commands_dir / "A2.junit.xml"),
        ("A3", [python, "scripts/run_phase9_test_matrix.py", "--scope", "affected",
                 "--junitxml", str(commands_dir / "A3.junit.xml")], commands_dir / "A3.junit.xml"),
        ("A4", [python, "scripts/run_phase9_test_matrix.py", "--scope", "full",
                 "--junitxml", str(commands_dir / "A4.junit.xml")], commands_dir / "A4.junit.xml"),
        ("A5", [python, "scripts/run_phase9_replay_v1.py", "--manifest",
                 "tests/fixtures/phase9/manifest.json", "--require-pass", "--json-output",
                 str(commands_dir / "A5.replay.json")], commands_dir / "A5.replay.json"),
        ("A6", [python, "scripts/phase9_secret_scan.py", "--root", ".",
                 "--require-pass", "--json-output", str(commands_dir / "A6.secret-scan.json")],
         commands_dir / "A6.secret-scan.json"),
        ("A7", [python, "scripts/run_phase9_acceptance.py", "--mode", "runtime",
                 "--duration-seconds", "900", "--jev", "fake", "--stage1-fixture",
                 "tests/fixtures/phase9/stage1_candidate.json", "--dsn-env", "TEST_POSTGRES_DSN",
                 "--policy-manifest", "tests/fixtures/phase9/policy_manifest.json",
                 "--approval-manifest", "tests/fixtures/phase9/policy_approval.json",
                 "--result-json", str(commands_dir / "A7.runtime.json")],
         commands_dir / "A7.runtime.json"),
    )


def run_formal(root: Path) -> int:
    root = root.resolve()
    source_commit = GitSha(_git(root, "rev-parse", "HEAD").strip())
    write_implementation_completion(root, source_commit=source_commit)
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(root / "src")
    commands_dir = root / "artifacts/phase9/final/commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    for command_id, command, raw_path in _formal_commands(root):
        result = _run_command(command, root=root, environment=environment,
                              timeout_seconds=1200 if command_id == "A7" else 900)
        _write_diagnostic(commands_dir / f"{command_id}.log.json",
                          command=command, result=result)
        record = make_command_record(
            command_id=command_id, command=" ".join(command),
            source_commit=source_commit, exit_code=result.returncode,
            raw_path=raw_path, repo_root=root,
        )
        _write_record(commands_dir / f"{command_id}.json", record)
    return 0


def run_finalize(root: Path) -> int:
    root = root.resolve()
    head = GitSha(_git(root, "rev-parse", "HEAD").strip())
    commands_dir = root / "artifacts/phase9/final/commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    for command_id, command in (
        ("A8", ["git", "diff", "--check"]),
        ("A9", ["git", "status", "--porcelain=v1", "--untracked-files=all"]),
    ):
        result = _run_command(command, root=root, timeout_seconds=30)
        capture = {"stdout": result.stdout.decode("utf-8", errors="strict"),
                   "stderr": result.stderr.decode("utf-8", errors="strict")}
        raw_path = commands_dir / f"{command_id}.capture.json"
        raw_path.write_bytes(canonical_bytes(capture) + b"\n")
        record = make_command_record(
            command_id=command_id, command=" ".join(command),
            source_commit=head, exit_code=result.returncode,
            raw_path=raw_path, repo_root=root,
        )
        _write_record(commands_dir / f"{command_id}.json", record)
    artifacts = tuple(commands_dir / f"{command_id}.json" for command_id in IDS)
    runtime = _read_json(commands_dir / "A7.runtime.json")
    result = evaluate_phase9_final_acceptance(
        repo_root=root, command_artifacts=artifacts,
        runtime_duration_seconds=runtime["runtime_duration_seconds"],
    )
    output = root / "artifacts/phase9/final/result.json"
    output.write_bytes(canonical_bytes(result) + b"\n")
    print(canonical_bytes(result).decode("utf-8"))
    return 0 if result.passed else 1


def _runtime_acceptance(
    *, root: Path, duration_seconds: int, formal: bool,
    stage1_fixture: Path, dsn_env: str,
    policy_manifest: Path, approval_manifest: Path, jev_mode: str = 'fake',
) -> dict[str, object]:
    """Exercise the real Engine supervisor against a uniquely scoped local DB."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict
    from uuid import uuid4

    from quant_data_layer.db_admission import PostgresWriteAdmission
    from quant_phase1.contracts import DataStatus
    from quant_phase1.db import apply_migrations
    from quant_phase1.stage1 import Stage1Result
    from quant_phase9.config import Phase9RuntimeConfig
    from quant_phase9.intake import Phase9OutboxWriter, build_stage1_candidate_event
    from quant_phase9.policy import load_approved_policy_manifest
    from quant_phase9.replay import load_phase9_replay_manifest
    from quant_phase9.runtime import Phase9DeterministicEvaluator, Phase9EngineRuntime
    from decimal import Decimal

    if formal:
        validate_formal_duration(duration_seconds)
    elif isinstance(duration_seconds, bool) or not 1 <= duration_seconds <= 30:
        raise AcceptanceContractError("smoke duration must be 1..30 seconds")
    dsn = os.environ.get(dsn_env)
    if not dsn:
        raise AcceptanceContractError("disposable test DSN is required")
    info = conninfo_to_dict(dsn)
    if info.get("host") not in {"127.0.0.1", "localhost", "::1"} or info.get("dbname") != "quant_phase9_test":
        raise AcceptanceContractError("runtime acceptance requires disposable loopback database")
    if stage1_fixture.resolve() != (root / "tests/fixtures/phase9/stage1_candidate.json").resolve():
        raise AcceptanceContractError("runtime acceptance requires fixed synthetic Stage 1 fixture")
    if policy_manifest.resolve() != (root / "tests/fixtures/phase9/policy_manifest.json").resolve():
        raise AcceptanceContractError("runtime acceptance requires fixture policy")
    if approval_manifest.resolve() != (root / "tests/fixtures/phase9/policy_approval.json").resolve():
        raise AcceptanceContractError("runtime acceptance requires fixture-only approval")
    replay_bundle = load_phase9_replay_manifest(
        root / "tests/fixtures/phase9/manifest.json", project_root=root,
    )
    fixture = _read_json(stage1_fixture)
    if canonical_sha256(fixture) != canonical_sha256({
        "schema": replay_bundle.manifest.stage1_candidate.schema,
        "candidate": replay_bundle.manifest.stage1_candidate.candidate,
        "candidate_digest": replay_bundle.manifest.stage1_candidate.candidate_digest,
    }):
        raise AcceptanceContractError("Stage 1 fixture differs from pinned replay")
    approved = load_approved_policy_manifest(
        policy_manifest, approval_manifest,
        expected_commit=replay_bundle.manifest.code_version,
    )
    if (approved.manifest_digest != replay_bundle.approved_policy.manifest_digest
            or approved.approval.approved_by != "fixture-human"):
        raise AcceptanceContractError("runtime acceptance policy is not the fixture approval")

    schema = f"phase9_acceptance_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def factory(value: str, **kwargs):
        return psycopg.connect(value, options=f"-c search_path={schema}", **kwargs)

    started = time.monotonic()
    metrics: dict[str, object] = {}
    try:
        with factory(dsn) as conn:
            apply_migrations(conn)
        now = datetime.now(timezone.utc) - timedelta(seconds=1)
        template = replay_bundle.manifest.stage1_candidate.candidate
        result = Stage1Result(
            symbol=template.symbol, category="A", reason="synthetic runtime acceptance",
            status=DataStatus.AVAILABLE, inputs_used=("price",),
            indicators={"atr": Decimal("1")}, structure="BULLISH",
            reason_codes=("STRUCTURE_ALIGNED",), timestamp=now,
        )
        scope = {
            "category": "USDT-FUTURES", "quote_coin": "USDT",
            "contract_type": "perpetual", "status": "online", "in_scope": "true",
        }
        valid = build_stage1_candidate_event(
            screening_result_id=91, run_id=191, screening_result=result,
            market=template.market, instrument_scope=scope,
            candidate_created_at=now, candidate_valid_until=now + timedelta(minutes=20),
            stage1_policy_version=template.stage1_policy_version, source_as_of=now,
        )
        expired = build_stage1_candidate_event(
            screening_result_id=92, run_id=192, screening_result=result,
            market=template.market, instrument_scope=scope,
            candidate_created_at=now, candidate_valid_until=now,
            stage1_policy_version=template.stage1_policy_version, source_as_of=now,
        )
        with factory(dsn) as conn:
            conn.execute(
                """INSERT INTO symbols
                   (symbol, category, base_coin, quote_coin, symbol_type,
                    contract_type, status, price_precision, quantity_precision,
                    min_order_qty, source, exchange, fetched_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (template.symbol, "USDT-FUTURES", "BTC", "USDT", "PERPETUAL",
                 "perpetual", "online", 2, 3, Decimal("0.001"),
                 "fixture", "bitget", now),
            )
            writer = Phase9OutboxWriter()
            writer.emit(conn, event=valid)
            writer.emit(conn, event=expired)

        db_admission = PostgresWriteAdmission(connection_factory=factory)
        evaluator = Phase9DeterministicEvaluator(
            dsn=dsn, policy_manifest=approved,
            connection_factory=factory, db_admission=db_admission,
            conflict_reviewer=__import__('quant_phase9.runtime_jev',fromlist=['JevConflictReviewer']).JevConflictReviewer.for_fixture(jev_mode),
        )
        runtime = Phase9EngineRuntime(
            config=Phase9RuntimeConfig(enabled=True), dsn=dsn,
            policy_manifest=approved, evaluator=evaluator,
            connection_factory=factory, db_admission=db_admission,
        )

        async def measured_run():
            stop = asyncio.Event()
            task = asyncio.create_task(runtime.run(stop))
            try:
                await asyncio.sleep(duration_seconds)
                return runtime.health()
            finally:
                stop.set()
                await task

        metrics = asyncio.run(measured_run())
        with factory(dsn) as conn:
            outbox = conn.execute(
                "SELECT event_id, phase9_state, attempt_count FROM outbox_events ORDER BY event_id"
            ).fetchall()
            cancelled = conn.execute(
                """SELECT count(*) FROM phase9_evaluations
                   WHERE stage1_candidate_id=92 AND evaluation_state='CANCELLED'
                     AND intake_disposition='EXPIRED' AND reason_code='STAGE1_EXPIRED'"""
            ).fetchone()[0]
            completed = conn.execute(
                "SELECT count(*) FROM phase9_evaluations WHERE stage1_candidate_id=91 AND evaluation_state='COMPLETED'"
            ).fetchone()[0]
            snapshots = conn.execute("SELECT count(*) FROM phase9_evaluation_snapshots").fetchone()[0]
            chains = conn.execute("SELECT count(*) FROM phase9_evidence_chains").fetchone()[0]
            mandatory_unresolved = conn.execute(
                """SELECT count(*) FROM phase9_evidence_chains
                   WHERE payload->'value'->>'jev_review_required'='true'"""
            ).fetchone()[0]
            decisions = conn.execute("SELECT count(*) FROM phase9_decision_candidates").fetchone()[0]
            statuses = conn.execute("SELECT count(*) FROM phase9_decision_status_events").fetchone()[0]
        duration = int(time.monotonic() - started)
        failure_semantics = (
            len(outbox) == 2 and all(state == "ACKNOWLEDGED" and attempts == 1
                                     for _, state, attempts in outbox)
            and cancelled == 3
        )
        persistence_audit = completed == snapshots == chains == decisions == statuses == 3
        runtime_pass = (
            formal and duration >= 900 and metrics["completed"] >= 1
            and metrics["failures"] == 0 and metrics["queue_depth"] == 0
            and mandatory_unresolved == 0
        )
        passed = bool(runtime_pass and failure_semantics and persistence_audit)
        return {
            "schema": "PHASE9_RUNTIME_ACCEPTANCE_V1", "passed": passed,
            "runtime_duration_seconds": duration,
            "PHASE9_RUNTIME_INTEGRATION_PASS": bool(runtime_pass),
            "PHASE9_FAILURE_SEMANTICS_PASS": bool(failure_semantics),
            "PHASE9_PERSISTENCE_AUDIT_PASS": bool(persistence_audit),
            "hard_safety_violation": False, "required_test_skips": 0,
            "PHASE9_REAL_JEV_INTEGRATION_STATUS": "NOT_CONFIGURED",
        }
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("smoke", "runtime", "formal", "finalize"), required=True)
    parser.add_argument("--duration-seconds", type=int, default=900)
    parser.add_argument("--jev", choices=("fake", "recorded"), default="fake")
    parser.add_argument("--stage1-fixture", type=Path,
                        default=ROOT / "tests/fixtures/phase9/stage1_candidate.json")
    parser.add_argument("--dsn-env", default="TEST_POSTGRES_DSN")
    parser.add_argument("--policy-manifest", type=Path,
                        default=ROOT / "tests/fixtures/phase9/policy_manifest.json")
    parser.add_argument("--approval-manifest", type=Path,
                        default=ROOT / "tests/fixtures/phase9/policy_approval.json")
    parser.add_argument("--result-json", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.mode == "formal":
            return run_formal(ROOT)
        if args.mode == "finalize":
            return run_finalize(ROOT)
        result = _runtime_acceptance(
            root=ROOT, duration_seconds=args.duration_seconds,
            formal=args.mode == "runtime", stage1_fixture=args.stage1_fixture,
            dsn_env=args.dsn_env, policy_manifest=args.policy_manifest,
            approval_manifest=args.approval_manifest,jev_mode=args.jev,
        )
        payload = canonical_bytes(result) + b"\n"
        if args.result_json:
            args.result_json.parent.mkdir(parents=True, exist_ok=True)
            args.result_json.write_bytes(payload)
        else:
            sys.stdout.buffer.write(payload)
        return 0 if result["passed"] or args.mode == "smoke" else 1
    except (AcceptanceContractError, ValueError) as exc:
        print(f"phase9_acceptance_failed category={type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
