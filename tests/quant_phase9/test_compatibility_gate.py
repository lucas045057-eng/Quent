from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from quant_phase9.canonical import canonical_bytes, canonical_sha256
from quant_phase9.compatibility_gate import (
    COMPATIBILITY_PROTECTED_PATHS,
    SUITE_OUTPUTS,
    SUITE_TESTS,
    CompatibilityGateError,
    CompatibilitySuiteEvidenceV1,
    evaluate_phase9_compatibility_gates,
    ensure_protected_tree_clean,
    main,
    protected_tree_digest,
    parse_junit_counts,
    run_compatibility_suite,
    resolve_phase9_compatibility_closure_commit,
)


ROOT = Path(__file__).parents[2]
MARKER = b'{"schema":"PHASE9_COMPATIBILITY_CLOSURE_MARKER_V1","version":"1"}\n'
SUITE_IDS = (
    "STAGE1_OUTBOX",
    "LIQUIDATION_PARTIAL",
    "IMMUTABLE_SNAPSHOT",
    "DETERMINISTIC_JEV",
)
EXPECTED_PROTECTED_PATHS = (
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


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.name", "Phase9 Test")
    _git(repo, "config", "user.email", "phase9-test@example.invalid")
    for relative in EXPECTED_PROTECTED_PATHS:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(MARKER if relative == "config/phase9/compatibility-closure-v1.json" else b"fixture\n")
    (repo / "unrelated.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "add compatibility closure marker")
    return repo


def _write_valid_artifacts(repo: Path, artifact_dir: Path, *, overrides=None) -> None:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    source_commit = str(resolve_phase9_compatibility_closure_commit(repo))
    tree_digest = str(protected_tree_digest(repo))
    for suite_id in SUITE_IDS:
        payload = {
            "schema": "PHASE9_COMPATIBILITY_SUITE_EVIDENCE_V1",
            "suite_id": suite_id,
            "source_commit": source_commit,
            "protected_tree_digest": tree_digest,
            "command_id": suite_id,
            "tests_passed": 3,
            "tests_skipped": 0,
            "tests_failed": 0,
            "tests_errors": 0,
            "passed": True,
            "generated_at": "2026-09-28T00:00:00.000000Z",
        }
        payload.update((overrides or {}).get(suite_id, {}))
        digest_input = {key: value for key, value in payload.items() if key != "artifact_digest"}
        payload["artifact_digest"] = hashlib.sha256(canonical_bytes(digest_input)).hexdigest()
        path = artifact_dir / SUITE_OUTPUTS[suite_id]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_bytes(payload) + b"\n")


def test_suite_mapping_marker_bytes_and_protected_paths_are_frozen():
    assert tuple(SUITE_TESTS) == SUITE_IDS
    assert SUITE_TESTS == {
        "STAGE1_OUTBOX": (
            "tests/quant_phase9/test_stage1_outbox_gate.py",
            "tests/quant_phase9/test_intake.py",
        ),
        "LIQUIDATION_PARTIAL": ("tests/quant_phase9/test_liquidation_gate.py",),
        "IMMUTABLE_SNAPSHOT": ("tests/quant_phase9/test_snapshot_replay_gate.py",),
        "DETERMINISTIC_JEV": ("tests/quant_phase9/test_jev_gate.py",),
    }
    assert SUITE_OUTPUTS == {
        "STAGE1_OUTBOX": "stage1-outbox.json",
        "LIQUIDATION_PARTIAL": "liquidation-partial.json",
        "IMMUTABLE_SNAPSHOT": "immutable-snapshot.json",
        "DETERMINISTIC_JEV": "deterministic-jev.json",
    }
    marker_path = ROOT / "config/phase9/compatibility-closure-v1.json"
    assert marker_path.read_bytes() == MARKER
    assert json.loads(marker_path.read_bytes()) == {
        "schema": "PHASE9_COMPATIBILITY_CLOSURE_MARKER_V1", "version": "1"
    }
    assert COMPATIBILITY_PROTECTED_PATHS == EXPECTED_PROTECTED_PATHS
    assert tuple(sorted(COMPATIBILITY_PROTECTED_PATHS)) == COMPATIBILITY_PROTECTED_PATHS


def test_first_add_commit_is_resolved_exactly_and_is_in_current_ancestry(tmp_path):
    repo = _init_repo(tmp_path)
    first_add = _git(repo, "rev-parse", "HEAD")
    (repo / "unrelated.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "unrelated.txt")
    _git(repo, "commit", "--quiet", "-m", "unrelated descendant")

    resolved = str(resolve_phase9_compatibility_closure_commit(repo))
    assert resolved == first_add
    assert _git(repo, "merge-base", "--is-ancestor", resolved, "HEAD") == ""
    assert len(resolved) == 40 and resolved == resolved.lower()


def test_repeated_first_add_is_ambiguous_and_fails_closed(tmp_path):
    repo = _init_repo(tmp_path)
    marker = repo / "config/phase9/compatibility-closure-v1.json"
    marker.unlink()
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "-m", "remove marker")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(MARKER)
    _git(repo, "add", str(marker.relative_to(repo)))
    _git(repo, "commit", "--quiet", "-m", "re-add marker")
    with pytest.raises(CompatibilityGateError, match="exactly one"):
        resolve_phase9_compatibility_closure_commit(repo)


def test_protected_tree_digest_is_stable_and_unrelated_changes_do_not_invalidate(tmp_path):
    repo = _init_repo(tmp_path)
    before = protected_tree_digest(repo)
    assert len(str(before)) == 64
    (repo / "unrelated.txt").write_text("uncommitted unrelated change\n", encoding="utf-8")
    ensure_protected_tree_clean(repo)
    assert protected_tree_digest(repo) == before


def test_protected_tree_digest_fails_if_any_frozen_path_is_not_tracked(tmp_path):
    repo = _init_repo(tmp_path)
    missing = "src/quant_phase9/jev.py"
    _git(repo, "rm", "--quiet", missing)
    _git(repo, "commit", "--quiet", "-m", "remove protected file")
    with pytest.raises(CompatibilityGateError, match="not tracked"):
        protected_tree_digest(repo)


@pytest.mark.parametrize("staged", [False, True])
def test_protected_worktree_or_index_edit_is_rejected(tmp_path, staged):
    repo = _init_repo(tmp_path)
    protected = repo / "src/quant_phase9/jev.py"
    protected.write_text("changed\n", encoding="utf-8")
    if staged:
        _git(repo, "add", str(protected.relative_to(repo)))
    with pytest.raises(CompatibilityGateError, match="protected"):
        ensure_protected_tree_clean(repo)


def test_evidence_schema_and_digest_cover_exactly_all_fields():
    names = tuple(field.name for field in fields(CompatibilitySuiteEvidenceV1))
    assert names == (
        "schema", "suite_id", "source_commit", "protected_tree_digest", "command_id",
        "tests_passed", "tests_skipped", "tests_failed", "tests_errors", "artifact_digest",
        "passed", "generated_at",
    )
    generated_at = datetime(2026, 9, 28, tzinfo=timezone.utc)
    unsigned = {
        "schema": "PHASE9_COMPATIBILITY_SUITE_EVIDENCE_V1",
        "suite_id": "DETERMINISTIC_JEV",
        "source_commit": "a" * 40,
        "protected_tree_digest": "b" * 64,
        "command_id": "DETERMINISTIC_JEV",
        "tests_passed": 1,
        "tests_skipped": 0,
        "tests_failed": 0,
        "tests_errors": 0,
        "passed": True,
        "generated_at": generated_at,
    }
    record = CompatibilitySuiteEvidenceV1(
        schema="PHASE9_COMPATIBILITY_SUITE_EVIDENCE_V1",
        suite_id="DETERMINISTIC_JEV", source_commit="a" * 40,
        protected_tree_digest="b" * 64, command_id="DETERMINISTIC_JEV",
        tests_passed=1, tests_skipped=0, tests_failed=0, tests_errors=0,
        artifact_digest=canonical_sha256(unsigned), passed=True,
        generated_at=generated_at,
    )
    payload = record.to_dict()
    assert set(payload) == set(names)
    assert payload["artifact_digest"] == canonical_sha256(unsigned)
    assert CompatibilitySuiteEvidenceV1.from_dict(payload) == record
    assert record.passed is True


def test_aggregate_gate_binds_four_artifacts_marker_commit_and_protected_tree(tmp_path):
    repo = _init_repo(tmp_path)
    artifact_dir = repo / "artifacts/phase9/compatibility"
    _write_valid_artifacts(repo, artifact_dir)

    result = evaluate_phase9_compatibility_gates(repo_root=repo, artifact_dir=artifact_dir)
    payload = result.to_dict()
    assert set(payload) == {
        "schema", "stage1_outbox_pass", "liquidation_partial_pass", "immutable_snapshot_pass",
        "deterministic_jev_pass", "tests_passed", "tests_skipped", "tests_failed", "tests_errors",
        "source_commit", "protected_tree_digest", "PHASE9_COMPATIBILITY_CLOSURE_PASS", "passed",
    }
    assert payload["schema"] == "PHASE9_COMPATIBILITY_GATE_V1"
    assert all(payload[key] for key in (
        "stage1_outbox_pass", "liquidation_partial_pass", "immutable_snapshot_pass",
        "deterministic_jev_pass", "PHASE9_COMPATIBILITY_CLOSURE_PASS", "passed",
    ))
    assert payload["tests_passed"] == 12
    assert payload["tests_skipped"] == payload["tests_failed"] == payload["tests_errors"] == 0
    assert payload["source_commit"] == str(resolve_phase9_compatibility_closure_commit(repo))
    assert payload["protected_tree_digest"] == str(protected_tree_digest(repo))


def test_gate_rejects_artifacts_outside_the_fixed_directory(tmp_path):
    repo = _init_repo(tmp_path)
    artifact_dir = repo / "artifacts/phase9/compatibility"
    _write_valid_artifacts(repo, artifact_dir)
    with pytest.raises(CompatibilityGateError, match="fixed artifact directory"):
        evaluate_phase9_compatibility_gates(
            repo_root=repo, artifact_dir=repo / "elsewhere/compatibility",
        )


def test_gate_rejects_symlinked_fixed_artifact_directory(tmp_path):
    repo = _init_repo(tmp_path)
    artifact_dir = repo / "artifacts/phase9/compatibility"
    _write_valid_artifacts(repo, artifact_dir)
    real_dir = repo / "artifacts/phase9/compatibility-real"
    artifact_dir.rename(real_dir)
    artifact_dir.symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(CompatibilityGateError, match="symlink"):
        evaluate_phase9_compatibility_gates(repo_root=repo, artifact_dir=artifact_dir)


def test_gate_rejects_symlinked_evidence_file(tmp_path):
    repo = _init_repo(tmp_path)
    artifact_dir = repo / "artifacts/phase9/compatibility"
    _write_valid_artifacts(repo, artifact_dir)
    artifact = artifact_dir / SUITE_OUTPUTS["STAGE1_OUTBOX"]
    outside = repo / "outside-stage1.json"
    outside.write_bytes(artifact.read_bytes())
    artifact.unlink()
    artifact.symlink_to(outside)
    with pytest.raises(CompatibilityGateError, match="symlink"):
        evaluate_phase9_compatibility_gates(repo_root=repo, artifact_dir=artifact_dir)


@pytest.mark.parametrize(
    "overrides",
    [
        {"STAGE1_OUTBOX": {"tests_skipped": 1, "passed": False}},
        {"LIQUIDATION_PARTIAL": {"tests_failed": 1, "passed": False}},
        {"IMMUTABLE_SNAPSHOT": {"tests_errors": 1, "passed": False}},
        {"DETERMINISTIC_JEV": {"tests_passed": 0, "passed": False}},
    ],
)
def test_gate_returns_valid_failure_for_skips_failures_errors_or_empty_tests(tmp_path, overrides):
    repo = _init_repo(tmp_path)
    artifact_dir = repo / "artifacts/phase9/compatibility"
    _write_valid_artifacts(repo, artifact_dir, overrides=overrides)
    result = evaluate_phase9_compatibility_gates(repo_root=repo, artifact_dir=artifact_dir)
    assert result.passed is False
    assert result.PHASE9_COMPATIBILITY_CLOSURE_PASS is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"STAGE1_OUTBOX": {"command_id": "LIQUIDATION_PARTIAL"}},
        {"STAGE1_OUTBOX": {"source_commit": "d" * 40}},
        {"STAGE1_OUTBOX": {"protected_tree_digest": "e" * 64}},
    ],
)
def test_gate_rejects_invalid_command_or_anchor_binding(tmp_path, overrides):
    repo = _init_repo(tmp_path)
    artifact_dir = repo / "artifacts/phase9/compatibility"
    _write_valid_artifacts(repo, artifact_dir, overrides=overrides)
    with pytest.raises(CompatibilityGateError):
        evaluate_phase9_compatibility_gates(repo_root=repo, artifact_dir=artifact_dir)


