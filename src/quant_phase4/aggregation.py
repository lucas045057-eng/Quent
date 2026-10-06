"""Deterministic, bounded liquidation windows and rollups."""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging

from .contracts import CanonicalLiquidation, CoverageSemantics, DataStatus, LiquidationSide, SourceGranularity


TIMEFRAME_SECONDS = {"5m": 300, "15m": 900, "1H": 3600, "4H": 14400}
LOGGER = logging.getLogger(__name__)


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC-aware")
    return value.astimezone(timezone.utc)


def _floor(value: datetime, seconds: int) -> datetime:
    normalized = _utc(value, "timestamp")
    epoch = int(normalized.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=timezone.utc)


@dataclass(frozen=True, slots=True)
class LiquidationWindow:
    exchange: str
    canonical_symbol: str
    timeframe: str
    window_open: datetime
    window_close: datetime
    observed_event_count: int
    observed_liquidated_long_count: int
    observed_liquidated_short_count: int
    observed_notional_usd: Decimal | None
    largest_observed_notional_usd: Decimal | None
    source_exchange_count: int
    source_granularity: SourceGranularity
    coverage_semantics: CoverageSemantics
    status: DataStatus
    reason: str | None
    processed_at: datetime

    def __post_init__(self) -> None:
        if not self.exchange.strip() or not self.canonical_symbol.strip() or not self.timeframe:
            raise ValueError("window source and timeframe fields are required")
        for field in ("window_open", "window_close", "processed_at"):
            _utc(getattr(self, field), field)
        if self.window_close <= self.window_open:
            raise ValueError("window_close must follow window_open")
        if min(
            self.observed_event_count,
            self.observed_liquidated_long_count,
            self.observed_liquidated_short_count,
            self.source_exchange_count,
        ) < 0:
            raise ValueError("observed counts cannot be negative")
        if self.observed_liquidated_long_count + self.observed_liquidated_short_count > self.observed_event_count:
            raise ValueError("side counts cannot exceed observed events")


@dataclass(slots=True)
class _Aggregate:
    source_granularity: SourceGranularity
    coverage_semantics: CoverageSemantics
    event_count: int = 0
    long_count: int = 0
    short_count: int = 0
    all_verified_notional: bool = True
    notional_sum: Decimal = Decimal("0")
    largest_notional: Decimal | None = None


