from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from quant_phase1.contracts import DataStatus
from quant_phase1.service import persist_stage1
from quant_phase1.stage1 import Stage1Result
from quant_phase9.canonical import canonical_json, canonical_sha256
from quant_phase9.snapshot import _snapshot_content, _stored_payload
from quant_phase9.persistence import _payload
from quant_phase9.contracts import (
    ConfidenceBandV1,
    DecisionCandidateV1,
    DecisionDirectionBiasV1,
    MissingEvidenceV1,
    PatternMatchStatusV1,
    PolicyDataStatusV1,
)
from quant_phase9.lifecycle import load_decision
from quant_phase9.intake import (
    Stage1IntakeTTLPolicyV1,
    resolve_stage1_candidate_expiry,
)
from quant_phase9.replay import load_phase9_replay_manifest
from quant_realtime_paper.phase9_bridge import (
    load_active_phase9_projection, validate_phase9_projection,
)


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 28, 0, 16, tzinfo=timezone.utc)


@pytest.fixture
def snapshot():
    bundle = load_phase9_replay_manifest(ROOT / "tests/fixtures/phase9/manifest.json", project_root=ROOT)
    return bundle.manifest.evaluation_snapshot.snapshot


def _candidate(snapshot, *, valid_until=None):
    return DecisionCandidateV1(
        decision_id=uuid4(),
        evaluation_id=snapshot.evaluation_id,
        stage1_candidate_id=snapshot.stage1_candidate_id,
        symbol=snapshot.symbol,
        market=snapshot.market,
        timeframe=snapshot.timeframe,
        created_at=snapshot.as_of,
        valid_until=valid_until or NOW + timedelta(minutes=15),
        eligible=True,
        direction_bias=DecisionDirectionBiasV1.BULLISH,
        confidence_band=ConfidenceBandV1.HIGH,
        matched_pattern="TREND_CONTINUATION",
        pattern_status=PatternMatchStatusV1.MATCHED,
        supporting_evidence_ids=(),
        conflicting_evidence_ids=(),
        degraded_evidence_ids=(),
        missing_evidence=(),
        veto_reasons=(),
        jev_review_id=None,
        reason_codes=("OWNER_POLICY_MATCH",),
        short_summary="persisted candidate fixture",
        input_snapshot_hash="e" * 64,
        evidence_schema_version=snapshot.evidence_schema_version,
        pattern_policy_version=snapshot.pattern_policy_version,
        freshness_policy_version=snapshot.freshness_policy_version,
        decision_policy_version=snapshot.decision_policy_version,
        ttl_policy_version=snapshot.ttl_policy_version,
        prompt_version="NONE",
        code_version=snapshot.code_version,
        supersedes_decision_id=None,
    )


def _validate(snapshot, candidate, **overrides):
    arguments = dict(
        evaluation_state="COMPLETED",
        intake_disposition="ADMITTED",
        active=True,
        lifecycle_status="ACTIVE",
        snapshot=snapshot,
        candidate=candidate,
        expected_stage1_candidate_id=snapshot.stage1_candidate_id,
        expected_policy_generation=snapshot.identity.policy_generation,
        expected_symbol=snapshot.symbol,
        expected_timeframe=snapshot.timeframe,
        expected_build_revision=str(snapshot.code_version),
        evidence_snapshot_hash=str(snapshot.snapshot_digest),
        evidence_input_snapshot_hash=str(candidate.input_snapshot_hash),
        now=NOW,
    )
    arguments.update(overrides)
    return validate_phase9_projection(**arguments)


def test_stage1_intake_ttl_is_separate_and_resolves_candidate_expiry():
    policy = Stage1IntakeTTLPolicyV1(stage1_candidate_ttl_seconds=300)

    assert resolve_stage1_candidate_expiry(NOW, policy=policy) == NOW + timedelta(seconds=300)
    assert policy.stage1_candidate_ttl_seconds == 300


def test_missing_stage1_intake_ttl_keeps_expiry_unset_for_phase9_deferral():
    policy = Stage1IntakeTTLPolicyV1(stage1_candidate_ttl_seconds=None)

    assert resolve_stage1_candidate_expiry(NOW, policy=policy) is None


@pytest.mark.parametrize("value", (0, -1, True, 1.5))
def test_stage1_intake_ttl_rejects_non_positive_or_non_integer_values(value):
    with pytest.raises((TypeError, ValueError)):
        Stage1IntakeTTLPolicyV1(stage1_candidate_ttl_seconds=value)


def test_durable_phase9_projection_requires_exact_identity_and_preserves_direction(snapshot):
    candidate = _candidate(snapshot)

    result = _validate(snapshot, candidate)

    assert result.ready is True
    assert result.snapshot is snapshot
    assert result.candidate is candidate
    assert result.candidate.direction_bias is DecisionDirectionBiasV1.BULLISH


@pytest.mark.parametrize(
    "overrides, expected",
    (
        ({"expected_build_revision": "c" * 40}, "BUILD_REVISION_MISMATCH"),
        ({"expected_stage1_candidate_id": 92}, "PHASE9_IDENTITY_MISMATCH"),
        ({"expected_policy_generation": "stale-policy-generation"}, "PHASE9_IDENTITY_MISMATCH"),
        ({"expected_symbol": "ETHUSDT"}, "PHASE9_IDENTITY_MISMATCH"),
        ({"expected_timeframe": "1H"}, "PHASE9_IDENTITY_MISMATCH"),
    ),
)
def test_durable_phase9_projection_rejects_stale_revision_or_identity(snapshot, overrides, expected):
    result = _validate(snapshot, _candidate(snapshot), **overrides)

    assert result.ready is False
    assert result.reason_code == expected
    assert result.snapshot is None
    assert result.candidate is None


