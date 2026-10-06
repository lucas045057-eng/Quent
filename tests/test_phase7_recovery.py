from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quant_phase7.contracts import DataStatus
from quant_phase7.recovery import (
    CheckpointState,
    CheckpointValidationError,
    GapKind,
    Phase7RecoveryCoordinator,
    RecoveryHealthEvent,
)


NOW = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)


def _checkpoint(**overrides):
    values = {
        "source_id": "ethereum_rpc",
        "scope_kind": "CHAIN",
        "scope_key": "ETHEREUM_MAINNET",
        "cursor_kind": "BLOCK",
        "cursor_value": "100",
        "last_observed_cursor": "100",
        "last_finalized_cursor": "98",
        "last_block_hash": "0x" + "ab" * 32,
        "parser_version": "phase7-ethereum-v1",
        "schema_version": "phase7-onchain-v1",
        "status": DataStatus.AVAILABLE,
        "reason": None,
        "updated_at": NOW,
    }
    values.update(overrides)
    return CheckpointState(**values)


def test_checkpoint_is_utc_typed_and_serializes_to_existing_schema():
    checkpoint = _checkpoint()
    row = checkpoint.to_row()
    assert row["status"] == "AVAILABLE"
    assert row["updated_at"] == NOW
    assert row["last_block_hash"].startswith("0x")

    with pytest.raises(CheckpointValidationError):
        _checkpoint(updated_at=datetime(2026, 9, 23))
    with pytest.raises(CheckpointValidationError):
        _checkpoint(scope_kind="CHAIN", last_block_hash=None)
    with pytest.raises(CheckpointValidationError):
        _checkpoint(status=DataStatus.PARTIAL, reason=None)
    initial = _checkpoint(
        cursor_value="0", last_observed_cursor=None, last_finalized_cursor=None,
    )
    assert initial.to_row()["last_observed_cursor"] is None


def test_catch_up_is_bounded_and_never_rescans_all_history():
    coordinator = Phase7RecoveryCoordinator(max_catch_up=12, max_backfill=144)
    plan = coordinator.plan_catch_up(_checkpoint(), head_cursor="200")
    assert plan.start_cursor == "101"
    assert plan.end_cursor == "112"
    assert plan.requested_count == 12
    assert plan.status is DataStatus.PARTIAL
    assert plan.reason == "CATCH_UP_BOUND"

    complete = coordinator.plan_catch_up(_checkpoint(), head_cursor="106")
    assert complete.start_cursor == "101"
    assert complete.end_cursor == "106"
    assert complete.requested_count == 6
    assert complete.status is DataStatus.AVAILABLE


def test_checkpoint_cursor_regression_and_parent_hash_mismatch_are_not_silent():
    coordinator = Phase7RecoveryCoordinator(max_catch_up=12, max_backfill=144)
    with pytest.raises(CheckpointValidationError):
        coordinator.plan_catch_up(_checkpoint(), head_cursor="99")
    event = coordinator.validate_parent_hash(
        _checkpoint(), parent_hash="0x" + "cd" * 32, observed_at=NOW,
    )
    assert event.status is DataStatus.STALE
    assert event.reason == "REORG_RISK"
    assert event.gap_kind is GapKind.PARENT_HASH
    block_event = coordinator.validate_block_hash(
        _checkpoint(), block_hash="0x" + "cd" * 32, observed_at=NOW,
    )
    assert block_event.gap_kind is GapKind.BLOCK_HASH
    reorg = coordinator.plan_reorg_window(_checkpoint(), new_block_hash="0x" + "cd" * 32)
    assert reorg.scan_start_cursor == "89"
    assert reorg.scan_end_cursor == "100"
    assert reorg.status is DataStatus.STALE

    loaded = coordinator.load_checkpoint(
        _checkpoint().to_row(), expected_source_id="ethereum_rpc",
        expected_scope_kind="CHAIN", expected_scope_key="ETHEREUM_MAINNET",
    )
    assert loaded == _checkpoint()
    coordinator.validate_checkpoint_progress(_checkpoint(), _checkpoint(cursor_value="101", last_observed_cursor="101", last_finalized_cursor="99"))


def test_unresolved_gap_rate_limit_and_queue_pressure_degrade_coverage():
    coordinator = Phase7RecoveryCoordinator(max_catch_up=12, max_backfill=144)
    unresolved = coordinator.unresolved_gap(
        GapKind.BLOCK_HEIGHT, source_id="ethereum_rpc", scope_kind="CHAIN", scope_key="ETHEREUM_MAINNET",
        observed_at=NOW, details={"missing_start": 101, "missing_end": 104},
    )
    assert unresolved.status is DataStatus.PARTIAL
    assert unresolved.reason == "GAP_UNRESOLVED"

    limited = coordinator.rate_limited(
        source_id="ethereum_rpc", scope_kind="CHAIN", scope_key="ETHEREUM_MAINNET",
        observed_at=NOW, retry_after_seconds=3,
    )
    assert limited.status is DataStatus.PARTIAL
    assert limited.reason == "RATE_LIMITED"
    assert limited.details["retry_after_seconds"] == 3

    pressure = coordinator.queue_pressure(
        source_id="binance_spot", scope_kind="EXCHANGE", scope_key="BTCUSDT",
        observed_at=NOW, queue_depth=512, queue_capacity=512,
    )
    assert pressure.status is DataStatus.PARTIAL
    assert pressure.reason == "QUEUE_PRESSURE"

    lag = coordinator.lag(
        source_id="btc_core_rpc", scope_kind="CHAIN", scope_key="BITCOIN_MAINNET",
        observed_at=NOW, head_cursor="100", last_observed_cursor="98",
    )
    assert lag.reason == "INGESTION_LAG"
    assert lag.details["lag_cursor"] == "2"


def test_health_event_requires_utc_and_bounded_details():
    event = RecoveryHealthEvent(
        source_id="btc_core_rpc",
        scope_kind="CHAIN",
        scope_key="BITCOIN_MAINNET",
        gap_kind=GapKind.BLOCK_HEIGHT,
        occurred_at=NOW,
        status=DataStatus.PARTIAL,
        reason="GAP_UNRESOLVED",
        details={"missing_count": 3},
    )
    assert event.as_details()["gap_kind"] == "BLOCK_HEIGHT"
    with pytest.raises(CheckpointValidationError):
        RecoveryHealthEvent(
            source_id="btc_core_rpc", scope_kind="CHAIN", scope_key="BITCOIN_MAINNET",
            gap_kind=GapKind.BLOCK_HEIGHT, occurred_at=NOW + timedelta(hours=1),
            status=DataStatus.PARTIAL, reason="GAP_UNRESOLVED", details={"x": "y" * 70000},
        )
    with pytest.raises(CheckpointValidationError):
        coordinator = Phase7RecoveryCoordinator(max_catch_up=12, max_backfill=144)
        coordinator.unresolved_gap(
            GapKind.BLOCK_HEIGHT, source_id="btc_core_rpc", scope_kind="CHAIN", scope_key="BITCOIN_MAINNET",
            observed_at=NOW, details={"body": "not metadata"},
        )
