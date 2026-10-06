from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, Mapping, TypeAlias
from uuid import UUID


class GitSha(str):
    """A full, lowercase Git SHA-1 commit identifier."""

    def __new__(cls, value: str) -> GitSha:
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise ValueError("GitSha must be exactly 40 lowercase hexadecimal characters")
        return str.__new__(cls, value)


class Sha256Hex(str):
    """A full, lowercase SHA-256 hexadecimal digest."""

    def __new__(cls, value: str) -> Sha256Hex:
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("Sha256Hex must be exactly 64 lowercase hexadecimal characters")
        return str.__new__(cls, value)


class SourcePhaseV1(StrEnum):
    PHASE1 = "PHASE1"
    PHASE2 = "PHASE2"
    PHASE3 = "PHASE3"
    PHASE4 = "PHASE4"
    PHASE5 = "PHASE5"
    PHASE6 = "PHASE6"
    PHASE7 = "PHASE7"
    PHASE8 = "PHASE8"


class PolicyDataStatusV1(StrEnum):
    AVAILABLE = "AVAILABLE"
    STALE = "STALE"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    PARTIAL = "PARTIAL"
    ERROR = "ERROR"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class EvidenceFreshnessV1(StrEnum):
    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EvidenceQualityV1(StrEnum):
    VALID = "VALID"
    PARTIAL = "PARTIAL"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"


class PolicyCoverageStatusV1(StrEnum):
    SOURCE_DECLARED_COMPLETE = "SOURCE_DECLARED_COMPLETE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class EvidenceTypeV1(StrEnum):
    PRICE_STRUCTURE = "PRICE_STRUCTURE"
    OPEN_INTEREST_STRUCTURE = "OPEN_INTEREST_STRUCTURE"
    TRADE_FLOW = "TRADE_FLOW"
    LIQUIDATION_CONTEXT = "LIQUIDATION_CONTEXT"
    FUNDING_BASIS_POSITIONING = "FUNDING_BASIS_POSITIONING"
    MARKET_REGIME = "MARKET_REGIME"
    OPTIONS_CONTEXT = "OPTIONS_CONTEXT"
    ONCHAIN_SPOT_MACRO = "ONCHAIN_SPOT_MACRO"


class RequiredEvidenceKindV1(StrEnum):
    """Exact Strategy V2 prerequisite names retained in missing-evidence records."""

    PRICE_STRUCTURE_15M = "PRICE_STRUCTURE:15m"
    PRICE_STRUCTURE_1H = "PRICE_STRUCTURE:1H"
    PRICE_STRUCTURE_4H = "PRICE_STRUCTURE:4H"
    VOLUME_15M = "VOLUME:15m"
    VOLUME_1H = "VOLUME:1H"
    VOLUME_4H = "VOLUME:4H"
    SPOT_FLOW = "SPOT_FLOW"
    PERP_FLOW = "PERP_FLOW"
    OPEN_INTEREST = "OI"
    FUNDING = "FUNDING"
    BENCHMARK = "BENCHMARK"
    CROSS_OPEN_INTEREST = "CROSS_OI"
    CROSS_FUNDING = "CROSS_FUNDING"
    EVENT_COVERAGE = "EVENT_COVERAGE"
    EXCHANGE_EVENT_COVERAGE = "EXCHANGE_EVENT_COVERAGE"
    MACRO_COVERAGE = "MACRO_COVERAGE"
    RETEST_15M = "RETEST:15m"
    RETEST_1H = "RETEST:1H"
    RETEST_4H = "RETEST:4H"


def evidence_type_from_value(value: str | EvidenceTypeV1 | RequiredEvidenceKindV1) -> EvidenceTypeV1 | RequiredEvidenceKindV1:
    if isinstance(value, (EvidenceTypeV1, RequiredEvidenceKindV1)):
        return value
    if not isinstance(value, str):
        raise TypeError("evidence_type must be text")
    for enum_type in (EvidenceTypeV1, RequiredEvidenceKindV1):
        try:
            return enum_type(value)
        except ValueError:
            continue
    raise ValueError(f"unknown evidence type: {value}")


