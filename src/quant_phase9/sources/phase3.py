"""Bounded Phase 3 flow, CVD, aggregate, and gap projections."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from psycopg import Connection

from quant_instruments import resolve_core_instrument

from quant_phase1.freshness import INTERVAL_SECONDS
from quant_phase9.contracts import (
    EvidenceFreshnessV1,
    EvidenceQualityV1,
    PolicyCoverageStatusV1,
    PolicyDataStatusV1,
    SourcePhaseV1,
    SourceProjectionV1,
    Stage1CandidateEventV1,
)

from . import MAX_SOURCE_ROWS, as_utc, context_evaluation_id, decimal_value, make_projection, read_rows, status_pair


def _expected_window_close(as_of: datetime, interval: str) -> datetime:
    seconds = INTERVAL_SECONDS[interval]
    boundary = int(as_of.timestamp()) // seconds * seconds
    return datetime.fromtimestamp(boundary, tz=timezone.utc)


def _window_freshness(window_close: datetime, expected_close: datetime, status: PolicyDataStatusV1) -> EvidenceFreshnessV1:
    if status is PolicyDataStatusV1.STALE or window_close < expected_close:
        return EvidenceFreshnessV1.STALE
    if status is not PolicyDataStatusV1.AVAILABLE and status is not PolicyDataStatusV1.PARTIAL:
        return EvidenceFreshnessV1.UNKNOWN
    return EvidenceFreshnessV1.FRESH if window_close == expected_close else EvidenceFreshnessV1.STALE


def select_phase3(
    conn: Connection,
    candidate: Stage1CandidateEventV1,
    timeframe: Literal["15m", "1H", "4H"],
    as_of: datetime,
) -> tuple[SourceProjectionV1, ...]:
    """Select bounded flow projections without deriving an unconfirmed side."""
    as_of = as_utc(as_of)
    evaluation_id = context_evaluation_id(candidate, timeframe, as_of)
    symbol = candidate.symbol
    canonical_symbol = resolve_core_instrument(conn, symbol).canonical_symbol
    market = candidate.market
    expected_close = _expected_window_close(as_of, timeframe)
    result = []

    flow_rows = read_rows(
        conn,
        """SELECT id, exchange, canonical_symbol, timeframe, window_open, window_close,
                  total_trade_count, buy_trade_count, sell_trade_count, unknown_trade_count,
                  total_volume_base, buy_volume_base, sell_volume_base, unknown_volume_base,
                  total_notional_usd, average_trade_size, trade_frequency, delta_base,
                  delta_ratio, first_trade_at, last_trade_at, freshness, status,
                  status_reason, processed_at
             FROM trade_flow_windows
            WHERE canonical_symbol = %s AND timeframe = %s
              AND window_close <= %s AND first_trade_at <= %s AND last_trade_at <= %s
              AND processed_at <= %s
            ORDER BY window_close DESC, id DESC
            LIMIT %s""",
        (canonical_symbol, timeframe, as_of, as_of, as_of, as_of, MAX_SOURCE_ROWS),
    )
    for row in reversed(flow_rows):
        status, quality = status_pair(row["status"])
        row_freshness = _window_freshness(row["window_close"], expected_close, status)
        if row["freshness"] == "STALE":
            row_freshness = EvidenceFreshnessV1.STALE
        elif row["freshness"] == "PARTIAL" and row_freshness is EvidenceFreshnessV1.FRESH:
            quality = EvidenceQualityV1.PARTIAL
        coverage = (
            PolicyCoverageStatusV1.PARTIAL
            if status is PolicyDataStatusV1.PARTIAL or row["freshness"] == "PARTIAL"
            else PolicyCoverageStatusV1.UNKNOWN
        )
        numeric = {
            key: decimal_value(row[key], optional=True)
            for key in (
                "total_volume_base", "buy_volume_base", "sell_volume_base", "unknown_volume_base",
                "total_notional_usd", "average_trade_size", "trade_frequency", "delta_base", "delta_ratio",
            )
        }
        payload = {
            "row_id": row["id"], "exchange": row["exchange"], "canonical_symbol": row["canonical_symbol"],
            "timeframe": row["timeframe"], "window_open": row["window_open"],
            "window_close": row["window_close"], "total_trade_count": row["total_trade_count"],
            "buy_trade_count": row["buy_trade_count"], "sell_trade_count": row["sell_trade_count"],
            "unknown_trade_count": row["unknown_trade_count"], **numeric,
            "first_trade_at": row["first_trade_at"], "last_trade_at": row["last_trade_at"],
            "freshness": row["freshness"], "status": status.value,
            "status_reason": row["status_reason"],
        }
        result.append(make_projection(
            evaluation_id=evaluation_id, source_phase=SourcePhaseV1.PHASE3,
            source_type="TRADE_FLOW_WINDOW", source_ref=f"phase3:trade_flow_windows/{row['id']}",
            symbol=symbol, market=market, event_time=row["last_trade_at"],
            observed_at=row["window_close"], captured_at=None, processed_at=row["processed_at"],
            available_at=row["processed_at"], availability_status=status, freshness=row_freshness,
            quality=quality, coverage=coverage, payload=payload,
        ))

    cvd_rows = read_rows(
        conn,
        """SELECT id, exchange, canonical_symbol, timeframe, window_end, value, status,
                  reason, processed_at
             FROM cvd_snapshots
            WHERE canonical_symbol = %s AND timeframe = %s
              AND window_end <= %s AND processed_at <= %s
            ORDER BY window_end DESC, id DESC
            LIMIT %s""",
        (canonical_symbol, timeframe, as_of, as_of, MAX_SOURCE_ROWS),
    )
    for row in reversed(cvd_rows):
        status, quality = status_pair(row["status"])
        row_freshness = _window_freshness(row["window_end"], expected_close, status)
        payload = {
            "row_id": row["id"], "exchange": row["exchange"],
            "canonical_symbol": row["canonical_symbol"], "timeframe": row["timeframe"],
            "window_end": row["window_end"], "value": decimal_value(row["value"], optional=True),
            "reason": row["reason"], "status": status.value,
        }
        result.append(make_projection(
            evaluation_id=evaluation_id, source_phase=SourcePhaseV1.PHASE3,
            source_type="CVD_SNAPSHOT", source_ref=f"phase3:cvd_snapshots/{row['id']}",
            symbol=symbol, market=market, event_time=row["window_end"],
            observed_at=row["window_end"], captured_at=None, processed_at=row["processed_at"],
            available_at=row["processed_at"], availability_status=status,
            freshness=row_freshness, quality=quality,
            coverage=(PolicyCoverageStatusV1.PARTIAL if status is PolicyDataStatusV1.PARTIAL
                      else PolicyCoverageStatusV1.UNKNOWN),
            payload=payload,
        ))

    aggregate_rows = read_rows(
        conn,
        """SELECT id, canonical_symbol, timeframe, snapshot_timestamp, volume_exchange_count,
                  directional_exchange_count, total_volume_base, total_notional_usd,
                  directional_delta_base, directional_delta_ratio, status, reason, processed_at
             FROM cross_exchange_flow_snapshots
            WHERE canonical_symbol = %s AND timeframe = %s
              AND snapshot_timestamp <= %s AND processed_at <= %s
            ORDER BY snapshot_timestamp DESC, id DESC
            LIMIT %s""",
        (canonical_symbol, timeframe, as_of, as_of, MAX_SOURCE_ROWS),
    )
    for row in aggregate_rows:
        status, quality = status_pair(row["status"])
        payload = {
            "row_id": row["id"], "canonical_symbol": row["canonical_symbol"],
            "timeframe": row["timeframe"], "snapshot_timestamp": row["snapshot_timestamp"],
            "volume_exchange_count": row["volume_exchange_count"],
            "directional_exchange_count": row["directional_exchange_count"],
            "total_volume_base": decimal_value(row["total_volume_base"], optional=True),
            "total_notional_usd": decimal_value(row["total_notional_usd"], optional=True),
            "directional_delta_base": decimal_value(row["directional_delta_base"], optional=True),
            "directional_delta_ratio": decimal_value(row["directional_delta_ratio"], optional=True),
            "reason": row["reason"], "status": status.value,
        }
        result.append(make_projection(
            evaluation_id=evaluation_id, source_phase=SourcePhaseV1.PHASE3,
            source_type="CROSS_EXCHANGE_FLOW", source_ref=f"phase3:cross_exchange_flow_snapshots/{row['id']}",
            symbol=symbol, market=market, event_time=None,
            observed_at=row["snapshot_timestamp"], captured_at=None,
            processed_at=row["processed_at"], available_at=row["processed_at"],
            availability_status=status, freshness=EvidenceFreshnessV1.UNKNOWN,
            quality=quality, coverage=(PolicyCoverageStatusV1.PARTIAL
                                       if status is PolicyDataStatusV1.PARTIAL
                                       else PolicyCoverageStatusV1.UNKNOWN),
            payload=payload,
        ))

    gap_rows = read_rows(
        conn,
        """SELECT id, exchange, canonical_symbol, gap_start, gap_end, detected_at,
                  resolved_at, status, reason, affected_timeframe, affected_window_open,
                  missing_count, dropped_count
             FROM trade_gap_events
            WHERE canonical_symbol = %s AND detected_at <= %s
              AND (resolved_at IS NULL OR resolved_at <= %s)
            ORDER BY detected_at DESC, id DESC
            LIMIT %s""",
        (canonical_symbol, as_of, as_of, MAX_SOURCE_ROWS),
    )
    for row in reversed(gap_rows):
        status, quality = status_pair(row["status"])
        payload = {
            "row_id": row["id"], "exchange": row["exchange"],
            "canonical_symbol": row["canonical_symbol"], "gap_start": row["gap_start"],
            "gap_end": row["gap_end"], "detected_at": row["detected_at"],
            "resolved_at": row["resolved_at"], "reason": row["reason"],
            "affected_timeframe": row["affected_timeframe"],
            "affected_window_open": row["affected_window_open"],
            "missing_count": row["missing_count"], "dropped_count": row["dropped_count"],
            "status": status.value,
        }
        result.append(make_projection(
            evaluation_id=evaluation_id, source_phase=SourcePhaseV1.PHASE3,
            source_type="TRADE_GAP", source_ref=f"phase3:trade_gap_events/{row['id']}",
            symbol=symbol, market=market, event_time=row["gap_end"],
            observed_at=row["detected_at"], captured_at=row["detected_at"],
            processed_at=None, available_at=row["detected_at"],
            availability_status=status,
            freshness=(EvidenceFreshnessV1.FRESH if status is PolicyDataStatusV1.PARTIAL
                       else EvidenceFreshnessV1.UNKNOWN),
            quality=quality, coverage=(PolicyCoverageStatusV1.PARTIAL
                                      if status is PolicyDataStatusV1.PARTIAL
                                      else PolicyCoverageStatusV1.UNKNOWN),
            payload=payload,
        ))

    return tuple(result)
