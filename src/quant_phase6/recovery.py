"""Failure isolation and restart-safe recovery state for Phase 6."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from .contracts import EventStatus


@dataclass(frozen=True, slots=True)
class RecoverySnapshot:
    component: str
    status: EventStatus
    consecutive_failures: int
    last_error: str | None
    last_success_at: datetime | None
    recovered_at: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "component": self.component,
            "status": self.status.value,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "last_success_at": self.last_success_at.isoformat() if self.last_success_at else None,
            "recovered_at": self.recovered_at.isoformat() if self.recovered_at else None,
        }


class RecoveryTracker:
    def __init__(self, component: str, snapshot: RecoverySnapshot | None = None) -> None:
        if not component.strip():
            raise ValueError("component is required")
        self._snapshot = snapshot or RecoverySnapshot(
            component=component,
            status=EventStatus.NOT_AVAILABLE,
            consecutive_failures=0,
            last_error=None,
            last_success_at=None,
            recovered_at=None,
        )
        if self._snapshot.component != component:
            raise ValueError("snapshot component mismatch")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RecoveryTracker":
        def parse(value: str | None) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        snapshot = RecoverySnapshot(
            component=str(value["component"]),
            status=EventStatus(str(value["status"])),
            consecutive_failures=int(value["consecutive_failures"]),
            last_error=value.get("last_error"),
            last_success_at=parse(value.get("last_success_at")),
            recovered_at=parse(value.get("recovered_at")),
        )
        return cls(snapshot.component, snapshot)

    def failure(self, at: datetime, error: BaseException) -> RecoverySnapshot:
        previous = self._snapshot
        self._snapshot = RecoverySnapshot(
            component=previous.component,
            status=EventStatus.ERROR,
            consecutive_failures=previous.consecutive_failures + 1,
            last_error=type(error).__name__,
            last_success_at=previous.last_success_at,
            recovered_at=previous.recovered_at,
        )
        return self._snapshot

    def success(self, at: datetime) -> RecoverySnapshot:
        previous = self._snapshot
        self._snapshot = RecoverySnapshot(
            component=previous.component,
            status=EventStatus.AVAILABLE,
            consecutive_failures=0,
            last_error=None,
            last_success_at=at,
            recovered_at=at if previous.status is EventStatus.ERROR else previous.recovered_at,
        )
        return self._snapshot

    def snapshot(self) -> RecoverySnapshot:
        return self._snapshot


@dataclass(frozen=True, slots=True)
class PersistenceRecoveryResult:
    status: EventStatus
    persisted: int
    reason_code: str | None = None


def safe_persist_events(repository: Any, kind: str, events: Iterable[Any]) -> PersistenceRecoveryResult:
    """Persist one bounded event batch without leaking DB errors to the engine."""
    methods = {"news": "upsert_news", "macro": "upsert_macro", "unlock": "upsert_unlock"}
    method_name = methods.get(kind)
    if method_name is None:
        return PersistenceRecoveryResult(EventStatus.ERROR, 0, "UNKNOWN_EVENT_KIND")
    try:
        count = int(getattr(repository, method_name)(tuple(events)))
    except Exception as exc:
        return PersistenceRecoveryResult(EventStatus.ERROR, 0, type(exc).__name__)
    return PersistenceRecoveryResult(EventStatus.AVAILABLE, max(0, count))
