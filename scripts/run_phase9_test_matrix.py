"""Run the Phase 9 test matrix.

For --scope full (Final RC A4), the caller must provide
A4_FIXTURE_TEST_POSTGRES_DSN for a separate fresh loopback PostgreSQL instance.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
from typing import Sequence
import xml.etree.ElementTree as ET

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo


ROOT = Path(__file__).resolve().parents[1]
PHASE8 = (
    "tests/test_phase8_migrations.py", "tests/test_phase8_persistence.py",
    "tests/test_phase8_replay.py",
)
OPTIONAL_LIVE = (
    "tests/contract/test_phase2_live.py",
    "tests/contract/test_phase3_public_live.py",
    "tests/contract/test_phase4_public_live.py",
)
AFFECTED = (
    "tests/test_stage1.py", "tests/test_repository_integration.py", "tests/test_pipeline.py",
    "tests/test_runtime.py", "tests/test_phase2_contracts.py",
    "tests/test_phase2_normalization.py", "tests/test_phase2_persistence.py",
    "tests/test_phase2_runtime.py", "tests/test_phase3_contracts.py",
    "tests/test_phase3_flow_windows.py", "tests/test_phase3_persistence.py",
    "tests/test_phase4_contracts.py", "tests/test_phase4_liquidation.py",
    "tests/test_phase4_persistence.py", "tests/test_phase4_runtime.py",
    "tests/test_phase5_contracts.py", "tests/test_phase5_persistence.py",
    "tests/test_phase5_runtime.py", "tests/test_phase6_ai.py",
    "tests/test_phase6_contract_v1.py", "tests/test_phase6_prompts.py",
    "tests/test_phase6_runtime_integration.py", "tests/test_phase7_contracts.py",
    "tests/test_phase7_persistence.py", "tests/test_phase7_runtime_integration.py",
    "tests/test_phase8_contracts.py", "tests/test_phase8_persistence.py",
    "tests/test_phase8_runtime_integration.py", "tests/test_data_layer_replay.py",
    "tests/test_data_layer_replay_runner.py",
)


A4_FIXTURE_NODE = (
    "tests/quant_realtime_paper/test_real_public_paper_db_integration.py::"
    "test_fixture_database_is_loopback_fresh_utc_and_noncanonical"
)
A4_FIXTURE_DSN_ENV = "A4_FIXTURE_TEST_POSTGRES_DSN"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def validate_a4_fixture_dsn(main_dsn: str, fixture_dsn: str) -> dict[str, str]:
    try:
        main_info = conninfo_to_dict(main_dsn)
        fixture_info = conninfo_to_dict(fixture_dsn)
    except (TypeError, ValueError) as exc:
        raise ValueError("A4_FIXTURE_DSN_INVALID") from exc
    main_hosts = [host.strip().lower() for host in (main_info.get("host") or "").split(",")]
    fixture_hosts = [host.strip().lower() for host in (fixture_info.get("host") or "").split(",")]
    if (
        main_info.get("dbname") != "quant_phase9_test"
        or not main_hosts
        or any(host not in LOOPBACK_HOSTS for host in main_hosts)
        or fixture_info.get("dbname") != "quant_phase9_test"
        or not fixture_hosts
        or any(host not in LOOPBACK_HOSTS for host in fixture_hosts)
        or str(main_info.get("port") or "5432") == str(fixture_info.get("port") or "5432")
    ):
        raise ValueError("A4_FIXTURE_DSN_MUST_BE_SEPARATE_LOOPBACK_QUANT_PHASE9_TEST")
    return fixture_info


def _junit_leaf_suites(path: Path) -> list[ET.Element]:
    root = ET.parse(path).getroot()
    if root.tag == "testsuite":
        return [root]
    if root.tag != "testsuites":
        raise ValueError("unexpected JUnit root")
    suites = [suite for suite in root.iter("testsuite") if not list(suite.findall("testsuite"))]
    if not suites:
        raise ValueError("empty JUnit report")
    return suites


def junit_counts(path: Path) -> dict[str, int]:
    totals = {"tests": 0, "skipped": 0, "failures": 0, "errors": 0}
    for suite in _junit_leaf_suites(path):
        for key in totals:
            value = suite.get(key)
            if value is None or not value.isdigit():
                raise ValueError(f"malformed JUnit count: {key}")
            totals[key] += int(value)
    totals["passed"] = totals["tests"] - totals["skipped"] - totals["failures"] - totals["errors"]
    return totals


def _a4_fixture_cases(path: Path) -> list[ET.Element]:
    module_path, case_name = A4_FIXTURE_NODE.split("::")
    expected_class = module_path.removesuffix(".py").replace("/", ".")
    root = ET.parse(path).getroot()
    return [
        case for case in root.iter("testcase")
        if case.get("classname") == expected_class and case.get("name") == case_name
    ]


def validate_a4_fixture_junit(
    fixture_junit: Path,
    core_junit: Path,
    phase8_junit: Path,
    aggregate_junit: Path,
) -> dict[str, int]:
    fixture_cases = _a4_fixture_cases(fixture_junit)
    core_cases = _a4_fixture_cases(core_junit)
    phase8_cases = _a4_fixture_cases(phase8_junit)
    aggregate_cases = _a4_fixture_cases(aggregate_junit)
    if len(fixture_cases) != 1 or core_cases or phase8_cases or len(aggregate_cases) != 1:
        raise ValueError("A4_FIXTURE_NODE_MUST_EXECUTE_EXACTLY_ONCE_IN_FIXTURE_CHILD")
    case = fixture_cases[0]
    skip_count = int(case.find("skipped") is not None)
    failure_count = int(case.find("failure") is not None)
    error_count = int(case.find("error") is not None)
    pass_count = int(not (skip_count or failure_count or error_count))
    if (pass_count, skip_count, failure_count, error_count) != (1, 0, 0, 0):
        raise ValueError("A4_FIXTURE_NODE_DID_NOT_PASS_EXACTLY_ONCE")
    totals = junit_counts(aggregate_junit)
    if totals["skipped"] or totals["failures"] or totals["errors"] or totals["passed"] != totals["tests"]:
        raise ValueError("A4_AGGREGATE_REQUIRES_ZERO_SKIPS_FAILURES_ERRORS")
    return {"execution_count": 1, "pass_count": pass_count, "skip_count": skip_count}


def assert_fresh_fixture_server(main_dsn: str, fixture_dsn: str) -> None:
    fixture_admin_dsn = make_conninfo(fixture_dsn, dbname="postgres")
    with psycopg.connect(main_dsn) as main:
        main_database = main.execute("SELECT current_database()").fetchone()[0]
        main_started = main.execute("SELECT pg_postmaster_start_time()").fetchone()[0]
    with psycopg.connect(fixture_admin_dsn) as fixture:
        current_database = fixture.execute("SELECT current_database()").fetchone()[0]
        fixture_started = fixture.execute("SELECT pg_postmaster_start_time()").fetchone()[0]
        timezone = fixture.execute("SHOW TIME ZONE").fetchone()[0]
        exists = fixture.execute(
            "SELECT 1 FROM pg_database WHERE datname='quant_phase9_test'"
        ).fetchone() is not None
    if main_database != "quant_phase9_test" or current_database != "postgres":
        raise ValueError("A4_FIXTURE_POSTGRES_IDENTITY_INVALID")
    if main_started == fixture_started:
        raise ValueError("A4_FIXTURE_POSTGRES_MUST_BE_SEPARATE_SERVER")
    if timezone != "UTC":
        raise ValueError("A4_FIXTURE_POSTGRES_TIME_ZONE_MUST_BE_UTC")
    if exists:
        raise ValueError("A4_FIXTURE_DATABASE_MUST_BE_ABSENT_BEFORE_RUN")


def assert_fixture_database_dropped(fixture_dsn: str) -> None:
    fixture_admin_dsn = make_conninfo(fixture_dsn, dbname="postgres")
    with psycopg.connect(fixture_admin_dsn) as fixture:
        exists = fixture.execute(
            "SELECT 1 FROM pg_database WHERE datname='quant_phase9_test'"
        ).fetchone() is not None
    if exists:
        raise ValueError("A4_FIXTURE_DATABASE_NOT_DROPPED_AFTER_RUN")


def merge_junit(parts: Sequence[Path], output: Path) -> None:
    root = ET.Element("testsuites")
    for path in parts:
        parsed = ET.parse(path).getroot()
        if parsed.tag == "testsuite":
            root.append(parsed)
        elif parsed.tag == "testsuites":
            for suite in parsed.findall("testsuite"):
                root.append(suite)
        else:
            raise ValueError("unexpected JUnit root")
    if not root.findall("testsuite"):
        raise ValueError("empty merged JUnit report")
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("affected", "full"), required=True)
    parser.add_argument("--junitxml", type=Path, required=True)
    args = parser.parse_args(argv)
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        parser.error("TEST_POSTGRES_DSN is required")
    info = conninfo_to_dict(dsn)
    if info.get("host") not in {"127.0.0.1", "localhost", "::1"} or info.get("dbname") != "quant_phase9_test":
        parser.error("test matrix requires disposable loopback quant_phase9_test database")
    fixture_dsn = None
    if args.scope == "full":
        fixture_dsn = os.environ.get(A4_FIXTURE_DSN_ENV)
        if not fixture_dsn:
            parser.error(f"{A4_FIXTURE_DSN_ENV} is required for --scope full (A4)")
        try:
            validate_a4_fixture_dsn(dsn, fixture_dsn)
            assert_fresh_fixture_server(dsn, fixture_dsn)
        except (ValueError, psycopg.Error) as exc:
            parser.error(str(exc))

    phase8_dsn = make_conninfo(dsn, dbname="quant_phase8_test")
    destination = args.junitxml.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    first = destination.with_name(destination.stem + ".core.xml")
    second = destination.with_name(destination.stem + ".phase8.xml")
    fixture_result = destination.with_name(destination.stem + ".fixture.xml")
    fixture_code = 0
    if args.scope == "affected":
        core_paths = tuple(path for path in AFFECTED if path not in PHASE8)
        phase8_paths = tuple(path for path in AFFECTED if path in PHASE8)
    else:
        core_paths = ("tests",)
        phase8_paths = PHASE8

    fixture_environment = os.environ.copy()
    fixture_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["QUANT_PROVISION_TEST_POSTGRES"] = "0"
    if args.scope == "full":
        assert fixture_dsn is not None
        fixture_environment["TEST_POSTGRES_DSN"] = fixture_dsn
        fixture_environment["QUANT_PROVISION_TEST_POSTGRES"] = "1"
        fixture = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", A4_FIXTURE_NODE,
             f"--junitxml={fixture_result}"],
            cwd=ROOT, env=fixture_environment, check=False, timeout=900,
        )
        fixture_code = fixture.returncode
        assert_fixture_database_dropped(fixture_dsn)

    schema = f"phase9_matrix_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        environment["TEST_POSTGRES_DSN"] = make_conninfo(
            dsn, options=f"-c search_path={schema}",
        )
        command = [sys.executable, "-m", "pytest", "-q", *core_paths]
        if args.scope == "full":
            command += [f"--ignore={path}" for path in (*PHASE8, *OPTIONAL_LIVE)]
            command += [f"--deselect={A4_FIXTURE_NODE}"]
        core = subprocess.run(
            [*command, f"--junitxml={first}"], cwd=ROOT, env=environment,
            check=False, timeout=900,
        )
        environment["TEST_POSTGRES_DSN"] = phase8_dsn
        phase8 = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *phase8_paths,
             f"--junitxml={second}"],
            cwd=ROOT, env=environment, check=False, timeout=900,
        )
        parts = (first, second) if args.scope == "affected" else (fixture_result, first, second)
        merge_junit(parts, destination)
        if args.scope == "full":
            validate_a4_fixture_junit(fixture_result, first, second, destination)
        return 0 if fixture_code == 0 and core.returncode == 0 and phase8.returncode == 0 else 1
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    raise SystemExit(main())
