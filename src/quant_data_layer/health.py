"""Source lifecycle and data-health state contract for Data Layer V1."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import math
import re

from .errors import NormalizedError
from .observability import (
    DataState,
    ErrorCategory,
    LifecycleState,
    MAX_SOURCE_SNAPSHOTS,
    SourceId,
    SourcePhase,
    require_utc,
)


_REASON_CODE = re.compile(r"[A-Z][A-Z0-9_.-]{0,63}")


class HealthEvidenceState(StrEnum):
    NOT_EXPOSED = "NOT_EXPOSED"
    NOT_REQUIRED = "NOT_REQUIRED"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    ERROR = "ERROR"


class HealthTransitionError(RuntimeError):
    """Raised when a source cannot safely enter a requested lifecycle state."""


@dataclass(frozen=True, slots=True)
class SourceHealthSnapshot:
    source_id: SourceId
    phase: SourcePhase
    configured: bool
    lifecycle_state: LifecycleState
    data_status: DataState
    read_status: HealthEvidenceState
    contract_status: HealthEvidenceState
    processing_status: HealthEvidenceState
    persistence_status: HealthEvidenceState
    cursor_status: HealthEvidenceState
    freshness_status: HealthEvidenceState
    checked_at_utc: datetime
    last_source_timestamp_utc: datetime | None
    last_fetched_at_utc: datetime | None
    last_processed_at_utc: datetime | None
    last_persisted_at_utc: datetime | None
    last_success_at_utc: datetime | None
    freshness_age_seconds: float | None
    reason_code: str | None
    last_error: NormalizedError | None

    def __post_init__(self) -> None:
        require_utc(self.checked_at_utc)
        if not isinstance(self.source_id, SourceId) or not isinstance(self.phase, SourcePhase):
            raise TypeError("source identity must use registered enums")
        if not isinstance(self.lifecycle_state, LifecycleState) or not isinstance(self.data_status, DataState):
            raise TypeError("health states must use registered enums")
        if any(
            not isinstance(value, HealthEvidenceState)
            for value in (
                self.read_status,
                self.contract_status,
                self.processing_status,
                self.persistence_status,
                self.cursor_status,
                self.freshness_status,
            )
        ):
            raise TypeError("evidence states must use registered HealthEvidenceState values")
        if not self.configured and self.lifecycle_state is not LifecycleState.NOT_CONFIGURED:
            raise ValueError("disabled source must remain NOT_CONFIGURED")
        for timestamp in (
            self.last_source_timestamp_utc,
            self.last_fetched_at_utc,
            self.last_processed_at_utc,
            self.last_persisted_at_utc,
            self.last_success_at_utc,
        ):
            if timestamp is not None:
                require_utc(timestamp)
        if self.freshness_age_seconds is not None and (
            not math.isfinite(self.freshness_age_seconds) or self.freshness_age_seconds < 0
        ):
            raise ValueError("freshness age must be finite and non-negative")
        if self.reason_code is not None:
            _require_reason_code(self.reason_code)

    @property
    def last_error_category(self) -> ErrorCategory | None:
        return self.last_error.category if self.last_error is not None else None

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id.value,
            "phase": self.phase.value,
            "configured": self.configured,
            "lifecycle_state": self.lifecycle_state.value,
            "data_status": self.data_status.value,
            "read_status": self.read_status.value,
            "contract_status": self.contract_status.value,
            "processing_status": self.processing_status.value,
            "persistence_status": self.persistence_status.value,
            "cursor_status": self.cursor_status.value,
            "freshness_status": self.freshness_status.value,
            "checked_at_utc": _iso_utc(self.checked_at_utc),
            "last_source_timestamp_utc": _iso_utc(self.last_source_timestamp_utc),
            "last_fetched_at_utc": _iso_utc(self.last_fetched_at_utc),
            "last_processed_at_utc": _iso_utc(self.last_processed_at_utc),
            "last_persisted_at_utc": _iso_utc(self.last_persisted_at_utc),
            "last_success_at_utc": _iso_utc(self.last_success_at_utc),
            "freshness_age_seconds": self.freshness_age_seconds,
            "reason_code": self.reason_code,
            "last_error_category": self.last_error_category.value if self.last_error_category else None,
            "last_error": self.last_error.to_dict() if self.last_error else None,
        }


class SourceHealthTracker:
    """Immutable-snapshot state machine requiring fresh source/persistence proof."""

    def __init__(
        self,
        *,
        source_id: SourceId,
        phase: SourcePhase,
        configured: bool,
        persistence_required: bool,
        cursor_required: bool,
        started_at_utc: datetime,
    ) -> None:
        if not isinstance(source_id, SourceId) or not isinstance(phase, SourcePhase):
            raise TypeError("source identity must use registered enums")
        if not all(isinstance(value, bool) for value in (configured, persistence_required, cursor_required)):
            raise TypeError("source configuration flags must be boolean")
        checked_at = require_utc(started_at_utc)
        self.source_id = source_id
        self.phase = phase
        self.configured = configured
        self.persistence_required = persistence_required
        self.cursor_required = cursor_required
        self.lifecycle_state = LifecycleState.INITIALIZING if configured else LifecycleState.NOT_CONFIGURED
        self.data_status = DataState.NOT_AVAILABLE
        initial = HealthEvidenceState.NOT_AVAILABLE if configured else HealthEvidenceState.NOT_EXPOSED
        self.read_status = initial
        self.contract_status = initial
        self.processing_status = initial
        self.persistence_status = (
            HealthEvidenceState.NOT_AVAILABLE
            if configured and persistence_required
            else HealthEvidenceState.NOT_REQUIRED
            if not persistence_required
            else HealthEvidenceState.NOT_EXPOSED
        )
        self.cursor_status = (
            HealthEvidenceState.NOT_AVAILABLE
            if configured and cursor_required
            else HealthEvidenceState.NOT_REQUIRED
            if not cursor_required
            else HealthEvidenceState.NOT_EXPOSED
        )
        self.freshness_status = initial
        self.checked_at_utc = checked_at
        self.last_source_timestamp_utc: datetime | None = None
        self.last_fetched_at_utc: datetime | None = None
        self.last_processed_at_utc: datetime | None = None
        self.last_persisted_at_utc: datetime | None = None
        self.last_success_at_utc: datetime | None = None
        self.freshness_age_seconds: float | None = None
        self.reason_code: str | None = None
        self.last_error: NormalizedError | None = None

    def snapshot(self) -> SourceHealthSnapshot:
        return SourceHealthSnapshot(
            source_id=self.source_id,
            phase=self.phase,
            configured=self.configured,
            lifecycle_state=self.lifecycle_state,
            data_status=self.data_status,
            read_status=self.read_status,
            contract_status=self.contract_status,
            processing_status=self.processing_status,
            persistence_status=self.persistence_status,
            cursor_status=self.cursor_status,
            freshness_status=self.freshness_status,
            checked_at_utc=self.checked_at_utc,
            last_source_timestamp_utc=self.last_source_timestamp_utc,
            last_fetched_at_utc=self.last_fetched_at_utc,
            last_processed_at_utc=self.last_processed_at_utc,
            last_persisted_at_utc=self.last_persisted_at_utc,
            last_success_at_utc=self.last_success_at_utc,
            freshness_age_seconds=self.freshness_age_seconds,
            reason_code=self.reason_code,
            last_error=self.last_error,
        )

    def record_available(
        self,
        *,
        checked_at_utc: datetime,
        source_timestamp_utc: datetime | None,
        fetched_at_utc: datetime,
        processed_at_utc: datetime,
        persisted_at_utc: datetime | None,
        freshness_reference_at_utc: datetime,
        freshness_window_seconds: float,
        source_read_valid: bool,
        contract_valid: bool,
        gap_resolved: bool,
        cursor_reconciled: bool | None,
    ) -> SourceHealthSnapshot:
        if not self.configured:
            raise HealthTransitionError("NOT_CONFIGURED source cannot become AVAILABLE")
        if self.lifecycle_state in {LifecycleState.ERROR, LifecycleState.SHUTTING_DOWN}:
            raise HealthTransitionError("restart is required before source recovery")
        if not all(isinstance(value, bool) for value in (source_read_valid, contract_valid, gap_resolved)):
            raise TypeError("availability evidence must be boolean")
        if not source_read_valid or not contract_valid or not gap_resolved:
            raise HealthTransitionError("valid read, contract, and resolved-gap evidence are required")
        if self.persistence_required and persisted_at_utc is None:
            raise HealthTransitionError("successful persistence evidence is required")
        if self.cursor_required and cursor_reconciled is not True:
            raise HealthTransitionError("cursor reconciliation evidence is required")
        if not self.cursor_required and cursor_reconciled not in {None, False}:
            raise HealthTransitionError("cursor reconciliation was supplied for a source without a cursor")
        if not math.isfinite(freshness_window_seconds) or freshness_window_seconds <= 0:
            raise ValueError("freshness window must be finite and positive")

        checked_at = self._advance_checked_at(checked_at_utc)
        fetched_at = require_utc(fetched_at_utc)
        processed_at = require_utc(processed_at_utc)
        persisted_at = require_utc(persisted_at_utc) if persisted_at_utc is not None else None
        freshness_reference = require_utc(freshness_reference_at_utc)
        source_at = require_utc(source_timestamp_utc) if source_timestamp_utc is not None else None
        if fetched_at > processed_at or processed_at > checked_at:
            raise HealthTransitionError("fetch/process timestamps are out of order")
        if source_at is not None and source_at > fetched_at:
            raise HealthTransitionError("source timestamp is later than fetch time")
        if persisted_at is not None and (persisted_at < processed_at or persisted_at > checked_at):
            raise HealthTransitionError("persistence timestamp is out of order")
        if freshness_reference > checked_at:
            raise HealthTransitionError("freshness reference cannot be in the future")
        freshness_age = (checked_at - freshness_reference).total_seconds()
        if freshness_age > freshness_window_seconds:
            raise HealthTransitionError("source is outside its freshness window")

        self.lifecycle_state = LifecycleState.AVAILABLE
        self.data_status = DataState.AVAILABLE
        self.read_status = HealthEvidenceState.AVAILABLE
        self.contract_status = HealthEvidenceState.AVAILABLE
        self.processing_status = HealthEvidenceState.AVAILABLE
        self.persistence_status = (
            HealthEvidenceState.AVAILABLE
            if self.persistence_required
            else HealthEvidenceState.NOT_REQUIRED
        )
        self.cursor_status = (
            HealthEvidenceState.AVAILABLE
            if self.cursor_required and cursor_reconciled is True
            else HealthEvidenceState.NOT_REQUIRED
        )
        self.freshness_status = HealthEvidenceState.AVAILABLE
        self.checked_at_utc = checked_at
        self.last_source_timestamp_utc = source_at
        self.last_fetched_at_utc = fetched_at
        self.last_processed_at_utc = processed_at
        if persisted_at is not None:
            self.last_persisted_at_utc = persisted_at
        self.last_success_at_utc = checked_at
        self.freshness_age_seconds = freshness_age
        self.reason_code = None
        self.last_error = None
        return self.snapshot()

    def mark_degraded(self, error: NormalizedError, *, checked_at_utc: datetime) -> SourceHealthSnapshot:
        if not isinstance(error, NormalizedError):
            raise TypeError("a normalized error is required")
        if error.category in {ErrorCategory.PROVIDER_RATE_LIMIT, ErrorCategory.SHUTDOWN}:
            raise HealthTransitionError("use the matching lifecycle transition for this error category")
        self._transition(LifecycleState.DEGRADED, checked_at_utc, error=error)
        return self.snapshot()

    def mark_rate_limited(self, error: NormalizedError, *, checked_at_utc: datetime) -> SourceHealthSnapshot:
        if not isinstance(error, NormalizedError) or error.category is not ErrorCategory.PROVIDER_RATE_LIMIT:
            raise HealthTransitionError("RATE_LIMITED requires a normalized provider rate-limit error")
        self._transition(LifecycleState.RATE_LIMITED, checked_at_utc, error=error)
        return self.snapshot()

    def mark_stale(self, reason_code: str, *, checked_at_utc: datetime) -> SourceHealthSnapshot:
        _require_reason_code(reason_code)
        self._transition(LifecycleState.STALE, checked_at_utc, reason_code=reason_code)
        self.data_status = DataState.STALE
        self.freshness_status = HealthEvidenceState.STALE
        return self.snapshot()

    def mark_error(self, error: NormalizedError, *, checked_at_utc: datetime) -> SourceHealthSnapshot:
        if not isinstance(error, NormalizedError):
            raise TypeError("a normalized error is required")
        self._transition(LifecycleState.ERROR, checked_at_utc, error=error)
        self.data_status = DataState.ERROR
        self._apply_error_evidence(error)
        return self.snapshot()

    def shutdown(self, *, checked_at_utc: datetime) -> SourceHealthSnapshot:
        if not self.configured or self.lifecycle_state is LifecycleState.SHUTTING_DOWN:
            return self.snapshot()
        self._transition(
            LifecycleState.SHUTTING_DOWN,
            checked_at_utc,
            reason_code="SHUTDOWN_REQUESTED",
        )
        return self.snapshot()

    def restart(self, *, checked_at_utc: datetime) -> SourceHealthSnapshot:
        if not self.configured:
            raise HealthTransitionError("NOT_CONFIGURED source cannot restart")
        if self.lifecycle_state not in {LifecycleState.ERROR, LifecycleState.SHUTTING_DOWN}:
            raise HealthTransitionError("restart is allowed only after ERROR or SHUTTING_DOWN")
        self.checked_at_utc = self._advance_checked_at(checked_at_utc)
        self.lifecycle_state = LifecycleState.INITIALIZING
        self.read_status = HealthEvidenceState.NOT_AVAILABLE
        self.contract_status = HealthEvidenceState.NOT_AVAILABLE
        self.processing_status = HealthEvidenceState.NOT_AVAILABLE
        self.persistence_status = (
            HealthEvidenceState.NOT_AVAILABLE
            if self.persistence_required
            else HealthEvidenceState.NOT_REQUIRED
        )
        self.cursor_status = (
            HealthEvidenceState.NOT_AVAILABLE
            if self.cursor_required
            else HealthEvidenceState.NOT_REQUIRED
        )
        self.freshness_status = HealthEvidenceState.NOT_AVAILABLE
        self.reason_code = "RESTART_RECONCILIATION_REQUIRED"
        return self.snapshot()

    def _transition(
        self,
        target: LifecycleState,
        checked_at_utc: datetime,
        *,
        error: NormalizedError | None = None,
        reason_code: str | None = None,
    ) -> None:
        if not self.configured:
            raise HealthTransitionError("NOT_CONFIGURED source cannot change lifecycle")
        if target is LifecycleState.AVAILABLE or target is LifecycleState.INITIALIZING:
            raise HealthTransitionError("use evidence-backed recovery or restart")
        if error is None and reason_code is None:
            raise HealthTransitionError("non-success lifecycle transition requires a reason")
        if reason_code is not None:
            _require_reason_code(reason_code)
        allowed = {
            LifecycleState.INITIALIZING: {
                LifecycleState.DEGRADED,
                LifecycleState.RATE_LIMITED,
                LifecycleState.STALE,
                LifecycleState.ERROR,
                LifecycleState.SHUTTING_DOWN,
            },
            LifecycleState.AVAILABLE: {
                LifecycleState.DEGRADED,
                LifecycleState.RATE_LIMITED,
                LifecycleState.STALE,
                LifecycleState.ERROR,
                LifecycleState.SHUTTING_DOWN,
            },
            LifecycleState.DEGRADED: {
                LifecycleState.RATE_LIMITED,
                LifecycleState.STALE,
                LifecycleState.ERROR,
                LifecycleState.SHUTTING_DOWN,
            },
            LifecycleState.RATE_LIMITED: {
                LifecycleState.DEGRADED,
                LifecycleState.STALE,
                LifecycleState.ERROR,
                LifecycleState.SHUTTING_DOWN,
            },
            LifecycleState.STALE: {
                LifecycleState.DEGRADED,
                LifecycleState.RATE_LIMITED,
                LifecycleState.ERROR,
                LifecycleState.SHUTTING_DOWN,
            },
            LifecycleState.ERROR: {LifecycleState.SHUTTING_DOWN},
            LifecycleState.SHUTTING_DOWN: set(),
            LifecycleState.NOT_CONFIGURED: set(),
        }
        if target not in allowed[self.lifecycle_state]:
            raise HealthTransitionError(
                f"illegal lifecycle transition: {self.lifecycle_state.value} -> {target.value}"
            )
        self.checked_at_utc = self._advance_checked_at(checked_at_utc)
        self.lifecycle_state = target
        self.reason_code = reason_code or (error.category.value if error else None)
        self.last_error = error
        if error is not None:
            self._apply_error_evidence(error)
        if target is LifecycleState.STALE:
            self.data_status = DataState.STALE
            self.freshness_status = HealthEvidenceState.STALE
        elif target is LifecycleState.ERROR:
            self.data_status = DataState.ERROR

    def _apply_error_evidence(self, error: NormalizedError) -> None:
        if error.category in {
            ErrorCategory.PROVIDER_RATE_LIMIT,
            ErrorCategory.PROVIDER_TIMEOUT,
            ErrorCategory.NETWORK,
            ErrorCategory.CONFIGURATION,
        }:
            self.read_status = HealthEvidenceState.ERROR
        elif error.category in {ErrorCategory.PROVIDER_CONTRACT, ErrorCategory.PARSER}:
            self.contract_status = HealthEvidenceState.ERROR
        elif error.category is ErrorCategory.PERSISTENCE:
            self.persistence_status = HealthEvidenceState.ERROR
        elif error.category is ErrorCategory.CHECKPOINT:
            self.cursor_status = HealthEvidenceState.ERROR
        elif error.category is ErrorCategory.DATA_QUALITY:
            self.processing_status = HealthEvidenceState.ERROR
        elif error.category in {
            ErrorCategory.BACKPRESSURE,
            ErrorCategory.ADMISSION_TIMEOUT,
            ErrorCategory.RESOURCE,
        }:
            self.processing_status = HealthEvidenceState.PARTIAL

    def _advance_checked_at(self, value: datetime) -> datetime:
        checked_at = require_utc(value)
        if checked_at < self.checked_at_utc:
            raise HealthTransitionError("health checked_at timestamp cannot move backwards")
        return checked_at


class SourceHealthRegistry:
    """Fixed-cardinality registry for process-local source health trackers."""

    def __init__(self, *, max_sources: int = MAX_SOURCE_SNAPSHOTS) -> None:
        if not isinstance(max_sources, int) or isinstance(max_sources, bool):
            raise TypeError("max_sources must be an integer")
        if max_sources <= 0 or max_sources > MAX_SOURCE_SNAPSHOTS:
            raise ValueError(f"max_sources must be between 1 and {MAX_SOURCE_SNAPSHOTS}")
        self.max_sources = max_sources
        self._trackers: dict[SourceId, SourceHealthTracker] = {}

    def register(self, tracker: SourceHealthTracker) -> None:
        if not isinstance(tracker, SourceHealthTracker):
            raise TypeError("registry accepts SourceHealthTracker instances only")
        if tracker.source_id in self._trackers:
            raise ValueError(f"duplicate source health registration: {tracker.source_id.value}")
        if len(self._trackers) >= self.max_sources:
            raise ValueError("source health registry bound exceeded")
        self._trackers[tracker.source_id] = tracker

    def get(self, source_id: SourceId) -> SourceHealthTracker:
        return self._trackers[source_id]

    def snapshot(self) -> tuple[SourceHealthSnapshot, ...]:
        return tuple(
            self._trackers[source_id].snapshot()
            for source_id in sorted(self._trackers, key=lambda item: item.value)
        )


def _require_reason_code(value: str) -> None:
    if not isinstance(value, str) or _REASON_CODE.fullmatch(value) is None:
        raise ValueError("reason_code must be a bounded uppercase token")


def _iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return require_utc(value).isoformat().replace("+00:00", "Z")
