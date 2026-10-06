"""Atomic final audit persistence on a caller-owned PostgreSQL transaction."""

from __future__ import annotations

import json
from collections.abc import Sequence
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg import Connection
from psycopg.types.json import Jsonb

from .canonical import canonical_json, canonical_sha256
from .contracts import (
    DecisionCandidateV1, DecisionStatusEventV1, EvidenceChainV1,
    EvidenceItemV1, JevReviewV1, PatternMatchV1, Sha256Hex,
)
from .intake import ack
from .sources import as_utc


def chain_id_for(evaluation_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, canonical_json({
        "schema": "PHASE9_EVIDENCE_CHAIN_ID_V1", "evaluation_id": evaluation_id,
    }))


def _payload(schema: str, value: object) -> dict[str, object]:
    return json.loads(canonical_json({"schema": schema, "value": value}))


def _insert_exact(
    conn: Connection, *, sql: str, values: tuple[object, ...],
    table: str, id_column: str, row_id: UUID,
    expected_payload: dict[str, object],
) -> None:
    conn.execute(sql, values)
    stored = conn.execute(
        f"SELECT payload FROM {table} WHERE {id_column} = %s",
        (row_id,),
    ).fetchone()
    if stored is None or stored[0] != expected_payload:
        raise ValueError(f"{table} immutable identity/content conflict")


def _validate_final(
    *, evaluation_id: UUID, snapshot_digest: Sha256Hex,
    evidence_items: tuple[EvidenceItemV1, ...],
    evidence_chain: EvidenceChainV1,
    pattern_matches: tuple[PatternMatchV1, ...],
    jev_review: JevReviewV1 | None,
    decision: DecisionCandidateV1,
    lifecycle_event: DecisionStatusEventV1,
) -> None:
    if len(evidence_items) > 64:
        raise ValueError("final evidence exceeds 64 items")
    if len({item.evidence_id for item in evidence_items}) != len(evidence_items):
        raise ValueError("duplicate evidence identity")
    evidence_ids = {item.evidence_id for item in evidence_items}
    if any(item.evaluation_id != evaluation_id for item in evidence_items):
        raise ValueError("evidence evaluation ID mismatch")
    if evidence_chain.evaluation_snapshot_hash != snapshot_digest:
        raise ValueError("evaluation snapshot digest mismatch")
    if (
        evidence_chain.evaluation_id != evaluation_id
        or decision.evaluation_id != evaluation_id
        or decision.stage1_candidate_id != evidence_chain.stage1_candidate_id
        or decision.symbol != evidence_chain.symbol
        or decision.timeframe != evidence_chain.timeframe
        or decision.input_snapshot_hash != evidence_chain.input_snapshot_hash
        or lifecycle_event.decision_id != decision.decision_id
        or lifecycle_event.evaluation_id != evaluation_id
    ):
        raise ValueError("final evaluation/chain/decision identity or digest mismatch")
    referenced = (
        evidence_chain.supporting + evidence_chain.conflicting
        + evidence_chain.neutral + evidence_chain.degraded
        + decision.supporting_evidence_ids + decision.conflicting_evidence_ids
        + decision.degraded_evidence_ids
    )
    if not set(referenced) <= evidence_ids:
        raise ValueError("final chain/decision references unknown evidence ID")
    match_ids = {match.pattern_match_id for match in pattern_matches}
    if not set(evidence_chain.matched_patterns) <= match_ids:
        raise ValueError("final chain references unknown pattern match")
    for match in pattern_matches:
        if match.evaluation_id != evaluation_id:
            raise ValueError("pattern evaluation ID mismatch")
        if not set(
            match.required_evidence_ids
            + match.supporting_evidence_ids
            + match.conflicting_evidence_ids
        ) <= evidence_ids:
            raise ValueError("pattern references unknown evidence ID")
    if jev_review is None:
        if decision.jev_review_id is not None:
            raise ValueError("decision references absent Jev review")
    elif (
        jev_review.evaluation_id != evaluation_id
        or jev_review.evidence_chain_id != chain_id_for(evaluation_id)
        or decision.jev_review_id != jev_review.review_id
    ):
        raise ValueError("Jev review identity/reference mismatch")


