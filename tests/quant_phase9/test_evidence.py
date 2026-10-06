from __future__ import annotations

from dataclasses import replace
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_phase1.contracts import DataStatus
from quant_phase1.stage1 import Stage1Result
from quant_phase9.canonical import canonical_sha256, evaluation_id_for
from quant_phase9.contracts import (
    EvaluationIdentityV1, EvaluationSnapshotV1, EvidenceDirectionV1,
    EvidenceTypeV1, EvidenceFreshnessV1, EvidenceQualityV1,
    PolicyDataStatusV1, SourcePhaseV1,
)
from quant_phase9.evidence import build_evidence, build_chain_draft
from quant_phase9.intake import build_stage1_candidate_event
from quant_phase9.policy import PolicyContentV1, load_approved_policy_manifest
from quant_phase9.sources import make_projection
from quant_phase9.validator import validate_evidence


AS_OF = datetime(2026, 9, 28, 0, 15, tzinfo=timezone.utc)


def _snapshot(projections=(), *, policy_manifest):
    result = Stage1Result(
        symbol="BTCUSDT", category="A", reason="fixture",
        status=DataStatus.AVAILABLE, inputs_used=("price",),
        indicators={"atr": Decimal("1")}, structure="BULLISH",
        reason_codes=("STRUCTURE_ALIGNED",),
        timestamp=AS_OF - timedelta(minutes=1),
    )
    candidate = build_stage1_candidate_event(
        screening_result_id=91, run_id=22, screening_result=result,
        market="USDT_PERPETUAL",
        instrument_scope={
            "category": "USDT-FUTURES", "quote_coin": "USDT",
            "contract_type": "perpetual", "status": "online", "in_scope": "true",
        },
        candidate_created_at=AS_OF - timedelta(minutes=1),
        candidate_valid_until=AS_OF + timedelta(minutes=20),
        stage1_policy_version="phase1-basic-v1",
        source_as_of=AS_OF - timedelta(minutes=1),
    )
    identity = EvaluationIdentityV1(
        stage1_candidate_id=91, market="USDT_PERPETUAL", symbol="BTCUSDT",
        timeframe="15m", evaluation_window_start=AS_OF - timedelta(minutes=15),
        evaluation_window_end=AS_OF, policy_generation=f"{policy_manifest.manifest_version}:{policy_manifest.manifest_digest}",
        material_change_generation="initial",
    )
    evaluation_id = evaluation_id_for(identity)
    rows = tuple(factory(evaluation_id) for factory in projections)
    return EvaluationSnapshotV1(
        identity=identity, evaluation_id=evaluation_id, stage1_candidate_id=91,
        symbol="BTCUSDT", market="USDT_PERPETUAL", timeframe="15m",
        evaluation_time=AS_OF, as_of=AS_OF, created_at=AS_OF,
        candidate_event=candidate, source_projections=rows,
        stage1_policy_version="phase1-basic-v1",
        evidence_schema_version="PHASE9_EVIDENCE_CHAIN_V1",
        freshness_policy_version="1.0.0", pattern_policy_version="1.0.0",
        decision_policy_version="1.0.0", ttl_policy_version="1.0.0",
        code_version="b" * 40, snapshot_digest="a" * 64,
    )


def _projection(source_phase, source_type, payload, *, status=PolicyDataStatusV1.AVAILABLE):
    def build(evaluation_id):
        return make_projection(
            evaluation_id=evaluation_id, source_phase=source_phase,
            source_type=source_type, source_ref=f"{source_phase.value}:{source_type}:1",
            symbol="BTCUSDT", market="USDT_PERPETUAL",
            event_time=AS_OF - timedelta(minutes=1),
            observed_at=AS_OF - timedelta(minutes=1),
            captured_at=AS_OF - timedelta(seconds=30),
            processed_at=AS_OF - timedelta(seconds=30),
            available_at=AS_OF - timedelta(seconds=30),
            availability_status=status,
            freshness=EvidenceFreshnessV1.FRESH,
            quality=(EvidenceQualityV1.VALID if status is PolicyDataStatusV1.AVAILABLE
                     else EvidenceQualityV1.UNKNOWN),
            coverage=None, payload=payload,
        )
    return build


@pytest.fixture
def approved_policy(tmp_path):
    content = {name: [] for name in PolicyContentV1.model_fields}
    digest = str(canonical_sha256(content))
    manifest = {"schema": "PHASE9_POLICY_MANIFEST_V1", "manifest_version": "1.0.0",
                "created_at": "2026-09-28T00:00:00Z", "policy_content": content,
                "manifest_digest": digest}
    approval = {"schema": "PHASE9_POLICY_APPROVAL_V1", "manifest_version": "1.0.0",
                "manifest_digest": digest, "approval_status": "APPROVED",
                "approved_at": "2026-09-28T00:01:00Z", "approved_by": "fixture-human",
                "approved_commit": "b" * 40}
    manifest_path = tmp_path / "policy.json"
    approval_path = tmp_path / "approval.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    approval_path.write_text(json.dumps(approval), encoding="utf-8")
    return load_approved_policy_manifest(manifest_path, approval_path, expected_commit="b" * 40)


