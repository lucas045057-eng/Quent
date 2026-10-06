from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_phase1.contracts import DataStatus
from quant_phase1.stage1 import Stage1Result
from quant_phase7.contracts import DataStatus as Phase7DataStatus
from quant_phase7.enrichment import (
    Phase7Context,
    build_stage1_phase7_enrichment,
)
from quant_phase7.retention import (
    Phase7HealthTracker,
    Phase7RetentionPolicy,
    cleanup_phase7_retention,
)


NOW = datetime(2024, 9, 23, 12, 0, tzinfo=timezone.utc)


def result(symbol: str, category: str) -> Stage1Result:
    return Stage1Result(
        symbol=symbol, category=category, reason="rule", status=DataStatus.AVAILABLE,
        inputs_used=("price",), indicators={"ema": Decimal("1")}, structure="BULLISH",
        reason_codes=("RULE",), key_metrics={"spread": Decimal("0.001")}, timestamp=NOW,
    )


def test_absent_phase7_sources_do_not_remove_or_mutate_stage1_candidates():
    candidates = (result("BTCUSDT", "A"), result("ETHUSDT", "B"))
    rows = build_stage1_phase7_enrichment(
        screening_run_id=42, candidates=candidates, contexts={}, processed_at=NOW,
    )
    assert [row["symbol"] for row in rows] == ["BTCUSDT", "ETHUSDT"]
    assert [candidate.category for candidate in candidates] == ["A", "B"]
    assert all(row["status"] == "NOT_AVAILABLE" for row in rows)
    assert all(row["reason"] == "PHASE7_CONTEXT_NOT_AVAILABLE" for row in rows)
    for row in rows:
        assert not {"category", "classification", "decision", "score", "eligibility"} & set(row["context_reference"])
        assert row["context_reference"]["sources"] == {}


def test_phase7_context_status_is_additive_and_never_rewrites_stage1_decision():
    candidate = result("BTCUSDT", "A")
    context = Phase7Context(
        source_id="binance_spot", status=DataStatus.AVAILABLE,
        reference="spot-window:btc:1m", coverage=Decimal("1.0"),
    )
    row = build_stage1_phase7_enrichment(
        screening_run_id=7, candidates=(candidate,),
        contexts={"BTCUSDT": (context,)}, processed_at=NOW,
    )[0]
    assert row["status"] == "AVAILABLE"
    assert row["context_reference"]["sources"]["binance_spot"]["reference"] == "spot-window:btc:1m"
    assert row["coverage"]["available_count"] == 1
    assert row["symbol"] == "BTCUSDT"


def test_context_errors_are_isolated_and_context_reference_is_bounded():
    candidate = result("BTCUSDT", "A")
    bad = Phase7Context(
        source_id="ethereum_rpc", status=DataStatus.ERROR,
        reference="health:event:1", coverage=Decimal("0"),
    )
    partial = Phase7Context(
        source_id="labels", status=Phase7DataStatus.PARTIAL,
        reference="labels:snapshot-v1", coverage=Decimal("0.5"),
    )
    row = build_stage1_phase7_enrichment(
        screening_run_id=7, candidates=(candidate,),
        contexts={"BTCUSDT": (bad, partial)}, processed_at=NOW,
    )[0]
    assert row["status"] == "ERROR"
    assert row["coverage"]["missing_count"] == 2
    assert candidate.category == "A"
    with pytest.raises(ValueError):
        build_stage1_phase7_enrichment(
            screening_run_id=7, candidates=(candidate,),
            contexts={"BTCUSDT": (Phase7Context(
                source_id="x", status=DataStatus.AVAILABLE,
                reference="x" * 10_000, coverage=Decimal("1"),
            ),)}, processed_at=NOW,
        )


