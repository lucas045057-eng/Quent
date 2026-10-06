from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from quant_phase9.canonical import canonical_sha256
from quant_phase9.contracts import (
    EvidenceChainV1,
    EvidenceDirectionV1,
    EvidenceFreshnessV1,
    EvidenceItemV1,
    EvidenceQualityV1,
    EvidenceStrengthV1,
    EvidenceTypeV1,
    EvaluationIdentityV1,
    EvaluationSnapshotV1,
    JevConflictClassV1,
    MissingEvidenceV1,
    PatternMatchStatusV1,
    PatternMatchV1,
    Phase9JevEvidenceSummaryV1,
    PolicyCoverageStatusV1,
    PolicyDataStatusV1,
    PolicyDirectionV1,
    Sha256Hex,
    SourcePhaseV1,
    Stage1CandidateEventV1,
    GitSha,
)

try:
    _jev = importlib.import_module("quant_phase9.jev")
except ModuleNotFoundError:
    _jev = None


NOW = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)
EVALUATION_ID = UUID("00000000-0000-0000-0000-000000000101")
CHAIN_ID = UUID("00000000-0000-0000-0000-000000000201")
SUPPORT_ID = UUID("00000000-0000-0000-0000-000000000301")
CONFLICT_ID = UUID("00000000-0000-0000-0000-000000000302")
DEGRADED_ID = UUID("00000000-0000-0000-0000-000000000303")


def _api():
    assert _jev is not None, "Phase 9 Jev adapter is not implemented"
    return _jev


def _inputs():
    identity = EvaluationIdentityV1(
        71, "USDT_PERPETUAL", "BTCUSDT", "15m", NOW - timedelta(minutes=15), NOW,
        "1.0.0:" + "a" * 64, "0",
    )
    candidate = Stage1CandidateEventV1(
        "1" * 64, "STAGE1_CANDIDATE", "PHASE9_STAGE1_CANDIDATE_EVENT_V1", 71, 71,
        "BTCUSDT", "USDT_PERPETUAL", NOW - timedelta(minutes=1), NOW + timedelta(minutes=30),
        "phase1-v1", NOW, ("stage1:71",), "2" * 64, NOW,
        {"schema": "PHASE9_STAGE1_CANDIDATE_EVENT_V1"},
    )
    snapshot = EvaluationSnapshotV1(
        identity, EVALUATION_ID, 71, "BTCUSDT", "USDT_PERPETUAL", "15m", NOW, NOW, NOW,
        candidate, (), "phase1-v1", "evidence-v1", "freshness-v1", "pattern-v1",
        "decision-v1", "ttl-v1", GitSha("a" * 40), Sha256Hex("b" * 64),
    )
    evidence = (
        EvidenceItemV1(SUPPORT_ID, EVALUATION_ID, "BTCUSDT", EvidenceTypeV1.TRADE_FLOW,
                      "FLOW_BUY_IMBALANCE", EvidenceDirectionV1.BULLISH, EvidenceStrengthV1.STRONG,
                      NOW, PolicyDataStatusV1.AVAILABLE, EvidenceFreshnessV1.FRESH,
                      EvidenceQualityV1.VALID, PolicyCoverageStatusV1.SOURCE_DECLARED_COMPLETE,
                      SourcePhaseV1.PHASE3, "private-source-ref-not-for-context", "fixture-provider",
                      "SOURCE_PROVIDED", ("raw:1",), "bounded flow interpretation", "evidence-v1", NOW),
        EvidenceItemV1(CONFLICT_ID, EVALUATION_ID, "BTCUSDT", EvidenceTypeV1.PRICE_STRUCTURE,
                      "PRICE_BREAKDOWN", EvidenceDirectionV1.BEARISH, EvidenceStrengthV1.MODERATE,
                      NOW, PolicyDataStatusV1.AVAILABLE, EvidenceFreshnessV1.FRESH,
                      EvidenceQualityV1.VALID, PolicyCoverageStatusV1.SOURCE_DECLARED_COMPLETE,
                      SourcePhaseV1.PHASE1, "private-source-ref-2", "fixture-provider",
                      "SOURCE_PROVIDED", ("raw:2",), "bounded price interpretation", "evidence-v1", NOW),
        EvidenceItemV1(DEGRADED_ID, EVALUATION_ID, "BTCUSDT", EvidenceTypeV1.LIQUIDATION_CONTEXT,
                      "LIQUIDATION_GAP", EvidenceDirectionV1.UNKNOWN, EvidenceStrengthV1.UNKNOWN,
                      NOW, PolicyDataStatusV1.PARTIAL, EvidenceFreshnessV1.UNKNOWN,
                      EvidenceQualityV1.PARTIAL, PolicyCoverageStatusV1.PARTIAL,
                      SourcePhaseV1.PHASE4, "private-source-ref-3", None,
                      "SOURCE_PROVIDED", (), "gap remains partial", "evidence-v1", NOW),
    )
    missing = MissingEvidenceV1(EvidenceTypeV1.OPTIONS_CONTEXT, PolicyDataStatusV1.NOT_CONFIGURED,
                                "source is optional and not configured", None, NOW)
    chain = EvidenceChainV1(
        71, "BTCUSDT", EVALUATION_ID, NOW, "15m", (SUPPORT_ID,), (CONFLICT_ID,), (),
        (missing,), (DEGRADED_ID,), (), (), True, "evidence-v1", Sha256Hex("b" * 64),
        Sha256Hex("c" * 64),
    )
    pattern = PatternMatchV1(
        UUID("00000000-0000-0000-0000-000000000401"), EVALUATION_ID, "FLOW_BREAKDOWN",
        PolicyDirectionV1.LONG, PatternMatchStatusV1.CONFLICTED, (SUPPORT_ID,),
        (SUPPORT_ID,), (CONFLICT_ID,), (), (), "pattern-v1",
    )
    return snapshot, evidence, chain, (pattern,)


