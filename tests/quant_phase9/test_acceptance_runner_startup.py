from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import psycopg
import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load_acceptance_runner():
    name = "_quant_phase9_acceptance_runner_test"
    if name in sys.modules:
        return sys.modules[name]
    path = ROOT / "scripts/run_phase9_acceptance.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_a7_startup_loads_replay_approval_before_database_access(monkeypatch):
    class DatabaseBoundaryReached(RuntimeError):
        pass

    def stop_before_database(*args, **kwargs):
        raise DatabaseBoundaryReached

    monkeypatch.setenv(
        "PHASE9_ACCEPTANCE_STARTUP_TEST_DSN",
        "host=127.0.0.1 dbname=quant_phase9_test user=acceptance_test",
    )
    monkeypatch.setattr(psycopg, "connect", stop_before_database)

    runner = _load_acceptance_runner()
    with pytest.raises(DatabaseBoundaryReached):
        runner._runtime_acceptance(
            root=ROOT,
            duration_seconds=1,
            formal=False,
            stage1_fixture=ROOT / "tests/fixtures/phase9/stage1_candidate.json",
            dsn_env="PHASE9_ACCEPTANCE_STARTUP_TEST_DSN",
            policy_manifest=ROOT / "tests/fixtures/phase9/policy_manifest.json",
            approval_manifest=ROOT / "tests/fixtures/phase9/policy_approval.json",
            jev_mode="fake",
        )
