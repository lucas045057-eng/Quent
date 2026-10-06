"""Bounded source-record identity, queue, and gap state for liquidations."""

from __future__ import annotations

from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_data_layer.admission import ReplayClass
from quant_data_layer.backpressure import BackpressureAction, decide_overload
from quant_data_layer.observability import DataState

from .contracts import CanonicalLiquidation, DataStatus


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC-aware")
    return value.astimezone(timezone.utc)


def _number_identity(value: object) -> str:
    if value is None:
        return ""
    decimal_value = Decimal(str(value))
    return format(decimal_value.normalize(), "f")


def liquidation_identity_key(event: CanonicalLiquidation) -> tuple[str, ...]:
    """Return a stable exchange-source identity that intentionally excludes arrival time."""

    return (
        event.exchange,
        event.canonical_symbol,
        event.source_endpoint,
        event.event_id,
        event.event_timestamp.isoformat(),
        event.side.value,
        _number_identity(event.price),
        _number_identity(event.raw_quantity),
    )


class BoundedLiquidationDeduplicator:
    def __init__(self, *, max_entries_per_exchange: int = 10_000, ttl_seconds: int = 300) -> None:
        if max_entries_per_exchange <= 0 or ttl_seconds <= 0:
            raise ValueError("dedup limits must be positive")
        self.max_entries_per_exchange = max_entries_per_exchange
        self.ttl_seconds = ttl_seconds
        self._entries: dict[str, OrderedDict[tuple[str, ...], datetime]] = defaultdict(OrderedDict)
        self.duplicate_count = 0

    def add(self, event: CanonicalLiquidation, *, now: datetime) -> bool:
        current = _utc(now, "now")
        bucket = self._entries[event.exchange]
        expiry = current - timedelta(seconds=self.ttl_seconds)
        for key, seen_at in list(bucket.items()):
            if seen_at <= expiry:
                del bucket[key]
        key = liquidation_identity_key(event)
        if key in bucket:
            self.duplicate_count += 1
            bucket.move_to_end(key)
            return False
        bucket[key] = current
        bucket.move_to_end(key)
        while len(bucket) > self.max_entries_per_exchange:
            bucket.popitem(last=False)
        return True

    def size(self, exchange: str | None = None) -> int:
        if exchange is not None:
            return len(self._entries.get(exchange, ()))
        return sum(len(bucket) for bucket in self._entries.values())


@dataclass(frozen=True, slots=True)
class LiquidationBackpressureEvent:
    exchange: str
    symbol: str
    queue_depth: int
    queue_capacity: int
    dropped_count: int
    occurred_at: datetime
    status: DataStatus = DataStatus.STALE
    reason: str = "LIQUIDATION_BACKPRESSURE"
    stream_id: str = "phase4.liquidation_events"
    replay_class: ReplayClass = ReplayClass.CANONICAL_UNRECOVERABLE
    overload_action: BackpressureAction = BackpressureAction.RECORD_GAP_AND_MARK_PARTIAL
    completeness_status: DataState = DataState.PARTIAL


@dataclass(frozen=True, slots=True)
class LiquidationGapState:
    occurred_at: datetime
    status: DataStatus = DataStatus.STALE
    reason: str = "LIQUIDATION_GAP_NO_BACKFILL"


class BoundedLiquidationQueue:
    def __init__(self, *, exchange: str, symbol: str, capacity: int, max_events: int = 256) -> None:
        if not exchange.strip() or not symbol.strip() or capacity <= 0 or max_events <= 0:
            raise ValueError("exchange, symbol, capacity, and event limit must be positive")
        self.exchange = exchange
        self.symbol = symbol
        self.capacity = capacity
        self._queue: deque[CanonicalLiquidation] = deque(maxlen=capacity)
        self._events: deque[LiquidationBackpressureEvent] = deque(maxlen=max_events)
        self.dropped_count = 0
        self.gap_state: LiquidationGapState | None = None

    @property
    def depth(self) -> int:
        return len(self._queue)

    @property
    def backpressure_events(self) -> tuple[LiquidationBackpressureEvent, ...]:
        return tuple(self._events)

    def put_nowait(self, event: CanonicalLiquidation, *, now: datetime) -> bool:
        current = _utc(now, "now")
        if len(self._queue) >= self.capacity:
            decision = decide_overload("phase4.liquidation_events")
            if decision.action is not BackpressureAction.RECORD_GAP_AND_MARK_PARTIAL:
                raise RuntimeError("canonical liquidation queue must use explicit gap-and-partial overload behavior")
            self.dropped_count += 1
            self._events.append(
                LiquidationBackpressureEvent(
                    exchange=self.exchange,
                    symbol=self.symbol,
                    queue_depth=self.depth,
                    queue_capacity=self.capacity,
                    dropped_count=self.dropped_count,
                    occurred_at=current,
                    status=DataStatus.STALE,
                    stream_id=decision.stream_id,
                    replay_class=decision.replay_class,
                    overload_action=decision.action,
                    completeness_status=decision.status,
                )
            )
            return False
        self._queue.append(event)
        return True

    def get_nowait(self) -> CanonicalLiquidation:
        return self._queue.popleft()

    def record_disconnect_without_backfill(self, *, now: datetime) -> LiquidationGapState:
        self.gap_state = LiquidationGapState(occurred_at=_utc(now, "now"))
        return self.gap_state
