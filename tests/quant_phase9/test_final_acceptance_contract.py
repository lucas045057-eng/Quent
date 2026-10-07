from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.run_phase9_acceptance import (
    AcceptanceContractError,
    PLAN_PATH,
    TASK_IDS,
    _formal_commands,
    _load_command_record,
    make_command_record,
    parse_a2_pytest_junit_v1,
    parse_a5_replay_v1,
    parse_a6_secret_scan_v1,
    parse_a7_runtime_acceptance_v1,
    parse_phase9_implementation_completion_v1,
    validate_formal_duration,
)
from scripts.phase9_secret_scan import scan_repository, scan_text


def test_secret_scan_covers_new_execution_adapter_and_research_sources(tmp_path):
    import subprocess
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    path=tmp_path/'src/quant_nautilus/route.py'
    path.parent.mkdir(parents=True)
    path.write_text('api_key = "'+'sensitive'+'0123456789"\n')
    subprocess.run(['git','-C',str(tmp_path),'add','.'],check=True)
    subprocess.run(['git','-C',str(tmp_path),'-c','user.name=fixture','-c','user.email=fixture@example.invalid',
        'commit','-qm','fixture'],check=True)
    assert scan_repository(tmp_path)['findings_count']==1


def test_c1_task9_names_the_actual_milestone_commit_not_later_integration_head():
    import subprocess
    from scripts.run_phase9_acceptance import write_implementation_completion, _TASK_COMMIT_PREFIXES
    root=Path(__file__).resolve().parents[2]
    # Source-snapshot uploads do not contain the original Phase9 milestone
    # history. Keep this evidence test opt-in to that history; do not replace
    # historical commits with the later snapshot or manufacture acceptance.
    for prefix in _TASK_COMMIT_PREFIXES.values():
        result = subprocess.run(
            ['git','-C',str(root),'rev-parse','--verify',prefix+'^{commit}'],
            capture_output=True, check=False,
        )
        if result.returncode:
            pytest.skip('Original Phase9 milestone Git history is unavailable in this source snapshot')
    def git(*args):
        return subprocess.check_output(['git','-C',str(root),*args],text=True).strip()
    head=git('rev-parse','HEAD')
    actual=git('log','--diff-filter=A','--format=%H','--','scripts/run_phase9_acceptance.py')
    record=json.loads(write_implementation_completion(root,source_commit=head).read_text())
    assert record['source_commit']==head
    assert record['task_commits']['TASK_9']==actual
from scripts.run_phase9_test_matrix import (
    A4_FIXTURE_NODE,
    merge_junit,
    validate_a4_fixture_dsn,
    validate_a4_fixture_junit,
)


def test_formal_duration_has_unforgable_900_second_floor():
    assert validate_formal_duration(900) == 900
    with pytest.raises(AcceptanceContractError, match="INVALID_ACCEPTANCE_DURATION"):
        validate_formal_duration(899)
    with pytest.raises(AcceptanceContractError, match="INVALID_ACCEPTANCE_DURATION"):
        validate_formal_duration(True)


def test_junit_parser_rejects_skips_errors_and_double_counting(tmp_path):
    path = tmp_path / "report.xml"
    path.write_text(
        '<testsuites tests="2"><testsuite tests="1" errors="0" failures="0" skipped="0"/>'
        '<testsuite tests="1" errors="0" failures="0" skipped="0"/></testsuites>',
        encoding="utf-8",
    )
    result = parse_a2_pytest_junit_v1(path, exit_code=0)
    assert result["tests_passed"] == 2
    path.write_text('<testsuite tests="1" errors="0" failures="0" skipped="1"/>', encoding="utf-8")
    with pytest.raises(AcceptanceContractError):
        parse_a2_pytest_junit_v1(path, exit_code=0)
    path.write_text("<testsuites>", encoding="utf-8")
    with pytest.raises(AcceptanceContractError):
        parse_a2_pytest_junit_v1(path, exit_code=0)


def test_replay_parser_requires_exact_six_hashes_and_no_external_calls(tmp_path):
    path = tmp_path / "replay.json"
    payload = {
        "schema": "PHASE9_REPLAY_RESULT_V1", "passed": True,
        "PHASE9_DETERMINISTIC_REPLAY_PASS": True,
        "hashes_compared": [
            "evaluation_snapshot_hash", "evidence_items_digest", "evidence_chain_digest",
            "pattern_matches_digest", "decision_candidate_digest", "input_snapshot_hash",
        ],
        "hashes_match": True, "provider_calls": 0, "ai_calls": 0,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert parse_a5_replay_v1(path, exit_code=0)["passed"] is True
    payload["provider_calls"] = 1
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AcceptanceContractError):
        parse_a5_replay_v1(path, exit_code=0)


def test_completion_artifact_missing_fails_closed(tmp_path):
    with pytest.raises(AcceptanceContractError):
        parse_phase9_implementation_completion_v1(
            tmp_path / "missing.json", repo_root=tmp_path,
            formal_source_commit="a" * 40,
        )