@pytest.mark.parametrize(
    "corruption", ["missing", "invalid-json", "wrong-schema", "wrong-digest", "bool-count", "unknown-field"]
)
def test_missing_or_corrupt_evidence_is_invalid_and_cli_returns_three(tmp_path, corruption, capsys):
    repo = _init_repo(tmp_path)
    artifact_dir = repo / "artifacts/phase9/compatibility"
    _write_valid_artifacts(repo, artifact_dir)
    path = artifact_dir / SUITE_OUTPUTS["DETERMINISTIC_JEV"]
    if corruption == "missing":
        path.unlink()
    else:
        payload = json.loads(path.read_bytes())
        if corruption == "invalid-json":
            path.write_text("{", encoding="utf-8")
        elif corruption == "wrong-schema":
            payload["schema"] = "wrong"
            path.write_bytes(canonical_bytes(payload) + b"\n")
        elif corruption == "wrong-digest":
            payload["artifact_digest"] = "0" * 64
            path.write_bytes(canonical_bytes(payload) + b"\n")
        elif corruption == "unknown-field":
            payload["unexpected"] = "not part of the frozen schema"
            path.write_bytes(canonical_bytes(payload) + b"\n")
        else:
            payload["tests_passed"] = True
            path.write_bytes(canonical_bytes(payload) + b"\n")

    assert main(["--require-pass"], repo_root=repo, artifact_dir=artifact_dir) == 3
    assert "PHASE9_COMPATIBILITY_CLOSURE_PASS" not in capsys.readouterr().out


