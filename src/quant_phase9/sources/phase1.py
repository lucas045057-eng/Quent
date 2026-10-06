"""Bounded Phase 1 ticker, observation, and closed-bar projections."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from psycopg import Connection

from quant_phase1.freshness import INTERVAL_SECONDS, evaluate_kline_freshness, expected_latest_closed_open
from quant_data_layer.freshness import FRESHNESS_POLICY
from quant_phase1.stage1 import DEFAULT_GRACE_SECONDS, REQUIRED_INTERVALS
from quant_phase9.contracts import (
    EvidenceFreshnessV1,
    EvidenceQualityV1,
    PolicyDataStatusV1,
    SourcePhaseV1,
    SourceProjectionV1,
    Stage1CandidateEventV1,
)

from . import (
    MAX_SOURCE_ROWS,
    as_utc,
    context_evaluation_id,
    decimal_value,
    freshness_status,
    make_projection,
    read_rows,
    status_pair,
)


_TICKER_NUMERIC_FIELDS = (
    "last_price", "lastPr", "bid_price", "bidPr", "ask_price", "askPr",
    "volume24h", "baseVolume", "turnover24h", "quoteVolume", "high24h", "low24h",
)


def _ticker_value(snapshot: object, key: str) -> object:
    if not isinstance(snapshot, dict):
        return None
    value = snapshot.get(key)
    if key in _TICKER_NUMERIC_FIELDS and value is not None:
        return decimal_value(value)
    return value if isinstance(value, (str, int, bool)) or value is None else None


def _age_freshness(event_time: datetime | None, status: object, as_of: datetime, max_age: int) -> EvidenceFreshnessV1:
    source_status, _ = status_pair(status)
    if source_status is PolicyDataStatusV1.STALE:
        return EvidenceFreshnessV1.STALE
    if source_status is not PolicyDataStatusV1.AVAILABLE or event_time is None:
        return EvidenceFreshnessV1.UNKNOWN
    age = (as_of - event_time).total_seconds()
    if age < 0:
        raise ValueError("Phase 1 source event is later than as_of")
    return EvidenceFreshnessV1.FRESH if age <= max_age else EvidenceFreshnessV1.STALE


def select_phase1(
    conn: Connection,
    candidate: Stage1CandidateEventV1,
    timeframe: Literal["15m", "1H", "4H"],
    as_of: datetime,
) -> tuple[SourceProjectionV1, ...]:
    """Select at most 16 rows per Phase 1 source category/interval.

    `snapshot_timestamp` and fetch/processing times are retained as capture
    metadata; only `exchange_timestamp` is treated as ticker event time.
    """
    as_of = as_utc(as_of)
    evaluation_id = context_evaluation_id(candidate, timeframe, as_of)
    symbol = candidate.symbol
    market = candidate.market
    projections = []

    ticker_rows = read_rows(
        conn,
        """SELECT id, snapshot_timestamp, source, exchange, exchange_timestamp,
                  fetched_at, processed_at, status, snapshot
             FROM market_snapshots
            WHERE symbol = %s AND fetched_at <= %s AND processed_at <= %s
              AND (exchange_timestamp IS NULL OR exchange_timestamp <= %s)
            ORDER BY exchange_timestamp DESC NULLS LAST, id DESC
            LIMIT %s""",
        (symbol, as_of, as_of, as_of, MAX_SOURCE_ROWS),
    )
    for row in ticker_rows:
        source_status, quality = status_pair(row["status"])
        event_time = row["exchange_timestamp"]
        snapshot = row["snapshot"]
        fields = {key: _ticker_value(snapshot, key) for key in _TICKER_NUMERIC_FIELDS}
        payload = {
            "row_id": row["id"], "source": row["source"], "exchange": row["exchange"],
            "snapshot_timestamp": row["snapshot_timestamp"], "exchange_timestamp": event_time,
            "status": source_status.value, **fields,
        }
        projections.append(make_projection(
            evaluation_id=evaluation_id, source_phase=SourcePhaseV1.PHASE1,
            source_type="PRICE_TICKER", source_ref=f"phase1:market_snapshots/{row['id']}",
            symbol=symbol, market=market, event_time=event_time, observed_at=event_time,
            captured_at=row["fetched_at"], processed_at=row["processed_at"],
            available_at=row["processed_at"], availability_status=source_status,
            freshness=_age_freshness(event_time, row["status"], as_of,
                                     int(FRESHNESS_POLICY["PRICE_DECISION"].hard_seconds)), quality=quality,
            coverage=None, payload=payload,
        ))

    observation_rows = read_rows(
        conn,
        """SELECT id, metric, value, unit, source, exchange, exchange_timestamp,
                  fetched_at, processed_at, status
             FROM market_observations
            WHERE symbol = %s AND metric = 'last_price'
              AND fetched_at <= %s AND processed_at <= %s
              AND (exchange_timestamp IS NULL OR exchange_timestamp <= %s)
            ORDER BY exchange_timestamp DESC NULLS LAST, id DESC
            LIMIT %s""",
        (symbol, as_of, as_of, as_of, MAX_SOURCE_ROWS),
    )
    for row in observation_rows:
        source_status, quality = status_pair(row["status"])
        event_time = row["exchange_timestamp"]
        value = decimal_value(row["value"], optional=True)
        payload = {
            "row_id": row["id"], "metric": row["metric"], "value": value,
            "unit": row["unit"], "source": row["source"], "exchange": row["exchange"],
            "exchange_timestamp": event_time, "status": source_status.value,
        }
        projections.append(make_projection(
            evaluation_id=evaluation_id, source_phase=SourcePhaseV1.PHASE1,
            source_type="PRICE_OBSERVATION", source_ref=f"phase1:market_observations/{row['id']}",
            symbol=symbol, market=market, event_time=event_time, observed_at=event_time,
            captured_at=row["fetched_at"], processed_at=row["processed_at"],
            available_at=row["processed_at"], availability_status=source_status,
            freshness=_age_freshness(event_time, row["status"], as_of,
                                     int(FRESHNESS_POLICY["PRICE_DECISION"].hard_seconds)), quality=quality,
            coverage=None, payload=payload,
        ))

    for interval in REQUIRED_INTERVALS:
        seconds = INTERVAL_SECONDS[interval]
        expected_open = expected_latest_closed_open(as_of, interval)
        bars = read_rows(
            conn,
            """SELECT id, interval, bar_open_timestamp, open, high, low, close,
                      volume, turnover, exchange_timestamp, fetched_at, processed_at,
                      status, source, exchange
                 FROM klines
                WHERE symbol = %s AND interval = %s
                  AND bar_open_timestamp <= %s
                  AND exchange_timestamp <= %s AND processed_at <= %s
                ORDER BY bar_open_timestamp DESC, id DESC
                LIMIT %s""",
            (symbol, interval, expected_open, as_of, as_of, MAX_SOURCE_ROWS),
        )
        latest_open = bars[0]["bar_open_timestamp"] if bars else None
        stream_result = evaluate_kline_freshness(
            as_of, interval, latest_open, grace_seconds=DEFAULT_GRACE_SECONDS[interval]
        )
        if not bars:
            absent_status = (
                PolicyDataStatusV1.NOT_AVAILABLE
                if stream_result.value == "NOT_AVAILABLE"
                else PolicyDataStatusV1.STALE
            )
            projections.append(make_projection(
                evaluation_id=evaluation_id, source_phase=SourcePhaseV1.PHASE1,
                source_type="CLOSED_KLINE",
                source_ref=f"phase1:klines:{symbol}:{interval}:expected:{expected_open.isoformat()}",
                symbol=symbol, market=market, event_time=None, observed_at=None,
                captured_at=None, processed_at=None, available_at=None,
                availability_status=absent_status,
                freshness=(EvidenceFreshnessV1.UNKNOWN if absent_status is PolicyDataStatusV1.NOT_AVAILABLE
                           else EvidenceFreshnessV1.STALE),
                quality=EvidenceQualityV1.UNKNOWN, coverage=None,
                payload={"interval": interval, "expected_bar_open": expected_open,
                         "expected_bar_close": expected_open + timedelta(seconds=seconds),
                         "close": None, "status": absent_status.value},
            ))
            continue

        for row in reversed(bars):
            source_status, quality = status_pair(row["status"])
            if stream_result.value == "STALE" and source_status is PolicyDataStatusV1.AVAILABLE:
                projected_status = PolicyDataStatusV1.STALE
                freshness = EvidenceFreshnessV1.STALE
            else:
                projected_status = source_status
                freshness = (
                    EvidenceFreshnessV1.FRESH
                    if source_status is PolicyDataStatusV1.AVAILABLE
                    and stream_result.value == "AVAILABLE"
                    else freshness_status(source_status)
                )
            bar_open = row["bar_open_timestamp"]
            payload = {
                "row_id": row["id"], "interval": interval, "bar_open_timestamp": bar_open,
                "bar_close_timestamp": bar_open + timedelta(seconds=seconds),
                "open": decimal_value(row["open"]), "high": decimal_value(row["high"]),
                "low": decimal_value(row["low"]), "close": decimal_value(row["close"]),
                "volume": decimal_value(row["volume"]), "turnover": decimal_value(row["turnover"]),
                "exchange_timestamp": row["exchange_timestamp"], "source": row["source"],
                "exchange": row["exchange"], "status": projected_status.value,
            }
            projections.append(make_projection(
                evaluation_id=evaluation_id, source_phase=SourcePhaseV1.PHASE1,
                source_type="CLOSED_KLINE", source_ref=f"phase1:klines/{row['id']}",
                symbol=symbol, market=market, event_time=bar_open,
                observed_at=row["exchange_timestamp"], captured_at=row["fetched_at"],
                processed_at=row["processed_at"], available_at=row["processed_at"],
                availability_status=projected_status, freshness=freshness, quality=quality,
                coverage=None, payload=payload,
            ))

    return tuple(projections)
