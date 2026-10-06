from __future__ import annotations

import importlib
import hashlib
import json
from dataclasses import fields, replace
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from quant_phase6.ai import (
    AIResponse,
    AIService,
    AIUsage,
    BudgetConfig,
    BudgetLedger,
    FakeAIProvider,
    StrictSchema,
)
from quant_phase6.prompts import PromptDefinition, PromptRegistry
from quant_phase9.canonical import canonical_bytes, canonical_sha256
from quant_phase9.contracts import (
    EvidenceDirectionV1,
    EvidenceFreshnessV1,
    EvidenceQualityV1,
    EvidenceStrengthV1,
    EvidenceTypeV1,
    JevConflictClassV1,
    Phase9JevEvidenceSummaryV1,
    Phase9JevPatternSummaryV1,
    Phase9JevPolicyVersionsV1,
    Phase9JevSafeContextV1,
    PolicyCoverageStatusV1,
    PolicyDataStatusV1,
    PolicyDirectionV1,
    PatternMatchStatusV1,
    SourcePhaseV1,
)

try:
    _jev = importlib.import_module("quant_phase9.jev")
except ModuleNotFoundError:
    _jev = None


NOW = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)
EVALUATION_ID = UUID("00000000-0000-0000-0000-000000000101")
CHAIN_ID = UUID("00000000-0000-0000-0000-000000000201")
EVIDENCE_ID = UUID("00000000-0000-0000-0000-000000000301")
REVIEW_ID = UUID("00000000-0000-0000-0000-000000000501")


def _api():
    assert _jev is not None, "Phase 9 Jev adapter is not implemented"
    return _jev


def _context():
    summary = Phase9JevEvidenceSummaryV1(
        EVIDENCE_ID, EvidenceTypeV1.TRADE_FLOW, "FLOW_BUY_IMBALANCE",
        EvidenceDirectionV1.BULLISH, EvidenceStrengthV1.STRONG,
        PolicyDataStatusV1.AVAILABLE, EvidenceFreshnessV1.FRESH, EvidenceQualityV1.VALID,
        PolicyCoverageStatusV1.SOURCE_DECLARED_COMPLETE, NOW, SourcePhaseV1.PHASE3,
        "PHASE3", "bounded flow context",
    )
    versions = Phase9JevPolicyVersionsV1(
        "evidence-v1", "pattern-v1", "decision-v1", "freshness-v1", "ttl-v1", "v1", "a" * 40
    )
    unsigned = {
        "schema": "PHASE9_JEV_SAFE_CONTEXT_V1", "evaluation_id": EVALUATION_ID,
        "evidence_chain_id": CHAIN_ID, "symbol": "BTCUSDT", "market": "USDT_PERPETUAL",
        "timeframe": "15m", "as_of": NOW, "requested_at": NOW,
        "evaluation_snapshot_hash": "b" * 64, "market_regime": None,
        "supporting_evidence": (summary,), "conflicting_evidence": (),
        "missing_evidence_types": (), "degraded_evidence": (), "pattern_summaries": (
            Phase9JevPatternSummaryV1("FLOW_BREAKDOWN", PolicyDirectionV1.LONG,
                                      PatternMatchStatusV1.CONFLICTED, "pattern-v1"),
        ), "unresolved_conflict_codes": (JevConflictClassV1.DIRECTIONAL_PATTERN_CONFLICT,),
        "policy_versions": versions,
    }
    return Phase9JevSafeContextV1(**unsigned, context_hash=canonical_sha256(unsigned))


def _request(api, context=None):
    context = context or _context()
    return api.build_jev_review_request(
        context=context, review_id=REVIEW_ID, prompt_id="phase9.jev.review",
        prompt_version="v1", schema_version="phase9.jev-review.output.v1",
        provider="fixture", model="fixture-model-v1",
    )