def persist_final(
    conn: Connection, *, evaluation_id: UUID, snapshot_digest: str,
    evidence_items: Sequence[EvidenceItemV1],
    evidence_chain: EvidenceChainV1,
    pattern_matches: Sequence[PatternMatchV1],
    jev_review: JevReviewV1 | None,
    decision: DecisionCandidateV1,
    lifecycle_event: DecisionStatusEventV1,
    event_id: str, consumer_name: str, now,
) -> None:
    _persist_final_records(conn,evaluation_id=evaluation_id,snapshot_digest=snapshot_digest,
        evidence_items=evidence_items,evidence_chain=evidence_chain,pattern_matches=pattern_matches,
        jev_review=jev_review,decision=decision,lifecycle_event=lifecycle_event,event_id=event_id,
        consumer_name=consumer_name,now=now,emit_decision=True)


def _persist_final_records(conn, *, evaluation_id, snapshot_digest, evidence_items,
    evidence_chain, pattern_matches, jev_review, decision, lifecycle_event, event_id,
    consumer_name, now, emit_decision):
    """Insert final records and acknowledge the lease; caller commits or rolls back."""
    if conn.autocommit:
        raise ValueError("persist_final requires a caller-owned transaction")
    snapshot_digest = Sha256Hex(snapshot_digest)
    now = as_utc(now)
    items = tuple(evidence_items)
    matches = tuple(pattern_matches)
    _validate_final(
        evaluation_id=evaluation_id, snapshot_digest=snapshot_digest,
        evidence_items=items, evidence_chain=evidence_chain,
        pattern_matches=matches, jev_review=jev_review,
        decision=decision, lifecycle_event=lifecycle_event,
    )
    stored_snapshot = conn.execute(
        """SELECT snapshot_digest FROM phase9_evaluation_snapshots
           WHERE evaluation_id = %s""",
        (evaluation_id,),
    ).fetchone()
    if stored_snapshot is None or stored_snapshot[0] != snapshot_digest:
        raise ValueError("durable evaluation snapshot digest mismatch")
    if jev_review is not None:
        stored_review = conn.execute(
            """SELECT evaluation_id, evidence_chain_id, status,
                      request_digest, response_digest
               FROM phase9_jev_reviews WHERE review_id = %s""",
            (jev_review.review_id,),
        ).fetchone()
        expected_review = (
            evaluation_id, chain_id_for(evaluation_id),
            jev_review.status.value, str(jev_review.request_digest),
            str(jev_review.response_digest) if jev_review.response_digest else None,
        )
        if stored_review != expected_review:
            raise ValueError("Jev review is absent or does not match durable record")
    for item in items:
        payload = _payload("PHASE9_EVIDENCE_ITEM_V1", item)
        digest = canonical_sha256(payload)
        _insert_exact(
            conn,
            sql="""INSERT INTO phase9_evidence_items
                   (evidence_id, evaluation_id, evidence_type, semantic_code,
                    source_ref, observed_at, availability_status,
                    canonical_digest, payload)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING""",
            values=(
                item.evidence_id, evaluation_id, item.evidence_type.value,
                item.semantic_code, item.source_ref, item.observed_at,
                item.availability_status.value, digest, Jsonb(payload),
            ),
            table="phase9_evidence_items", id_column="evidence_id",
            row_id=item.evidence_id, expected_payload=payload,
        )
    chain_id = chain_id_for(evaluation_id)
    chain_payload = _payload("PHASE9_EVIDENCE_CHAIN_V1", evidence_chain)
    _insert_exact(
        conn,
        sql="""INSERT INTO phase9_evidence_chains
               (chain_id, evaluation_id, evaluation_snapshot_hash,
                input_snapshot_hash, payload)
               VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        values=(
            chain_id, evaluation_id, snapshot_digest,
            evidence_chain.input_snapshot_hash, Jsonb(chain_payload),
        ),
        table="phase9_evidence_chains", id_column="chain_id",
        row_id=chain_id, expected_payload=chain_payload,
    )
    for match in matches:
        payload = _payload("PHASE9_PATTERN_MATCH_V1", match)
        _insert_exact(
            conn,
            sql="""INSERT INTO phase9_pattern_matches
                   (pattern_match_id, evaluation_id, pattern_type, direction,
                    status, pattern_policy_version, payload)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
            values=(
                match.pattern_match_id, evaluation_id, match.pattern_type,
                match.direction.value, match.status.value,
                match.pattern_policy_version, Jsonb(payload),
            ),
            table="phase9_pattern_matches", id_column="pattern_match_id",
            row_id=match.pattern_match_id, expected_payload=payload,
        )
    if not emit_decision:
        ack(conn,event_id=event_id,consumer_name=consumer_name,now=now)
        return
    decision_payload = _payload("PHASE9_DECISION_CANDIDATE_V1", decision)
    _insert_exact(
        conn,
        sql="""INSERT INTO phase9_decision_candidates
               (decision_id, evaluation_id, stage1_candidate_id, symbol,
                market, timeframe, valid_until, eligible, direction_bias,
                confidence_band, input_snapshot_hash, jev_review_id, payload)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT DO NOTHING""",
        values=(
            decision.decision_id, evaluation_id, decision.stage1_candidate_id,
            decision.symbol, decision.market, decision.timeframe,
            decision.valid_until, decision.eligible,
            decision.direction_bias.value, decision.confidence_band.value,
            decision.input_snapshot_hash, decision.jev_review_id,
            Jsonb(decision_payload),
        ),
        table="phase9_decision_candidates", id_column="decision_id",
        row_id=decision.decision_id, expected_payload=decision_payload,
    )
    status_payload = _payload("PHASE9_DECISION_STATUS_EVENT_V1", lifecycle_event)
    _insert_exact(
        conn,
        sql="""INSERT INTO phase9_decision_status_events
               (event_id, decision_id, evaluation_id, status,
                event_time, reason_code, payload)
               VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        values=(
            lifecycle_event.event_id, decision.decision_id, evaluation_id,
            lifecycle_event.status, lifecycle_event.event_time,
            lifecycle_event.reason_code, Jsonb(status_payload),
        ),
        table="phase9_decision_status_events", id_column="event_id",
        row_id=lifecycle_event.event_id, expected_payload=status_payload,
    )
    if decision.supersedes_decision_id is not None:
        from .lifecycle import acquire_decision_advisory_lock,load_decision,append_status
        from .decision import build_status_event
        acquire_decision_advisory_lock(conn, decision.supersedes_decision_id)
        old_row = conn.execute('SELECT payload FROM phase9_decision_candidates WHERE decision_id=%s',
                               (decision.supersedes_decision_id,)).fetchone()
        if old_row is None:
            raise ValueError('superseded decision is missing')
        old = load_decision(old_row[0])
        if (old.symbol,old.market,old.timeframe,old.stage1_candidate_id)!=(decision.symbol,decision.market,decision.timeframe,decision.stage1_candidate_id):
            raise ValueError('superseded decision belongs to a different candidate')
        append_status(conn,build_status_event(decision=old,status='SUPERSEDED',event_time=decision.created_at,
            reason_code='MATERIAL_REVALIDATION',supersedes_decision_id=decision.decision_id))
    ack(conn, event_id=event_id, consumer_name=consumer_name, now=now)


def persist_jev_review(conn: Connection, *, review: JevReviewV1) -> None:
    """Persist a validated review in an earlier caller-owned transaction."""
    if conn.autocommit:
        raise ValueError("Jev review persistence requires a caller-owned transaction")
    if not isinstance(review, JevReviewV1):
        raise TypeError("review must be JevReviewV1")
    if review.evidence_chain_id != chain_id_for(review.evaluation_id):
        raise ValueError("Jev review evidence chain ID mismatch")
    payload = _payload("PHASE9_JEV_REVIEW_V1", review)
    if len(canonical_json(payload).encode("utf-8")) > 32_768:
        raise ValueError("Jev review exceeds 32 KiB")
    _insert_exact(
        conn,
        sql="""INSERT INTO phase9_jev_reviews
               (review_id, evaluation_id, evidence_chain_id, status,
                reason_code, request_digest, response_digest, provider,
                model, model_version, prompt_version, payload)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT DO NOTHING""",
        values=(
            review.review_id, review.evaluation_id, review.evidence_chain_id,
            review.status.value, review.reason_code, review.request_digest,
            review.response_digest, review.provider, review.model,
            review.model_version, review.prompt_version, Jsonb(payload),
        ),
        table="phase9_jev_reviews", id_column="review_id",
        row_id=review.review_id, expected_payload=payload,
    )
