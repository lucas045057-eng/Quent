"""PostgreSQL persistence for bounded Phase 3 flow context."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Iterable, Mapping

from psycopg.types.json import Jsonb

from .contracts import FlowStatus
from .flow import TradeFlowWindow


@dataclass(frozen=True, slots=True)
class PersistedFlowWindowContext:
    timeframe: str
    delta_base: Decimal | None
    delta_ratio: Decimal | None
    freshness: FlowStatus
    status: FlowStatus
    status_reason: str | None


@dataclass(frozen=True, slots=True)
class PersistedCVDContext:
    timeframe: str
    value: Decimal | None
    status: FlowStatus


@dataclass(frozen=True, slots=True)
class PersistedCrossExchangeContext:
    directional_delta_ratio: Decimal | None
    directional_status: str
    volume_exchange_count: int
    directional_exchange_count: int


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _status(value: Any) -> str:
    return getattr(value, "value", value)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Phase 3 persistence timestamps must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


class Phase3Repository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def insert_flow_windows(self, windows: Iterable[TradeFlowWindow]) -> int:
        rows = [
            (
                row.exchange,
                row.canonical_symbol,
                row.timeframe,
                _utc(row.window_open),
                _utc(row.window_close),
                row.total_trade_count,
                row.buy_trade_count,
                row.sell_trade_count,
                row.unknown_trade_count,
                row.total_volume_base,
                row.buy_volume_base,
                row.sell_volume_base,
                row.unknown_volume_base,
                row.total_notional_usd,
                row.average_trade_size,
                row.trade_frequency,
                row.delta_base,
                row.delta_ratio,
                _utc(row.first_trade_at),
                _utc(row.last_trade_at),
                _status(row.freshness),
                _status(row.status),
                row.status_reason,
                _utc(row.processed_at),
            )
            for row in windows
        ]
        if not rows:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO trade_flow_windows (
                exchange, canonical_symbol, timeframe, window_open, window_close,
                total_trade_count, buy_trade_count, sell_trade_count, unknown_trade_count,
                total_volume_base, buy_volume_base, sell_volume_base, unknown_volume_base,
                total_notional_usd, average_trade_size, trade_frequency, delta_base, delta_ratio,
                first_trade_at, last_trade_at, freshness, status, status_reason, processed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (exchange, canonical_symbol, timeframe, window_open) DO UPDATE SET
                window_close=EXCLUDED.window_close,
                total_trade_count=EXCLUDED.total_trade_count,
                buy_trade_count=EXCLUDED.buy_trade_count,
                sell_trade_count=EXCLUDED.sell_trade_count,
                unknown_trade_count=EXCLUDED.unknown_trade_count,
                total_volume_base=EXCLUDED.total_volume_base,
                buy_volume_base=EXCLUDED.buy_volume_base,
                sell_volume_base=EXCLUDED.sell_volume_base,
                unknown_volume_base=EXCLUDED.unknown_volume_base,
                total_notional_usd=EXCLUDED.total_notional_usd,
                average_trade_size=EXCLUDED.average_trade_size,
                trade_frequency=EXCLUDED.trade_frequency,
                delta_base=EXCLUDED.delta_base,
                delta_ratio=EXCLUDED.delta_ratio,
                first_trade_at=EXCLUDED.first_trade_at,
                last_trade_at=EXCLUDED.last_trade_at,
                freshness=EXCLUDED.freshness,
                status=EXCLUDED.status,
                status_reason=EXCLUDED.status_reason,
                processed_at=EXCLUDED.processed_at
            """,
            rows,
        )
        return len(rows)

    def insert_cvd_snapshots(self, points: Iterable[Any]) -> int:
        rows = [
            (
                point.exchange,
                point.canonical_symbol,
                point.timeframe,
                _utc(point.window_end),
                point.value,
                _status(point.status),
                point.reason,
                _utc(point.processed_at),
            )
            for point in points
        ]
        if not rows:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO cvd_snapshots (
                exchange, canonical_symbol, timeframe, window_end, value, status, reason, processed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (exchange, canonical_symbol, timeframe, window_end) DO UPDATE SET
                value=EXCLUDED.value, status=EXCLUDED.status, reason=EXCLUDED.reason,
                processed_at=EXCLUDED.processed_at
            """,
            rows,
        )
        return len(rows)

    def insert_cross_exchange_snapshots(self, snapshots: Iterable[Any]) -> int:
        rows = [
            (
                snapshot.canonical_symbol,
                snapshot.timeframe,
                _utc(snapshot.snapshot_timestamp),
                snapshot.volume_exchange_count,
                snapshot.directional_exchange_count,
                snapshot.total_volume_base,
                snapshot.total_notional_usd,
                snapshot.directional_delta_base,
                snapshot.directional_delta_ratio,
                _status(snapshot.status),
                snapshot.reason,
                Jsonb(_jsonable(snapshot.snapshot)),
                _utc(snapshot.processed_at),
            )
            for snapshot in snapshots
        ]
        if not rows:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO cross_exchange_flow_snapshots (
                canonical_symbol, timeframe, snapshot_timestamp, volume_exchange_count,
                directional_exchange_count, total_volume_base, total_notional_usd,
                directional_delta_base, directional_delta_ratio, status, reason, snapshot, processed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (canonical_symbol, timeframe, snapshot_timestamp) DO UPDATE SET
                volume_exchange_count=EXCLUDED.volume_exchange_count,
                directional_exchange_count=EXCLUDED.directional_exchange_count,
                total_volume_base=EXCLUDED.total_volume_base,
                total_notional_usd=EXCLUDED.total_notional_usd,
                directional_delta_base=EXCLUDED.directional_delta_base,
                directional_delta_ratio=EXCLUDED.directional_delta_ratio,
                status=EXCLUDED.status, reason=EXCLUDED.reason,
                snapshot=EXCLUDED.snapshot, processed_at=EXCLUDED.processed_at
            """,
            rows,
        )
        return len(rows)

    def insert_gap_events(self, events: Iterable[Any]) -> int:
        rows = [
            (
                event.exchange,
                event.canonical_symbol,
                _utc(event.gap_start),
                _utc(event.gap_end),
                _utc(event.detected_at),
                _utc(event.resolved_at) if event.resolved_at else None,
                _status(event.status),
                event.reason,
                event.affected_timeframe,
                _utc(event.affected_window_open) if event.affected_window_open else None,
                event.missing_count,
                event.dropped_count,
                Jsonb(_jsonable(event.details)),
            )
            for event in events
        ]
        if not rows:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO trade_gap_events (
                exchange, canonical_symbol, gap_start, gap_end, detected_at, resolved_at,
                status, reason, affected_timeframe, affected_window_open,
                missing_count, dropped_count, details
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (exchange, canonical_symbol, gap_start, gap_end, reason) DO UPDATE SET
                resolved_at=EXCLUDED.resolved_at, status=EXCLUDED.status,
                affected_timeframe=EXCLUDED.affected_timeframe,
                affected_window_open=EXCLUDED.affected_window_open,
                missing_count=EXCLUDED.missing_count, dropped_count=EXCLUDED.dropped_count,
                details=EXCLUDED.details
            """,
            rows,
        )
        return len(rows)

    def insert_stage1_flow_enrichment(self, run_id: int, rows: Iterable[Any]) -> int:
        values = [
            (
                run_id,
                row.symbol,
                row.canonical_symbol,
                _status(row.flow_status),
                Jsonb(_jsonable(row.flow_context)),
                _utc(row.processed_at),
            )
            for row in rows
        ]
        if not values:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO stage1_flow_enrichment (
                screening_run_id, symbol, canonical_symbol, flow_status, flow_context, processed_at
            ) VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (screening_run_id, symbol) DO UPDATE SET
                canonical_symbol=EXCLUDED.canonical_symbol,
                flow_status=EXCLUDED.flow_status,
                flow_context=EXCLUDED.flow_context,
                processed_at=EXCLUDED.processed_at
            """,
            values,
        )
        return len(values)

    def _flow_window_from_row(self, row: tuple[Any, ...]) -> TradeFlowWindow:
        return TradeFlowWindow(
            exchange=row[0],
            canonical_symbol=row[1],
            timeframe=row[2],
            window_open=row[3],
            window_close=row[4],
            total_trade_count=int(row[5]),
            buy_trade_count=int(row[6]),
            sell_trade_count=int(row[7]),
            unknown_trade_count=int(row[8]),
            total_volume_base=Decimal(str(row[9])),
            buy_volume_base=Decimal(str(row[10])),
            sell_volume_base=Decimal(str(row[11])),
            unknown_volume_base=Decimal(str(row[12])),
            total_notional_usd=Decimal(str(row[13])) if row[13] is not None else None,
            average_trade_size=Decimal(str(row[14])),
            trade_frequency=Decimal(str(row[15])),
            delta_base=Decimal(str(row[16])) if row[16] is not None else None,
            delta_ratio=Decimal(str(row[17])) if row[17] is not None else None,
            first_trade_at=row[18],
            last_trade_at=row[19],
            freshness=FlowStatus(row[20]),
            status=FlowStatus(row[21]),
            status_reason=row[22],
            processed_at=row[23],
        )

    def load_recent_available_minute_windows(
        self,
        *,
        routes: Iterable[tuple[str, str]],
        per_route_limit: int = 300,
    ) -> list[TradeFlowWindow]:
        """Load closed AVAILABLE 1m history with an independent bound per active route."""
        if per_route_limit <= 0:
            raise ValueError("per-route flow window limit must be positive")
        active_routes = tuple(dict.fromkeys((exchange.strip(), symbol.strip()) for exchange, symbol in routes))
        if any(not exchange or not symbol for exchange, symbol in active_routes):
            raise ValueError("active flow routes require exchange and canonical symbol")
        if not active_routes:
            return []
        rows = self.connection.execute(
            """
            SELECT flow.exchange, flow.canonical_symbol, flow.timeframe, flow.window_open, flow.window_close,
                   flow.total_trade_count, flow.buy_trade_count, flow.sell_trade_count, flow.unknown_trade_count,
                   flow.total_volume_base, flow.buy_volume_base, flow.sell_volume_base, flow.unknown_volume_base,
                   flow.total_notional_usd, flow.average_trade_size, flow.trade_frequency,
                   flow.delta_base, flow.delta_ratio, flow.first_trade_at, flow.last_trade_at,
                   flow.freshness, flow.status, flow.status_reason, flow.processed_at
            FROM unnest(%s::text[], %s::text[]) AS active(exchange, canonical_symbol)
            CROSS JOIN LATERAL (
                SELECT windows.*
                FROM trade_flow_windows AS windows
                WHERE windows.exchange = active.exchange
                  AND windows.canonical_symbol = active.canonical_symbol
                  AND windows.timeframe = '1m'
                  AND windows.status = 'AVAILABLE'
                  AND windows.window_close <= now()
                ORDER BY windows.window_open DESC
                LIMIT %s
            ) AS flow
            ORDER BY flow.exchange, flow.canonical_symbol, flow.window_open ASC
            """,
            (
                [exchange for exchange, _ in active_routes],
                [symbol for _, symbol in active_routes],
                per_route_limit,
            ),
        ).fetchall()
        return [self._flow_window_from_row(row) for row in rows]

    def load_recent_flow_windows(self, *, limit: int = 20_000) -> list[TradeFlowWindow]:
        if limit <= 0:
            raise ValueError("flow window limit must be positive")
        rows = self.connection.execute(
            """
            SELECT exchange, canonical_symbol, timeframe, window_open, window_close,
                   total_trade_count, buy_trade_count, sell_trade_count, unknown_trade_count,
                   total_volume_base, buy_volume_base, sell_volume_base, unknown_volume_base,
                   total_notional_usd, average_trade_size, trade_frequency, delta_base, delta_ratio,
                   first_trade_at, last_trade_at, freshness, status, status_reason, processed_at
            FROM trade_flow_windows
            ORDER BY window_open DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [self._flow_window_from_row(row) for row in rows]

    def load_latest_flow_context(
        self, canonical_symbol: str
    ) -> tuple[list[PersistedFlowWindowContext], list[PersistedCVDContext], PersistedCrossExchangeContext | None]:
        """Load only the latest bounded flow context needed by Stage1 enrichment."""
        window_rows = self.connection.execute(
            """
            SELECT DISTINCT ON (exchange, timeframe)
                   timeframe, delta_base, delta_ratio, freshness, status, status_reason
            FROM trade_flow_windows
            WHERE canonical_symbol = %s
            ORDER BY exchange, timeframe, window_open DESC
            """,
            (canonical_symbol,),
        ).fetchall()
        windows = [
            PersistedFlowWindowContext(
                timeframe=row[0],
                delta_base=Decimal(str(row[1])) if row[1] is not None else None,
                delta_ratio=Decimal(str(row[2])) if row[2] is not None else None,
                freshness=FlowStatus(row[3]),
                status=FlowStatus(row[4]),
                status_reason=row[5],
            )
            for row in window_rows
        ]
        cvd_rows = self.connection.execute(
            """
            SELECT DISTINCT ON (timeframe) timeframe, value, status
            FROM cvd_snapshots
            WHERE canonical_symbol = %s
            ORDER BY timeframe, window_end DESC
            """,
            (canonical_symbol,),
        ).fetchall()
        cvd = [
            PersistedCVDContext(
                timeframe=row[0],
                value=Decimal(str(row[1])) if row[1] is not None else None,
                status=FlowStatus(row[2]),
            )
            for row in cvd_rows
        ]
        cross_row = self.connection.execute(
            """
            SELECT directional_delta_ratio, volume_exchange_count,
                   directional_exchange_count, status, snapshot
            FROM cross_exchange_flow_snapshots
            WHERE canonical_symbol = %s
            ORDER BY snapshot_timestamp DESC
            LIMIT 1
            """,
            (canonical_symbol,),
        ).fetchone()
        cross = None
        if cross_row is not None:
            snapshot = cross_row[4] if isinstance(cross_row[4], Mapping) else {}
            cross = PersistedCrossExchangeContext(
                directional_delta_ratio=(
                    Decimal(str(cross_row[0])) if cross_row[0] is not None else None
                ),
                directional_status=str(snapshot.get("directional_status", cross_row[3])),
                volume_exchange_count=int(cross_row[1]),
                directional_exchange_count=int(cross_row[2]),
            )
        return windows, cvd, cross

    def cleanup_flow(
        self,
        *,
        retention_days: Mapping[str, int],
        cvd_retention_days: int,
        gap_retention_days: int,
        cross_exchange_retention_days: int = 180,
        enrichment_retention_days: int = 90,
    ) -> None:
        if (
            any(value <= 0 for value in retention_days.values())
            or cvd_retention_days <= 0
            or gap_retention_days <= 0
            or cross_exchange_retention_days <= 0
            or enrichment_retention_days <= 0
        ):
            raise ValueError("Phase 3 retention values must be positive")
        for timeframe, days in retention_days.items():
            if timeframe not in {"1m", "5m", "15m", "1H", "4H"}:
                raise ValueError(f"unsupported flow retention timeframe: {timeframe}")
            self.connection.execute(
                "DELETE FROM trade_flow_windows WHERE timeframe = %s AND window_open < now() - (%s * interval '1 day')",
                (timeframe, days),
            )
        self.connection.execute(
            "DELETE FROM cvd_snapshots WHERE processed_at < now() - (%s * interval '1 day')",
            (cvd_retention_days,),
        )
        self.connection.execute(
            "DELETE FROM trade_gap_events WHERE detected_at < now() - (%s * interval '1 day')",
            (gap_retention_days,),
        )
        self.connection.execute(
            "DELETE FROM cross_exchange_flow_snapshots WHERE snapshot_timestamp < now() - (%s * interval '1 day')",
            (cross_exchange_retention_days,),
        )
        self.connection.execute(
            "DELETE FROM stage1_flow_enrichment WHERE processed_at < now() - (%s * interval '1 day')",
            (enrichment_retention_days,),
        )