class EvidenceDirectionV1(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class EvidenceStrengthV1(StrEnum):
    STRONG = "STRONG"
    MODERATE = "MODERATE"
    WEAK = "WEAK"
    UNKNOWN = "UNKNOWN"


class PatternMatchStatusV1(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    MATCHED = "MATCHED"
    PARTIAL_MATCH = "PARTIAL_MATCH"
    CONFLICTED = "CONFLICTED"
    NOT_MATCHED = "NOT_MATCHED"


class PolicyDirectionV1(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class DecisionDirectionBiasV1(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class ConfidenceBandV1(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INSUFFICIENT = "INSUFFICIENT"


class JevReviewStatusV1(StrEnum):
    COMPLETED = "COMPLETED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    TIMEOUT = "TIMEOUT"
    INVALID = "INVALID"
    FAILED = "FAILED"


class JevRelationV1(StrEnum):
    CONSISTENT = "CONSISTENT"
    MOSTLY_CONSISTENT = "MOSTLY_CONSISTENT"
    CONFLICTING = "CONFLICTING"
    HIGHLY_CONFLICTING = "HIGHLY_CONFLICTING"
    INDETERMINATE = "INDETERMINATE"


class JevConflictSeverityV1(StrEnum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class JevDominantContextV1(StrEnum):
    FLOW_CONFIRMATION = "FLOW_CONFIRMATION"
    FLOW_DIVERGENCE = "FLOW_DIVERGENCE"
    CROWDING_RISK = "CROWDING_RISK"
    REGIME_CONFLICT = "REGIME_CONFLICT"
    LIQUIDATION_CONTEXT = "LIQUIDATION_CONTEXT"
    PRICE_STRUCTURE_DOMINANT = "PRICE_STRUCTURE_DOMINANT"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class JevConflictClassV1(StrEnum):
    PATTERN_AMBIGUITY = "PATTERN_AMBIGUITY"
    DIRECTIONAL_PATTERN_CONFLICT = "DIRECTIONAL_PATTERN_CONFLICT"
    MATERIAL_AUXILIARY_CONTRADICTION = "MATERIAL_AUXILIARY_CONTRADICTION"
    REQUIRED_HIGH_CONFLICT = "REQUIRED_HIGH_CONFLICT"


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def _require_utc(value: datetime | None, name: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical payload object keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class EvaluationIdentityV1:
    stage1_candidate_id: int
    market: str
    symbol: str
    timeframe: Literal["15m", "1H", "4H"]
    evaluation_window_start: datetime
    evaluation_window_end: datetime
    policy_generation: str
    material_change_generation: str

    def __post_init__(self) -> None:
        if isinstance(self.stage1_candidate_id, bool) or self.stage1_candidate_id <= 0:
            raise ValueError("stage1_candidate_id must be a positive integer")
        _require_text(self.market, "market")
        _require_text(self.symbol, "symbol")
        if self.timeframe not in {"15m", "1H", "4H"}:
            raise ValueError("timeframe must be one of 15m, 1H, 4H")
        _require_utc(self.evaluation_window_start, "evaluation_window_start")
        _require_utc(self.evaluation_window_end, "evaluation_window_end")
        if self.evaluation_window_end <= self.evaluation_window_start:
            raise ValueError("evaluation window end must be later than start")
        _require_text(self.policy_generation, "policy_generation")
        _require_text(self.material_change_generation, "material_change_generation")


@dataclass(frozen=True, slots=True)
class EventIdentityV1:
    event_type: str
    event_schema_version: str
    screening_result_id: int
    canonical_payload_digest: Sha256Hex

    def __post_init__(self) -> None:
        _require_text(self.event_type, "event_type")
        _require_text(self.event_schema_version, "event_schema_version")
        if isinstance(self.screening_result_id, bool) or self.screening_result_id <= 0:
            raise ValueError("screening_result_id must be a positive integer")
        object.__setattr__(self, "canonical_payload_digest", Sha256Hex(self.canonical_payload_digest))


@dataclass(frozen=True, slots=True)
class Stage1CandidateEventV1:
    event_id: Sha256Hex
    event_type: str
    event_schema_version: str
    stage1_candidate_id: int
    screening_result_id: int
    symbol: str
    market: str
    candidate_created_at: datetime
    candidate_valid_until: datetime | None
    stage1_policy_version: str
    source_as_of: datetime
    source_refs: tuple[str, ...]
    canonical_payload_digest: Sha256Hex
    created_at: datetime
    canonical_payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", Sha256Hex(self.event_id))
        object.__setattr__(self, "canonical_payload_digest", Sha256Hex(self.canonical_payload_digest))
        for name in ("event_type", "event_schema_version", "symbol", "market", "stage1_policy_version"):
            _require_text(getattr(self, name), name)
        for name in ("stage1_candidate_id", "screening_result_id"):
            value = getattr(self, name)
            if isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.stage1_candidate_id != self.screening_result_id:
            raise ValueError("V1 Stage 1 candidate and screening result IDs must match")
        _require_utc(self.candidate_created_at, "candidate_created_at")
        _require_utc(self.candidate_valid_until, "candidate_valid_until", optional=True)
        _require_utc(self.source_as_of, "source_as_of")
        _require_utc(self.created_at, "created_at")
        if not isinstance(self.source_refs, tuple) or any(not isinstance(ref, str) or not ref for ref in self.source_refs):
            raise ValueError("source_refs must be an ordered tuple of non-empty strings")
        payload = _freeze(self.canonical_payload)
        if not isinstance(payload, Mapping):
            raise TypeError("canonical_payload must be a mapping")
        object.__setattr__(self, "canonical_payload", payload)


@dataclass(frozen=True, slots=True)
class SourceProjectionV1:
    projection_id: UUID
    evaluation_id: UUID
    source_phase: SourcePhaseV1
    source_type: str
    source_ref: str
    symbol: str
    market: str
    event_time: datetime | None
    observed_at: datetime | None
    captured_at: datetime | None
    processed_at: datetime | None
    available_at: datetime | None
    availability_status: PolicyDataStatusV1
    freshness_status: EvidenceFreshnessV1
    quality_status: EvidenceQualityV1
    coverage_status: PolicyCoverageStatusV1 | None
    canonical_payload: Mapping[str, object]
    source_schema_version: str
    projection_version: str
    canonical_digest: Sha256Hex

    def __post_init__(self) -> None:
        for name in ("source_type", "source_ref", "symbol", "market", "source_schema_version", "projection_version"):
            _require_text(getattr(self, name), name)
        for name in ("event_time", "observed_at", "captured_at", "processed_at", "available_at"):
            _require_utc(getattr(self, name), name, optional=True)
        object.__setattr__(self, "canonical_digest", Sha256Hex(self.canonical_digest))
        payload = _freeze(self.canonical_payload)
        if not isinstance(payload, Mapping):
            raise TypeError("canonical_payload must be a mapping")
        object.__setattr__(self, "canonical_payload", payload)


@dataclass(frozen=True, slots=True)
class EvaluationSnapshotV1:
    identity: EvaluationIdentityV1
    evaluation_id: UUID
    stage1_candidate_id: int
    symbol: str
    market: str
    timeframe: Literal["15m", "1H", "4H"]
    evaluation_time: datetime
    as_of: datetime
    created_at: datetime
    candidate_event: Stage1CandidateEventV1
    source_projections: tuple[SourceProjectionV1, ...]
    stage1_policy_version: str
    evidence_schema_version: str
    freshness_policy_version: str
    pattern_policy_version: str
    decision_policy_version: str
    ttl_policy_version: str
    code_version: GitSha
    snapshot_digest: Sha256Hex

    def __post_init__(self) -> None:
        if self.stage1_candidate_id != self.identity.stage1_candidate_id:
            raise ValueError("snapshot candidate ID must match identity")
        if self.timeframe != self.identity.timeframe:
            raise ValueError("snapshot timeframe must match identity")
        if self.evaluation_id.int == 0:
            raise ValueError("evaluation_id must not be the nil UUID")
        for name in ("symbol", "market", "stage1_policy_version", "evidence_schema_version",
                     "freshness_policy_version", "pattern_policy_version", "decision_policy_version",
                     "ttl_policy_version"):
            _require_text(getattr(self, name), name)
        for name in ("evaluation_time", "as_of", "created_at"):
            _require_utc(getattr(self, name), name)
        if not isinstance(self.source_projections, tuple):
            raise TypeError("source_projections must preserve a defined tuple order")
        object.__setattr__(self, "code_version", GitSha(self.code_version))
        object.__setattr__(self, "snapshot_digest", Sha256Hex(self.snapshot_digest))


@dataclass(frozen=True, slots=True)
class MissingEvidenceV1:
    evidence_type: EvidenceTypeV1 | RequiredEvidenceKindV1
    source_status: PolicyDataStatusV1
    reason: str
    source_timestamp: datetime | None
    captured_at: datetime | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_type", evidence_type_from_value(self.evidence_type))
        _require_text(self.reason, "reason")
        _require_utc(self.source_timestamp, "source_timestamp", optional=True)
        _require_utc(self.captured_at, "captured_at", optional=True)


@dataclass(frozen=True, slots=True)
class EvidenceItemV1:
    evidence_id: UUID
    evaluation_id: UUID
    symbol: str
    evidence_type: EvidenceTypeV1
    semantic_code: str
    direction: EvidenceDirectionV1
    strength: EvidenceStrengthV1
    observed_at: datetime | None
    availability_status: PolicyDataStatusV1
    freshness_status: EvidenceFreshnessV1
    quality_status: EvidenceQualityV1
    coverage_status: PolicyCoverageStatusV1 | None
    source_phase: SourcePhaseV1
    source_ref: str
    provider: str | None
    provenance: str
    raw_refs: tuple[str, ...]
    interpretation: str
    schema_version: str
    created_at: datetime
    numeric_value: Decimal | None = field(default=None, metadata={"omit_if_none": True})
    numeric_unit: str | None = field(default=None, metadata={"omit_if_none": True})

    def __post_init__(self) -> None:
        if (self.numeric_value is None) != (self.numeric_unit is None):
            raise ValueError("numeric value and unit must be supplied together")
        if self.numeric_value is not None and (not isinstance(self.numeric_value, Decimal) or not self.numeric_value.is_finite() or not self.numeric_unit):
            raise ValueError("finite Decimal evidence value and known unit required")
        for name in ("symbol", "semantic_code", "source_ref", "provenance", "interpretation", "schema_version"):
            _require_text(getattr(self, name), name)
        _require_text(self.provider, "provider") if self.provider is not None else None
        _require_utc(self.observed_at, "observed_at", optional=True)
        _require_utc(self.created_at, "created_at")
        if not isinstance(self.raw_refs, tuple) or any(not isinstance(ref, str) or not ref for ref in self.raw_refs):
            raise ValueError("raw_refs must be an ordered tuple of non-empty references")


@dataclass(frozen=True, slots=True)
class EvidenceChainV1:
    stage1_candidate_id: int
    symbol: str
    evaluation_id: UUID
    evaluation_time: datetime
    timeframe: Literal["15m", "1H", "4H"]
    supporting: tuple[UUID, ...]
    conflicting: tuple[UUID, ...]
    neutral: tuple[UUID, ...]
    missing: tuple[MissingEvidenceV1, ...]
    degraded: tuple[UUID, ...]
    hard_vetoes: tuple[str, ...]
    matched_patterns: tuple[UUID, ...]
    jev_review_required: bool
    evidence_schema_version: str
    evaluation_snapshot_hash: Sha256Hex
    input_snapshot_hash: Sha256Hex

    def __post_init__(self) -> None:
        if isinstance(self.stage1_candidate_id, bool) or self.stage1_candidate_id <= 0:
            raise ValueError("stage1_candidate_id must be positive")
        _require_text(self.symbol, "symbol")
        _require_utc(self.evaluation_time, "evaluation_time")
        _require_text(self.evidence_schema_version, "evidence_schema_version")
        object.__setattr__(self, "evaluation_snapshot_hash", Sha256Hex(self.evaluation_snapshot_hash))
        object.__setattr__(self, "input_snapshot_hash", Sha256Hex(self.input_snapshot_hash))
        for name in ("supporting", "conflicting", "neutral", "missing", "degraded", "hard_vetoes", "matched_patterns"):
            if not isinstance(getattr(self, name), tuple):
                raise TypeError(f"{name} must be an ordered tuple")


@dataclass(frozen=True, slots=True)
class PatternMatchV1:
    pattern_match_id: UUID
    evaluation_id: UUID
    pattern_type: str
    direction: PolicyDirectionV1
    status: PatternMatchStatusV1
    required_evidence_ids: tuple[UUID, ...]
    supporting_evidence_ids: tuple[UUID, ...]
    conflicting_evidence_ids: tuple[UUID, ...]
    missing: tuple[MissingEvidenceV1, ...]
    vetoes: tuple[str, ...]
    pattern_policy_version: str

    def __post_init__(self) -> None:
        for name in ("pattern_type", "pattern_policy_version"):
            _require_text(getattr(self, name), name)
        for name in ("required_evidence_ids", "supporting_evidence_ids", "conflicting_evidence_ids", "missing", "vetoes"):
            if not isinstance(getattr(self, name), tuple):
                raise TypeError(f"{name} must be an ordered tuple")


@dataclass(frozen=True, slots=True)
class JevEvidenceAssessmentV1:
    evidence_ids: tuple[UUID, ...]
    assessment: str

    def __post_init__(self) -> None:
        if len(self.assessment) > 256:
            raise ValueError("assessment exceeds 256 characters")


@dataclass(frozen=True, slots=True)
class JevConflictNoteV1:
    reason_code: str
    evidence_ids: tuple[UUID, ...]
    detail: str

    def __post_init__(self) -> None:
        _require_text(self.reason_code, "reason_code")
        if len(self.detail) > 256:
            raise ValueError("conflict detail exceeds 256 characters")


@dataclass(frozen=True, slots=True)
class JevDegradationNoteV1:
    reason_code: str
    detail: str

    def __post_init__(self) -> None:
        _require_text(self.reason_code, "reason_code")
        if len(self.detail) > 256:
            raise ValueError("degradation detail exceeds 256 characters")


@dataclass(frozen=True, slots=True)
class Phase9JevEvidenceSummaryV1:
    evidence_id: UUID
    evidence_type: EvidenceTypeV1
    semantic_code: str
    direction: EvidenceDirectionV1
    strength: EvidenceStrengthV1
    availability_status: PolicyDataStatusV1
    freshness_status: EvidenceFreshnessV1
    quality_status: EvidenceQualityV1
    coverage_status: PolicyCoverageStatusV1 | None
    observed_at: datetime | None
    source_phase: SourcePhaseV1
    source_type: str
    interpretation: str

    def __post_init__(self) -> None:
        _require_text(self.semantic_code, "semantic_code")
        _require_text(self.source_type, "source_type")
        if len(self.interpretation) > 256:
            raise ValueError("interpretation exceeds 256 characters")
        _require_utc(self.observed_at, "observed_at", optional=True)


@dataclass(frozen=True, slots=True)
class Phase9JevPatternSummaryV1:
    pattern_type: str
    direction: PolicyDirectionV1
    status: PatternMatchStatusV1
    pattern_policy_version: str

    def __post_init__(self) -> None:
        _require_text(self.pattern_type, "pattern_type")
        _require_text(self.pattern_policy_version, "pattern_policy_version")


@dataclass(frozen=True, slots=True)
class Phase9JevPolicyVersionsV1:
    evidence_schema_version: str
    pattern_policy_version: str
    decision_policy_version: str
    freshness_policy_version: str
    ttl_policy_version: str
    prompt_version: str
    code_version: str


@dataclass(frozen=True, slots=True)
class Phase9JevSafeContextV1:
    schema: Literal["PHASE9_JEV_SAFE_CONTEXT_V1"]
    evaluation_id: UUID
    evidence_chain_id: UUID
    symbol: str
    market: str
    timeframe: Literal["15m", "1H", "4H"]
    as_of: datetime
    requested_at: datetime
    evaluation_snapshot_hash: Sha256Hex
    market_regime: str | None
    supporting_evidence: tuple[Phase9JevEvidenceSummaryV1, ...]
    conflicting_evidence: tuple[Phase9JevEvidenceSummaryV1, ...]
    missing_evidence_types: tuple[EvidenceTypeV1, ...]
    degraded_evidence: tuple[Phase9JevEvidenceSummaryV1, ...]
    pattern_summaries: tuple[Phase9JevPatternSummaryV1, ...]
    unresolved_conflict_codes: tuple[JevConflictClassV1, ...]
    policy_versions: Phase9JevPolicyVersionsV1
    context_hash: Sha256Hex

    def __post_init__(self) -> None:
        if self.schema != "PHASE9_JEV_SAFE_CONTEXT_V1":
            raise ValueError("unsupported Jev safe-context schema")
        for name in ("symbol", "market"):
            _require_text(getattr(self, name), name)
        _require_utc(self.as_of, "as_of")
        _require_utc(self.requested_at, "requested_at")
        object.__setattr__(self, "evaluation_snapshot_hash", Sha256Hex(self.evaluation_snapshot_hash))
        object.__setattr__(self, "context_hash", Sha256Hex(self.context_hash))
        if len(self.supporting_evidence) > 16 or len(self.conflicting_evidence) > 16 or len(self.degraded_evidence) > 16:
            raise ValueError("Jev safe-context evidence arrays are limited to 16")
        if len(self.pattern_summaries) > 16 or len(self.unresolved_conflict_codes) > 16:
            raise ValueError("Jev safe-context pattern/conflict arrays are limited to 16")
        if len(self.missing_evidence_types) > 8:
            raise ValueError("Jev safe-context missing evidence types are limited to 8")


@dataclass(frozen=True, slots=True)
class JevReviewV1:
    evaluation_id: UUID
    review_id: UUID
    status: JevReviewStatusV1
    reason_code: str | None
    reason_detail: str | None
    evidence_chain_id: UUID
    request_digest: Sha256Hex
    response_digest: Sha256Hex | None
    provider: str | None
    model: str | None
    model_version: str | None
    prompt_version: str
    relation: JevRelationV1 | None
    conflict_severity: JevConflictSeverityV1 | None
    dominant_context: JevDominantContextV1 | None
    supporting_assessments: tuple[JevEvidenceAssessmentV1, ...]
    conflicting_assessments: tuple[JevEvidenceAssessmentV1, ...]
    unresolved_conflicts: tuple[JevConflictNoteV1, ...]
    degradation_notes: tuple[JevDegradationNoteV1, ...]
    referenced_evidence_ids: tuple[UUID, ...]
    reasoning_summary: str | None
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_digest", Sha256Hex(self.request_digest))
        if self.response_digest is not None:
            object.__setattr__(self, "response_digest", Sha256Hex(self.response_digest))
        _require_text(self.prompt_version, "prompt_version")
        _require_utc(self.created_at, "created_at")
        if self.reason_detail is not None and len(self.reason_detail) > 256:
            raise ValueError("reason_detail exceeds 256 characters")
        if self.reasoning_summary is not None and len(self.reasoning_summary) > 512:
            raise ValueError("reasoning_summary exceeds 512 characters")
        for name in ("supporting_assessments", "conflicting_assessments", "unresolved_conflicts", "degradation_notes"):
            if len(getattr(self, name)) > 16:
                raise ValueError(f"{name} is limited to 16 entries")
        if len(self.referenced_evidence_ids) > 64:
            raise ValueError("referenced_evidence_ids is limited to 64 entries")
        if self.status == JevReviewStatusV1.COMPLETED:
            if any(value is None for value in (self.response_digest, self.provider, self.model, self.model_version,
                                                self.relation, self.conflict_severity, self.dominant_context)):
                raise ValueError("completed Jev review requires complete response metadata")
            if self.reason_code is not None or self.reason_detail is not None:
                raise ValueError("completed Jev review cannot contain failure detail")
        elif not self.reason_code:
            raise ValueError("non-completed Jev review requires a reason_code")


@dataclass(frozen=True, slots=True)
class Phase9ReplayArtifactRefV1:
    path: str
    sha256: Sha256Hex

    def __post_init__(self) -> None:
        _require_text(self.path, "path")
        if self.path.startswith("/") or ".." in self.path.replace("\\", "/").split("/"):
            raise ValueError("artifact path must be relative and may not traverse parents")
        object.__setattr__(self, "sha256", Sha256Hex(self.sha256))


@dataclass(frozen=True, slots=True)
class PolicyManifestRefV1:
    schema: Literal["PHASE9_POLICY_MANIFEST_REF_V1"]
    manifest_version: str
    manifest_digest: Sha256Hex
    approval_digest: Sha256Hex
    manifest_artifact: Phase9ReplayArtifactRefV1
    approval_artifact: Phase9ReplayArtifactRefV1

    def __post_init__(self) -> None:
        if self.schema != "PHASE9_POLICY_MANIFEST_REF_V1":
            raise ValueError("unsupported policy reference schema")
        _require_text(self.manifest_version, "manifest_version")
        object.__setattr__(self, "manifest_digest", Sha256Hex(self.manifest_digest))
        object.__setattr__(self, "approval_digest", Sha256Hex(self.approval_digest))


@dataclass(frozen=True, slots=True)
class DecisionCandidateV1:
    decision_id: UUID
    evaluation_id: UUID
    stage1_candidate_id: int
    symbol: str
    market: str
    timeframe: Literal["15m", "1H", "4H"]
    created_at: datetime
    valid_until: datetime
    eligible: bool
    direction_bias: DecisionDirectionBiasV1
    confidence_band: ConfidenceBandV1
    matched_pattern: str | None
    pattern_status: PatternMatchStatusV1
    supporting_evidence_ids: tuple[UUID, ...]
    conflicting_evidence_ids: tuple[UUID, ...]
    degraded_evidence_ids: tuple[UUID, ...]
    missing_evidence: tuple[MissingEvidenceV1, ...]
    veto_reasons: tuple[str, ...]
    jev_review_id: UUID | None
    reason_codes: tuple[str, ...]
    short_summary: str
    input_snapshot_hash: Sha256Hex
    evidence_schema_version: str
    pattern_policy_version: str
    freshness_policy_version: str
    decision_policy_version: str
    ttl_policy_version: str
    prompt_version: str
    code_version: GitSha
    supersedes_decision_id: UUID | None

    def __post_init__(self) -> None:
        if isinstance(self.stage1_candidate_id, bool) or self.stage1_candidate_id <= 0:
            raise ValueError("stage1_candidate_id must be positive")
        for name in ("symbol", "market", "short_summary", "evidence_schema_version", "pattern_policy_version",
                     "freshness_policy_version", "decision_policy_version", "ttl_policy_version", "prompt_version"):
            _require_text(getattr(self, name), name)
        _require_utc(self.created_at, "created_at")
        _require_utc(self.valid_until, "valid_until")
        object.__setattr__(self, "input_snapshot_hash", Sha256Hex(self.input_snapshot_hash))
        object.__setattr__(self, "code_version", GitSha(self.code_version))


@dataclass(frozen=True, slots=True)
class DecisionStatusEventV1:
    event_id: UUID
    decision_id: UUID
    evaluation_id: UUID
    status: str
    event_time: datetime
    reason_code: str
    supersedes_decision_id: UUID | None

    def __post_init__(self) -> None:
        _require_utc(self.event_time, "event_time")
        _require_text(self.status, "status")
        _require_text(self.reason_code, "reason_code")


JsonValue: TypeAlias = None | bool | int | str | Decimal | datetime | UUID | StrEnum | Mapping[str, object] | tuple[object, ...] | list[object]
