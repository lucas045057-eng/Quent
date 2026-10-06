"""Transactional Stage 1 candidate event creation and Phase 9 outbox writer."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from psycopg import Connection
from psycopg.types.json import Jsonb

from quant_phase1.repositories import Phase1Repository
from quant_phase1.stage1 import Stage1Result

from .canonical import canonical_bytes, canonical_json, canonical_sha256, event_id_for, evaluation_id_for
from .contracts import EvaluationIdentityV1, EventIdentityV1, Stage1CandidateEventV1


STAGE1_CANDIDATE_EVENT_TYPE = "phase9.stage1_candidate"
STAGE1_CANDIDATE_EVENT_SCHEMA = "PHASE9_STAGE1_CANDIDATE_EVENT_V1"
MAX_STAGE1_EVENT_BYTES = 64 * 1024
_SAFE_REFERENCE = re.compile(r"^[A-Za-z0-9_.:/-]{1,256}$")
PHASE9_OUTBOX_MAX_ATTEMPTS_V1 = 3


@dataclass(frozen=True, slots=True)
class Stage1IntakeTTLPolicyV1:
    """Optional Stage1 intake lifetime, independent of Phase9 and execution TTLs."""

    stage1_candidate_ttl_seconds: int | None

    def __post_init__(self) -> None:
        value = self.stage1_candidate_ttl_seconds
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise ValueError("stage1_candidate_ttl_seconds must be a positive integer or None")


def resolve_stage1_candidate_expiry(
    candidate_created_at: datetime, *, policy: Stage1IntakeTTLPolicyV1
) -> datetime | None:
    if not isinstance(policy, Stage1IntakeTTLPolicyV1):
        raise TypeError("policy must be Stage1IntakeTTLPolicyV1")
    if (not isinstance(candidate_created_at, datetime)
            or candidate_created_at.tzinfo is None
            or candidate_created_at.utcoffset() is None
            or candidate_created_at.utcoffset() != timezone.utc.utcoffset(candidate_created_at)):
        raise ValueError("candidate_created_at must be UTC-aware")
    seconds = policy.stage1_candidate_ttl_seconds
    return None if seconds is None else candidate_created_at + timedelta(seconds=seconds)


class AdmissionDispositionV1(StrEnum):
    ADMITTED = "ADMITTED"
    ALREADY_ADMITTED = "ALREADY_ADMITTED"
    EVENT_EXPIRED = "EVENT_EXPIRED"
    INVALID_EVENT = "INVALID_EVENT"


class OutboxEventStateV1(StrEnum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RETRY_WAIT = "RETRY_WAIT"
    DEAD_LETTER = "DEAD_LETTER"


class RetryableErrorCodeV1(StrEnum):
    DB_SERIALIZATION_CONFLICT = "DB_SERIALIZATION_CONFLICT"
    AI_GATEWAY_TIMEOUT = "AI_GATEWAY_TIMEOUT"
    AI_GATEWAY_RATE_LIMITED = "AI_GATEWAY_RATE_LIMITED"
    AI_GATEWAY_UNAVAILABLE = "AI_GATEWAY_UNAVAILABLE"


class EventIdentityConflictError(RuntimeError):
    """A stored event identity was reused with different canonical content."""


class Phase9LeaseError(RuntimeError):
    """Base error for invalid Phase 9 outbox lease operations."""


class EventNotClaimedError(Phase9LeaseError):
    pass


class LeaseOwnershipError(Phase9LeaseError):
    pass


class LeaseExpiredError(Phase9LeaseError):
    pass


class AlreadyAcknowledgedError(Phase9LeaseError):
    pass


def _event_content(
    *,
    event_type: str,
    event_schema_version: str,
    stage1_candidate_id: int,
    screening_result_id: int,
    symbol: str,
    market: str,
    candidate_created_at: datetime,
    candidate_valid_until: datetime | None,
    stage1_policy_version: str,
    source_as_of: datetime,
    source_refs: tuple[str, ...],
    canonical_payload: Mapping[str, object],
) -> dict[str, object]:
    return {
        "event_type": event_type,
        "event_schema_version": event_schema_version,
        "stage1_candidate_id": stage1_candidate_id,
        "screening_result_id": screening_result_id,
        "symbol": symbol,
        "market": market,
        "candidate_created_at": candidate_created_at,
        "candidate_valid_until": candidate_valid_until,
        "stage1_policy_version": stage1_policy_version,
        "source_as_of": source_as_of,
        "source_refs": source_refs,
        "canonical_payload": canonical_payload,
    }


def _event_digest(event: Stage1CandidateEventV1) -> str:
    return str(
        canonical_sha256(
            _event_content(
                event_type=event.event_type,
                event_schema_version=event.event_schema_version,
                stage1_candidate_id=event.stage1_candidate_id,
                screening_result_id=event.screening_result_id,
                symbol=event.symbol,
                market=event.market,
                candidate_created_at=event.candidate_created_at,
                candidate_valid_until=event.candidate_valid_until,
                stage1_policy_version=event.stage1_policy_version,
                source_as_of=event.source_as_of,
                source_refs=event.source_refs,
                canonical_payload=event.canonical_payload,
            )
        )
    )


def _valid_event(event: object) -> bool:
    if not isinstance(event, Stage1CandidateEventV1):
        return False
    try:
        if event.event_type != STAGE1_CANDIDATE_EVENT_TYPE:
            return False
        if event.event_schema_version != STAGE1_CANDIDATE_EVENT_SCHEMA:
            return False
        if event.stage1_candidate_id != event.screening_result_id:
            return False
        if _event_digest(event) != event.canonical_payload_digest:
            return False
        expected_id = event_id_for(
            EventIdentityV1(
                event_type=event.event_type,
                event_schema_version=event.event_schema_version,
                screening_result_id=event.screening_result_id,
                canonical_payload_digest=event.canonical_payload_digest,
            )
        )
        if event.event_id != expected_id:
            return False
        payload = event.canonical_payload
        projection = payload.get("stage1_projection")
        if payload.get("schema") != STAGE1_CANDIDATE_EVENT_SCHEMA or not isinstance(projection, Mapping):
            return False
        if projection.get("symbol") != event.symbol or projection.get("category") not in {"A", "B"}:
            return False
        if projection.get("status") not in {"AVAILABLE", "STALE", "NOT_AVAILABLE", "ERROR"}:
            return False
        return isinstance(payload.get("instrument_scope"), Mapping)
    except (TypeError, ValueError):
        return False


def _event_envelope(event: Stage1CandidateEventV1) -> dict[str, object]:
    return {
        "schema": event.event_schema_version,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "event_schema_version": event.event_schema_version,
        "stage1_candidate_id": event.stage1_candidate_id,
        "screening_result_id": event.screening_result_id,
        "symbol": event.symbol,
        "market": event.market,
        "candidate_created_at": event.candidate_created_at,
        "candidate_valid_until": event.candidate_valid_until,
        "stage1_policy_version": event.stage1_policy_version,
        "source_as_of": event.source_as_of,
        "source_refs": event.source_refs,
        "canonical_payload_digest": event.canonical_payload_digest,
        "created_at": event.created_at,
        "canonical_payload": event.canonical_payload,
    }


def _require_utc(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")


def _safe_stage1_reference(result: Stage1Result) -> dict[str, str] | None:
    reference = result.data_snapshot_reference
    if reference is None:
        return None
    provider = reference.provider if _SAFE_REFERENCE.fullmatch(reference.provider) else "REDACTED"
    endpoint = reference.endpoint if _SAFE_REFERENCE.fullmatch(reference.endpoint) else "REDACTED"
    payload_hash = reference.payload_hash
    if payload_hash is not None and not re.fullmatch(r"[0-9a-f]{64}", payload_hash):
        payload_hash = None
    return {
        "provider": provider,
        "endpoint": endpoint,
        "payload_hash": payload_hash or "",
    }


def build_stage1_candidate_event(
    *,
    screening_result_id: int,
    run_id: int,
    screening_result: Stage1Result,
    market: str,
    instrument_scope: Mapping[str, str],
    candidate_created_at: datetime,
    candidate_valid_until: datetime | None,
    stage1_policy_version: str,
    source_as_of: datetime,
) -> Stage1CandidateEventV1:
    """Build the immutable event payload from the persisted Stage 1 result."""
    if screening_result.category not in {"A", "B"}:
        raise ValueError("only Stage 1 categories A and B produce Phase 9 events")
    if screening_result.timestamp is None:
        raise ValueError("Stage 1 result timestamp is required for candidate provenance")

    snapshot_reference = _safe_stage1_reference(screening_result)
    source_refs = (
        f"phase1:screening_runs/{run_id}",
        f"phase1:screening_results/{screening_result_id}",
        f"phase1:symbols/{screening_result.symbol}",
    )
    if snapshot_reference is not None:
        source_refs += (
            "phase1:snapshot:"
            f"{snapshot_reference['provider']}/{snapshot_reference['endpoint']}"
            f":sha256/{snapshot_reference['payload_hash'] or 'unhashed'}",
        )
    projection = {
        "schema": STAGE1_CANDIDATE_EVENT_SCHEMA,
        "screening_run_id": run_id,
        "instrument_scope": dict(instrument_scope),
        "stage1_projection": {
            "symbol": screening_result.symbol,
            "category": screening_result.category,
            "classification": screening_result.classification,
            "status": screening_result.status.value,
            "reason": screening_result.reason,
            "reason_codes": screening_result.reason_codes,
            "inputs_used": screening_result.inputs_used,
            "indicators": screening_result.indicators,
            "structure": screening_result.structure,
            "key_metrics": screening_result.key_metrics,
            "data_snapshot_reference": snapshot_reference,
            "processed_at": screening_result.timestamp,
        },
        "source_refs": source_refs,
    }
    digest = canonical_sha256(
        _event_content(
            event_type=STAGE1_CANDIDATE_EVENT_TYPE,
            event_schema_version=STAGE1_CANDIDATE_EVENT_SCHEMA,
            stage1_candidate_id=screening_result_id,
            screening_result_id=screening_result_id,
            symbol=screening_result.symbol,
            market=market,
            candidate_created_at=candidate_created_at,
            candidate_valid_until=candidate_valid_until,
            stage1_policy_version=stage1_policy_version,
            source_as_of=source_as_of,
            source_refs=source_refs,
            canonical_payload=projection,
        )
    )
    event_id = event_id_for(
        EventIdentityV1(
            event_type=STAGE1_CANDIDATE_EVENT_TYPE,
            event_schema_version=STAGE1_CANDIDATE_EVENT_SCHEMA,
            screening_result_id=screening_result_id,
            canonical_payload_digest=digest,
        )
    )
    return Stage1CandidateEventV1(
        event_id=event_id,
        event_type=STAGE1_CANDIDATE_EVENT_TYPE,
        event_schema_version=STAGE1_CANDIDATE_EVENT_SCHEMA,
        stage1_candidate_id=screening_result_id,
        screening_result_id=screening_result_id,
        symbol=screening_result.symbol,
        market=market,
        candidate_created_at=candidate_created_at,
        candidate_valid_until=candidate_valid_until,
        stage1_policy_version=stage1_policy_version,
        source_as_of=source_as_of,
        source_refs=source_refs,
        canonical_payload_digest=digest,
        created_at=candidate_created_at,
        canonical_payload=projection,
    )


class Phase9OutboxWriter:
    """Insert a Phase 9 event using the caller-owned transaction only."""

    def emit(self, conn: Connection, *, event: Stage1CandidateEventV1) -> None:
        if not _valid_event(event):
            raise ValueError("Stage 1 candidate canonical payload digest mismatch")
        serialized = canonical_bytes(_event_envelope(event))
        if len(serialized) > MAX_STAGE1_EVENT_BYTES:
            raise ValueError("Phase 9 Stage 1 candidate event exceeds 64 KiB")
        payload = json.loads(serialized)
        aggregate_key = f"{event.screening_result_id}:{event.canonical_payload_digest}"
        inserted = conn.execute(
            """
            INSERT INTO outbox_events (
                event_type, aggregate_key, created_at, payload, event_id, phase9_state,
                attempt_count, attempt_limit, next_attempt_at
            ) VALUES (%s, %s, %s, %s, %s, 'PENDING', 0, NULL, %s)
            ON CONFLICT (event_type, event_id) WHERE event_id IS NOT NULL DO NOTHING
            RETURNING id
            """,
            (
                event.event_type,
                aggregate_key,
                event.created_at,
                Jsonb(payload),
                event.event_id,
                event.created_at,
            ),
        ).fetchone()
        if inserted is not None:
            return
        existing = conn.execute(
            "SELECT payload FROM outbox_events WHERE event_type = %s AND event_id = %s",
            (event.event_type, event.event_id),
        ).fetchone()
        if existing is None or canonical_json(existing[0]) != serialized.decode("utf-8"):
            raise RuntimeError("Phase 9 event identity conflicts with an existing outbox payload")


def _parse_utc(value: object, name: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValueError(f"stored Phase 9 {name} is not a timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require_utc(parsed, name)
    return parsed


def _event_from_envelope(payload: object) -> Stage1CandidateEventV1:
    if not isinstance(payload, Mapping):
        raise ValueError("stored Phase 9 event payload is not an object")
    refs = payload.get("source_refs")
    canonical_payload = payload.get("canonical_payload")
    if not isinstance(refs, list) or not isinstance(canonical_payload, Mapping):
        raise ValueError("stored Phase 9 event payload has invalid nested fields")
    return Stage1CandidateEventV1(
        event_id=str(payload.get("event_id", "")),
        event_type=str(payload.get("event_type", "")),
        event_schema_version=str(payload.get("event_schema_version", "")),
        stage1_candidate_id=int(payload.get("stage1_candidate_id", 0)),
        screening_result_id=int(payload.get("screening_result_id", 0)),
        symbol=str(payload.get("symbol", "")),
        market=str(payload.get("market", "")),
        candidate_created_at=_parse_utc(payload.get("candidate_created_at"), "candidate_created_at"),
        candidate_valid_until=_parse_utc(
            payload.get("candidate_valid_until"), "candidate_valid_until", optional=True
        ),
        stage1_policy_version=str(payload.get("stage1_policy_version", "")),
        source_as_of=_parse_utc(payload.get("source_as_of"), "source_as_of"),
        source_refs=tuple(str(ref) for ref in refs),
        canonical_payload_digest=str(payload.get("canonical_payload_digest", "")),
        created_at=_parse_utc(payload.get("created_at"), "created_at"),
        canonical_payload=canonical_payload,
    )


def _identity_matches_event(identity: object, event: Stage1CandidateEventV1) -> bool:
    return (
        isinstance(identity, EvaluationIdentityV1)
        and identity.stage1_candidate_id == event.stage1_candidate_id
        and identity.market == event.market
        and identity.symbol == event.symbol
        and identity.timeframe in {"15m", "1H", "4H"}
    )


def admit_event(
    conn: Connection,
    *,
    event: Stage1CandidateEventV1,
    identity: EvaluationIdentityV1,
    now: datetime,
) -> AdmissionDispositionV1:
    """Persist one durable evaluation intake disposition without owning TX."""
    _require_utc(now, "now")
    if not _valid_event(event) or not _identity_matches_event(identity, event):
        return AdmissionDispositionV1.INVALID_EVENT

    outbox = conn.execute(
        """SELECT phase9_state, published_at, payload
           FROM outbox_events WHERE event_type = %s AND event_id = %s FOR UPDATE""",
        (event.event_type, event.event_id),
    ).fetchone()
    if outbox is None:
        return AdmissionDispositionV1.INVALID_EVENT
    state, published_at, stored_payload = outbox
    if not isinstance(stored_payload, Mapping):
        return AdmissionDispositionV1.INVALID_EVENT
    stored_digest = stored_payload.get("canonical_payload_digest")
    if stored_digest != event.canonical_payload_digest:
        raise EventIdentityConflictError("canonical event identity has a different stored payload digest")
    try:
        if canonical_json(stored_payload) != canonical_json(_event_envelope(event)):
            raise EventIdentityConflictError("canonical event identity has a different stored payload")
    except (TypeError, ValueError) as exc:
        raise EventIdentityConflictError("canonical event identity has an invalid stored payload") from exc

    if state in {OutboxEventStateV1.ACKNOWLEDGED.value, OutboxEventStateV1.DEAD_LETTER.value}:
        return AdmissionDispositionV1.ALREADY_ADMITTED
    if published_at is not None:
        return AdmissionDispositionV1.ALREADY_ADMITTED

    existing = conn.execute(
        """SELECT evaluation_id FROM phase9_evaluations
           WHERE stage1_candidate_id = %s AND timeframe = %s
             AND policy_generation = %s AND material_change_generation = %s""",
        (
            identity.stage1_candidate_id,
            identity.timeframe,
            identity.policy_generation,
            identity.material_change_generation,
        ),
    ).fetchone()
    if existing is not None:
        return AdmissionDispositionV1.ALREADY_ADMITTED

    projection = event.canonical_payload["stage1_projection"]
    scope = event.canonical_payload["instrument_scope"]
    stage1_status = projection["status"]
    expired = event.candidate_valid_until is not None and event.candidate_valid_until <= now
    if expired:
        evaluation_state, disposition, reason = "CANCELLED", "EXPIRED", "STAGE1_EXPIRED"
    elif stage1_status != "AVAILABLE":
        evaluation_state, disposition, reason = "CANCELLED", "REJECTED", "STAGE1_STATUS_NOT_AVAILABLE"
    elif event.market != "USDT_PERPETUAL" or scope.get("in_scope") != "true":
        evaluation_state, disposition, reason = "CANCELLED", "REJECTED", "INSTRUMENT_OUT_OF_SCOPE"
    elif event.candidate_valid_until is None:
        evaluation_state, disposition, reason = "QUEUED", "DEFERRED", "INTAKE_TTL_NOT_CONFIGURED"
    else:
        evaluation_state, disposition, reason = "QUEUED", "ADMITTED", None

    inserted = conn.execute(
        """INSERT INTO phase9_evaluations (
               evaluation_id, stage1_candidate_id, symbol, market, timeframe,
               evaluation_state, intake_disposition, candidate_valid_until,
               policy_generation, material_change_generation, reason_code
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (
               stage1_candidate_id, timeframe, policy_generation, material_change_generation
           ) DO NOTHING
           RETURNING evaluation_id""",
        (
            evaluation_id_for(identity),
            identity.stage1_candidate_id,
            identity.symbol,
            identity.market,
            identity.timeframe,
            evaluation_state,
            disposition,
            event.candidate_valid_until,
            identity.policy_generation,
            identity.material_change_generation,
            reason,
        ),
    ).fetchone()
    if inserted is None:
        return AdmissionDispositionV1.ALREADY_ADMITTED
    return AdmissionDispositionV1.EVENT_EXPIRED if expired else AdmissionDispositionV1.ADMITTED


def claim_pending(
    conn: Connection,
    *,
    consumer_name: str,
    limit: int,
    now: datetime,
    lease_until: datetime,
) -> tuple[Stage1CandidateEventV1, ...]:
    """Claim a bounded due page and increment each event's attempt once."""
    _require_utc(now, "now")
    _require_utc(lease_until, "lease_until")
    if not isinstance(consumer_name, str) or not consumer_name.strip():
        raise ValueError("consumer_name must be non-empty")
    if isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit must be positive")
    if lease_until <= now:
        raise ValueError("lease_until must be later than now")

    conn.execute(
        """UPDATE outbox_events
           SET phase9_state = 'DEAD_LETTER', attempt_limit = %s,
               lease_owner = NULL, lease_expires_at = NULL, acknowledged_at = NULL,
               terminal_reason = 'ATTEMPT_LIMIT_EXHAUSTED'
           WHERE event_id IS NOT NULL
             AND phase9_state IN ('PENDING', 'RETRY_WAIT', 'LEASED')
             AND attempt_count >= %s AND next_attempt_at <= %s
             AND (phase9_state <> 'LEASED' OR lease_expires_at <= %s)""",
        (PHASE9_OUTBOX_MAX_ATTEMPTS_V1, PHASE9_OUTBOX_MAX_ATTEMPTS_V1, now, now),
    )
    rows = conn.execute(
        """WITH candidates AS MATERIALIZED (
               SELECT id
               FROM outbox_events
               WHERE event_id IS NOT NULL
                 AND phase9_state IN ('PENDING', 'RETRY_WAIT', 'LEASED')
                 AND attempt_count < %s AND next_attempt_at <= %s
                 AND (phase9_state <> 'LEASED' OR lease_expires_at <= %s)
               ORDER BY next_attempt_at ASC, created_at ASC, event_id ASC
               LIMIT %s FOR UPDATE SKIP LOCKED
           ), claimed AS (
               UPDATE outbox_events AS event
               SET phase9_state = 'LEASED',
                   attempt_count = event.attempt_count + 1,
                   attempt_limit = COALESCE(event.attempt_limit, %s),
                   lease_owner = %s,
                   lease_expires_at = %s,
                   acknowledged_at = NULL,
                   terminal_reason = NULL
               FROM candidates
               WHERE event.id = candidates.id
               RETURNING event.id, event.event_type, event.event_id, event.payload,
                         event.created_at, event.next_attempt_at
           )
           SELECT id, event_type, event_id, payload
           FROM claimed
           ORDER BY next_attempt_at ASC, created_at ASC, event_id ASC""",
        (
            PHASE9_OUTBOX_MAX_ATTEMPTS_V1,
            now,
            now,
            limit,
            PHASE9_OUTBOX_MAX_ATTEMPTS_V1,
            consumer_name,
            lease_until,
        ),
    ).fetchall()
    events: list[Stage1CandidateEventV1] = []
    for _, event_type, event_id, payload in rows:
        event = _event_from_envelope(payload)
        if event.event_type != event_type or event.event_id != event_id or not _valid_event(event):
            raise ValueError("claimed outbox row contains an invalid Phase 9 event")
        events.append(event)
    return tuple(events)


def _locked_event(conn: Connection, event_id: str) -> tuple[int, str, str | None, datetime | None] | None:
    if not isinstance(event_id, str) or re.fullmatch(r"[0-9a-f]{64}", event_id) is None:
        return None
    return conn.execute(
        """SELECT id, phase9_state, lease_owner, lease_expires_at
           FROM outbox_events WHERE event_id = %s FOR UPDATE""",
        (event_id,),
    ).fetchone()


def ack(conn: Connection, *, event_id: str, consumer_name: str, now: datetime) -> None:
    """Acknowledge an actively leased event; duplicate ack is idempotent."""
    _require_utc(now, "now")
    row = _locked_event(conn, event_id)
    if row is None:
        raise EventNotClaimedError("Phase 9 event is not claimed")
    row_id, state, owner, lease_expires_at = row
    if state == OutboxEventStateV1.ACKNOWLEDGED.value:
        return
    if state != OutboxEventStateV1.LEASED.value:
        raise EventNotClaimedError("Phase 9 event is not in LEASED state")
    if owner != consumer_name:
        raise LeaseOwnershipError("Phase 9 lease belongs to another consumer")
    if lease_expires_at is None or lease_expires_at <= now:
        raise LeaseExpiredError("Phase 9 event lease has expired")
    conn.execute(
        """UPDATE outbox_events SET phase9_state = 'ACKNOWLEDGED', acknowledged_at = %s,
               lease_owner = NULL, lease_expires_at = NULL, terminal_reason = NULL
           WHERE id = %s""",
        (now, row_id),
    )


def release_for_retry(
    conn: Connection,
    *,
    event_id: str,
    consumer_name: str,
    retry_at: datetime,
    error_code: RetryableErrorCodeV1,
    now: datetime,
) -> None:
    """Release an active lease to retry or a terminal dead-letter state."""
    _require_utc(now, "now")
    _require_utc(retry_at, "retry_at")
    if retry_at <= now:
        raise ValueError("retry_at must be later than now")
    try:
        normalized_error = RetryableErrorCodeV1(error_code)
    except (TypeError, ValueError) as exc:
        raise ValueError("error_code is not retryable under Phase 9 V1") from exc

    row = _locked_event(conn, event_id)
    if row is None:
        raise EventNotClaimedError("Phase 9 event is not claimed")
    row_id, state, owner, lease_expires_at = row
    if state == OutboxEventStateV1.ACKNOWLEDGED.value:
        raise AlreadyAcknowledgedError("Phase 9 event is already acknowledged")
    if state != OutboxEventStateV1.LEASED.value:
        raise EventNotClaimedError("Phase 9 event is not in LEASED state")
    if owner != consumer_name:
        raise LeaseOwnershipError("Phase 9 lease belongs to another consumer")
    if lease_expires_at is None or lease_expires_at <= now:
        raise LeaseExpiredError("Phase 9 event lease has expired")

    current_attempt = conn.execute(
        "SELECT attempt_count FROM outbox_events WHERE id = %s", (row_id,)
    ).fetchone()[0]
    if current_attempt >= PHASE9_OUTBOX_MAX_ATTEMPTS_V1:
        conn.execute(
            """UPDATE outbox_events SET phase9_state = 'DEAD_LETTER',
                   attempt_limit = %s, last_error_code = %s,
                   terminal_reason = 'ATTEMPT_LIMIT_EXHAUSTED',
                   lease_owner = NULL, lease_expires_at = NULL, acknowledged_at = NULL
               WHERE id = %s""",
            (PHASE9_OUTBOX_MAX_ATTEMPTS_V1, normalized_error.value, row_id),
        )
        return
    conn.execute(
        """UPDATE outbox_events SET phase9_state = 'RETRY_WAIT',
               attempt_limit = %s, next_attempt_at = %s, last_error_code = %s,
               lease_owner = NULL, lease_expires_at = NULL, acknowledged_at = NULL,
               terminal_reason = NULL
           WHERE id = %s""",
        (PHASE9_OUTBOX_MAX_ATTEMPTS_V1, retry_at, normalized_error.value, row_id),
    )


def persist_stage1_candidate_with_event(
    conn: Connection,
    *,
    run_id: int,
    screening_result: Stage1Result,
    candidate_created_at: datetime,
    candidate_valid_until: datetime | None,
    stage1_policy_version: str,
    source_as_of: datetime,
) -> int:
    """Persist one Stage 1 result and its A/B event atomically on ``conn``."""
    repository = Phase1Repository(conn)
    screening_result_id = repository.insert_stage1_result(run_id, screening_result)
    if screening_result.category not in {"A", "B"}:
        return screening_result_id
    if stage1_policy_version == "QUANT_PAPER_V2" and screening_result.category == "B":
        # V2 B observations remain in the durable screening/waiting projection.
        # Only a later fresh A classification may request deep research.
        return screening_result_id

    instrument = conn.execute(
        """SELECT category, quote_coin, contract_type, status
           FROM symbols WHERE symbol = %s""",
        (screening_result.symbol,),
    ).fetchone()
    if instrument is None:
        raise ValueError("Stage 1 candidate instrument is missing")
    category, quote_coin, contract_type, instrument_status = instrument
    supported_market = (
        category.upper() == "USDT-FUTURES"
        and quote_coin.upper() == "USDT"
        and contract_type.lower() == "perpetual"
    )
    in_scope = supported_market and instrument_status.lower() == "online"
    market = "USDT_PERPETUAL" if supported_market else "UNSUPPORTED"
    event = build_stage1_candidate_event(
        screening_result_id=screening_result_id,
        run_id=run_id,
        screening_result=screening_result,
        market=market,
        instrument_scope={
            "category": category,
            "quote_coin": quote_coin,
            "contract_type": contract_type,
            "status": instrument_status,
            "in_scope": "true" if in_scope else "false",
        },
        candidate_created_at=candidate_created_at,
        candidate_valid_until=candidate_valid_until,
        stage1_policy_version=stage1_policy_version,
        source_as_of=source_as_of,
    )
    Phase9OutboxWriter().emit(conn, event=event)
    return screening_result_id
