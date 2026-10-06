"""Strict, offline Phase 9 replay over a pinned Data Layer parent bundle."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Literal, Mapping
from uuid import UUID

from quant_data_layer.replay import ReplayDataset, load_replay_dataset

from .canonical import canonical_sha256
from .contracts import (
    EvaluationSnapshotV1, GitSha, JevConflictNoteV1,
    JevConflictSeverityV1, JevDegradationNoteV1, JevDominantContextV1,
    JevEvidenceAssessmentV1, JevRelationV1, JevReviewStatusV1,
    JevReviewV1, Phase9ReplayArtifactRefV1, PolicyManifestRefV1,
    Sha256Hex, Stage1CandidateEventV1,
)
from .policy import ApprovedPolicyManifestV1, load_approved_policy_manifest
from .runtime import _load_snapshot


_HASH_NAMES = (
    "evaluation_snapshot_hash", "evidence_items_digest", "evidence_chain_digest",
    "pattern_matches_digest", "decision_candidate_digest", "input_snapshot_hash",
)


def _exact(value: object, names: tuple[str, ...] | set[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(names):
        raise ValueError(f"{label} has invalid fields")
    return value


def _read_json(path: Path, *, limit: int = 1_048_576) -> dict[str, object]:
    raw = path.read_bytes()
    if len(raw) > limit:
        raise ValueError("Phase 9 replay artifact exceeds byte limit")

    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate Phase 9 replay key")
            result[key] = value
        return result

    parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicates,
                        parse_float=Decimal)
    if not isinstance(parsed, dict):
        raise ValueError("Phase 9 replay artifact must be an object")
    return parsed


def _utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("replay timestamp must be explicit UTC")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise ValueError("replay timestamp must be explicit UTC")
    return result.astimezone(timezone.utc)


def _artifact(value: object) -> Phase9ReplayArtifactRefV1:
    row = _exact(value, ("path", "sha256"), "artifact reference")
    return Phase9ReplayArtifactRefV1(path=row["path"], sha256=row["sha256"])


def _resolve_artifact(root: Path, reference: Phase9ReplayArtifactRefV1) -> Path:
    if "\\" in reference.path or "//" in reference.path:
        raise ValueError("invalid replay artifact path")
    target = (root / reference.path).resolve()
    if root not in target.parents:
        raise ValueError("Phase 9 replay artifact escapes project root")
    raw = target.read_bytes()
    if len(raw) > 1_048_576 or hashlib.sha256(raw).hexdigest() != reference.sha256:
        raise ValueError("Phase 9 replay artifact digest mismatch")
    return target


@dataclass(frozen=True, slots=True)
class DataLayerReplayParentRefV1:
    manifest_path: str
    replay_version: Literal["DATA_LAYER_REPLAY_V1"]
    parent_data_layer_bundle_hash: Sha256Hex
    git_sha: str

    def __post_init__(self) -> None:
        if self.replay_version != "DATA_LAYER_REPLAY_V1":
            raise ValueError("invalid Data Layer replay parent version")
        Phase9ReplayArtifactRefV1(self.manifest_path, "0" * 64)
        object.__setattr__(self, "parent_data_layer_bundle_hash",
                           Sha256Hex(self.parent_data_layer_bundle_hash))
        if not isinstance(self.git_sha, str) or len(self.git_sha) not in {40, 64}:
            raise ValueError("invalid parent git SHA")
        int(self.git_sha, 16)


@dataclass(frozen=True, slots=True)
class Stage1CandidateFixtureV1:
    schema: Literal["PHASE9_STAGE1_CANDIDATE_FIXTURE_V1"]
    candidate: Stage1CandidateEventV1
    candidate_digest: Sha256Hex

    def __post_init__(self) -> None:
        if self.schema != "PHASE9_STAGE1_CANDIDATE_FIXTURE_V1":
            raise ValueError("invalid candidate fixture schema")
        object.__setattr__(self, "candidate_digest", Sha256Hex(self.candidate_digest))
        if canonical_sha256(self.candidate) != self.candidate_digest:
            raise ValueError("candidate fixture digest mismatch")


@dataclass(frozen=True, slots=True)
class EvaluationSnapshotFixtureV1:
    schema: Literal["PHASE9_EVALUATION_SNAPSHOT_FIXTURE_V1"]
    snapshot: EvaluationSnapshotV1
    snapshot_digest: Sha256Hex

    def __post_init__(self) -> None:
        if self.schema != "PHASE9_EVALUATION_SNAPSHOT_FIXTURE_V1":
            raise ValueError("invalid snapshot fixture schema")
        object.__setattr__(self, "snapshot_digest", Sha256Hex(self.snapshot_digest))
        if self.snapshot.snapshot_digest != self.snapshot_digest:
            raise ValueError("snapshot fixture digest mismatch")


@dataclass(frozen=True, slots=True)
class RecordedJevReviewFixtureV1:
    schema: Literal["PHASE9_RECORDED_JEV_REVIEW_FIXTURE_V1"]
    review: JevReviewV1
    request_digest: Sha256Hex
    response_digest: Sha256Hex | None

    def __post_init__(self) -> None:
        if self.schema != "PHASE9_RECORDED_JEV_REVIEW_FIXTURE_V1":
            raise ValueError("invalid Jev fixture schema")
        object.__setattr__(self, "request_digest", Sha256Hex(self.request_digest))
        if self.response_digest is not None:
            object.__setattr__(self, "response_digest", Sha256Hex(self.response_digest))
        if (self.review.request_digest != self.request_digest
                or self.review.response_digest != self.response_digest):
            raise ValueError("recorded Jev digest mismatch")


@dataclass(frozen=True, slots=True)
class ExpectedPhase9HashesV1:
    evaluation_snapshot_hash: Sha256Hex
    evidence_items_digest: Sha256Hex
    evidence_chain_digest: Sha256Hex
    pattern_matches_digest: Sha256Hex
    decision_candidate_digest: Sha256Hex
    input_snapshot_hash: Sha256Hex

    def __post_init__(self) -> None:
        for name in _HASH_NAMES:
            object.__setattr__(self, name, Sha256Hex(getattr(self, name)))


@dataclass(frozen=True, slots=True)
class Phase9ReplayManifestV1:
    schema: Literal["PHASE9_REPLAY_V1"]
    version: Literal["v1"]
    parent: DataLayerReplayParentRefV1
    stage1_candidate: Stage1CandidateFixtureV1
    evaluation_snapshot: EvaluationSnapshotFixtureV1
    policy_manifest: PolicyManifestRefV1
    review_mode: Literal["RECORDED", "NO_REVIEW", "REVIEW_NOT_AVAILABLE"]
    no_review_reason: Literal["NOT_REQUIRED"] | None
    recorded_jev_review: RecordedJevReviewFixtureV1 | None
    expected_hashes: ExpectedPhase9HashesV1
    migration_version: Literal["016"]
    code_version: GitSha

    def __post_init__(self) -> None:
        if self.schema != "PHASE9_REPLAY_V1" or self.version != "v1":
            raise ValueError("invalid Phase 9 replay version")
        if self.migration_version != "016":
            raise ValueError("invalid Phase 9 replay migration version")
        object.__setattr__(self, "code_version", GitSha(self.code_version))
        if self.review_mode == "NO_REVIEW":
            if self.recorded_jev_review is not None or self.no_review_reason != "NOT_REQUIRED":
                raise ValueError("NO_REVIEW requires an explicit NOT_REQUIRED sentinel")
        elif self.review_mode in {"RECORDED", "REVIEW_NOT_AVAILABLE"}:
            if self.recorded_jev_review is None or self.no_review_reason is not None:
                raise ValueError("recorded review mode requires a review fixture")
            completed = self.recorded_jev_review.review.status is JevReviewStatusV1.COMPLETED
            if completed != (self.review_mode == "RECORDED"):
                raise ValueError("recorded review status does not match mode")
        else:
            raise ValueError("invalid review mode")
        snapshot = self.evaluation_snapshot.snapshot
        if (snapshot.candidate_event != self.stage1_candidate.candidate
                or snapshot.code_version != self.code_version):
            raise ValueError("replay candidate or code version does not match snapshot")
        if (self.recorded_jev_review is not None
                and self.recorded_jev_review.review.evaluation_id != snapshot.evaluation_id):
            raise ValueError("recorded Jev evaluation identity mismatch")
        if self.recorded_jev_review is not None:
            from .persistence import chain_id_for

            if self.recorded_jev_review.review.evidence_chain_id != chain_id_for(snapshot.evaluation_id):
                raise ValueError("recorded Jev evidence chain identity mismatch")


@dataclass(frozen=True, slots=True)
class Phase9ReplayBundleV1:
    manifest: Phase9ReplayManifestV1
    parent_dataset: ReplayDataset
    approved_policy: ApprovedPolicyManifestV1


@dataclass(frozen=True, slots=True)
class Phase9ReplayResultV1:
    schema: Literal["PHASE9_REPLAY_RESULT_V1"]
    passed: bool
    PHASE9_DETERMINISTIC_REPLAY_PASS: bool
    hashes_compared: tuple[str, ...]
    hashes_match: bool
    provider_calls: int
    ai_calls: int


def _parse_jev(value: object) -> JevReviewV1:
    row = _exact(value, {field.name for field in fields(JevReviewV1)}, "Jev review")

    def assessment(item: object) -> JevEvidenceAssessmentV1:
        part = _exact(item, ("evidence_ids", "assessment"), "Jev assessment")
        return JevEvidenceAssessmentV1(tuple(UUID(x) for x in part["evidence_ids"]),
                                       part["assessment"])

    def conflict(item: object) -> JevConflictNoteV1:
        part = _exact(item, ("reason_code", "evidence_ids", "detail"), "Jev conflict")
        return JevConflictNoteV1(part["reason_code"],
                                 tuple(UUID(x) for x in part["evidence_ids"]), part["detail"])

    def degradation(item: object) -> JevDegradationNoteV1:
        part = _exact(item, ("reason_code", "detail"), "Jev degradation")
        return JevDegradationNoteV1(part["reason_code"], part["detail"])

    return JevReviewV1(
        **{**row,
           "evaluation_id": UUID(row["evaluation_id"]),
           "review_id": UUID(row["review_id"]),
           "status": JevReviewStatusV1(row["status"]),
           "evidence_chain_id": UUID(row["evidence_chain_id"]),
           "relation": JevRelationV1(row["relation"]) if row["relation"] else None,
           "conflict_severity": JevConflictSeverityV1(row["conflict_severity"])
               if row["conflict_severity"] else None,
           "dominant_context": JevDominantContextV1(row["dominant_context"])
               if row["dominant_context"] else None,
           "supporting_assessments": tuple(map(assessment, row["supporting_assessments"])),
           "conflicting_assessments": tuple(map(assessment, row["conflicting_assessments"])),
           "unresolved_conflicts": tuple(map(conflict, row["unresolved_conflicts"])),
           "degradation_notes": tuple(map(degradation, row["degradation_notes"])),
           "referenced_evidence_ids": tuple(UUID(x) for x in row["referenced_evidence_ids"]),
           "created_at": _utc(row["created_at"])}
    )


def load_phase9_replay_manifest(path: Path, *, project_root: Path) -> Phase9ReplayBundleV1:
    """Load and validate all typed fixtures before any replay computation."""
    root = project_root.resolve()
    raw = _read_json(path)
    _exact(raw, {field.name for field in fields(Phase9ReplayManifestV1)}, "Phase 9 manifest")
    parent_raw = _exact(raw["parent"],
                        {field.name for field in fields(DataLayerReplayParentRefV1)}, "parent")
    parent = DataLayerReplayParentRefV1(**parent_raw)
    parent_path = (root / parent.manifest_path).resolve()
    if root not in parent_path.parents:
        raise ValueError("parent replay manifest escapes project root")
    parent_dataset = load_replay_dataset(parent_path, project_root=root)
    if (parent_dataset.manifest.replay_version != parent.replay_version
            or parent_dataset.dataset_sha256 != parent.parent_data_layer_bundle_hash
            or parent_dataset.manifest.git_sha != parent.git_sha):
        raise ValueError("parent Data Layer replay binding mismatch")

    snapshot_raw = _exact(raw["evaluation_snapshot"],
                          {field.name for field in fields(EvaluationSnapshotFixtureV1)},
                          "snapshot fixture")
    snapshot = _load_snapshot(snapshot_raw["snapshot"])
    snapshot_fixture = EvaluationSnapshotFixtureV1(
        schema=snapshot_raw["schema"], snapshot=snapshot,
        snapshot_digest=snapshot_raw["snapshot_digest"],
    )
    candidate_raw = _exact(raw["stage1_candidate"],
                           {field.name for field in fields(Stage1CandidateFixtureV1)},
                           "candidate fixture")
    if canonical_sha256(candidate_raw["candidate"]) != canonical_sha256(snapshot.candidate_event):
        raise ValueError("candidate fixture does not match snapshot")
    candidate_fixture = Stage1CandidateFixtureV1(
        schema=candidate_raw["schema"], candidate=snapshot.candidate_event,
        candidate_digest=candidate_raw["candidate_digest"],
    )

    policy_raw = _exact(raw["policy_manifest"],
                        {field.name for field in fields(PolicyManifestRefV1)},
                        "policy reference")
    policy_ref = PolicyManifestRefV1(
        **{**policy_raw,
           "manifest_artifact": _artifact(policy_raw["manifest_artifact"]),
           "approval_artifact": _artifact(policy_raw["approval_artifact"])}
    )
    approved = load_approved_policy_manifest(
        _resolve_artifact(root, policy_ref.manifest_artifact),
        _resolve_artifact(root, policy_ref.approval_artifact),
        expected_commit=raw["code_version"],
    )
    if (approved.manifest_version != policy_ref.manifest_version
            or approved.manifest_digest != policy_ref.manifest_digest
            or canonical_sha256(approved.approval.model_dump(mode="python")) != policy_ref.approval_digest):
        raise ValueError("approved policy replay reference mismatch")
    if snapshot.identity.policy_generation != f"{approved.manifest_version}:{approved.manifest_digest}":
        raise ValueError("replay snapshot approved policy mismatch")

    review_fixture = None
    if raw["recorded_jev_review"] is not None:
        review_raw = _exact(raw["recorded_jev_review"],
                            {field.name for field in fields(RecordedJevReviewFixtureV1)},
                            "Jev fixture")
        review_fixture = RecordedJevReviewFixtureV1(
            schema=review_raw["schema"], review=_parse_jev(review_raw["review"]),
            request_digest=review_raw["request_digest"],
            response_digest=review_raw["response_digest"],
        )
    expected_raw = _exact(raw["expected_hashes"], _HASH_NAMES, "expected hashes")
    manifest = Phase9ReplayManifestV1(
        **{**raw, "parent": parent, "stage1_candidate": candidate_fixture,
           "evaluation_snapshot": snapshot_fixture, "policy_manifest": policy_ref,
           "recorded_jev_review": review_fixture,
           "expected_hashes": ExpectedPhase9HashesV1(**expected_raw)}
    )
    return Phase9ReplayBundleV1(manifest, parent_dataset, approved)


def compute_phase9_hashes(bundle: Phase9ReplayBundleV1) -> ExpectedPhase9HashesV1:
    """Pure reconstruction from the frozen snapshot and approved policy only."""
    from .contracts import EvidenceChainV1, PatternMatchStatusV1
    from .decision import build_decision_candidate, input_snapshot_hash_for
    from .evidence import build_chain_draft, build_evidence
    from .patterns import match_patterns
    from .validator import validate_evidence

    manifest = bundle.manifest
    snapshot = manifest.evaluation_snapshot.snapshot
    policy = bundle.approved_policy
    review = manifest.recorded_jev_review.review if manifest.recorded_jev_review else None
    items = build_evidence(snapshot=snapshot, policy_manifest=policy)
    validation = validate_evidence(
        snapshot=snapshot, evidence_items=items, policy_manifest=policy,
    )
    draft = build_chain_draft(
        snapshot=snapshot, evidence_items=items, validation=validation,
    )
    matches = match_patterns(
        evidence_items=items, validation=validation,
        policy_manifest=policy, timeframe=snapshot.timeframe,
    )
    chain = EvidenceChainV1(
        stage1_candidate_id=snapshot.stage1_candidate_id, symbol=snapshot.symbol,
        evaluation_id=snapshot.evaluation_id, evaluation_time=snapshot.evaluation_time,
        timeframe=snapshot.timeframe, supporting=draft.supporting,
        conflicting=draft.conflicting, neutral=draft.neutral,
        missing=draft.missing, degraded=draft.degraded,
        hard_vetoes=draft.hard_vetoes,
        matched_patterns=tuple(
            match.pattern_match_id for match in matches
            if match.status is PatternMatchStatusV1.MATCHED
        ),
        jev_review_required=bool(
            draft.conflicting
            or any(match.status is PatternMatchStatusV1.CONFLICTED for match in matches)
        ),
        evidence_schema_version=snapshot.evidence_schema_version,
        evaluation_snapshot_hash=snapshot.snapshot_digest,
        input_snapshot_hash=input_snapshot_hash_for(
            snapshot_hash=snapshot.snapshot_digest, jev_review=review,
        ),
    )
    decision, _ = build_decision_candidate(
        snapshot=snapshot, evidence_chain=chain, pattern_matches=matches,
        jev_review=review, policy_manifest=policy, now=snapshot.as_of,
    )
    return ExpectedPhase9HashesV1(
        evaluation_snapshot_hash=snapshot.snapshot_digest,
        evidence_items_digest=canonical_sha256(items),
        evidence_chain_digest=canonical_sha256(chain),
        pattern_matches_digest=canonical_sha256(matches),
        decision_candidate_digest=canonical_sha256(decision),
        input_snapshot_hash=chain.input_snapshot_hash,
    )


def replay_phase9(bundle: Phase9ReplayBundleV1) -> Phase9ReplayResultV1:
    actual = compute_phase9_hashes(bundle)
    match = actual == bundle.manifest.expected_hashes
    return Phase9ReplayResultV1(
        schema="PHASE9_REPLAY_RESULT_V1", passed=match,
        PHASE9_DETERMINISTIC_REPLAY_PASS=match,
        hashes_compared=_HASH_NAMES, hashes_match=match,
        provider_calls=0, ai_calls=0,
    )
