from __future__ import annotations

from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
from uuid import UUID

import pytest

from quant_phase9.contracts import (
    DecisionCandidateV1,
    EvidenceChainV1,
    EvidenceDirectionV1,
    EvidenceFreshnessV1,
    EvidenceItemV1,
    EvidenceQualityV1,
    EvidenceStrengthV1,
    EvidenceTypeV1,
    EvaluationIdentityV1,
    EvaluationSnapshotV1,
    EventIdentityV1,
    GitSha,
    JevReviewV1,
    PatternMatchStatusV1,
    PatternMatchV1,
    Sha256Hex,
    SourceProjectionV1,
    Stage1CandidateEventV1,
)


def _field_names(model: type) -> set[str]:
    return {field.name for field in fields(model)}


def test_canonical_contracts_have_the_frozen_field_sets():
    expected = {
        EvaluationIdentityV1: {
            "stage1_candidate_id", "market", "symbol", "timeframe",
            "evaluation_window_start", "evaluation_window_end",
            "policy_generation", "material_change_generation",
        },
        EventIdentityV1: {
            "event_type", "event_schema_version", "screening_result_id",
            "canonical_payload_digest",
        },
        Stage1CandidateEventV1: {
            "event_id", "event_type", "event_schema_version",
            "stage1_candidate_id", "screening_result_id", "symbol", "market",
            "candidate_created_at", "candidate_valid_until", "stage1_policy_version",
            "source_as_of", "source_refs", "canonical_payload_digest", "created_at",
            "canonical_payload",
        },
        SourceProjectionV1: {
            "projection_id", "evaluation_id", "source_phase", "source_type",
            "source_ref", "symbol", "market", "event_time", "observed_at",
            "captured_at", "processed_at", "available_at", "availability_status",
            "freshness_status", "quality_status", "coverage_status", "canonical_payload",
            "source_schema_version", "projection_version", "canonical_digest",
        },
        EvaluationSnapshotV1: {
            "identity", "evaluation_id", "stage1_candidate_id", "symbol", "market",
            "timeframe", "evaluation_time", "as_of", "created_at",
            "candidate_event", "source_projections", "stage1_policy_version",
            "evidence_schema_version", "freshness_policy_version",
            "pattern_policy_version", "decision_policy_version", "ttl_policy_version",
            "code_version", "snapshot_digest",
        },
        EvidenceItemV1: {
            "evidence_id", "evaluation_id", "symbol", "evidence_type",
            "semantic_code", "direction", "strength", "observed_at",
            "availability_status", "freshness_status", "quality_status",
            "coverage_status", "source_phase", "source_ref", "provider",
            "provenance", "raw_refs", "interpretation", "schema_version", "created_at",
            "numeric_value", "numeric_unit",
        },
        EvidenceChainV1: {
            "stage1_candidate_id", "symbol", "evaluation_id", "evaluation_time",
            "timeframe", "supporting", "conflicting", "neutral", "missing",
            "degraded", "hard_vetoes", "matched_patterns", "jev_review_required",
            "evidence_schema_version", "evaluation_snapshot_hash", "input_snapshot_hash",
        },
        PatternMatchV1: {
            "pattern_match_id", "evaluation_id", "pattern_type", "direction", "status",
            "required_evidence_ids", "supporting_evidence_ids", "conflicting_evidence_ids",
            "missing", "vetoes", "pattern_policy_version",
        },
        DecisionCandidateV1: {
            "decision_id", "evaluation_id", "stage1_candidate_id", "symbol", "market",
            "timeframe", "created_at", "valid_until", "eligible", "direction_bias",
            "confidence_band", "matched_pattern", "pattern_status",
            "supporting_evidence_ids", "conflicting_evidence_ids", "degraded_evidence_ids",
            "missing_evidence", "veto_reasons", "jev_review_id", "reason_codes",
            "short_summary", "input_snapshot_hash", "evidence_schema_version",
            "pattern_policy_version", "freshness_policy_version", "decision_policy_version",
            "ttl_policy_version", "prompt_version", "code_version", "supersedes_decision_id",
        },
    }
    for model, names in expected.items():
        assert _field_names(model) == names

    assert not ({"entry_price", "leverage", "position_size", "stop_loss", "take_profit",
                 "order_type", "account_balance", "private_position", "execution_instruction"}
                & _field_names(DecisionCandidateV1))
    assert set(EvidenceTypeV1) == {
        EvidenceTypeV1.PRICE_STRUCTURE,
        EvidenceTypeV1.OPEN_INTEREST_STRUCTURE,
        EvidenceTypeV1.TRADE_FLOW,
        EvidenceTypeV1.LIQUIDATION_CONTEXT,
        EvidenceTypeV1.FUNDING_BASIS_POSITIONING,
        EvidenceTypeV1.MARKET_REGIME,
        EvidenceTypeV1.OPTIONS_CONTEXT,
        EvidenceTypeV1.ONCHAIN_SPOT_MACRO,
    }


