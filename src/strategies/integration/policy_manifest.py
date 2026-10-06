"""Use the existing exact-commit approval loader for V2; no second activation system."""
from quant_phase9.policy import PolicyContentV1, PolicyManifestV1
from quant_phase9.canonical import canonical_sha256

def is_strategy_v2(approved):
    patterns=approved.manifest.policy_content.enabled_patterns
    return bool(patterns) and all(p.strategy_profile=="QUANT_PAPER_V2" for p in patterns)

def assert_legacy_producer(approved):
    if is_strategy_v2(approved):
        raise ValueError("V2 policy requires the V2 deterministic strategy producer")

def build_v2_manifest(policy, *, created_at):
    content={name:[] for name in PolicyContentV1.model_fields}
    content["predicates"]=[dict(predicate_id="v2-pass",source_phase="PHASE6",
        source_type="STRATEGY_V2_RESULT",evidence_type="V2_EXECUTION_POLICY",accepted_semantic_codes=["EXECUTION_POLICY_PASS"],
        operator="PRESENT",threshold=None,upper_threshold=None,unit=None,missing_behavior="FAIL_CLOSED")]
    content["freshness_rules"]=[dict(rule_id="v2-fresh",source_phase="PHASE6",source_type="STRATEGY_V2_RESULT",
        maximum_age_seconds=policy.max_view_age_seconds,required=True,stale_behavior="FAIL_CLOSED")]
    content["coverage_rules"]=[dict(rule_id="v2-complete",source_phase="PHASE6",source_type="STRATEGY_V2_RESULT",
        accepted_coverage_statuses=["SOURCE_DECLARED_COMPLETE"],minimum_coverage_ratio=None,ratio_unit=None)]
    for timeframe in ("15m","1H","4H"):
        ttl="v2-ttl-"+timeframe; revalidate="v2-revalidate-"+timeframe
        content["ttl_rules"].append(dict(rule_id=ttl,pattern_type="MARKET_EVIDENCE_CHAIN",
            timeframe=timeframe,ttl_seconds=policy.candidate_ttl_seconds))
        content["revalidation_rules"].append(dict(rule_id=revalidate,pattern_type="MARKET_EVIDENCE_CHAIN",
            timeframe=timeframe,cadence_seconds=30,max_runs_per_minute=2))
        for side in ("LONG","SHORT"):
            content["enabled_patterns"].append(dict(pattern_type="MARKET_EVIDENCE_CHAIN",timeframe=timeframe,direction=side,
                required_predicates=["v2-pass"],supporting_predicates=[],contradicting_predicates=[],hard_conflict_predicates=[],
                freshness_rule_ids=["v2-fresh"],coverage_rule_ids=["v2-complete"],confidence_ceiling="HIGH",
                jev_required_conflict_classes=[],ttl_rule_id=ttl,revalidation_rule_id=revalidate,
                approval_reference="V2_DURABLE_EXECUTION_POLICY",strategy_profile="QUANT_PAPER_V2",
                strategy_policy_digest=policy.digest))
    validated=PolicyContentV1.model_validate(content)
    digest=str(canonical_sha256(validated.model_dump(mode="python")))
    return PolicyManifestV1.model_validate(dict(schema="PHASE9_POLICY_MANIFEST_V1",manifest_version="2.0.0",
        created_at=created_at,policy_content=validated,manifest_digest=digest))
