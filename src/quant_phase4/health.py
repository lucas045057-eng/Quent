"""Bounded component health for the Phase 4 runtime."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum


class Phase4HealthState(StrEnum):
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    ERROR = "ERROR"
    RECOVERED = "RECOVERED"


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("health timestamp must be UTC-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class Phase4HealthSnapshot:
    component: str
    state: Phase4HealthState
    checked_at: datetime
    reason: str | None = None
    reconnect_count: int = 0
    dropped_count: int = 0
    rejected_count: int = 0
    gap_count: int = 0
    last_exchange_at: datetime | None = None
    last_fetched_at: datetime | None = None
    last_processed_at: datetime | None = None
    gap_reason: str | None = None
    gap_detected_at: datetime | None = None
    gap_watermark_received_at: datetime | None = None
    gap_watermark_event_timestamp: datetime | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)


class Phase4HealthRegistry:
    """Keep exactly one bounded health snapshot per Phase 4 component."""

    COMPONENTS = ("liquidation", "long_short", "basis")

    def __init__(self) -> None:
        self._snapshots: dict[str, Phase4HealthSnapshot] = {}

    def _set(self, component: str, state: Phase4HealthState, now: datetime, *, reason: str | None = None) -> Phase4HealthSnapshot:
        if component not in self.COMPONENTS:
            raise ValueError(f"unknown Phase 4 component: {component}")
        checked_at = _utc(now)
        previous = self._snapshots.get(component)
        snapshot = Phase4HealthSnapshot(
            component=component,
            state=state,
            checked_at=checked_at,
            reason=reason,
            reconnect_count=previous.reconnect_count if previous else 0,
            dropped_count=previous.dropped_count if previous else 0,
            rejected_count=previous.rejected_count if previous else 0,
            gap_count=previous.gap_count if previous else 0,
            last_exchange_at=previous.last_exchange_at if previous else None,
            last_fetched_at=previous.last_fetched_at if previous else None,
            last_processed_at=previous.last_processed_at if previous else None,
            gap_reason=previous.gap_reason if previous else None,
            gap_detected_at=previous.gap_detected_at if previous else None,
            gap_watermark_received_at=previous.gap_watermark_received_at if previous else None,
            gap_watermark_event_timestamp=previous.gap_watermark_event_timestamp if previous else None,
            diagnostics=dict(previous.diagnostics) if previous else {},
        )
        self._snapshots[component] = snapshot
        return snapshot

    def mark_started(self, now: datetime) -> None:
        for component in self.COMPONENTS:
            self._set(component, Phase4HealthState.RUNNING, now)

    def mark_disconnected(self, component: str, now: datetime, *, reason: str = "DISCONNECTED") -> None:
        current = self._set(component, Phase4HealthState.DEGRADED, now, reason=reason)
        self._snapshots[component] = current

    def mark_reconnected(self, component: str, now: datetime) -> None:
        previous = self._snapshots.get(component)
        snapshot = self._set(component, Phase4HealthState.RECOVERED, now)
        self._snapshots[component] = replace(snapshot, reconnect_count=(previous.reconnect_count + 1 if previous else 1))

    def mark_stale(self, component: str, now: datetime, *, reason: str = "STALE") -> None:
        self._set(component, Phase4HealthState.STALE, now, reason=reason)

    def _record_failure(
        self,
        component: str,
        state: Phase4HealthState,
        now: datetime,
        *,
        reason: str,
        diagnostics: dict[str, object],
    ) -> None:
        now = _utc(now)
        snapshot = self._set(component, state, now, reason=reason)
        values = dict(snapshot.diagnostics)
        previous_failures = values.get("consecutive_failures", 0)
        values.update(diagnostics)
        values.update({
            "last_attempt_at": now.isoformat(),
            "last_error_at": now.isoformat(),
            "last_error_category": diagnostics.get("error_category"),
            "last_error_type": diagnostics.get("error_type"),
            "consecutive_failures": int(previous_failures or 0) + 1,
        })
        self._snapshots[component] = replace(snapshot, diagnostics=values)

    def mark_error(
        self,
        component: str,
        now: datetime,
        *,
        reason: str = "ERROR",
        diagnostics: dict[str, object] | None = None,
    ) -> None:
        self._record_failure(
            component, Phase4HealthState.ERROR, now, reason=reason, diagnostics=diagnostics or {}
        )

    def mark_degraded(
        self,
        component: str,
        now: datetime,
        *,
        reason: str,
        diagnostics: dict[str, object],
    ) -> None:
        self._record_failure(
            component, Phase4HealthState.DEGRADED, now, reason=reason, diagnostics=diagnostics
        )

    def mark_success(
        self,
        component: str,
        now: datetime,
        *,
        provider: str,
        endpoint: str,
        exchange: datetime | None,
        fetched: datetime,
        processed: datetime,
    ) -> None:
        now = _utc(now)
        previous = self.snapshot(component)
        target = Phase4HealthState.RUNNING if previous.state is Phase4HealthState.RUNNING else Phase4HealthState.RECOVERED
        snapshot = self._set(component, target, now)
        diagnostics = dict(snapshot.diagnostics)
        diagnostics.update({
            "provider": provider,
            "endpoint": endpoint,
            "last_attempt_at": now.isoformat(),
            "last_success_at": _utc(fetched).isoformat(),
            "consecutive_failures": 0,
            "error_category": None,
            "error_code": None,
            "error_type": None,
            "http_status": None,
            "schema_stage": None,
            "schema_error_summary": None,
        })
        self._snapshots[component] = replace(
            snapshot,
            last_exchange_at=_utc(exchange) if exchange else snapshot.last_exchange_at,
            last_fetched_at=_utc(fetched),
            last_processed_at=_utc(processed),
            diagnostics=diagnostics,
        )

    def mark_db_outage(self, component: str, now: datetime) -> None:
        self._set(component, Phase4HealthState.DEGRADED, now, reason="DB_OUTAGE")

    def mark_backpressure(self, component: str, now: datetime, *, dropped: int) -> None:
        current = self._set(component, Phase4HealthState.DEGRADED, now, reason="BACKPRESSURE")
        self._snapshots[component] = replace(current, dropped_count=current.dropped_count + dropped)

    def mark_gap(
        self,
        component: str,
        now: datetime,
        *,
        reason: str = "GAP",
        watermark_received_at: datetime | None = None,
        watermark_event_timestamp: datetime | None = None,
        count: bool = True,
    ) -> None:
        current = self._set(component, Phase4HealthState.DEGRADED, now, reason=reason)
        awaiting = "AWAITING_FRESH_EVENT" in reason
        self._snapshots[component] = replace(
            current,
            gap_count=current.gap_count + int(count),
            gap_reason=current.gap_reason if awaiting and current.gap_reason else reason,
            gap_detected_at=current.gap_detected_at if awaiting and current.gap_detected_at else _utc(now),
            gap_watermark_received_at=(
                _utc(watermark_received_at) if watermark_received_at is not None
                else current.gap_watermark_received_at
            ),
            gap_watermark_event_timestamp=(
                _utc(watermark_event_timestamp) if watermark_event_timestamp is not None
                else current.gap_watermark_event_timestamp
            ),
        )

    def mark_recovered(self, component: str, now: datetime) -> None:
        self._set(component, Phase4HealthState.RECOVERED, now)

    def mark_rejected(self, component: str, now: datetime, *, reason: str) -> None:
        """Record an intentional input rejection without degrading health."""

        current = self.snapshot(component)
        self._snapshots[component] = replace(
            current,
            checked_at=_utc(now),
            reason=reason,
            rejected_count=current.rejected_count + 1,
        )

    def snapshot(self, component: str) -> Phase4HealthSnapshot:
        if component not in self._snapshots:
            self._set(component, Phase4HealthState.STALE, datetime.now(timezone.utc), reason="NOT_STARTED")
        return self._snapshots[component]

    def snapshots(self) -> dict[str, Phase4HealthSnapshot]:
        return dict(self._snapshots)

    def update_timestamps(self, component: str, *, exchange: datetime | None = None, fetched: datetime | None = None, processed: datetime | None = None) -> None:
        current = self.snapshot(component)
        self._snapshots[component] = replace(
            current,
            last_exchange_at=_utc(exchange) if exchange else current.last_exchange_at,
            last_fetched_at=_utc(fetched) if fetched else current.last_fetched_at,
            last_processed_at=_utc(processed) if processed else current.last_processed_at,
        )
