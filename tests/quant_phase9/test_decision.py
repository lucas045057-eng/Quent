from __future__ import annotations

from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from decimal import Decimal

import json
import pytest

from quant_phase9.canonical import canonical_sha256
from quant_phase9.contracts import (
    ConfidenceBandV1, DecisionCandidateV1, DecisionDirectionBiasV1,
    EvaluationIdentityV1, EvaluationSnapshotV1, EvidenceChainV1,
    JevConflictSeverityV1, JevDominantContextV1, JevRelationV1,
    JevReviewStatusV1, JevReviewV1, PatternMatchStatusV1,
    PatternMatchV1, PolicyDirectionV1, Stage1CandidateEventV1,
)
from quant_phase9.decision import build_decision_candidate, build_status_event, input_snapshot_hash_for
from quant_phase9.policy import PolicyContentV1, load_approved_policy_manifest


NOW = datetime(2026, 9, 28, 0, 15, tzinfo=timezone.utc)
EVAL_ID = uuid4()


def _policy(tmp_path, *, enabled):
    content = {name: [] for name in PolicyContentV1.model_fields}
    if enabled:
        content["predicates"] = [{
            "predicate_id": "setup", "source_phase": "PHASE1",
            "source_type": "STAGE1_CANDIDATE", "evidence_type": "PRICE_STRUCTURE",
            "accepted_semantic_codes": ["TREND_UP"], "operator": "PRESENT",
            "threshold": None, "upper_threshold": None, "unit": None,
            "missing_behavior": "FAIL_CLOSED",
        }]
        content["ttl_rules"] = [{
            "rule_id": "ttl", "pattern_type": "TREND_CONTINUATION",
            "timeframe": "1H", "ttl_seconds": 2700,
        }]
        content["revalidation_rules"] = [{
            "rule_id": "revalidate", "pattern_type": "TREND_CONTINUATION",
            "timeframe": "1H", "cadence_seconds": 600, "max_runs_per_minute": 3,
        }]
        content["enabled_patterns"] = [{
            "pattern_type": "TREND_CONTINUATION", "timeframe": "1H",
            "direction": "LONG", "required_predicates": ["setup"],
            "supporting_predicates": [], "contradicting_predicates": [],
            "hard_conflict_predicates": [], "freshness_rule_ids": [],
            "coverage_rule_ids": [], "confidence_ceiling": "HIGH",
            "jev_required_conflict_classes": [], "ttl_rule_id": "ttl",
            "revalidation_rule_id": "revalidate", "approval_reference": "fixture",
        }]
    digest = str(canonical_sha256(content))
    manifest = {
        "schema": "PHASE9_POLICY_MANIFEST_V1", "manifest_version": "1.0.0",
        "created_at": "2026-09-28T00:00:00Z", "policy_content": content,
        "manifest_digest": digest,
    }
    approval = {
        "schema": "PHASE9_POLICY_APPROVAL_V1", "manifest_version": "1.0.0",
        "manifest_digest": digest, "approval_status": "APPROVED",
        "approved_at": "2026-09-28T00:01:00Z",
        "approved_by": "fixture-human", "approved_commit": "b" * 40,
    }
    manifest_path = tmp_path / "manifest.json"
    approval_path = tmp_path / "approval.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    approval_path.write_text(json.dumps(approval), encoding="utf-8")
    return load_approved_policy_manifest(
        manifest_path, approval_path, expected_commit="b" * 40,
    )


