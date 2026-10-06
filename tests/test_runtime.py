import asyncio
from datetime import timezone
import json

from quant_phase1.runtime import BoundedEventBuffer, PeriodicScheduler, RuntimeHealthTracker


def test_periodic_scheduler_runs_until_cancelled():
    stop = asyncio.Event()
    calls: list[int] = []

    async def callback() -> None:
        calls.append(1)
        if len(calls) == 2:
            stop.set()

    asyncio.run(PeriodicScheduler(0.001, callback).run(stop, run_immediately=True))
    assert len(calls) == 2


def test_bounded_event_buffer_drops_oldest_and_tracks_loss():
    buffer = BoundedEventBuffer[int](capacity=2)
    buffer.append(1)
    buffer.append(2)
    buffer.append(3)
    assert buffer.drain() == [2, 3]
    assert buffer.dropped_count == 1


def test_runtime_health_records_outage_and_recovery():
    tracker = RuntimeHealthTracker("quant-collector")
    first = tracker.mark_degraded("database unavailable")
    assert first.state == "DEGRADED"
    assert first.outage_started_at.tzinfo == timezone.utc
    assert tracker.mark_degraded("still unavailable") is None
    recovery = tracker.mark_running()
    assert recovery is not None
    assert recovery.outage_started_at == first.outage_started_at
    assert tracker.state == "RUNNING"


def test_runtime_health_serialization_keeps_only_normalized_error_evidence():
    tracker = RuntimeHealthTracker("quant-collector")
    secret = "api-key: test-secret-value https://rpc.example/private/token raw_payload=large"

    snapshot = tracker.mark_degraded(RuntimeError(secret))
    serialized = json.dumps(snapshot.to_dict())

    assert "test-secret-value" not in serialized
    assert "rpc.example" not in serialized
    assert "raw_payload" not in serialized
    assert snapshot.last_error.category.value == "UNKNOWN"
