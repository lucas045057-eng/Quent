import json
import pytest
from tests.strategies.test_execution_policy import NOW
from strategies.execution.execution_policy import ExecutionPolicyV2

def test_v2_manifest_binds_execution_policy_and_loads_through_existing_approval(tmp_path):
    from strategies.integration.policy_manifest import build_v2_manifest, is_strategy_v2
    from quant_phase9.policy import load_approved_policy_manifest
    policy=ExecutionPolicyV2(allowed_symbols=("SOLUSDT",))
    manifest=build_v2_manifest(policy,created_at=NOW)
    mp=tmp_path/"manifest.json";ap=tmp_path/"approval.json"
    mp.write_text(manifest.model_dump_json())
    ap.write_text(json.dumps({"schema":"PHASE9_POLICY_APPROVAL_V1","manifest_version":"2.0.0",
        "manifest_digest":manifest.manifest_digest,"approval_status":"APPROVED","approved_at":NOW.isoformat(),
        "approved_by":"fixture-human","approved_commit":"b"*40}))
    approved=load_approved_policy_manifest(mp,ap,expected_commit="b"*40)
    assert is_strategy_v2(approved)
    assert {p.strategy_policy_digest for p in approved.manifest.policy_content.enabled_patterns}=={policy.digest}
    assert len(approved.manifest.policy_content.enabled_patterns)==6

def test_old_decision_builder_cannot_handle_v2_manifest(tmp_path):
    from strategies.integration.policy_manifest import assert_legacy_producer
    from types import SimpleNamespace
    manifest=SimpleNamespace(manifest=SimpleNamespace(policy_content=SimpleNamespace(enabled_patterns=(
        SimpleNamespace(strategy_profile="QUANT_PAPER_V2"),))))
    with pytest.raises(ValueError):assert_legacy_producer(manifest)