def test_safe_context_is_exact_hashed_and_references_only_bounded_typed_evidence():
    api = _api()
    snapshot, evidence, chain, patterns = _inputs()
    context = api.build_phase9_jev_safe_context(
        snapshot=snapshot, evidence_items=evidence, evidence_chain=chain,
        pattern_matches=patterns, requested_at=NOW,
    )

    assert [field.name for field in fields(context)] == [
        "schema", "evaluation_id", "evidence_chain_id", "symbol", "market", "timeframe",
        "as_of", "requested_at", "evaluation_snapshot_hash", "market_regime",
        "supporting_evidence", "conflicting_evidence", "missing_evidence_types",
        "degraded_evidence", "pattern_summaries", "unresolved_conflict_codes",
        "policy_versions", "context_hash",
    ]
    unsigned = {field.name: getattr(context, field.name) for field in fields(context)
                if field.name != "context_hash"}
    assert context.context_hash == canonical_sha256(unsigned)
    assert [item.value for item in EvidenceDirectionV1] == [
        "BULLISH", "BEARISH", "NEUTRAL", "MIXED", "UNKNOWN",
    ]
    assert [item.value for item in EvidenceStrengthV1] == ["STRONG", "MODERATE", "WEAK", "UNKNOWN"]
    assert [item.value for item in PolicyDataStatusV1] == [
        "AVAILABLE", "STALE", "NOT_AVAILABLE", "PARTIAL", "ERROR", "NOT_CONFIGURED",
    ]
    assert [item.evidence_id for item in context.supporting_evidence] == [SUPPORT_ID]
    assert [item.evidence_id for item in context.conflicting_evidence] == [CONFLICT_ID]
    assert [item.evidence_id for item in context.degraded_evidence] == [DEGRADED_ID]
    assert context.missing_evidence_types == (EvidenceTypeV1.OPTIONS_CONTEXT,)
    assert context.unresolved_conflict_codes == (JevConflictClassV1.DIRECTIONAL_PATTERN_CONFLICT,)
    assert context.evidence_chain_id == api.evidence_chain_id_for(EVALUATION_ID)
    from quant_phase9.persistence import chain_id_for
    assert context.evidence_chain_id == chain_id_for(EVALUATION_ID)
    assert "private-source-ref-not-for-context" not in repr(context)
    assert "fixture-provider" not in repr(context)


