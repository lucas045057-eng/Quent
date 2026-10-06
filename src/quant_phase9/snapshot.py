"""Immutable Phase 9 evaluation snapshot construction and persistence."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from psycopg import Connection

from .canonical import canonical_json, canonical_sha256, evaluation_id_for
from .contracts import (
    EvaluationIdentityV1,
    EvaluationSnapshotV1,
    GitSha,
    SourceProjectionV1,
    Stage1CandidateEventV1,
)
from .sources import MAX_SOURCE_ROWS, as_utc, projection_id_for


MAX_SNAPSHOT_BYTES = 256 * 1024
_SNAPSHOT_SCHEMA = "PHASE9_EVALUATION_SNAPSHOT_V1"
_EVIDENCE_SCHEMA = "PHASE9_EVIDENCE_CHAIN_V1"
_MANIFEST_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ApprovedPolicyManifest(Protocol):
    manifest_version: str
    manifest_digest: str


class SnapshotIdentityConflictError(RuntimeError):
    """A fixed evaluation identity was already persisted with different input."""


def _manifest_field(manifest: object, name: str, fallback: str) -> str:
    value = getattr(manifest, name, fallback)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"policy manifest {name} must be a non-empty string")
    return value


def _snapshot_content(snapshot: EvaluationSnapshotV1) -> dict[str, object]:
    return {
        "schema": _SNAPSHOT_SCHEMA,
        "identity": snapshot.identity,
        "evaluation_id": snapshot.evaluation_id,
        "stage1_candidate_id": snapshot.stage1_candidate_id,
        "symbol": snapshot.symbol,
        "market": snapshot.market,
        "timeframe": snapshot.timeframe,
        "evaluation_time": snapshot.evaluation_time,
        "as_of": snapshot.as_of,
        "created_at": snapshot.created_at,
        "candidate_event": snapshot.candidate_event,
        "source_projections": snapshot.source_projections,
        "stage1_policy_version": snapshot.stage1_policy_version,
        "evidence_schema_version": snapshot.evidence_schema_version,
        "freshness_policy_version": snapshot.freshness_policy_version,
        "pattern_policy_version": snapshot.pattern_policy_version,
        "decision_policy_version": snapshot.decision_policy_version,
        "ttl_policy_version": snapshot.ttl_policy_version,
        "code_version": snapshot.code_version,
    }


def _stored_payload(snapshot: EvaluationSnapshotV1) -> dict[str, object]:
    return {**_snapshot_content(snapshot), "snapshot_digest": snapshot.snapshot_digest}


def _validate_projection(
    projection: SourceProjectionV1,
    *,
    evaluation_id: UUID,
    candidate: Stage1CandidateEventV1,
    as_of: datetime,
) -> SourceProjectionV1:
    if not isinstance(projection, SourceProjectionV1):
        raise TypeError("source reader must return SourceProjectionV1 rows")
    if projection.symbol != candidate.symbol or projection.market != candidate.market:
        raise ValueError("source projection symbol/market does not match Stage 1 candidate")
    if canonical_sha256(projection.canonical_payload) != projection.canonical_digest:
        raise ValueError("source projection canonical digest does not match payload")
    for field in ("event_time", "observed_at", "captured_at", "processed_at", "available_at"):
        value = getattr(projection, field)
        if value is not None and value > as_of:
            raise ValueError(f"source projection {field} is later than as_of")
    rebound_id = projection_id_for(evaluation_id, projection.source_ref, str(projection.canonical_digest))
    return replace(projection, evaluation_id=evaluation_id, projection_id=rebound_id)


def _require_repeatable_read(conn: Connection) -> None:
    if conn.autocommit:
        raise ValueError("snapshot requires a caller-owned REPEATABLE READ transaction")
    isolation = conn.execute("SHOW transaction_isolation").fetchone()
    if not isolation or str(isolation[0]).lower() != "repeatable read":
        raise ValueError("snapshot requires a caller-owned REPEATABLE READ transaction")


def build_snapshot(
    conn: Connection,
    *,
    identity: EvaluationIdentityV1,
    stage1_candidate: Stage1CandidateEventV1,
    as_of: datetime,
    source_reader: object,
    policy_manifest: ApprovedPolicyManifest,
    code_version: str,
) -> EvaluationSnapshotV1:
    """Freeze one bounded source view using only the caller's transaction."""
    _require_repeatable_read(conn)
    if not isinstance(identity, EvaluationIdentityV1):
        raise TypeError("identity must be EvaluationIdentityV1")
    if not isinstance(stage1_candidate, Stage1CandidateEventV1):
        raise TypeError("stage1_candidate must be Stage1CandidateEventV1")
    as_of = as_utc(as_of)
    if (identity.stage1_candidate_id != stage1_candidate.stage1_candidate_id
            or identity.symbol != stage1_candidate.symbol
            or identity.market != stage1_candidate.market
            or identity.timeframe not in {"15m", "1H", "4H"}):
        raise ValueError("evaluation identity does not match Stage 1 candidate")
    if identity.evaluation_window_end > as_of:
        raise ValueError("evaluation window is later than as_of")
    if any(value > as_of for value in (
        stage1_candidate.candidate_created_at,
        stage1_candidate.source_as_of,
        stage1_candidate.created_at,
    )):
        raise ValueError("Stage 1 candidate is later than as_of")

    manifest_version = _manifest_field(policy_manifest, "manifest_version", "")
    manifest_digest = _manifest_field(policy_manifest, "manifest_digest", "")
    if _MANIFEST_DIGEST.fullmatch(manifest_digest) is None:
        raise ValueError("policy manifest digest must be a lowercase SHA-256 value")
    if identity.policy_generation != f"{manifest_version}:{manifest_digest}":
        raise ValueError("evaluation identity policy_generation does not bind the approved manifest")

    code_sha = GitSha(code_version)
    reader = getattr(source_reader, "read", None)
    if not callable(reader):
        raise TypeError("source_reader must provide read(conn, candidate, timeframe, as_of)")
    # Do not create another connection or commit: every adapter receives
    # the exact connection participating in this repeatable-read snapshot.
    selected = reader(
        conn,
        candidate=stage1_candidate,
        timeframe=identity.timeframe,
        as_of=as_of,
    )
    if not isinstance(selected, tuple):
        raise TypeError("source reader must return an ordered tuple")
    projections = tuple(
        _validate_projection(
            item,
            evaluation_id=evaluation_id_for(identity),
            candidate=stage1_candidate,
            as_of=as_of,
        )
        for item in selected
    )
    counts: dict[tuple[str, str, str], int] = {}
    for projection in projections:
        interval_key = (
            str(projection.canonical_payload.get("interval"))
            if projection.source_type == "CLOSED_KLINE"
            else ""
        )
        key = (projection.source_phase.value, projection.source_type, interval_key)
        counts[key] = counts.get(key, 0) + 1
        limit = MAX_SOURCE_ROWS + (1 if projection.source_type == "CLOSED_KLINE" else 0)
        if counts[key] > limit:
            raise ValueError("source projection exceeds the bounded per-type row limit")

    version = manifest_version
    snapshot = EvaluationSnapshotV1(
        identity=identity,
        evaluation_id=evaluation_id_for(identity),
        stage1_candidate_id=stage1_candidate.stage1_candidate_id,
        symbol=stage1_candidate.symbol,
        market=stage1_candidate.market,
        timeframe=identity.timeframe,
        evaluation_time=as_of,
        as_of=as_of,
        # Use the logical as-of time so replay is byte-for-byte stable.
        created_at=as_of,
        candidate_event=stage1_candidate,
        source_projections=projections,
        stage1_policy_version=stage1_candidate.stage1_policy_version,
        evidence_schema_version=_manifest_field(
            policy_manifest, "evidence_schema_version", _EVIDENCE_SCHEMA
        ),
        freshness_policy_version=_manifest_field(
            policy_manifest, "freshness_policy_version", version
        ),
        pattern_policy_version=_manifest_field(
            policy_manifest, "pattern_policy_version", version
        ),
        decision_policy_version=_manifest_field(
            policy_manifest, "decision_policy_version", version
        ),
        ttl_policy_version=_manifest_field(policy_manifest, "ttl_policy_version", version),
        code_version=code_sha,
        snapshot_digest="0" * 64,
    )
    digest = canonical_sha256(_snapshot_content(snapshot))
    snapshot = replace(snapshot, snapshot_digest=digest)
    encoded = canonical_json(_stored_payload(snapshot)).encode("utf-8")
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise ValueError("normalized EvaluationSnapshot exceeds 256 KiB")
    return snapshot


