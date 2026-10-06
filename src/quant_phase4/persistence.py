"""Bounded, idempotent PostgreSQL persistence for Phase 4 context metrics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, NamedTuple

from psycopg.types.json import Jsonb

from .aggregation import LiquidationWindow
from .contracts import (
    BasisObservation,
    BasisType,
    CanonicalLiquidation,
    CoverageSemantics,
    DataStatus,
    LongShortMetricType,
    LongShortObservation,
    LiquidationSide,
    QuantityUnit,
    ReasonCode,
    SourceGranularity,
)


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _reason(row: Any) -> str | None:
    reason = getattr(row, "reason_code", None)
    return _value(reason) if reason is not None else None


def _reason_code(value: Any) -> ReasonCode | None:
    if value is None:
        return None
    try:
        return ReasonCode(str(_value(value)))
    except ValueError:
        return None


class LiquidationEventRow(NamedTuple):
    exchange: str
    exchange_symbol: str
    canonical_symbol: str
    source_endpoint: str
    source_channel: str
    source_event_id: str
    event_timestamp: datetime
    received_at: datetime
    processed_at: datetime
    side: str
    raw_side: str | None
    raw_side_semantics: str | None
    price: Any
    raw_quantity: Any
    quantity_unit: str
    quantity_base: Any
    notional_usd: Any
    source_granularity: str
    coverage_semantics: str
    status: str
    reason: str | None
    raw_reference: str | None


class LiquidationWindowRow(NamedTuple):
    exchange: str
    canonical_symbol: str
    timeframe: str
    window_open: datetime
    window_close: datetime
    event_count: int
    liquidated_long_count: int
    liquidated_short_count: int
    convertible_notional_usd: Any
    largest_source_notional_usd: Any
    source_exchange_count: int
    source_granularity: str
    coverage_semantics: str
    status: str
    reason: str | None
    processed_at: datetime


class LongShortRow(NamedTuple):
    exchange: str
    exchange_symbol: str
    canonical_symbol: str
    metric_type: str
    population_semantics: str
    period: str
    long_value: Any
    short_value: Any
    ratio: Any
    exchange_timestamp: datetime
    fetched_at: datetime
    received_at: datetime
    processed_at: datetime
    source_endpoint: str
    status: str
    reason: str | None
    raw_reference: str | None


class BasisRow(NamedTuple):
    exchange: str
    exchange_symbol: str
    canonical_symbol: str
    basis_type: str
    perpetual_price: Any
    reference_price: Any
    absolute_basis: Any
    basis_bps: Any
    basis_pct: Any
    exchange_timestamp: datetime
    fetched_at: datetime
    received_at: datetime
    processed_at: datetime
    max_timestamp_skew: timedelta
    timestamp_skew: timedelta
    source_endpoint: str
    status: str
    reason: str | None
    raw_reference: str | None


class CrossExchangeRow(NamedTuple):
    canonical_symbol: str
    metric: str
    timeframe: str | None
    snapshot_timestamp: datetime
    exchange_count: int
    comparable_exchange_count: int
    missing_sources: tuple[str, ...]
    stale_sources: tuple[str, ...]
    coverage_semantics: str | None
    source_granularity: str | None
    context: dict[str, Any]
    status: str
    reason: str | None
    processed_at: datetime


def _bounded_text(value: Any, limit: int = 128) -> str:
    return str(_value(value))[:limit]


def _optional_text(value: Any, limit: int = 128) -> str | None:
    return None if value is None else _bounded_text(value, limit)


def _safe_observation(row: Any) -> dict[str, Any]:
    fields = (
        "exchange", "canonical_symbol", "metric_type", "population_semantics", "period",
        "status", "reason_code", "source_granularity", "coverage_semantics", "basis_type",
        "exchange_timestamp", "event_timestamp", "source_endpoint", "source_channel",
        "quantity_unit",
    )
    result: dict[str, Any] = {}
    for field in fields:
        value = getattr(row, field, None)
        if value is None:
            continue
        if isinstance(value, datetime):
            result[field] = _utc(value, field).isoformat()
        elif isinstance(value, (str, int, float, bool)):
            result[field] = _bounded_text(value) if isinstance(value, str) else value
        else:
            result[field] = _bounded_text(value)
    return result


def _metric_context_payload(metric_context: Any) -> dict[str, Any]:
    observations = tuple(getattr(metric_context, "observations", ()))[:64]
    return {
        "status": _value(metric_context.status),
        "source_count": int(getattr(metric_context, "source_count", 0)),
        "comparable_count": int(getattr(metric_context, "comparable_count", 0)),
        "missing_count": int(getattr(metric_context, "missing_count", 0)),
        "stale_count": int(getattr(metric_context, "stale_count", 0)),
        "error_count": int(getattr(metric_context, "error_count", 0)),
        "reason_codes": [_bounded_text(value) for value in getattr(metric_context, "reason_codes", ())[:32]],
        "reason": _optional_text(getattr(metric_context, "reason", None)),
        "source_granularity": [_bounded_text(value) for value in getattr(metric_context, "source_granularity", ())[:16]],
        "coverage_semantics": [_bounded_text(value) for value in getattr(metric_context, "coverage_semantics", ())[:16]],
        "observations": [_safe_observation(row) for row in observations],
    }


@dataclass(frozen=True, slots=True)
class Phase4Retention:
    liquidation_event_hours: int = 24
    liquidation_window_days: dict[str, int] | None = None
    long_short_days: int = 90
    basis_days: int = 90
    cross_exchange_days: int = 90
    enrichment_days: int = 90

    def __post_init__(self) -> None:
        windows = self.liquidation_window_days or {"1m": 7, "5m": 30, "15m": 90, "1H": 180, "4H": 365}
        object.__setattr__(self, "liquidation_window_days", dict(windows))
        values = [self.liquidation_event_hours, *windows.values(), self.long_short_days, self.basis_days,
                  self.cross_exchange_days, self.enrichment_days]
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
            raise ValueError("Phase 4 retention values must be positive integers")


@dataclass(frozen=True, slots=True)
class Phase4MetricContext:
    status: DataStatus
    reason: str | None = None
    row: tuple[Any, ...] | None = None


@dataclass(frozen=True, slots=True)
class Phase4Context:
    liquidation: Phase4MetricContext
    long_short: Phase4MetricContext
    basis: Phase4MetricContext


class Phase4Repository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def insert_liquidation_events(self, events: Iterable[CanonicalLiquidation]) -> int:
        rows = [
            LiquidationEventRow(
                event.exchange, event.exchange_symbol, event.canonical_symbol, event.source_endpoint,
                event.source_channel, event.event_id, _utc(event.event_timestamp, "event_timestamp"),
                _utc(event.received_at, "received_at"), _utc(event.processed_at, "processed_at"),
                _value(event.side), event.raw_side, event.raw_side_semantics, event.price, event.raw_quantity,
                _value(event.quantity_unit), event.quantity_base, event.notional_usd,
                _value(event.source_granularity), _value(event.coverage_semantics), _value(event.status),
                _reason(event), event.raw_reference,
            )
            for event in events
        ]
        if not rows:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO liquidation_events (
                exchange, exchange_symbol, canonical_symbol, source_endpoint, source_channel,
                source_event_id, event_timestamp, received_at, processed_at, side, raw_side,
                raw_side_semantics, price, raw_quantity, quantity_unit, quantity_base, notional_usd,
                source_granularity, coverage_semantics, status, reason, raw_reference
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (exchange, source_endpoint, source_event_id) DO UPDATE SET
                exchange_symbol=EXCLUDED.exchange_symbol, canonical_symbol=EXCLUDED.canonical_symbol,
                source_channel=EXCLUDED.source_channel, event_timestamp=EXCLUDED.event_timestamp,
                received_at=EXCLUDED.received_at, processed_at=EXCLUDED.processed_at, side=EXCLUDED.side,
                raw_side=EXCLUDED.raw_side, raw_side_semantics=EXCLUDED.raw_side_semantics,
                price=EXCLUDED.price, raw_quantity=EXCLUDED.raw_quantity, quantity_unit=EXCLUDED.quantity_unit,
                quantity_base=EXCLUDED.quantity_base, notional_usd=EXCLUDED.notional_usd,
                source_granularity=EXCLUDED.source_granularity, coverage_semantics=EXCLUDED.coverage_semantics,
                status=EXCLUDED.status, reason=EXCLUDED.reason, raw_reference=EXCLUDED.raw_reference
            """,
            rows,
        )
        return len(rows)

    def insert_liquidation_windows(self, windows: Iterable[LiquidationWindow]) -> int:
        rows = [
            LiquidationWindowRow(
                row.exchange, row.canonical_symbol, row.timeframe, _utc(row.window_open, "window_open"),
                _utc(row.window_close, "window_close"), row.observed_event_count,
                row.observed_liquidated_long_count, row.observed_liquidated_short_count,
                row.observed_notional_usd, row.largest_observed_notional_usd, row.source_exchange_count,
                _value(row.source_granularity), _value(row.coverage_semantics), _value(row.status), row.reason,
                _utc(row.processed_at, "processed_at"),
            )
            for row in windows
        ]
        if not rows:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO liquidation_windows (
                exchange, canonical_symbol, timeframe, window_open, window_close, event_count,
                liquidated_long_count, liquidated_short_count, convertible_notional_usd,
                largest_source_notional_usd, source_exchange_count, source_granularity,
                coverage_semantics, status, reason, processed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (exchange, canonical_symbol, timeframe, window_open) DO UPDATE SET
                window_close=EXCLUDED.window_close, event_count=EXCLUDED.event_count,
                liquidated_long_count=EXCLUDED.liquidated_long_count,
                liquidated_short_count=EXCLUDED.liquidated_short_count,
                convertible_notional_usd=EXCLUDED.convertible_notional_usd,
                largest_source_notional_usd=EXCLUDED.largest_source_notional_usd,
                source_exchange_count=EXCLUDED.source_exchange_count,
                source_granularity=EXCLUDED.source_granularity,
                coverage_semantics=EXCLUDED.coverage_semantics, status=EXCLUDED.status,
                reason=EXCLUDED.reason, processed_at=EXCLUDED.processed_at
            """,
            rows,
        )
        return len(rows)

    def load_recent_liquidation_windows(
        self, *, per_key_limit: int = 240, max_rows: int | None = None
    ) -> tuple[LiquidationWindow, ...]:
        """Load bounded normalized 1-minute state for collector restart hydration."""

        per_key_limit = max(1, int(per_key_limit))
        params: tuple[Any, ...]
        sql = """
            WITH recent AS (
                SELECT exchange, canonical_symbol, timeframe, window_open, window_close,
                       event_count, liquidated_long_count, liquidated_short_count,
                       convertible_notional_usd, largest_source_notional_usd, source_exchange_count,
                       source_granularity, coverage_semantics, status, reason, processed_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY exchange, canonical_symbol
                           ORDER BY window_open DESC
                       ) AS row_number
                FROM liquidation_windows
                WHERE timeframe = '1m'
            )
            SELECT exchange, canonical_symbol, timeframe, window_open, window_close,
                   event_count, liquidated_long_count, liquidated_short_count,
                   convertible_notional_usd, largest_source_notional_usd, source_exchange_count,
                   source_granularity, coverage_semantics, status, reason, processed_at
            FROM recent
            WHERE row_number <= %s
            ORDER BY window_open ASC
        """
        params = (per_key_limit,)
        if max_rows is not None:
            max_rows = max(1, int(max_rows))
            sql += " LIMIT %s"
            params = (*params, max_rows)
        result = self.connection.execute(sql, params).fetchall()
        windows: list[LiquidationWindow] = []
        for row in result:
            windows.append(
                LiquidationWindow(
                    exchange=row[0], canonical_symbol=row[1], timeframe=row[2],
                    window_open=_utc(row[3], "window_open"), window_close=_utc(row[4], "window_close"),
                    observed_event_count=int(row[5]), observed_liquidated_long_count=int(row[6]),
                    observed_liquidated_short_count=int(row[7]), observed_notional_usd=row[8],
                    largest_observed_notional_usd=row[9], source_exchange_count=int(row[10]),
                    source_granularity=SourceGranularity(row[11]),
                    coverage_semantics=CoverageSemantics(row[12]), status=DataStatus(row[13]),
                    reason=row[14], processed_at=_utc(row[15], "processed_at"),
                )
            )
        return tuple(windows)

    def load_recent_liquidation_rollup_keys(self, *, max_rows: int = 4096) -> tuple[tuple[str, str, str, datetime], ...]:
        """Load bounded durable rollup identities for restart write suppression."""

        result = self.connection.execute(
            "SELECT exchange, canonical_symbol, timeframe, window_open "
            "FROM liquidation_windows WHERE timeframe <> '1m' "
            "ORDER BY window_open DESC LIMIT %s",
            (max(1, int(max_rows)),),
        ).fetchall()
        return tuple((row[0], row[1], row[2], _utc(row[3], "window_open")) for row in result)

    def load_liquidation_windows_range(
        self,
        exchange: str,
        canonical_symbol: str,
        *,
        window_open_from: datetime,
        window_open_to: datetime,
        limit: int = 256,
    ) -> tuple[LiquidationWindow, ...]:
        """Load one bounded source range for deterministic rollup recovery."""

        result = self.connection.execute(
            """
            SELECT exchange, canonical_symbol, timeframe, window_open, window_close,
                   event_count, liquidated_long_count, liquidated_short_count,
                   convertible_notional_usd, largest_source_notional_usd, source_exchange_count,
                   source_granularity, coverage_semantics, status, reason, processed_at
            FROM liquidation_windows
            WHERE timeframe = '1m' AND exchange = %s AND canonical_symbol = %s
              AND window_open >= %s AND window_open < %s
            ORDER BY window_open ASC
            LIMIT %s
            """,
            (
                exchange,
                canonical_symbol,
                _utc(window_open_from, "window_open_from"),
                _utc(window_open_to, "window_open_to"),
                max(1, int(limit)),
            ),
        ).fetchall()
        return tuple(
            LiquidationWindow(
                exchange=row[0], canonical_symbol=row[1], timeframe=row[2],
                window_open=_utc(row[3], "window_open"), window_close=_utc(row[4], "window_close"),
                observed_event_count=int(row[5]), observed_liquidated_long_count=int(row[6]),
                observed_liquidated_short_count=int(row[7]), observed_notional_usd=row[8],
                largest_observed_notional_usd=row[9], source_exchange_count=int(row[10]),
                source_granularity=SourceGranularity(row[11]),
                coverage_semantics=CoverageSemantics(row[12]), status=DataStatus(row[13]),
                reason=row[14], processed_at=_utc(row[15], "processed_at"),
            )
            for row in result
        )

    def iter_liquidation_window_scopes(self, *, page_size: int = 128) -> Iterable[tuple[str, str]]:
        """Stream source scopes with bounded keyset pages."""

        page_size = max(1, int(page_size))
        last_scope: tuple[str, str] | None = None
        while True:
            if last_scope is None:
                result = self.connection.execute(
                    """
                    SELECT DISTINCT exchange, canonical_symbol
                    FROM liquidation_windows
                    WHERE timeframe = '1m'
                    ORDER BY exchange, canonical_symbol
                    LIMIT %s
                    """,
                    (page_size,),
                ).fetchall()
            else:
                result = self.connection.execute(
                    """
                    SELECT DISTINCT exchange, canonical_symbol
                    FROM liquidation_windows
                    WHERE timeframe = '1m'
                      AND (exchange, canonical_symbol) > (%s, %s)
                    ORDER BY exchange, canonical_symbol
                    LIMIT %s
                    """,
                    (*last_scope, page_size),
                ).fetchall()
            if not result:
                return
            for row in result:
                yield row[0], row[1]
            last_scope = (result[-1][0], result[-1][1])
            if len(result) < page_size:
                return

    def load_liquidation_window_scopes(self, *, page_size: int = 128) -> Iterable[tuple[str, str]]:
        """Compatibility alias for the bounded scope iterator."""

        return self.iter_liquidation_window_scopes(page_size=page_size)

    def upsert_system_health(
        self, component: str, status: DataStatus, checked_at: datetime, details: dict[str, Any]
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO system_health (component, status, checked_at, details)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (component) DO UPDATE SET
                status = EXCLUDED.status, checked_at = EXCLUDED.checked_at, details = EXCLUDED.details
            """,
            (component, status.value, _utc(checked_at, "checked_at"), Jsonb(dict(details))),
        )

    def upsert_liquidation_rollup_rebuild_marker(
        self,
        exchange: str,
        canonical_symbol: str,
        timeframe: str,
        earliest_window_open: datetime,
        latest_window_open: datetime,
        checked_at: datetime,
        *,
        cursor_exchange: str | None = None,
        cursor_symbol: str | None = None,
        cursor_timeframe: str | None = None,
        rebuild_earliest_window_open: datetime | None = None,
        rebuild_latest_window_open: datetime | None = None,
    ) -> None:
        details = {
            "kind": "PHASE4_ROLLUP_REBUILD",
            "active": True,
            "exchange": exchange,
            "canonical_symbol": canonical_symbol,
            "timeframe": timeframe,
            "earliest_window_open": _utc(earliest_window_open, "earliest_window_open").isoformat(),
            "latest_window_open": _utc(latest_window_open, "latest_window_open").isoformat(),
        }
        if (exchange, canonical_symbol, timeframe) == ("*", "*", "*"):
            details.update({
                "cursor_exchange": cursor_exchange,
                "cursor_symbol": cursor_symbol,
                "cursor_timeframe": cursor_timeframe,
                "rebuild_earliest_window_open": _utc(
                    rebuild_earliest_window_open or earliest_window_open,
                    "rebuild_earliest_window_open",
                ).isoformat(),
                "rebuild_latest_window_open": _utc(
                    rebuild_latest_window_open or latest_window_open,
                    "rebuild_latest_window_open",
                ).isoformat(),
            })
        self.upsert_system_health(
            f"phase4-rollup-rebuild:{exchange}:{canonical_symbol}:{timeframe}",
            DataStatus.STALE,
            checked_at,
            details,
        )

    def load_liquidation_rollup_rebuild_markers(
        self,
    ) -> tuple[tuple[str, str, str, datetime, datetime], ...]:
        result = self.connection.execute(
            """
            SELECT details
            FROM system_health
            WHERE component LIKE 'phase4-rollup-rebuild:%'
            """
        ).fetchall()
        markers = []
        for (details,) in result:
            if not details or details.get("active") is not True:
                continue
            marker = (
                str(details["exchange"]),
                str(details["canonical_symbol"]),
                str(details["timeframe"]),
                _utc(datetime.fromisoformat(details["earliest_window_open"]), "earliest_window_open"),
                _utc(datetime.fromisoformat(details["latest_window_open"]), "latest_window_open"),
            )
            if marker[:3] == ("*", "*", "*"):
                markers.append(marker + (
                    details.get("cursor_exchange"),
                    details.get("cursor_symbol"),
                    details.get("cursor_timeframe"),
                    _utc(
                        datetime.fromisoformat(details.get("rebuild_earliest_window_open", details["earliest_window_open"])),
                        "rebuild_earliest_window_open",
                    ),
                    _utc(
                        datetime.fromisoformat(details.get("rebuild_latest_window_open", details["latest_window_open"])),
                        "rebuild_latest_window_open",
                    ),
                ))
            else:
                markers.append(marker)
        return tuple(markers)

    def clear_liquidation_rollup_rebuild_marker(
        self, exchange: str, canonical_symbol: str, timeframe: str, checked_at: datetime
    ) -> None:
        self.connection.execute(
            """
            UPDATE system_health
            SET status = %s, checked_at = %s, details = details || %s
            WHERE component = %s
            """,
            (
                DataStatus.AVAILABLE.value,
                _utc(checked_at, "checked_at"),
                Jsonb({"active": False}),
                f"phase4-rollup-rebuild:{exchange}:{canonical_symbol}:{timeframe}",
            ),
        )

    def insert_long_short(self, observations: Iterable[LongShortObservation]) -> int:
        rows = [
            LongShortRow(
                row.exchange, row.exchange_symbol, row.canonical_symbol, _value(row.metric_type),
                _value(row.population_semantics), row.period,
                row.long_value, row.short_value, row.ratio, _utc(row.exchange_timestamp, "exchange_timestamp"),
                _utc(row.fetched_at, "fetched_at"), _utc(row.received_at, "received_at"),
                _utc(row.processed_at, "processed_at"), row.source_endpoint, _value(row.status), _reason(row),
                row.raw_reference,
            )
            for row in observations
        ]
        if not rows:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO long_short_observations (
                exchange, exchange_symbol, canonical_symbol, metric_type, population_semantics, period, long_value, short_value,
                ratio, exchange_timestamp, fetched_at, received_at, processed_at, source_endpoint,
                status, reason, raw_reference
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (exchange, canonical_symbol, metric_type, period, exchange_timestamp) DO UPDATE SET
                exchange_symbol=EXCLUDED.exchange_symbol, long_value=EXCLUDED.long_value,
                population_semantics=EXCLUDED.population_semantics, short_value=EXCLUDED.short_value,
                ratio=EXCLUDED.ratio, fetched_at=EXCLUDED.fetched_at,
                received_at=EXCLUDED.received_at, processed_at=EXCLUDED.processed_at,
                source_endpoint=EXCLUDED.source_endpoint, status=EXCLUDED.status, reason=EXCLUDED.reason,
                raw_reference=EXCLUDED.raw_reference
            """,
            rows,
        )
        return len(rows)

    def insert_basis(self, observations: Iterable[BasisObservation]) -> int:
        rows = [
            BasisRow(
                row.exchange, row.exchange_symbol, row.canonical_symbol, _value(row.basis_type),
                row.perpetual_price, row.reference_price, row.absolute_basis, row.basis_bps, row.basis_pct,
                _utc(row.exchange_timestamp, "exchange_timestamp"), _utc(row.fetched_at, "fetched_at"),
                _utc(row.received_at, "received_at"), _utc(row.processed_at, "processed_at"),
                row.max_timestamp_skew, row.timestamp_skew, row.source_endpoint, _value(row.status), _reason(row),
                row.raw_reference,
            )
            for row in observations
        ]
        if not rows:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO basis_snapshots (
                exchange, exchange_symbol, canonical_symbol, basis_type, perpetual_price, reference_price,
                absolute_basis, basis_bps, basis_pct, exchange_timestamp, fetched_at, received_at,
                processed_at, max_timestamp_skew, timestamp_skew, source_endpoint, status, reason,
                raw_reference
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (exchange, canonical_symbol, basis_type, exchange_timestamp) DO UPDATE SET
                exchange_symbol=EXCLUDED.exchange_symbol, perpetual_price=EXCLUDED.perpetual_price,
                reference_price=EXCLUDED.reference_price, absolute_basis=EXCLUDED.absolute_basis,
                basis_bps=EXCLUDED.basis_bps, basis_pct=EXCLUDED.basis_pct,
                fetched_at=EXCLUDED.fetched_at, received_at=EXCLUDED.received_at,
                processed_at=EXCLUDED.processed_at, max_timestamp_skew=EXCLUDED.max_timestamp_skew,
                timestamp_skew=EXCLUDED.timestamp_skew, source_endpoint=EXCLUDED.source_endpoint,
                status=EXCLUDED.status, reason=EXCLUDED.reason, raw_reference=EXCLUDED.raw_reference
            """,
            rows,
        )
        return len(rows)

    def load_latest_context(
        self,
        canonical_symbol: str,
        *,
        long_short_metric_type: LongShortMetricType,
        long_short_period: str,
        basis_type: BasisType,
    ) -> Phase4Context:
        liquidation = self._latest(
            "SELECT status, reason FROM liquidation_windows WHERE canonical_symbol = %s "
            "ORDER BY window_close DESC LIMIT 64",
            (canonical_symbol,),
            merge_recent_gap=True,
        )
        long_short = self._latest(
            "SELECT status, reason FROM long_short_observations "
            "WHERE canonical_symbol = %s AND metric_type = %s AND period = %s "
            "ORDER BY exchange_timestamp DESC LIMIT 1",
            (canonical_symbol, _value(long_short_metric_type), long_short_period),
        )
        basis = self._latest(
            "SELECT status, reason FROM basis_snapshots "
            "WHERE canonical_symbol = %s AND basis_type = %s "
            "ORDER BY exchange_timestamp DESC LIMIT 1",
            (canonical_symbol, _value(basis_type)),
        )
        return Phase4Context(liquidation=liquidation, long_short=long_short, basis=basis)

    def _latest(
        self, sql: str, params: tuple[Any, ...], *, merge_recent_gap: bool = False
    ) -> Phase4MetricContext:
        rows = self.connection.execute(sql, params).fetchall()
        if not rows:
            return Phase4MetricContext(DataStatus.NOT_AVAILABLE, "NO_DATA")
        row = rows[0]
        if merge_recent_gap:
            gap_rows = [candidate for candidate in rows if DataStatus(candidate[0]) in {
                DataStatus.STALE, DataStatus.NOT_AVAILABLE, DataStatus.ERROR
            }]
            if gap_rows and DataStatus(row[0]) is DataStatus.AVAILABLE:
                gap = max(gap_rows, key=lambda candidate: {
                    DataStatus.STALE: 1, DataStatus.NOT_AVAILABLE: 2, DataStatus.ERROR: 3,
                }[DataStatus(candidate[0])])
                return Phase4MetricContext(DataStatus(gap[0]), gap[1], tuple(gap))
        return Phase4MetricContext(DataStatus(row[0]), row[1], tuple(row))

    def load_latest_observations(
        self,
        canonical_symbol: str,
        *,
        long_short_metric_type: LongShortMetricType,
        long_short_period: str,
        basis_type: BasisType,
    ) -> tuple[tuple[Any, ...], tuple[Any, ...], tuple[Any, ...]]:
        """Load bounded normalized rows for context reconstruction; never raw payloads."""
        from types import SimpleNamespace

        queries = (
            (
                "SELECT exchange, source_event_id, exchange_symbol, canonical_symbol, event_timestamp, "
                "received_at, side, raw_side, raw_side_semantics, price, raw_quantity, quantity_unit, "
                "quantity_base, notional_usd, source_endpoint, source_channel, source_granularity, "
                "coverage_semantics, status, reason, raw_reference, processed_at "
                "FROM liquidation_events WHERE canonical_symbol = %s "
                "ORDER BY event_timestamp DESC LIMIT 64",
                (canonical_symbol,),
            ),
            (
                "SELECT exchange, exchange_symbol, canonical_symbol, metric_type, population_semantics, "
                "period, exchange_timestamp, status, reason, source_endpoint, fetched_at, received_at, processed_at "
                "FROM long_short_observations WHERE canonical_symbol = %s AND metric_type = %s AND period = %s "
                "ORDER BY exchange_timestamp DESC LIMIT 64",
                (canonical_symbol, _value(long_short_metric_type), long_short_period),
            ),
            (
                "SELECT exchange, exchange_symbol, canonical_symbol, basis_type, exchange_timestamp, status, reason, "
                "source_endpoint, fetched_at, received_at, processed_at FROM basis_snapshots "
                "WHERE canonical_symbol = %s AND basis_type = %s ORDER BY exchange_timestamp DESC LIMIT 64",
                (canonical_symbol, _value(basis_type)),
            ),
            (
                "SELECT exchange, canonical_symbol, timeframe, window_open, window_close, event_count, "
                "liquidated_long_count, liquidated_short_count, convertible_notional_usd, "
                "largest_source_notional_usd, source_exchange_count, source_granularity, coverage_semantics, "
                "status, reason, processed_at FROM liquidation_windows "
                "WHERE canonical_symbol = %s AND timeframe = '1m' "
                "ORDER BY window_close DESC LIMIT 64",
                (canonical_symbol,),
            ),
        )
        loaded: list[tuple[Any, ...]] = []
        for sql, params in queries:
            result = self.connection.execute(sql, params).fetchall()
            loaded.append(tuple(result))
        liquidation = [
            SimpleNamespace(
                event_id=row[1], exchange=row[0], exchange_symbol=row[2], canonical_symbol=row[3],
                event_timestamp=row[4], received_at=row[5], side=LiquidationSide(row[6]),
                raw_side=row[7], raw_side_semantics=row[8], price=row[9], raw_quantity=row[10],
                quantity_unit=QuantityUnit(row[11]), quantity_base=row[12], notional_usd=row[13],
                source_endpoint=row[14], source_channel=row[15], source_granularity=SourceGranularity(row[16]),
                coverage_semantics=CoverageSemantics(row[17]), status=DataStatus(row[18]),
                reason_code=_reason_code(row[19]), reason=row[19], raw_reference=row[20],
                processed_at=row[21], timeframe=None,
            ) for row in loaded[0]
        ]
        # A persisted gap may have no liquidation_events row at all.  Add a
        # bounded synthetic context observation so the engine cannot rebuild
        # an AVAILABLE liquidation context from events that predate the gap.
        known_gap_keys = set()
        for row in loaded[3]:
            status = DataStatus(row[13])
            if status is DataStatus.AVAILABLE:
                continue
            key = (row[0], row[1], row[3])
            if key in known_gap_keys:
                continue
            known_gap_keys.add(key)
            liquidation.append(
                SimpleNamespace(
                    event_id=f"persisted-gap:{row[0]}:{row[1]}:{row[3].isoformat()}",
                    exchange=row[0], exchange_symbol=row[1], canonical_symbol=canonical_symbol,
                    event_timestamp=row[3], received_at=row[15], processed_at=row[15],
                    side=LiquidationSide.UNKNOWN, raw_side=None, raw_side_semantics=None,
                    price=None, raw_quantity=None, quantity_unit=QuantityUnit.UNKNOWN,
                    quantity_base=None, notional_usd=None,
                    source_endpoint="persisted/liquidation_window", source_channel="liquidation_window",
                    source_granularity=SourceGranularity(row[11]),
                    coverage_semantics=CoverageSemantics(row[12]), status=status,
                    reason_code=_reason_code(row[14]), reason=row[14], raw_reference=None,
                    timeframe="1m",
                )
            )
        liquidation = tuple(liquidation)
        long_short = tuple(
            SimpleNamespace(
                exchange=row[0], exchange_symbol=row[1], canonical_symbol=row[2], metric_type=row[3],
                population_semantics=row[4], period=row[5], exchange_timestamp=row[6],
                status=DataStatus(row[7]), reason_code=_reason_code(row[8]), reason=row[8], source_endpoint=row[9],
                fetched_at=row[10], received_at=row[11], processed_at=row[12],
            ) for row in loaded[1]
        )
        basis = tuple(
            SimpleNamespace(
                exchange=row[0], exchange_symbol=row[1], canonical_symbol=row[2], basis_type=row[3],
                exchange_timestamp=row[4], status=DataStatus(row[5]), reason_code=_reason_code(row[6]), reason=row[6],
                source_endpoint=row[7], fetched_at=row[8], received_at=row[9], processed_at=row[10],
            ) for row in loaded[2]
        )
        return liquidation, long_short, basis

    def cleanup(self, retention: Phase4Retention) -> None:
        self.connection.execute(
            "DELETE FROM liquidation_events WHERE processed_at < now() - (%s * interval '1 hour')",
            (retention.liquidation_event_hours,),
        )
        for timeframe, days in sorted(retention.liquidation_window_days.items()):
            self.connection.execute(
                "DELETE FROM liquidation_windows WHERE timeframe = %s AND window_open < now() - (%s * interval '1 day')",
                (timeframe, days),
            )
        for table, days in (
            ("long_short_observations", retention.long_short_days),
            ("basis_snapshots", retention.basis_days),
            ("cross_exchange_phase4_snapshots", retention.cross_exchange_days),
            ("stage1_phase4_enrichment", retention.enrichment_days),
        ):
            self.connection.execute(
                f"DELETE FROM {table} WHERE processed_at < now() - (%s * interval '1 day')",
                (days,),
            )

    def insert_cross_exchange(self, rows: Iterable[Any]) -> int:
        payload_rows = []
        for context in rows:
            for metric in ("liquidation", "long_short", "basis"):
                metric_context = getattr(context, metric, None)
                if metric_context is None:
                    continue
                observations = tuple(getattr(metric_context, "observations", ()))
                canonical = getattr(context, "canonical_symbol", None) or (
                    getattr(observations[0], "canonical_symbol", None) if observations else None
                )
                if not canonical:
                    continue
                timestamp = _utc(getattr(context, "processed_at"), "snapshot_timestamp")
                sources = tuple(dict.fromkeys(str(getattr(row, "exchange", "")) for row in observations if getattr(row, "exchange", None)))
                timeframe = (
                    getattr(observations[0], "period", None) or ""
                    if metric == "long_short" and observations
                    else ""
                )
                payload_rows.append(CrossExchangeRow(
                    canonical, metric, timeframe, timestamp,
                    int(getattr(metric_context, "source_count", len(sources))),
                    int(getattr(metric_context, "comparable_count", 0)),
                    [
                        _bounded_text(getattr(row, "exchange"))
                        for row in observations
                        if getattr(row, "status", None) is DataStatus.NOT_AVAILABLE
                    ],
                    [
                        _bounded_text(getattr(row, "exchange"))
                        for row in observations
                        if getattr(row, "status", None) is DataStatus.STALE
                    ],
                    ",".join(_bounded_text(value) for value in getattr(metric_context, "coverage_semantics", ())[:16]) or None,
                    ",".join(_bounded_text(value) for value in getattr(metric_context, "source_granularity", ())[:16]) or None,
                    Jsonb(_metric_context_payload(metric_context)), _value(metric_context.status),
                    _optional_text(getattr(metric_context, "reason", None)), timestamp,
                ))
        if not payload_rows:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO cross_exchange_phase4_snapshots (
                canonical_symbol, metric, timeframe, snapshot_timestamp, exchange_count,
                comparable_exchange_count, missing_sources, stale_sources, coverage_semantics,
                source_granularity, context, status, reason, processed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (canonical_symbol, metric, timeframe, snapshot_timestamp) DO UPDATE SET
                exchange_count=EXCLUDED.exchange_count,
                comparable_exchange_count=EXCLUDED.comparable_exchange_count,
                missing_sources=EXCLUDED.missing_sources, stale_sources=EXCLUDED.stale_sources,
                coverage_semantics=EXCLUDED.coverage_semantics, source_granularity=EXCLUDED.source_granularity,
                context=EXCLUDED.context, status=EXCLUDED.status, reason=EXCLUDED.reason,
                processed_at=EXCLUDED.processed_at
            """,
            payload_rows,
        )
        return len(payload_rows)

    def insert_stage1_enrichment(self, run_id: int, rows: Iterable[Any]) -> int:
        payload_rows = []
        for enrichment in rows:
            context = enrichment.phase4_context
            phase1 = enrichment.phase1_result
            serialized = {
                "state": _value(enrichment.state), "reason": _bounded_text(enrichment.reason),
                "phase1": {
                    "symbol": _bounded_text(phase1.symbol), "category": _bounded_text(phase1.category),
                    "classification": _bounded_text(phase1.classification), "status": _value(phase1.status),
                    "reason": _bounded_text(phase1.reason), "reason_codes": [_bounded_text(v) for v in phase1.reason_codes[:32]],
                },
                "metrics": {name: _metric_context_payload(getattr(context, name)) for name in ("liquidation", "long_short", "basis")},
            }
            payload_rows.append((
                run_id, _bounded_text(enrichment.symbol), enrichment.canonical_symbol,
                _value(context.liquidation.status), _value(context.long_short.status), _value(context.basis.status),
                Jsonb(serialized), _bounded_text(enrichment.reason), _utc(enrichment.processed_at, "processed_at"),
            ))
        if not payload_rows:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO stage1_phase4_enrichment (
                screening_run_id, symbol, canonical_symbol, liquidation_status,
                long_short_status, basis_status, context, reason, processed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
            ON CONFLICT (screening_run_id, symbol) DO UPDATE SET
                canonical_symbol=EXCLUDED.canonical_symbol, liquidation_status=EXCLUDED.liquidation_status,
                long_short_status=EXCLUDED.long_short_status, basis_status=EXCLUDED.basis_status,
                context=EXCLUDED.context, reason=EXCLUDED.reason, processed_at=EXCLUDED.processed_at
            """,
            payload_rows,
        )
        return len(payload_rows)
