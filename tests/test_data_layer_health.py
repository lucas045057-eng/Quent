from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from quant_data_layer.errors import normalize_error
from quant_data_layer.health import (
    HealthEvidenceState,
    HealthTransitionError,
    SourceHealthRegistry,
    SourceHealthTracker,
)
from quant_data_layer.observability import (
    DataState,
    ErrorCategory,
    LifecycleState,
    MAX_SOURCE_SNAPSHOTS,
    SourceId,
    SourcePhase,
)


START = datetime(2026, 9, 26, 7, 0, tzinfo=timezone.utc)


class DataQualityError(Exception):
    pass


def _tracker(
    *, configured: bool = True, persistence_required: bool = True, cursor_required: bool = False
):
    return SourceHealthTracker(
        source_id=SourceId.PHASE1_MARKET_DATA,
        phase=SourcePhase.PHASE1,
        configured=configured,
        persistence_required=persistence_required,
        cursor_required=cursor_required,
        started_at_utc=START,
    )


def _mark_available(tracker, *, checked_at=START + timedelta(seconds=10), **overrides):
    source_timestamp = checked_at - timedelta(seconds=6)
    values = {
        "checked_at_utc": checked_at,
        "source_timestamp_utc": source_timestamp,
        "fetched_at_utc": checked_at - timedelta(seconds=5),
        "processed_at_utc": checked_at - timedelta(seconds=4),
        "persisted_at_utc": checked_at - timedelta(seconds=2),
        "freshness_reference_at_utc": source_timestamp,
        "freshness_window_seconds": 60,
        "source_read_valid": True,
        "contract_valid": True,
        "gap_resolved": True,
        "cursor_reconciled": tracker.cursor_required,
    }
    values.update(overrides)
    return tracker.record_available(**values)


def test_configured_source_starts_initializing_and_disabled_source_is_not_configured():
    configured = _tracker().snapshot()
    disabled = _tracker(configured=False).snapshot()

    assert configured.lifecycle_state is LifecycleState.INITIALIZING
    assert configured.data_status is DataState.NOT_AVAILABLE
    assert disabled.lifecycle_state is LifecycleState.NOT_CONFIGURED
    assert disabled.configured is False


def test_available_requires_read_contract_gap_freshness_and_required_persistence_evidence():
    tracker = _tracker()
    snapshot = _mark_available(tracker)

    assert snapshot.lifecycle_state is LifecycleState.AVAILABLE
    assert snapshot.data_status is DataState.AVAILABLE
    assert snapshot.read_status is HealthEvidenceState.AVAILABLE
    assert snapshot.contract_status is HealthEvidenceState.AVAILABLE
    assert snapshot.processing_status is HealthEvidenceState.AVAILABLE
    assert snapshot.persistence_status is HealthEvidenceState.AVAILABLE
    assert snapshot.cursor_status is HealthEvidenceState.NOT_REQUIRED
    assert snapshot.freshness_status is HealthEvidenceState.AVAILABLE
    assert snapshot.last_source_timestamp_utc == START + timedelta(seconds=4)
    assert snapshot.last_fetched_at_utc == START + timedelta(seconds=5)
    assert snapshot.last_processed_at_utc == START + timedelta(seconds=6)
    assert snapshot.last_persisted_at_utc == START + timedelta(seconds=8)
    assert snapshot.freshness_age_seconds == 6


@pytest.mark.parametrize(
    "evidence",
    [
        {"source_read_valid": False},
        {"contract_valid": False},
        {"gap_resolved": False},
        {"persisted_at_utc": None},
        {"freshness_window_seconds": 2},
    ],
)
def test_unready_or_stale_source_cannot_claim_available(evidence):
    tracker = _tracker()
    with pytest.raises(HealthTransitionError):
        _mark_available(tracker, **evidence)
    assert tracker.snapshot().lifecycle_state is LifecycleState.INITIALIZING
    assert tracker.snapshot().data_status is DataState.NOT_AVAILABLE


def test_degraded_rate_limited_stale_then_recovery_preserves_separate_data_state():
    tracker = _tracker()
    _mark_available(tracker)

    degraded = tracker.mark_degraded(normalize_error(ConnectionError("reset")), checked_at_utc=START + timedelta(seconds=11))
    assert degraded.lifecycle_state is LifecycleState.DEGRADED
    assert degraded.data_status is DataState.AVAILABLE

    limited = tracker.mark_rate_limited(
        normalize_error(RuntimeError("429"), http_status=429), checked_at_utc=START + timedelta(seconds=12)
    )
    assert limited.lifecycle_state is LifecycleState.RATE_LIMITED
    assert limited.data_status is DataState.AVAILABLE

    stale = tracker.mark_stale("FRESHNESS_EXPIRED", checked_at_utc=START + timedelta(seconds=70))
    assert stale.lifecycle_state is LifecycleState.STALE
    assert stale.data_status is DataState.STALE

    recovered = _mark_available(tracker, checked_at=START + timedelta(seconds=71))
    assert recovered.lifecycle_state is LifecycleState.AVAILABLE
    assert recovered.data_status is DataState.AVAILABLE
    assert recovered.last_error is None


