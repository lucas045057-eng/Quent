"""Typed Phase 9 policy manifest and sole production approval loader.

A DRAFT manifest cannot activate a pattern. Tests may create approved artifacts
in temporary directories; production approval provenance is not inferred.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator, model_serializer

from quant_phase9.canonical import canonical_sha256
from quant_phase9.contracts import (
    ConfidenceBandV1,
    JevConflictClassV1,
    PolicyCoverageStatusV1,
    PolicyDataStatusV1,
    PolicyDirectionV1,
    SourcePhaseV1,
)


_SEMVER = r"^[0-9]+\.[0-9]+\.[0-9]+$"
_SHA256 = r"^[0-9a-f]{64}$"
_SHA1 = r"^[0-9a-f]{40}$"


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PredicateOperatorV1(StrEnum):
    PRESENT = "PRESENT"
    EQUALS = "EQUALS"
    IN_SET = "IN_SET"
    GT = "GT"
    GTE = "GTE"
    LT = "LT"
    LTE = "LTE"
    BETWEEN = "BETWEEN"


class PolicyUnitV1(StrEnum):
    NONE = "NONE"
    SECONDS = "SECONDS"
    COUNT = "COUNT"
    USD = "USD"
    BASE_ASSET = "BASE_ASSET"
    QUOTE_ASSET = "QUOTE_ASSET"
    RATIO = "RATIO"
    PERCENT = "PERCENT"
    BASIS_POINTS = "BASIS_POINTS"
    PRICE = "PRICE"
    SOURCE_NATIVE = "SOURCE_NATIVE"


class MissingBehaviorV1(StrEnum):
    FAIL_CLOSED = "FAIL_CLOSED"
    PARTIAL_MATCH = "PARTIAL_MATCH"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class PolicyApprovalStatusV1(StrEnum):
    APPROVED = "APPROVED"
    REVOKED = "REVOKED"


class StaleBehaviorV1(StrEnum):
    FAIL_CLOSED = "FAIL_CLOSED"
    PARTIAL = "PARTIAL"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class PredicateEffectV1(StrEnum):
    BLOCK_MATCH = "BLOCK_MATCH"
    CAP_CONFIDENCE = "CAP_CONFIDENCE"
    RECORD_ONLY = "RECORD_ONLY"


class DecimalNullBehaviorV1(StrEnum):
    REJECT = "REJECT"
    PRESERVE_NULL = "PRESERVE_NULL"


class RoundingModeV1(StrEnum):
    HALF_EVEN = "HALF_EVEN"
    HALF_UP = "HALF_UP"
    DOWN = "DOWN"
    UP = "UP"


class MaterialChangeFieldV1(StrEnum):
    DIRECTION = "DIRECTION"
    ELIGIBILITY = "ELIGIBILITY"
    CONFIDENCE_BAND = "CONFIDENCE_BAND"
    MATCHED_PATTERN = "MATCHED_PATTERN"
    HARD_VETO_SET = "HARD_VETO_SET"
    CORE_EVIDENCE = "CORE_EVIDENCE"
    JEV_JUDGMENT = "JEV_JUDGMENT"


class ConflictSeverityV1(StrEnum):
    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class JevUnavailableBehaviorV1(StrEnum):
    FAIL_CLOSED = "FAIL_CLOSED"
    DEGRADE = "DEGRADE"


class HardVetoCodeV1(StrEnum):
    CORE_MARKET_DATA_INVALID = "CORE_MARKET_DATA_INVALID"
    STAGE1_INVALIDATED = "STAGE1_INVALIDATED"
    CORE_DIRECTIONAL_EVIDENCE_MISSING = "CORE_DIRECTIONAL_EVIDENCE_MISSING"
    DATA_CORRUPTION_OR_PROVENANCE_INVALID = "DATA_CORRUPTION_OR_PROVENANCE_INVALID"
    EXTREME_REGIME_CONFLICT = "EXTREME_REGIME_CONFLICT"
    EVIDENCE_TOO_STALE = "EVIDENCE_TOO_STALE"


class PredicateRuleV1(_Record):
    predicate_id: str = Field(min_length=1)
    source_phase: SourcePhaseV1
    source_type: str = Field(min_length=1)
    evidence_type: str = Field(min_length=1)
    accepted_semantic_codes: tuple[str, ...]
    operator: PredicateOperatorV1
    threshold: Decimal | None
    upper_threshold: Decimal | None
    unit: PolicyUnitV1 | None
    missing_behavior: MissingBehaviorV1

    @model_validator(mode="after")
    def validate_threshold(self):
        numeric = self.operator in {
            PredicateOperatorV1.GT, PredicateOperatorV1.GTE,
            PredicateOperatorV1.LT, PredicateOperatorV1.LTE,
            PredicateOperatorV1.BETWEEN,
        }
        if numeric:
            if self.threshold is None or self.unit in {None, PolicyUnitV1.NONE}:
                raise ValueError("numeric predicate requires threshold and known unit")
            if not self.threshold.is_finite():
                raise ValueError("threshold must be finite")
            if self.operator is PredicateOperatorV1.BETWEEN:
                if self.upper_threshold is None or not self.upper_threshold.is_finite():
                    raise ValueError("BETWEEN requires finite upper threshold")
                if self.upper_threshold < self.threshold:
                    raise ValueError("BETWEEN upper threshold must follow lower")
            elif self.upper_threshold is not None:
                raise ValueError("upper threshold is only valid for BETWEEN")
        elif self.threshold is not None or self.upper_threshold is not None:
            raise ValueError("semantic predicate cannot use numeric thresholds")
        return self


class PatternPolicyV1(_Record):
    pattern_type: str = Field(min_length=1)
    timeframe: Literal["15m", "1H", "4H"]
    direction: PolicyDirectionV1
    required_predicates: tuple[str, ...]
    supporting_predicates: tuple[str, ...]
    contradicting_predicates: tuple[str, ...]
    hard_conflict_predicates: tuple[str, ...]
    freshness_rule_ids: tuple[str, ...]
    coverage_rule_ids: tuple[str, ...]
    confidence_ceiling: ConfidenceBandV1
    jev_required_conflict_classes: tuple[JevConflictClassV1, ...]
    ttl_rule_id: str = Field(min_length=1)
    revalidation_rule_id: str = Field(min_length=1)
    approval_reference: str = Field(min_length=1)
    strategy_profile: Literal["QUANT_PAPER_V1_CONSERVATIVE", "QUANT_PAPER_V2"] | None = None
    strategy_policy_digest: str | None = Field(default=None, pattern=_SHA256)

    @model_serializer(mode="wrap")
    def serialize_profile(self, handler):
        value = handler(self)
        if self.strategy_profile is None:
            value.pop("strategy_profile", None)  # Preserve legacy manifest digests.
        if self.strategy_policy_digest is None:
            value.pop("strategy_policy_digest", None)
        return value


class FreshnessRuleV1(_Record):
    rule_id: str = Field(min_length=1)
    source_phase: SourcePhaseV1
    source_type: str = Field(min_length=1)
    maximum_age_seconds: int = Field(gt=0)
    required: bool
    stale_behavior: StaleBehaviorV1


class CoverageRuleV1(_Record):
    rule_id: str = Field(min_length=1)
    source_phase: SourcePhaseV1
    source_type: str = Field(min_length=1)
    accepted_coverage_statuses: tuple[PolicyCoverageStatusV1, ...]
    minimum_coverage_ratio: Decimal | None
    ratio_unit: PolicyUnitV1 | None

    @model_validator(mode="after")
    def validate_ratio(self):
        if (self.minimum_coverage_ratio is None) != (self.ratio_unit is None):
            raise ValueError("coverage ratio and unit must be supplied together")
        if self.minimum_coverage_ratio is not None:
            if self.ratio_unit is not PolicyUnitV1.RATIO:
                raise ValueError("coverage ratio unit must be RATIO")
            if not Decimal(0) <= self.minimum_coverage_ratio <= Decimal(1):
                raise ValueError("coverage ratio must be within 0..1")
        return self


class HardVetoRuleV1(_Record):
    veto_code: HardVetoCodeV1
    predicate_ids: tuple[str, ...]
    precedence: int = Field(gt=0)
    applies_to_pattern_types: tuple[str, ...]


class SoftDegradationRuleV1(_Record):
    rule_id: str = Field(min_length=1)
    source_phase: SourcePhaseV1
    source_type: str = Field(min_length=1)
    triggering_statuses: tuple[PolicyDataStatusV1, ...]
    reason_code: str = Field(min_length=1)
    confidence_ceiling: ConfidenceBandV1
    required_predicate_effect: PredicateEffectV1


class ConfidenceCeilingRuleV1(_Record):
    rule_id: str = Field(min_length=1)
    predicate_ids: tuple[str, ...]
    maximum_band: ConfidenceBandV1
    reason_code: str = Field(min_length=1)


class TTLRuleV1(_Record):
    rule_id: str = Field(min_length=1)
    pattern_type: str = Field(min_length=1)
    timeframe: Literal["15m", "1H", "4H"]
    ttl_seconds: int = Field(gt=0)


class RevalidationRuleV1(_Record):
    rule_id: str = Field(min_length=1)
    pattern_type: str = Field(min_length=1)
    timeframe: Literal["15m", "1H", "4H"]
    cadence_seconds: int = Field(gt=0)
    max_runs_per_minute: int = Field(gt=0)


class MaterialChangeRuleV1(_Record):
    rule_id: str = Field(min_length=1)
    semantic_fields: tuple[MaterialChangeFieldV1, ...]
    decimal_tolerance: Decimal | None
    tolerance_unit: PolicyUnitV1 | None

    @model_validator(mode="after")
    def validate_tolerance(self):
        if (self.decimal_tolerance is None) != (self.tolerance_unit is None):
            raise ValueError("tolerance and unit must be supplied together")
        if self.decimal_tolerance is not None and self.decimal_tolerance < 0:
            raise ValueError("tolerance cannot be negative")
        return self


class JevConflictRuleV1(_Record):
    rule_id: str = Field(min_length=1)
    conflict_class: JevConflictClassV1
    minimum_severity: ConflictSeverityV1
    review_required: bool
    unavailable_behavior: JevUnavailableBehaviorV1


class NumericUnitRuleV1(_Record):
    semantic_field: str = Field(min_length=1)
    unit: PolicyUnitV1
    source_phase: SourcePhaseV1
    source_type: str = Field(min_length=1)
    verified_by_contract: bool


class DecimalRuleV1(_Record):
    semantic_field: str = Field(min_length=1)
    precision: int = Field(ge=0)
    input_unit: PolicyUnitV1
    nullable_behavior: DecimalNullBehaviorV1


class RoundingRuleV1(_Record):
    semantic_field: str = Field(min_length=1)
    rounding_mode: RoundingModeV1
    precision: int = Field(ge=0)
    unit: PolicyUnitV1


class HardVetoPrecedenceV1(_Record):
    higher_veto_code: HardVetoCodeV1
    lower_veto_code: HardVetoCodeV1
    precedence: int = Field(gt=0)


class PolicyContentV1(_Record):
    enabled_patterns: tuple[PatternPolicyV1, ...]
    predicates: tuple[PredicateRuleV1, ...]
    freshness_rules: tuple[FreshnessRuleV1, ...]
    coverage_rules: tuple[CoverageRuleV1, ...]
    hard_veto_rules: tuple[HardVetoRuleV1, ...]
    soft_degradation_rules: tuple[SoftDegradationRuleV1, ...]
    confidence_ceiling_rules: tuple[ConfidenceCeilingRuleV1, ...]
    ttl_rules: tuple[TTLRuleV1, ...]
    revalidation_rules: tuple[RevalidationRuleV1, ...]
    material_change_rules: tuple[MaterialChangeRuleV1, ...]
    jev_conflict_rules: tuple[JevConflictRuleV1, ...]
    numeric_units: tuple[NumericUnitRuleV1, ...]
    decimal_rules: tuple[DecimalRuleV1, ...]
    rounding_rules: tuple[RoundingRuleV1, ...]
    hard_veto_precedence: tuple[HardVetoPrecedenceV1, ...]

    @model_validator(mode="after")
    def validate_references(self):
        predicates = {rule.predicate_id for rule in self.predicates}
        freshness = {rule.rule_id for rule in self.freshness_rules}
        coverage = {rule.rule_id for rule in self.coverage_rules}
        ttls = {rule.rule_id for rule in self.ttl_rules}
        revalidations = {rule.rule_id for rule in self.revalidation_rules}
        if len(predicates) != len(self.predicates):
            raise ValueError("duplicate predicate_id")
        pattern_keys = {(p.pattern_type, p.timeframe, p.direction) for p in self.enabled_patterns}
        if len(pattern_keys) != len(self.enabled_patterns):
            raise ValueError("duplicate enabled pattern")
        for pattern in self.enabled_patterns:
            refs = (
                pattern.required_predicates + pattern.supporting_predicates
                + pattern.contradicting_predicates + pattern.hard_conflict_predicates
            )
            if not pattern.required_predicates or not set(refs) <= predicates:
                raise ValueError("enabled pattern requires reviewed predicate references")
            if not set(pattern.freshness_rule_ids) <= freshness:
                raise ValueError("enabled pattern has unknown freshness rule")
            if not set(pattern.coverage_rule_ids) <= coverage:
                raise ValueError("enabled pattern has unknown coverage rule")
            if pattern.ttl_rule_id not in ttls or pattern.revalidation_rule_id not in revalidations:
                raise ValueError("enabled pattern requires TTL and revalidation rules")
        conservative = [p for p in self.enabled_patterns if p.strategy_profile == "QUANT_PAPER_V1_CONSERVATIVE"]
        if conservative:
            if len(conservative) != len(self.enabled_patterns) or {(p.pattern_type,p.timeframe,p.direction.value) for p in conservative} != {("BREAKOUT_CONFIRMATION","15m","LONG"),("BREAKOUT_CONFIRMATION","15m","SHORT")}:
                raise ValueError("Conservative profile execution scope invalid")
            by_id = {r.predicate_id:r for r in self.predicates}
            for p in conservative:
                side=p.direction.value
                required=tuple(name+"_"+side for name in ("setup","perp","spot","oi","funding","market","events"))
                if p.required_predicates != required or p.confidence_ceiling != ConfidenceBandV1.HIGH:
                    raise ValueError("Conservative profile core predicates and HIGH ceiling required")
                for ref in required:
                    r=by_id[ref]
                    expected_code="OI_BASE_CHANGE" if ref.startswith("oi_") else ref.rsplit("_",1)[0].upper()+"_"+side+"_CONFIRMED"
                    if r.accepted_semantic_codes != (expected_code,) or r.missing_behavior != MissingBehaviorV1.FAIL_CLOSED:
                        raise ValueError("Conservative core semantic binding invalid")
                    if ref.startswith("oi_"):
                        if r.operator != PredicateOperatorV1.GTE or r.threshold != Decimal("0.0025") or r.unit != PolicyUnitV1.RATIO:
                            raise ValueError("Conservative core OI threshold invalid")
                    elif r.operator != PredicateOperatorV1.PRESENT:
                        raise ValueError("Conservative core predicate invalid")
                ttl=next(r for r in self.ttl_rules if r.rule_id==p.ttl_rule_id)
                revalidate=next(r for r in self.revalidation_rules if r.rule_id==p.revalidation_rule_id)
                if ttl.ttl_seconds>600 or revalidate.cadence_seconds>60:
                    raise ValueError("Conservative TTL/revalidation limits invalid")
        v2 = [p for p in self.enabled_patterns if p.strategy_profile == "QUANT_PAPER_V2"]
        if v2:
            expected = {("MARKET_EVIDENCE_CHAIN",tf,side) for tf in ("15m","1H","4H") for side in ("LONG","SHORT")}
            if len(v2) != len(self.enabled_patterns) or {(p.pattern_type,p.timeframe,p.direction.value) for p in v2} != expected:
                raise ValueError("V2 requires its own complete three-horizon producer scope")
            if any(p.strategy_policy_digest is None for p in v2) or len({p.strategy_policy_digest for p in v2}) != 1:
                raise ValueError("V2 patterns must bind one execution policy digest")
        return self


class PolicyManifestV1(_Record):
    schema: Literal["PHASE9_POLICY_MANIFEST_V1"]
    manifest_version: str = Field(pattern=_SEMVER)
    created_at: datetime
    policy_content: PolicyContentV1
    manifest_digest: str = Field(pattern=_SHA256)

    @field_validator("created_at")
    @classmethod
    def utc_created(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("created_at must be UTC")
        return value


class PolicyApprovalV1(_Record):
    schema: Literal["PHASE9_POLICY_APPROVAL_V1"]
    manifest_version: str = Field(pattern=_SEMVER)
    manifest_digest: str = Field(pattern=_SHA256)
    approval_status: PolicyApprovalStatusV1
    approved_at: datetime
    approved_by: str = Field(min_length=1)
    approved_commit: str = Field(pattern=_SHA1)

    @field_validator("approved_by")
    @classmethod
    def nonempty_approver(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("approved_by must identify a human approver")
        return value

    @field_validator("approved_at")
    @classmethod
    def utc_approved(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("approved_at must be UTC")
        return value


_APPROVAL_TOKEN = object()


@dataclass(frozen=True, slots=True, init=False)
class ApprovedPolicyManifestV1:
    manifest: PolicyManifestV1
    approval: PolicyApprovalV1
    code_version: str

    def __init__(
        self, manifest: PolicyManifestV1, approval: PolicyApprovalV1,
        code_version: str, *, _token: object,
    ) -> None:
        if _token is not _APPROVAL_TOKEN:
            raise ValueError("approved policy can only be created by the loader")
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(self, "approval", approval)
        object.__setattr__(self, "code_version", code_version)

    @property
    def manifest_version(self) -> str:
        return self.manifest.manifest_version

    @property
    def manifest_digest(self) -> str:
        return self.manifest.manifest_digest


def _read_json(path: Path) -> object:
    data = path.read_bytes()
    if len(data) > 262_144:
        raise ValueError("policy artifact exceeds 256 KiB")

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate policy JSON key: {key}")
            result[key] = value
        return result

    def reject_number(token: str) -> object:
        raise ValueError(f"policy JSON cannot contain a binary float: {token}")

    return json.loads(data.decode("utf-8"), object_pairs_hook=unique,
                      parse_float=reject_number, parse_constant=reject_number)


def load_approved_policy_manifest(
    manifest_path: Path, approval_path: Path, *, expected_commit: str,
) -> ApprovedPolicyManifestV1:
    """Load only an approval bound to this exact runtime revision."""
    revision = expected_commit
    if re.fullmatch(_SHA1, revision) is None:
        raise ValueError("expected_commit must be a full Git SHA")
    manifest = PolicyManifestV1.model_validate(_read_json(manifest_path))
    approval = PolicyApprovalV1.model_validate(_read_json(approval_path))
    digest = str(canonical_sha256(manifest.policy_content.model_dump(mode="python")))
    if digest != manifest.manifest_digest:
        raise ValueError("policy content digest mismatch")
    if approval.approval_status is not PolicyApprovalStatusV1.APPROVED:
        raise ValueError("policy approval is revoked or unavailable")
    if approval.manifest_version != manifest.manifest_version or approval.manifest_digest != digest:
        raise ValueError("policy approval digest/version mismatch")
    if approval.approved_commit != revision:
        raise ValueError("policy approval commit does not match expected runtime commit")
    return ApprovedPolicyManifestV1(
        manifest, approval, revision, _token=_APPROVAL_TOKEN,
    )


def policy_for(
    *, manifest: ApprovedPolicyManifestV1, pattern_type: str,
    timeframe: Literal["15m", "1H", "4H"], direction: PolicyDirectionV1,
) -> PatternPolicyV1 | None:
    if not isinstance(manifest, ApprovedPolicyManifestV1):
        raise TypeError("pattern policy requires an approved manifest")
    if not isinstance(direction, PolicyDirectionV1):
        raise TypeError("direction must be PolicyDirectionV1")
    for pattern in manifest.manifest.policy_content.enabled_patterns:
        if (
            pattern.pattern_type == pattern_type
            and pattern.timeframe == timeframe
            and pattern.direction is direction
        ):
            return pattern
    return None
