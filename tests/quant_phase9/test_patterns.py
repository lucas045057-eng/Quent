from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from quant_phase9.canonical import canonical_sha256
from quant_phase9.contracts import (
    EvidenceDirectionV1, EvidenceFreshnessV1, EvidenceItemV1,
    EvidenceQualityV1, EvidenceStrengthV1, EvidenceTypeV1,
    PatternMatchStatusV1, PolicyDataStatusV1, PolicyCoverageStatusV1, SourcePhaseV1,
)
from quant_phase9.patterns import match_patterns
from quant_phase9.policy import PolicyContentV1, load_approved_policy_manifest
from quant_phase9.validator import ValidationResultV1


NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
EVALUATION_ID = uuid4()


def _approved(tmp_path, *, pattern_type=None, direction=None, timeframe="1H"):
    content = {name: [] for name in PolicyContentV1.model_fields}
    if pattern_type is not None:
        up = direction == "LONG"
        setup = {
            "TREND_CONTINUATION": "TREND_UP" if up else "TREND_DOWN",
            "BREAKOUT_CONFIRMATION": "BREAKOUT_UP" if up else "BREAKOUT_DOWN",
            "LIQUIDATION_REVERSAL": "LONG_LIQUIDATION_CLUSTER" if up else "SHORT_LIQUIDATION_CLUSTER",
        }[pattern_type]
        flow = "BUY_FLOW_DOMINANT" if up else "SELL_FLOW_DOMINANT"
        for name, phase, source_type, evidence_type, code in (
            ("setup", "PHASE1" if pattern_type != "LIQUIDATION_REVERSAL" else "PHASE4",
             "STAGE1_CANDIDATE" if pattern_type != "LIQUIDATION_REVERSAL" else "LIQUIDATION_EVENT",
             "PRICE_STRUCTURE" if pattern_type != "LIQUIDATION_REVERSAL" else "LIQUIDATION_CONTEXT", setup),
            ("oi", "PHASE2", "OPEN_INTEREST", "OPEN_INTEREST_STRUCTURE", "OI_OBSERVED"),
            ("flow", "PHASE3", "TRADE_FLOW_WINDOW", "TRADE_FLOW", flow),
        ):
            content["predicates"].append({
                "predicate_id": name, "source_phase": phase, "source_type": source_type,
                "evidence_type": evidence_type, "accepted_semantic_codes": [code],
                "operator": "PRESENT", "threshold": None, "upper_threshold": None,
                "unit": None, "missing_behavior": "PARTIAL_MATCH",
            })
        content["ttl_rules"] = [{
            "rule_id": "ttl", "pattern_type": pattern_type,
            "timeframe": timeframe, "ttl_seconds": 1200,
        }]
        content["revalidation_rules"] = [{
            "rule_id": "revalidate", "pattern_type": pattern_type,
            "timeframe": timeframe, "cadence_seconds": 300,
            "max_runs_per_minute": 3,
        }]
        content["enabled_patterns"] = [{
            "pattern_type": pattern_type, "timeframe": timeframe, "direction": direction,
            "required_predicates": ["setup", "oi", "flow"],
            "supporting_predicates": [], "contradicting_predicates": [],
            "hard_conflict_predicates": [], "freshness_rule_ids": [],
            "coverage_rule_ids": [], "confidence_ceiling": "HIGH",
            "jev_required_conflict_classes": [], "ttl_rule_id": "ttl",
            "revalidation_rule_id": "revalidate", "approval_reference": "fixture",
        }]
    digest = str(canonical_sha256(content))
    manifest = {
        "schema": "PHASE9_POLICY_MANIFEST_V1", "manifest_version": "1.0.0",
        "created_at": "2026-09-28T00:00:00Z", "policy_content": content,
        "manifest_digest": digest,
    }
    approval = {
        "schema": "PHASE9_POLICY_APPROVAL_V1", "manifest_version": "1.0.0",
        "manifest_digest": digest, "approval_status": "APPROVED",
        "approved_at": "2026-09-28T00:01:00Z", "approved_by": "fixture-human",
        "approved_commit": "b" * 40,
    }
    manifest_path = tmp_path / "manifest.json"
    approval_path = tmp_path / "approval.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    approval_path.write_text(json.dumps(approval), encoding="utf-8")
    return load_approved_policy_manifest(
        manifest_path, approval_path, expected_commit="b" * 40,
    )


