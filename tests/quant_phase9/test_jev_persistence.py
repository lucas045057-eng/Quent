from __future__ import annotations

import importlib
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from quant_phase1.db import apply_migrations
from quant_phase9.contracts import (
    JevConflictSeverityV1,
    JevDominantContextV1,
    JevEvidenceAssessmentV1,
    JevRelationV1,
    JevReviewStatusV1,
    JevReviewV1,
)

try:
    _persistence = importlib.import_module("quant_phase9.jev_persistence")
except ModuleNotFoundError:
    _persistence = None


NOW = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)
EVALUATION_ID = UUID("00000000-0000-0000-0000-000000000701")
CHAIN_ID = UUID("00000000-0000-0000-0000-000000000702")
REVIEW_ID = UUID("00000000-0000-0000-0000-000000000703")


def _api():
    assert _persistence is not None, "Phase 9 Jev persistence is not implemented"
    return _persistence


@pytest.fixture
def database():
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for Jev persistence integration tests")
    info = conninfo_to_dict(dsn)
    assert info.get("host") in {"127.0.0.1", "localhost", "::1"}
    assert info.get("dbname") == "quant_phase9_test"
    schema = f"phase9_jev_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    conn = psycopg.connect(dsn, options=f"-c search_path={schema},public")
    try:
        apply_migrations(conn)
        conn.commit()
        yield dsn, schema, conn
    finally:
        conn.close()
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def _review(**overrides):
    values = dict(
        evaluation_id=EVALUATION_ID,
        review_id=REVIEW_ID,
        status=JevReviewStatusV1.COMPLETED,
        reason_code=None,
        reason_detail=None,
        evidence_chain_id=CHAIN_ID,
        request_digest="a" * 64,
        response_digest="b" * 64,
        provider="recorded-fixture",
        model="fixture-model-v1",
        model_version="fixture-model-v1",
        prompt_version="v1",
        relation=JevRelationV1.CONFLICTING,
        conflict_severity=JevConflictSeverityV1.MEDIUM,
        dominant_context=JevDominantContextV1.FLOW_DIVERGENCE,
        supporting_assessments=(JevEvidenceAssessmentV1(
            (UUID("00000000-0000-0000-0000-000000000704"),), "flow evidence supports the existing view"
        ),),
        conflicting_assessments=(),
        unresolved_conflicts=(),
        degradation_notes=(),
        referenced_evidence_ids=(UUID("00000000-0000-0000-0000-000000000704"),),
        reasoning_summary="Review describes evidence conflict only.",
        created_at=NOW,
    )
    values.update(overrides)
    return JevReviewV1(**values)


def _seed_evaluation(conn):
    conn.execute(
        """INSERT INTO phase9_evaluations
             (evaluation_id,stage1_candidate_id,symbol,market,timeframe,evaluation_state,
              intake_disposition,policy_generation,material_change_generation)
           VALUES (%s,701,'BTCUSDT','USDT_PERPETUAL','15m','WAITING_JEV','ADMITTED','test-policy','0')""",
        (EVALUATION_ID,),
    )
    conn.commit()


def test_review_stays_in_caller_transaction_and_is_visible_before_decision_reference(database):
    dsn, schema, conn = database
    _seed_evaluation(conn)
    review = _review()
    conn.execute("SELECT 1")
    _api().persist_jev_review(conn, review=review)
    assert conn.info.transaction_status.name == "INTRANS"

    with psycopg.connect(dsn, options=f"-c search_path={schema},public") as observer:
        assert observer.execute(
            "SELECT count(*) FROM phase9_jev_reviews WHERE review_id=%s", (REVIEW_ID,)
        ).fetchone()[0] == 0
    conn.commit()

    with psycopg.connect(dsn, options=f"-c search_path={schema},public") as observer:
        assert observer.execute(
            "SELECT count(*) FROM phase9_jev_reviews WHERE review_id=%s", (REVIEW_ID,)
        ).fetchone()[0] == 1
        observer.execute(
            """INSERT INTO phase9_decision_candidates
                 (decision_id,evaluation_id,stage1_candidate_id,symbol,market,timeframe,valid_until,
                  eligible,direction_bias,confidence_band,input_snapshot_hash,jev_review_id,payload)
               VALUES (%s,%s,701,'BTCUSDT','USDT_PERPETUAL','15m',%s,FALSE,'NEUTRAL',
                       'INSUFFICIENT',%s,%s,'{}'::jsonb)""",
            (uuid4(), EVALUATION_ID, NOW + timedelta(minutes=15), "c" * 64, REVIEW_ID),
        )
        observer.commit()