def test_pending_or_inactive_phase9_output_is_not_exposed(snapshot):
    candidate = _candidate(snapshot)

    pending = _validate(snapshot, candidate, evaluation_state="RUNNING")
    inactive = _validate(snapshot, candidate, lifecycle_status="SUPERSEDED")

    assert pending.ready is False
    assert pending.reason_code == "PHASE9_NOT_COMPLETE"
    assert pending.candidate is None
    assert inactive.ready is False
    assert inactive.reason_code == "PHASE9_NOT_ACTIVE"
    assert inactive.candidate is None


def test_phase9_bridge_keeps_stage1_and_pattern_ttls_distinct(snapshot):
    stage1_expiry = snapshot.candidate_event.candidate_valid_until
    candidate = _candidate(snapshot, valid_until=NOW + timedelta(minutes=7))
    expired_stage1 = replace(
        snapshot,
        candidate_event=replace(snapshot.candidate_event, candidate_valid_until=NOW),
        snapshot_digest="0" * 64,
    )
    expired_stage1 = replace(
        expired_stage1,
        snapshot_digest=canonical_sha256(_snapshot_content(expired_stage1)),
    )

    result = _validate(expired_stage1, candidate)

    assert stage1_expiry is not None
    assert result.ready is False
    assert result.reason_code == "STAGE1_CANDIDATE_EXPIRED"


def test_stage1_intake_ttl_config_is_optional_and_has_no_implicit_default():
    from quant_phase9.config import load_phase9_runtime_config

    assert load_phase9_runtime_config({}).stage1_candidate_ttl_seconds is None
    assert load_phase9_runtime_config(
        {"PHASE9_STAGE1_CANDIDATE_TTL_SECONDS": "300"}
    ).stage1_candidate_ttl_seconds == 300


def test_stage1_intake_ttl_config_rejects_zero_and_non_decimal_values():
    from quant_phase9.config import load_phase9_runtime_config

    for value in ("0", "-1", "1.5", "latest"):
        with pytest.raises(ValueError):
            load_phase9_runtime_config({"PHASE9_STAGE1_CANDIDATE_TTL_SECONDS": value})


class _FakeResult:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _FakeConnection:
    def __init__(self, row):
        self.row = row
        self.params = None

    def execute(self, statement, params):
        assert "phase9_evaluations" in statement
        self.params = params
        return _FakeResult(self.row)


def test_runtime_bridge_reads_typed_snapshot_and_decision_from_durable_rows(snapshot):
    candidate = _candidate(snapshot)
    connection = _FakeConnection((
        "COMPLETED", "ADMITTED", True, "ACTIVE",
        __import__("json").loads(canonical_json(_stored_payload(snapshot))),
        _payload("PHASE9_DECISION_CANDIDATE_V1", candidate),
        str(snapshot.snapshot_digest), str(candidate.input_snapshot_hash),
    ))

    result = load_active_phase9_projection(
        connection,
        stage1_candidate_id=snapshot.stage1_candidate_id,
        policy_generation=snapshot.identity.policy_generation,
        symbol=snapshot.symbol,
        timeframe=snapshot.timeframe,
        build_revision=str(snapshot.code_version),
        now=NOW,
    )

    assert result.ready is True
    assert result.candidate.direction_bias is candidate.direction_bias
    assert connection.params == (
        snapshot.stage1_candidate_id, snapshot.symbol, snapshot.timeframe,
        snapshot.identity.policy_generation,
    )


def test_phase1_service_uses_explicit_stage1_intake_policy_ttl(monkeypatch):
    import quant_phase9.intake as intake

    captured = []
    monkeypatch.setattr(
        intake,
        "persist_stage1_candidate_with_event",
        lambda connection, **kwargs: captured.append(kwargs) or 1,
    )
    created_at = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    result = Stage1Result(
        "BTCUSDT", "A", "available", DataStatus.AVAILABLE,
        ("price",), {"atr": "1"}, "BULLISH", ("STRUCTURE_ALIGNED",), {}, None, created_at,
    )
    repository = SimpleNamespace(
        connection=object(), create_screening_run=lambda at, rule_version: 7,
    )

    persisted = persist_stage1(
        repository, SimpleNamespace(collected_at=created_at), [result],
        stage1_candidate_ttl_seconds=300,
    )

    assert persisted == 1
    assert captured[0]["candidate_valid_until"] == created_at + timedelta(seconds=300)


def test_persisted_strategy_missing_evidence_round_trips(snapshot):
    candidate = replace(
        _candidate(snapshot),
        missing_evidence=(
            MissingEvidenceV1(
                evidence_type="EXCHANGE_EVENT_COVERAGE",
                source_status=PolicyDataStatusV1.NOT_AVAILABLE,
                reason="REQUIRED_EVIDENCE_UNAVAILABLE",
                source_timestamp=None,
                captured_at=None,
            ),
            MissingEvidenceV1(
                evidence_type="RETEST:15m",
                source_status=PolicyDataStatusV1.NOT_AVAILABLE,
                reason="REQUIRED_EVIDENCE_UNAVAILABLE",
                source_timestamp=None,
                captured_at=None,
            ),
        ),
    )

    restored = load_decision(_payload("PHASE9_DECISION_CANDIDATE_V1", candidate))

    assert tuple(str(item.evidence_type) for item in restored.missing_evidence) == (
        "EXCHANGE_EVENT_COVERAGE",
        "RETEST:15m",
    )
