"""Bounded, read-only Phase 9 source projections."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg import Connection
from psycopg.rows import dict_row

from quant_phase9.canonical import canonical_json, canonical_sha256
from quant_phase9.contracts import (
    EvidenceFreshnessV1,
    EvidenceQualityV1,
    PolicyCoverageStatusV1,
    PolicyDataStatusV1,
    SourcePhaseV1,
    SourceProjectionV1,
    Stage1CandidateEventV1,
)


MAX_SOURCE_ROWS = 16
SOURCE_PROJECTION_SCHEMA_V1 = "PHASE9_SOURCE_PROJECTION_V1"


class Phase9SourceReader(Protocol):
    def read(
        self,
        conn: Connection,
        *,
        candidate: Stage1CandidateEventV1,
        timeframe: Literal["15m", "1H", "4H"],
        as_of: datetime,
    ) -> tuple[SourceProjectionV1, ...]: ...


def require_utc(value: object, name: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    return value.astimezone(timezone.utc)


def as_utc(value: datetime) -> datetime:
    result = require_utc(value, "timestamp")
    assert isinstance(result, datetime)
    return result


def decimal_value(value: object, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("source numeric values must not use binary floats")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        result = Decimal(value)
    else:
        raise ValueError("source numeric value has an unsupported type")
    if not result.is_finite():
        raise ValueError("source numeric value must be finite")
    return result


def status_pair(value: object) -> tuple[PolicyDataStatusV1, EvidenceQualityV1]:
    raw = getattr(value, "value", value)
    mapping = {
        "AVAILABLE": (PolicyDataStatusV1.AVAILABLE, EvidenceQualityV1.VALID),
        "STALE": (PolicyDataStatusV1.STALE, EvidenceQualityV1.UNKNOWN),
        "NOT_AVAILABLE": (PolicyDataStatusV1.NOT_AVAILABLE, EvidenceQualityV1.UNKNOWN),
        "ERROR": (PolicyDataStatusV1.ERROR, EvidenceQualityV1.UNKNOWN),
        "PARTIAL": (PolicyDataStatusV1.PARTIAL, EvidenceQualityV1.PARTIAL),
    }
    if raw not in mapping:
        return PolicyDataStatusV1.NOT_AVAILABLE, EvidenceQualityV1.UNKNOWN
    return mapping[raw]


def freshness_status(value: object) -> EvidenceFreshnessV1:
    raw = getattr(value, "value", value)
    return {
        "AVAILABLE": EvidenceFreshnessV1.FRESH,
        "FRESH": EvidenceFreshnessV1.FRESH,
        "STALE": EvidenceFreshnessV1.STALE,
        "PARTIAL": EvidenceFreshnessV1.UNKNOWN,
        "NOT_AVAILABLE": EvidenceFreshnessV1.UNKNOWN,
        "ERROR": EvidenceFreshnessV1.UNKNOWN,
    }.get(raw, EvidenceFreshnessV1.UNKNOWN)


def context_evaluation_id(
    candidate: Stage1CandidateEventV1,
    timeframe: Literal["15m", "1H", "4H"],
    as_of: datetime,
) -> UUID:
    """Temporary deterministic read-context ID, rebound by build_snapshot."""
    return uuid5(
        NAMESPACE_URL,
        canonical_json({"candidate_event_id": candidate.event_id, "timeframe": timeframe, "as_of": as_utc(as_of)}),
    )


def make_projection(
    *,
    evaluation_id: UUID,
    source_phase: SourcePhaseV1,
    source_type: str,
    source_ref: str,
    symbol: str,
    market: str,
    event_time: datetime | None,
    observed_at: datetime | None,
    captured_at: datetime | None,
    processed_at: datetime | None,
    available_at: datetime | None,
    availability_status: PolicyDataStatusV1,
    freshness: EvidenceFreshnessV1,
    quality: EvidenceQualityV1,
    coverage: PolicyCoverageStatusV1 | None,
    payload: Mapping[str, object],
    source_schema_version: str = SOURCE_PROJECTION_SCHEMA_V1,
) -> SourceProjectionV1:
    canonical_payload = {"schema": source_schema_version, **dict(payload)}
    digest = canonical_sha256(canonical_payload)
    projection_id = projection_id_for(evaluation_id, source_ref, digest)
    return SourceProjectionV1(
        projection_id=projection_id,
        evaluation_id=evaluation_id,
        source_phase=source_phase,
        source_type=source_type,
        source_ref=source_ref,
        symbol=symbol,
        market=market,
        event_time=event_time,
        observed_at=observed_at,
        captured_at=captured_at,
        processed_at=processed_at,
        available_at=available_at,
        availability_status=availability_status,
        freshness_status=freshness,
        quality_status=quality,
        coverage_status=coverage,
        canonical_payload=canonical_payload,
        source_schema_version=source_schema_version,
        projection_version=SOURCE_PROJECTION_SCHEMA_V1,
        canonical_digest=digest,
    )


def projection_id_for(evaluation_id: UUID, source_ref: str, digest: str) -> UUID:
    return uuid5(
        NAMESPACE_URL,
        canonical_json({"evaluation_id": evaluation_id, "source_ref": source_ref, "canonical_digest": digest}),
    )


def read_rows(conn: Connection, query: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
    """Read rows through the caller's transaction without changing its settings."""
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(query, params)
        return list(cursor.fetchall())


class CorePhase9SourceReader:
    """Compose Phase 1–8 readers in fixed source order on one connection."""

    def read(
        self,
        conn: Connection,
        *,
        candidate: Stage1CandidateEventV1,
        timeframe: Literal["15m", "1H", "4H"],
        as_of: datetime,
    ) -> tuple[SourceProjectionV1, ...]:
        from .phase1 import select_phase1
        from .phase2 import select_phase2
        from .phase3 import select_phase3
        from .phase4 import select_phase4
        from .phase5 import select_phase5
        from .phase6 import select_phase6
        from .phase7 import select_phase7
        from .phase8 import select_phase8

        return (
            *select_phase1(conn, candidate, timeframe, as_of),
            *select_phase2(conn, candidate, timeframe, as_of),
            *select_phase3(conn, candidate, timeframe, as_of),
            *select_phase4(conn, candidate, timeframe, as_of),
            *select_phase5(conn, candidate, timeframe, as_of),
            *select_phase6(conn, candidate, timeframe, as_of),
            *select_phase7(conn, candidate, timeframe, as_of),
            *select_phase8(conn, candidate, timeframe, as_of),
        )
