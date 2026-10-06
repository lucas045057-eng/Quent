from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from quant_phase9.replay import (
    compute_phase9_hashes, load_phase9_replay_manifest, replay_phase9,
)


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tests/fixtures/phase9/manifest.json"


def test_no_review_fixture_replays_all_six_hashes_without_external_calls():
    bundle = load_phase9_replay_manifest(MANIFEST, project_root=ROOT)
    first = replay_phase9(bundle)
    second = replay_phase9(bundle)
    assert first == second
    assert first.passed is True
    assert first.hashes_match is True
    assert first.provider_calls == first.ai_calls == 0
    assert len(first.hashes_compared) == 6


def test_standalone_fixture_files_match_embedded_typed_manifest():
    import json

    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    for field, filename in (
        ("stage1_candidate", "stage1_candidate.json"),
        ("evaluation_snapshot", "evaluation_snapshot.json"),
    ):
        standalone = json.loads((MANIFEST.parent / filename).read_text(encoding="utf-8"))
        assert standalone == raw[field]


def test_replay_rejects_wrong_expected_digest():
    bundle = load_phase9_replay_manifest(MANIFEST, project_root=ROOT)
    wrong = replace(
        bundle.manifest.expected_hashes,
        evidence_chain_digest="0" * 64,
    )
    result = replay_phase9(replace(bundle, manifest=replace(bundle.manifest, expected_hashes=wrong)))
    assert result.passed is False
    assert result.hashes_match is False


def test_manifest_rejects_unknown_nested_field_and_parent_hash(tmp_path):
    import json

    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    raw["parent"]["unknown"] = "bad"
    copy = tmp_path / "bad.json"
    copy.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        load_phase9_replay_manifest(copy, project_root=ROOT)
    raw["parent"].pop("unknown")
    raw["parent"]["parent_data_layer_bundle_hash"] = "0" * 64
    copy.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        load_phase9_replay_manifest(copy, project_root=ROOT)


@pytest.mark.parametrize("mode", ("RECORDED", "REVIEW_NOT_AVAILABLE"))
def test_recorded_and_unavailable_review_modes_preserve_frozen_cause(tmp_path, mode):
    import json

    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    review = json.loads(
        (MANIFEST.parent / "recorded_jev_review.json").read_text(encoding="utf-8")
    )
    if mode == "REVIEW_NOT_AVAILABLE":
        review["review"].update(
            status="NOT_AVAILABLE", reason_code="GATEWAY_NOT_CONFIGURED",
            reason_detail="Synthetic unavailable review",
            response_digest=None, provider=None, model=None, model_version=None,
            relation=None, conflict_severity=None, dominant_context=None,
            reasoning_summary=None,
        )
        review["response_digest"] = None
    raw.update(review_mode=mode, no_review_reason=None, recorded_jev_review=review)
    path = tmp_path / "review.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    bundle = load_phase9_replay_manifest(path, project_root=ROOT)
    assert bundle.manifest.recorded_jev_review is not None
    if mode == "REVIEW_NOT_AVAILABLE":
        assert bundle.manifest.recorded_jev_review.review.reason_code == "GATEWAY_NOT_CONFIGURED"
    actual = compute_phase9_hashes(bundle)
    pinned = replace(bundle, manifest=replace(bundle.manifest, expected_hashes=actual))
    assert replay_phase9(pinned).passed is True


def test_review_mode_and_artifact_tamper_fail_closed(tmp_path):
    import json

    raw = json.loads(MANIFEST.read_text(encoding="utf-8"))
    raw["review_mode"] = "RECORDED"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="review fixture"):
        load_phase9_replay_manifest(path, project_root=ROOT)
    raw["review_mode"] = "NO_REVIEW"
    raw["policy_manifest"]["manifest_artifact"]["sha256"] = "0" * 64
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        load_phase9_replay_manifest(path, project_root=ROOT)