def test_retention_policy_is_configured_per_table_and_cleanup_is_bounded_and_isolated():
    policy = Phase7RetentionPolicy(
        onchain_transfer_events_days=90,
        spot_flow_windows_days=30,
        stablecoin_context_days=365,
    )
    assert policy.days_for("phase7_spot_flow_windows") == 30
    assert policy.days_for("phase7_stablecoin_context") == 365

    class Repository:
        def __init__(self):
            self.calls = []
        def cleanup(self, table, cutoff, *, batch_size, max_batches):
            self.calls.append((table, cutoff, batch_size, max_batches))
            if table == "phase7_whale_flow_windows":
                raise RuntimeError("temporary db failure")
            return 2

    repo = Repository()
    metrics = cleanup_phase7_retention(
        repo, policy, now=NOW, batch_size=50, max_batches=2,
    )
    assert len(repo.calls) == len(policy.tables())
    assert all(call[2:] == (50, 2) for call in repo.calls)
    assert any(metric.table == "phase7_whale_flow_windows" and metric.status == "ERROR" for metric in metrics)
    assert any(metric.table == "phase7_spot_flow_windows" and metric.deleted == 2 for metric in metrics)
    with pytest.raises(ValueError):
        cleanup_phase7_retention(repo, policy, now=NOW, batch_size=0, max_batches=2)


def test_retention_uses_independent_transaction_scope_when_repository_exposes_connection():
    class Transaction:
        def __init__(self, owner):
            self.owner = owner
        def __enter__(self):
            self.owner.started += 1
        def __exit__(self, exc_type, exc, traceback):
            if exc_type is not None:
                self.owner.rolled_back += 1
            return False

    class Connection:
        def __init__(self):
            self.started = 0
            self.rolled_back = 0
        def transaction(self):
            return Transaction(self)

    class Repository:
        def __init__(self):
            self.connection = Connection()
        def cleanup(self, table, cutoff, *, batch_size, max_batches):
            if table == "phase7_whale_flow_windows":
                raise RuntimeError("one table failed")
            return 1

    repo = Repository()
    metrics = cleanup_phase7_retention(repo, Phase7RetentionPolicy(), now=NOW)
    assert repo.connection.started == len(Phase7RetentionPolicy().tables())
    assert repo.connection.rolled_back == 1
    assert sum(metric.deleted for metric in metrics) == len(metrics) - 1


def test_health_tracker_emits_recovery_without_creating_a_new_health_table():
    tracker = Phase7HealthTracker()
    first = tracker.update("ethereum_rpc", Phase7DataStatus.ERROR, NOW, {"reason": "timeout"})
    recovered = tracker.update("ethereum_rpc", Phase7DataStatus.AVAILABLE, NOW + timedelta(minutes=1), {"cursor": "101"})
    assert first.recovered is False
    assert recovered.recovered is True
    assert recovered.previous_status is Phase7DataStatus.ERROR
    assert tracker.snapshot()["ethereum_rpc"].status is Phase7DataStatus.AVAILABLE
    assert recovered.details["cursor"] == "101"


def test_health_tracker_reuses_existing_repository_health_and_runtime_event_methods():
    class Repository:
        def __init__(self):
            self.system = []
            self.events = []
        def upsert_system_health(self, *args):
            self.system.append(args)
        def insert_runtime_health_event(self, *args, **kwargs):
            self.events.append((args, kwargs))

    repo = Repository()
    tracker = Phase7HealthTracker(repo)
    tracker.update("spot", Phase7DataStatus.PARTIAL, NOW, {"coverage": "0.5"})
    tracker.update("spot", Phase7DataStatus.AVAILABLE, NOW + timedelta(minutes=1), {"heartbeat": True})
    assert repo.system[0][1] is DataStatus.NOT_AVAILABLE
    assert repo.system[-1][1] is DataStatus.AVAILABLE
    assert repo.events[-1][0][1] == "RUNNING"
    assert repo.events[-1][1]["reason"] == "PHASE7_SOURCE_RECOVERED"
    assert repo.events[-1][1]["outage_started_at"] == NOW


def test_health_tracker_accepts_only_scalar_allowlisted_metadata():
    tracker = Phase7HealthTracker()
    with pytest.raises(ValueError, match="unsupported keys"):
        tracker.update("spot", Phase7DataStatus.ERROR, NOW, {"nested": {"raw": "x"}})
    with pytest.raises(ValueError, match="scalar metadata"):
        tracker.update("spot", Phase7DataStatus.ERROR, NOW, {"queue_depth": [1, 2]})
    with pytest.raises(ValueError, match="raw payload"):
        tracker.update("spot", Phase7DataStatus.ERROR, NOW, {"reason": "raw_payload captured"})

