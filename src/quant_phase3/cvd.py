"""Bybit-only rolling CVD derived from complete 1-minute delta windows."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from .contracts import FlowStatus
from .flow import TradeFlowWindow


CVD_HORIZONS_SECONDS = {"15m": 900, "1H": 3600, "4H": 14400, "24H": 86400}


@dataclass(frozen=True, slots=True)
class CVDPoint:
    exchange: str
    canonical_symbol: str
    timeframe: str
    window_end: datetime
    value: Decimal | None
    status: FlowStatus
    reason: str | None
    processed_at: datetime


class BybitCVDBuilder:
    def __init__(self, *, max_history_minutes: int = 1440) -> None:
        if max_history_minutes <= 0:
            raise ValueError("max_history_minutes must be positive")
        self.max_history_minutes = max_history_minutes
        self._history: dict[str, OrderedDict[datetime, TradeFlowWindow]] = {}

    def hydrate(self, windows: list[TradeFlowWindow] | tuple[TradeFlowWindow, ...]) -> None:
        for window in sorted(windows, key=lambda item: item.window_open):
            if self._usable(window):
                self._remember(window)

    def ingest(self, window: TradeFlowWindow, *, processed_at: datetime) -> tuple[CVDPoint, ...]:
        if window.exchange != "bybit":
            return ()
        if not self._usable(window):
            return tuple(
                CVDPoint(
                    exchange=window.exchange,
                    canonical_symbol=window.canonical_symbol,
                    timeframe=timeframe,
                    window_end=window.window_close,
                    value=None,
                    status=FlowStatus.NOT_AVAILABLE,
                    reason=f"SOURCE_WINDOW_{window.status.value}",
                    processed_at=processed_at.astimezone(timezone.utc),
                )
                for timeframe in CVD_HORIZONS_SECONDS
            )
        self._remember(window)
        return tuple(
            self._point(window, timeframe, seconds, processed_at)
            for timeframe, seconds in CVD_HORIZONS_SECONDS.items()
        )

    def _usable(self, window: TradeFlowWindow) -> bool:
        return (
            window.exchange == "bybit"
            and window.timeframe == "1m"
            and window.delta_base is not None
            and window.status is FlowStatus.AVAILABLE
            and window.freshness is FlowStatus.AVAILABLE
        )

    def _remember(self, window: TradeFlowWindow) -> None:
        history = self._history.setdefault(window.canonical_symbol, OrderedDict())
        history[window.window_open] = window
        history.move_to_end(window.window_open)
        while len(history) > self.max_history_minutes:
            history.popitem(last=False)

    def _point(
        self, window: TradeFlowWindow, timeframe: str, seconds: int, processed_at: datetime
    ) -> CVDPoint:
        history = self._history[window.canonical_symbol]
        start = window.window_open - timedelta(seconds=seconds - 60)
        expected = {
            start + timedelta(minutes=index)
            for index in range(seconds // 60)
        }
        relevant = [item for opened, item in history.items() if start <= opened <= window.window_open]
        value = sum((item.delta_base for item in relevant if item.delta_base is not None), Decimal("0"))
        if expected - {item.window_open for item in relevant}:
            status = FlowStatus.PARTIAL
            reason = "INSUFFICIENT_HISTORY"
        else:
            status = FlowStatus.AVAILABLE
            reason = None
        return CVDPoint(
            exchange=window.exchange,
            canonical_symbol=window.canonical_symbol,
            timeframe=timeframe,
            window_end=window.window_close,
            value=value,
            status=status,
            reason=reason,
            processed_at=processed_at.astimezone(timezone.utc),
        )