def test_cli_exit_codes_for_valid_pass_valid_failure_and_invalid_evidence(tmp_path):
    repo = _init_repo(tmp_path)
    artifact_dir = repo / "artifacts/phase9/compatibility"
    _write_valid_artifacts(repo, artifact_dir)
    assert main(["--require-pass"], repo_root=repo, artifact_dir=artifact_dir) == 0

    _write_valid_artifacts(repo, artifact_dir, overrides={"STAGE1_OUTBOX": {"tests_skipped": 1, "passed": False}})
    assert main(["--require-pass"], repo_root=repo, artifact_dir=artifact_dir) == 2

    (artifact_dir / SUITE_OUTPUTS["STAGE1_OUTBOX"]).unlink()
    assert main(["--require-pass"], repo_root=repo, artifact_dir=artifact_dir) == 3


def test_cli_json_output_matches_evaluated_gate_for_formal_acceptance(tmp_path, capsys):
    repo = _init_repo(tmp_path)
    artifact_dir = repo / "artifacts/phase9/compatibility"
    _write_valid_artifacts(repo, artifact_dir)
    output = repo / "artifacts/phase9/final/A1.gate.json"
    assert main(["--require-pass", "--json-output", str(output.relative_to(repo))],
                repo_root=repo, artifact_dir=artifact_dir) == 0
    expected = evaluate_phase9_compatibility_gates(repo_root=repo, artifact_dir=artifact_dir)
    assert output.read_bytes() == canonical_bytes(expected) + b"\n"
    assert output.read_text() == capsys.readouterr().out

    _write_valid_artifacts(repo, artifact_dir,
                           overrides={"STAGE1_OUTBOX": {"tests_skipped": 1, "passed": False}})
    assert main(["--require-pass", "--json-output", str(output)],
                repo_root=repo, artifact_dir=artifact_dir) == 2
    assert json.loads(output.read_bytes())["passed"] is False

    (artifact_dir / SUITE_OUTPUTS["STAGE1_OUTBOX"]).unlink()
    assert main(["--require-pass", "--json-output", str(output)],
                repo_root=repo, artifact_dir=artifact_dir) == 3
    assert not output.exists(), "invalid evidence must not retain an old gate result"