def _output(review_id=REVIEW_ID, evidence_id=EVIDENCE_ID):
    return {
        "review_id": str(review_id),
        "relation": "CONFLICTING",
        "conflict_severity": "MEDIUM",
        "dominant_context": "FLOW_DIVERGENCE",
        "supporting_assessments": [{"evidence_ids": [str(evidence_id)], "assessment": "flow supports"}],
        "conflicting_assessments": [],
        "unresolved_conflicts": [{
            "reason_code": "DIRECTIONAL_PATTERN_CONFLICT",
            "evidence_ids": [str(evidence_id)], "detail": "flow and price diverge",
        }],
        "degradation_notes": [],
        "reasoning_summary": "Existing evidence conflicts; this review does not decide eligibility.",
    }


def test_request_digest_binds_ids_times_hash_and_safe_context():
    api = _api()
    context = _context()
    request = _request(api, context)
    assert request.schema == "PHASE9_JEV_REVIEW_REQUEST_V1"
    assert request.task_identifier == f"phase9-jev:{EVALUATION_ID}"
    assert request.review_id == REVIEW_ID
    assert request.evaluation_id == context.evaluation_id
    assert request.evidence_chain_id == context.evidence_chain_id
    assert request.requested_at == context.requested_at
    assert request.evaluation_snapshot_hash == context.evaluation_snapshot_hash
    unsigned = {field.name: getattr(request, field.name) for field in fields(request)
                if field.name != "request_digest"}
    assert request.request_digest == canonical_sha256(unsigned)

    with pytest.raises(ValueError, match="snapshot"):
        mismatched = replace(request, evaluation_snapshot_hash="d" * 64)
        api.review_with_jev(
            request=mismatched, prompt_registry=PromptRegistry(), ai_service=_service({}),
            output_schema=api.JEV_REVIEW_OUTPUT_SCHEMA,
            allowed_evidence_ids=frozenset({str(EVIDENCE_ID)}),
        )


def test_nested_validator_accepts_exact_schema_and_resolves_existing_evidence():
    api = _api()
    result = api.validate_jev_review_output(
        raw_output=_output(), allowed_evidence_ids=frozenset({str(EVIDENCE_ID)}),
    )
    assert result.review_id == REVIEW_ID
    assert result.relation.value == "CONFLICTING"
    assert result.referenced_evidence_ids == (EVIDENCE_ID,)
    assert result.reasoning_summary.startswith("Existing evidence")


@pytest.mark.parametrize("mutate", [
    lambda output: output.update({"extra": "not allowed"}),
    lambda output: output.update({"relation": "BUY"}),
    lambda output: output["supporting_assessments"][0].update({"evidence_ids": [str(uuid4())]}),
    lambda output: output.update({"BUY": True}),
    lambda output: output["unresolved_conflicts"][0].update({"ORDER": "submit"}),
    lambda output: output["supporting_assessments"][0].update({"assessment": "x" * 257}),
    lambda output: output.update({"supporting_assessments": [
        {"evidence_ids": [], "assessment": "x"} for _ in range(17)
    ]}),
])
def test_nested_validator_rejects_unknown_enums_foreign_ids_trade_fields_and_overlimits(mutate):
    api = _api()
    output = _output()
    mutate(output)
    with pytest.raises((TypeError, ValueError)):
        api.validate_jev_review_output(
            raw_output=output, allowed_evidence_ids=frozenset({str(EVIDENCE_ID)}),
        )


def test_response_depth_and_byte_caps_fail_closed():
    api = _api()
    output = _output()
    output["extension"] = {"a": {"b": {"c": {"d": "too deep"}}}}
    with pytest.raises(ValueError):
        api.validate_jev_review_output(raw_output=output, allowed_evidence_ids=frozenset({str(EVIDENCE_ID)}))

    ids = tuple(str(UUID(int=value)) for value in range(1, 65))
    oversized = _output()
    oversized["supporting_assessments"] = [
        {"evidence_ids": list(ids), "assessment": "bounded"} for _ in range(16)
    ]
    with pytest.raises(ValueError, match="32 KiB"):
        api.validate_jev_review_output(
            raw_output=oversized,
            allowed_evidence_ids=frozenset((*ids, str(EVIDENCE_ID))),
        )


