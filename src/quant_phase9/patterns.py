"""Policy-bound Phase 9 pattern truth tables; no evidence voting."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from .canonical import canonical_json
from .contracts import (
    EvidenceDirectionV1, EvidenceFreshnessV1, EvidenceItemV1,
    EvidenceQualityV1, EvidenceTypeV1, MissingEvidenceV1,
    PolicyCoverageStatusV1,
    PatternMatchStatusV1, PatternMatchV1, PolicyDataStatusV1,
    PolicyDirectionV1,
)
from .policy import (
    ApprovedPolicyManifestV1, PatternPolicyV1, PredicateOperatorV1,
    PredicateRuleV1, policy_for,
)
from .validator import ValidationResultV1


_PATTERN_TIMEFRAMES = (
    ("TREND_CONTINUATION", frozenset(("1H", "4H"))),
    ("BREAKOUT_CONFIRMATION", frozenset(("15m", "1H"))),
    ("LIQUIDATION_REVERSAL", frozenset(("15m", "1H"))),
)


class _Truth(StrEnum):
    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class _PredicateResult:
    truth: _Truth
    evidence_id: UUID | None
    conflicting_id: UUID | None
    missing: MissingEvidenceV1 | None


def _source_type(item: EvidenceItemV1) -> str | None:
    prefix = "source_type="
    if not item.provenance.startswith(prefix):
        return None
    source_type = item.provenance[len(prefix):].split(";", 1)[0]
    return source_type or None


def _usable(item: EvidenceItemV1, degraded: frozenset[UUID]) -> bool:
    return (
        item.availability_status is PolicyDataStatusV1.AVAILABLE
        and item.freshness_status is EvidenceFreshnessV1.FRESH
        and item.quality_status is EvidenceQualityV1.VALID
        and item.coverage_status not in {
            PolicyCoverageStatusV1.PARTIAL, PolicyCoverageStatusV1.UNKNOWN,
            PolicyCoverageStatusV1.NOT_AVAILABLE,
        }
        and item.observed_at is not None
        and item.evidence_id not in degraded
    )


def _evaluate(
    rule: PredicateRuleV1, items: tuple[EvidenceItemV1, ...],
    degraded: frozenset[UUID],
) -> _PredicateResult:
    category = EvidenceTypeV1(rule.evidence_type)
    relevant = tuple(
        item for item in items
        if item.source_phase is rule.source_phase
        and item.evidence_type is category
        and _source_type(item) == rule.source_type
    )
    if not relevant:
        return _PredicateResult(
            _Truth.UNKNOWN, None, None,
            MissingEvidenceV1(
                evidence_type=category, source_status=PolicyDataStatusV1.NOT_AVAILABLE,
                reason=f"required predicate {rule.predicate_id} has no source projection",
                source_timestamp=None, captured_at=None,
            ),
        )
    if rule.operator not in {PredicateOperatorV1.PRESENT, PredicateOperatorV1.EQUALS,
                             PredicateOperatorV1.IN_SET}:
        candidates = tuple(item for item in relevant
            if item.semantic_code in rule.accepted_semantic_codes)
        usable = tuple(item for item in candidates if _usable(item, degraded)
            and item.numeric_value is not None and item.numeric_unit == rule.unit.value)
        if usable and len(usable) == len(candidates) and len({item.numeric_value for item in usable}) == 1:
            item = usable[0]
            value = item.numeric_value
            operations = {
                PredicateOperatorV1.GT: lambda: value > rule.threshold,
                PredicateOperatorV1.GTE: lambda: value >= rule.threshold,
                PredicateOperatorV1.LT: lambda: value < rule.threshold,
                PredicateOperatorV1.LTE: lambda: value <= rule.threshold,
                PredicateOperatorV1.BETWEEN: lambda: rule.threshold <= value <= rule.upper_threshold,
            }
            truth = operations[rule.operator]()
            return _PredicateResult(_Truth.TRUE if truth else _Truth.FALSE,
                item.evidence_id if truth else None, item.evidence_id if not truth else None, None)
        return _PredicateResult(
            _Truth.UNKNOWN, None, None,
            MissingEvidenceV1(
                evidence_type=category, source_status=PolicyDataStatusV1.NOT_AVAILABLE,
                reason=f"numeric predicate {rule.predicate_id} lacks typed evidence value",
                source_timestamp=relevant[0].observed_at, captured_at=None,
            ),
        )
    matching = tuple(
        item for item in relevant if item.semantic_code in rule.accepted_semantic_codes
    )
    usable = next((item for item in matching if _usable(item, degraded)), None)
    if usable is not None:
        return _PredicateResult(_Truth.TRUE, usable.evidence_id, None, None)
    if matching:
        item = matching[0]
        return _PredicateResult(
            _Truth.UNKNOWN, None, None,
            MissingEvidenceV1(
                evidence_type=category, source_status=item.availability_status,
                reason=f"required predicate {rule.predicate_id} source quality/freshness incomplete",
                source_timestamp=item.observed_at, captured_at=None,
            ),
        )
    expected_directions = {
        EvidenceDirectionV1.BULLISH if "UP" in code or "BUY" in code or "LONG" in code
        else EvidenceDirectionV1.BEARISH
        for code in rule.accepted_semantic_codes
        if any(token in code for token in ("UP", "DOWN", "BUY", "SELL", "LONG", "SHORT"))
    }
    opposing = next(
        (item for item in relevant
         if _usable(item, degraded)
         and item.direction in {EvidenceDirectionV1.BULLISH, EvidenceDirectionV1.BEARISH}
         and expected_directions
         and item.direction not in expected_directions),
        None,
    )
    if opposing is not None:
        return _PredicateResult(_Truth.FALSE, None, opposing.evidence_id, None)
    return _PredicateResult(
        _Truth.UNKNOWN, None, None,
        MissingEvidenceV1(
            evidence_type=category, source_status=relevant[0].availability_status,
            reason=f"required predicate {rule.predicate_id} semantic fact unavailable",
            source_timestamp=relevant[0].observed_at, captured_at=None,
        ),
    )


def _pattern_id(
    evaluation_id: UUID, pattern_type: str, timeframe: str,
    direction: PolicyDirectionV1, policy_digest: str,
) -> UUID:
    return uuid5(NAMESPACE_URL, canonical_json({
        "evaluation_id": evaluation_id, "pattern_type": pattern_type,
        "timeframe": timeframe, "direction": direction,
        "policy_manifest_digest": policy_digest,
    }))


def _not_configured(
    evaluation_id: UUID, pattern_type: str, timeframe: str,
    direction: PolicyDirectionV1, manifest: ApprovedPolicyManifestV1,
) -> PatternMatchV1:
    return PatternMatchV1(
        pattern_match_id=_pattern_id(
            evaluation_id, pattern_type, timeframe, direction, manifest.manifest_digest,
        ),
        evaluation_id=evaluation_id, pattern_type=pattern_type, direction=direction,
        status=PatternMatchStatusV1.NOT_CONFIGURED,
        required_evidence_ids=(), supporting_evidence_ids=(),
        conflicting_evidence_ids=(), missing=(), vetoes=(),
        pattern_policy_version=manifest.manifest_version,
    )


def _match_one(
    *, items: tuple[EvidenceItemV1, ...], validation: ValidationResultV1,
    manifest: ApprovedPolicyManifestV1, timeframe: str,
    policy: PatternPolicyV1,
) -> PatternMatchV1:
    rules = {rule.predicate_id: rule for rule in manifest.manifest.policy_content.predicates}
    degraded = frozenset(validation.degraded)
    required = tuple(_evaluate(rules[ref], items, degraded)
                     for ref in policy.required_predicates)
    setup = required[0]
    contradictions = tuple(result.conflicting_id for result in required
                           if result.truth is _Truth.FALSE
                           and result.conflicting_id is not None)
    missing = tuple(result.missing for result in required
                    if result.truth is _Truth.UNKNOWN and result.missing is not None)
    required_ids = tuple(result.evidence_id for result in required
                         if result.truth is _Truth.TRUE and result.evidence_id is not None)
    if setup.truth is not _Truth.TRUE:
        status = PatternMatchStatusV1.NOT_MATCHED
    elif contradictions:
        status = PatternMatchStatusV1.CONFLICTED
    elif missing:
        status = PatternMatchStatusV1.PARTIAL_MATCH
    else:
        status = PatternMatchStatusV1.MATCHED
    supporting: list[UUID] = []
    for ref in policy.supporting_predicates:
        result = _evaluate(rules[ref], items, degraded)
        if result.truth is _Truth.TRUE and result.evidence_id is not None:
            supporting.append(result.evidence_id)
    conflicts = list(contradictions)
    for ref in policy.contradicting_predicates + policy.hard_conflict_predicates:
        result = _evaluate(rules[ref], items, degraded)
        if result.truth is _Truth.TRUE and result.evidence_id is not None:
            conflicts.append(result.evidence_id)
    if conflicts and status is PatternMatchStatusV1.MATCHED:
        status = PatternMatchStatusV1.CONFLICTED
    return PatternMatchV1(
        pattern_match_id=_pattern_id(
            validation.evaluation_id, policy.pattern_type, timeframe,
            policy.direction, manifest.manifest_digest,
        ),
        evaluation_id=validation.evaluation_id,
        pattern_type=policy.pattern_type, direction=policy.direction, status=status,
        required_evidence_ids=required_ids, supporting_evidence_ids=tuple(supporting),
        conflicting_evidence_ids=tuple(dict.fromkeys(conflicts)),
        missing=missing, vetoes=validation.hard_vetoes,
        pattern_policy_version=manifest.manifest_version,
    )


def match_patterns(
    *, evidence_items: Sequence[EvidenceItemV1], validation: ValidationResultV1,
    policy_manifest: ApprovedPolicyManifestV1,
    timeframe: Literal["15m", "1H", "4H"],
) -> tuple[PatternMatchV1, ...]:
    if not isinstance(policy_manifest, ApprovedPolicyManifestV1):
        raise TypeError("patterns require an approved policy manifest")
    if not isinstance(validation, ValidationResultV1):
        raise TypeError("validation must be ValidationResultV1")
    if timeframe not in {"15m", "1H", "4H"}:
        raise ValueError("unsupported pattern timeframe")
    items = tuple(evidence_items)
    if tuple(item.evidence_id for item in items) != validation.evidence_ids:
        raise ValueError("pattern evidence IDs do not match validation")
    if any(item.evaluation_id != validation.evaluation_id for item in items):
        raise ValueError("pattern evidence belongs to another evaluation")
    matches: list[PatternMatchV1] = []
    for pattern_type, timeframes in _PATTERN_TIMEFRAMES:
        if timeframe not in timeframes:
            continue
        for direction in (PolicyDirectionV1.LONG, PolicyDirectionV1.SHORT):
            policy = policy_for(
                manifest=policy_manifest, pattern_type=pattern_type,
                timeframe=timeframe, direction=direction,
            )
            if policy is None:
                matches.append(_not_configured(
                    validation.evaluation_id, pattern_type, timeframe,
                    direction, policy_manifest,
                ))
            else:
                matches.append(_match_one(
                    items=items, validation=validation,
                    manifest=policy_manifest, timeframe=timeframe, policy=policy,
                ))
    credible_long = tuple(m for m in matches
                          if m.direction is PolicyDirectionV1.LONG
                          and m.status is PatternMatchStatusV1.MATCHED)
    credible_short = tuple(m for m in matches
                           if m.direction is PolicyDirectionV1.SHORT
                           and m.status is PatternMatchStatusV1.MATCHED)
    if credible_long and credible_short:
        long_ids = tuple(eid for m in credible_long for eid in m.required_evidence_ids)
        short_ids = tuple(eid for m in credible_short for eid in m.required_evidence_ids)
        matches = [
            replace(
                match, status=PatternMatchStatusV1.CONFLICTED,
                conflicting_evidence_ids=tuple(dict.fromkeys(
                    match.conflicting_evidence_ids
                    + (short_ids if match.direction is PolicyDirectionV1.LONG else long_ids)
                )),
            )
            if match.status is PatternMatchStatusV1.MATCHED else match
            for match in matches
        ]
    return tuple(matches)
