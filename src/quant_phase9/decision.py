"""Pure Phase 9 decision policy and deterministic append-only status events."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid5

from .canonical import canonical_json, canonical_sha256
from quant_data_layer.freshness import FRESHNESS_POLICY
from quant_phase1.freshness import INTERVAL_SECONDS
from quant_phase1.stage1 import DEFAULT_GRACE_SECONDS

from .contracts import (
    ConfidenceBandV1, DecisionCandidateV1, DecisionDirectionBiasV1,
    DecisionStatusEventV1, EvaluationSnapshotV1, EvidenceChainV1,
    JevConflictSeverityV1, JevRelationV1,
    JevReviewStatusV1, JevReviewV1, PatternMatchStatusV1, PatternMatchV1,
    PolicyDirectionV1, Sha256Hex,
)
from .policy import ApprovedPolicyManifestV1, policy_for
from .sources import as_utc


def input_snapshot_hash_for(
    *, snapshot_hash: Sha256Hex | str, jev_review: JevReviewV1 | None,
) -> Sha256Hex:
    frozen = Sha256Hex(snapshot_hash)
    if jev_review is None:
        review: object = {"status": "NO_REVIEW"}
    elif jev_review.status is JevReviewStatusV1.COMPLETED:
        review = {
            "status": "COMPLETED", "review_id": jev_review.review_id,
            "response_digest": jev_review.response_digest,
        }
    else:
        review = {
            "status": "REVIEW_NOT_AVAILABLE", "review_id": jev_review.review_id,
            "review_status": jev_review.status, "cause": jev_review.reason_code,
        }
    return canonical_sha256({
        "schema": "PHASE9_INPUT_SNAPSHOT_HASH_V1",
        "evaluation_snapshot_hash": frozen, "review": review,
    })


def _select_pattern(
    matches: tuple[PatternMatchV1, ...], manifest: ApprovedPolicyManifestV1,
    timeframe: str,
) -> PatternMatchV1 | None:
    matched = tuple(match for match in matches
                    if match.status is PatternMatchStatusV1.MATCHED
                    and policy_for(
                        manifest=manifest, pattern_type=match.pattern_type,
                        timeframe=timeframe, direction=match.direction,
                    ) is not None)
    if len(matched) != 1:
        return None
    selected = matched[0]
    if any(match.status is PatternMatchStatusV1.MATCHED
           and match.direction is not selected.direction for match in matches):
        return None
    return selected


def _fallback_status(matches: tuple[PatternMatchV1, ...]) -> PatternMatchStatusV1:
    for status in (
        PatternMatchStatusV1.CONFLICTED, PatternMatchStatusV1.PARTIAL_MATCH,
        PatternMatchStatusV1.NOT_MATCHED, PatternMatchStatusV1.NOT_CONFIGURED,
    ):
        if any(match.status is status for match in matches):
            return status
    return PatternMatchStatusV1.NOT_CONFIGURED


def _freshness_rule_age_limit(rule, *, timeframe: str | None = None) -> float:
    maximum_age = float(rule.maximum_age_seconds)
    source_type = rule.source_type
    if source_type in {"STAGE1_CANDIDATE", "PRICE_TICKER", "PRICE_OBSERVATION"}:
        hard_limit = FRESHNESS_POLICY["PRICE_DECISION"].hard_seconds
    elif source_type == "CLOSED_KLINE":
        interval = timeframe if timeframe in INTERVAL_SECONDS else "15m"
        hard_limit = INTERVAL_SECONDS[interval] + DEFAULT_GRACE_SECONDS.get(interval, 0)
    elif source_type in {"OPEN_INTEREST", "FUNDING_RATE", "DERIVATIVE_SUMMARY"}:
        hard_limit = FRESHNESS_POLICY["OPEN_INTEREST"].hard_seconds
    elif source_type in {"TRADE_FLOW_WINDOW", "CVD_SNAPSHOT", "CROSS_EXCHANGE_FLOW", "TRADE_GAP"}:
        interval = timeframe if timeframe in INTERVAL_SECONDS else "5m"
        hard_limit = INTERVAL_SECONDS[interval] + 5
    elif source_type in {"LIQUIDATION_EVENT", "LIQUIDATION_HEALTH"}:
        hard_limit = FRESHNESS_POLICY["LIQUIDATION"].hard_seconds
    else:
        return maximum_age
    return min(maximum_age, float(hard_limit))


def build_status_event(
    *, decision: DecisionCandidateV1, status: str, event_time: datetime,
    reason_code: str, supersedes_decision_id: UUID | None = None,
) -> DecisionStatusEventV1:
    if status not in {"ACTIVE", "EXPIRED", "SUPERSEDED", "INVALIDATED"}:
        raise ValueError("unsupported decision status transition")
    event_time = as_utc(event_time)
    if status == "EXPIRED" and event_time < decision.valid_until:
        raise ValueError("expiry event cannot precede valid_until")
    if status == "SUPERSEDED" and supersedes_decision_id is None:
        raise ValueError("supersede event requires replacement decision ID")
    if not reason_code:
        raise ValueError("status reason_code is required")
    event_id = uuid5(NAMESPACE_URL, canonical_json({
        "decision_id": decision.decision_id, "status": status,
        "reason_code": reason_code,
        "supersedes_decision_id": supersedes_decision_id,
    }))
    return DecisionStatusEventV1(
        event_id=event_id, decision_id=decision.decision_id,
        evaluation_id=decision.evaluation_id, status=status,
        event_time=event_time, reason_code=reason_code,
        supersedes_decision_id=supersedes_decision_id,
    )


def build_decision_candidate(
    *, snapshot: EvaluationSnapshotV1, evidence_chain: EvidenceChainV1,
    pattern_matches: Sequence[PatternMatchV1],
    jev_review: JevReviewV1 | None,
    policy_manifest: ApprovedPolicyManifestV1,
    now: datetime,
) -> tuple[DecisionCandidateV1, DecisionStatusEventV1]:
    if not isinstance(snapshot, EvaluationSnapshotV1):
        raise TypeError("snapshot must be EvaluationSnapshotV1")
    if not isinstance(evidence_chain, EvidenceChainV1):
        raise TypeError("evidence_chain must be EvidenceChainV1")
    if not isinstance(policy_manifest, ApprovedPolicyManifestV1):
        raise TypeError("decision requires an approved policy manifest")
    from strategies.integration.policy_manifest import assert_legacy_producer
    assert_legacy_producer(policy_manifest)
    now = as_utc(now)
    if snapshot.identity.policy_generation != (
        f"{policy_manifest.manifest_version}:{policy_manifest.manifest_digest}"
    ):
        raise ValueError("snapshot policy generation mismatch")
    if snapshot.code_version != policy_manifest.code_version:
        raise ValueError("snapshot code version mismatch")
    if (
        evidence_chain.evaluation_id != snapshot.evaluation_id
        or evidence_chain.stage1_candidate_id != snapshot.stage1_candidate_id
        or evidence_chain.symbol != snapshot.symbol
        or evidence_chain.timeframe != snapshot.timeframe
        or evidence_chain.evaluation_snapshot_hash != snapshot.snapshot_digest
        or evidence_chain.evidence_schema_version != snapshot.evidence_schema_version
    ):
        raise ValueError("evidence chain identity/snapshot hash mismatch")
    if jev_review is not None and jev_review.evaluation_id != snapshot.evaluation_id:
        raise ValueError("Jev review evaluation ID mismatch")
    expected_hash = input_snapshot_hash_for(
        snapshot_hash=snapshot.snapshot_digest, jev_review=jev_review,
    )
    if evidence_chain.input_snapshot_hash != expected_hash:
        raise ValueError("final input snapshot hash mismatch")
    matches = tuple(pattern_matches)
    if any(not isinstance(match, PatternMatchV1)
           or match.evaluation_id != snapshot.evaluation_id
           or match.pattern_policy_version != snapshot.pattern_policy_version
           for match in matches):
        raise ValueError("pattern match identity/version mismatch")
    keys = {(m.pattern_type, m.direction) for m in matches}
    if len(keys) != len(matches):
        raise ValueError("duplicate directional pattern match")
    selected = _select_pattern(matches, policy_manifest, snapshot.timeframe)
    status = (PatternMatchStatusV1.MATCHED if selected is not None
              else _fallback_status(matches))
    reasons: list[str] = []
    vetoes = list(evidence_chain.hard_vetoes)
    if selected is None:
        reasons.append(
            "PATTERN_NOT_CONFIGURED" if status is PatternMatchStatusV1.NOT_CONFIGURED
            else f"PATTERN_{status.value}"
        )
    if vetoes:
        reasons.append("HARD_VETO")
    if evidence_chain.missing:
        reasons.append("MISSING_EVIDENCE")
    if evidence_chain.degraded:
        reasons.append("DEGRADED_EVIDENCE")
    if evidence_chain.jev_review_required and (
        jev_review is None or jev_review.status is not JevReviewStatusV1.COMPLETED
    ):
        reasons.append("UNRESOLVED_HIGH_CONFLICT")
    if (jev_review is not None
            and jev_review.status is JevReviewStatusV1.COMPLETED
            and jev_review.conflict_severity is JevConflictSeverityV1.HIGH
            and jev_review.relation in {
                JevRelationV1.CONFLICTING, JevRelationV1.HIGHLY_CONFLICTING,
            }):
        reasons.append("JEV_HIGH_CONFLICT")
    candidate_expiry = snapshot.candidate_event.candidate_valid_until
    if candidate_expiry is None or now >= candidate_expiry:
        reasons.append("STAGE1_EXPIRED")
    if now < snapshot.as_of:
        raise ValueError("decision clock cannot precede immutable snapshot")
    if selected is not None:
        policy = policy_for(
            manifest=policy_manifest, pattern_type=selected.pattern_type,
            timeframe=snapshot.timeframe, direction=selected.direction,
        )
        assert policy is not None
        confidence_ceiling = policy.confidence_ceiling
        ttl = next(
            (rule for rule in policy_manifest.manifest.policy_content.ttl_rules
             if rule.rule_id == policy.ttl_rule_id
             and rule.pattern_type == selected.pattern_type
             and rule.timeframe == snapshot.timeframe),
            None,
        )
        if ttl is None:
            raise ValueError("matched pattern has no approved TTL rule")
        valid_until = snapshot.evaluation_time + timedelta(seconds=ttl.ttl_seconds)
        if candidate_expiry is not None:
            valid_until = min(valid_until, candidate_expiry)
    else:
        confidence_ceiling = ConfidenceBandV1.INSUFFICIENT
        valid_until = candidate_expiry or now
    if now >= valid_until:
        reasons.append("DECISION_EXPIRED")

    core_stale = False
    if selected is not None:
        rules = tuple(rule for rule in policy_manifest.manifest.policy_content.freshness_rules
                      if rule.rule_id in policy.freshness_rule_ids and rule.required)
        for rule in rules:
            if rule.source_type == 'STAGE1_CANDIDATE':
                times = (snapshot.candidate_event.source_as_of,)
            else:
                from .features import _timestamp
                sources = tuple(source for source in snapshot.source_projections
                    if source.source_phase==rule.source_phase and source.source_type==rule.source_type)
                times = tuple(_timestamp(source.canonical_payload.get('bar_close_timestamp')
                    or source.canonical_payload.get('window_close') or source.canonical_payload.get('window_end')
                    or source.event_time or source.observed_at) for source in sources)
            available = tuple(value for value in times if value is not None and value<=now)
            if not available or (now-max(available)).total_seconds()>_freshness_rule_age_limit(rule, timeframe=snapshot.timeframe):
                core_stale = True
        if core_stale:
            reasons.append('CORE_INPUT_STALE')
    from .paper_v1 import is_paper_v1, PROFILE
    if is_paper_v1(policy_manifest):
        if confidence_ceiling is not ConfidenceBandV1.HIGH:
            reasons.append("PAPER_V1_HIGH_REQUIRED")
        valid_until = min(valid_until, snapshot.evaluation_time + timedelta(seconds=PROFILE.decision_ttl_seconds))
    has_degradation = bool(evidence_chain.degraded or evidence_chain.missing)
    eligible = (
        selected is not None
        and confidence_ceiling in {ConfidenceBandV1.HIGH, ConfidenceBandV1.MEDIUM}
        and not vetoes and not has_degradation and not core_stale
        and "PAPER_V1_HIGH_REQUIRED" not in reasons
        and "UNRESOLVED_HIGH_CONFLICT" not in reasons
        and "JEV_HIGH_CONFLICT" not in reasons
        and "STAGE1_EXPIRED" not in reasons
        and "DECISION_EXPIRED" not in reasons
    )
    if selected is None:
        direction = DecisionDirectionBiasV1.NEUTRAL
        band = ConfidenceBandV1.INSUFFICIENT
    else:
        direction = (
            DecisionDirectionBiasV1.BULLISH
            if selected.direction is PolicyDirectionV1.LONG
            else DecisionDirectionBiasV1.BEARISH
        )
        band = (
            confidence_ceiling if eligible
            else ConfidenceBandV1.INSUFFICIENT if vetoes or core_stale or "UNRESOLVED_HIGH_CONFLICT" in reasons or "JEV_HIGH_CONFLICT" in reasons
            else ConfidenceBandV1.INSUFFICIENT if confidence_ceiling is ConfidenceBandV1.INSUFFICIENT
            else ConfidenceBandV1.LOW
        )
    reasons = list(dict.fromkeys(reasons))
    if eligible:
        reasons.append("APPROVED_PATTERN_MATCH")
    decision_id = uuid5(NAMESPACE_URL, canonical_json({
        "evaluation_id": snapshot.evaluation_id,
        "input_snapshot_hash": expected_hash,
        "pattern_type": selected.pattern_type if selected else None,
        "pattern_status": status,
        "eligible": eligible,
        "reason_codes": tuple(reasons),
    }))
    decision = DecisionCandidateV1(
        decision_id=decision_id, evaluation_id=snapshot.evaluation_id,
        stage1_candidate_id=snapshot.stage1_candidate_id,
        symbol=snapshot.symbol, market=snapshot.market,
        timeframe=snapshot.timeframe, created_at=now,
        valid_until=valid_until, eligible=eligible,
        direction_bias=direction, confidence_band=band,
        matched_pattern=selected.pattern_type if selected else None,
        pattern_status=status,
        supporting_evidence_ids=evidence_chain.supporting,
        conflicting_evidence_ids=evidence_chain.conflicting,
        degraded_evidence_ids=evidence_chain.degraded,
        missing_evidence=evidence_chain.missing,
        veto_reasons=tuple(vetoes),
        jev_review_id=jev_review.review_id if jev_review is not None else None,
        reason_codes=tuple(reasons),
        short_summary=(
            f"{status.value}: {selected.pattern_type if selected else 'no approved match'}; "
            f"eligible={eligible}"
        ),
        input_snapshot_hash=expected_hash,
        evidence_schema_version=snapshot.evidence_schema_version,
        pattern_policy_version=snapshot.pattern_policy_version,
        freshness_policy_version=snapshot.freshness_policy_version,
        decision_policy_version=snapshot.decision_policy_version,
        ttl_policy_version=snapshot.ttl_policy_version,
        prompt_version=jev_review.prompt_version if jev_review is not None else "NONE",
        code_version=snapshot.code_version, supersedes_decision_id=None,
    )
    event = build_status_event(
        decision=decision, status="ACTIVE", event_time=now,
        reason_code="DECISION_CREATED",
    )
    return decision, event
