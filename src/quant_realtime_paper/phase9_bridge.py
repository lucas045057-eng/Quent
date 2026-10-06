"""Durable, identity-checked read bridge for completed Phase 9 outputs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from quant_phase9.canonical import canonical_sha256
from quant_phase9.contracts import DecisionCandidateV1, EvaluationSnapshotV1, EvidenceFreshnessV1
from quant_phase9.lifecycle import load_decision
from quant_phase9.runtime import _load_snapshot
from quant_phase9.snapshot import _snapshot_content


@dataclass(frozen=True, slots=True)
class Phase9BridgeResultV1:
    reason_code: str
    snapshot: EvaluationSnapshotV1 | None
    candidate: DecisionCandidateV1 | None
    effective_status: str | None

    @property
    def ready(self) -> bool:
        return (
            self.snapshot is not None
            and self.candidate is not None
            and self.effective_status == "ACTIVE"
        )


def _blocked(reason_code: str) -> Phase9BridgeResultV1:
    return Phase9BridgeResultV1(reason_code, None, None, None)


def validate_phase9_projection(
    *,
    evaluation_state: str,
    intake_disposition: str,
    active: bool,
    lifecycle_status: str | None,
    snapshot: EvaluationSnapshotV1 | None,
    candidate: DecisionCandidateV1 | None,
    expected_stage1_candidate_id: int,
    expected_policy_generation: str,
    expected_symbol: str,
    expected_timeframe: str,
    expected_build_revision: str,
    evidence_snapshot_hash: str | None,
    evidence_input_snapshot_hash: str | None,
    now: datetime,
    approved_policy=None,
) -> Phase9BridgeResultV1:
    """Expose only a complete, active, unexpired and fully linked persisted result."""
    if (now.tzinfo is None or now.utcoffset() is None
            or now.utcoffset() != timezone.utc.utcoffset(now)):
        return _blocked("INVALID_RUNTIME_TIME")
    if evaluation_state != "COMPLETED":
        return _blocked("PHASE9_NOT_COMPLETE")
    if intake_disposition != "ADMITTED":
        return _blocked("PHASE9_INTAKE_NOT_ADMITTED")
    if not active or lifecycle_status != "ACTIVE":
        return _blocked("PHASE9_NOT_ACTIVE")
    if not isinstance(snapshot, EvaluationSnapshotV1) or not isinstance(candidate, DecisionCandidateV1):
        return _blocked("PHASE9_DURABLE_RECORD_INVALID")

    if (
        snapshot.stage1_candidate_id != expected_stage1_candidate_id
        or snapshot.identity.stage1_candidate_id != expected_stage1_candidate_id
        or snapshot.identity.policy_generation != expected_policy_generation
        or snapshot.symbol != expected_symbol
        or snapshot.identity.symbol != expected_symbol
        or snapshot.timeframe != expected_timeframe
        or snapshot.identity.timeframe != expected_timeframe
        or candidate.stage1_candidate_id != expected_stage1_candidate_id
        or candidate.symbol != expected_symbol
        or candidate.timeframe != expected_timeframe
        or candidate.evaluation_id != snapshot.evaluation_id
        or candidate.market != snapshot.market
        or snapshot.candidate_event.stage1_candidate_id != expected_stage1_candidate_id
        or snapshot.candidate_event.symbol != expected_symbol
        or snapshot.candidate_event.market != snapshot.market
    ):
        return _blocked("PHASE9_IDENTITY_MISMATCH")

    expected_revision = expected_build_revision
    if (
        str(snapshot.code_version) != expected_revision
        or str(candidate.code_version) != expected_revision
    ):
        return _blocked("BUILD_REVISION_MISMATCH")

    try:
        if canonical_sha256(_snapshot_content(snapshot)) != snapshot.snapshot_digest:
            return _blocked("PHASE9_SNAPSHOT_INVALID")
    except (TypeError, ValueError):
        return _blocked("PHASE9_SNAPSHOT_INVALID")
    if (
        evidence_snapshot_hash != str(snapshot.snapshot_digest)
        or evidence_input_snapshot_hash != str(candidate.input_snapshot_hash)
    ):
        return _blocked("PHASE9_EVIDENCE_LINK_INVALID")
    if any(
        source.evaluation_id != snapshot.evaluation_id
        or source.symbol != snapshot.symbol
        or source.market != snapshot.market
        or canonical_sha256(source.canonical_payload) != source.canonical_digest
        for source in snapshot.source_projections
    ):
        return _blocked("PHASE9_EVIDENCE_LINK_INVALID")
    from quant_phase9.paper_v1 import is_paper_v1, CORE_SOURCE_TYPES
    scoped = approved_policy is not None and is_paper_v1(approved_policy)
    if any(
        source.freshness_status in {EvidenceFreshnessV1.STALE, EvidenceFreshnessV1.UNKNOWN}
        for source in snapshot.source_projections
        if not scoped or source.source_type in CORE_SOURCE_TYPES
    ):
        return _blocked("STALE_SOURCE_DATA")
    if snapshot.as_of > now or snapshot.candidate_event.candidate_created_at > now:
        return _blocked("PHASE9_SNAPSHOT_FROM_FUTURE")
    if snapshot.candidate_event.candidate_valid_until is None:
        return _blocked("INTAKE_TTL_NOT_CONFIGURED")
    if snapshot.candidate_event.candidate_valid_until <= now:
        return _blocked("STAGE1_CANDIDATE_EXPIRED")
    if candidate.valid_until <= now:
        return _blocked("PATTERN_DECISION_TTL_EXPIRED")

    return Phase9BridgeResultV1("PHASE9_ACTIVE", snapshot, candidate, "ACTIVE")


_PHASE9_READER_SQL = """
SELECT e.evaluation_state,
       e.intake_disposition,
       COALESCE(c.active, FALSE),
       (SELECT status FROM phase9_decision_status_events s
          WHERE s.decision_id = d.decision_id
          ORDER BY s.event_time DESC, s.created_at DESC, s.event_id DESC LIMIT 1),
       snap.payload,
       d.payload,
       chain.evaluation_snapshot_hash,
       chain.input_snapshot_hash
  FROM phase9_evaluations e
  LEFT JOIN phase9_evaluation_snapshots snap ON snap.evaluation_id = e.evaluation_id
  LEFT JOIN phase9_decision_candidates d ON d.evaluation_id = e.evaluation_id
  LEFT JOIN phase9_evidence_chains chain ON chain.evaluation_id = e.evaluation_id
  LEFT JOIN phase9_revalidation_cursors c
    ON c.stage1_candidate_id = e.stage1_candidate_id
   AND c.timeframe = e.timeframe
   AND c.policy_generation = e.policy_generation
 WHERE e.stage1_candidate_id = %s
   AND e.symbol = %s
   AND e.timeframe = %s
   AND e.policy_generation = %s
 ORDER BY e.created_at DESC, e.updated_at DESC, e.evaluation_id DESC
 LIMIT 1
