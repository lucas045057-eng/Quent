"""Bounded public backfill and explicit trade-gap evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .adapters.base import PublicTradeAdapter
from .contracts import CanonicalTrade, FlowStatus


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class TradeGapEvent:
    exchange: str
    canonical_symbol: str
    gap_start: datetime
    gap_end: datetime
    detected_at: datetime
    resolved_at: datetime | None
    status: FlowStatus
    reason: str
    affected_timeframe: str | None
    affected_window_open: datetime | None
    missing_count: int
    dropped_count: int
    details: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    trades: tuple[CanonicalTrade, ...]
    event: TradeGapEvent


class TradeRecoveryManager:
    def __init__(self, adapter: PublicTradeAdapter, *, max_backfill_seconds: int) -> None:
        if max_backfill_seconds <= 0:
            raise ValueError("max_backfill_seconds must be positive")
        self.adapter = adapter
        self.max_backfill_seconds = max_backfill_seconds

    def recover(
        self,
        payload: dict[str, Any],
        *,
        received_at: datetime,
        exchange_symbol: str,
        gap_start: datetime,
        gap_end: datetime,
        affected_timeframe: str = "1m",
    ) -> RecoveryResult:
        detected_at = _utc(received_at, "received_at")
        start = _utc(gap_start, "gap_start")
        end = _utc(gap_end, "gap_end")
        if end <= start:
            raise ValueError("gap_end must be after gap_start")
        window_open = start.replace(second=0, microsecond=0)
        if (end - start).total_seconds() > self.max_backfill_seconds:
            return RecoveryResult((), self._partial_event(
                start, end, detected_at, affected_timeframe, window_open, "BACKFILL_WINDOW_EXCEEDS_BOUND"
            ))
        trades = tuple(
            self.adapter.parse_recent_trades(
                payload, detected_at, exchange_symbol=exchange_symbol
            )
        )
        if not trades:
            return RecoveryResult((), self._partial_event(
                start, end, detected_at, affected_timeframe, window_open, "REST_COVERAGE_INSUFFICIENT"
            ))
        first = min(trade.exchange_timestamp for trade in trades)
        last = max(trade.exchange_timestamp for trade in trades)
        if first > start or last < end:
            return RecoveryResult((), self._partial_event(
                start, end, detected_at, affected_timeframe, window_open, "REST_COVERAGE_INSUFFICIENT"
            ))
        event = TradeGapEvent(
            exchange=self.adapter.exchange,
            canonical_symbol=trades[0].canonical_symbol or trades[0].exchange_symbol,
            gap_start=start,
            gap_end=end,
            detected_at=detected_at,
            resolved_at=detected_at,
            status=FlowStatus.AVAILABLE,
            reason="REST_BACKFILL_RESOLVED",
            affected_timeframe=affected_timeframe,
            affected_window_open=window_open,
            missing_count=0,
            dropped_count=0,
            details={"trade_count": len(trades)},
        )
        return RecoveryResult(trades, event)

    def _partial_event(
        self,
        start: datetime,
        end: datetime,
        detected_at: datetime,
        affected_timeframe: str,
        window_open: datetime,
        reason: str,
    ) -> TradeGapEvent:
        return TradeGapEvent(
            exchange=self.adapter.exchange,
            canonical_symbol="",
            gap_start=start,
            gap_end=end,
            detected_at=detected_at,
            resolved_at=None,
            status=FlowStatus.PARTIAL,
            reason=reason,
            affected_timeframe=affected_timeframe,
            affected_window_open=window_open,
            missing_count=1,
            dropped_count=0,
            details={"max_backfill_seconds": self.max_backfill_seconds},
        )
