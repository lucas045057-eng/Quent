from __future__ import annotations

import json
from pathlib import Path

import pytest

from quant_phase9.canonical import canonical_sha256
from quant_phase9.policy import (
    ApprovedPolicyManifestV1,
    PolicyContentV1,
    PredicateRuleV1,
    load_approved_policy_manifest,
    policy_for,
)
from quant_phase9.contracts import PolicyDirectionV1


CONTENT = {
    "enabled_patterns": [],
    "predicates": [],
    "freshness_rules": [],
    "coverage_rules": [],
    "hard_veto_rules": [],
    "soft_degradation_rules": [],
    "confidence_ceiling_rules": [],
    "ttl_rules": [],
    "revalidation_rules": [],
    "material_change_rules": [],
    "jev_conflict_rules": [],
    "numeric_units": [],
    "decimal_rules": [],
    "rounding_rules": [],
    "hard_veto_precedence": [],
}


def _files(tmp_path: Path, *, status: str = "APPROVED", digest: str | None = None):
    true_digest = str(canonical_sha256(CONTENT))
    manifest = {
        "schema": "PHASE9_POLICY_MANIFEST_V1",
        "manifest_version": "1.0.0",
        "created_at": "2026-09-28T00:00:00Z",
        "policy_content": CONTENT,
        "manifest_digest": digest or true_digest,
    }
    approval = {
        "schema": "PHASE9_POLICY_APPROVAL_V1",
        "manifest_version": "1.0.0",
        "manifest_digest": true_digest,
        "approval_status": status,
        "approved_at": "2026-09-28T00:01:00Z",
        "approved_by": "fixture-human",
        "approved_commit": "b" * 40,
    }
    manifest_path = tmp_path / "policy.json"
    approval_path = tmp_path / "approval.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    approval_path.write_text(json.dumps(approval), encoding="utf-8")
    return manifest_path, approval_path


def test_only_matching_approved_artifacts_load(tmp_path):
    manifest_path, approval_path = _files(tmp_path)
    loaded = load_approved_policy_manifest(
        manifest_path, approval_path, expected_commit="b" * 40,
    )
    assert isinstance(loaded, ApprovedPolicyManifestV1)
    assert loaded.manifest.manifest_digest == str(canonical_sha256(CONTENT))
    assert isinstance(loaded.manifest.policy_content, PolicyContentV1)
    assert loaded.manifest_version == "1.0.0"
    assert loaded.manifest_digest == str(canonical_sha256(CONTENT))
    assert policy_for(
        manifest=loaded, pattern_type="TREND_CONTINUATION",
        timeframe="15m", direction=PolicyDirectionV1.LONG,
    ) is None


@pytest.mark.parametrize("status", ["REVOKED", "DRAFT"])
def test_revoked_or_unknown_approval_fails_closed(tmp_path, status):
    manifest_path, approval_path = _files(tmp_path, status=status)
    with pytest.raises((ValueError, TypeError)):
        load_approved_policy_manifest(
            manifest_path, approval_path, expected_commit="b" * 40,
        )


def test_policy_digest_covers_content_and_rejects_tampering(tmp_path):
    manifest_path, approval_path = _files(tmp_path, digest="0" * 64)
    with pytest.raises(ValueError, match="digest"):
        load_approved_policy_manifest(
            manifest_path, approval_path, expected_commit="b" * 40,
        )


def test_policy_records_reject_untyped_units_and_missing_numeric_threshold():
    with pytest.raises((ValueError, TypeError)):
        PredicateRuleV1(
            predicate_id="oi-growth", source_phase="PHASE2", source_type="OPEN_INTEREST",
            evidence_type="OPEN_INTEREST_STRUCTURE", accepted_semantic_codes=("OI_RISING",),
            operator="GT", threshold=None, upper_threshold=None, unit="NONE",
            missing_behavior="FAIL_CLOSED",
        )
    with pytest.raises((ValueError, TypeError)):
        PredicateRuleV1(
            predicate_id="oi-growth", source_phase="PHASE2", source_type="OPEN_INTEREST",
            evidence_type="OPEN_INTEREST_STRUCTURE", accepted_semantic_codes=("OI_RISING",),
            operator="GT", threshold="1", upper_threshold=None, unit="UNKNOWN_UNIT",
            missing_behavior="FAIL_CLOSED",
        )


def test_missing_production_approval_file_never_activates_draft(tmp_path):
    manifest_path, approval_path = _files(tmp_path)
    approval_path.unlink()
    with pytest.raises((FileNotFoundError, ValueError)):
        load_approved_policy_manifest(
            manifest_path, approval_path, expected_commit="b" * 40,
        )


def test_approval_commit_must_match_runtime_expected_commit(tmp_path):
    manifest_path, approval_path = _files(tmp_path)
    with pytest.raises(ValueError, match="commit"):
        load_approved_policy_manifest(
            manifest_path, approval_path, expected_commit="c" * 40,
        )


def test_exact_runtime_commit_binding_loads(tmp_path):
    manifest_path, approval_path = _files(tmp_path)

    loaded = load_approved_policy_manifest(
        manifest_path, approval_path, expected_commit="b" * 40,
    )

    assert loaded.code_version == "b" * 40
    assert loaded.approval.approved_commit == "b" * 40


def test_whitespace_approver_is_rejected_by_loader(tmp_path):
    import json

    manifest_path, approval_path = _files(tmp_path)
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    approval["approved_by"] = "   "
    approval_path.write_text(json.dumps(approval), encoding="utf-8")

    with pytest.raises(ValueError, match="approved_by"):
        load_approved_policy_manifest(
            manifest_path, approval_path, expected_commit="b" * 40,
        )