def _item(phase, evidence_type, source_type, semantic_code, direction):
    return EvidenceItemV1(
        evidence_id=uuid4(), evaluation_id=EVALUATION_ID, symbol="BTCUSDT",
        evidence_type=evidence_type, semantic_code=semantic_code,
        direction=direction, strength=EvidenceStrengthV1.WEAK,
        observed_at=NOW, availability_status=PolicyDataStatusV1.AVAILABLE,
        freshness_status=EvidenceFreshnessV1.FRESH,
        quality_status=EvidenceQualityV1.VALID, coverage_status=None,
        source_phase=phase, source_ref=f"{phase.value}:{source_type}/{uuid4()}",
        provider=None, provenance=f"source_type={source_type};schema=fixture;sha256={'a' * 64}",
        raw_refs=("fixture:source",), interpretation="fixture",
        schema_version="PHASE9_EVIDENCE_CHAIN_V1", created_at=NOW,
    )


def _case_items(pattern_type, direction):
    up = direction == "LONG"
    setup = {
        "TREND_CONTINUATION": "TREND_UP" if up else "TREND_DOWN",
        "BREAKOUT_CONFIRMATION": "BREAKOUT_UP" if up else "BREAKOUT_DOWN",
        "LIQUIDATION_REVERSAL": "LONG_LIQUIDATION_CLUSTER" if up else "SHORT_LIQUIDATION_CLUSTER",
    }[pattern_type]
    setup_phase = SourcePhaseV1.PHASE4 if pattern_type == "LIQUIDATION_REVERSAL" else SourcePhaseV1.PHASE1
    setup_type = EvidenceTypeV1.LIQUIDATION_CONTEXT if pattern_type == "LIQUIDATION_REVERSAL" else EvidenceTypeV1.PRICE_STRUCTURE
    setup_source = "LIQUIDATION_EVENT" if pattern_type == "LIQUIDATION_REVERSAL" else "STAGE1_CANDIDATE"
    return (
        _item(setup_phase, setup_type, setup_source, setup,
              EvidenceDirectionV1.BULLISH if up else EvidenceDirectionV1.BEARISH),
        _item(SourcePhaseV1.PHASE2, EvidenceTypeV1.OPEN_INTEREST_STRUCTURE,
              "OPEN_INTEREST", "OI_OBSERVED", EvidenceDirectionV1.UNKNOWN),
        _item(SourcePhaseV1.PHASE3, EvidenceTypeV1.TRADE_FLOW,
              "TRADE_FLOW_WINDOW", "BUY_FLOW_DOMINANT" if up else "SELL_FLOW_DOMINANT",
              EvidenceDirectionV1.BULLISH if up else EvidenceDirectionV1.BEARISH),
    )


def _validated(items):
    return ValidationResultV1(
        evaluation_id=EVALUATION_ID,
        evidence_ids=tuple(item.evidence_id for item in items),
        missing=(), degraded=(), hard_vetoes=(),
    )


def test_empty_approved_manifest_does_not_configure_any_pattern(tmp_path):
    manifest = _approved(tmp_path)
    matches = match_patterns(
        evidence_items=(), validation=_validated(()),
        policy_manifest=manifest, timeframe="1H",
    )
    assert len(matches) == 6
    assert all(m.status is PatternMatchStatusV1.NOT_CONFIGURED for m in matches)


@pytest.mark.parametrize("pattern_type", [
    "TREND_CONTINUATION", "BREAKOUT_CONFIRMATION", "LIQUIDATION_REVERSAL",
])
@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_six_reviewed_directional_structures_can_match_semantic_facts(
    tmp_path, pattern_type, direction,
):
    manifest = _approved(tmp_path, pattern_type=pattern_type, direction=direction)
    items = _case_items(pattern_type, direction)
    matches = match_patterns(
        evidence_items=items, validation=_validated(items),
        policy_manifest=manifest, timeframe="1H",
    )
    selected = next(m for m in matches if m.pattern_type == pattern_type
                    and m.direction.value == direction)
    assert selected.status is PatternMatchStatusV1.MATCHED
    assert selected.required_evidence_ids == tuple(item.evidence_id for item in items)


def test_missing_flow_is_partial_and_failed_breakout_does_not_flip_short(tmp_path):
    manifest = _approved(tmp_path, pattern_type="BREAKOUT_CONFIRMATION", direction="LONG")
    items = _case_items("BREAKOUT_CONFIRMATION", "LONG")[:2]
    matches = match_patterns(
        evidence_items=items, validation=_validated(items),
        policy_manifest=manifest, timeframe="1H",
    )
    long = next(m for m in matches if m.pattern_type == "BREAKOUT_CONFIRMATION"
                and m.direction.value == "LONG")
    short = next(m for m in matches if m.pattern_type == "BREAKOUT_CONFIRMATION"
                 and m.direction.value == "SHORT")
    assert long.status is PatternMatchStatusV1.PARTIAL_MATCH
    assert short.status is PatternMatchStatusV1.NOT_CONFIGURED


