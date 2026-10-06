"""Event-time spot flow aggregation and descriptive spot/perp context.

The aggregator is deliberately separate from perpetual flow and Stage1 decision
logic. It only produces bounded descriptive context for the dedicated
phase7_spot_flow_windows persistence path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from itertools import islice
from typing import Any, Iterable

from .contracts import DataStatus, MarketKind
from .spot import is_approved_spot_symbol, SpotSide, SpotTradeEvent


_TIMEFRAME_DURATION = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1H": timedelta(hours=1),
    "4H": timedelta(hours=4),
}
_MAX_EVENTS = 3_000


class SpotFlowError(ValueError):
    """Spot event or comparison semantics are incompatible."""


class SpotFlowReason(StrEnum):
    NO_EVENTS = "NO_EVENTS"
    LATE_EVENT_OUTSIDE_GRACE = "LATE_EVENT_OUTSIDE_GRACE"
    SIDE_SEMANTICS_UNKNOWN = "SIDE_SEMANTICS_UNKNOWN"
    PARTIAL_SIDE_SEMANTICS = "PARTIAL_SIDE_SEMANTICS"
    MIXED_SOURCE = "MIXED_SOURCE"
    TIMESTAMP_MISMATCH = "TIMESTAMP_MISMATCH"


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise SpotFlowError(f"{field} must be UTC-aware")
    return value


def _nonnegative(value: Decimal, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise SpotFlowError(f"{field} must be a finite non-negative Decimal")
    return value


def _finite(value: Decimal, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise SpotFlowError(f"{field} must be a finite Decimal")
    return value


def _duration(timeframe: str) -> timedelta:
    try:
        return _TIMEFRAME_DURATION[timeframe]
    except KeyError as exc:
        raise SpotFlowError(f"unsupported spot timeframe: {timeframe}") from exc


def _quote_volume(event: SpotTradeEvent) -> Decimal:
    if event.quote_quantity is not None:
        return event.quote_quantity
    return event.price * event.quantity


def _coverage(available: int, sample: int) -> Decimal:
    if sample == 0:
        return Decimal("0")
    return (Decimal(available) / Decimal(sample)).quantize(Decimal("0.0001"))


@dataclass(frozen=True, slots=True)
class SpotWindowResult:
    exchange: str
    source_id: str
    symbol: str
    market_kind: MarketKind
    timeframe: str
    window_open: datetime
    window_close: datetime
    aggregation_version: str
    base_volume: Decimal
    quote_volume: Decimal
    buy_volume: Decimal | None
    sell_volume: Decimal | None
    unknown_volume: Decimal
    delta: Decimal | None
    cvd: Decimal | None
    trade_count: int
    directional_trade_count: int
    event_time_first: datetime | None
    event_time_last: datetime | None
    cursor_first: str | None
    cursor_last: str | None
    sample_count: int
    source_count: int
    available_count: int
    missing_count: int
    coverage_ratio: Decimal
    status: DataStatus
    reason: str
    source_reference: str
    normalization_version: str = "phase7-spot-flow-v1"

    def __post_init__(self) -> None:
        if self.market_kind is not MarketKind.SPOT:
            raise SpotFlowError("spot window market_kind must be SPOT")
        _utc(self.window_open, "window_open")
        _utc(self.window_close, "window_close")
        if self.window_close <= self.window_open:
            raise SpotFlowError("window_close must follow window_open")
        if self.window_close - self.window_open != _duration(self.timeframe):
            raise SpotFlowError("window bounds do not match timeframe")
        from .flow_scope import approved_sbe_spot_window
        if not (is_approved_spot_symbol(self.symbol) or
                self.source_id=='bitget:uta:sbe:xml-v4' and approved_sbe_spot_window(self.symbol,
                    exchange=self.exchange,aggregation_version=self.aggregation_version,
                    normalization_version=self.normalization_version,source_reference=self.source_reference)):
            raise SpotFlowError("spot window symbol is not approved")
        for field in ("base_volume", "quote_volume", "unknown_volume"):
            _nonnegative(getattr(self, field), field)
        for field in ("buy_volume", "sell_volume"):
            value = getattr(self, field)
            if value is not None:
                _nonnegative(value, field)
        for field in ("delta", "cvd"):
            value = getattr(self, field)
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite()):
                raise SpotFlowError(f"{field} must be a finite Decimal")
        if self.unknown_volume > self.base_volume:
            raise SpotFlowError("unknown_volume cannot exceed base_volume")
        if self.trade_count < 0 or self.directional_trade_count < 0 or self.sample_count < 0:
            raise SpotFlowError("window counts must be non-negative")
        if self.available_count < 0 or self.missing_count < 0 or self.source_count < 0:
            raise SpotFlowError("coverage counts must be non-negative")
        if self.directional_trade_count > self.trade_count:
            raise SpotFlowError("directional_trade_count cannot exceed trade_count")
        if self.available_count > self.sample_count:
            raise SpotFlowError("available_count cannot exceed sample_count")
        if not isinstance(self.status, DataStatus):
            raise SpotFlowError("status must be DataStatus")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise SpotFlowError("reason is required")
        if not Decimal("0") <= self.coverage_ratio <= Decimal("1"):
            raise SpotFlowError("coverage_ratio must be between zero and one")

    def to_row(self, *, processed_at: datetime, created_at: datetime) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "market_kind": self.market_kind.value,
            "timeframe": self.timeframe,
            "window_open": self.window_open,
            "window_close": self.window_close,
            "aggregation_version": self.aggregation_version,
            "base_volume": self.base_volume,
            "quote_volume": self.quote_volume,
            "buy_volume": self.buy_volume,
            "sell_volume": self.sell_volume,
            "unknown_volume": self.unknown_volume,
            "delta": self.delta,
            "cvd": self.cvd,
            "trade_count": self.trade_count,
            "directional_trade_count": self.directional_trade_count,
            "event_time_first": self.event_time_first,
            "event_time_last": self.event_time_last,
            "cursor_first": self.cursor_first,
            "cursor_last": self.cursor_last,
            "sample_count": self.sample_count,
            "source_count": self.source_count,
            "available_count": self.available_count,
            "missing_count": self.missing_count,
            "coverage_ratio": self.coverage_ratio,
            "status": self.status.value,
            "reason": self.reason,
            "source_reference": self.source_reference,
            "normalization_version": self.normalization_version,
            "processed_at": _utc(processed_at, "processed_at"),
            "created_at": _utc(created_at, "created_at"),
        }


def aggregate_spot_window(
    events: Iterable[SpotTradeEvent],
    *,
    window_open: datetime,
    timeframe: str,
    now: datetime,
    late_arrival_grace: timedelta = timedelta(seconds=30),
    expected_cadence: timedelta | None = None,
    freshness_grace: timedelta = timedelta(seconds=30),
    previous_cvd: Decimal | None = None,
    aggregation_version: str = "phase7-spot-flow-v1",
) -> SpotWindowResult:
    """Aggregate one exchange/source into one event-time window.

    Events outside the half-open event-time window are ignored. Events inside
    the window but fetched after window_close + late_arrival_grace are
    reported as missing rather than silently incorporated.
    """
    window_open = _utc(window_open, "window_open")
    _utc(now, "now")
    duration = _duration(timeframe)
    window_close = window_open + duration
    if not isinstance(late_arrival_grace, timedelta) or late_arrival_grace < timedelta(0):
        raise SpotFlowError("late_arrival_grace must be non-negative")
    if expected_cadence is not None and (not isinstance(expected_cadence, timedelta) or expected_cadence <= timedelta(0)):
        raise SpotFlowError("expected_cadence must be positive")
    if not isinstance(freshness_grace, timedelta) or freshness_grace < timedelta(0):
        raise SpotFlowError("freshness_grace must be non-negative")
    if previous_cvd is not None:
        _finite(previous_cvd, "previous_cvd")

    bounded = tuple(islice(events, _MAX_EVENTS + 1))
    if len(bounded) > _MAX_EVENTS:
        raise SpotFlowError("spot event batch exceeds bounded cap")
    if any(not isinstance(event, SpotTradeEvent) for event in bounded):
        raise SpotFlowError("spot aggregation requires SpotTradeEvent values")

    all_exchanges = {event.exchange for event in bounded}
    all_source_ids = {event.source_id for event in bounded}
    all_symbols = {event.symbol for event in bounded}
    if len(all_exchanges) > 1 or len(all_source_ids) > 1 or len(all_symbols) > 1:
        raise SpotFlowError("mixed exchange/source/symbol semantics cannot be blended")

    in_window = tuple(
        event for event in bounded
        if window_open <= event.event_timestamp < window_close
    )
    if not in_window:
        return _empty_result(
            bounded, symbol=None, timeframe=timeframe, window_open=window_open,
            window_close=window_close, aggregation_version=aggregation_version,
        )

    exchanges = {event.exchange for event in in_window}
    source_ids = {event.source_id for event in in_window}
    symbols = {event.symbol for event in in_window}
    if len(exchanges) != 1 or len(source_ids) != 1 or len(symbols) != 1:
        raise SpotFlowError("mixed exchange/source/symbol semantics cannot be blended")

    deduped: list[SpotTradeEvent] = []
    seen: set[tuple[str, str, str]] = set()
    late_count = 0
    for event in sorted(in_window, key=lambda item: (item.event_timestamp, item.trade_id)):
        if event.identity in seen:
            continue
        seen.add(event.identity)
        if event.fetched_at > window_close + late_arrival_grace:
            late_count += 1
            continue
        deduped.append(event)

    symbol = next(iter(symbols))
    source_id = next(iter(source_ids))
    exchange = next(iter(exchanges))
    if not deduped:
        return SpotWindowResult(
            exchange=exchange, source_id=source_id, symbol=symbol, market_kind=MarketKind.SPOT,
            timeframe=timeframe, window_open=window_open, window_close=window_close,
            aggregation_version=aggregation_version, base_volume=Decimal("0"),
            quote_volume=Decimal("0"), buy_volume=None, sell_volume=None,
            unknown_volume=Decimal("0"), delta=None, cvd=None, trade_count=0,
            directional_trade_count=0, event_time_first=None, event_time_last=None,
            cursor_first=None, cursor_last=None, sample_count=len(seen), source_count=1,
            available_count=0, missing_count=late_count, coverage_ratio=Decimal("0"),
            status=DataStatus.PARTIAL if late_count else DataStatus.NOT_AVAILABLE,
            reason=SpotFlowReason.LATE_EVENT_OUTSIDE_GRACE.value if late_count else SpotFlowReason.NO_EVENTS.value,
            source_reference=source_id,
        )

    cadence = expected_cadence or duration
    last_received_at = max(event.fetched_at for event in deduped)
    stale = now > last_received_at + cadence + freshness_grace
    base_volume = sum((event.quantity for event in deduped), Decimal("0"))
    quote_volume = sum((_quote_volume(event) for event in deduped), Decimal("0"))
    unknown_volume = sum(
        (event.quantity for event in deduped if event.side is SpotSide.UNKNOWN), Decimal("0"),
    )
    directional = tuple(event for event in deduped if event.side is not SpotSide.UNKNOWN)
    all_directional = len(directional) == len(deduped)
    buy_volume = (
        sum((event.quantity for event in directional if event.side is SpotSide.BUY), Decimal("0"))
        if all_directional else None
    )
    sell_volume = (
        sum((event.quantity for event in directional if event.side is SpotSide.SELL), Decimal("0"))
        if all_directional else None
    )
    delta = buy_volume - sell_volume if all_directional and buy_volume is not None and sell_volume is not None else None
    cvd = (previous_cvd or Decimal("0")) + delta if delta is not None else None
    ordered = sorted(deduped, key=lambda item: (item.event_timestamp, item.trade_id))
    status = (
        DataStatus.STALE if stale else
        DataStatus.PARTIAL if late_count or (unknown_volume and directional) else
        DataStatus.AVAILABLE
    )
    if stale:
        reason = "SOURCE_STALE"
    elif unknown_volume and directional:
        reason = SpotFlowReason.PARTIAL_SIDE_SEMANTICS.value
    elif unknown_volume:
        reason = SpotFlowReason.SIDE_SEMANTICS_UNKNOWN.value
    elif late_count:
        reason = SpotFlowReason.LATE_EVENT_OUTSIDE_GRACE.value
    else:
        reason = "COMPLETE"
    return SpotWindowResult(
        exchange=exchange, source_id=source_id, symbol=symbol, market_kind=MarketKind.SPOT,
        timeframe=timeframe, window_open=window_open, window_close=window_close,
        aggregation_version=aggregation_version, base_volume=base_volume,
        quote_volume=quote_volume, buy_volume=buy_volume, sell_volume=sell_volume,
        unknown_volume=unknown_volume, delta=delta, cvd=cvd, trade_count=len(deduped),
        directional_trade_count=len(directional), event_time_first=ordered[0].event_timestamp,
        event_time_last=ordered[-1].event_timestamp, cursor_first=ordered[0].trade_id,
        cursor_last=ordered[-1].trade_id, sample_count=len(seen), source_count=1,
        available_count=len(deduped), missing_count=late_count,
        coverage_ratio=_coverage(len(deduped), len(seen)), status=status, reason=reason,
        source_reference=source_id,
    )


def _empty_result(
    events: tuple[SpotTradeEvent, ...],
    *,
    symbol: str | None,
    timeframe: str,
    window_open: datetime,
    window_close: datetime,
    aggregation_version: str,
) -> SpotWindowResult:
    if symbol is None:
        symbol = events[0].symbol if events else "BTCUSDT"
    source_ids = {event.source_id for event in events}
    exchanges = {event.exchange for event in events}
    if len(source_ids) > 1 or len(exchanges) > 1:
        raise SpotFlowError("mixed source/exchange batch cannot produce an empty window")
    source_id = next(iter(source_ids), "NONE")
    exchange = next(iter(exchanges), "NONE")
    return SpotWindowResult(
        exchange=exchange, source_id=source_id, symbol=symbol, market_kind=MarketKind.SPOT,
        timeframe=timeframe, window_open=window_open, window_close=window_close,
        aggregation_version=aggregation_version, base_volume=Decimal("0"),
        quote_volume=Decimal("0"), buy_volume=None, sell_volume=None,
        unknown_volume=Decimal("0"), delta=None, cvd=None, trade_count=0,
        directional_trade_count=0, event_time_first=None, event_time_last=None,
        cursor_first=None, cursor_last=None, sample_count=0, source_count=len(source_ids),
        available_count=0, missing_count=0, coverage_ratio=Decimal("0"),
        status=DataStatus.NOT_AVAILABLE, reason=SpotFlowReason.NO_EVENTS.value,
        source_reference=source_id,
    )


@dataclass(frozen=True, slots=True)
class PerpetualFlowSnapshot:
    symbol: str
    base_volume: Decimal
    delta: Decimal | None
    event_timestamp: datetime
    market_kind: str
    base_unit: str
    status: DataStatus

    def __post_init__(self) -> None:
        if self.market_kind != "PERPETUAL":
            raise SpotFlowError("comparison requires market_kind=PERPETUAL")
        if not is_approved_spot_symbol(self.symbol):
            raise SpotFlowError("perpetual comparison symbol is not approved")
        _nonnegative(self.base_volume, "perpetual base_volume")
        _utc(self.event_timestamp, "perpetual event_timestamp")
        if self.delta is not None:
            _finite(self.delta, "perpetual delta")


@dataclass(frozen=True, slots=True)
class SpotPerpFlowContext:
    symbol: str
    spot_volume: Decimal
    perp_volume: Decimal
    spot_delta: Decimal | None
    perp_delta: Decimal | None
    timestamp_skew: timedelta
    direction_relation: str
    status: DataStatus
    reason: str


def compare_spot_perp(
    spot: SpotWindowResult,
    perp: PerpetualFlowSnapshot,
    *,
    max_timestamp_skew: timedelta = timedelta(seconds=5),
) -> SpotPerpFlowContext:
    if spot.market_kind is not MarketKind.SPOT:
        raise SpotFlowError("spot/perp comparison requires SPOT spot context")
    if spot.symbol != perp.symbol:
        raise SpotFlowError("spot/perp symbol mismatch")
    if perp.base_unit != "BASE":
        raise SpotFlowError("spot/perp base units are incompatible")
    if max_timestamp_skew < timedelta(0):
        raise SpotFlowError("max_timestamp_skew must be non-negative")
    if spot.event_time_last is None:
        return SpotPerpFlowContext(
            symbol=spot.symbol, spot_volume=spot.base_volume, perp_volume=perp.base_volume,
            spot_delta=spot.delta, perp_delta=perp.delta, timestamp_skew=timedelta.max,
            direction_relation="NOT_AVAILABLE", status=DataStatus.NOT_AVAILABLE,
            reason=SpotFlowReason.NO_EVENTS.value,
        )
    skew = abs(spot.event_time_last - perp.event_timestamp)
    if skew > max_timestamp_skew:
        return SpotPerpFlowContext(
            symbol=spot.symbol, spot_volume=spot.base_volume, perp_volume=perp.base_volume,
            spot_delta=spot.delta, perp_delta=perp.delta, timestamp_skew=skew,
            direction_relation="NOT_AVAILABLE", status=DataStatus.NOT_AVAILABLE,
            reason=SpotFlowReason.TIMESTAMP_MISMATCH.value,
        )
    if spot.delta is None or perp.delta is None:
        relation = "NOT_AVAILABLE"
    elif (spot.delta > 0 and perp.delta < 0) or (spot.delta < 0 and perp.delta > 0):
        relation = "DIVERGENCE"
    elif spot.delta == 0 or perp.delta == 0:
        relation = "NEUTRAL"
    else:
        relation = "AGREEMENT"
    statuses = (spot.status, perp.status)
    if DataStatus.ERROR in statuses:
        status, reason = DataStatus.ERROR, "INPUT_ERROR"
    elif DataStatus.STALE in statuses:
        status, reason = DataStatus.STALE, "INPUT_STALE"
    elif DataStatus.NOT_AVAILABLE in statuses:
        status, reason = DataStatus.NOT_AVAILABLE, "INPUT_NOT_AVAILABLE"
    elif DataStatus.PARTIAL in statuses:
        status, reason = DataStatus.PARTIAL, "DESCRIPTIVE_CONTEXT_ONLY"
    else:
        status, reason = DataStatus.AVAILABLE, "DESCRIPTIVE_CONTEXT_ONLY"
    return SpotPerpFlowContext(
        symbol=spot.symbol, spot_volume=spot.base_volume, perp_volume=perp.base_volume,
        spot_delta=spot.delta, perp_delta=perp.delta, timestamp_skew=skew,
        direction_relation=relation, status=status, reason=reason,
    )

