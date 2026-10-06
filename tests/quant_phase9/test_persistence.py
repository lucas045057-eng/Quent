from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from quant_phase1.db import apply_migrations
from quant_phase9.contracts import (
    ConfidenceBandV1, DecisionCandidateV1, DecisionDirectionBiasV1,
    DecisionStatusEventV1, EvidenceChainV1, EvidenceDirectionV1,
    EvidenceFreshnessV1, EvidenceItemV1, EvidenceQualityV1,
    EvidenceStrengthV1, EvidenceTypeV1, JevReviewStatusV1, JevReviewV1,
    PatternMatchStatusV1,
    PatternMatchV1, PolicyDataStatusV1, PolicyDirectionV1, SourcePhaseV1,
)
from quant_phase9.persistence import chain_id_for, persist_final, persist_jev_review


NOW = datetime(2026, 9, 28, 0, 15, tzinfo=timezone.utc)
SNAPSHOT_DIGEST = "d" * 64
INPUT_HASH = "e" * 64
EVENT_ID = "a" * 64
OWNER = "fixture-worker"


@pytest.fixture
def db():
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for persistence integration")
    info = conninfo_to_dict(dsn)
    assert info.get("host") in {"localhost", "127.0.0.1", "::1"}
    assert info.get("dbname") == "quant_phase9_test"
    schema = f"phase9_persistence_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    conn = psycopg.connect(dsn, options=f"-c search_path={schema},public")
    try:
        apply_migrations(conn)
        conn.commit()
        yield conn
    finally:
        conn.close()
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def _seed(db, *, candidate_id=91, event_id=EVENT_ID, material_generation='initial'):
    evaluation_id = uuid4()
    db.execute(
        """INSERT INTO phase9_evaluations (
            evaluation_id, stage1_candidate_id, symbol, market, timeframe,
            evaluation_state, intake_disposition, candidate_valid_until,
            policy_generation, material_change_generation)
           VALUES (%s, %s, 'BTCUSDT', 'USDT_PERPETUAL', '1H',
                   'RUNNING', 'ADMITTED', %s, %s, %s)""",
        (evaluation_id, candidate_id, NOW + timedelta(hours=1), "1.0.0:" + "f" * 64, material_generation),
    )
    db.execute(
        """INSERT INTO phase9_evaluation_snapshots (
            evaluation_id, snapshot_digest, as_of, payload, code_version)
           VALUES (%s, %s, %s, '{}'::jsonb, %s)""",
        (evaluation_id, SNAPSHOT_DIGEST, NOW, "b" * 40),
    )
    db.execute(
        """INSERT INTO outbox_events (
            event_type, aggregate_key, payload, event_id, phase9_state,
            attempt_count, attempt_limit, next_attempt_at,
            lease_owner, lease_expires_at)
           VALUES ('phase9.stage1_candidate', %s, '{}'::jsonb, %s,
                   'LEASED', 1, 3, %s, %s, %s)""",
        (str(candidate_id) + ":" + event_id, event_id, NOW, OWNER, NOW + timedelta(hours=1)),
    )
    db.commit()
    evidence = EvidenceItemV1(
        evidence_id=uuid4(), evaluation_id=evaluation_id, symbol="BTCUSDT",
        evidence_type=EvidenceTypeV1.PRICE_STRUCTURE, semantic_code="TREND_UP",
        direction=EvidenceDirectionV1.BULLISH, strength=EvidenceStrengthV1.WEAK,
        observed_at=NOW, availability_status=PolicyDataStatusV1.AVAILABLE,
        freshness_status=EvidenceFreshnessV1.FRESH,
        quality_status=EvidenceQualityV1.VALID, coverage_status=None,
        source_phase=SourcePhaseV1.PHASE1, source_ref="phase1:fixture/1",
        provider=None, provenance="fixture:sha256:" + "a" * 64,
        raw_refs=("phase1:fixture/1",), interpretation="fixture",
        schema_version="PHASE9_EVIDENCE_CHAIN_V1", created_at=NOW,
    )
    chain = EvidenceChainV1(
        stage1_candidate_id=candidate_id, symbol="BTCUSDT", evaluation_id=evaluation_id,
        evaluation_time=NOW, timeframe="1H", supporting=(evidence.evidence_id,),
        conflicting=(), neutral=(), missing=(), degraded=(), hard_vetoes=(),
        matched_patterns=(), jev_review_required=False,
        evidence_schema_version="PHASE9_EVIDENCE_CHAIN_V1",
        evaluation_snapshot_hash=SNAPSHOT_DIGEST, input_snapshot_hash=INPUT_HASH,
    )
    match = PatternMatchV1(
        pattern_match_id=uuid4(), evaluation_id=evaluation_id,
        pattern_type="TREND_CONTINUATION", direction=PolicyDirectionV1.LONG,
        status=PatternMatchStatusV1.MATCHED,
        required_evidence_ids=(evidence.evidence_id,),
        supporting_evidence_ids=(), conflicting_evidence_ids=(),
        missing=(), vetoes=(), pattern_policy_version="1.0.0",
    )
    decision = DecisionCandidateV1(
        decision_id=uuid4(), evaluation_id=evaluation_id,
        stage1_candidate_id=candidate_id, symbol="BTCUSDT", market="USDT_PERPETUAL",
        timeframe="1H", created_at=NOW, valid_until=NOW + timedelta(minutes=20),
        eligible=True, direction_bias=DecisionDirectionBiasV1.BULLISH,
        confidence_band=ConfidenceBandV1.HIGH,
        matched_pattern="TREND_CONTINUATION",
        pattern_status=PatternMatchStatusV1.MATCHED,
        supporting_evidence_ids=(evidence.evidence_id,),
        conflicting_evidence_ids=(), degraded_evidence_ids=(),
        missing_evidence=(), veto_reasons=(), jev_review_id=None,
        reason_codes=("APPROVED_PATTERN_MATCH",), short_summary="fixture",
        input_snapshot_hash=INPUT_HASH,
        evidence_schema_version="PHASE9_EVIDENCE_CHAIN_V1",
        pattern_policy_version="1.0.0", freshness_policy_version="1.0.0",
        decision_policy_version="1.0.0", ttl_policy_version="1.0.0",
        prompt_version="NONE", code_version="b" * 40,
        supersedes_decision_id=None,
    )
    lifecycle = DecisionStatusEventV1(
        event_id=uuid4(), decision_id=decision.decision_id,
        evaluation_id=evaluation_id, status="ACTIVE", event_time=NOW,
        reason_code="DECISION_CREATED", supersedes_decision_id=None,
    )
    return evaluation_id, evidence, chain, match, decision, lifecycle