def test_data_quality_error_is_attributed_to_processing_not_source_read_or_contract():
    tracker = _tracker()
    snapshot = tracker.mark_degraded(
        normalize_error(DataQualityError("invalid canonical value")),
        checked_at_utc=START + timedelta(seconds=1),
    )

    assert snapshot.lifecycle_state is LifecycleState.DEGRADED
    assert snapshot.read_status is HealthEvidenceState.NOT_AVAILABLE
    assert snapshot.contract_status is HealthEvidenceState.NOT_AVAILABLE
    assert snapshot.processing_status is HealthEvidenceState.ERROR


def test_error_requires_restart_and_restart_requires_new_persistence_proof():
    tracker = _tracker(cursor_required=True)
    failed = tracker.mark_error(
        normalize_error(RuntimeError("corrupt")), checked_at_utc=START + timedelta(seconds=1)
    )
    assert failed.lifecycle_state is LifecycleState.ERROR
    assert failed.data_status is DataState.ERROR
    with pytest.raises(HealthTransitionError):
        _mark_available(tracker, checked_at=START + timedelta(seconds=2))

    restarted = tracker.restart(checked_at_utc=START + timedelta(seconds=3))
    assert restarted.lifecycle_state is LifecycleState.INITIALIZING
    with pytest.raises(HealthTransitionError):
        _mark_available(
            tracker,
            checked_at=START + timedelta(seconds=4),
            persisted_at_utc=None,
        )
    assert tracker.snapshot().lifecycle_state is LifecycleState.INITIALIZING



def test_shutdown_does_not_erase_last_data_state_and_restart_reinitializes():
    tracker = _tracker()
    available = _mark_available(tracker)
    stopping = tracker.shutdown(checked_at_utc=START + timedelta(seconds=11))

    assert stopping.lifecycle_state is LifecycleState.SHUTTING_DOWN
    assert stopping.data_status is DataState.AVAILABLE
    assert stopping.last_success_at_utc == available.last_success_at_utc

    restarted = tracker.restart(checked_at_utc=START + timedelta(seconds=12))
    assert restarted.lifecycle_state is LifecycleState.INITIALIZING
    assert restarted.data_status is DataState.AVAILABLE
    assert restarted.lifecycle_state is not LifecycleState.AVAILABLE


def test_health_timestamps_require_utc_and_reject_future_evidence():
    with pytest.raises(ValueError, match="UTC"):
        SourceHealthTracker(
            source_id=SourceId.PHASE1_MARKET_DATA,
            phase=SourcePhase.PHASE1,
            configured=True,
            persistence_required=True,
            cursor_required=False,
            started_at_utc=datetime(2026, 9, 26, 7, 0),
        )

    tracker = _tracker()
    with pytest.raises(HealthTransitionError):
        _mark_available(tracker, source_timestamp_utc=START + timedelta(seconds=11))


def test_reason_and_serialized_error_are_secret_safe():
    tracker = _tracker()
    with pytest.raises(ValueError):
        tracker.mark_stale("https://rpc.example/path?token=hidden", checked_at_utc=START + timedelta(seconds=1))

    tracker.mark_degraded(
        normalize_error(RuntimeError("api-key: test-secret-value https://rpc.example/path")),
        checked_at_utc=START + timedelta(seconds=2),
    )
    serialized = json.dumps(tracker.snapshot().to_dict())
    assert "test-secret-value" not in serialized
    assert "rpc.example" not in serialized
    assert "api-key" not in serialized.lower()
    assert "message" not in serialized


def test_source_health_registry_has_fixed_bounded_cardinality():
    registry = SourceHealthRegistry(max_sources=MAX_SOURCE_SNAPSHOTS)
    registry.register(_tracker())
    with pytest.raises(ValueError, match="duplicate"):
        registry.register(_tracker())

    for source_id in list(SourceId)[1:MAX_SOURCE_SNAPSHOTS]:
        registry.register(
            SourceHealthTracker(
                source_id=source_id,
                phase=SourcePhase.PHASE1,
                configured=True,
                persistence_required=False,
                cursor_required=False,
                started_at_utc=START,
            )
        )
    with pytest.raises(ValueError, match="bound"):
        registry.register(
            SourceHealthTracker(
                source_id=list(SourceId)[MAX_SOURCE_SNAPSHOTS],
                phase=SourcePhase.PHASE1,
                configured=True,
                persistence_required=False,
                cursor_required=False,
                started_at_utc=START,
            )
        )
    assert len(registry.snapshot()) == MAX_SOURCE_SNAPSHOTS