def test_cli_json_output_rejects_external_paths_and_symlink_parents(tmp_path):
    repo = _init_repo(tmp_path)
    artifact_dir = repo / "artifacts/phase9/compatibility"
    _write_valid_artifacts(repo, artifact_dir)
    outside = tmp_path / "outside"
    outside.mkdir()
    output = outside / "gate.json"
    assert main(["--require-pass", "--json-output", str(output)],
                repo_root=repo, artifact_dir=artifact_dir) == 3
    assert not output.exists()
    linked = repo / "artifacts/phase9/linked"
    linked.symlink_to(outside, target_is_directory=True)
    assert main(["--require-pass", "--json-output", str(linked / "gate.json")],
                repo_root=repo, artifact_dir=artifact_dir) == 3
    assert not output.exists()


def test_gate_rejects_protected_tree_change_but_accepts_unrelated_file(tmp_path):
    repo = _init_repo(tmp_path)
    artifact_dir = repo / "artifacts/phase9/compatibility"
    _write_valid_artifacts(repo, artifact_dir)
    (repo / "unrelated.txt").write_text("other change\n", encoding="utf-8")
    assert evaluate_phase9_compatibility_gates(repo_root=repo, artifact_dir=artifact_dir).passed

    (repo / "src/quant_phase9/jev.py").write_text("protected change\n", encoding="utf-8")
    with pytest.raises(CompatibilityGateError, match="protected"):
        evaluate_phase9_compatibility_gates(repo_root=repo, artifact_dir=artifact_dir)


