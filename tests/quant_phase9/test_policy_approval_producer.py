from __future__ import annotations

import importlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from quant_phase9.canonical import canonical_sha256


MANIFEST = Path(__file__).parents[2] / "policies" / "phase9_policy_v1.json"


def _producer():
    try:
        module = importlib.import_module("quant_phase9.approval")
    except ModuleNotFoundError:
        pytest.fail("production approval producer module is missing")
    return module.create_approval_artifact


def test_producer_derives_approval_reference_from_frozen_manifest(tmp_path):
    create = _producer()
    source_before = MANIFEST.read_bytes()
    output = tmp_path / "approval.json"
    approved_at = datetime(2026, 9, 30, tzinfo=timezone.utc)

    approval = create(
        MANIFEST, output, approved_by="strategy-owner",
        approved_commit="6" * 40, approved_at=approved_at,
    )

    manifest = json.loads(source_before)
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["schema"] == "PHASE9_POLICY_APPROVAL_V1"
    assert result["manifest_version"] == manifest["manifest_version"]
    assert result["manifest_digest"] == str(canonical_sha256(manifest["policy_content"]))
    assert result["approved_commit"] == "6" * 40
    assert result["approved_by"] == "strategy-owner"
    assert approval.manifest_digest == result["manifest_digest"]
    assert MANIFEST.read_bytes() == source_before


def test_producer_rejects_empty_approver_without_creating_artifact(tmp_path):
    create = _producer()
    output = tmp_path / "approval.json"
    with pytest.raises(ValueError, match="approved_by"):
        create(MANIFEST, output, approved_by="  ", approved_commit="6" * 40)
    assert not output.exists()


def test_producer_never_overwrites_existing_approval(tmp_path):
    create = _producer()
    output = tmp_path / "approval.json"
    output.write_text("human-owned", encoding="utf-8")
    with pytest.raises(FileExistsError):
        create(MANIFEST, output, approved_by="owner", approved_commit="6" * 40)
    assert output.read_text(encoding="utf-8") == "human-owned"
