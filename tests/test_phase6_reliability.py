from __future__ import annotations

from datetime import datetime, timezone

from quant_phase6.contracts import EventStatus
from quant_phase6.recovery import RecoveryTracker, safe_persist_events


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


class Repository:
    def __init__(self):
        self.calls = 0

    def upsert_news(self, events):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("database temporarily unavailable")
        return len(events)


def test_database_outage_is_isolated_and_recovers_without_duplicate_batching():
    repository = Repository()
    first = safe_persist_events(repository, "news", ("event-1",))
    second = safe_persist_events(repository, "news", ("event-1",))
    assert first.status is EventStatus.ERROR
    assert first.persisted == 0
    assert first.reason_code == "RuntimeError"
    assert second.status is EventStatus.AVAILABLE
    assert second.persisted == 1
    assert repository.calls == 2


def test_recovery_state_survives_restart_and_marks_recovered():
    tracker = RecoveryTracker("phase6-news")
    failed = tracker.failure(NOW, RuntimeError("secret should not be persisted"))
    restored = RecoveryTracker.from_dict(failed.to_dict())
    recovered = restored.success(NOW.replace(second=1))
    assert failed.status is EventStatus.ERROR
    assert failed.consecutive_failures == 1
    assert failed.last_error == "RuntimeError"
    assert recovered.status is EventStatus.AVAILABLE
    assert recovered.recovered_at == NOW.replace(second=1)
