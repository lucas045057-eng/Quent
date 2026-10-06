"""Bounded Phase 8 options projections with per-metric time/unit fidelity."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
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

from . import MAX_SOURCE_ROWS, as_utc, context_evaluation_id, make_projection, read_rows, status_pair


def _bounded(rows: list[dict[str, object]], category: str) -> list[dict[str, object]]:
    if len(rows) > MAX_SOURCE_ROWS:
        raise ValueError(f"Phase 8 {category} projection exceeds the bounded row limit")
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
    if isinstance(value, list):
        return [_json(item) for item in value]
    return value


def _timestamp(value: object, name: str, as_of: datetime) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"Phase 8 {name} must be an aware timestamp")
    value = value.astimezone(as_of.tzinfo)
    if value > as_of:
        raise ValueError(f"Phase 8 {name} is later than as_of")
    return value


def _numeric(value: object) -> object:
    if isinstance(value, bool) or value is None or isinstance(value, (Decimal, int)):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except InvalidOperation:
            return value
        return parsed if parsed.is_finite() else value
    return _json(value)


def _metric(value: object, metadata: object, *, name: str, as_of: datetime) -> dict[str, object]:
    meta = metadata if isinstance(metadata, dict) else {}
    value = _numeric(value)
    raw_status, quality = status_pair(meta.get("status", "NOT_AVAILABLE"))
    unit_status = meta.get("unit_status")
    unit_ok = unit_status in {"VERIFIED", "NOT_APPLICABLE"}
    if raw_status is not PolicyDataStatusV1.AVAILABLE:
        # Preserve an observed degradation/error; unit uncertainty must not
        # turn a stale or failed source into a different health state.
        availability = raw_status
    elif value is None or not unit_ok:
        # Retain source value verbatim, but never treat an unverified scale as usable.
        availability = PolicyDataStatusV1.NOT_AVAILABLE
        quality = EvidenceQualityV1.UNKNOWN
    else:
        availability = raw_status
    source_time = _timestamp(meta.get("field_last_updated_at"), f"{name}.field_last_updated_at", as_of)
    if source_time is None:
        source_time = _timestamp(meta.get("exchange_timestamp"), f"{name}.exchange_timestamp", as_of)
    source_times_raw = meta.get("source_timestamps")
    source_times: list[datetime] = []
    if isinstance(source_times_raw, (list, tuple)):
        source_times = [
            parsed for item in source_times_raw
            if (parsed := _timestamp(item, f"{name}.source_timestamp", as_of)) is not None
        ]
    elif source_time is not None:
        source_times = [source_time]
    if "source_timestamp" not in meta:
        source_time = max(source_times) if source_times else source_time
    data_age = meta.get("data_age_seconds")
    if data_age is not None:
        data_age = _numeric(data_age)
        if isinstance(data_age, Decimal) and data_age < 0:
            raise ValueError(f"Phase 8 {name} has negative data age")
    elif source_times:
        data_age = Decimal(str((as_of - min(source_times)).total_seconds()))
    else:
        data_age = None
    result = {
        "value": value,
        "source_field": meta.get("source_field"),
        "source_timestamp": source_time,
        "source_timestamps": source_times,
        "timestamp_semantics": meta.get("timestamp_semantics"),
        "source_capture_times": {
            key: _timestamp(meta.get(key), f"{name}.{key}", as_of)
            for key in ("fetched_at", "received_at", "processed_at")
            if meta.get(key) is not None
        },
        "data_age_seconds": data_age,
        "status": raw_status.value,
        "availability_status": availability.value,
        "quality_status": quality.value,
        "unit_code": meta.get("unit_code"),
        "unit_status": unit_status if isinstance(unit_status, str) else "UNKNOWN",
        "coverage": _json(meta.get("coverage", meta.get("coverage_ratio"))),
        "provenance": meta.get("provenance"),
        "quality_reason": meta.get("quality_reason", meta.get("reason")),
        "raw_reference": meta.get("raw_reference"),
    }
    return result


def _metrics(values: object, metadata: object, *, as_of: datetime) -> dict[str, object]:
    values = values if isinstance(values, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    names = sorted(set(values) | set(metadata))
    result = {}
    for name in names:
        value = values.get(name)
        metric_metadata = metadata.get(name)
        if isinstance(value, dict) and "value" in value:
            metric_metadata = {**value, **(metric_metadata if isinstance(metric_metadata, dict) else {})}
            value = value.get("value")
        result[name] = _metric(value, metric_metadata, name=name, as_of=as_of)
    return result


def select_phase8(
    conn: Connection,
    candidate: Stage1CandidateEventV1,
    timeframe: Literal["15m", "1H", "4H"],
    as_of: datetime,
) -> tuple[SourceProjectionV1, ...]:
    """Freeze BTC/ETH option metrics without refreshing per-field source clocks."""
    as_of = as_utc(as_of)
    evaluation_id = context_evaluation_id(candidate, timeframe, as_of)
    underlying = candidate.symbol.removesuffix("USDT")
    if candidate.symbol != f"{underlying}USDT" or underlying not in {"BTC", "ETH"}:
        return ()
    result: list[SourceProjectionV1] = []

    observations = _bounded(read_rows(conn, """SELECT id,exchange,source,symbol,underlying,
              price_index,underlying_index,quote_currency,observation_kind,exchange_timestamp,
              timestamp_semantics,fetched_at,received_at,processed_at,status,schema_version,
              metrics::text AS metrics_text,field_metadata::text AS field_metadata_text,
              payload_hash,raw_reference
         FROM phase8_option_market_snapshots
        WHERE underlying=%s AND processed_at<=%s
          AND (exchange_timestamp IS NULL OR exchange_timestamp<=%s)
          AND (fetched_at IS NULL OR fetched_at<=%s)
          AND (received_at IS NULL OR received_at<=%s)
        ORDER BY processed_at DESC,id DESC LIMIT %s""",
        (underlying,as_of,as_of,as_of,as_of,MAX_SOURCE_ROWS+1)), "market snapshots")
    for row in reversed(observations):
        status, quality = status_pair(row["status"])
        row_time = row["exchange_timestamp"]
        verified = row["timestamp_semantics"] == "VERIFIED"
        event_time = row_time if verified else None
        captured_at = row["fetched_at"] or row["received_at"]
        values = _json(row["metrics_text"])
        metadata = _json(row["field_metadata_text"])
        metric_values = _metrics(values, metadata, as_of=as_of)
        payload = {
            "row_id": row["id"], "exchange": row["exchange"], "source": row["source"],
            "option_symbol": row["symbol"], "underlying": row["underlying"],
            "price_index": row["price_index"], "underlying_index": row["underlying_index"],
            "quote_currency": row["quote_currency"], "observation_kind": row["observation_kind"],
            "exchange_timestamp": row_time, "timestamp_semantics": row["timestamp_semantics"],
            "fetched_at": row["fetched_at"], "received_at": row["received_at"],
            "processed_at": row["processed_at"], "status": status.value,
            "schema_version": row["schema_version"], "metrics": metric_values,
            "payload_hash": row["payload_hash"], "raw_reference": row["raw_reference"],
        }
        result.append(make_projection(
            evaluation_id=evaluation_id,source_phase=SourcePhaseV1.PHASE8,
            source_type="OPTION_MARKET_SNAPSHOT",
            source_ref=f"phase8:phase8_option_market_snapshots/{row['id']}",
            symbol=candidate.symbol,market=candidate.market,event_time=event_time,
            observed_at=event_time,captured_at=captured_at,processed_at=row["processed_at"],
            available_at=row["processed_at"],availability_status=status,
            freshness=(EvidenceFreshnessV1.STALE if status is PolicyDataStatusV1.STALE
                       else EvidenceFreshnessV1.UNKNOWN),
            quality=(EvidenceQualityV1.PARTIAL if status is PolicyDataStatusV1.PARTIAL else quality),
            coverage=(PolicyCoverageStatusV1.PARTIAL if status is PolicyDataStatusV1.PARTIAL
                      else PolicyCoverageStatusV1.UNKNOWN),payload=payload,
        ))

    contexts = _bounded(read_rows(conn, """SELECT id,underlying,context_timestamp,processed_at,
              calculation_version,metrics::text AS metrics_text,source_timestamps::text AS source_timestamps_text,
              source_capture_times::text AS source_capture_times_text,coverage::text AS coverage_text,
              provenance::text AS provenance_text,input_observation_ids::text AS input_ids_text,
              status,reason_code,unit_contract_version,context_only
         FROM phase8_option_context_snapshots
        WHERE underlying=%s AND context_timestamp<=%s AND processed_at<=%s
        ORDER BY context_timestamp DESC,id DESC LIMIT %s""",
        (underlying,as_of,as_of,MAX_SOURCE_ROWS+1)), "context snapshots")
    for row in reversed(contexts):
        status, quality = status_pair(row["status"])
        values = _json(row["metrics_text"])
        timestamps = _json(row["source_timestamps_text"])
        capture_times = _json(row["source_capture_times_text"])
        coverage = _json(row["coverage_text"])
        provenance = _json(row["provenance_text"])
        input_ids = _json(row["input_ids_text"])
        context_metrics = _metrics(values, {}, as_of=as_of)
        # Context metrics keep their own source clocks/status/unit contracts;
        # context_timestamp is only the derived calculation's clock.
        for name, item in context_metrics.items():
            raw_times = timestamps.get(name, []) if isinstance(timestamps, dict) else []
            item["source_timestamps"] = [
                parsed for raw in (raw_times if isinstance(raw_times, (list, tuple)) else [raw_times])
                if (parsed := _timestamp(raw, f"{name}.source_timestamp", as_of)) is not None
            ]
            item["source_timestamp"] = max(item["source_timestamps"]) if item["source_timestamps"] else None
            item["data_age_seconds"] = item["data_age_seconds"]
            if isinstance(coverage, dict):
                item["coverage"] = _json(coverage.get(name))
            if isinstance(provenance, dict):
                item["provenance"] = _json(provenance.get(name))
            if isinstance(capture_times, dict):
                captures = capture_times.get(name)
                if isinstance(captures, dict):
                    item["source_capture_times"] = {
                        key: _timestamp(value, f"{name}.{key}", as_of)
                        for key, value in captures.items() if value is not None
                    }
        payload = {
            "row_id":row["id"],"underlying":row["underlying"],
            "context_timestamp":row["context_timestamp"],"processed_at":row["processed_at"],
            "calculation_version":row["calculation_version"],"metrics":context_metrics,
            "source_timestamps":timestamps,"source_capture_times":capture_times,
            "coverage":coverage,"provenance":provenance,"input_observation_ids":input_ids,
            "status":status.value,"reason_code":row["reason_code"],
            "unit_contract_version":row["unit_contract_version"],"context_only":row["context_only"],
        }
        result.append(make_projection(
            evaluation_id=evaluation_id,source_phase=SourcePhaseV1.PHASE8,
            source_type="OPTION_CONTEXT_SNAPSHOT",
            source_ref=f"phase8:phase8_option_context_snapshots/{row['id']}",
            symbol=candidate.symbol,market=candidate.market,event_time=None,
            observed_at=row["context_timestamp"],captured_at=None,processed_at=row["processed_at"],
            available_at=row["processed_at"],availability_status=status,
            freshness=(EvidenceFreshnessV1.STALE if status is PolicyDataStatusV1.STALE
                       else EvidenceFreshnessV1.UNKNOWN),
            quality=(EvidenceQualityV1.PARTIAL if status is PolicyDataStatusV1.PARTIAL else quality),
            coverage=(PolicyCoverageStatusV1.PARTIAL if status is PolicyDataStatusV1.PARTIAL
                      else PolicyCoverageStatusV1.UNKNOWN),payload=payload,
        ))

    return tuple(result)