class LiquidationWindowBuilder:
    def __init__(self, *, max_open_windows: int = 4096) -> None:
        if max_open_windows <= 0:
            raise ValueError("max_open_windows must be positive")
        self.max_open_windows = max_open_windows
        self._aggregates: OrderedDict[tuple[str, str, datetime], _Aggregate] = OrderedDict()
        self._gaps: OrderedDict[tuple[str, str, datetime], str] = OrderedDict()
        self._pending: OrderedDict[tuple[str, str, datetime], LiquidationWindow] = OrderedDict()
        self._finalized: OrderedDict[tuple[str, str, datetime], None] = OrderedDict()
        self.dropped_window_count = 0

    def add(self, event: CanonicalLiquidation) -> bool:
        window_open = _floor(event.event_timestamp, 60)
        key = (event.exchange, event.canonical_symbol, window_open)
        if key in self._finalized or key in self._pending:
            return False
        aggregate = self._aggregates.get(key)
        if aggregate is None:
            if not self._reserve(key):
                self.dropped_window_count += 1
                return False
            # Keep only normalized aggregation metadata.  In particular, do
            # not retain the adapter's raw payload as the first event for the
            # lifetime of an open minute window.
            aggregate = _Aggregate(
                source_granularity=event.source_granularity,
                coverage_semantics=event.coverage_semantics,
            )
            self._aggregates[key] = aggregate
        elif (
            event.source_granularity is not aggregate.source_granularity
            or event.coverage_semantics is not aggregate.coverage_semantics
        ):
            raise ValueError("one liquidation window cannot mix source semantics")
        aggregate.event_count += 1
        aggregate.long_count += event.side is LiquidationSide.LIQUIDATED_LONG
        aggregate.short_count += event.side is LiquidationSide.LIQUIDATED_SHORT
        if event.notional_usd is None:
            aggregate.all_verified_notional = False
        else:
            notional = Decimal(str(event.notional_usd))
            aggregate.notional_sum += notional
            aggregate.largest_notional = max(aggregate.largest_notional or notional, notional)
        self._aggregates.move_to_end(key)
        return True

    def mark_gap(self, exchange: str, canonical_symbol: str, event_timestamp: datetime, *, reason: str) -> bool:
        if not exchange.strip() or not canonical_symbol.strip() or not reason.strip():
            raise ValueError("gap source and reason fields are required")
        key = (exchange, canonical_symbol, _floor(event_timestamp, 60))
        if key in self._finalized:
            return False
        if key not in self._aggregates and not self._reserve(key):
            self.dropped_window_count += 1
            return False
        self._gaps[key] = reason
        self._gaps.move_to_end(key)
        return True

    def finalize(self, watermark: datetime, *, processed_at: datetime) -> tuple[LiquidationWindow, ...]:
        return self._finalize(watermark, processed_at=processed_at, limit=None, mark_finalized=True)

    def finalize_pending(
        self, watermark: datetime, *, processed_at: datetime, limit: int
    ) -> tuple[LiquidationWindow, ...]:
        """Finalize at most ``limit`` windows until persistence acknowledges them."""

        if limit <= 0:
            return ()
        return self._finalize(watermark, processed_at=processed_at, limit=limit, mark_finalized=False)

    def acknowledge(self, windows: tuple[LiquidationWindow, ...] | list[LiquidationWindow]) -> None:
        """Mark windows as durably persisted so late records cannot reopen them."""

        for row in windows:
            key = (row.exchange, row.canonical_symbol, row.window_open)
            self._pending.pop(key, None)
            self._finalized[key] = None
            self._finalized.move_to_end(key)
        self._trim(self._finalized)

    def hydrate(self, windows: tuple[LiquidationWindow, ...] | list[LiquidationWindow]) -> None:
        """Restore bounded finalized identities from persisted 1-minute rows."""

        for row in windows:
            if row.timeframe != "1m":
                raise ValueError("builder hydration requires 1m windows")
            key = (row.exchange, row.canonical_symbol, _utc(row.window_open, "window_open"))
            self._aggregates.pop(key, None)
            self._gaps.pop(key, None)
            self._pending.pop(key, None)
            self._finalized[key] = None
            self._finalized.move_to_end(key)
        self._trim(self._finalized)

    def compact_finalized(self, limit: int) -> None:
        if limit <= 0:
            self._finalized.clear()
            return
        while len(self._finalized) > limit:
            self._finalized.popitem(last=False)

    def contains(self, event: CanonicalLiquidation) -> bool:
        key = (event.exchange, event.canonical_symbol, _floor(event.event_timestamp, 60))
        return key in self._aggregates or key in self._gaps or key in self._pending or key in self._finalized

    @property
    def state_count(self) -> int:
        return len(self._window_keys())

    @property
    def active_count(self) -> int:
        return len(set(self._aggregates) | set(self._gaps) | set(self._pending))

    @property
    def state_bytes(self) -> int:
        # Compact counters/enums and keys only.  The first-event payload is
        # intentionally not part of this state.
        aggregate_bytes = sum(256 + aggregate.event_count * 16 for aggregate in self._aggregates.values())
        return aggregate_bytes + len(self._gaps) * 96 + len(self._pending) * 512 + len(self._finalized) * 96

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def contains_window(self, exchange: str, canonical_symbol: str, event_timestamp: datetime) -> bool:
        """Return whether this builder already owns the minute identity."""

        key = (exchange, canonical_symbol, _floor(event_timestamp, 60))
        return key in self._window_keys()

    def _window_keys(self) -> set[tuple[str, str, datetime]]:
        return set(self._aggregates) | set(self._gaps) | set(self._pending) | set(self._finalized)

    def _finalize(
        self,
        watermark: datetime,
        *,
        processed_at: datetime,
        limit: int | None,
        mark_finalized: bool,
    ) -> tuple[LiquidationWindow, ...]:
        current = _utc(watermark, "watermark")
        processed_at = _utc(processed_at, "processed_at")
        candidates = sorted(set(self._aggregates) | set(self._gaps), key=lambda key: key[2])
        completed: list[LiquidationWindow] = []
        for key in candidates:
            if limit is not None and len(completed) >= limit:
                break
            if key[2] + timedelta(minutes=1) > current:
                continue
            aggregate = self._aggregates.pop(key, None)
            reason = self._gaps.pop(key, None)
            row = self._build(key, aggregate, processed_at, reason)
            completed.append(row)
            if mark_finalized:
                self._finalized[key] = None
                self._finalized.move_to_end(key)
            else:
                self._pending[key] = row
                self._pending.move_to_end(key)
        self._trim(self._finalized)
        return tuple(completed)

    def _reserve(self, new_key: tuple[str, str, datetime]) -> bool:
        active = set(self._aggregates) | set(self._gaps) | set(self._pending)
        if new_key in active or len(active) < self.max_open_windows:
            return True
        return False

    def _trim(self, entries: OrderedDict[tuple[str, str, datetime], object]) -> None:
        while len(entries) > self.max_open_windows:
            entries.popitem(last=False)

    def _build(
        self,
        key: tuple[str, str, datetime],
        aggregate: _Aggregate | None,
        processed_at: datetime,
        reason: str | None,
    ) -> LiquidationWindow:
        exchange, canonical_symbol, window_open = key
        if aggregate is not None:
            granularity = aggregate.source_granularity
            coverage = aggregate.coverage_semantics
        else:
            granularity = SourceGranularity.NOT_AVAILABLE
            coverage = CoverageSemantics.NOT_AVAILABLE
        all_verified = aggregate is not None and aggregate.event_count > 0 and aggregate.all_verified_notional
        return LiquidationWindow(
            exchange=exchange,
            canonical_symbol=canonical_symbol,
            timeframe="1m",
            window_open=window_open,
            window_close=window_open + timedelta(minutes=1),
            observed_event_count=aggregate.event_count if aggregate is not None else 0,
            observed_liquidated_long_count=aggregate.long_count if aggregate is not None else 0,
            observed_liquidated_short_count=aggregate.short_count if aggregate is not None else 0,
            observed_notional_usd=aggregate.notional_sum if all_verified else None,
            largest_observed_notional_usd=aggregate.largest_notional if all_verified else None,
            source_exchange_count=1 if aggregate is not None else 0,
            source_granularity=granularity,
            coverage_semantics=coverage,
            status=DataStatus.STALE if reason else DataStatus.AVAILABLE,
            reason=reason,
            processed_at=processed_at,
        )


