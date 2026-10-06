"""Deterministic rollups from finalized 1-minute flow windows."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .contracts import FlowStatus
from .flow import TradeFlowWindow


TIMEFRAME_SECONDS = {"5m": 300, "15m": 900, "1H": 3600, "4H": 14400}


def _floor(value: datetime, seconds: int) -> datetime:
    epoch = int(value.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=timezone.utc)


class FlowRollupBuilder:
    def build(
        self,
        minute_windows: list[TradeFlowWindow] | tuple[TradeFlowWindow, ...],
        *,
        timeframe: str,
        processed_at: datetime,
    ) -> tuple[TradeFlowWindow, ...]:
        try:
            seconds = TIMEFRAME_SECONDS[timeframe]
        except KeyError as exc:
            raise ValueError(f"unsupported Phase 3 rollup timeframe: {timeframe}") from exc
        groups: dict[tuple[str, str, datetime], dict[datetime, TradeFlowWindow]] = defaultdict(dict)
        for row in minute_windows:
            if row.timeframe != "1m":
                raise ValueError("rollups require 1m windows")
            opened = row.window_open.astimezone(timezone.utc)
            key = (row.exchange, row.canonical_symbol, _floor(opened, seconds))
            groups[key].setdefault(opened, row)
        return tuple(
            self._combine(rows, timeframe=timeframe, seconds=seconds, window_open=window_open, processed_at=processed_at)
            for (exchange, canonical_symbol, window_open), rows in sorted(groups.items())
        )

    def _combine(
        self,
        rows: dict[datetime, TradeFlowWindow],
        *,
        timeframe: str,
        seconds: int,
        window_open: datetime,
        processed_at: datetime,
    ) -> TradeFlowWindow:
        expected_count = seconds // 60
        expected = {
            window_open + timedelta(minutes=index)
            for index in range(expected_count)
        }
        missing = expected - set(rows)
        ordered = sorted(rows.values(), key=lambda row: row.window_open)
        first = ordered[0]
        total_count = sum(row.total_trade_count for row in ordered)
        total_volume = sum((row.total_volume_base for row in ordered), Decimal("0"))
        buy_volume = sum((row.buy_volume_base for row in ordered), Decimal("0"))
        sell_volume = sum((row.sell_volume_base for row in ordered), Decimal("0"))
        unknown_volume = sum((row.unknown_volume_base for row in ordered), Decimal("0"))
        total_notional = (
            sum((row.total_notional_usd for row in ordered if row.total_notional_usd is not None), Decimal("0"))
            if all(row.total_notional_usd is not None for row in ordered)
            else None
        )
        if any(row.delta_base is None for row in ordered):
            delta = None
            ratio = None
        else:
            delta = sum((row.delta_base for row in ordered if row.delta_base is not None), Decimal("0"))
            ratio = delta / total_volume if total_volume else None
        status = FlowStatus.AVAILABLE
        reason = None
        if missing:
            status, reason = FlowStatus.PARTIAL, "MISSING_MINUTE_WINDOWS"
        for row in ordered:
            if row.status is FlowStatus.ERROR:
                status, reason = FlowStatus.ERROR, row.status_reason
                break
            if row.status is FlowStatus.PARTIAL and status is not FlowStatus.ERROR:
                status, reason = FlowStatus.PARTIAL, row.status_reason
            elif row.status is FlowStatus.STALE and status is FlowStatus.AVAILABLE:
                status, reason = FlowStatus.STALE, row.status_reason
        return TradeFlowWindow(
            exchange=first.exchange,
            canonical_symbol=first.canonical_symbol,
            timeframe=timeframe,
            window_open=window_open,
            window_close=window_open + timedelta(seconds=seconds),
            total_trade_count=total_count,
            buy_trade_count=sum(row.buy_trade_count for row in ordered),
            sell_trade_count=sum(row.sell_trade_count for row in ordered),
            unknown_trade_count=sum(row.unknown_trade_count for row in ordered),
            total_volume_base=total_volume,
            buy_volume_base=buy_volume,
            sell_volume_base=sell_volume,
            unknown_volume_base=unknown_volume,
            total_notional_usd=total_notional,
            average_trade_size=total_volume / total_count if total_count else Decimal("0"),
            trade_frequency=Decimal(total_count) / Decimal(seconds),
            delta_base=delta,
            delta_ratio=ratio,
            first_trade_at=min(row.first_trade_at for row in ordered),
            last_trade_at=max(row.last_trade_at for row in ordered),
            freshness=status,
            status=status,
            status_reason=reason,
            processed_at=processed_at.astimezone(timezone.utc),
        )
