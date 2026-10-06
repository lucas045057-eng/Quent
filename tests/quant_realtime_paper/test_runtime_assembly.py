from pathlib import Path

import pytest

from quant_realtime_paper.config import RuntimeConfig
from quant_realtime_paper import cli


REVISION = "b" * 40


def _assembly_module():
    import importlib

    try:
        return importlib.import_module("quant_realtime_paper.assembly")
    except ModuleNotFoundError:
        pytest.fail("default realtime paper assembly module is missing")


def _config(tmp_path):
    return RuntimeConfig(
        None, tmp_path / "sessions.sqlite3", 2.0,
        ("BTCUSDT", "ETHUSDT"), tmp_path,
    )


def _env(tmp_path, **overrides):
    values = {
        "QUANT_BUILD_REVISION": REVISION,
        "TRADING_MODE": "paper",
        "PAPER_ONLY": "true",
        "LIVE_ALLOWED": "false",
        "PHASE9_POLICY_MANIFEST_PATH": str(
            Path(__file__).parents[2] / "policies" / "phase9_policy_v1.json"
        ),
        "PHASE9_POLICY_APPROVAL_PATH": str(tmp_path / "missing-approval.json"),
    }
    values.update(overrides)
    return values


def test_run_command_uses_default_assembly(tmp_path, monkeypatch):
    assembly = _assembly_module()
    config = _config(tmp_path)
    calls = []

    class Monitor:
        def run(self, *, duration_seconds, stop_event):
            calls.append((duration_seconds, stop_event))

    monkeypatch.setattr(cli, "_state_directory", lambda _value: tmp_path)
    monkeypatch.setattr(cli.RuntimeConfig, "from_env", classmethod(lambda cls, env=None: config))
    monkeypatch.setattr(cli.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(assembly, "build_default_runtime", lambda cfg, environ: (calls.append((cfg, environ)) or Monitor()))

    assert cli.main(["run", "--state-dir", str(tmp_path)]) == 0
    assert len(calls) == 2
    assert calls[0][0] == config
    assert calls[1][0] is None


def test_missing_approval_returns_blocked_monitor_without_fixture_fallback(tmp_path, monkeypatch):
    assembly = _assembly_module()
    calls = []
    original = assembly.RealtimePaperMonitor

    def monitor_factory(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(assembly, "RealtimePaperMonitor", monitor_factory)
    monitor = assembly.build_default_runtime(_config(tmp_path), _env(tmp_path))

    assert monitor.execution_pipeline is None
    assert monitor.startup_blocker == "APPROVAL_MISSING"
    assert monitor._snapshot_strategy() == (False, "APPROVAL_MISSING")
    assert len(calls) == 1
    assert calls[0]["execution_pipeline"] is None


def test_approval_build_revision_mismatch_blocks_before_pipeline_construction(tmp_path):
    from quant_phase9.approval import create_approval_artifact

    assembly = _assembly_module()
    manifest = Path(__file__).parents[2] / "policies" / "phase9_policy_v1.json"
    approval = tmp_path / "mismatched-approval.json"
    create_approval_artifact(
        manifest, approval, approved_by="test-owner", approved_commit="a" * 40,
    )
    monitor = assembly.build_default_runtime(
        _config(tmp_path),
        _env(tmp_path, PHASE9_POLICY_APPROVAL_PATH=str(approval)),
    )

    assert monitor.execution_pipeline is None
    assert monitor.startup_blocker == "BUILD_REVISION_MISMATCH"


@pytest.mark.parametrize("overrides", [
    {"TRADING_MODE": "live", "PAPER_ONLY": "true", "LIVE_ALLOWED": "false"},
    {"TRADING_MODE": "paper", "PAPER_ONLY": "false", "LIVE_ALLOWED": "false"},
    {"TRADING_MODE": "paper", "PAPER_ONLY": "true", "LIVE_ALLOWED": "true"},
    {"TRADING_MODE": "", "PAPER_ONLY": "true", "LIVE_ALLOWED": "false"},
])
def test_any_incomplete_paper_lock_blocks_default_assembly(tmp_path, overrides):
    assembly = _assembly_module()
    monitor = assembly.build_default_runtime(
        _config(tmp_path), _env(tmp_path, **overrides),
    )
    assert monitor.execution_pipeline is None
    assert monitor.startup_blocker == "PAPER_ONLY_CONFIGURATION_INVALID"