def test_identity_is_frozen_and_requires_utc_and_supported_timeframe():
    start = datetime(2026, 9, 27, 10, tzinfo=timezone.utc)
    identity = EvaluationIdentityV1(
        stage1_candidate_id=9,
        market="USDT_PERPETUAL",
        symbol="BTCUSDT",
        timeframe="15m",
        evaluation_window_start=start,
        evaluation_window_end=datetime(2026, 9, 27, 10, 15, tzinfo=timezone.utc),
        policy_generation="policy-v1",
        material_change_generation="0",
    )
    with pytest.raises(FrozenInstanceError):
        identity.symbol = "ETHUSDT"

    with pytest.raises((ValueError, TypeError)):
        EvaluationIdentityV1(
            stage1_candidate_id=9,
            market="USDT_PERPETUAL",
            symbol="BTCUSDT",
            timeframe="5m",
            evaluation_window_start=datetime(2026, 9, 27, 10),
            evaluation_window_end=datetime(2026, 9, 27, 10, 15, tzinfo=timezone.utc),
            policy_generation="policy-v1",
            material_change_generation="0",
        )


def test_scalar_hash_types_validate_exact_lowercase_lengths():
    assert str(GitSha("a" * 40)) == "a" * 40
    assert str(Sha256Hex("b" * 64)) == "b" * 64
    for invalid in ("A" * 40, "a" * 39, "g" * 40):
        with pytest.raises((ValueError, TypeError)):
            GitSha(invalid)
    for invalid in ("A" * 64, "b" * 63, "z" * 64):
        with pytest.raises((ValueError, TypeError)):
            Sha256Hex(invalid)


def test_pattern_statuses_are_exact_and_unknown_is_not_neutral():
    assert {value.value for value in PatternMatchStatusV1} == {
        "NOT_CONFIGURED", "MATCHED", "PARTIAL_MATCH", "CONFLICTED", "NOT_MATCHED"
    }
    assert EvidenceDirectionV1.UNKNOWN.value != EvidenceDirectionV1.NEUTRAL.value
    assert EvidenceStrengthV1.UNKNOWN.value == "UNKNOWN"
    assert EvidenceFreshnessV1.UNKNOWN.value == "UNKNOWN"
    assert EvidenceQualityV1.UNKNOWN.value == "UNKNOWN"


def test_jev_review_contract_is_typed_and_does_not_include_execution_fields():
    expected = {
        "evaluation_id", "review_id", "status", "reason_code", "reason_detail",
        "evidence_chain_id", "request_digest", "response_digest", "provider", "model",
        "model_version", "prompt_version", "relation", "conflict_severity",
        "dominant_context", "supporting_assessments", "conflicting_assessments",
        "unresolved_conflicts", "degradation_notes", "referenced_evidence_ids",
        "reasoning_summary", "created_at",
    }
    assert _field_names(JevReviewV1) == expected
    assert not ({"order", "position", "leverage", "entry_price", "execution_instruction"}
                & _field_names(JevReviewV1))
    assert UUID("12345678-1234-5678-1234-567812345678")