def test_secret_scan_reports_categories_without_retaining_values():
    bearer = "Authorization: Bearer " + "abcdefghijklmnopqrstuvwxyz123456"
    assert scan_text(bearer) == ("AUTHORIZATION_BEARER",)
    credential = 'password = "' + 'abcdef0123456789abcdef' + '"'
    assert scan_text(credential) == ("CREDENTIAL_ASSIGNMENT",)
    assert scan_text('"account_id":"privateacct123"', private_context=True) == (
        "PRIVATE_ACCOUNT_ORDER_PAYLOAD",
    )
    assert scan_text('secret = "postgresql://user:fake@localhost/db"', synthetic=True) == ()


def test_secret_scan_preserves_test_path_context_in_tracked_diff(tmp_path):
    import subprocess

    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    path = tmp_path / 'tests/quant_phase9/test_fixture.py'
    path.parent.mkdir(parents=True)
    path.write_text('secret = "postgresql://user:fake@localhost/db"\n', encoding='utf-8')
    subprocess.run(['git', '-C', str(tmp_path), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(tmp_path), '-c', 'user.name=Fixture',
                    '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'baseline'], check=True)
    path.write_text('secret = "postgresql://user:fake@localhost/new"\n', encoding='utf-8')
    assert scan_repository(tmp_path)['findings_count'] == 0


def test_database_matrix_merges_real_leaf_suite_counts(tmp_path):
    first = tmp_path / "core.xml"
    second = tmp_path / "phase8.xml"
    output = tmp_path / "combined.xml"
    first.write_text('<testsuites><testsuite tests="2" failures="0" errors="0" skipped="0"/></testsuites>', encoding="utf-8")
    second.write_text('<testsuite tests="3" failures="0" errors="0" skipped="0"/>', encoding="utf-8")
    merge_junit((first, second), output)
    assert parse_a2_pytest_junit_v1(output, exit_code=0)["tests_passed"] == 5


def test_a4_fixture_dsn_is_loopback_exact_database_and_separate_server():
    main = "postgresql://quant_test:secret@127.0.0.1:55443/quant_phase9_test"
    fixture = "postgresql://quant_test:secret@127.0.0.1:55444/quant_phase9_test"
    assert validate_a4_fixture_dsn(main, fixture)["port"] == "55444"
    with pytest.raises(ValueError, match="A4_FIXTURE_DSN"):
        validate_a4_fixture_dsn(main, "postgresql://quant_test:fixture-only@remote:55444/quant_phase9_test")
    with pytest.raises(ValueError, match="A4_FIXTURE_DSN"):
        validate_a4_fixture_dsn(main, "postgresql://quant_test:secret@127.0.0.1:55443/quant_phase9_test")
    with pytest.raises(ValueError, match="A4_FIXTURE_DSN"):
        validate_a4_fixture_dsn(main, "postgresql://quant_test:secret@127.0.0.1:55444/quant")


def test_a4_fixture_junit_requires_one_pass_without_core_duplicate_or_skip(tmp_path):
    fixture = tmp_path / "fixture.xml"
    core = tmp_path / "core.xml"
    phase8 = tmp_path / "phase8.xml"
    aggregate = tmp_path / "aggregate.xml"
    node_class, node_name = A4_FIXTURE_NODE.split("::")
    node_class = node_class.removesuffix(".py").replace("/", ".")
    fixture.write_text(
        f'<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase classname="{node_class}" name="{node_name}"/></testsuite>',
        encoding="utf-8",
    )
    core.write_text('<testsuite tests="2" failures="0" errors="0" skipped="0"/>', encoding="utf-8")
    phase8.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"/>', encoding="utf-8")
    merge_junit((fixture, core, phase8), aggregate)
    counts = validate_a4_fixture_junit(fixture, core, phase8, aggregate)
    assert counts == {"execution_count": 1, "pass_count": 1, "skip_count": 0}
    assert parse_a2_pytest_junit_v1(aggregate, exit_code=0)["tests_passed"] == 4

    core.write_text(
        f'<testsuite tests="1" failures="0" errors="0" skipped="1"><testcase classname="{node_class}" name="{node_name}"><skipped message="opt-in"/></testcase></testsuite>',
        encoding="utf-8",
    )
    merge_junit((fixture, core, phase8), aggregate)
    with pytest.raises(ValueError, match="A4_FIXTURE_NODE"):
        validate_a4_fixture_junit(fixture, core, phase8, aggregate)


def test_completion_recomputes_plan_digest_task_ancestry_and_passed(tmp_path):
    import hashlib
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Fixture")
    plan = repo / PLAN_PATH
    plan.parent.mkdir(parents=True)
    plan.write_text("approved fixture plan\n", encoding="utf-8")
    commits = {}
    for task_id in TASK_IDS:
        (repo / f"{task_id}.txt").write_text(task_id, encoding="utf-8")
        git("add", ".")
        git("commit", "-qm", task_id)
        commits[task_id] = git("rev-parse", "HEAD")
    source = commits["TASK_9"]
    payload = {
        "schema": "PHASE9_IMPLEMENTATION_COMPLETION_V1",
        "plan_path": PLAN_PATH,
        "plan_digest": hashlib.sha256(plan.read_bytes()).hexdigest(),
        "source_commit": source,
        "required_task_ids": list(TASK_IDS),
        "completed_task_ids": list(TASK_IDS),
        "task_commits": commits,
        "generated_at": "2026-09-28T00:00:00Z", "passed": True,
    }
    artifact = tmp_path / "C1.json"

    def verify(value):
        artifact.write_text(json.dumps(value), encoding="utf-8")
        return parse_phase9_implementation_completion_v1(
            artifact, repo_root=repo, formal_source_commit=source,
        )

    assert verify(payload).passed is True
    for change in (
        {"passed": False},
        {"plan_digest": "0" * 64},
        {"completed_task_ids": list(TASK_IDS[:-1])},
        {"task_commits": {**commits, "TASK_9": commits["TASK_8"]}},
    ):
        with pytest.raises(AcceptanceContractError):
            verify({**payload, **change})


def test_a6_and_a7_require_exact_machine_gates(tmp_path):
    secret_path = tmp_path / "A6.json"
    secret = {
        "schema": "PHASE9_SECRET_SCAN_V1", "passed": True,
        "secret_leak_found": False, "findings_count": 0,
    }
    secret_path.write_text(json.dumps(secret), encoding="utf-8")
    assert parse_a6_secret_scan_v1(secret_path, exit_code=0)["passed"] is True
    secret["findings_count"] = 1
    secret_path.write_text(json.dumps(secret), encoding="utf-8")
    with pytest.raises(AcceptanceContractError):
        parse_a6_secret_scan_v1(secret_path, exit_code=0)

    runtime_path = tmp_path / "A7.json"
    runtime = {
        "schema": "PHASE9_RUNTIME_ACCEPTANCE_V1", "passed": True,
        "runtime_duration_seconds": 900,
        "PHASE9_RUNTIME_INTEGRATION_PASS": True,
        "PHASE9_FAILURE_SEMANTICS_PASS": True,
        "PHASE9_PERSISTENCE_AUDIT_PASS": True,
        "hard_safety_violation": False, "required_test_skips": 0,
        "PHASE9_REAL_JEV_INTEGRATION_STATUS": "NOT_CONFIGURED",
    }
    runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
    assert parse_a7_runtime_acceptance_v1(runtime_path, exit_code=0)["passed"] is True
    runtime["runtime_duration_seconds"] = 899
    runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
    with pytest.raises(AcceptanceContractError, match="INVALID_ACCEPTANCE_DURATION"):
        parse_a7_runtime_acceptance_v1(runtime_path, exit_code=0)


def test_command_record_rechecks_raw_digest_and_frozen_command(tmp_path):
    from quant_phase9.canonical import canonical_bytes

    raw_path = tmp_path / "artifacts/phase9/final/commands/A5.replay.json"
    raw_path.parent.mkdir(parents=True)
    payload = {
        "schema": "PHASE9_REPLAY_RESULT_V1", "passed": True,
        "PHASE9_DETERMINISTIC_REPLAY_PASS": True,
        "hashes_compared": [
            "evaluation_snapshot_hash", "evidence_items_digest", "evidence_chain_digest",
            "pattern_matches_digest", "decision_candidate_digest", "input_snapshot_hash",
        ],
        "hashes_match": True, "provider_calls": 0, "ai_calls": 0,
    }
    raw_path.write_text(json.dumps(payload), encoding="utf-8")
    command = next(" ".join(parts) for name, parts, _ in _formal_commands(tmp_path) if name == "A5")
    record = make_command_record(
        command_id="A5", command=command, source_commit="a" * 40,
        exit_code=0, raw_path=raw_path, repo_root=tmp_path,
    )
    record_path = raw_path.with_name("A5.json")
    record_path.write_bytes(canonical_bytes(record) + b"\n")
    assert _load_command_record(record_path, repo_root=tmp_path, expected_id="A5").passed
    payload["provider_calls"] = 1
    raw_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(AcceptanceContractError):
        _load_command_record(record_path, repo_root=tmp_path, expected_id="A5")


def test_remote_rejection_dsn_fixture_is_marked_without_ignoring_unmarked_remote_urls():
    import ast
    from pathlib import Path

    module = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    remote_fixture = next(
        node.value
        for node in ast.walk(module)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("postgresql://")
        and "@remote:" in node.value
        and "/quant_phase9_test" in node.value
    )
    assert scan_text(remote_fixture, synthetic=True) == ()

    unmarked_remote = "postgresql://test:" + ("x" * 24) + "@remote:55444/quant_phase9_test"
    assert scan_text(unmarked_remote, synthetic=True) == ("CREDENTIAL_URL",)