def _snapshot(policy):
    candidate = Stage1CandidateEventV1(
        event_id="a" * 64, event_type="phase9.stage1_candidate",
        event_schema_version="PHASE9_STAGE1_CANDIDATE_EVENT_V1",
        stage1_candidate_id=91, screening_result_id=91, symbol="BTCUSDT",
        market="USDT_PERPETUAL", candidate_created_at=NOW - timedelta(minutes=1),
        candidate_valid_until=NOW + timedelta(minutes=20),
        stage1_policy_version="phase1-basic-v1",
        source_as_of=NOW - timedelta(minutes=1),
        source_refs=("fixture:price",), canonical_payload_digest="c" * 64,
        created_at=NOW - timedelta(minutes=1),
        canonical_payload={"stage1_projection": {"status": "AVAILABLE", "structure": "BULLISH"}},
    )
    identity = EvaluationIdentityV1(
        stage1_candidate_id=91, market="USDT_PERPETUAL", symbol="BTCUSDT",
        timeframe="1H", evaluation_window_start=NOW - timedelta(hours=1),
        evaluation_window_end=NOW,
        policy_generation=f"{policy.manifest_version}:{policy.manifest_digest}",
        material_change_generation="initial",
    )
    return EvaluationSnapshotV1(
        identity=identity, evaluation_id=EVAL_ID, stage1_candidate_id=91,
        symbol="BTCUSDT", market="USDT_PERPETUAL", timeframe="1H",
        evaluation_time=NOW, as_of=NOW, created_at=NOW,
        candidate_event=candidate, source_projections=(),
        stage1_policy_version="phase1-basic-v1",
        evidence_schema_version="PHASE9_EVIDENCE_CHAIN_V1",
        freshness_policy_version="1.0.0", pattern_policy_version="1.0.0",
        decision_policy_version="1.0.0", ttl_policy_version="1.0.0",
        code_version="b" * 40, snapshot_digest="d" * 64,
    )


def _chain(snapshot, review=None, *, vetoes=(), degraded=(), review_required=False):
    return EvidenceChainV1(
        stage1_candidate_id=91, symbol="BTCUSDT", evaluation_id=EVAL_ID,
        evaluation_time=NOW, timeframe="1H", supporting=(),
        conflicting=(), neutral=(), missing=(), degraded=degraded,
        hard_vetoes=vetoes, matched_patterns=(), jev_review_required=review_required,
        evidence_schema_version=snapshot.evidence_schema_version,
        evaluation_snapshot_hash=snapshot.snapshot_digest,
        input_snapshot_hash=input_snapshot_hash_for(
            snapshot_hash=snapshot.snapshot_digest, jev_review=review,
        ),
    )


def _pattern(status):
    return PatternMatchV1(
        pattern_match_id=uuid4(), evaluation_id=EVAL_ID,
        pattern_type="TREND_CONTINUATION", direction=PolicyDirectionV1.LONG,
        status=status, required_evidence_ids=(), supporting_evidence_ids=(),
        conflicting_evidence_ids=(), missing=(), vetoes=(),
        pattern_policy_version="1.0.0",
    )


def test_no_reviewed_pattern_is_ineligible_and_has_exact_fields(tmp_path):
    policy = _policy(tmp_path, enabled=False)
    snapshot = _snapshot(policy)
    candidate, event = build_decision_candidate(
        snapshot=snapshot, evidence_chain=_chain(snapshot),
        pattern_matches=(_pattern(PatternMatchStatusV1.NOT_CONFIGURED),),
        jev_review=None, policy_manifest=policy, now=NOW,
    )
    assert candidate.eligible is False
    assert candidate.pattern_status is PatternMatchStatusV1.NOT_CONFIGURED
    assert candidate.direction_bias is DecisionDirectionBiasV1.NEUTRAL
    assert candidate.confidence_band is ConfidenceBandV1.INSUFFICIENT
    assert event.status == "ACTIVE"
    assert {f.name for f in fields(candidate)} == {f.name for f in fields(DecisionCandidateV1)}
    assert not {"entry_price", "stop_loss", "take_profit", "leverage", "order_id"} & {
        f.name for f in fields(candidate)
    }


def test_matched_pattern_uses_approved_ttl_but_veto_and_expiry_close(tmp_path):
    policy = _policy(tmp_path, enabled=True)
    snapshot = _snapshot(policy)
    matched = _pattern(PatternMatchStatusV1.MATCHED)
    candidate, event = build_decision_candidate(
        snapshot=snapshot, evidence_chain=_chain(snapshot),
        pattern_matches=(matched,), jev_review=None,
        policy_manifest=policy, now=NOW,
    )
    assert candidate.eligible is True
    assert candidate.valid_until == NOW + timedelta(minutes=20)
    assert candidate.direction_bias is DecisionDirectionBiasV1.BULLISH
    assert candidate.confidence_band is ConfidenceBandV1.HIGH
    assert event.status == "ACTIVE"
    blocked, _ = build_decision_candidate(
        snapshot=snapshot,
        evidence_chain=_chain(snapshot, vetoes=("CORE_MARKET_DATA_INVALID",)),
        pattern_matches=(matched,), jev_review=None,
        policy_manifest=policy, now=NOW,
    )
    assert blocked.eligible is False
    assert blocked.confidence_band is ConfidenceBandV1.INSUFFICIENT
    late, _ = build_decision_candidate(
        snapshot=snapshot, evidence_chain=_chain(snapshot),
        pattern_matches=(matched,), jev_review=None,
        policy_manifest=policy, now=NOW + timedelta(minutes=21),
    )
    assert late.eligible is False
    assert "STAGE1_EXPIRED" in late.reason_codes