def persist_evaluation_snapshot(conn: Connection, *, snapshot: EvaluationSnapshotV1) -> bool:
    """Insert once; identical retries are idempotent and conflicts never update."""
    try:
        _require_repeatable_read(conn)
        return _persist_evaluation_snapshot(conn, snapshot=snapshot)
    except Exception:
        conn.rollback()
        raise


def _persist_evaluation_snapshot(conn: Connection, *, snapshot: EvaluationSnapshotV1) -> bool:
    if not isinstance(snapshot, EvaluationSnapshotV1):
        raise TypeError("snapshot must be EvaluationSnapshotV1")
    payload = _stored_payload(snapshot)
    encoded = canonical_json(payload)
    if len(encoded.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ValueError("normalized EvaluationSnapshot exceeds 256 KiB")
    expected_digest = canonical_sha256(_snapshot_content(snapshot))
    if expected_digest != snapshot.snapshot_digest:
        raise ValueError("snapshot digest does not match canonical content")
    inserted = conn.execute(
        """INSERT INTO phase9_evaluation_snapshots
               (evaluation_id, snapshot_digest, as_of, payload, code_version, created_at)
             VALUES (%s, %s, %s, %s::jsonb, %s, %s)
             ON CONFLICT (evaluation_id) DO NOTHING""",
        (
            snapshot.evaluation_id,
            str(snapshot.snapshot_digest),
            snapshot.as_of,
            encoded,
            str(snapshot.code_version),
            snapshot.created_at,
        ),
    )
    stored = conn.execute(
        """SELECT snapshot_digest, payload
             FROM phase9_evaluation_snapshots
            WHERE evaluation_id = %s""",
        (snapshot.evaluation_id,),
    ).fetchone()
    if stored is None:
        raise RuntimeError("evaluation snapshot insert did not produce a durable row")
    stored_digest, stored_payload = stored
    expected_payload = json.loads(encoded)
    if str(stored_digest) != str(snapshot.snapshot_digest) or canonical_json(stored_payload) != canonical_json(expected_payload):
        raise SnapshotIdentityConflictError(
            "evaluation identity already has a different immutable snapshot"
        )
    return inserted.rowcount == 1
