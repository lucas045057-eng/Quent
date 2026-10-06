"""Event-time 1-minute flow aggregation from canonical trades."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .capabilities import get_trade_source_capabilities
from .contracts import CanonicalTrade, FlowStatus, TradeSide
from .ordering import EventTimeWindowRouter


@dataclass(frozen=True, slots=True)
class TradeFlowWindow:
    exchange: str
    canonical_symbol: str
    timeframe: str
    window_open: datetime
    window_close: datetime
    total_trade_count: int
    buy_trade_count: int
    sell_trade_count: int
    unknown_trade_count: int
    total_volume_base: Decimal
    buy_volume_base: Decimal
    sell_volume_base: Decimal
    unknown_volume_base: Decimal
    total_notional_usd: Decimal | None
    average_trade_size: Decimal
    trade_frequency: Decimal
    delta_base: Decimal | None
    delta_ratio: Decimal | None
    first_trade_at: datetime
    last_trade_at: datetime
    freshness: FlowStatus
    status: FlowStatus
    status_reason: str | None
    processed_at: datetime


@dataclass(slots=True)
class _Aggregate:
    exchange: str
    canonical_symbol: str
    window_open: datetime
    directional: bool
    total_trade_count: int = 0
    buy_trade_count: int = 0
    sell_trade_count: int = 0
    unknown_trade_count: int = 0
    total_volume_base: Decimal = Decimal("0")
    buy_volume_base: Decimal = Decimal("0")
    sell_volume_base: Decimal = Decimal("0")
    unknown_volume_base: Decimal = Decimal("0")
    total_notional_usd: Decimal = Decimal("0")
    notional_complete: bool = True
    first_trade_at: datetime | None = None
    last_trade_at: datetime | None = None

    def add(self, trade: CanonicalTrade) -> None:
        self.total_trade_count += 1
        self.total_volume_base += trade.quantity_base
        if trade.notional_usd is None:
            self.notional_complete = False
        elif self.notional_complete:
            self.total_notional_usd += trade.notional_usd
        if self.directional and trade.aggressor_side is TradeSide.BUY:
            self.buy_trade_count += 1
            self.buy_volume_base += trade.quantity_base
        elif self.directional and trade.aggressor_side is TradeSide.SELL:
            self.sell_trade_count += 1
            self.sell_volume_base += trade.quantity_base
        else:
            self.unknown_trade_count += 1
            self.unknown_volume_base += trade.quantity_base
        self.first_trade_at = min(self.first_trade_at or trade.exchange_timestamp, trade.exchange_timestamp)
        self.last_trade_at = max(self.last_trade_at or trade.exchange_timestamp, trade.exchange_timestamp)


class TradeFlowWindowBuilder:
    def __init__(
        self,
        *,
        timeframe: str,
        window_seconds: int,
        allowed_lateness_seconds: int,
    ) -> None:
        if not timeframe or window_seconds <= 0:
            raise ValueError("timeframe and window_seconds are required")
        self.timeframe = timeframe
        self.window_seconds = window_seconds
        self.router = EventTimeWindowRouter(
            window_seconds=window_seconds,
            allowed_lateness_seconds=allowed_lateness_seconds,
        )
        self._aggregates: dict[tuple[str, str, datetime], _Aggregate] = {}
        self._partial: dict[tuple[str, str, datetime], str] = {}

    @property
    def has_pending_partials(self) -> bool:
        return bool(self._partial)

    def add(self, trade: CanonicalTrade, *, now: datetime) -> bool:
        result = self.router.route(trade)
        if not result.accepted or result.window_open is None:
            return False
        canonical_symbol = trade.canonical_symbol or trade.exchange_symbol
        key = (trade.exchange, canonical_symbol, result.window_open)
        aggregate = self._aggregates.get(key)
        if aggregate is None:
            capabilities = get_trade_source_capabilities(trade.exchange)
            aggregate = _Aggregate(
                exchange=trade.exchange,
                canonical_symbol=canonical_symbol,
                window_open=result.window_open,
                directional=capabilities.supports_directional_flow,
            )
            self._aggregates[key] = aggregate
        aggregate.add(trade)
        return True

    def mark_partial(
        self,
        *,
        exchange: str,
        canonical_symbol: str,
        window_open: datetime,
        reason: str,
    ) -> None:
        if window_open.tzinfo is None or window_open.utcoffset() is None:
            raise ValueError("window_open must be timezone-aware UTC")
        key = (exchange, canonical_symbol, window_open.astimezone(timezone.utc))
        self._partial[key] = reason

    def finalize(self, watermark: datetime, *, processed_at: datetime) -> tuple[TradeFlowWindow, ...]:
        finalized = set(self.router.advance_watermark(watermark))
        completed: list[TradeFlowWindow] = []
        for key, aggregate in list(self._aggregates.items()):
            if aggregate.window_open not in finalized:
                continue
            reason = self._partial.pop(key, None)
            completed.append(self._build(aggregate, processed_at, reason))
            del self._aggregates[key]
        return tuple(
            sorted(completed, key=lambda row: (row.window_open, row.exchange, row.canonical_symbol))
        )

    def _build(self, aggregate: _Aggregate, processed_at: datetime, reason: str | None) -> TradeFlowWindow:
        total = aggregate.total_volume_base
        delta = aggregate.buy_volume_base - aggregate.sell_volume_base if aggregate.directional else None
        delta_ratio = delta / total if delta is not None and total else None
        return TradeFlowWindow(
            exchange=aggregate.exchange,
            canonical_symbol=aggregate.canonical_symbol,
            timeframe=self.timeframe,
            window_open=aggregate.window_open,
            window_close=aggregate.window_open + timedelta(seconds=self.window_seconds),
            total_trade_count=aggregate.total_trade_count,
            buy_trade_count=aggregate.buy_trade_count,
            sell_trade_count=aggregate.sell_trade_count,
            unknown_trade_count=aggregate.unknown_trade_count,
            total_volume_base=total,
            buy_volume_base=aggregate.buy_volume_base,
            sell_volume_base=aggregate.sell_volume_base,
            unknown_volume_base=aggregate.unknown_volume_base,
            total_notional_usd=aggregate.total_notional_usd if aggregate.notional_complete else None,
            average_trade_size=total / aggregate.total_trade_count,
            trade_frequency=Decimal(aggregate.total_trade_count) / Decimal(self.window_seconds),
            delta_base=delta,
            delta_ratio=delta_ratio,
            first_trade_at=aggregate.first_trade_at,
            last_trade_at=aggregate.last_trade_at,
            freshness=FlowStatus.PARTIAL if reason else FlowStatus.AVAILABLE,
            status=FlowStatus.PARTIAL if reason else FlowStatus.AVAILABLE,
            status_reason=reason,
            processed_at=processed_at.astimezone(timezone.utc),
        )