def test_chain_hash_mismatch_rejected(tmp_path):
    policy = _policy(tmp_path, enabled=True)
    snapshot = _snapshot(policy)
    bad = replace(_chain(snapshot), input_snapshot_hash="f" * 64)
    with pytest.raises(ValueError, match="hash"):
        build_decision_candidate(
            snapshot=snapshot, evidence_chain=bad,
            pattern_matches=(_pattern(PatternMatchStatusV1.MATCHED),),
            jev_review=None, policy_manifest=policy, now=NOW,
        )

def test_required_jev_unavailable_fails_closed(tmp_path):
    policy = _policy(tmp_path, enabled=True)
    snapshot = _snapshot(policy)
    review = JevReviewV1(
        evaluation_id=EVAL_ID, review_id=uuid4(),
        status=JevReviewStatusV1.NOT_CONFIGURED,
        reason_code="PROVIDER_NOT_CONFIGURED", reason_detail=None,
        evidence_chain_id=uuid4(), request_digest="a" * 64,
        response_digest=None, provider=None, model=None, model_version=None,
        prompt_version="PHASE9_JEV_PROMPT_V1",
        relation=None, conflict_severity=None, dominant_context=None,
        supporting_assessments=(), conflicting_assessments=(),
        unresolved_conflicts=(), degradation_notes=(),
        referenced_evidence_ids=(), reasoning_summary=None, created_at=NOW,
    )
    decision, _ = build_decision_candidate(
        snapshot=snapshot,
        evidence_chain=_chain(snapshot, review, review_required=True),
        pattern_matches=(_pattern(PatternMatchStatusV1.MATCHED),),
        jev_review=review, policy_manifest=policy, now=NOW,
    )
    assert decision.eligible is False
    assert decision.confidence_band is ConfidenceBandV1.INSUFFICIENT
    assert "UNRESOLVED_HIGH_CONFLICT" in decision.reason_codes


def test_status_events_are_append_only_and_idempotent(tmp_path):
    policy = _policy(tmp_path, enabled=True)
    snapshot = _snapshot(policy)
    decision, _ = build_decision_candidate(
        snapshot=snapshot, evidence_chain=_chain(snapshot),
        pattern_matches=(_pattern(PatternMatchStatusV1.MATCHED),),
        jev_review=None, policy_manifest=policy, now=NOW,
    )
    expired_at = decision.valid_until
    first = build_status_event(
        decision=decision, status="EXPIRED",
        event_time=expired_at, reason_code="TTL_EXPIRED",
    )
    second = build_status_event(
        decision=decision, status="EXPIRED",
        event_time=expired_at + timedelta(seconds=5),
        reason_code="TTL_EXPIRED",
    )
    assert first.event_id == second.event_id
    assert decision.eligible is True
    with pytest.raises(ValueError, match="precede"):
        build_status_event(
            decision=decision, status="EXPIRED",
            event_time=NOW, reason_code="TTL_EXPIRED",
        )

