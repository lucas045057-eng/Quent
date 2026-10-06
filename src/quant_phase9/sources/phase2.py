"""Bounded Phase 2 derivatives projections with source-time preservation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from psycopg import Connection

from quant_instruments import resolve_core_instrument
from quant_data_layer.freshness import FRESHNESS_POLICY

from quant_phase9.contracts import (
    EvidenceFreshnessV1,
    EvidenceQualityV1,
    PolicyDataStatusV1,
    SourcePhaseV1,
    SourceProjectionV1,
    Stage1CandidateEventV1,
)

from . import MAX_SOURCE_ROWS, as_utc, context_evaluation_id, decimal_value, make_projection, read_rows, status_pair


_FRESHNESS_SECONDS = FRESHNESS_POLICY["OPEN_INTEREST"].hard_seconds

_VALID_OI_METHODS = {
    ("BASE_ASSET", "base_quantity_times_mark_price"),
    ("CONTRACTS", "contracts_times_multiplier_times_mark_price"),
    ("USD_NOTIONAL", "exchange_reported_quote_notional"),
    ("QUOTE_NOTIONAL", "exchange_reported_quote_notional"),
    ("QUOTE_NOTIONAL", "user_confirmed_quote_notional"),
}


def _valid_oi_normalization(row: dict) -> bool:
    """A documented conversion is lossless only when its inputs and outputs exist."""
    unit = row["raw_unit"]
    method = row["normalization_method"]
    raw = row["raw_open_interest"]
    mark = row["mark_price"]
    base = row["open_interest_base"]
    usd = row["open_interest_usd"]
    if (unit, method) not in _VALID_OI_METHODS:
        return False
    if method=='user_confirmed_quote_notional':
        from quant_phase2.unit_contracts import verified_user_unit
        if not verified_user_unit(row):return False
    if raw is None or raw < 0 or mark is None or mark <= 0 or base is None or usd is None:
        return False
    if unit == "BASE_ASSET":
        return base == raw and usd == raw * mark
    if unit in {"USD_NOTIONAL", "QUOTE_NOTIONAL"}:
        return usd == raw and base == raw / mark
    return base >= 0 and usd == base * mark



def _freshness(event_time: datetime | None, status: PolicyDataStatusV1, as_of: datetime) -> EvidenceFreshnessV1:
    if status is PolicyDataStatusV1.STALE:
        return EvidenceFreshnessV1.STALE
    if status is not PolicyDataStatusV1.AVAILABLE or event_time is None:
        return EvidenceFreshnessV1.UNKNOWN
    if event_time > as_of:
        raise ValueError("Phase 2 source event is later than as_of")
    return (
        EvidenceFreshnessV1.FRESH
        if (as_of - event_time).total_seconds() <= _FRESHNESS_SECONDS
        else EvidenceFreshnessV1.STALE
    )


def select_phase2(
    conn: Connection,
    candidate: Stage1CandidateEventV1,
    timeframe: Literal["15m", "1H", "4H"],
    as_of: datetime,
) -> tuple[SourceProjectionV1, ...]:
    """Project OI/funding observations and aggregate context, capped per table."""
    as_of = as_utc(as_of)
    evaluation_id = context_evaluation_id(candidate, timeframe, as_of)
    symbol = candidate.symbol
    canonical_symbol = resolve_core_instrument(conn, symbol).canonical_symbol
    market = candidate.market
    result = []

    oi_rows = read_rows(
        conn,
        """SELECT id, symbol, canonical_symbol, exchange, contract_type, margin_asset,
                  settle_asset, raw_open_interest, raw_unit, open_interest_base,
                  open_interest_quote, open_interest_usd, mark_price, normalization_method,
                  exchange_timestamp, fetched_at, processed_at, status, observation_key, source_endpoint, raw_payload
             FROM open_interest
            WHERE (canonical_symbol = %s OR (canonical_symbol IS NULL AND symbol = %s))
              AND fetched_at <= %s AND processed_at <= %s
              AND (exchange_timestamp IS NULL OR exchange_timestamp <= %s)
            ORDER BY exchange_timestamp DESC NULLS FIRST, id DESC
            LIMIT %s""",
        (canonical_symbol, symbol, as_of, as_of, as_of, MAX_SOURCE_ROWS),
    )
    for row in oi_rows:
        status, quality = status_pair(row["status"])
        event_time = row["exchange_timestamp"]
        values = {
            key: decimal_value(row[key], optional=True)
            for key in ("raw_open_interest", "open_interest_base", "open_interest_quote", "open_interest_usd", "mark_price")
        }
        normalization = row["normalization_method"]
        if status is PolicyDataStatusV1.AVAILABLE and not _valid_oi_normalization(row):
            quality = EvidenceQualityV1.PARTIAL
        payload = {
            "row_id": row["id"], "symbol": row["symbol"], "canonical_symbol": row["canonical_symbol"],
            "exchange": row["exchange"], "contract_type": row["contract_type"],
            "margin_asset": row["margin_asset"], "settle_asset": row["settle_asset"],
            **values, "raw_unit": row["raw_unit"], "normalization_method": normalization,
            "exchange_timestamp": event_time, "fetched_at": row["fetched_at"],
            "processed_at": row["processed_at"], "status": status.value,
            "observation_key": row["observation_key"],
        }
        result.append(make_projection(
            evaluation_id=evaluation_id, source_phase=SourcePhaseV1.PHASE2,
            source_type="OPEN_INTEREST", source_ref=f"phase2:open_interest/{row['id']}",
            symbol=symbol, market=market, event_time=event_time, observed_at=event_time,
            captured_at=row["fetched_at"], processed_at=row["processed_at"],
            available_at=row["processed_at"], availability_status=status,
            freshness=_freshness(event_time, status, as_of), quality=quality, coverage=None,
            payload=payload,
        ))

    funding_rows = read_rows(
        conn,
        """SELECT id, symbol, canonical_symbol, exchange, contract_type, funding_rate,
                  funding_interval_seconds, normalized_8h_rate, predicted_funding_rate,
                  realized_funding_rate, next_funding_time, exchange_timestamp, fetched_at,
                  processed_at, status, observation_key, classification
             FROM funding_rates
            WHERE (canonical_symbol = %s OR (canonical_symbol IS NULL AND symbol = %s))
              AND fetched_at <= %s AND processed_at <= %s
              AND (exchange_timestamp IS NULL OR exchange_timestamp <= %s)
            ORDER BY exchange_timestamp DESC NULLS FIRST, id DESC
            LIMIT %s""",
        (canonical_symbol, symbol, as_of, as_of, as_of, MAX_SOURCE_ROWS),
    )
    for row in funding_rows:
        status, quality = status_pair(row["status"])
        event_time = row["exchange_timestamp"]
        values = {
            key: decimal_value(row[key], optional=True)
            for key in ("funding_rate", "normalized_8h_rate", "predicted_funding_rate", "realized_funding_rate")
        }
        payload = {
            "row_id": row["id"], "symbol": row["symbol"], "canonical_symbol": row["canonical_symbol"],
            "exchange": row["exchange"], "contract_type": row["contract_type"], **values,
            "funding_interval_seconds": row["funding_interval_seconds"],
            "next_funding_time": row["next_funding_time"],
            "exchange_timestamp": event_time, "fetched_at": row["fetched_at"],
            "processed_at": row["processed_at"], "status": status.value,
            "observation_key": row["observation_key"], "classification": row["classification"],
        }
        result.append(make_projection(
            evaluation_id=evaluation_id, source_phase=SourcePhaseV1.PHASE2,
            source_type="FUNDING_RATE", source_ref=f"phase2:funding_rates/{row['id']}",
            symbol=symbol, market=market, event_time=event_time, observed_at=event_time,
            captured_at=row["fetched_at"], processed_at=row["processed_at"],
            available_at=row["processed_at"], availability_status=status,
            freshness=_freshness(event_time, status, as_of), quality=quality, coverage=None,
            payload=payload,
        ))

    summaries = read_rows(
        conn,
        """SELECT id, canonical_symbol, snapshot_timestamp, status, oi_exchange_count,
                  funding_exchange_count, oi_total_usd, oi_weighted_funding, median_funding,
                  max_funding, min_funding, funding_dispersion, reason
             FROM cross_exchange_derivative_snapshots
            WHERE canonical_symbol = %s AND snapshot_timestamp <= %s
            ORDER BY snapshot_timestamp DESC, id DESC
            LIMIT %s""",
        (canonical_symbol, as_of, MAX_SOURCE_ROWS),
    )
    for row in summaries:
        status, quality = status_pair(row["status"])
        snapshot_time = row["snapshot_timestamp"]
        payload = {
            "row_id": row["id"], "canonical_symbol": row["canonical_symbol"],
            "snapshot_timestamp": snapshot_time, "oi_exchange_count": row["oi_exchange_count"],
            "funding_exchange_count": row["funding_exchange_count"],
            "oi_total_usd": decimal_value(row["oi_total_usd"], optional=True),
            "oi_weighted_funding": decimal_value(row["oi_weighted_funding"], optional=True),
            "median_funding": decimal_value(row["median_funding"], optional=True),
            "max_funding": decimal_value(row["max_funding"], optional=True),
            "min_funding": decimal_value(row["min_funding"], optional=True),
            "funding_dispersion": decimal_value(row["funding_dispersion"], optional=True),
            "reason": row["reason"], "status": status.value,
        }
        # The derived summary timestamp is retained as context/availability,
        # never promoted to a source event timestamp.
        result.append(make_projection(
            evaluation_id=evaluation_id, source_phase=SourcePhaseV1.PHASE2,
            source_type="DERIVATIVE_SUMMARY", source_ref=f"phase2:cross_exchange_derivative_snapshots/{row['id']}",
            symbol=symbol, market=market, event_time=None, observed_at=snapshot_time,
            captured_at=None, processed_at=None, available_at=snapshot_time,
            availability_status=status, freshness=EvidenceFreshnessV1.UNKNOWN,
            quality=quality, coverage=None, payload=payload,
        ))

    return tuple(result)