def test_missing_oi_iv_and_unknown_flow_never_become_zero_or_directional(approved_policy):
    snapshot = _snapshot((
        _projection(SourcePhaseV1.PHASE2, "OPEN_INTEREST", {
            "open_interest_usd": None, "normalization_method": None,
        }, status=PolicyDataStatusV1.NOT_AVAILABLE),
        _projection(SourcePhaseV1.PHASE3, "TRADE_FLOW_WINDOW", {
            "buy_trade_count": 0, "sell_trade_count": 0,
            "unknown_trade_count": 10, "delta_base": None,
        }),
        _projection(SourcePhaseV1.PHASE8, "OPTIONS_CONTEXT", {
            "iv": None, "metric_status": "NOT_CONFIGURED",
        }, status=PolicyDataStatusV1.NOT_CONFIGURED),
    ), policy_manifest=approved_policy)
    items = build_evidence(snapshot=snapshot, policy_manifest=approved_policy)
    flow = next(item for item in items if item.source_phase is SourcePhaseV1.PHASE3)
    assert flow.direction is EvidenceDirectionV1.UNKNOWN
    assert "0" not in flow.interpretation
    validation = validate_evidence(
        snapshot=snapshot, evidence_items=items, policy_manifest=approved_policy,
    )
    assert {missing.evidence_type for missing in validation.missing} >= {
        EvidenceTypeV1.OPEN_INTEREST_STRUCTURE, EvidenceTypeV1.OPTIONS_CONTEXT,
    }
    assert all(item.source_ref and item.provenance for item in items)
    draft = build_chain_draft(snapshot=snapshot, evidence_items=items, validation=validation)
    assert draft.degraded
    assert draft.missing == validation.missing


def test_tampered_source_payload_digest_hard_fails(approved_policy):
    snapshot = _snapshot((
        _projection(SourcePhaseV1.PHASE2, "OPEN_INTEREST", {
            "open_interest_usd": Decimal("1000"), "normalization_method": "base_quantity_times_mark_price",
        }),
    ), policy_manifest=approved_policy)
    bad = replace(snapshot.source_projections[0], canonical_digest="f" * 64)
    snapshot = replace(snapshot, source_projections=(bad,))
    with pytest.raises(ValueError, match="digest|provenance"):
        build_evidence(snapshot=snapshot, policy_manifest=approved_policy)


def test_future_availability_and_evidence_cap_fail_closed(approved_policy):
    snapshot = _snapshot((
        _projection(SourcePhaseV1.PHASE2, "OPEN_INTEREST", {
            "open_interest_usd": Decimal("1000"),
        }),
    ), policy_manifest=approved_policy)
    future = replace(
        snapshot.source_projections[0], available_at=AS_OF + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="later than as_of"):
        build_evidence(
            snapshot=replace(snapshot, source_projections=(future,)),
            policy_manifest=approved_policy,
        )
    over = replace(snapshot, source_projections=snapshot.source_projections * 64)
    with pytest.raises(ValueError, match="64"):
        build_evidence(snapshot=over, policy_manifest=approved_policy)

def test_policy_binding_and_stage1_event_digest_fail_closed(approved_policy):
    snapshot = _snapshot(policy_manifest=approved_policy)
    bad_identity = replace(snapshot.identity, policy_generation="1.0.0:" + "f" * 64)
    with pytest.raises(ValueError, match="policy"):
        build_evidence(
            snapshot=replace(snapshot, identity=bad_identity),
            policy_manifest=approved_policy,
        )
    corrupted = replace(snapshot.candidate_event, canonical_payload_digest="f" * 64)
    with pytest.raises(ValueError, match="Stage 1.*provenance"):
        build_evidence(
            snapshot=replace(snapshot, candidate_event=corrupted),
            policy_manifest=approved_policy,
        )


def test_validator_rejects_tampered_direction_and_missing_item(approved_policy):
    snapshot = _snapshot(policy_manifest=approved_policy)
    items = build_evidence(snapshot=snapshot, policy_manifest=approved_policy)
    bad = replace(items[0], direction=EvidenceDirectionV1.BEARISH)
    with pytest.raises(ValueError, match="tampered|provenance"):
        validate_evidence(
            snapshot=snapshot, evidence_items=(bad,),
            policy_manifest=approved_policy,
        )
    with pytest.raises(ValueError, match="tampered|provenance"):
        validate_evidence(
            snapshot=snapshot, evidence_items=(),
            policy_manifest=approved_policy,
        )