def test_phase6_context_uses_existing_aisafecontext_contract_and_distinct_hash():
    api = _api()
    snapshot, evidence, chain, patterns = _inputs()
    context = api.build_phase9_jev_safe_context(
        snapshot=snapshot, evidence_items=evidence, evidence_chain=chain,
        pattern_matches=patterns, requested_at=NOW,
    )
    mapped = api.build_phase6_safe_context_for_phase9(context=context)
    task_id = f"phase9-jev:{EVALUATION_ID}"
    assert mapped.task_id == task_id
    assert mapped.__class__.__module__ == "quant_phase6.security"
    phase6_bytes = json.dumps(
        mapped.to_dict(),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    assert mapped.context_hash == hashlib.sha256(phase6_bytes).hexdigest()
    assert mapped.context_hash != context.context_hash
    serialized = json.dumps(mapped.to_dict(), ensure_ascii=False)
    for forbidden in ("source_ref", "raw_refs", "provider", "password", "authorization"):
        assert forbidden not in serialized


def test_phase6_mapping_fails_closed_if_strict_redaction_changes_payload(monkeypatch):
    api = _api()
    snapshot, evidence, chain, patterns = _inputs()
    context = api.build_phase9_jev_safe_context(
        snapshot=snapshot, evidence_items=evidence, evidence_chain=chain,
        pattern_matches=patterns, requested_at=NOW,
    )
    monkeypatch.setattr(api, "redact_sensitive", lambda value, strict: ({"input": {}}, ("input.secret",)))
    with pytest.raises(ValueError, match="redaction"):
        api.build_phase6_safe_context_for_phase9(context=context)


def test_snapshot_and_chain_mismatch_is_rejected_before_context_creation():
    api = _api()
    snapshot, evidence, chain, patterns = _inputs()
    bad_chain = EvidenceChainV1(
        chain.stage1_candidate_id, chain.symbol, UUID("00000000-0000-0000-0000-000000000999"),
        chain.evaluation_time, chain.timeframe, chain.supporting, chain.conflicting, chain.neutral,
        chain.missing, chain.degraded, chain.hard_vetoes, chain.matched_patterns,
        chain.jev_review_required, chain.evidence_schema_version, chain.evaluation_snapshot_hash,
        chain.input_snapshot_hash,
    )
    with pytest.raises(ValueError, match="evaluation"):
        api.build_phase9_jev_safe_context(
            snapshot=snapshot, evidence_items=evidence, evidence_chain=bad_chain,
            pattern_matches=patterns, requested_at=NOW,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"stage1_candidate_id": 72},
        {"evaluation_time": NOW - timedelta(seconds=1)},
        {"evidence_schema_version": "evidence-v2"},
    ],
)
def test_snapshot_chain_candidate_time_and_schema_identity_must_match(changes):
    api = _api()
    snapshot, evidence, chain, patterns = _inputs()
    with pytest.raises(ValueError, match="candidate, time, and schema"):
        api.build_phase9_jev_safe_context(
            snapshot=snapshot, evidence_items=evidence, evidence_chain=replace(chain, **changes),
            pattern_matches=patterns, requested_at=NOW,
        )


def test_evidence_schema_version_must_match_immutable_snapshot():
    api = _api()
    snapshot, evidence, chain, patterns = _inputs()
    mismatched = replace(evidence[0], schema_version="evidence-v2")
    with pytest.raises(ValueError, match="Evidence schema version"):
        api.build_phase9_jev_safe_context(
            snapshot=snapshot, evidence_items=(mismatched, *evidence[1:]), evidence_chain=chain,
            pattern_matches=patterns, requested_at=NOW,
        )


def test_safe_context_rejects_chain_that_does_not_require_jev_review():
    api = _api()
    snapshot, evidence, chain, patterns = _inputs()
    with pytest.raises(ValueError, match="does not require Jev review"):
        api.build_phase9_jev_safe_context(
            snapshot=snapshot, evidence_items=evidence,
            evidence_chain=replace(chain, jev_review_required=False),
            pattern_matches=patterns, requested_at=NOW,
        )


def test_free_form_interpretation_is_not_forwarded_to_ai_context():
    api = _api()
    snapshot, evidence, chain, patterns = _inputs()
    private_text = "raw-private-reference api-key=not-for-ai"
    tainted = replace(evidence[0], interpretation=private_text)
    context = api.build_phase9_jev_safe_context(
        snapshot=snapshot, evidence_items=(tainted, *evidence[1:]), evidence_chain=chain,
        pattern_matches=patterns, requested_at=NOW,
    )
    summary = context.supporting_evidence[0]
    assert private_text not in summary.interpretation
    assert summary.interpretation == (
        "TRADE_FLOW: FLOW_BUY_IMBALANCE; direction=BULLISH; strength=STRONG; "
        "availability=AVAILABLE; freshness=FRESH; quality=VALID"
    )


def test_semantic_code_rejects_non_identifier_source_text():
    api = _api()
    snapshot, evidence, chain, patterns = _inputs()
    unsafe_code = replace(evidence[0], semantic_code="https://rpc.invalid/?api-key=secret")
    with pytest.raises(ValueError, match="semantic code"):
        api.build_phase9_jev_safe_context(
            snapshot=snapshot, evidence_items=(unsafe_code, *evidence[1:]), evidence_chain=chain,
            pattern_matches=patterns, requested_at=NOW,
        )


def test_future_observed_evidence_is_rejected_against_immutable_snapshot_asof():
    api = _api()
    snapshot, evidence, chain, patterns = _inputs()
    future = replace(evidence[0], observed_at=NOW + timedelta(microseconds=1))
    with pytest.raises(ValueError, match="later than snapshot as_of"):
        api.build_phase9_jev_safe_context(
            snapshot=snapshot, evidence_items=(future, *evidence[1:]), evidence_chain=chain,
            pattern_matches=patterns, requested_at=NOW + timedelta(minutes=1),
        )
