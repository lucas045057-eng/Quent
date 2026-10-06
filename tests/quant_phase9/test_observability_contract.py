from __future__ import annotations

from quant_data_layer.admission import ReplayClass
from quant_data_layer.backpressure import BackpressureAction, get_stream_contract
from quant_data_layer.observability import MAX_SOURCE_SNAPSHOTS, SourceId, SourcePhase

from quant_phase1.config import Settings
from quant_phase1.entrypoints.engine import build_engine_observability_snapshot
from quant_phase9.config import Phase9RuntimeConfig
from quant_phase9.runtime import Phase9EngineRuntime


def test_phase9_registry_is_bounded_and_cursor_replayable():
    contract = get_stream_contract("phase9.evaluations")
    assert contract.registered
    assert contract.phase is SourcePhase.PHASE9
    assert contract.source_id is SourceId.PHASE9_EVALUATIONS
    assert contract.replay_class is ReplayClass.RECOVERABLE_REPLAYABLE
    assert contract.overload_action is BackpressureAction.DEFER_TO_DURABLE_CURSOR
    assert MAX_SOURCE_SNAPSHOTS == 12


def test_engine_snapshot_has_one_aggregate_phase9_source():
    snapshot = build_engine_observability_snapshot(
        Settings.from_env({"TRADING_MODE": "paper"}),
    )
    phase9 = [source for source in snapshot.sources if source.phase is SourcePhase.PHASE9]
    assert len(phase9) == 1
    assert phase9[0].source_id is SourceId.PHASE9_EVALUATIONS
    assert len(snapshot.sources) <= MAX_SOURCE_SNAPSHOTS


def test_phase9_health_exposes_only_bounded_aggregate_fields():
    runtime = Phase9EngineRuntime(config=Phase9RuntimeConfig(), dsn="unused")
    health = runtime.health()
    assert set(health) == {
        "state", "queue_depth", "queue_bytes", "completed", "deferred",
        "failures", "real_jev_status", "observations_skipped",
    }
    assert "symbol" not in repr(health).lower()
    assert "candidate" not in repr(health).lower()
    assert health["observations_skipped"] == 0
