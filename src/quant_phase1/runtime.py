"""Bounded runtime primitives for the Phase 1 long-running services."""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Generic, TypeVar

from .adapters.bitget_v3.websocket import parse_kline_message, parse_ticker_message
from .contracts import Candle, Ticker
from quant_data_layer.errors import NormalizedError, normalize_error


T = TypeVar("T")


async def await_or_stop(awaitable: Awaitable[T], stop_event: asyncio.Event) -> T | None:
    """Await startup work, but cancel and drain it promptly when shutdown begins."""
    operation = asyncio.ensure_future(awaitable)
    stop_waiter = asyncio.create_task(stop_event.wait())
    try:
        done, _ = await asyncio.wait(
            (operation, stop_waiter),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if operation in done:
            return await operation
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        return None
    finally:
        stop_waiter.cancel()
        await asyncio.gather(stop_waiter, return_exceptions=True)
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)


class BoundedEventBuffer(Generic[T]):
    """A fixed-size queue that makes overload observable instead of unbounded."""

    def __init__(self, *, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._items: deque[T] = deque(maxlen=capacity)
        self._capacity = capacity
        self.dropped_count = 0

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def depth(self) -> int:
        """Return the current buffered item count without consuming items."""

        return len(self._items)

    def append(self, item: T) -> None:
        if len(self._items) == self._capacity:
            self.dropped_count += 1
        self._items.append(item)

    def drain(self, max_items: int | None = None) -> list[T]:
        count = len(self._items) if max_items is None else min(max_items, len(self._items))
        return [self._items.popleft() for _ in range(count)]


class PeriodicScheduler:
    """Run one non-overlapping async callback until a stop event is set."""

    def __init__(self, interval_seconds: float, callback: Callable[[], Awaitable[None]]) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.interval_seconds = interval_seconds
        self.callback = callback

    async def run(self, stop_event: asyncio.Event, *, run_immediately: bool = False) -> None:
        first = True
        while not stop_event.is_set():
            if run_immediately and first:
                first = False
            else:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
                    continue
                except asyncio.TimeoutError:
                    pass
            await self.callback()


@dataclass(frozen=True, slots=True)
class RuntimeHealthSnapshot:
    component: str
    state: str
    checked_at: datetime
    outage_started_at: datetime | None = None
    last_error: NormalizedError | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "component": self.component,
            "state": self.state,
            "checked_at_utc": self.checked_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "outage_started_at_utc": (
                self.outage_started_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                if self.outage_started_at is not None else None
            ),
            "last_error": self.last_error.to_dict() if self.last_error is not None else None,
        }


class RuntimeHealthTracker:
    def __init__(self, component: str) -> None:
        self.component = component
        self.state = "RUNNING"
        self._outage_started_at: datetime | None = None
        self._last_error: NormalizedError | None = None

    def mark_degraded(self, reason: str | BaseException | NormalizedError) -> RuntimeHealthSnapshot | None:
        now = datetime.now(timezone.utc)
        if isinstance(reason, NormalizedError):
            normalized = reason
        elif isinstance(reason, BaseException):
            normalized = normalize_error(reason)
        elif isinstance(reason, str):
            normalized = normalize_error(RuntimeError(reason))
        else:
            raise TypeError("runtime health reason must be text, an exception, or NormalizedError")
        self._last_error = normalized
        if self.state == "DEGRADED":
            return None
        self.state = "DEGRADED"
        self._outage_started_at = now
        return self.snapshot(now)

    def mark_running(self) -> RuntimeHealthSnapshot | None:
        now = datetime.now(timezone.utc)
        if self.state != "DEGRADED":
            self.state = "RUNNING"
            return None
        recovery = self.snapshot(now)
        self.state = "RUNNING"
        self._outage_started_at = None
        self._last_error = None
        return recovery

    @property
    def outage_started_at(self) -> datetime | None:
        """Read-only outage start for grace-aware health persistence."""
        return self._outage_started_at

    def snapshot(self, now: datetime | None = None) -> RuntimeHealthSnapshot:
        return RuntimeHealthSnapshot(
            component=self.component,
            state=self.state,
            checked_at=now or datetime.now(timezone.utc),
            outage_started_at=self._outage_started_at,
            last_error=self._last_error,
        )


class WebSocketCanonicalStore:
    """Latest-value store for WS canonical data with bounded closed-bar history."""

    def __init__(self, *, capacity: int = 100) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self.tickers: dict[str, Ticker] = {}
        self.closed_klines: dict[str, dict[str, deque[Candle]]] = defaultdict(dict)
        self.received_messages = 0
        self.received_tickers = 0
        self.received_klines = 0

    def ingest(self, message: dict, *, now: datetime) -> None:
        if message.get("event") in {"subscribe", "unsubscribe", "error"}:
            return
        topic = message.get("arg", {}).get("topic")
        fetched_at = now
        if not message.get("data"):
            return
        self.received_messages += 1
        if topic == "ticker":
            ticker = parse_ticker_message(message, fetched_at=fetched_at)
            self.tickers[ticker.symbol] = ticker
            self.received_tickers += 1
            return
        if topic != "kline":
            return
        candles = parse_kline_message(message, fetched_at=fetched_at, now=now)
        if not candles:
            return
        self.received_klines += len(candles)
        self.add_closed_candles(
            [
                candle
                for candle in candles
                if candle.is_closed and candle.status.value == "AVAILABLE"
            ]
        )

    def add_closed_candles(self, candles: list[Candle]) -> None:
        grouped: dict[tuple[str, str], dict[datetime, Candle]] = {}
        for candle in candles:
            if not candle.is_closed:
                continue
            key = (candle.symbol, candle.interval)
            existing = grouped.setdefault(
                key,
                {
                    item.bar_open_timestamp: item
                    for item in self.closed_klines[candle.symbol].get(candle.interval, ())
                },
            )
            existing[candle.bar_open_timestamp] = candle
        for (symbol, interval), by_open in grouped.items():
            history = self.closed_klines[symbol].setdefault(interval, deque(maxlen=self.capacity))
            history.clear()
            for bar_open in sorted(by_open)[-self.capacity :]:
                history.append(by_open[bar_open])