def test_junit_counts_are_derived_from_testcase_outcomes_not_process_exit_code(tmp_path):
    junit = tmp_path / "results.xml"
    junit.write_text(
        '<testsuites tests="5" failures="1" errors="1" skipped="1">'
        '<testsuite><testcase/><testcase/><testcase><failure/></testcase>'
        '<testcase><error/></testcase><testcase><skipped/></testcase></testsuite>'
        '</testsuites>',
        encoding="utf-8",
    )
    assert parse_junit_counts(junit) == (2, 1, 1, 1)


def test_junit_parser_rejects_missing_or_malformed_report(tmp_path):
    with pytest.raises(CompatibilityGateError):
        parse_junit_counts(tmp_path / "missing.xml")
    malformed = tmp_path / "bad.xml"
    malformed.write_text("<testsuites>", encoding="utf-8")
    with pytest.raises(CompatibilityGateError):
        parse_junit_counts(malformed)


def test_suite_runner_uses_fixed_mapping_and_writes_bound_canonical_evidence(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    output = repo / "artifacts/phase9/compatibility/deterministic-jev.json"

    def fake_pytest(command, junit_path, timeout_seconds, *, cwd):
        assert command[4:] == list(SUITE_TESTS["DETERMINISTIC_JEV"])
        assert timeout_seconds > 0
        assert Path(cwd) == repo
        junit_path.write_text(
            '<testsuites tests="2" failures="0" errors="0" skipped="0">'
            '<testsuite><testcase/><testcase/></testsuite></testsuites>',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr("quant_phase9.compatibility_gate._execute_pytest", fake_pytest)
    record = run_compatibility_suite(
        suite_id="DETERMINISTIC_JEV", output=output, repo_root=repo,
    )
    assert record.passed is True
    assert record.tests_passed == 2
    assert record.source_commit == resolve_phase9_compatibility_closure_commit(repo)
    assert record.protected_tree_digest == protected_tree_digest(repo)
    assert output.read_bytes() == canonical_bytes(record) + b"\n"


def test_suite_runner_does_not_accept_nonzero_pytest_exit_with_clean_junit_counts(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    output = repo / "artifacts/phase9/compatibility/deterministic-jev.json"

    def fake_pytest(command, junit_path, timeout_seconds, *, cwd):
        junit_path.write_text(
            '<testsuites><testsuite><testcase/></testsuite></testsuites>', encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 5, b"", b"")

    monkeypatch.setattr("quant_phase9.compatibility_gate._execute_pytest", fake_pytest)
    record = run_compatibility_suite(
        suite_id="DETERMINISTIC_JEV", output=output, repo_root=repo,
    )
    assert record.tests_passed == 1
    assert record.tests_errors == 1
    assert record.passed is False


def test_suite_runner_rejects_unmapped_output_path(tmp_path):
    repo = _init_repo(tmp_path)
    with pytest.raises(CompatibilityGateError, match="output path"):
        run_compatibility_suite(
            suite_id="DETERMINISTIC_JEV", output=repo / "other.json", repo_root=repo,
        )


def test_suite_runner_removes_old_evidence_before_reexecution(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    output = repo / "artifacts/phase9/compatibility/deterministic-jev.json"
    output.parent.mkdir(parents=True)
    output.write_text("old passing evidence", encoding="utf-8")

    def fake_pytest(command, junit_path, timeout_seconds, *, cwd):
        assert not output.exists()
        junit_path.write_text(
            '<testsuites><testsuite><testcase/></testsuite></testsuites>', encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr("quant_phase9.compatibility_gate._execute_pytest", fake_pytest)
    record = run_compatibility_suite(
        suite_id="DETERMINISTIC_JEV", output=output, repo_root=repo,
    )
    assert record.passed is True
