"""Phase 5 context health transitions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .contracts import ContextStatus
from quant_phase1.time import ensure_utc, utc_now


@dataclass(frozen=True, slots=True)
class Phase5HealthSnapshot:
    component: str
    status: ContextStatus
    checked_at: datetime
    details: dict[str, Any]
    transition_count: int = 0


class Phase5HealthRegistry:
    def __init__(self) -> None:
        self._snapshots: dict[str, Phase5HealthSnapshot] = {}

    def _set(self, component: str, status: ContextStatus, checked_at: datetime, details: dict[str, Any]) -> Phase5HealthSnapshot:
        checked_at = ensure_utc(checked_at)
        previous = self._snapshots.get(component)
        snapshot = Phase5HealthSnapshot(
            component=component,
            status=status,
            checked_at=checked_at,
            details=dict(details),
            transition_count=(previous.transition_count + 1 if previous is not None and previous.status is not status else (previous.transition_count if previous else 0)),
        )
        self._snapshots[component] = snapshot
        return snapshot

    def mark_running(self, component: str, checked_at: datetime, details: dict[str, Any] | None = None) -> Phase5HealthSnapshot:
        return self._set(component, ContextStatus.AVAILABLE, checked_at, details or {"heartbeat": True})

    def mark_status(
        self,
        component: str,
        status: ContextStatus,
        checked_at: datetime,
        details: dict[str, Any] | None = None,
    ) -> Phase5HealthSnapshot:
        return self._set(component, status, checked_at, details or {"heartbeat": True})

    def mark_degraded(self, component: str, checked_at: datetime, reason: str, details: dict[str, Any] | None = None) -> Phase5HealthSnapshot:
        payload = dict(details or {})
        payload["reason"] = reason
        return self._set(component, ContextStatus.ERROR, checked_at, payload)

    def snapshot(self, component: str) -> Phase5HealthSnapshot:
        return self._snapshots.get(
            component,
            Phase5HealthSnapshot(component, ContextStatus.NOT_AVAILABLE, utc_now(), {"reason": "not_started"}),
        )

    def overall(self) -> Phase5HealthSnapshot:
        if not self._snapshots:
            return self.snapshot("phase5_context")
        priority = {
            ContextStatus.ERROR: 4,
            ContextStatus.STALE: 3,
            ContextStatus.NOT_AVAILABLE: 2,
            ContextStatus.PARTIAL: 2,
            ContextStatus.AVAILABLE: 1,
        }
        return max(self._snapshots.values(), key=lambda item: priority[item.status])
