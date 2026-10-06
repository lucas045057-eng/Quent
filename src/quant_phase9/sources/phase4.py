"""Bounded Phase 4 liquidation projections with explicit gap coverage."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from psycopg import Connection

from quant_instruments import resolve_core_instrument

from quant_phase9.contracts import (
    EvidenceFreshnessV1,
    EvidenceQualityV1,
    PolicyCoverageStatusV1,
    PolicyDataStatusV1,
    SourcePhaseV1,
    SourceProjectionV1,
    Stage1CandidateEventV1,
)
from quant_phase9.liquidation import project_liquidation

from . import MAX_SOURCE_ROWS, as_utc, context_evaluation_id, decimal_value, make_projection, read_rows, status_pair


def select_phase4(
    conn: Connection,
    candidate: Stage1CandidateEventV1,
    timeframe: Literal["15m", "1H", "4H"],
    as_of: datetime,
) -> tuple[SourceProjectionV1, ...]:
    """Project liquidation windows/events and the authoritative health watermark."""
    as_of = as_utc(as_of)
    evaluation_id = context_evaluation_id(candidate, timeframe, as_of)
    symbol = candidate.symbol
    canonical_symbol = resolve_core_instrument(conn, symbol).canonical_symbol
    market = candidate.market
    result = []

    health_rows = read_rows(
        conn,
        """SELECT component, status, checked_at, details
             FROM system_health
            WHERE component = 'phase4-liquidation' AND checked_at <= %s
            LIMIT 1""",
        (as_of,),
    )
    health = health_rows[0] if health_rows else None
    if health is not None:
        details = health["details"] if isinstance(health["details"], dict) else {}
        timestamp_fields = (
            "gap_detected_at", "gap_watermark_received_at", "gap_watermark_event_timestamp",
        )
        for field in timestamp_fields:
            value = details.get(field)
            if isinstance(value, str):
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, datetime) and value > as_of:
                raise ValueError("Phase 4 health evidence is later than as_of")
        status, quality = status_pair(health["status"])
        payload = {
            "component": health["component"], "health_status": status.value,
            "checked_at": health["checked_at"],
            "gap_detected": details.get("gap_detected"),
            "gap_count": details.get("gap_count"), "gap_reason": details.get("gap_reason"),
            "gap_detected_at": details.get("gap_detected_at"),
            "gap_watermark_received_at": details.get("gap_watermark_received_at"),
            "gap_watermark_event_timestamp": details.get("gap_watermark_event_timestamp"),
            "phase4_status": details.get("phase4_status"),
            "data_quality": details.get("data_quality"),
            "runtime_state": details.get("runtime_state"),
        }
        gap_known = details.get("gap_detected") is True or (
            isinstance(details.get("gap_count"), int) and details.get("gap_count", 0) > 0
        )
        partial_health = (
            details.get("phase4_status") == "PARTIAL"
            or details.get("data_quality") == "PARTIAL"
        )
        coverage = (
            PolicyCoverageStatusV1.PARTIAL if gap_known or partial_health
            else PolicyCoverageStatusV1.UNKNOWN
        )
        result.append(make_projection(
            evaluation_id=evaluation_id, source_phase=SourcePhaseV1.PHASE4,
            source_type="LIQUIDATION_HEALTH", source_ref="phase4:system_health/phase4-liquidation",
            symbol=symbol, market=market, event_time=None, observed_at=health["checked_at"],
            captured_at=health["checked_at"], processed_at=health["checked_at"],
            available_at=health["checked_at"], availability_status=status,
            freshness=EvidenceFreshnessV1.UNKNOWN,
            quality=(EvidenceQualityV1.PARTIAL if gap_known or partial_health else quality),
            coverage=coverage, payload=payload,
        ))

    window_rows = read_rows(
        conn,
        """SELECT id, exchange, canonical_symbol, timeframe, window_open, window_close,
                  event_count, liquidated_long_count, liquidated_short_count,
                  convertible_notional_usd, largest_source_notional_usd, source_exchange_count,
                  source_granularity, coverage_semantics, status, reason, processed_at
             FROM liquidation_windows
            WHERE canonical_symbol = %s AND timeframe = %s
              AND window_open < %s AND window_close <= %s AND processed_at <= %s
            ORDER BY window_close DESC, id DESC
            LIMIT %s""",
        (canonical_symbol, timeframe, as_of, as_of, as_of, MAX_SOURCE_ROWS),
    )
    for row in reversed(window_rows):
        projection_row = dict(row)
        projection_row["evaluation_id"] = evaluation_id
        projection_row["market"] = market
        projection_row["core_symbol"] = symbol
        projection = project_liquidation(projection_row, health, as_of=as_of)
        result.append(projection)

    event_rows = read_rows(
        conn,
        """SELECT id, exchange, exchange_symbol, canonical_symbol, source_endpoint,
                  source_channel, source_event_id, event_timestamp, received_at, processed_at,
                  side, raw_side, raw_side_semantics, price, raw_quantity, quantity_unit,
                  quantity_base, notional_usd, source_granularity, coverage_semantics,
                  status, reason
             FROM liquidation_events
            WHERE canonical_symbol = %s AND event_timestamp <= %s
              AND received_at <= %s AND processed_at <= %s
            ORDER BY event_timestamp DESC, id DESC
            LIMIT %s""",
        (canonical_symbol, as_of, as_of, as_of, MAX_SOURCE_ROWS),
    )
    for row in reversed(event_rows):
        status, quality = status_pair(row["status"])
        payload = {
            "row_id": row["id"], "exchange": row["exchange"],
            "exchange_symbol": row["exchange_symbol"], "canonical_symbol": row["canonical_symbol"],
            "source_channel": row["source_channel"], "source_event_id": row["source_event_id"],
            "event_timestamp": row["event_timestamp"], "side": row["side"],
            "raw_side": row["raw_side"], "raw_side_semantics": row["raw_side_semantics"],
            "price": decimal_value(row["price"], optional=True),
            "raw_quantity": decimal_value(row["raw_quantity"], optional=True),
            "quantity_unit": row["quantity_unit"],
            "quantity_base": decimal_value(row["quantity_base"], optional=True),
            "notional_usd": decimal_value(row["notional_usd"], optional=True),
            "source_granularity": row["source_granularity"],
            "coverage_semantics": row["coverage_semantics"], "status": status.value,
            "reason": row["reason"],
        }
        result.append(make_projection(
            evaluation_id=evaluation_id, source_phase=SourcePhaseV1.PHASE4,
            source_type="LIQUIDATION_EVENT", source_ref=f"phase4:liquidation_events/{row['id']}",
            symbol=symbol, market=market, event_time=row["event_timestamp"],
            observed_at=row["event_timestamp"], captured_at=row["received_at"],
            processed_at=row["processed_at"], available_at=row["processed_at"],
            availability_status=status, freshness=EvidenceFreshnessV1.UNKNOWN,
            quality=quality, coverage=PolicyCoverageStatusV1.UNKNOWN, payload=payload,
        ))

    return tuple(result)