def _call(db, fixture, *, snapshot_digest=SNAPSHOT_DIGEST, jev_review=None, event_id=EVENT_ID):
    evaluation_id, evidence, chain, match, decision, lifecycle = fixture
    persist_final(
        db, evaluation_id=evaluation_id, snapshot_digest=snapshot_digest,
        evidence_items=(evidence,), evidence_chain=chain,
        pattern_matches=(match,), jev_review=jev_review,
        decision=decision, lifecycle_event=lifecycle,
        event_id=event_id, consumer_name=OWNER, now=NOW,
    )


def test_final_rows_and_ack_commit_together_and_retry_is_idempotent(db):
    fixture = _seed(db)
    _call(db, fixture)
    db.commit()
    for table in (
        "phase9_evidence_items", "phase9_evidence_chains",
        "phase9_pattern_matches", "phase9_decision_candidates",
        "phase9_decision_status_events",
    ):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (1,)
    assert db.execute(
        "SELECT phase9_state FROM outbox_events WHERE event_id = %s", (EVENT_ID,)
    ).fetchone() == ("ACKNOWLEDGED",)
    _call(db, fixture)
    db.commit()
    assert db.execute("SELECT count(*) FROM phase9_decision_candidates").fetchone() == (1,)


def test_supersede_retry_preserves_original_status_event_clock(db):
    first = _seed(db)
    _call(db,first)
    db.commit()
    second = list(_seed(db,event_id='b'*64,material_generation='following'))
    second[4] = replace(second[4],supersedes_decision_id=first[4].decision_id)
    _call(db,second,event_id='b'*64)
    db.commit()
    evaluation_id,evidence,chain,match,decision,lifecycle = second
    persist_final(db,evaluation_id=evaluation_id,snapshot_digest=SNAPSHOT_DIGEST,
        evidence_items=(evidence,),evidence_chain=chain,pattern_matches=(match,),jev_review=None,
        decision=decision,lifecycle_event=lifecycle,event_id='b'*64,consumer_name=OWNER,
        now=NOW+timedelta(seconds=1))
    db.commit()
    assert db.execute("SELECT count(*) FROM phase9_decision_status_events WHERE status='SUPERSEDED'").fetchone()[0] == 1


