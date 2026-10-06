"""Phase 7 retention bounds and health recovery bookkeeping."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from typing import Any

from .contracts import DataStatus


_PHASE7_RETENTION_TABLES = (
    "phase7_onchain_transfer_events",
    "phase7_address_labels",
    "phase7_onchain_flow_windows",
    "phase7_whale_flow_windows",
    "phase7_spot_flow_windows",
    "phase7_stablecoin_context",
    "phase7_ingestion_checkpoints",
    "stage1_phase7_context_enrichment",
)

_HEALTH_DETAIL_KEYS = frozenset({
    "missing_start", "missing_end", "missing_count", "retry_after_seconds",
    "queue_depth", "queue_capacity", "expected_parent_hash", "actual_parent_hash",
    "expected_block_hash", "actual_block_hash", "old_block_hash", "new_block_hash",
    "affected_from_cursor", "affected_to_cursor", "replacement_required", "mark_reorged",
    "head_cursor", "last_observed_cursor", "lag_cursor", "lag_seconds",
    "max_backfill", "max_catch_up", "reason", "coverage", "heartbeat", "cursor",
    "phase7_status", "data_quality", "error_category", "consecutive_failures",
    "failure_stage", "retry_policy",
})


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC-aware")
    return value


@dataclass(frozen=True, slots=True)
class Phase7RetentionPolicy:
    onchain_transfer_events_days: int = 90
    address_labels_days: int = 730
    onchain_flow_windows_days: int = 365
    whale_flow_windows_days: int = 365
    spot_flow_windows_days: int = 30
    stablecoin_context_days: int = 365
    ingestion_checkpoints_days: int = 365
    enrichment_days: int = 365

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (
                self.onchain_transfer_events_days, self.address_labels_days,
                self.onchain_flow_windows_days, self.whale_flow_windows_days,
                self.spot_flow_windows_days, self.stablecoin_context_days,
                self.ingestion_checkpoints_days, self.enrichment_days,
            )
        ):
            raise ValueError("Phase 7 retention days must be positive integers")

    def as_map(self) -> dict[str, int]:
        return {
            "phase7_onchain_transfer_events": self.onchain_transfer_events_days,
            "phase7_address_labels": self.address_labels_days,
            "phase7_onchain_flow_windows": self.onchain_flow_windows_days,
            "phase7_whale_flow_windows": self.whale_flow_windows_days,
            "phase7_spot_flow_windows": self.spot_flow_windows_days,
            "phase7_stablecoin_context": self.stablecoin_context_days,
            "phase7_ingestion_checkpoints": self.ingestion_checkpoints_days,
            "stage1_phase7_context_enrichment": self.enrichment_days,
        }

    def tables(self) -> tuple[str, ...]:
        return _PHASE7_RETENTION_TABLES

    def days_for(self, table: str) -> int:
        try:
            return self.as_map()[table]
        except KeyError as exc:
            raise ValueError("unsupported Phase 7 retention table") from exc


@dataclass(frozen=True, slots=True)
class RetentionMetric:
    table: str
    deleted: int
    status: str
    reason: str | None


def cleanup_phase7_retention(
    repository: Any,
    policy: Phase7RetentionPolicy,
    *,
    now: datetime,
    batch_size: int = 1_000,
    max_batches: int = 1,
) -> tuple[RetentionMetric, ...]:
    now = _utc(now, "now")
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError("batch_size must be positive")
    if isinstance(max_batches, bool) or not isinstance(max_batches, int) or max_batches <= 0:
        raise ValueError("max_batches must be positive")
    metrics = []
    for table in policy.tables():
        cutoff = now - timedelta(days=policy.days_for(table))
        try:
            connection = getattr(repository, "connection", None)
            transaction = getattr(connection, "transaction", None)
            if callable(transaction):
                with transaction():
                    deleted = int(repository.cleanup(
                        table, cutoff, batch_size=batch_size, max_batches=max_batches,
                    ))
            else:
                deleted = int(repository.cleanup(
                    table, cutoff, batch_size=batch_size, max_batches=max_batches,
                ))
            metrics.append(RetentionMetric(table, deleted, "AVAILABLE", None))
        except Exception as exc:  # noqa: BLE001 - failure isolation per table
            metrics.append(RetentionMetric(table, 0, "ERROR", type(exc).__name__))
    return tuple(metrics)


@dataclass(frozen=True, slots=True)
class Phase7HealthState:
    component: str
    status: DataStatus
    checked_at: datetime
    details: dict[str, Any]
    outage_started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Phase7HealthTransition:
    component: str
    status: DataStatus
    previous_status: DataStatus | None
    checked_at: datetime
    recovered: bool
    details: dict[str, Any]


class Phase7HealthTracker:
    """In-memory transition adapter for the existing system_health/runtime health path."""

    def __init__(self, repository: Any | None = None) -> None:
        self._states: dict[str, Phase7HealthState] = {}
        self.repository = repository

    def update(
        self,
        component: str,
        status: DataStatus,
        checked_at: datetime,
        details: dict[str, Any] | None = None,
    ) -> Phase7HealthTransition:
        if not isinstance(component, str) or not component.strip():
            raise ValueError("health component is required")
        if not isinstance(status, DataStatus):
            try:
                status = DataStatus(status.value if hasattr(status, "value") else status)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid health status") from exc
        checked_at = _utc(checked_at, "checked_at")
        previous = self._states.get(component)
        details = dict(details or {})
        unknown = set(details) - _HEALTH_DETAIL_KEYS
        if unknown:
            raise ValueError(f"health details contain unsupported keys: {sorted(unknown)}")
        if any(not isinstance(value, (str, int, bool, type(None))) for value in details.values()):
            raise ValueError("health details must contain scalar metadata")
        details_text = json.dumps(details, separators=(",", ":"), ensure_ascii=False)
        if len(details_text.encode("utf-8")) > 16 * 1024:
            raise ValueError("health details exceed bounded safe metadata")
        if "raw_payload" in details_text.lower() or "full_payload" in details_text.lower():
            raise ValueError("health details cannot contain raw payload")
        degraded = status in {
            DataStatus.ERROR, DataStatus.STALE, DataStatus.NOT_AVAILABLE, DataStatus.PARTIAL,
        }
        recovery_outage_started_at = previous.outage_started_at if previous is not None else None
        if degraded and (previous is None or previous.status is DataStatus.AVAILABLE):
            recovery_outage_started_at = checked_at
        state = Phase7HealthState(
            component, status, checked_at, details,
            None if status is DataStatus.AVAILABLE else recovery_outage_started_at,
        )
        self._states[component] = state
        recovered = previous is not None and previous.status in {
            DataStatus.ERROR, DataStatus.STALE, DataStatus.NOT_AVAILABLE, DataStatus.PARTIAL,
        } and status is DataStatus.AVAILABLE
        if self.repository is not None:
            from quant_phase1.contracts import DataStatus as Phase1DataStatus
            persisted_status = (
                Phase1DataStatus.NOT_AVAILABLE
                if status is DataStatus.PARTIAL else
                Phase1DataStatus(status.value)
            )
            persisted_details = {**details, "phase7_status": status.value}
            self.repository.upsert_system_health(component, persisted_status, checked_at, persisted_details)
            if recovered:
                self.repository.insert_runtime_health_event(
                    component, "RUNNING", outage_started_at=recovery_outage_started_at,
                    recovered_at=checked_at, reason="PHASE7_SOURCE_RECOVERED",
                    details=persisted_details,
                )
        return Phase7HealthTransition(
            component, status, previous.status if previous else None,
            checked_at, recovered, details,
        )

    def snapshot(self) -> dict[str, Phase7HealthState]:
        return dict(self._states)
