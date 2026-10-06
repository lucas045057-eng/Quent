"""Freshness gates for public trade flow context."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .contracts import FlowStatus


def _utc(value: datetime, field: str) -> datetime | None:
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class FreshnessResult:
    status: FlowStatus
    reason: str
    last_trade_at: datetime | None
    last_complete_window_at: datetime | None
    signal_eligible: bool


class TradeFreshnessEvaluator:
    def evaluate(
        self,
        *,
        now: datetime,
        last_trade_at: datetime | None,
        last_complete_window_at: datetime | None,
        trade_stale_seconds: int,
        complete_window_stale_seconds: int,
        source_connected: bool,
        has_gap: bool = False,
        partial: bool = False,
        queue_depth: int = 0,
        queue_capacity: int | None = None,
    ) -> FreshnessResult:
        current = _utc(now, "now")
        trade_at = _utc(last_trade_at, "last_trade_at") if last_trade_at is not None else None
        window_at = (
            _utc(last_complete_window_at, "last_complete_window_at")
            if last_complete_window_at is not None
            else None
        )
        if current is None or (last_trade_at is not None and trade_at is None) or (
            last_complete_window_at is not None and window_at is None
        ):
            return FreshnessResult(FlowStatus.ERROR, "NON_UTC_TIMESTAMP", trade_at, window_at, False)
        if not source_connected:
            return FreshnessResult(FlowStatus.ERROR, "SOURCE_DISCONNECTED", trade_at, window_at, False)
        if trade_at is None or window_at is None:
            return FreshnessResult(FlowStatus.NOT_AVAILABLE, "NO_COMPLETE_FLOW_DATA", trade_at, window_at, False)
        if has_gap or partial:
            return FreshnessResult(FlowStatus.PARTIAL, "TRADE_GAP_OR_PARTIAL_WINDOW", trade_at, window_at, False)
        if queue_capacity is not None and queue_depth >= queue_capacity:
            return FreshnessResult(FlowStatus.PARTIAL, "BACKPRESSURE_EVENT", trade_at, window_at, False)
        if (current - trade_at).total_seconds() > trade_stale_seconds:
            return FreshnessResult(FlowStatus.STALE, "LAST_TRADE_STALE", trade_at, window_at, False)
        if (current - window_at).total_seconds() > complete_window_stale_seconds:
            return FreshnessResult(FlowStatus.STALE, "LAST_COMPLETE_WINDOW_STALE", trade_at, window_at, False)
        return FreshnessResult(FlowStatus.AVAILABLE, "FRESH", trade_at, window_at, True)