def test_response_exact_32k_boundary_passes_and_next_byte_fails():
    api = _api()
    ids = tuple(str(UUID(int=value)) for value in range(1, 65))
    allowed = frozenset((*ids, str(EVIDENCE_ID)))
    exact = None
    for assessment_count in range(1, 17):
        for last_count in range(65):
            output = _output()
            output["supporting_assessments"] = [
                {"evidence_ids": list(ids), "assessment": "bounded"}
                for _ in range(assessment_count - 1)
            ]
            output["supporting_assessments"].append(
                {"evidence_ids": list(ids[:last_count]), "assessment": "bounded"}
            )
            output["reasoning_summary"] = None
            base_size = len(json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            padding = api.MAX_JEV_RESPONSE_BYTES - base_size + 2
            if 0 <= padding <= 511:
                candidate = dict(output)
                candidate["reasoning_summary"] = "x" * padding
                if len(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) == api.MAX_JEV_RESPONSE_BYTES:
                    exact = candidate
                    break
        if exact is not None:
            break
    assert exact is not None
    api.validate_jev_review_output(raw_output=exact, allowed_evidence_ids=allowed)
    one_byte_over = dict(exact)
    one_byte_over["reasoning_summary"] += "x"
    with pytest.raises(ValueError, match="32 KiB"):
        api.validate_jev_review_output(raw_output=one_byte_over, allowed_evidence_ids=allowed)


def test_evidence_reference_limit_accepts_64_and_rejects_65():
    api = _api()
    ids = tuple(str(UUID(int=value)) for value in range(1, 66))
    output = _output()
    output["unresolved_conflicts"] = []
    output["supporting_assessments"] = [{"evidence_ids": list(ids[:64]), "assessment": "at limit"}]
    accepted = api.validate_jev_review_output(raw_output=output, allowed_evidence_ids=frozenset(ids))
    assert len(accepted.referenced_evidence_ids) == 64
    output["supporting_assessments"][0]["evidence_ids"] = list(ids)
    with pytest.raises(ValueError, match="64 Evidence IDs"):
        api.validate_jev_review_output(raw_output=output, allowed_evidence_ids=frozenset(ids))


def test_request_size_above_64k_is_rejected():
    api = _api()
    context = _context()
    base_request = _request(api, context)
    base_size = len(canonical_bytes(base_request))
    exact_symbol_length = len(context.symbol) + api.MAX_JEV_REQUEST_BYTES - base_size
    unsigned = {field.name: getattr(context, field.name) for field in fields(context)
                if field.name != "context_hash"}
    unsigned["symbol"] = "X" * exact_symbol_length
    exact_context = replace(context, symbol=unsigned["symbol"], context_hash=canonical_sha256(unsigned))
    exact_request = _request(api, exact_context)
    assert len(canonical_bytes(exact_request)) == api.MAX_JEV_REQUEST_BYTES

    unsigned["symbol"] += "X"
    oversized_context = replace(
        exact_context, symbol=unsigned["symbol"], context_hash=canonical_sha256(unsigned)
    )
    with pytest.raises(ValueError, match="64 KiB"):
        _request(api, oversized_context)


class _RecordingAIService(AIService):
    def complete(self, request, schema, **kwargs):
        self.received_request = request
        self.received_schema = schema
        self.received_kwargs = kwargs
        return super().complete(request, schema, **kwargs)


class _RecordedProvider:
    def __init__(self, output):
        self.output = output
        self.calls = 0

    def complete(self, request):
        self.calls += 1
        return AIResponse("fixture", "fixture-model-v1", self.output, AIUsage())


def _service(providers):
    return _RecordingAIService(
        providers, budget=BudgetLedger(BudgetConfig()), max_retries=0,
        now=lambda: NOW,
    )


@pytest.mark.parametrize("recorded", [False, True])
def test_fake_and_recorded_provider_use_phase6_gateway_strictschema_and_same_nested_validator(recorded):
    api = _api()
    output = _output()
    if recorded:
        provider = _RecordedProvider(output)
    else:
        provider = FakeAIProvider(lambda request: AIResponse(
            "fixture", "fixture-model-v1", output, AIUsage()
        ))
    service = _service({"fixture": provider})
    request = _request(api)
    prompt = PromptDefinition(
        prompt_id="phase9.jev.review", prompt_version="v1",
        schema_version="phase9.jev-review.output.v1", model_policy_version="jev-model-policy-v1",
        system_instructions="Describe only conflicts among supplied evidence. Never issue trading instructions.",
        purpose="evidence_conflict_review", forbidden_behavior=("BUY", "SELL", "ORDER"),
    )
    review = api.review_with_jev(
        request=request, prompt_registry=PromptRegistry((prompt,)), ai_service=service,
        output_schema=api.JEV_REVIEW_OUTPUT_SCHEMA,
        allowed_evidence_ids=frozenset({str(EVIDENCE_ID)}),
    )
    assert review.status.value == "COMPLETED"
    assert review.review_id == REVIEW_ID
    assert review.referenced_evidence_ids == (EVIDENCE_ID,)
    assert isinstance(service.received_schema, StrictSchema)
    assert service.received_schema is api.JEV_REVIEW_OUTPUT_SCHEMA
    assert service.received_kwargs["fallback_provider"] is None
    assert service.received_request.max_output_bytes == 32_768
    untrusted = service.received_request.envelope.untrusted_data.split("\n", 1)[1].rsplit("\n", 1)[0]
    rendered_payload = json.loads(untrusted)
    assert rendered_payload.get("review_id") == str(REVIEW_ID)
    assert rendered_payload.get("request_digest") == str(request.request_digest)
    assert service.received_request.context_hash == hashlib.sha256(
        json.dumps(rendered_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert service.received_request.envelope.prompt_id == service.received_request.prompt_id
    assert service.received_request.envelope.prompt_version == service.received_request.prompt_version
    assert "PHASE9_JEV_SAFE_CONTEXT_V1" in service.received_request.envelope.untrusted_data
    assert service.received_request.purpose == "evidence_conflict_review"
    assert service.received_request.schema_version == "phase9.jev-review.output.v1"
    assert service.received_request.timeout_seconds == service.default_timeout_seconds


def test_absent_provider_is_explicitly_not_configured_without_gateway_call():
    api = _api()
    service = _service({})
    prompt = PromptDefinition(
        prompt_id="phase9.jev.review", prompt_version="v1",
        schema_version="phase9.jev-review.output.v1", model_policy_version="jev-model-policy-v1",
        system_instructions="Describe only the supplied evidence.",
    )
    review = api.review_with_jev(
        request=_request(api), prompt_registry=PromptRegistry((prompt,)), ai_service=service,
        output_schema=api.JEV_REVIEW_OUTPUT_SCHEMA,
        allowed_evidence_ids=frozenset({str(EVIDENCE_ID)}),
    )
    assert review.status.value == "NOT_CONFIGURED"
    assert review.reason_code == "PROVIDER_LIFECYCLE_NOT_CONFIGURED"
    assert not hasattr(service, "received_request")


def test_clear_evidence_is_rejected_before_any_gateway_call():
    api = _api()
    base = _context()
    unsigned = {field.name: getattr(base, field.name) for field in fields(base)
                if field.name != "context_hash"}
    unsigned["pattern_summaries"] = ()
    unsigned["unresolved_conflict_codes"] = ()
    clear_context = Phase9JevSafeContextV1(
        **unsigned, context_hash=canonical_sha256(unsigned)
    )
    provider = FakeAIProvider(lambda request: AIResponse(
        "fixture", "fixture-model-v1", _output(), AIUsage()
    ))
    service = _service({"fixture": provider})
    prompt = PromptDefinition(
        prompt_id="phase9.jev.review", prompt_version="v1",
        schema_version="phase9.jev-review.output.v1", model_policy_version="jev-model-policy-v1",
        system_instructions="Describe only the supplied evidence.",
    )

    with pytest.raises(api.JevContractError, match="no unresolved conflict"):
        api.review_with_jev(
            request=_request(api, clear_context), prompt_registry=PromptRegistry((prompt,)),
            ai_service=service, output_schema=api.JEV_REVIEW_OUTPUT_SCHEMA,
            allowed_evidence_ids=frozenset({str(EVIDENCE_ID)}),
        )

    assert provider.calls == 0
