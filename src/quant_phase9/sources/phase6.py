"""Bounded projections of configured Phase 6 sources and their known events."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
from typing import Literal

from psycopg import Connection
from psycopg.types.json import Jsonb

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
        raise ValueError(f"Phase 6 {category} projection exceeds the bounded row limit")
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


def _event_projection(
    *, evaluation_id, candidate, source_type: str, table: str,
    row: dict[str, object], as_of: datetime, numeric_fields: tuple[str, ...] = (),
) -> SourceProjectionV1:
    status, quality = status_pair(row["status"])
    event_at = row.get("event_at")
    observed_at = row["observed_at"]
    fetched_at = row["fetched_at"]
    processed_at = row["processed_at"]
    if not all(isinstance(value, datetime) for value in (observed_at, fetched_at, processed_at)):
        raise ValueError("Phase 6 event lacks an availability timestamp")
    for field in ("published_at", "released_at", "value_at"):
        timestamp = row.get(field)
        if isinstance(timestamp, datetime) and timestamp > as_of:
            raise ValueError(f"Phase 6 {field} is later than as_of")
    payload: dict[str, object] = {key: _json(value) for key, value in row.items() if key != "id"}
    for field in numeric_fields:
        payload[field] = decimal_value(row[field], optional=True)
    # The scheduled/event date may be in the future while its announcement is
    # already available. Keep it as content, but never use it as observed time.
    event_time = event_at if isinstance(event_at, datetime) and event_at <= as_of else None
    return make_projection(
        evaluation_id=evaluation_id, source_phase=SourcePhaseV1.PHASE6,
        source_type=source_type, source_ref=f"phase6:{table}/{row['id']}",
        symbol=candidate.symbol, market=candidate.market,
        event_time=event_time, observed_at=observed_at, captured_at=fetched_at,
        processed_at=processed_at, available_at=processed_at,
        availability_status=status,
        freshness=(EvidenceFreshnessV1.STALE if status is PolicyDataStatusV1.STALE
                   else EvidenceFreshnessV1.UNKNOWN),
        quality=(EvidenceQualityV1.PARTIAL if status is PolicyDataStatusV1.PARTIAL else quality),
        coverage=(PolicyCoverageStatusV1.PARTIAL if status is PolicyDataStatusV1.PARTIAL
                  else PolicyCoverageStatusV1.UNKNOWN),
        payload=payload,
    )


def select_phase6(
    conn: Connection,
    candidate: Stage1CandidateEventV1,
    timeframe: Literal["15m", "1H", "4H"],
    as_of: datetime,
) -> tuple[SourceProjectionV1, ...]:
    """Snapshot registry configuration separately from absent event records."""
    as_of = as_utc(as_of)
    evaluation_id = context_evaluation_id(candidate, timeframe, as_of)
    params = (candidate.symbol, as_of, as_of, as_of)
    news = _bounded(read_rows(conn, """SELECT id,event_id,event_fingerprint,source,source_type,source_ref,
              published_at,observed_at,event_at,fetched_at,processed_at,event_type,entities,symbols,
              headline,summary,importance,sentiment,impact_horizon,status,reason_code,confidence,
              content_hash,parser_version
         FROM phase6_news_events
        WHERE symbols @> %s AND observed_at<=%s AND fetched_at<=%s AND processed_at<=%s
        ORDER BY observed_at DESC,id DESC LIMIT %s""",
        (Jsonb([candidate.symbol]), *params[1:], MAX_SOURCE_ROWS + 1)), "news events")
    macro = _bounded(read_rows(conn, """SELECT id,event_id,event_fingerprint,source,source_type,source_ref,
              published_at,observed_at,event_at,scheduled_at,released_at,fetched_at,processed_at,
              event_type,macro_event_type,region,actual,forecast,previous,unit,forecast_unit,surprise,
              entities,symbols,summary,importance,status,reason_code,confidence,content_hash,parser_version
         FROM phase6_macro_events
        WHERE observed_at<=%s AND fetched_at<=%s AND processed_at<=%s
        ORDER BY observed_at DESC,id DESC LIMIT %s""",
        (*params[1:], MAX_SOURCE_ROWS + 1)), "macro events")
    unlocks = _bounded(read_rows(conn, """SELECT id,event_id,event_fingerprint,source,source_type,source_ref,
              published_at,observed_at,event_at,fetched_at,processed_at,event_type,symbol,asset,amount,
              amount_unit,value,value_currency,value_at,circulating_supply,circulating_supply_unit,
              circulating_supply_ref,unlock_pct,recipient_category,entities,summary,importance,status,
              reason_code,confidence,content_hash,parser_version
         FROM phase6_unlock_events
        WHERE symbol=%s AND observed_at<=%s AND fetched_at<=%s AND processed_at<=%s
        ORDER BY observed_at DESC,id DESC LIMIT %s""",
        (candidate.symbol, *params[1:], MAX_SOURCE_ROWS + 1)), "unlock events")
    registry = _bounded(read_rows(conn, """SELECT source_id,source_type,parser_version,policy_version,
              max_bytes,timeout_seconds,max_redirects,status,processed_at
         FROM phase6_source_registry WHERE processed_at<=%s
        ORDER BY source_id LIMIT %s""", (as_of, MAX_SOURCE_ROWS + 1)), "source registry")

    result: list[SourceProjectionV1] = []
    if not registry:
        has_events = bool(news or macro or unlocks)
        status = PolicyDataStatusV1.ERROR if has_events else PolicyDataStatusV1.NOT_CONFIGURED
        quality = EvidenceQualityV1.INVALID if has_events else EvidenceQualityV1.UNKNOWN
        result.append(make_projection(
            evaluation_id=evaluation_id,source_phase=SourcePhaseV1.PHASE6,
            source_type="SOURCE_REGISTRY",source_ref="phase6:source_registry:empty",
            symbol=candidate.symbol,market=candidate.market,event_time=None,observed_at=None,
            captured_at=None,processed_at=None,available_at=None,availability_status=status,
            freshness=EvidenceFreshnessV1.UNKNOWN,quality=quality,
            coverage=PolicyCoverageStatusV1.NOT_AVAILABLE,
            payload={"configured":False,"reason":"EVENTS_WITHOUT_REGISTRY" if has_events else "NO_REGISTERED_SOURCE"},
        ))
    else:
        for row in registry:
            status, quality = status_pair(row["status"])
            result.append(make_projection(
                evaluation_id=evaluation_id,source_phase=SourcePhaseV1.PHASE6,
                source_type="SOURCE_REGISTRY",source_ref=f"phase6:source_registry/{row['source_id']}",
                symbol=candidate.symbol,market=candidate.market,event_time=None,observed_at=None,
                captured_at=None,processed_at=row["processed_at"],available_at=row["processed_at"],
                availability_status=status,freshness=EvidenceFreshnessV1.UNKNOWN,
                quality=quality,coverage=PolicyCoverageStatusV1.UNKNOWN,
                payload={"configured":True,**row},
            ))

    for row in news:
        if len(str(row["headline"])) > 4096 or len(str(row["summary"])) > 4096:
            raise ValueError("Phase 6 news text exceeds the bounded projection size")
        result.append(_event_projection(evaluation_id=evaluation_id,candidate=candidate,
            source_type="NEWS_EVENT",table="phase6_news_events",row=row,as_of=as_of,
            numeric_fields=("confidence",)))
    for row in macro:
        if len(str(row["summary"])) > 4096:
            raise ValueError("Phase 6 macro summary exceeds the bounded projection size")
        result.append(_event_projection(evaluation_id=evaluation_id,candidate=candidate,
            source_type="MACRO_EVENT",table="phase6_macro_events",row=row,as_of=as_of,
            numeric_fields=("actual","forecast","previous","surprise","confidence")))
    for row in unlocks:
        if len(str(row["summary"])) > 4096:
            raise ValueError("Phase 6 unlock summary exceeds the bounded projection size")
        result.append(_event_projection(evaluation_id=evaluation_id,candidate=candidate,
            source_type="UNLOCK_EVENT",table="phase6_unlock_events",row=row,as_of=as_of,
            numeric_fields=("amount","value","circulating_supply","unlock_pct","confidence")))
    return tuple(result)
