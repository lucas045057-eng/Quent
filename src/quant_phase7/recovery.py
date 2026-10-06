"""Bounded Phase 7 checkpoint, gap and recovery semantics.

This module is deliberately transport-agnostic.  It plans finite work and
emits durable-health-shaped evidence; it does not call a chain or exchange.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
import json
from collections.abc import Mapping
from typing import Any
import re

from .contracts import DataStatus


class CheckpointValidationError(ValueError):
    """Raised when a cursor or recovery health event is unsafe to persist."""


class GapKind(StrEnum):
    BLOCK_HEIGHT = "BLOCK_HEIGHT"
    BLOCK_HASH = "BLOCK_HASH"
    PARENT_HASH = "PARENT_HASH"
    LOG_PAGINATION = "LOG_PAGINATION"
    TRADE_SEQUENCE = "TRADE_SEQUENCE"
    QUEUE_DROP = "QUEUE_DROP"


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CheckpointValidationError(f"{field} is required")
    normalized = value.strip()
    if len(normalized.encode("utf-8")) > limit:
        raise CheckpointValidationError(f"{field} exceeds bounded length")
    return normalized


def _utc(value: Any, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise CheckpointValidationError(f"{field} must be UTC")
    return value


def _cursor_int(value: str, field: str) -> int:
    if not isinstance(value, str) or not value.isdigit():
        raise CheckpointValidationError(f"{field} must be a non-negative decimal cursor")
    return int(value)


def _block_hash(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise CheckpointValidationError(f"{field} must be a block hash")
    normalized = value.strip()
    if re.fullmatch(r"[0-9a-f]{64}", normalized) or re.fullmatch(r"0x[0-9a-f]{64}", normalized):
        return normalized
    raise CheckpointValidationError(f"{field} must be lowercase 256-bit hex")


@dataclass(frozen=True, slots=True)
class CheckpointState:
    source_id: str
    scope_kind: str
    scope_key: str
    cursor_kind: str
    cursor_value: str
    last_observed_cursor: str | None
    last_finalized_cursor: str | None
    last_block_hash: str | None
    parser_version: str
    schema_version: str
    status: DataStatus
    reason: str | None
    updated_at: datetime

    def __post_init__(self) -> None:
        for field, limit in (
            ("source_id", 256), ("scope_key", 256), ("cursor_kind", 64),
            ("cursor_value", 512), ("parser_version", 128), ("schema_version", 128),
        ):
            _text(getattr(self, field), field, limit)
        if self.scope_kind not in {"CHAIN", "EXCHANGE"}:
            raise CheckpointValidationError("scope_kind must be CHAIN or EXCHANGE")
        if self.cursor_kind not in {"BLOCK", "LOG_INDEX", "TRADE_ID", "TRADE_SEQUENCE"}:
            raise CheckpointValidationError("unsupported cursor_kind")
        _utc(self.updated_at, "updated_at")
        if not isinstance(self.status, DataStatus):
            raise CheckpointValidationError("status must be DataStatus")
        if self.reason is not None:
            _text(self.reason, "reason", 2048)
        if self.status is not DataStatus.AVAILABLE and self.reason is None:
            raise CheckpointValidationError("degraded checkpoint requires reason")
        if self.scope_kind == "CHAIN" and self.last_block_hash is None:
            raise CheckpointValidationError("chain checkpoint requires last_block_hash")
        if self.last_block_hash is not None:
            _block_hash(self.last_block_hash, "last_block_hash")
        current = _cursor_int(self.cursor_value, "cursor_value")
        if self.last_observed_cursor is None and self.last_finalized_cursor is not None:
            raise CheckpointValidationError("finalized cursor requires observed cursor")
        if self.last_observed_cursor is not None:
            observed = _cursor_int(self.last_observed_cursor, "last_observed_cursor")
            if observed != current:
                raise CheckpointValidationError("cursor_value and last_observed_cursor must agree")
        if self.last_finalized_cursor is not None:
            finalized = _cursor_int(self.last_finalized_cursor, "last_finalized_cursor")
            if finalized > current:
                raise CheckpointValidationError("last_finalized_cursor cannot exceed cursor")
        for field in ("last_observed_cursor", "last_finalized_cursor"):
            value = getattr(self, field)
            if value is not None:
                _text(value, field, 512)

    def to_row(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "scope_kind": self.scope_kind,
            "scope_key": self.scope_key,
            "cursor_kind": self.cursor_kind,
            "cursor_value": self.cursor_value,
            "last_observed_cursor": self.last_observed_cursor,
            "last_finalized_cursor": self.last_finalized_cursor,
            "last_block_hash": self.last_block_hash,
            "parser_version": self.parser_version,
            "schema_version": self.schema_version,
            "status": self.status.value,
            "reason": self.reason,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(
        cls,
        row: Mapping[str, Any],
        *,
        expected_source_id: str | None = None,
        expected_scope_kind: str | None = None,
        expected_scope_key: str | None = None,
        expected_parser_version: str | None = None,
        expected_schema_version: str | None = None,
    ) -> "CheckpointState":
        if not isinstance(row, Mapping):
            raise CheckpointValidationError("checkpoint row must be a mapping")
        required = (
            "source_id", "scope_kind", "scope_key", "cursor_kind", "cursor_value",
            "last_observed_cursor", "last_finalized_cursor", "last_block_hash",
            "parser_version", "schema_version", "status", "reason", "updated_at",
        )
        missing = [field for field in required if field not in row]
        if missing:
            raise CheckpointValidationError(f"checkpoint row missing: {', '.join(missing)}")
        try:
            status = row["status"] if isinstance(row["status"], DataStatus) else DataStatus(row["status"])
        except (TypeError, ValueError):
            raise CheckpointValidationError("checkpoint status is invalid") from None
        checkpoint = cls(
            source_id=row["source_id"], scope_kind=row["scope_kind"], scope_key=row["scope_key"],
            cursor_kind=row["cursor_kind"], cursor_value=row["cursor_value"],
            last_observed_cursor=row["last_observed_cursor"],
            last_finalized_cursor=row["last_finalized_cursor"], last_block_hash=row["last_block_hash"],
            parser_version=row["parser_version"], schema_version=row["schema_version"],
            status=status, reason=row["reason"], updated_at=row["updated_at"],
        )
        for field, expected in (
            ("source_id", expected_source_id), ("scope_kind", expected_scope_kind),
            ("scope_key", expected_scope_key), ("parser_version", expected_parser_version),
            ("schema_version", expected_schema_version),
        ):
            if expected is not None and getattr(checkpoint, field) != expected:
                raise CheckpointValidationError(f"checkpoint {field} does not match expected runtime")
        return checkpoint


@dataclass(frozen=True, slots=True)
class CatchUpPlan:
    start_cursor: str | None
    end_cursor: str | None
    requested_count: int
    max_count: int
    status: DataStatus
    reason: str | None


@dataclass(frozen=True, slots=True)
class ReorgRecoveryPlan:
    scan_start_cursor: str
    scan_end_cursor: str
    old_block_hash: str
    new_block_hash: str
    status: DataStatus
    reason: str


@dataclass(frozen=True, slots=True)
class RecoveryHealthEvent:
    source_id: str
    scope_kind: str
    scope_key: str
    gap_kind: GapKind
    occurred_at: datetime
    status: DataStatus
    reason: str
    details: dict[str, Any]

    def __post_init__(self) -> None:
        _text(self.source_id, "source_id", 256)
        _text(self.scope_key, "scope_key", 256)
        if self.scope_kind not in {"CHAIN", "EXCHANGE"}:
            raise CheckpointValidationError("scope_kind must be CHAIN or EXCHANGE")
        if not isinstance(self.gap_kind, GapKind):
            raise CheckpointValidationError("gap_kind must be GapKind")
        _utc(self.occurred_at, "occurred_at")
        if not isinstance(self.status, DataStatus):
            raise CheckpointValidationError("status must be DataStatus")
        _text(self.reason, "reason", 128)
        if not isinstance(self.details, dict):
            raise CheckpointValidationError("details must be a dictionary")
        allowed_keys = {
            "missing_start", "missing_end", "missing_count", "retry_after_seconds",
            "queue_depth", "queue_capacity", "expected_parent_hash", "actual_parent_hash",
            "expected_block_hash", "actual_block_hash", "old_block_hash", "new_block_hash",
            "affected_from_cursor", "affected_to_cursor", "replacement_required", "mark_reorged",
            "head_cursor", "last_observed_cursor", "lag_cursor", "lag_seconds",
            "max_backfill", "max_catch_up",
        }
        unknown = set(self.details) - allowed_keys
        if unknown:
            raise CheckpointValidationError(f"health details contain unsupported keys: {sorted(unknown)}")
        if any(not isinstance(value, (str, int, bool, type(None))) for value in self.details.values()):
            raise CheckpointValidationError("health details must contain scalar metadata")
        encoded = json.dumps(self.details, default=str, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 16 * 1024:
            raise CheckpointValidationError("health details exceed bounded size")
        if "raw_payload" in encoded.lower() or "full_payload" in encoded.lower():
            raise CheckpointValidationError("health details cannot contain raw payload")

    def as_details(self) -> dict[str, Any]:
        return {
            **self.details,
            "source_id": self.source_id,
            "scope_kind": self.scope_kind,
            "scope_key": self.scope_key,
            "gap_kind": self.gap_kind.value,
        }


class Phase7RecoveryCoordinator:
    def __init__(self, *, max_catch_up: int, max_backfill: int, max_reorg_window: int = 12) -> None:
        if isinstance(max_catch_up, bool) or not isinstance(max_catch_up, int) or max_catch_up <= 0:
            raise ValueError("max_catch_up must be positive")
        if isinstance(max_backfill, bool) or not isinstance(max_backfill, int) or max_backfill <= 0:
            raise ValueError("max_backfill must be positive")
        if isinstance(max_reorg_window, bool) or not isinstance(max_reorg_window, int) or max_reorg_window <= 0:
            raise ValueError("max_reorg_window must be positive")
        self.max_catch_up = max_catch_up
        self.max_backfill = max_backfill
        self.max_reorg_window = max_reorg_window

    def load_checkpoint(self, row: Mapping[str, Any], **expected: str | None) -> CheckpointState:
        return CheckpointState.from_row(row, **expected)

    @staticmethod
    def validate_checkpoint_progress(previous: CheckpointState, candidate: CheckpointState) -> None:
        if (previous.source_id, previous.scope_kind, previous.scope_key, previous.cursor_kind) != (
            candidate.source_id, candidate.scope_kind, candidate.scope_key, candidate.cursor_kind
        ):
            raise CheckpointValidationError("checkpoint identity or cursor kind changed")
        old_cursor = _cursor_int(previous.cursor_value, "previous cursor")
        new_cursor = _cursor_int(candidate.cursor_value, "candidate cursor")
        if new_cursor < old_cursor:
            raise CheckpointValidationError("checkpoint cursor regressed")
        old_finalized = _cursor_int(previous.last_finalized_cursor, "previous finalized cursor") if previous.last_finalized_cursor else None
        new_finalized = _cursor_int(candidate.last_finalized_cursor, "candidate finalized cursor") if candidate.last_finalized_cursor else None
        if old_finalized is not None and (new_finalized is None or new_finalized < old_finalized):
            raise CheckpointValidationError("finalized cursor regressed")

    def plan_catch_up(self, checkpoint: CheckpointState | None, *, head_cursor: str) -> CatchUpPlan:
        head = _cursor_int(_text(head_cursor, "head_cursor", 512), "head_cursor")
        if checkpoint is None:
            start = max(0, head - self.max_catch_up + 1)
            count = head - start + 1
            return CatchUpPlan(
                str(start), str(head), count, self.max_catch_up,
                DataStatus.PARTIAL if start > 0 else DataStatus.AVAILABLE,
                "INITIAL_CATCH_UP_BOUND" if start > 0 else None,
            )
        current = _cursor_int(checkpoint.last_observed_cursor or checkpoint.cursor_value, "checkpoint cursor")
        if head < current:
            raise CheckpointValidationError("source head regressed below checkpoint")
        if head == current:
            return CatchUpPlan(None, None, 0, self.max_catch_up, DataStatus.AVAILABLE, None)
        requested = min(head - current, self.max_catch_up)
        end = current + requested
        bounded = head - current > self.max_catch_up
        return CatchUpPlan(
            str(current + 1), str(end), requested, self.max_catch_up,
            DataStatus.PARTIAL if bounded else DataStatus.AVAILABLE,
            "CATCH_UP_BOUND" if bounded else None,
        )

    def plan_backfill(self, *, start_cursor: str, end_cursor: str) -> CatchUpPlan:
        start = _cursor_int(_text(start_cursor, "start_cursor", 512), "start_cursor")
        end = _cursor_int(_text(end_cursor, "end_cursor", 512), "end_cursor")
        if end < start:
            raise CheckpointValidationError("backfill end cannot precede start")
        requested = end - start + 1
        bounded = requested > self.max_backfill
        bounded_end = start + self.max_backfill - 1 if bounded else end
        return CatchUpPlan(
            str(start), str(bounded_end), min(requested, self.max_backfill), self.max_backfill,
            DataStatus.PARTIAL if bounded else DataStatus.AVAILABLE,
            "BACKFILL_BOUND" if bounded else None,
        )

    def plan_reorg_window(
        self, checkpoint: CheckpointState, *, new_block_hash: str,
    ) -> ReorgRecoveryPlan:
        old_block_hash = checkpoint.last_block_hash
        if old_block_hash is None:
            raise CheckpointValidationError("reorg scan requires an existing block hash")
        new_block_hash = _block_hash(new_block_hash, "new_block_hash")
        current = _cursor_int(checkpoint.last_observed_cursor or checkpoint.cursor_value, "checkpoint cursor")
        start = max(0, current - self.max_reorg_window + 1)
        return ReorgRecoveryPlan(
            scan_start_cursor=str(start), scan_end_cursor=str(current),
            old_block_hash=old_block_hash, new_block_hash=new_block_hash,
            status=DataStatus.STALE, reason="REORG_WINDOW_REQUIRED",
        )

    def validate_parent_hash(
        self, checkpoint: CheckpointState, *, parent_hash: str, observed_at: datetime,
    ) -> RecoveryHealthEvent:
        parent_hash = _block_hash(parent_hash, "parent_hash")
        if checkpoint.last_block_hash == parent_hash:
            return self._health(
                checkpoint, GapKind.PARENT_HASH, observed_at, DataStatus.AVAILABLE,
                "PARENT_HASH_VALID", {"expected_parent_hash": parent_hash, "actual_parent_hash": parent_hash},
            )
        return self._health(
            checkpoint, GapKind.PARENT_HASH, observed_at, DataStatus.STALE,
            "REORG_RISK", {"expected_parent_hash": checkpoint.last_block_hash, "actual_parent_hash": parent_hash},
        )

    def validate_block_hash(
        self, checkpoint: CheckpointState, *, block_hash: str, observed_at: datetime,
    ) -> RecoveryHealthEvent:
        block_hash = _block_hash(block_hash, "block_hash")
        status = DataStatus.AVAILABLE if checkpoint.last_block_hash == block_hash else DataStatus.STALE
        reason = "BLOCK_HASH_VALID" if status is DataStatus.AVAILABLE else "REORG_RISK"
        return self._health(
            checkpoint, GapKind.BLOCK_HASH, observed_at, status, reason,
            {"expected_block_hash": checkpoint.last_block_hash, "actual_block_hash": block_hash},
        )

    def unresolved_gap(
        self, gap_kind: GapKind, *, source_id: str, scope_kind: str, scope_key: str,
        observed_at: datetime, details: dict[str, Any],
    ) -> RecoveryHealthEvent:
        return self._generic_health(
            source_id, scope_kind, scope_key, gap_kind, observed_at,
            DataStatus.PARTIAL, "GAP_UNRESOLVED", details,
        )

    def rate_limited(
        self, *, source_id: str, scope_kind: str, scope_key: str,
        observed_at: datetime, retry_after_seconds: int,
    ) -> RecoveryHealthEvent:
        if isinstance(retry_after_seconds, bool) or not isinstance(retry_after_seconds, int) or retry_after_seconds < 0:
            raise CheckpointValidationError("retry_after_seconds must be non-negative")
        return self._generic_health(
            source_id, scope_kind, scope_key, GapKind.LOG_PAGINATION, observed_at,
            DataStatus.PARTIAL, "RATE_LIMITED",
            {"retry_after_seconds": retry_after_seconds},
        )

    def queue_pressure(
        self, *, source_id: str, scope_kind: str, scope_key: str,
        observed_at: datetime, queue_depth: int, queue_capacity: int,
    ) -> RecoveryHealthEvent:
        if isinstance(queue_depth, bool) or not isinstance(queue_depth, int) or queue_depth < 0:
            raise CheckpointValidationError("queue_depth must be non-negative")
        if isinstance(queue_capacity, bool) or not isinstance(queue_capacity, int) or queue_capacity <= 0:
            raise CheckpointValidationError("queue_capacity must be positive")
        return self._generic_health(
            source_id, scope_kind, scope_key, GapKind.QUEUE_DROP, observed_at,
            DataStatus.PARTIAL, "QUEUE_PRESSURE",
            {"queue_depth": queue_depth, "queue_capacity": queue_capacity},
        )

    def lag(
        self, *, source_id: str, scope_kind: str, scope_key: str,
        observed_at: datetime, head_cursor: str, last_observed_cursor: str,
    ) -> RecoveryHealthEvent:
        head = _cursor_int(_text(head_cursor, "head_cursor", 512), "head_cursor")
        observed = _cursor_int(_text(last_observed_cursor, "last_observed_cursor", 512), "last_observed_cursor")
        if observed > head:
            raise CheckpointValidationError("last observed cursor exceeds source head")
        return self._generic_health(
            source_id, scope_kind, scope_key, GapKind.BLOCK_HEIGHT, observed_at,
            DataStatus.PARTIAL if head > observed else DataStatus.AVAILABLE,
            "INGESTION_LAG" if head > observed else "NO_LAG",
            {"head_cursor": str(head), "last_observed_cursor": str(observed), "lag_cursor": str(head - observed)},
        )

    def _health(
        self, checkpoint: CheckpointState, gap_kind: GapKind, occurred_at: datetime,
        status: DataStatus, reason: str, details: dict[str, Any],
    ) -> RecoveryHealthEvent:
        return RecoveryHealthEvent(
            source_id=checkpoint.source_id, scope_kind=checkpoint.scope_kind,
            scope_key=checkpoint.scope_key, gap_kind=gap_kind, occurred_at=occurred_at,
            status=status, reason=reason, details=details,
        )

    @staticmethod
    def _generic_health(
        source_id: str, scope_kind: str, scope_key: str, gap_kind: GapKind,
        occurred_at: datetime, status: DataStatus, reason: str, details: dict[str, Any],
    ) -> RecoveryHealthEvent:
        return RecoveryHealthEvent(
            source_id=source_id, scope_kind=scope_kind, scope_key=scope_key,
            gap_kind=gap_kind, occurred_at=occurred_at, status=status,
            reason=reason, details=details,
        )
