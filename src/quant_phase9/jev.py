"""Deterministic Phase 9 conflict-review adapter over the Phase 6 AI Gateway."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any
from uuid import UUID

from quant_phase6.ai import AIErrorCode, AIRequest, AIResult, AIService, StrictSchema
from quant_phase6.contract_v1 import phase9_jev_task_id
from quant_phase6.contracts import EventStatus
from quant_phase6.prompts import PromptRegistry
from quant_phase6.security import AISafeContext, redact_sensitive
from .canonical import canonical_bytes, canonical_json, canonical_sha256
from .contracts import (
    EvidenceChainV1,
    EvidenceDirectionV1,
    EvidenceFreshnessV1,
    EvidenceItemV1,
    EvidenceQualityV1,
    EvidenceStrengthV1,
    EvidenceTypeV1,
    EvaluationSnapshotV1,
    JevConflictClassV1,
    JevConflictSeverityV1,
    JevConflictNoteV1,
    JevDegradationNoteV1,
    JevDominantContextV1,
    JevEvidenceAssessmentV1,
    JevRelationV1,
    JevReviewStatusV1,
    JevReviewV1,
    MissingEvidenceV1,
    PatternMatchStatusV1,
    PatternMatchV1,
    Phase9JevEvidenceSummaryV1,
    Phase9JevPatternSummaryV1,
    Phase9JevPolicyVersionsV1,
    Phase9JevSafeContextV1,
    PolicyDataStatusV1,
    PolicyDirectionV1,
    Sha256Hex,
    SourcePhaseV1,
)


MAX_JEV_REQUEST_BYTES = 65_536
MAX_JEV_RESPONSE_BYTES = 32_768
JEV_PROMPT_VERSION_V1 = "v1"
JEV_OUTPUT_SCHEMA_VERSION_V1 = "phase9.jev-review.output.v1"
JEV_REVIEW_PROMPT_ID_V1 = "phase9.jev.review"
_REQUEST_SCHEMA = "PHASE9_JEV_REVIEW_REQUEST_V1"
_CONTEXT_SCHEMA = "PHASE9_JEV_SAFE_CONTEXT_V1"
_HEX_256 = re.compile(r"^[0-9a-f]{64}$")
_SEMANTIC_CODE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")

_OUTPUT_FIELDS = (
    "review_id", "relation", "conflict_severity", "dominant_context",
    "supporting_assessments", "conflicting_assessments", "unresolved_conflicts",
    "degradation_notes", "reasoning_summary",
)
_TRADE_FIELDS = frozenset({
    "BUY", "SELL", "ENTRY", "STOP", "TAKEPROFIT", "LEVERAGE", "POSITION_SIZE", "ORDER",
})

JEV_REVIEW_OUTPUT_SCHEMA = StrictSchema(
    required=_OUTPUT_FIELDS,
    allowed=_OUTPUT_FIELDS,
    enums=MappingProxyType({
        "relation": tuple(value.value for value in JevRelationV1),
        "conflict_severity": tuple(value.value for value in JevConflictSeverityV1),
        "dominant_context": tuple(value.value for value in JevDominantContextV1),
    }),
)


class JevContractError(ValueError):
    """A safe-context, request, or Jev response violated its frozen contract."""


@dataclass(frozen=True, slots=True)
class JevReviewRequestV1:
    schema: str
    review_id: UUID
    evaluation_id: UUID
    evidence_chain_id: UUID
    safe_context: Phase9JevSafeContextV1
    task_identifier: str
    prompt_id: str
    prompt_version: str
    schema_version: str
    provider: str
    model: str
    requested_at: datetime
    evaluation_snapshot_hash: Sha256Hex
    request_digest: Sha256Hex

    def __post_init__(self) -> None:
        if self.schema != _REQUEST_SCHEMA:
            raise JevContractError("unsupported Jev request schema")
        if not isinstance(self.safe_context, Phase9JevSafeContextV1):
            raise TypeError("safe_context must be Phase9JevSafeContextV1")
        if self.review_id.int == 0 or self.evaluation_id != self.safe_context.evaluation_id:
            raise JevContractError("Jev request identity does not match its context")
        if self.evidence_chain_id != self.safe_context.evidence_chain_id:
            raise JevContractError("Jev request chain identity does not match its context")
        if self.task_identifier != phase9_jev_task_id(self.evaluation_id):
            raise JevContractError("Jev request task identifier is not bound to evaluation")
        if self.requested_at != self.safe_context.requested_at:
            raise JevContractError("Jev request time does not match its context")
        _require_utc(self.requested_at, "requested_at")
        if self.evaluation_snapshot_hash != self.safe_context.evaluation_snapshot_hash:
            raise JevContractError("Jev request snapshot hash does not match its context")
        if self.prompt_version != self.safe_context.policy_versions.prompt_version:
            raise JevContractError("Jev prompt version does not match the frozen context")
        for name in ("task_identifier", "prompt_id", "prompt_version", "schema_version", "provider", "model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise JevContractError(f"{name} must be non-empty")
        object.__setattr__(self, "evaluation_snapshot_hash", Sha256Hex(self.evaluation_snapshot_hash))
        object.__setattr__(self, "request_digest", Sha256Hex(self.request_digest))
        _verify_phase9_context(self.safe_context)
        if canonical_sha256(_request_content(self)) != self.request_digest:
            raise JevContractError("Jev request digest mismatch")
        if len(canonical_bytes(self)) > MAX_JEV_REQUEST_BYTES:
            raise JevContractError("Jev request exceeds 64 KiB")


@dataclass(frozen=True, slots=True)
class ValidatedJevReviewV1:
    review_id: UUID
    relation: JevRelationV1
    conflict_severity: JevConflictSeverityV1
    dominant_context: JevDominantContextV1
    supporting_assessments: tuple[JevEvidenceAssessmentV1, ...]
    conflicting_assessments: tuple[JevEvidenceAssessmentV1, ...]
    unresolved_conflicts: tuple[JevConflictNoteV1, ...]
    degradation_notes: tuple[JevDegradationNoteV1, ...]
    referenced_evidence_ids: tuple[UUID, ...]
    reasoning_summary: str | None
    response_digest: Sha256Hex


def evidence_chain_id_for(evaluation_id: UUID) -> UUID:
    """Stable one-chain-per-evaluation identity for the frozen migration contract."""
    if not isinstance(evaluation_id, UUID) or evaluation_id.int == 0:
        raise ValueError("evaluation_id must be a non-nil UUID")
    from .persistence import chain_id_for

    return chain_id_for(evaluation_id)


def build_phase9_jev_safe_context(
    *,
    snapshot: EvaluationSnapshotV1,
    evidence_items: Sequence[EvidenceItemV1],
    evidence_chain: EvidenceChainV1,
    pattern_matches: Sequence[PatternMatchV1],
    requested_at: datetime,
) -> Phase9JevSafeContextV1:
    if not isinstance(snapshot, EvaluationSnapshotV1) or not isinstance(evidence_chain, EvidenceChainV1):
        raise TypeError("snapshot and evidence_chain must be their frozen Phase 9 contracts")
    requested_at = _require_utc(requested_at, "requested_at")
    if (snapshot.evaluation_id != evidence_chain.evaluation_id
            or snapshot.symbol != evidence_chain.symbol
            or snapshot.timeframe != evidence_chain.timeframe
            or evidence_chain.evaluation_snapshot_hash != snapshot.snapshot_digest):
        raise JevContractError("snapshot and EvidenceChain evaluation/hash mismatch")
    if (snapshot.stage1_candidate_id != evidence_chain.stage1_candidate_id
            or snapshot.evaluation_time != evidence_chain.evaluation_time
            or snapshot.evidence_schema_version != evidence_chain.evidence_schema_version):
        raise JevContractError("snapshot and EvidenceChain candidate, time, and schema mismatch")
    if not evidence_chain.jev_review_required:
        raise JevContractError("EvidenceChain does not require Jev review")
    if snapshot.as_of > requested_at:
        raise JevContractError("Jev request cannot precede snapshot as_of")
    if not isinstance(evidence_items, Sequence) or isinstance(evidence_items, (str, bytes)):
        raise TypeError("evidence_items must be an ordered sequence")
    if not isinstance(pattern_matches, Sequence) or isinstance(pattern_matches, (str, bytes)):
        raise TypeError("pattern_matches must be an ordered sequence")

    evidence_by_id: dict[UUID, EvidenceItemV1] = {}
    for item in evidence_items:
        if not isinstance(item, EvidenceItemV1):
            raise TypeError("evidence_items must contain EvidenceItemV1")
        if item.evaluation_id != snapshot.evaluation_id or item.symbol != snapshot.symbol:
            raise JevContractError("Evidence item belongs to a different evaluation or symbol")
        if item.schema_version != snapshot.evidence_schema_version:
            raise JevContractError("Evidence schema version does not match immutable snapshot")
        if len(item.semantic_code) > 64 or not _SEMANTIC_CODE.fullmatch(item.semantic_code):
            raise JevContractError("Evidence semantic code is not a bounded schema identifier")
        if item.observed_at is not None and item.observed_at > snapshot.as_of:
            raise JevContractError("Evidence item was observed later than snapshot as_of")
        if item.evidence_id in evidence_by_id:
            raise JevContractError("duplicate Evidence identity in Jev input")
        evidence_by_id[item.evidence_id] = item

    all_chain_ids = (*evidence_chain.supporting, *evidence_chain.conflicting,
                     *evidence_chain.neutral, *evidence_chain.degraded)
    if len(set(all_chain_ids)) != len(all_chain_ids):
        raise JevContractError("EvidenceChain repeats an evidence identity across groups")
    if any(item_id not in evidence_by_id for item_id in all_chain_ids):
        raise JevContractError("EvidenceChain references an absent Evidence item")

    patterns: list[PatternMatchV1] = []
    pattern_ids: set[UUID] = set()
    for pattern in pattern_matches:
        if not isinstance(pattern, PatternMatchV1) or pattern.evaluation_id != snapshot.evaluation_id:
            raise JevContractError("pattern match belongs to a different evaluation")
        if pattern.pattern_match_id in pattern_ids:
            raise JevContractError("duplicate pattern-match identity")
        pattern_ids.add(pattern.pattern_match_id)
        pattern_evidence_ids = (*pattern.required_evidence_ids, *pattern.supporting_evidence_ids,
                                *pattern.conflicting_evidence_ids)
        if any(item_id not in evidence_by_id for item_id in pattern_evidence_ids):
            raise JevContractError("pattern references an absent Evidence item")
        patterns.append(pattern)
    if any(pattern_id not in pattern_ids for pattern_id in evidence_chain.matched_patterns):
        raise JevContractError("EvidenceChain references an absent pattern match")

    def summaries(ids: Sequence[UUID]) -> tuple[Phase9JevEvidenceSummaryV1, ...]:
        return tuple(_evidence_summary(evidence_by_id[item_id]) for item_id in ids)

    unresolved: list[JevConflictClassV1] = []
    if any(pattern.status is PatternMatchStatusV1.CONFLICTED for pattern in patterns):
        unresolved.append(JevConflictClassV1.DIRECTIONAL_PATTERN_CONFLICT)
    auxiliary_types = {
        EvidenceTypeV1.OPEN_INTEREST_STRUCTURE,
        EvidenceTypeV1.FUNDING_BASIS_POSITIONING,
        EvidenceTypeV1.OPTIONS_CONTEXT,
        EvidenceTypeV1.ONCHAIN_SPOT_MACRO,
        EvidenceTypeV1.MARKET_REGIME,
    }
    if (not unresolved and any(evidence_by_id[item_id].evidence_type in auxiliary_types
                               for item_id in evidence_chain.conflicting)):
        unresolved.append(JevConflictClassV1.MATERIAL_AUXILIARY_CONTRADICTION)
    if (evidence_chain.jev_review_required
            and any(pattern.status is PatternMatchStatusV1.PARTIAL_MATCH for pattern in patterns)):
        unresolved.append(JevConflictClassV1.PATTERN_AMBIGUITY)
    if not unresolved and evidence_chain.conflicting:
        unresolved.append(JevConflictClassV1.DIRECTIONAL_PATTERN_CONFLICT)
    if evidence_chain.jev_review_required and not unresolved:
        raise JevContractError("Jev review is required but no typed conflict or ambiguity is present")

    regimes = [item.semantic_code for item in evidence_items
               if item.evidence_type is EvidenceTypeV1.MARKET_REGIME
               and item.availability_status is PolicyDataStatusV1.AVAILABLE
               and item.freshness_status is EvidenceFreshnessV1.FRESH
               and item.quality_status is EvidenceQualityV1.VALID]
    market_regime = regimes[0] if len(regimes) == 1 else None
    policy_versions = Phase9JevPolicyVersionsV1(
        evidence_schema_version=snapshot.evidence_schema_version,
        pattern_policy_version=snapshot.pattern_policy_version,
        decision_policy_version=snapshot.decision_policy_version,
        freshness_policy_version=snapshot.freshness_policy_version,
        ttl_policy_version=snapshot.ttl_policy_version,
        prompt_version=JEV_PROMPT_VERSION_V1,
        code_version=str(snapshot.code_version),
    )
    unsigned: dict[str, Any] = {
        "schema": _CONTEXT_SCHEMA,
        "evaluation_id": snapshot.evaluation_id,
        "evidence_chain_id": evidence_chain_id_for(snapshot.evaluation_id),
        "symbol": snapshot.symbol,
        "market": snapshot.market,
        "timeframe": snapshot.timeframe,
        "as_of": snapshot.as_of,
        "requested_at": requested_at,
        "evaluation_snapshot_hash": snapshot.snapshot_digest,
        "market_regime": market_regime,
        "supporting_evidence": summaries(evidence_chain.supporting),
        "conflicting_evidence": summaries(evidence_chain.conflicting),
        "missing_evidence_types": tuple(item.evidence_type for item in evidence_chain.missing),
        "degraded_evidence": summaries(evidence_chain.degraded),
        "pattern_summaries": tuple(Phase9JevPatternSummaryV1(
            pattern.pattern_type, pattern.direction, pattern.status, pattern.pattern_policy_version
        ) for pattern in patterns),
        "unresolved_conflict_codes": tuple(dict.fromkeys(unresolved)),
        "policy_versions": policy_versions,
    }
    return Phase9JevSafeContextV1(**unsigned, context_hash=canonical_sha256(unsigned))


def build_phase6_safe_context_for_phase9(*, context: Phase9JevSafeContextV1) -> AISafeContext:
    if not isinstance(context, Phase9JevSafeContextV1):
        raise TypeError("context must be Phase9JevSafeContextV1")
    _verify_phase9_context(context)
    task_id = phase9_jev_task_id(context.evaluation_id)
    input_payload = json.loads(canonical_json(context))
    payload = {"task_id": task_id, "input": input_payload}
    safe, paths = redact_sensitive(payload, strict=True)
    if paths or safe != payload:
        raise JevContractError("strict redaction changed the Phase 9 AI input")
    encoded = json.dumps(safe, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_JEV_REQUEST_BYTES:
        raise JevContractError("Phase 6 safe context exceeds 64 KiB")
    context_hash = hashlib.sha256(encoded).hexdigest()
    return AISafeContext(task_id=task_id, allowed_fields=MappingProxyType(safe), context_hash=context_hash)


def build_jev_review_request(
    *,
    context: Phase9JevSafeContextV1,
    review_id: UUID,
    prompt_id: str,
    prompt_version: str,
    schema_version: str,
    provider: str,
    model: str,
) -> JevReviewRequestV1:
    if not isinstance(context, Phase9JevSafeContextV1):
        raise TypeError("context must be Phase9JevSafeContextV1")
    _verify_phase9_context(context)
    if prompt_version != context.policy_versions.prompt_version:
        raise JevContractError("prompt version does not match context policy versions")
    values = {
        "schema": _REQUEST_SCHEMA,
        "review_id": review_id,
        "evaluation_id": context.evaluation_id,
        "evidence_chain_id": context.evidence_chain_id,
        "safe_context": context,
        "task_identifier": phase9_jev_task_id(context.evaluation_id),
        "prompt_id": prompt_id,
        "prompt_version": prompt_version,
        "schema_version": schema_version,
        "provider": provider,
        "model": model,
        "requested_at": context.requested_at,
        "evaluation_snapshot_hash": context.evaluation_snapshot_hash,
    }
    digest = canonical_sha256(values)
    return JevReviewRequestV1(**values, request_digest=digest)


def validate_jev_review_output(
    *, raw_output: object, allowed_evidence_ids: frozenset[str]
) -> ValidatedJevReviewV1:
    if not isinstance(raw_output, Mapping) or any(not isinstance(key, str) for key in raw_output):
        raise JevContractError("Jev output must be a JSON object")
    try:
        encoded = json.dumps(raw_output, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise JevContractError("Jev output is not bounded JSON") from exc
    if len(encoded) > MAX_JEV_RESPONSE_BYTES:
        raise JevContractError("Jev response exceeds 32 KiB")
    _validate_depth(raw_output, 1)
    _reject_trade_fields(raw_output)
    if set(raw_output) != set(_OUTPUT_FIELDS):
        raise JevContractError("Jev output has missing or unknown fields")
    if not isinstance(allowed_evidence_ids, frozenset) or any(
        not isinstance(value, str) or _canonical_uuid(value) is None for value in allowed_evidence_ids
    ):
        raise JevContractError("allowed evidence IDs must be canonical UUID strings")
    review_id = _canonical_uuid(raw_output["review_id"])
    if review_id is None or review_id.int == 0:
        raise JevContractError("review_id must be a non-nil canonical UUID")
    relation = _enum_value(JevRelationV1, raw_output["relation"], "relation")
    severity = _enum_value(JevConflictSeverityV1, raw_output["conflict_severity"], "conflict_severity")
    dominant = _enum_value(JevDominantContextV1, raw_output["dominant_context"], "dominant_context")
    supporting = _validate_assessment_array(
        raw_output["supporting_assessments"], allowed_evidence_ids, "supporting_assessments"
    )
    conflicting = _validate_assessment_array(
        raw_output["conflicting_assessments"], allowed_evidence_ids, "conflicting_assessments"
    )
    unresolved = _validate_conflict_array(
        raw_output["unresolved_conflicts"], allowed_evidence_ids, "unresolved_conflicts"
    )
    degradation = _validate_degradation_array(raw_output["degradation_notes"])
    reasoning = raw_output["reasoning_summary"]
    if reasoning is not None and (not isinstance(reasoning, str) or len(reasoning) > 512):
        raise JevContractError("reasoning_summary must be null or at most 512 characters")
    references = tuple(dict.fromkeys(
        evidence_id
        for group in (*supporting, *conflicting)
        for evidence_id in group.evidence_ids
    ))
    references += tuple(item_id for note in unresolved for item_id in note.evidence_ids
                        if item_id not in references)
    response_digest = canonical_sha256(raw_output)
    return ValidatedJevReviewV1(
        review_id, relation, severity, dominant, supporting, conflicting, unresolved,
        degradation, references, reasoning, response_digest,
    )


def review_with_jev(
    *,
    request: JevReviewRequestV1,
    prompt_registry: PromptRegistry,
    ai_service: AIService,
    output_schema: StrictSchema,
    allowed_evidence_ids: frozenset[str],
) -> JevReviewV1:
    if not isinstance(request, JevReviewRequestV1):
        raise TypeError("request must be JevReviewRequestV1")
    if not isinstance(prompt_registry, PromptRegistry) or not isinstance(ai_service, AIService):
        raise TypeError("Phase 6 PromptRegistry and AIService contracts are required")
    _verify_output_schema(output_schema)
    if not request.safe_context.unresolved_conflict_codes:
        raise JevContractError("Jev review has no unresolved conflict")
    if request.provider not in ai_service.providers:
        return _terminal_review(request, JevReviewStatusV1.NOT_CONFIGURED,
                                "PROVIDER_LIFECYCLE_NOT_CONFIGURED")

    base_context = build_phase6_safe_context_for_phase9(context=request.safe_context)
    phase6_payload = base_context.to_dict()
    phase6_payload["review_id"] = str(request.review_id)
    phase6_payload["request_digest"] = str(request.request_digest)
    redacted, redaction_paths = redact_sensitive(phase6_payload, strict=True)
    if redaction_paths or redacted != phase6_payload:
        raise JevContractError("strict redaction changed the Jev request metadata")
    encoded_context = json.dumps(
        redacted, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded_context) > MAX_JEV_REQUEST_BYTES:
        raise JevContractError("Phase 6 Jev request context exceeds 64 KiB")
    safe_context = AISafeContext(
        task_id=base_context.task_id,
        allowed_fields=MappingProxyType(redacted),
        context_hash=hashlib.sha256(encoded_context).hexdigest(),
    )
    envelope = prompt_registry.render(
        request.prompt_id, safe_context, prompt_version=request.prompt_version,
        schema_version=request.schema_version,
    )
    if (envelope.prompt_id != request.prompt_id
            or envelope.prompt_version != request.prompt_version
            or envelope.schema_version != request.schema_version):
        raise JevContractError("Phase 6 PromptEnvelope does not match Jev request")
    ai_request = AIRequest(
        purpose="evidence_conflict_review",
        prompt_id=request.prompt_id,
        prompt_version=request.prompt_version,
        schema_version=request.schema_version,
        model_policy_version=envelope.model_policy_version,
        provider=request.provider,
        model=request.model,
        envelope=envelope,
        context_hash=safe_context.context_hash,
        timeout_seconds=ai_service.default_timeout_seconds,
        max_output_bytes=MAX_JEV_RESPONSE_BYTES,
    )

    def evidence_validator(value: Mapping[str, Any]) -> None:
        validated = validate_jev_review_output(
            raw_output=value, allowed_evidence_ids=allowed_evidence_ids
        )
        if validated.review_id != request.review_id:
            raise JevContractError("Jev output review_id does not match the request")

    result = ai_service.complete(
        ai_request,
        schema=output_schema,
        fallback_provider=None,
        evidence_validator=evidence_validator,
    )
    if result.status is EventStatus.AVAILABLE and result.output is not None:
        validated = validate_jev_review_output(
            raw_output=result.output, allowed_evidence_ids=allowed_evidence_ids
        )
        if validated.review_id != request.review_id:
            return _terminal_review(request, JevReviewStatusV1.INVALID, "REVIEW_ID_MISMATCH")
        if not result.provider or not result.model:
            return _terminal_review(request, JevReviewStatusV1.INVALID, "RESPONSE_METADATA_MISSING")
        return JevReviewV1(
            evaluation_id=request.evaluation_id,
            review_id=request.review_id,
            status=JevReviewStatusV1.COMPLETED,
            reason_code=None,
            reason_detail=None,
            evidence_chain_id=request.evidence_chain_id,
            request_digest=request.request_digest,
            response_digest=validated.response_digest,
            provider=result.provider,
            model=request.model,
            model_version=result.model,
            prompt_version=request.prompt_version,
            relation=validated.relation,
            conflict_severity=validated.conflict_severity,
            dominant_context=validated.dominant_context,
            supporting_assessments=validated.supporting_assessments,
            conflicting_assessments=validated.conflicting_assessments,
            unresolved_conflicts=validated.unresolved_conflicts,
            degradation_notes=validated.degradation_notes,
            referenced_evidence_ids=validated.referenced_evidence_ids,
            reasoning_summary=validated.reasoning_summary,
            created_at=request.requested_at,
        )
    return _review_from_ai_failure(request, result)


def _request_content(request: JevReviewRequestV1) -> dict[str, object]:
    return {field.name: getattr(request, field.name) for field in fields(request)
            if field.name != "request_digest"}


def _verify_phase9_context(context: Phase9JevSafeContextV1) -> None:
    unsigned = {field.name: getattr(context, field.name) for field in fields(context)
                if field.name != "context_hash"}
    if canonical_sha256(unsigned) != context.context_hash:
        raise JevContractError("Phase 9 safe-context hash mismatch")


def _evidence_summary(item: EvidenceItemV1) -> Phase9JevEvidenceSummaryV1:
    return Phase9JevEvidenceSummaryV1(
        evidence_id=item.evidence_id,
        evidence_type=item.evidence_type,
        semantic_code=item.semantic_code,
        direction=item.direction,
        strength=item.strength,
        availability_status=item.availability_status,
        freshness_status=item.freshness_status,
        quality_status=item.quality_status,
        coverage_status=item.coverage_status,
        observed_at=item.observed_at,
        source_phase=item.source_phase,
        source_type=f"{item.source_phase.value}:{item.evidence_type.value}",
        interpretation=_safe_interpretation(item),
    )


def _safe_interpretation(item: EvidenceItemV1) -> str:
    """Render only bounded typed evidence fields; never forward source free text."""
    return (
        f"{item.evidence_type.value}: {item.semantic_code}; direction={item.direction.value}; "
        f"strength={item.strength.value}; availability={item.availability_status.value}; "
        f"freshness={item.freshness_status.value}; quality={item.quality_status.value}"
    )


def _require_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise JevContractError(f"{name} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _validate_depth(value: object, depth: int) -> None:
    if isinstance(value, (Mapping, list)) and depth > 4:
        raise JevContractError("Jev response nesting exceeds depth 4")
    if isinstance(value, Mapping):
        for child in value.values():
            _validate_depth(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _validate_depth(child, depth + 1)


def _reject_trade_fields(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = re.sub(r"[^A-Z0-9]", "", str(key).upper())
            if normalized in {re.sub(r"[^A-Z0-9]", "", name) for name in _TRADE_FIELDS}:
                raise JevContractError("Jev output contains a prohibited trade field")
            _reject_trade_fields(child)
    elif isinstance(value, list):
        for child in value:
            _reject_trade_fields(child)


def _enum_value(enum_type: type[Enum], value: object, name: str):
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise JevContractError(f"invalid {name} enum") from exc


def _canonical_uuid(value: object) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        return None
    return parsed if str(parsed) == value else None


def _evidence_refs(value: object, allowed: frozenset[str], name: str) -> tuple[UUID, ...]:
    if not isinstance(value, list) or len(value) > 64:
        raise JevContractError(f"{name} must be an array of at most 64 Evidence IDs")
    if any(not isinstance(item, str) or item not in allowed or _canonical_uuid(item) is None for item in value):
        raise JevContractError(f"{name} contains an Evidence ID outside this evaluation")
    if len(set(value)) != len(value):
        raise JevContractError(f"{name} contains duplicate Evidence IDs")
    return tuple(UUID(item) for item in value)


def _bounded_text(value: object, name: str, limit: int, *, nonempty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (nonempty and not value.strip()):
        raise JevContractError(f"{name} must be text no longer than {limit} characters")
    return value


def _validate_assessment_array(value, allowed, name):
    if not isinstance(value, list) or len(value) > 16:
        raise JevContractError(f"{name} must contain at most 16 items")
    items: list[JevEvidenceAssessmentV1] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"evidence_ids", "assessment"}:
            raise JevContractError(f"{name} item has an invalid nested schema")
        refs = _evidence_refs(item["evidence_ids"], allowed, f"{name}.evidence_ids")
        text = _bounded_text(item["assessment"], f"{name}.assessment", 256)
        items.append(JevEvidenceAssessmentV1(refs, text))
    return tuple(items)


def _validate_conflict_array(value, allowed, name):
    if not isinstance(value, list) or len(value) > 16:
        raise JevContractError(f"{name} must contain at most 16 items")
    items: list[JevConflictNoteV1] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"reason_code", "evidence_ids", "detail"}:
            raise JevContractError(f"{name} item has an invalid nested schema")
        code = _bounded_text(item["reason_code"], f"{name}.reason_code", 64, nonempty=True)
        refs = _evidence_refs(item["evidence_ids"], allowed, f"{name}.evidence_ids")
        detail = _bounded_text(item["detail"], f"{name}.detail", 256)
        items.append(JevConflictNoteV1(code, refs, detail))
    return tuple(items)


def _validate_degradation_array(value):
    if not isinstance(value, list) or len(value) > 16:
        raise JevContractError("degradation_notes must contain at most 16 items")
    items: list[JevDegradationNoteV1] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"reason_code", "detail"}:
            raise JevContractError("degradation note has an invalid nested schema")
        code = _bounded_text(item["reason_code"], "degradation.reason_code", 64, nonempty=True)
        detail = _bounded_text(item["detail"], "degradation.detail", 256)
        items.append(JevDegradationNoteV1(code, detail))
    return tuple(items)


def _verify_output_schema(schema: StrictSchema) -> None:
    if not isinstance(schema, StrictSchema):
        raise TypeError("Phase 6 StrictSchema is required for Jev output")
    if schema.required != _OUTPUT_FIELDS or schema.allowed != _OUTPUT_FIELDS:
        raise JevContractError("Phase 6 StrictSchema does not match the frozen Jev output contract")
    expected_enums = JEV_REVIEW_OUTPUT_SCHEMA.enums
    if dict(schema.enums) != dict(expected_enums):
        raise JevContractError("Phase 6 StrictSchema enum contract mismatch")


def _terminal_review(request, status, reason_code):
    return JevReviewV1(
        evaluation_id=request.evaluation_id,
        review_id=request.review_id,
        status=status,
        reason_code=reason_code,
        reason_detail=None,
        evidence_chain_id=request.evidence_chain_id,
        request_digest=request.request_digest,
        response_digest=None,
        provider=None,
        model=None,
        model_version=None,
        prompt_version=request.prompt_version,
        relation=None,
        conflict_severity=None,
        dominant_context=None,
        supporting_assessments=(),
        conflicting_assessments=(),
        unresolved_conflicts=(),
        degradation_notes=(),
        referenced_evidence_ids=(),
        reasoning_summary=None,
        created_at=request.requested_at,
    )


def _review_from_ai_failure(request: JevReviewRequestV1, result: AIResult) -> JevReviewV1:
    code = result.error_code
    if code is AIErrorCode.TIMEOUT:
        status = JevReviewStatusV1.TIMEOUT
    elif code in {AIErrorCode.INVALID_JSON, AIErrorCode.SCHEMA_ERROR, AIErrorCode.EVIDENCE_ERROR}:
        status = JevReviewStatusV1.INVALID
    elif code in {AIErrorCode.BUDGET, AIErrorCode.RATE_LIMIT, AIErrorCode.TRANSPORT,
                  AIErrorCode.QUEUE_FULL, AIErrorCode.AUTHENTICATION, AIErrorCode.PROVIDER_REJECTED}:
        status = JevReviewStatusV1.NOT_AVAILABLE
    elif result.status is EventStatus.NOT_AVAILABLE:
        status = JevReviewStatusV1.NOT_AVAILABLE
    else:
        status = JevReviewStatusV1.FAILED
    reason = {
        AIErrorCode.BUDGET: "BUDGET_BLOCKED",
        AIErrorCode.RATE_LIMIT: "RATE_LIMITED",
        AIErrorCode.TRANSPORT: "PROVIDER_UNAVAILABLE",
        AIErrorCode.QUEUE_FULL: "GATEWAY_UNAVAILABLE",
        AIErrorCode.AUTHENTICATION: "PROVIDER_UNAVAILABLE",
        AIErrorCode.PROVIDER_REJECTED: "PROVIDER_UNAVAILABLE",
    }.get(code, code.value if code is not None else result.status.value)
    return _terminal_review(request, status, reason)