"""


def load_active_phase9_projection(
    connection: Any,
    *,
    stage1_candidate_id: int,
    policy_generation: str,
    symbol: str,
    timeframe: str,
    build_revision: str,
    now: datetime,
    approved_policy=None,
) -> Phase9BridgeResultV1:
    """Read only durable Phase 9 tables; never evaluates or synthesizes a candidate."""
    row = connection.execute(
        _PHASE9_READER_SQL,
        (stage1_candidate_id, symbol, timeframe, policy_generation),
    ).fetchone()
    if row is None:
        return _blocked("PHASE9_RESULT_NOT_FOUND")
    (
        state, disposition, active, status, snapshot_payload, candidate_payload,
        chain_snapshot_hash, chain_input_hash,
    ) = row
    if snapshot_payload is None or candidate_payload is None:
        return validate_phase9_projection(
            evaluation_state=state,
            intake_disposition=disposition,
            active=active,
            lifecycle_status=status,
            snapshot=None,
            candidate=None,
            expected_stage1_candidate_id=stage1_candidate_id,
            expected_policy_generation=policy_generation,
            expected_symbol=symbol,
            expected_timeframe=timeframe,
            expected_build_revision=build_revision,
            evidence_snapshot_hash=chain_snapshot_hash,
            evidence_input_snapshot_hash=chain_input_hash,
            now=now, approved_policy=approved_policy,
        )
    try:
        snapshot = _load_snapshot(snapshot_payload)
        candidate = load_decision(candidate_payload)
    except (KeyError, TypeError, ValueError, AttributeError):
        return _blocked("PHASE9_DURABLE_RECORD_INVALID")
    return validate_phase9_projection(
        evaluation_state=state,
        intake_disposition=disposition,
        active=active,
        lifecycle_status=status,
        snapshot=snapshot,
        candidate=candidate,
        expected_stage1_candidate_id=stage1_candidate_id,
        expected_policy_generation=policy_generation,
        expected_symbol=symbol,
        expected_timeframe=timeframe,
        expected_build_revision=build_revision,
        evidence_snapshot_hash=chain_snapshot_hash,
        evidence_input_snapshot_hash=chain_input_hash,
        now=now, approved_policy=approved_policy,
    )
