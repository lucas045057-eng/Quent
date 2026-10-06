"""Validate Phase 9 evidence identity, availability and provenance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence
from uuid import UUID

from .contracts import (
    EvaluationSnapshotV1, EvidenceDirectionV1, EvidenceFreshnessV1,
    EvidenceItemV1, EvidenceQualityV1, EvidenceTypeV1, MissingEvidenceV1,
    PolicyCoverageStatusV1, PolicyDataStatusV1,
)
from .policy import ApprovedPolicyManifestV1


@dataclass(frozen=True, slots=True)
class ValidationResultV1:
    evaluation_id: UUID
    evidence_ids: tuple[UUID, ...]
    missing: tuple[MissingEvidenceV1, ...]
    degraded: tuple[UUID, ...]
    hard_vetoes: tuple[str, ...]


_REQUIRED_CATEGORIES = (
    EvidenceTypeV1.PRICE_STRUCTURE, EvidenceTypeV1.OPEN_INTEREST_STRUCTURE,
    EvidenceTypeV1.TRADE_FLOW, EvidenceTypeV1.LIQUIDATION_CONTEXT,
    EvidenceTypeV1.MARKET_REGIME, EvidenceTypeV1.OPTIONS_CONTEXT,
)


def validate_evidence(
    *, snapshot: EvaluationSnapshotV1, evidence_items: Sequence[EvidenceItemV1],
    policy_manifest: ApprovedPolicyManifestV1,
) -> ValidationResultV1:
    if not isinstance(snapshot, EvaluationSnapshotV1):
        raise TypeError("snapshot must be EvaluationSnapshotV1")
    if not isinstance(policy_manifest, ApprovedPolicyManifestV1):
        raise TypeError("validation requires an approved policy manifest")
    items = tuple(evidence_items)
    from .evidence import build_evidence
    if items != build_evidence(snapshot=snapshot, policy_manifest=policy_manifest):
        raise ValueError("evidence items are missing or tampered relative to snapshot provenance")
    if len(items) > 64:
        raise ValueError("Phase 9 evidence exceeds 64 items")
    refs = {f"phase1:stage1_candidate/{snapshot.candidate_event.event_id}"}
    projections = {p.source_ref: p for p in snapshot.source_projections}
    refs.update(projections)
    seen: set[UUID] = set()
    missing: list[MissingEvidenceV1] = []
    degraded: list[UUID] = []
    for item in items:
        if not isinstance(item, EvidenceItemV1):
            raise TypeError("evidence_items must contain EvidenceItemV1")
        if (item.evidence_id in seen or item.evaluation_id != snapshot.evaluation_id
                or item.symbol != snapshot.symbol or item.source_ref not in refs):
            raise ValueError("evidence identity/provenance invalid")
        seen.add(item.evidence_id)
        if item.schema_version != snapshot.evidence_schema_version:
            raise ValueError("evidence schema version/provenance mismatch")
        if not item.provenance or not item.raw_refs or item.source_ref not in item.raw_refs:
            raise ValueError("evidence source provenance invalid")
        if item.created_at > snapshot.as_of or (
            item.observed_at is not None and item.observed_at > snapshot.as_of
        ):
            raise ValueError("evidence timestamp is later than as_of")
        source = projections.get(item.source_ref)
        if source is not None:
            if (item.source_phase != source.source_phase
                    or item.availability_status != source.availability_status
                    or item.freshness_status != source.freshness_status
                    or item.quality_status != source.quality_status
                    or item.coverage_status != source.coverage_status
                    or item.observed_at != source.observed_at
                    or item.provenance != f"source_type={source.source_type};schema={source.source_schema_version};sha256={source.canonical_digest}"):
                raise ValueError("evidence source status/provenance mismatch")
        unavailable = item.availability_status in {
            PolicyDataStatusV1.NOT_AVAILABLE, PolicyDataStatusV1.NOT_CONFIGURED,
            PolicyDataStatusV1.ERROR,
        }
        if unavailable:
            missing.append(MissingEvidenceV1(
                evidence_type=item.evidence_type, source_status=item.availability_status,
                reason=f"{item.source_ref}: {item.availability_status.value}",
                source_timestamp=item.observed_at,
                captured_at=source.captured_at if source is not None else None,
            ))
        if (unavailable or item.availability_status in {
                PolicyDataStatusV1.PARTIAL, PolicyDataStatusV1.STALE,
            } or item.quality_status is not EvidenceQualityV1.VALID
                or item.freshness_status is not EvidenceFreshnessV1.FRESH
                or item.coverage_status in {
                    PolicyCoverageStatusV1.PARTIAL, PolicyCoverageStatusV1.UNKNOWN,
                    PolicyCoverageStatusV1.NOT_AVAILABLE,
                } or (item.observed_at is None and source is not None)):
            degraded.append(item.evidence_id)
        if unavailable and item.direction is not EvidenceDirectionV1.UNKNOWN:
            raise ValueError("unavailable evidence cannot have directional meaning")
    present = {item.evidence_type for item in items}
    for category in _REQUIRED_CATEGORIES:
        if category not in present:
            missing.append(MissingEvidenceV1(
                evidence_type=category, source_status=PolicyDataStatusV1.NOT_AVAILABLE,
                reason=f"No {category.value} projection in immutable snapshot",
                source_timestamp=None, captured_at=None,
            ))
    from .paper_v1 import is_paper_v1, CORE_SOURCE_TYPES, paper_vetoes
    vetoes = []
    if is_paper_v1(policy_manifest):
        core_items = tuple(item for item in items if projections.get(item.source_ref) is not None
            and projections[item.source_ref].source_type in CORE_SOURCE_TYPES)
        core_ids = {item.evidence_id for item in core_items}
        core_categories = {item.evidence_type for item in core_items}
        missing = [m for m in missing if any(m.reason.startswith(i.source_ref + ":") for i in core_items)]
        degraded = [eid for eid in degraded if eid in core_ids]
        degraded.extend(i.evidence_id for i in core_items if i.semantic_code.endswith("UNKNOWN"))
        present_types = {projections[i.source_ref].source_type for i in core_items}
        for source_type in sorted(CORE_SOURCE_TYPES - present_types):
            missing.append(MissingEvidenceV1(evidence_type=EvidenceTypeV1.PRICE_STRUCTURE,
                source_status=PolicyDataStatusV1.NOT_AVAILABLE,reason=f"Required {source_type} missing",
                source_timestamp=None,captured_at=None))
        vetoes.extend(paper_vetoes(snapshot))
    # Generic approved veto rules are consumed after provenance and scope validation.
    from .patterns import _evaluate
    rules = {rule.predicate_id: rule for rule in policy_manifest.manifest.policy_content.predicates}
    patterns = {p.pattern_type for p in policy_manifest.manifest.policy_content.enabled_patterns if p.timeframe == snapshot.timeframe}
    for rule in sorted(policy_manifest.manifest.policy_content.hard_veto_rules,key=lambda r:r.precedence):
        if rule.applies_to_pattern_types and not patterns.intersection(rule.applies_to_pattern_types):
            continue
        results = [_evaluate(rules[ref],items,frozenset(degraded)) for ref in rule.predicate_ids]
        if any(r.truth == "TRUE" for r in results): vetoes.append(rule.veto_code.value)
        elif any(r.truth == "UNKNOWN" for r in results): vetoes.append("HARD_VETO_EVIDENCE_UNKNOWN")
    return ValidationResultV1(
        evaluation_id=snapshot.evaluation_id,
        evidence_ids=tuple(item.evidence_id for item in items),
        missing=tuple(missing), degraded=tuple(dict.fromkeys(degraded)), hard_vetoes=tuple(dict.fromkeys(vetoes)),
    )