def test_five_bullish_items_cannot_outvote_required_bearish_flow(tmp_path):
    manifest = _approved(tmp_path, pattern_type="TREND_CONTINUATION", direction="LONG")
    items = _case_items("TREND_CONTINUATION", "LONG")
    bearish_flow = replace(items[2], semantic_code="SELL_FLOW_DOMINANT",
                           direction=EvidenceDirectionV1.BEARISH)
    extras = tuple(_item(SourcePhaseV1.PHASE5, EvidenceTypeV1.MARKET_REGIME,
                         "MARKET_REGIME_CONTEXT", "SOURCE_CONTEXT",
                         EvidenceDirectionV1.BULLISH) for _ in range(5))
    combined = items[:2] + (bearish_flow,) + extras
    matches = match_patterns(
        evidence_items=combined, validation=_validated(combined),
        policy_manifest=manifest, timeframe="1H",
    )
    long = next(m for m in matches if m.pattern_type == "TREND_CONTINUATION"
                and m.direction.value == "LONG")
    assert long.status is PatternMatchStatusV1.CONFLICTED
    assert bearish_flow.evidence_id in long.conflicting_evidence_ids


def test_partial_required_liquidation_never_fully_matches(tmp_path):
    manifest = _approved(tmp_path, pattern_type="LIQUIDATION_REVERSAL", direction="LONG")
    items = list(_case_items("LIQUIDATION_REVERSAL", "LONG"))
    items[0] = replace(items[0], availability_status=PolicyDataStatusV1.PARTIAL,
                       quality_status=EvidenceQualityV1.PARTIAL)
    matches = match_patterns(
        evidence_items=items, validation=_validated(items),
        policy_manifest=manifest, timeframe="1H",
    )
    selected = next(m for m in matches if m.pattern_type == "LIQUIDATION_REVERSAL"
                    and m.direction.value == "LONG")
    assert selected.status is PatternMatchStatusV1.NOT_MATCHED

def test_partial_required_oi_coverage_downgrades_match(tmp_path):
    manifest = _approved(tmp_path, pattern_type="TREND_CONTINUATION", direction="LONG")
    items = list(_case_items("TREND_CONTINUATION", "LONG"))
    items[1] = replace(items[1], coverage_status=PolicyCoverageStatusV1.PARTIAL)
    matches = match_patterns(
        evidence_items=items, validation=_validated(items),
        policy_manifest=manifest, timeframe="1H",
    )
    selected = next(m for m in matches if m.pattern_type == "TREND_CONTINUATION"
                    and m.direction.value == "LONG")
    assert selected.status is PatternMatchStatusV1.PARTIAL_MATCH


def test_opposing_credible_directions_are_both_conflicted(tmp_path):
    manifest = _approved(tmp_path, pattern_type="TREND_CONTINUATION", direction="LONG")
    content = manifest.manifest.policy_content.model_dump(mode="json")
    short = dict(content["enabled_patterns"][0])
    short["direction"] = "SHORT"
    short["required_predicates"] = ["short_setup", "oi", "short_flow"]
    content["enabled_patterns"].append(short)
    for original, predicate_id, code in (
        ("setup", "short_setup", "TREND_DOWN"),
        ("flow", "short_flow", "SELL_FLOW_DOMINANT"),
    ):
        predicate = next(p for p in content["predicates"] if p["predicate_id"] == original)
        copied = dict(predicate)
        copied["predicate_id"] = predicate_id
        copied["accepted_semantic_codes"] = [code]
        content["predicates"].append(copied)
    digest = str(canonical_sha256(content))
    manifest_path = tmp_path / "manifest.json"
    approval_path = tmp_path / "approval.json"
    raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_approval = json.loads(approval_path.read_text(encoding="utf-8"))
    raw_manifest["policy_content"] = content
    raw_manifest["manifest_digest"] = digest
    raw_approval["manifest_digest"] = digest
    manifest_path.write_text(json.dumps(raw_manifest), encoding="utf-8")
    approval_path.write_text(json.dumps(raw_approval), encoding="utf-8")
    manifest = load_approved_policy_manifest(
        manifest_path, approval_path, expected_commit="b" * 40,
    )
    items = _case_items("TREND_CONTINUATION", "LONG") + _case_items(
        "TREND_CONTINUATION", "SHORT",
    )
    matches = match_patterns(
        evidence_items=items, validation=_validated(items),
        policy_manifest=manifest, timeframe="1H",
    )
    selected = [m for m in matches if m.pattern_type == "TREND_CONTINUATION"]
    assert all(m.status is PatternMatchStatusV1.CONFLICTED for m in selected)
    assert all(m.conflicting_evidence_ids for m in selected)