def test_review_insert_is_idempotent_but_same_request_with_changed_result_conflicts(database):
    _, _, conn = database
    _seed_evaluation(conn)
    review = _review()
    conn.execute("SELECT 1")
    _api().persist_jev_review(conn, review=review)
    _api().persist_jev_review(conn, review=review)
    conn.commit()
    assert conn.execute(
        "SELECT count(*) FROM phase9_jev_reviews WHERE evaluation_id=%s AND request_digest=%s",
        (EVALUATION_ID, "a" * 64),
    ).fetchone()[0] == 1
    conn.commit()

    conn.execute("SELECT 1")
    with pytest.raises(_api().JevReviewIdentityConflictError):
        _api().persist_jev_review(conn, review=replace(review, reasoning_summary="different output"))
    conn.rollback()

    conn.execute("SELECT 1")
    with pytest.raises(_api().JevReviewIdentityConflictError):
        _api().persist_jev_review(conn, review=replace(review, created_at=NOW + timedelta(seconds=1)))
    conn.rollback()


def test_review_persistence_requires_active_caller_transaction(database):
    dsn, schema, conn = database
    _seed_evaluation(conn)
    with psycopg.connect(dsn, autocommit=True, options=f"-c search_path={schema},public") as idle:
        with pytest.raises(ValueError, match="caller-owned transaction"):
            _api().persist_jev_review(idle, review=_review())


def test_review_payload_over_32k_is_rejected_without_truncation(database):
    _, _, conn = database
    _seed_evaluation(conn)
    ids = tuple(UUID(int=index) for index in range(1, 65))
    assessments = tuple(JevEvidenceAssessmentV1(ids, "x" * 256) for _ in range(16))
    oversized = _review(supporting_assessments=assessments, referenced_evidence_ids=ids)
    conn.execute("SELECT 1")
    with pytest.raises(ValueError, match="32 KiB"):
        _api().persist_jev_review(conn, review=oversized)
    conn.rollback()
    assert conn.execute("SELECT count(*) FROM phase9_jev_reviews").fetchone()[0] == 0


def test_review_payload_limit_uses_postgresql_jsonb_rendered_bytes(database):
    _, _, conn = database
    _seed_evaluation(conn)
    evidence_ids = tuple(UUID(int=index) for index in range(1, 65))
    candidate = None
    for last_assessment_ref_count in range(1, 65):
        review = _review(
            supporting_assessments=(
                *(JevEvidenceAssessmentV1(evidence_ids, "x" * 256) for _ in range(10)),
                JevEvidenceAssessmentV1(evidence_ids[:last_assessment_ref_count], "x" * 256),
            ),
            referenced_evidence_ids=evidence_ids,
        )
        payload = _api()._review_payload(review)
        canonical_size = len(_api().canonical_bytes(payload))
        jsonb_size = conn.execute(
            "SELECT octet_length(%s::jsonb::text)", (json.dumps(payload),)
        ).fetchone()[0]
        if canonical_size <= _api().MAX_JEV_REVIEW_PAYLOAD_BYTES < jsonb_size:
            candidate = review
            break

    assert candidate is not None, "fixture must straddle canonical and PostgreSQL JSONB size"
    conn.execute("SELECT 1")
    with pytest.raises(ValueError, match="32 KiB"):
        _api().persist_jev_review(conn, review=candidate)
    conn.rollback()
    assert conn.execute("SELECT count(*) FROM phase9_jev_reviews").fetchone()[0] == 0
