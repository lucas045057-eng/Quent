"""Bounded public-trade queues with explicit backpressure evidence."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from quant_data_layer.admission import ReplayClass
from quant_data_layer.backpressure import BackpressureAction, decide_overload

from .contracts import CanonicalTrade, FlowStatus


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class BackpressureEvent:
    exchange: str
    symbol: str
    queue_depth: int
    queue_capacity: int
    dropped_count: int
    occurred_at: datetime
    status: FlowStatus = FlowStatus.PARTIAL
    reason: str = "BACKPRESSURE_EVENT"
    stream_id: str = ""
    replay_class: ReplayClass = ReplayClass.CANONICAL_UNRECOVERABLE
    overload_action: BackpressureAction = BackpressureAction.RECORD_GAP_AND_MARK_PARTIAL


class BoundedTradeQueue:
    def __init__(self, *, exchange: str, symbol: str, capacity: int, max_events: int = 256) -> None:
        if capacity <= 0 or max_events <= 0:
            raise ValueError("queue capacity and event limit must be positive")
        self.exchange = exchange
        self.symbol = symbol
        self.stream_id = f"phase3.{exchange.lower()}_trades"
        self.capacity = capacity
        self._queue: asyncio.Queue[CanonicalTrade] = asyncio.Queue(maxsize=capacity)
        self._events: deque[BackpressureEvent] = deque(maxlen=max_events)
        self.dropped_count = 0

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    @property
    def backpressure_events(self) -> tuple[BackpressureEvent, ...]:
        return tuple(self._events)

    def put_nowait(self, trade: CanonicalTrade, *, now: datetime) -> bool:
        current = _utc(now)
        try:
            self._queue.put_nowait(trade)
        except asyncio.QueueFull:
            decision = decide_overload(self.stream_id)
            if decision.action is not BackpressureAction.RECORD_GAP_AND_MARK_PARTIAL:
                raise RuntimeError("canonical trade queue must use explicit gap-and-partial overload behavior")
            self.dropped_count += 1
            self._events.append(
                BackpressureEvent(
                    exchange=self.exchange,
                    symbol=self.symbol,
                    queue_depth=self.depth,
                    queue_capacity=self.capacity,
                    dropped_count=self.dropped_count,
                    occurred_at=current,
                    status=FlowStatus(decision.status.value),
                    stream_id=decision.stream_id,
                    replay_class=decision.replay_class,
                    overload_action=decision.action,
                )
            )
            return False
        return True

    def get_nowait(self) -> CanonicalTrade:
        return self._queue.get_nowait()

    async def get(self) -> CanonicalTrade:
        return await self._queue.get()
