"""Bounded BTC/ETH Phase 7 spot and on-chain context projections."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
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

from . import MAX_SOURCE_ROWS, as_utc, context_evaluation_id, decimal_value, make_projection, read_rows, status_pair


def _bounded(rows: list[dict[str, object]], category: str) -> list[dict[str, object]]:
    if len(rows) > MAX_SOURCE_ROWS:
        raise ValueError(f"Phase 7 {category} projection exceeds the bounded row limit")
    return rows


def _assert_row_as_of(row: dict[str, object], as_of: datetime) -> None:
    if any(isinstance(value, datetime) and value > as_of for value in row.values()):
        raise ValueError("Phase 7 source timestamp is later than as_of")


def _coverage(status: PolicyDataStatusV1, explicit: object, ratio: object) -> PolicyCoverageStatusV1:
    raw = getattr(explicit, "value", explicit)
    if raw == "PARTIAL" or status is PolicyDataStatusV1.PARTIAL:
        return PolicyCoverageStatusV1.PARTIAL
    if ratio is not None and decimal_value(ratio) < 1:
        return PolicyCoverageStatusV1.PARTIAL
    if raw == "NOT_AVAILABLE":
        return PolicyCoverageStatusV1.NOT_AVAILABLE
    if raw == "ERROR":
        return PolicyCoverageStatusV1.UNKNOWN
    if raw == "AVAILABLE":
        return PolicyCoverageStatusV1.SOURCE_DECLARED_COMPLETE
    return PolicyCoverageStatusV1.UNKNOWN


def _projection(
    *, evaluation_id, candidate, source_type: str, table: str, row: dict[str, object],
    event_time: datetime | None, observed_at: datetime, captured_at: datetime | None,
    status_field: str = "status", coverage_field: str | None = None,
    ratio_field: str | None = None, payload: dict[str, object],
) -> SourceProjectionV1:
    status, quality = status_pair(row[status_field])
    processed_at = row["processed_at"]
    if not isinstance(processed_at, datetime):
        raise ValueError("Phase 7 row lacks processed_at")
    explicit = row.get(coverage_field) if coverage_field else None
    ratio = row.get(ratio_field) if ratio_field else None
    coverage = _coverage(status, explicit, ratio)
    if status is PolicyDataStatusV1.PARTIAL:
        quality = EvidenceQualityV1.PARTIAL
    return make_projection(
        evaluation_id=evaluation_id,source_phase=SourcePhaseV1.PHASE7,
        source_type=source_type,source_ref=f"phase7:{table}/{row.get('window_id', row.get('context_id'))}",
        symbol=candidate.symbol,market=candidate.market,event_time=event_time,
        observed_at=observed_at,captured_at=captured_at,processed_at=processed_at,
        available_at=processed_at,availability_status=status,
        freshness=(EvidenceFreshnessV1.STALE if status is PolicyDataStatusV1.STALE
                   else EvidenceFreshnessV1.UNKNOWN),quality=quality,
        coverage=coverage,payload=payload,
    )


def select_phase7(
    conn: Connection,
    candidate: Stage1CandidateEventV1,
    timeframe: Literal["15m", "1H", "4H"],
    as_of: datetime,
) -> tuple[SourceProjectionV1, ...]:
    """Project only BTC/ETH-aligned data; do not relabel it for altcoins."""
    as_of = as_utc(as_of)
    evaluation_id = context_evaluation_id(candidate, timeframe, as_of)
    base = candidate.symbol.removesuffix("USDT")
    if base not in {"BTC", "ETH"} or candidate.symbol != f"{base}USDT":
        return ()
    chain = "BITCOIN" if base == "BTC" else "ETHEREUM"
    result: list[SourceProjectionV1] = []

    spots = _bounded(read_rows(conn, """SELECT window_id,exchange,symbol,market_kind,timeframe,
              window_open,window_close,aggregation_version,base_volume,quote_volume,buy_volume,
              sell_volume,unknown_volume,delta,cvd,trade_count,directional_trade_count,
              event_time_first,event_time_last,cursor_first,cursor_last,sample_count,source_count,
              available_count,missing_count,coverage_ratio,status,reason,source_reference,
              normalization_version,processed_at,created_at
         FROM phase7_spot_flow_windows
        WHERE symbol=%s AND timeframe=%s AND window_close<=%s AND processed_at<=%s
        ORDER BY window_close DESC,window_id DESC LIMIT %s""",
        (candidate.symbol,timeframe,as_of,as_of,MAX_SOURCE_ROWS+1)), "spot flow")
    for row in reversed(spots):
        _assert_row_as_of(row, as_of)
        payload = dict(row)
        for field in ("base_volume","quote_volume","buy_volume","sell_volume","unknown_volume","delta","cvd","coverage_ratio"):
            payload[field] = decimal_value(row[field],optional=True)
        result.append(_projection(evaluation_id=evaluation_id,candidate=candidate,
            source_type="SPOT_FLOW_WINDOW",table="phase7_spot_flow_windows",row=row,
            event_time=row["event_time_last"],observed_at=row["window_close"],
            captured_at=row["created_at"],coverage_field=None,ratio_field="coverage_ratio",payload=payload))

    onchain = _bounded(read_rows(conn, """SELECT f.window_id,f.chain,f.asset_id,f.timeframe,f.window_open,
              f.window_close,f.context_version,f.flow_domain,f.aggregation_scope,f.aggregation_eligible,
              f.bridge_leg_id,f.inbound_amount,f.outbound_amount,f.net_amount,f.inbound_amount_usd,
              f.outbound_amount_usd,f.net_amount_usd,f.exchange_inflow_amount,f.exchange_outflow_amount,
              f.unknown_transfer_count,f.sample_count,f.source_count,f.available_count,f.missing_count,
              f.known_address_count,f.labeled_address_count,f.labeled_count,f.source_quality,
              f.coverage_status,f.coverage_ratio,f.label_coverage_ratio,f.status,f.reason,
              f.source_version,f.normalization_version,f.source_reference,f.processed_at,f.created_at
         FROM phase7_onchain_flow_windows f
         JOIN phase7_asset_registry a ON a.asset_id=f.asset_id AND a.chain=f.chain
        WHERE f.chain=%s AND a.symbol=%s AND a.asset_kind='NATIVE'
          AND a.effective_from<=f.window_close AND (a.effective_to IS NULL OR a.effective_to>f.window_close)
          AND f.timeframe=%s AND f.window_close<=%s AND f.processed_at<=%s
        ORDER BY f.window_close DESC,f.window_id DESC LIMIT %s""",
        (chain,base,timeframe,as_of,as_of,MAX_SOURCE_ROWS+1)), "on-chain flow")
    for row in reversed(onchain):
        _assert_row_as_of(row, as_of)
        payload=dict(row)
        for field in ("inbound_amount","outbound_amount","net_amount","inbound_amount_usd","outbound_amount_usd","net_amount_usd","exchange_inflow_amount","exchange_outflow_amount","coverage_ratio","label_coverage_ratio"):
            payload[field]=decimal_value(row[field],optional=True)
        result.append(_projection(evaluation_id=evaluation_id,candidate=candidate,
            source_type="ONCHAIN_FLOW_WINDOW",table="phase7_onchain_flow_windows",row=row,
            event_time=row["window_close"],observed_at=row["window_close"],captured_at=row["created_at"],
            coverage_field="coverage_status",ratio_field="coverage_ratio",payload=payload))

    whales = _bounded(read_rows(conn, """SELECT f.window_id,f.chain,f.asset_id,f.timeframe,f.window_open,
              f.window_close,f.aggregation_version,f.flow_domain,f.aggregation_scope,f.aggregation_eligible,
              f.bridge_leg_id,f.threshold_version,f.threshold_tier,f.large_inflow_count,f.large_outflow_count,
              f.large_inflow_amount,f.large_outflow_amount,f.large_inflow_usd,f.large_outflow_usd,
              f.threshold_not_evaluable_count,f.unknown_transfer_count,f.sample_count,f.source_count,
              f.available_count,f.missing_count,f.known_address_count,f.labeled_address_count,
              f.labeled_count,f.source_quality,f.coverage_status,f.coverage_ratio,f.label_coverage_ratio,
              f.status,f.reason,f.source_reference,f.normalization_version,f.processed_at,f.created_at
         FROM phase7_whale_flow_windows f
         JOIN phase7_asset_registry a ON a.asset_id=f.asset_id AND a.chain=f.chain
        WHERE f.chain=%s AND a.symbol=%s AND a.asset_kind='NATIVE'
          AND a.effective_from<=f.window_close AND (a.effective_to IS NULL OR a.effective_to>f.window_close)
          AND f.timeframe=%s AND f.window_close<=%s AND f.processed_at<=%s
        ORDER BY f.window_close DESC,f.window_id DESC LIMIT %s""",
        (chain,base,timeframe,as_of,as_of,MAX_SOURCE_ROWS+1)), "whale flow")
    for row in reversed(whales):
        _assert_row_as_of(row, as_of)
        payload=dict(row)
        for field in ("large_inflow_amount","large_outflow_amount","large_inflow_usd","large_outflow_usd","coverage_ratio","label_coverage_ratio"):
            payload[field]=decimal_value(row[field],optional=True)
        result.append(_projection(evaluation_id=evaluation_id,candidate=candidate,
            source_type="WHALE_FLOW_WINDOW",table="phase7_whale_flow_windows",row=row,
            event_time=row["window_close"],observed_at=row["window_close"],captured_at=row["created_at"],
            coverage_field="coverage_status",ratio_field="coverage_ratio",payload=payload))

    if base == "ETH":
        stable = _bounded(read_rows(conn, """SELECT s.context_id,s.chain,s.asset_id,s.contract_address,s.category,
                  s.timeframe,s.window_open,s.window_close,s.aggregation_version,s.flow_domain,
                  s.aggregation_scope,s.bridge_leg_id,s.aggregation_eligible,s.transfer_count,
                  s.amount_normalized,s.amount_usd,s.mint_count,s.burn_count,s.exchange_deposit_count,
                  s.exchange_withdrawal_count,s.sample_count,s.source_count,s.available_count,s.missing_count,
                  s.coverage_ratio,s.finality_status,s.freshness_status,s.status,s.reason,s.source_reference,
                  s.normalization_version,s.processed_at,s.created_at
             FROM phase7_stablecoin_context s
            WHERE s.chain='ETHEREUM' AND s.timeframe=%s AND s.window_close<=%s AND s.processed_at<=%s
            ORDER BY s.window_close DESC,s.context_id DESC LIMIT %s""",
            (timeframe,as_of,as_of,MAX_SOURCE_ROWS+1)), "stablecoin context")
        for row in reversed(stable):
            _assert_row_as_of(row, as_of)
            payload=dict(row)
            for field in ("amount_normalized","amount_usd","coverage_ratio"):
                payload[field]=decimal_value(row[field],optional=True)
            result.append(_projection(evaluation_id=evaluation_id,candidate=candidate,
                source_type="STABLECOIN_CONTEXT",table="phase7_stablecoin_context",row=row,
                event_time=row["window_close"],observed_at=row["window_close"],captured_at=row["created_at"],
                ratio_field="coverage_ratio",payload=payload))

    return tuple(result)