def rollup_liquidation_windows(
    windows: list[LiquidationWindow] | tuple[LiquidationWindow, ...], timeframe: str
) -> tuple[LiquidationWindow, ...]:
    try:
        seconds = TIMEFRAME_SECONDS[timeframe]
    except KeyError as exc:
        raise ValueError(f"unsupported liquidation rollup timeframe: {timeframe}") from exc
    groups: dict[tuple[str, str, datetime], dict[datetime, LiquidationWindow]] = defaultdict(dict)
    for row in windows:
        if row.timeframe != "1m":
            raise ValueError("liquidation rollups require 1m windows")
        key = (row.exchange, row.canonical_symbol, _floor(row.window_open, seconds))
        existing = groups[key].get(row.window_open)
        if existing is not None:
            groups[key][row.window_open] = _merge_duplicate(existing, row)
        else:
            groups[key][row.window_open] = row
    return tuple(
        _combine(rows, timeframe=timeframe, seconds=seconds, window_open=window_open)
        for (_, _, window_open), rows in sorted(groups.items())
    )


def _merge_duplicate(first: LiquidationWindow, second: LiquidationWindow) -> LiquidationWindow:
    comparable = (
        "observed_event_count",
        "observed_liquidated_long_count",
        "observed_liquidated_short_count",
        "observed_notional_usd",
        "largest_observed_notional_usd",
        "source_exchange_count",
        "source_granularity",
        "coverage_semantics",
    )
    if any(getattr(first, field) != getattr(second, field) for field in comparable):
        raise ValueError("conflicting duplicate liquidation minute window")
    severity = {
        DataStatus.AVAILABLE: 0,
        DataStatus.STALE: 1,
        DataStatus.NOT_AVAILABLE: 2,
        DataStatus.ERROR: 3,
    }
    selected = first if severity[first.status] >= severity[second.status] else second
    reason = selected.reason or first.reason or second.reason
    return LiquidationWindow(
        exchange=first.exchange,
        canonical_symbol=first.canonical_symbol,
        timeframe=first.timeframe,
        window_open=first.window_open,
        window_close=first.window_close,
        observed_event_count=first.observed_event_count,
        observed_liquidated_long_count=first.observed_liquidated_long_count,
        observed_liquidated_short_count=first.observed_liquidated_short_count,
        observed_notional_usd=first.observed_notional_usd,
        largest_observed_notional_usd=first.largest_observed_notional_usd,
        source_exchange_count=first.source_exchange_count,
        source_granularity=first.source_granularity,
        coverage_semantics=first.coverage_semantics,
        status=selected.status,
        reason=reason,
        processed_at=max(first.processed_at, second.processed_at),
    )


