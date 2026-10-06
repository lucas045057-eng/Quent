"""Bounded, as-of Phase 5 context projections."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
from typing import Literal

from psycopg import Connection

from quant_phase9.contracts import (
    EvidenceFreshnessV1,
    EvidenceQualityV1,
    PolicyCoverageStatusV1,
    PolicyDataStatusV1,
    SourcePhaseV1,
    SourceProjectionV1,
    Stage1CandidateEventV1,
)

from . import MAX_SOURCE_ROWS, as_utc, context_evaluation_id, decimal_value, freshness_status, make_projection, read_rows, status_pair


def _bounded(rows: list[dict[str, object]], category: str) -> list[dict[str, object]]:
    if len(rows) > MAX_SOURCE_ROWS:
        raise ValueError(f"Phase 5 {category} projection exceeds the bounded row limit")
    return rows


def _json(value: object) -> object:
    if isinstance(value, str):
        try:
            value = json.loads(value, parse_float=Decimal)
        except (TypeError, ValueError):
            return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {str(key): _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


def _coverage(status: PolicyDataStatusV1, ratio: object) -> PolicyCoverageStatusV1:
    if status is PolicyDataStatusV1.PARTIAL:
        return PolicyCoverageStatusV1.PARTIAL
    if ratio is None:
        return PolicyCoverageStatusV1.UNKNOWN
    value = decimal_value(ratio)
    if value < 1:
        return PolicyCoverageStatusV1.PARTIAL
    return PolicyCoverageStatusV1.SOURCE_DECLARED_COMPLETE


def _context_projection(
    *, evaluation_id, candidate, source_type: str, table: str, row: dict[str, object],
    as_of: datetime, payload: dict[str, object], status_field: str = "status",
    coverage_ratio: object = None, freshness_field: str | None = None,
) -> SourceProjectionV1:
    status, quality = status_pair(row[status_field])
    context_time = row["context_timestamp"]
    processed_at = row["processed_at"]
    assert isinstance(context_time, datetime) and isinstance(processed_at, datetime)
    if context_time > as_of:
        raise ValueError("Phase 5 context time is later than as_of")
    for key in ("input_window_start", "input_window_end"):
        value = row.get(key)
        if isinstance(value, datetime) and value > as_of:
            raise ValueError("Phase 5 input window is later than as_of")
    return make_projection(
        evaluation_id=evaluation_id, source_phase=SourcePhaseV1.PHASE5,
        source_type=source_type, source_ref=f"phase5:{table}/{row['id']}",
        symbol=candidate.symbol, market=candidate.market,
        event_time=context_time, observed_at=context_time, captured_at=None,
        processed_at=processed_at, available_at=processed_at,
        availability_status=status,
        freshness=(freshness_status(row[freshness_field]) if freshness_field else
                   (EvidenceFreshnessV1.STALE if status is PolicyDataStatusV1.STALE
                    else EvidenceFreshnessV1.UNKNOWN)),
        quality=(EvidenceQualityV1.PARTIAL if status is PolicyDataStatusV1.PARTIAL else quality),
        coverage=_coverage(status, coverage_ratio), payload=payload,
    )


def select_phase5(
    conn: Connection,
    candidate: Stage1CandidateEventV1,
    timeframe: Literal["15m", "1H", "4H"],
    as_of: datetime,
) -> tuple[SourceProjectionV1, ...]:
    """Project candidate context and market-wide regime without timestamp substitution."""
    as_of = as_utc(as_of)
    evaluation_id = context_evaluation_id(candidate, timeframe, as_of)
    result: list[SourceProjectionV1] = []

    leader_rows = _bounded(read_rows(conn, """SELECT id,symbol,timeframe,context_timestamp,
              input_window_start,input_window_end,return_pct,trend_state,structure_state,
              volatility_state,volume_state,volatility_value,volume_ratio,freshness_status,
              data_quality,source_count,missing_count,support_evidence,conflict_evidence,
              missing_evidence,status,reason_code,calculation_version,input_reference,processed_at
         FROM phase5_market_leader_context
        WHERE symbol=%s AND timeframe=%s AND context_timestamp<=%s AND processed_at<=%s
          AND (input_window_end IS NULL OR input_window_end<=%s)
        ORDER BY context_timestamp DESC,id DESC LIMIT %s""",
        (candidate.symbol, timeframe, as_of, as_of, as_of, MAX_SOURCE_ROWS + 1)), "market leader")
    for row in reversed(leader_rows):
        payload = {key: _json(value) for key, value in row.items() if key != "id"}
        payload["return_pct"] = decimal_value(row["return_pct"], optional=True)
        payload["volatility_value"] = decimal_value(row["volatility_value"], optional=True)
        payload["volume_ratio"] = decimal_value(row["volume_ratio"], optional=True)
        result.append(_context_projection(evaluation_id=evaluation_id,candidate=candidate,
            source_type="MARKET_LEADER_CONTEXT",table="phase5_market_leader_context",row=row,
            as_of=as_of,payload=payload,freshness_field="freshness_status"))

    regime_rows = _bounded(read_rows(conn, """SELECT id,timeframe,context_timestamp,direction_regime,
              volatility_regime,breadth_regime,direction_status,volatility_status,breadth_status,
              universe_run_id,sample_size,available_count,missing_count,coverage_ratio,
              support_evidence,conflict_evidence,missing_evidence,status,reason_code,
              calculation_version,input_reference,processed_at
         FROM phase5_market_regime_snapshots
        WHERE timeframe=%s AND context_timestamp<=%s AND processed_at<=%s
        ORDER BY context_timestamp DESC,id DESC LIMIT %s""",
        (timeframe, as_of, as_of, MAX_SOURCE_ROWS + 1)), "market regime")
    for row in reversed(regime_rows):
        payload = {key: _json(value) for key, value in row.items() if key != "id"}
        payload["coverage_ratio"] = decimal_value(row["coverage_ratio"], optional=True)
        result.append(_context_projection(evaluation_id=evaluation_id,candidate=candidate,
            source_type="MARKET_REGIME_CONTEXT",table="phase5_market_regime_snapshots",row=row,
            as_of=as_of,payload=payload,coverage_ratio=row["coverage_ratio"]))

    relative_rows = _bounded(read_rows(conn, """SELECT id,symbol,benchmark,timeframe,context_timestamp,
              candidate_return_pct,benchmark_return_pct,relative_return_pct,relative_class,
              universe_run_id,sample_size,available_count,missing_count,coverage_ratio,status,
              reason_code,calculation_version,input_reference,missing_evidence,processed_at
         FROM phase5_relative_strength_snapshots
        WHERE symbol=%s AND timeframe=%s AND context_timestamp<=%s AND processed_at<=%s
        ORDER BY context_timestamp DESC,id DESC LIMIT %s""",
        (candidate.symbol, timeframe, as_of, as_of, MAX_SOURCE_ROWS + 1)), "relative strength")
    for row in reversed(relative_rows):
        payload = {key: _json(value) for key, value in row.items() if key != "id"}
        for field in ("candidate_return_pct", "benchmark_return_pct", "relative_return_pct", "coverage_ratio"):
            payload[field] = decimal_value(row[field], optional=True)
        result.append(_context_projection(evaluation_id=evaluation_id,candidate=candidate,
            source_type="RELATIVE_STRENGTH_CONTEXT",table="phase5_relative_strength_snapshots",row=row,
            as_of=as_of,payload=payload,coverage_ratio=row["coverage_ratio"]))

    memberships = _bounded(read_rows(conn, """SELECT id,mapping_version,symbol,sector,source_reference,
              effective_from,effective_to,status,processed_at
         FROM phase5_sector_membership
        WHERE symbol=%s AND effective_from<=%s AND (effective_to IS NULL OR effective_to>%s)
          AND processed_at<=%s
        ORDER BY effective_from DESC,id DESC LIMIT %s""",
        (candidate.symbol, as_of, as_of, as_of, MAX_SOURCE_ROWS + 1)), "sector membership")
    for membership in reversed(memberships):
        status, quality = status_pair(membership["status"])
        result.append(make_projection(
            evaluation_id=evaluation_id,source_phase=SourcePhaseV1.PHASE5,
            source_type="SECTOR_MEMBERSHIP",source_ref=f"phase5:phase5_sector_membership/{membership['id']}",
            symbol=candidate.symbol,market=candidate.market,event_time=membership["effective_from"],
            observed_at=membership["effective_from"],captured_at=None,
            processed_at=membership["processed_at"],available_at=membership["processed_at"],
            availability_status=status,freshness=EvidenceFreshnessV1.UNKNOWN,quality=quality,
            coverage=PolicyCoverageStatusV1.UNKNOWN,
            payload={key:value for key,value in membership.items() if key!="id"},
        ))
        sector_rows = _bounded(read_rows(conn, """SELECT id,sector,timeframe,context_timestamp,
                  universe_run_id,mapping_version,sector_return_pct,sector_positive_ratio,
                  member_count,sample_size,available_count,missing_count,coverage_ratio,status,
                  reason_code,missing_evidence,input_reference,calculation_version,processed_at
             FROM phase5_sector_context_snapshots
            WHERE sector=%s AND mapping_version=%s AND timeframe=%s
              AND context_timestamp<=%s AND processed_at<=%s
            ORDER BY context_timestamp DESC,id DESC LIMIT %s""",
            (membership["sector"],membership["mapping_version"],timeframe,as_of,as_of,MAX_SOURCE_ROWS+1)),
            "sector context")
        for row in reversed(sector_rows):
            payload={key:_json(value) for key,value in row.items() if key!="id"}
            for field in ("sector_return_pct","sector_positive_ratio","coverage_ratio"):
                payload[field]=decimal_value(row[field],optional=True)
            result.append(_context_projection(evaluation_id=evaluation_id,candidate=candidate,
                source_type="SECTOR_CONTEXT",table="phase5_sector_context_snapshots",row=row,
                as_of=as_of,payload=payload,coverage_ratio=row["coverage_ratio"]))

    return tuple(result)
