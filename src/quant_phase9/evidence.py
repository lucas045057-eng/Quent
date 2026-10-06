"""Deterministic, bounded Phase 9 evidence from one immutable snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from .canonical import canonical_json, canonical_sha256
from .contracts import (
    EvaluationSnapshotV1, EvidenceDirectionV1, EvidenceFreshnessV1,
    EvidenceItemV1, EvidenceQualityV1, EvidenceStrengthV1, EvidenceTypeV1,
    MissingEvidenceV1, PolicyCoverageStatusV1, PolicyDataStatusV1,
    SourcePhaseV1, SourceProjectionV1,
)
from .policy import ApprovedPolicyManifestV1
from .intake import _valid_event
from .sources import projection_id_for
from .validator import ValidationResultV1
from quant_features.core import exact_decimal


MAX_EVIDENCE_ITEMS_V1 = 64
_PHASE_TYPE = {
    SourcePhaseV1.PHASE1: EvidenceTypeV1.PRICE_STRUCTURE,
    SourcePhaseV1.PHASE2: EvidenceTypeV1.OPEN_INTEREST_STRUCTURE,
    SourcePhaseV1.PHASE3: EvidenceTypeV1.TRADE_FLOW,
    SourcePhaseV1.PHASE4: EvidenceTypeV1.LIQUIDATION_CONTEXT,
    SourcePhaseV1.PHASE5: EvidenceTypeV1.MARKET_REGIME,
    SourcePhaseV1.PHASE6: EvidenceTypeV1.ONCHAIN_SPOT_MACRO,
    SourcePhaseV1.PHASE7: EvidenceTypeV1.ONCHAIN_SPOT_MACRO,
    SourcePhaseV1.PHASE8: EvidenceTypeV1.OPTIONS_CONTEXT,
}
_UNAVAILABLE = {
    PolicyDataStatusV1.NOT_AVAILABLE, PolicyDataStatusV1.NOT_CONFIGURED,
    PolicyDataStatusV1.ERROR,
}


@dataclass(frozen=True, slots=True)
class EvidenceChainDraftV1:
    evaluation_id: UUID
    supporting: tuple[UUID, ...]
    conflicting: tuple[UUID, ...]
    neutral: tuple[UUID, ...]
    missing: tuple[MissingEvidenceV1, ...]
    degraded: tuple[UUID, ...]
    hard_vetoes: tuple[str, ...]
    evaluation_snapshot_hash: str


def _evidence_type(projection: SourceProjectionV1) -> EvidenceTypeV1:
    if projection.source_phase is SourcePhaseV1.PHASE2 and projection.source_type in {"FUNDING_RATE", "PAPER_FUNDING"}:
        return EvidenceTypeV1.FUNDING_BASIS_POSITIONING
    if projection.source_phase is SourcePhaseV1.PHASE4 and projection.source_type in {
        "FUNDING_RATE", "BASIS_SNAPSHOT", "LONG_SHORT_RATIO",
    }:
        return EvidenceTypeV1.FUNDING_BASIS_POSITIONING
    return _PHASE_TYPE[projection.source_phase]


def _number(value: object) -> Decimal | None:
    return exact_decimal(value)


def _meaning(projection: SourceProjectionV1) -> tuple[str, EvidenceDirectionV1, EvidenceStrengthV1, str]:
    payload = projection.canonical_payload
    if projection.availability_status in _UNAVAILABLE:
        return "SOURCE_NOT_AVAILABLE", EvidenceDirectionV1.UNKNOWN, EvidenceStrengthV1.UNKNOWN, (
            f"{projection.source_type} status={projection.availability_status.value}; value unavailable"
        )
    if projection.freshness_status is EvidenceFreshnessV1.STALE:
        return "SOURCE_STALE", EvidenceDirectionV1.UNKNOWN, EvidenceStrengthV1.UNKNOWN, (
            f"{projection.source_type} source timestamp is stale"
        )
    if projection.source_type == "OPEN_INTEREST":
        value = _number(payload.get("open_interest_usd"))
        if value is None or not payload.get("normalization_method"):
            return "OI_UNIT_UNVERIFIED", EvidenceDirectionV1.UNKNOWN, EvidenceStrengthV1.UNKNOWN, (
                "Open interest USD normalization unavailable; source value remains null or unverified"
            )
        return "OI_OBSERVED", EvidenceDirectionV1.UNKNOWN, EvidenceStrengthV1.WEAK, (
            "Open interest USD observation with source-declared normalization; no change direction inferred"
        )
    if projection.source_type == "TRADE_FLOW_WINDOW":
        unknown = _number(payload.get("unknown_trade_count"))
        delta = _number(payload.get("delta_base"))
        if delta is None or (unknown is not None and unknown > 0):
            return "DIRECTION_NOT_AVAILABLE", EvidenceDirectionV1.UNKNOWN, EvidenceStrengthV1.UNKNOWN, (
                "Trade side or delta is unknown; no buy/sell direction inferred"
            )
        if delta > 0:
            return "BUY_FLOW_DOMINANT", EvidenceDirectionV1.BULLISH, EvidenceStrengthV1.WEAK, (
                "Verified directional flow delta is positive in source base units"
            )
        if delta < 0:
            return "SELL_FLOW_DOMINANT", EvidenceDirectionV1.BEARISH, EvidenceStrengthV1.WEAK, (
                "Verified directional flow delta is negative in source base units"
            )
        return "FLOW_BALANCED", EvidenceDirectionV1.NEUTRAL, EvidenceStrengthV1.WEAK, (
            "Verified directional flow delta is exactly zero"
        )
    if projection.source_phase is SourcePhaseV1.PHASE8:
        return "OPTIONS_CONTEXT_OBSERVED", EvidenceDirectionV1.UNKNOWN, EvidenceStrengthV1.WEAK, (
            "Options context recorded; no IV/skew direction inferred without reviewed policy"
        )
    return "SOURCE_CONTEXT", EvidenceDirectionV1.UNKNOWN, EvidenceStrengthV1.UNKNOWN, (
        f"{projection.source_type} context recorded without a reviewed directional predicate"
    )


def _id(snapshot: EvaluationSnapshotV1, evidence_type: EvidenceTypeV1,
        semantic_code: str, source_ref: str, digest: str) -> UUID:
    return uuid5(NAMESPACE_URL, canonical_json({
        "evaluation_snapshot_hash": snapshot.snapshot_digest,
        "evidence_type": evidence_type,
        "semantic_code": semantic_code,
        "symbol": snapshot.symbol,
        "market": snapshot.market,
        "source_refs": (source_ref,),
        "source_digest": digest,
    }))


def build_evidence(
    *, snapshot: EvaluationSnapshotV1, policy_manifest: ApprovedPolicyManifestV1,
) -> tuple[EvidenceItemV1, ...]:
    if not isinstance(snapshot, EvaluationSnapshotV1):
        raise TypeError("snapshot must be EvaluationSnapshotV1")
    if not isinstance(policy_manifest, ApprovedPolicyManifestV1):
        raise TypeError("evidence requires an approved policy manifest")
    if snapshot.identity.policy_generation != f"{policy_manifest.manifest_version}:{policy_manifest.manifest_digest}":
        raise ValueError("snapshot policy generation does not bind approved policy")
    if snapshot.code_version != policy_manifest.code_version:
        raise ValueError("snapshot code version does not bind approved policy")
    candidate = snapshot.candidate_event
    if not _valid_event(candidate):
        raise ValueError("Stage 1 candidate provenance invalid")
    if (candidate.symbol != snapshot.symbol or candidate.market != snapshot.market
            or candidate.stage1_candidate_id != snapshot.stage1_candidate_id):
        raise ValueError("Stage 1 provenance does not match evaluation")
    if candidate.source_as_of > snapshot.as_of or candidate.created_at > snapshot.as_of:
        raise ValueError("Stage 1 candidate is later than as_of")
    stage1 = candidate.canonical_payload.get("stage1_projection")
    if not hasattr(stage1, "get"):
        raise ValueError("Stage 1 projection provenance is invalid")
    structure = stage1.get("structure")
    status = stage1.get("status")
    stage_direction = {
        "BULLISH": EvidenceDirectionV1.BULLISH,
        "BEARISH": EvidenceDirectionV1.BEARISH,
    }.get(structure, EvidenceDirectionV1.UNKNOWN)
    stage_code = {
        EvidenceDirectionV1.BULLISH: "TREND_UP",
        EvidenceDirectionV1.BEARISH: "TREND_DOWN",
    }.get(stage_direction, "DIRECTION_NOT_AVAILABLE")
    stage_status = PolicyDataStatusV1(status)
    if stage_status is not PolicyDataStatusV1.AVAILABLE:
        stage_direction = EvidenceDirectionV1.UNKNOWN
        stage_code = "SOURCE_NOT_AVAILABLE"
    stage_ref = f"phase1:stage1_candidate/{candidate.event_id}"
    items = [EvidenceItemV1(
        evidence_id=_id(snapshot, EvidenceTypeV1.PRICE_STRUCTURE, stage_code,
                        stage_ref, str(candidate.canonical_payload_digest)),
        evaluation_id=snapshot.evaluation_id, symbol=snapshot.symbol,
        evidence_type=EvidenceTypeV1.PRICE_STRUCTURE, semantic_code=stage_code,
        direction=stage_direction,
        strength=(EvidenceStrengthV1.WEAK if stage_direction is not EvidenceDirectionV1.UNKNOWN
                  else EvidenceStrengthV1.UNKNOWN),
        observed_at=candidate.source_as_of, availability_status=stage_status,
        freshness_status=(EvidenceFreshnessV1.FRESH if stage_status is PolicyDataStatusV1.AVAILABLE
                          else EvidenceFreshnessV1.UNKNOWN),
        quality_status=(EvidenceQualityV1.VALID if stage_status is PolicyDataStatusV1.AVAILABLE
                        else EvidenceQualityV1.UNKNOWN),
        coverage_status=None, source_phase=SourcePhaseV1.PHASE1,
        source_ref=stage_ref, provider=None,
        provenance=f"source_type=STAGE1_CANDIDATE;schema={candidate.event_schema_version};sha256={candidate.canonical_payload_digest}",
        raw_refs=(stage_ref,), interpretation=f"Stage 1 structure={structure}; status={status}",
        schema_version=snapshot.evidence_schema_version, created_at=snapshot.as_of,
    )]
    if len(snapshot.source_projections) + len(items) > MAX_EVIDENCE_ITEMS_V1:
        raise ValueError("Phase 9 evidence exceeds 64 items")
    for projection in snapshot.source_projections:
        if (projection.evaluation_id != snapshot.evaluation_id
                or projection.symbol != snapshot.symbol or projection.market != snapshot.market):
            raise ValueError("source projection identity/provenance mismatch")
        if canonical_sha256(projection.canonical_payload) != projection.canonical_digest:
            raise ValueError("source projection digest/provenance mismatch")
        if projection.projection_id != projection_id_for(
            snapshot.evaluation_id, projection.source_ref, str(projection.canonical_digest)
        ):
            raise ValueError("source projection ID/provenance mismatch")
        for field in ("event_time", "observed_at", "captured_at", "processed_at", "available_at"):
            value = getattr(projection, field)
            if value is not None and value > snapshot.as_of:
                raise ValueError(f"source projection {field} is later than as_of")
        evidence_type = _evidence_type(projection)
        from .paper_v1 import is_paper_v1, paper_semantics
        derived = paper_semantics(projection, snapshot) if is_paper_v1(policy_manifest) else None
        numeric_value = numeric_unit = None
        if derived is None:
            semantic_code, direction, strength, interpretation = _meaning(projection)
        else:
            semantic_code, direction, strength, interpretation, numeric_value, numeric_unit = derived
        if (projection.availability_status is not PolicyDataStatusV1.AVAILABLE
                or projection.quality_status is not EvidenceQualityV1.VALID
                or projection.freshness_status is not EvidenceFreshnessV1.FRESH
                or projection.coverage_status in {
                    PolicyCoverageStatusV1.PARTIAL, PolicyCoverageStatusV1.UNKNOWN,
                    PolicyCoverageStatusV1.NOT_AVAILABLE,
                }):
            if direction is not EvidenceDirectionV1.UNKNOWN:
                direction = EvidenceDirectionV1.UNKNOWN
                semantic_code = "DIRECTION_NOT_AVAILABLE"
                strength = EvidenceStrengthV1.UNKNOWN
                interpretation = f"{projection.source_type} source status/quality/coverage prevents directional inference"
        payload = projection.canonical_payload
        provider = payload.get("exchange") or payload.get("provider") or payload.get("source")
        if provider is not None and (not isinstance(provider, str) or not provider.strip()):
            provider = None
        items.append(EvidenceItemV1(
            evidence_id=_id(snapshot, evidence_type, semantic_code,
                            projection.source_ref, str(projection.canonical_digest)),
            evaluation_id=snapshot.evaluation_id, symbol=snapshot.symbol,
            evidence_type=evidence_type, semantic_code=semantic_code,
            direction=direction, strength=strength,
            numeric_value=numeric_value, numeric_unit=numeric_unit,
            observed_at=projection.observed_at,
            availability_status=projection.availability_status,
            freshness_status=projection.freshness_status,
            quality_status=projection.quality_status,
            coverage_status=projection.coverage_status,
            source_phase=projection.source_phase,
            source_ref=projection.source_ref, provider=provider,
            provenance=f"source_type={projection.source_type};schema={projection.source_schema_version};sha256={projection.canonical_digest}",
            raw_refs=(projection.source_ref,),
            interpretation=interpretation, schema_version=snapshot.evidence_schema_version,
            created_at=snapshot.as_of,
        ))
    return tuple(items)


def build_chain_draft(
    *, snapshot: EvaluationSnapshotV1, evidence_items: Sequence[EvidenceItemV1],
    validation: ValidationResultV1,
) -> EvidenceChainDraftV1:
    if not isinstance(validation, ValidationResultV1):
        raise TypeError("validation must be ValidationResultV1")
    if validation.evaluation_id != snapshot.evaluation_id:
        raise ValueError("validation evaluation ID mismatch")
    items = tuple(evidence_items)
    if tuple(item.evidence_id for item in items) != validation.evidence_ids:
        raise ValueError("validation evidence IDs do not match supplied items")
    stage = next((item for item in items if item.source_ref.startswith("phase1:stage1_candidate/")), None)
    direction = stage.direction if stage is not None else EvidenceDirectionV1.UNKNOWN
    supporting: list[UUID] = []
    conflicting: list[UUID] = []
    neutral: list[UUID] = []
    for item in items:
        if item.direction in {EvidenceDirectionV1.BULLISH, EvidenceDirectionV1.BEARISH}:
            if item.direction is direction:
                supporting.append(item.evidence_id)
            else:
                conflicting.append(item.evidence_id)
        else:
            neutral.append(item.evidence_id)
    return EvidenceChainDraftV1(
        evaluation_id=snapshot.evaluation_id, supporting=tuple(supporting),
        conflicting=tuple(conflicting), neutral=tuple(neutral),
        missing=validation.missing, degraded=validation.degraded,
        hard_vetoes=validation.hard_vetoes,
        evaluation_snapshot_hash=str(snapshot.snapshot_digest),
    )
