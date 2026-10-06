"""Immutable, caller-transaction-owned persistence for Phase 9 Jev reviews."""

from __future__ import annotations

import json

from psycopg import Connection
from psycopg.types.json import Jsonb

from .canonical import canonical_bytes
from .contracts import JevReviewV1


MAX_JEV_REVIEW_PAYLOAD_BYTES = 32_768


class JevReviewIdentityConflictError(RuntimeError):
    """A persisted Jev identity was reused with different immutable content."""


def persist_jev_review(conn: Connection, *, review: JevReviewV1) -> None:
    """Insert a review in the caller's active transaction; never commit or truncate."""
    if not isinstance(review, JevReviewV1):
        raise TypeError("review must be JevReviewV1")
    if conn.autocommit or conn.info.transaction_status.name != "INTRANS":
        raise ValueError("persist_jev_review requires an active caller-owned transaction")

    payload = _review_payload(review)
    encoded = canonical_bytes(payload)
    if len(encoded) > MAX_JEV_REVIEW_PAYLOAD_BYTES:
        raise ValueError("Jev review payload exceeds 32 KiB")
    json_payload = json.loads(encoded)
    jsonb_size = conn.execute(
        "SELECT octet_length(%s::jsonb::text)", (Jsonb(json_payload),)
    ).fetchone()[0]
    if jsonb_size > MAX_JEV_REVIEW_PAYLOAD_BYTES:
        raise ValueError("Jev review payload exceeds 32 KiB in PostgreSQL JSONB representation")
    expected = (
        review.review_id,
        review.evidence_chain_id,
        review.status.value,
        review.reason_code,
        str(review.request_digest),
        str(review.response_digest) if review.response_digest is not None else None,
        review.provider,
        review.model,
        review.model_version,
        review.prompt_version,
        json_payload,
        review.created_at,
    )

    existing = conn.execute(
        """SELECT review_id,evidence_chain_id,status,reason_code,request_digest,response_digest,
                  provider,model,model_version,prompt_version,payload,created_at
           FROM phase9_jev_reviews WHERE evaluation_id=%s AND request_digest=%s""",
        (review.evaluation_id, str(review.request_digest)),
    ).fetchone()
    if existing is not None:
        if tuple(existing) != expected:
            raise JevReviewIdentityConflictError("Jev request identity already has different immutable content")
        return

    existing_id = conn.execute(
        """SELECT review_id,evidence_chain_id,status,reason_code,request_digest,response_digest,
                  provider,model,model_version,prompt_version,payload
           FROM phase9_jev_reviews WHERE review_id=%s""",
        (review.review_id,),
    ).fetchone()
    if existing_id is not None:
        raise JevReviewIdentityConflictError("Jev review_id already exists with different immutable content")

    conn.execute(
        """INSERT INTO phase9_jev_reviews
             (review_id,evaluation_id,evidence_chain_id,status,reason_code,request_digest,
              response_digest,provider,model,model_version,prompt_version,payload,created_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
           ON CONFLICT DO NOTHING""",
        (
            review.review_id, review.evaluation_id, review.evidence_chain_id, review.status.value,
            review.reason_code, str(review.request_digest),
            str(review.response_digest) if review.response_digest is not None else None,
            review.provider, review.model, review.model_version, review.prompt_version,
            Jsonb(json_payload), review.created_at,
        ),
    )
    persisted = conn.execute(
        """SELECT review_id,evidence_chain_id,status,reason_code,request_digest,response_digest,
                  provider,model,model_version,prompt_version,payload,created_at
           FROM phase9_jev_reviews WHERE evaluation_id=%s AND request_digest=%s""",
        (review.evaluation_id, str(review.request_digest)),
    ).fetchone()
    if persisted is None or tuple(persisted) != expected:
        raise JevReviewIdentityConflictError("Jev insert conflicted with different immutable content")


def _review_payload(review: JevReviewV1) -> dict[str, object]:
    return {
        "reason_detail": review.reason_detail,
        "relation": review.relation.value if review.relation is not None else None,
        "conflict_severity": review.conflict_severity.value if review.conflict_severity is not None else None,
        "dominant_context": review.dominant_context.value if review.dominant_context is not None else None,
        "supporting_assessments": [
            {"evidence_ids": [str(item) for item in assessment.evidence_ids],
             "assessment": assessment.assessment}
            for assessment in review.supporting_assessments
        ],
        "conflicting_assessments": [
            {"evidence_ids": [str(item) for item in assessment.evidence_ids],
             "assessment": assessment.assessment}
            for assessment in review.conflicting_assessments
        ],
        "unresolved_conflicts": [
            {"reason_code": note.reason_code, "evidence_ids": [str(item) for item in note.evidence_ids],
             "detail": note.detail}
            for note in review.unresolved_conflicts
        ],
        "degradation_notes": [
            {"reason_code": note.reason_code, "detail": note.detail}
            for note in review.degradation_notes
        ],
        "referenced_evidence_ids": [str(item) for item in review.referenced_evidence_ids],
        "reasoning_summary": review.reasoning_summary,
    }
