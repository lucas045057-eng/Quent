"""Compact per-exchange health state for the Phase 3 public runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .contracts import FlowStatus


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class ExchangeHealthSnapshot:
    exchange: str
    status: FlowStatus
    connected: bool
    subscribed_symbols: tuple[str, ...]
    reconnect_count: int
    gap_count: int
    dropped_trades: int
    last_event_at: datetime | None
    last_reason: str | None


@dataclass(slots=True)
class _MutableHealth:
    connected: bool = False
    status: FlowStatus = FlowStatus.NOT_AVAILABLE
    subscribed_symbols: set[str] = field(default_factory=set)
    reconnect_count: int = 0
    gap_count: int = 0
    dropped_trades: int = 0
    last_event_at: datetime | None = None
    last_reason: str | None = None


class TradeHealthRegistry:
    def __init__(self) -> None:
        self._states: dict[str, _MutableHealth] = {}

    def _state(self, exchange: str) -> _MutableHealth:
        return self._states.setdefault(exchange, _MutableHealth())

    def mark_connected(self, exchange: str, now: datetime) -> None:
        state = self._state(exchange)
        state.connected = True
        state.status = FlowStatus.AVAILABLE
        state.last_event_at = _utc(now)
        state.last_reason = None

    def mark_disconnected(self, exchange: str, now: datetime, *, reason: str = "DISCONNECTED") -> None:
        state = self._state(exchange)
        state.connected = False
        state.status = FlowStatus.PARTIAL
        state.last_event_at = _utc(now)
        state.last_reason = reason

    def record_subscription(self, exchange: str, symbol: str) -> None:
        self._state(exchange).subscribed_symbols.add(symbol)

    def remove_subscription(self, exchange: str, symbol: str) -> None:
        self._state(exchange).subscribed_symbols.discard(symbol)

    def record_reconnect(self, exchange: str, now: datetime) -> None:
        state = self._state(exchange)
        state.reconnect_count += 1
        state.connected = True
        state.status = FlowStatus.AVAILABLE
        state.last_event_at = _utc(now)

    def record_gap(self, exchange: str, *, reason: str, now: datetime) -> None:
        state = self._state(exchange)
        state.gap_count += 1
        state.status = FlowStatus.PARTIAL
        state.last_reason = reason
        state.last_event_at = _utc(now)

    def record_backpressure(self, exchange: str, *, dropped: int, now: datetime) -> None:
        state = self._state(exchange)
        state.dropped_trades += dropped
        state.gap_count += dropped
        state.status = FlowStatus.PARTIAL
        state.last_reason = "BACKPRESSURE_EVENT"
        state.last_event_at = _utc(now)

    def snapshot(self, exchange: str) -> ExchangeHealthSnapshot:
        state = self._state(exchange)
        return ExchangeHealthSnapshot(
            exchange=exchange,
            status=state.status,
            connected=state.connected,
            subscribed_symbols=tuple(sorted(state.subscribed_symbols)),
            reconnect_count=state.reconnect_count,
            gap_count=state.gap_count,
            dropped_trades=state.dropped_trades,
            last_event_at=state.last_event_at,
            last_reason=state.last_reason,
        )

    def heartbeat(self, exchange: str, now: datetime) -> dict[str, object]:
        snapshot = self.snapshot(exchange)
        current = _utc(now)
        return {
            "exchange": snapshot.exchange,
            "status": snapshot.status.value,
            "connected": snapshot.connected,
            "subscribed_symbols": len(snapshot.subscribed_symbols),
            "reconnect_count": snapshot.reconnect_count,
            "gap_count": snapshot.gap_count,
            "dropped_trades": snapshot.dropped_trades,
            "last_event_at": (snapshot.last_event_at or current).isoformat(),
            "last_reason": snapshot.last_reason,
        }