@pytest.mark.parametrize("failure_table", [
    "phase9_evidence_items", "phase9_evidence_chains",
    "phase9_pattern_matches", "phase9_decision_candidates",
    "phase9_decision_status_events",
])
def test_insert_failure_rolls_back_all_final_rows_and_ack(db, failure_table):
    fixture = _seed(db)
    db.execute(
        """CREATE FUNCTION fail_pattern() RETURNS trigger LANGUAGE plpgsql AS $$
           BEGIN RAISE EXCEPTION 'injected pattern failure'; END; $$"""
    )
    db.execute(sql.SQL("CREATE TRIGGER fail_pattern BEFORE INSERT ON {} "
                       "FOR EACH ROW EXECUTE FUNCTION fail_pattern()").format(
        sql.Identifier(failure_table)
    ))
    db.commit()
    with pytest.raises(psycopg.Error, match="injected pattern failure"):
        _call(db, fixture)
    db.rollback()
    for table in (
        "phase9_evidence_items", "phase9_evidence_chains",
        "phase9_pattern_matches", "phase9_decision_candidates",
        "phase9_decision_status_events",
    ):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    assert db.execute(
        "SELECT phase9_state FROM outbox_events WHERE event_id = %s", (EVENT_ID,)
    ).fetchone() == ("LEASED",)


def test_wrong_snapshot_digest_rejects_without_writes(db):
    fixture = _seed(db)
    with pytest.raises(ValueError, match="snapshot digest"):
        _call(db, fixture, snapshot_digest="f" * 64)
    db.rollback()
    assert db.execute("SELECT count(*) FROM phase9_evidence_items").fetchone() == (0,)
    assert db.execute(
        "SELECT phase9_state FROM outbox_events WHERE event_id = %s", (EVENT_ID,)
    ).fetchone() == ("LEASED",)

def test_missing_jev_reference_rejects_before_final_insert(db):
    fixture = _seed(db)
    evaluation_id, evidence, chain, match, decision, lifecycle = fixture
    bad = replace(decision, jev_review_id=uuid4())
    with pytest.raises(ValueError, match="Jev review"):
        _call(db, (evaluation_id, evidence, chain, match, bad, lifecycle))
    db.rollback()
    assert db.execute("SELECT count(*) FROM phase9_decision_candidates").fetchone() == (0,)


def test_expired_or_wrong_owner_lease_rolls_back_final_rows(db):
    fixture = _seed(db)
    db.execute(
        "UPDATE outbox_events SET lease_owner = 'other-worker' WHERE event_id = %s",
        (EVENT_ID,),
    )
    db.commit()
    with pytest.raises(Exception, match="another consumer"):
        _call(db, fixture)
    db.rollback()
    assert db.execute("SELECT count(*) FROM phase9_evidence_items").fetchone() == (0,)
    assert db.execute(
        "SELECT phase9_state FROM outbox_events WHERE event_id = %s", (EVENT_ID,)
    ).fetchone() == ("LEASED",)

def test_jev_review_must_be_durable_before_decision_reference(db):
    fixture = _seed(db)
    evaluation_id, evidence, chain, match, decision, lifecycle = fixture
    review = JevReviewV1(
        evaluation_id=evaluation_id, review_id=uuid4(),
        status=JevReviewStatusV1.NOT_CONFIGURED,
        reason_code="PROVIDER_NOT_CONFIGURED", reason_detail=None,
        evidence_chain_id=chain_id_for(evaluation_id),
        request_digest="a" * 64, response_digest=None,
        provider=None, model=None, model_version=None,
        prompt_version="PHASE9_JEV_PROMPT_V1",
        relation=None, conflict_severity=None, dominant_context=None,
        supporting_assessments=(), conflicting_assessments=(),
        unresolved_conflicts=(), degradation_notes=(),
        referenced_evidence_ids=(), reasoning_summary=None, created_at=NOW,
    )
    no_trade = replace(
        decision, eligible=False, confidence_band=ConfidenceBandV1.INSUFFICIENT,
        jev_review_id=review.review_id,
    )
    modified = (evaluation_id, evidence, chain, match, no_trade, lifecycle)
    with pytest.raises(ValueError, match="durable record"):
        _call(db, modified, jev_review=review)
    db.rollback()
    persist_jev_review(db, review=review)
    db.commit()
    _call(db, modified, jev_review=review)
    db.commit()
    assert db.execute("SELECT count(*) FROM phase9_jev_reviews").fetchone() == (1,)
    assert db.execute("SELECT jev_review_id FROM phase9_decision_candidates").fetchone() == (
        review.review_id,
    )