def test_completed_high_conflict_cannot_be_eligible(tmp_path):
    policy = _policy(tmp_path, enabled=True)
    snapshot = _snapshot(policy)
    review = JevReviewV1(
        evaluation_id=EVAL_ID, review_id=uuid4(),
        status=JevReviewStatusV1.COMPLETED,
        reason_code=None, reason_detail=None, evidence_chain_id=uuid4(),
        request_digest="a" * 64, response_digest="b" * 64,
        provider="fixture", model="fixture", model_version="1",
        prompt_version="PHASE9_JEV_PROMPT_V1",
        relation=JevRelationV1.HIGHLY_CONFLICTING,
        conflict_severity=JevConflictSeverityV1.HIGH,
        dominant_context=JevDominantContextV1.FLOW_DIVERGENCE,
        supporting_assessments=(), conflicting_assessments=(),
        unresolved_conflicts=(), degradation_notes=(),
        referenced_evidence_ids=(), reasoning_summary="severe conflict",
        created_at=NOW,
    )
    decision, _ = build_decision_candidate(
        snapshot=snapshot, evidence_chain=_chain(snapshot, review),
        pattern_matches=(_pattern(PatternMatchStatusV1.MATCHED),),
        jev_review=review, policy_manifest=policy, now=NOW,
    )
    assert decision.eligible is False
    assert decision.confidence_band is ConfidenceBandV1.INSUFFICIENT
    assert "JEV_HIGH_CONFLICT" in decision.reason_codes

def test_reviewed_confidence_ceiling_cannot_be_exceeded(tmp_path):
    policy = _policy(tmp_path, enabled=True)
    content = policy.manifest.policy_content.model_dump(mode="json")
    content["enabled_patterns"][0]["confidence_ceiling"] = "LOW"
    digest = str(canonical_sha256(content))
    manifest_path = tmp_path / "manifest.json"
    approval_path = tmp_path / "approval.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    manifest["policy_content"] = content
    manifest["manifest_digest"] = digest
    approval["manifest_digest"] = digest
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    approval_path.write_text(json.dumps(approval), encoding="utf-8")
    policy = load_approved_policy_manifest(
        manifest_path, approval_path, expected_commit="b" * 40,
    )
    snapshot = _snapshot(policy)
    decision, _ = build_decision_candidate(
        snapshot=snapshot, evidence_chain=_chain(snapshot),
        pattern_matches=(_pattern(PatternMatchStatusV1.MATCHED),),
        jev_review=None, policy_manifest=policy, now=NOW,
    )
    assert decision.eligible is False
    assert decision.confidence_band is ConfidenceBandV1.LOW


def test_core_freshness_is_rechecked_at_decision_clock_after_wait(tmp_path):
    policy = _policy(tmp_path,enabled=True)
    content = policy.manifest.policy_content.model_dump(mode='json')
    content['freshness_rules'] = [dict(rule_id='oi-fresh',source_phase='PHASE2',
        source_type='OPEN_INTEREST',maximum_age_seconds=10,required=True,stale_behavior='FAIL_CLOSED')]
    content['enabled_patterns'][0]['freshness_rule_ids'] = ['oi-fresh']
    digest = str(canonical_sha256(content))
    manifest_path,approval_path = tmp_path/'manifest.json',tmp_path/'approval.json'
    manifest,approval = json.loads(manifest_path.read_text()),json.loads(approval_path.read_text())
    manifest.update(policy_content=content,manifest_digest=digest)
    approval['manifest_digest'] = digest
    manifest_path.write_text(json.dumps(manifest))
    approval_path.write_text(json.dumps(approval))
    policy = load_approved_policy_manifest(manifest_path,approval_path,expected_commit='b'*40)
    from quant_phase9.sources import make_projection
    from quant_phase9.contracts import SourcePhaseV1,PolicyDataStatusV1,EvidenceFreshnessV1,EvidenceQualityV1
    projection = make_projection(evaluation_id=EVAL_ID,source_phase=SourcePhaseV1.PHASE2,
        source_type='OPEN_INTEREST',source_ref='phase2:open_interest/1',symbol='BTCUSDT',market='USDT_PERPETUAL',
        event_time=NOW,observed_at=NOW,captured_at=NOW,processed_at=NOW,available_at=NOW,
        availability_status=PolicyDataStatusV1.AVAILABLE,freshness=EvidenceFreshnessV1.FRESH,
        quality=EvidenceQualityV1.VALID,coverage=None,payload={'open_interest_usd':Decimal('1')})
    snapshot = replace(_snapshot(policy),source_projections=(projection,))
    candidate,_ = build_decision_candidate(snapshot=snapshot,evidence_chain=_chain(snapshot),
        pattern_matches=(_pattern(PatternMatchStatusV1.MATCHED),),jev_review=None,
        policy_manifest=policy,now=NOW+timedelta(seconds=11))
    assert candidate.eligible is False
    assert 'CORE_INPUT_STALE' in candidate.reason_codes
