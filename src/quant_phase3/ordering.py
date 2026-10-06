"""Event-time routing with explicit late and gap outcomes."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .contracts import FlowStatus


def _utc(value: datetime, field: str) -> datetime | None:
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class WindowRoutingResult:
    accepted: bool
    window_open: datetime | None
    status: FlowStatus
    reason: str | None = None


class EventTimeWindowRouter:
    def __init__(
        self,
        *,
        window_seconds: int,
        allowed_lateness_seconds: int,
        max_finalized_windows: int = 4096,
    ) -> None:
        if window_seconds <= 0 or allowed_lateness_seconds < 0 or max_finalized_windows <= 0:
            raise ValueError("window and finalized-window limits are invalid")
        self.window_seconds = window_seconds
        self.allowed_lateness_seconds = allowed_lateness_seconds
        self.max_finalized_windows = max_finalized_windows
        self._latest_watermark: datetime | None = None
        self._seen: OrderedDict[datetime, None] = OrderedDict()
        self._finalized: OrderedDict[datetime, None] = OrderedDict()
        self._gaps: OrderedDict[datetime, str] = OrderedDict()

    def window_open(self, event_time: datetime) -> datetime | None:
        normalized = _utc(event_time, "exchange_timestamp")
        if normalized is None:
            return None
        epoch = int(normalized.timestamp())
        return datetime.fromtimestamp(epoch - (epoch % self.window_seconds), tz=timezone.utc)

    def route(self, trade: object) -> WindowRoutingResult:
        event_time = getattr(trade, "exchange_timestamp", None)
        window_open = self.window_open(event_time) if isinstance(event_time, datetime) else None
        if window_open is None:
            return WindowRoutingResult(False, None, FlowStatus.ERROR, "NON_UTC_EVENT_TIME")
        if window_open in self._gaps:
            return WindowRoutingResult(False, window_open, FlowStatus.PARTIAL, self._gaps[window_open])
        cutoff = window_open + timedelta(seconds=self.window_seconds + self.allowed_lateness_seconds)
        if window_open in self._finalized or (
            self._latest_watermark is not None and cutoff <= self._latest_watermark
        ):
            return WindowRoutingResult(False, window_open, FlowStatus.PARTIAL, "LATE_TRADE")
        self._seen[window_open] = None
        self._seen.move_to_end(window_open)
        while len(self._seen) > self.max_finalized_windows:
            self._seen.popitem(last=False)
        return WindowRoutingResult(True, window_open, FlowStatus.AVAILABLE)

    def advance_watermark(self, watermark: datetime) -> tuple[datetime, ...]:
        normalized = _utc(watermark, "watermark")
        if normalized is None:
            raise ValueError("watermark must be timezone-aware UTC")
        if self._latest_watermark is None or normalized > self._latest_watermark:
            self._latest_watermark = normalized
        cutoff = normalized - timedelta(seconds=self.window_seconds + self.allowed_lateness_seconds)
        candidates = sorted(
            window for window in set(self._seen) | set(self._gaps) | set(self._finalized)
            if window <= cutoff
        )
        for window in candidates:
            self._remember_finalized(window)
            self._seen.pop(window, None)
        return tuple(candidates)

    def mark_gap(self, window_open: datetime, *, reason: str = "TRADE_GAP") -> None:
        normalized = _utc(window_open, "window_open")
        if normalized is None:
            raise ValueError("window_open must be timezone-aware UTC")
        self._gaps[normalized] = reason
        self._gaps.move_to_end(normalized)
        while len(self._gaps) > self.max_finalized_windows:
            self._gaps.popitem(last=False)

    def _remember_finalized(self, window_open: datetime) -> None:
        self._finalized[window_open] = None
        self._finalized.move_to_end(window_open)
        while len(self._finalized) > self.max_finalized_windows:
            self._finalized.popitem(last=False)