def _combine(
    rows: dict[datetime, LiquidationWindow], *, timeframe: str, seconds: int, window_open: datetime
) -> LiquidationWindow:
    ordered = tuple(row for _, row in sorted(rows.items()))
    first = ordered[0]
    expected = {window_open + timedelta(minutes=index) for index in range(seconds // 60)}
    missing = expected - set(rows)
    if any(
        row.source_granularity is not first.source_granularity or row.coverage_semantics is not first.coverage_semantics
        for row in ordered
    ):
        LOGGER.warning(
            "liquidation_rollup_mixed_source_semantics exchange=%s symbol=%s timeframe=%s window_open=%s",
            first.exchange,
            first.canonical_symbol,
            timeframe,
            window_open.isoformat(),
        )
        return LiquidationWindow(
            exchange=first.exchange,
            canonical_symbol=first.canonical_symbol,
            timeframe=timeframe,
            window_open=window_open,
            window_close=window_open + timedelta(seconds=seconds),
            observed_event_count=0,
            observed_liquidated_long_count=0,
            observed_liquidated_short_count=0,
            observed_notional_usd=None,
            largest_observed_notional_usd=None,
            source_exchange_count=0,
            source_granularity=SourceGranularity.NOT_AVAILABLE,
            coverage_semantics=CoverageSemantics.NOT_AVAILABLE,
            status=DataStatus.ERROR,
            reason="MIXED_SOURCE_SEMANTICS",
            processed_at=max(row.processed_at for row in ordered),
        )
    all_verified = all(row.observed_notional_usd is not None for row in ordered)
    observed_notionals = [row.observed_notional_usd for row in ordered if row.observed_notional_usd is not None]
    status = DataStatus.STALE if missing else DataStatus.AVAILABLE
    reason = "MISSING_MINUTE_WINDOWS" if missing else None
    for row in ordered:
        if row.status is DataStatus.ERROR:
            status, reason = DataStatus.ERROR, row.reason
            break
        if row.status in {DataStatus.NOT_AVAILABLE, DataStatus.STALE} and status is not DataStatus.ERROR:
            status, reason = row.status, row.reason
    return LiquidationWindow(
        exchange=first.exchange,
        canonical_symbol=first.canonical_symbol,
        timeframe=timeframe,
        window_open=window_open,
        window_close=window_open + timedelta(seconds=seconds),
        observed_event_count=sum(row.observed_event_count for row in ordered),
        observed_liquidated_long_count=sum(row.observed_liquidated_long_count for row in ordered),
        observed_liquidated_short_count=sum(row.observed_liquidated_short_count for row in ordered),
        observed_notional_usd=sum(observed_notionals, Decimal("0")) if all_verified else None,
        largest_observed_notional_usd=max(
            (row.largest_observed_notional_usd for row in ordered if row.largest_observed_notional_usd is not None),
            default=None,
        ) if all_verified else None,
        source_exchange_count=max(row.source_exchange_count for row in ordered),
        source_granularity=first.source_granularity,
        coverage_semantics=first.coverage_semantics,
        status=status,
        reason=reason,
        processed_at=max(row.processed_at for row in ordered),
    )
