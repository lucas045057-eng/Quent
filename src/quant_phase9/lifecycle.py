"""Bounded append-only decision lifecycle; immutable candidates are never edited."""
from dataclasses import fields
from datetime import datetime, timezone
from hashlib import sha256
from uuid import UUID
import json

from psycopg.types.json import Jsonb
from .canonical import canonical_json
from .contracts import (DecisionCandidateV1, DecisionDirectionBiasV1, ConfidenceBandV1,
    PatternMatchStatusV1, MissingEvidenceV1, PolicyDataStatusV1, evidence_type_from_value)
from .decision import build_status_event
from .persistence import _insert_exact, _payload


def decision_advisory_lock_key(decision_id: UUID | str) -> int:
    'Return a stable, namespaced PostgreSQL bigint lock key for one decision.'
    identity = UUID(str(decision_id))
    digest = sha256(b"PHASE9_DECISION_CANDIDATE_LOCK_V1:" + identity.bytes).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def acquire_decision_advisory_lock(conn, decision_id: UUID | str, *, wait=True) -> bool:
    'Acquire a transaction-scoped lock; it is released on commit or rollback.'
    key = decision_advisory_lock_key(decision_id)
    if wait:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        return True
    return conn.execute(
        "SELECT pg_try_advisory_xact_lock(%s)", (key,)
    ).fetchone()[0]


def load_decision(payload):
    if not isinstance(payload,dict) or set(payload) != {'schema','value'} or payload['schema'] != 'PHASE9_DECISION_CANDIDATE_V1':
        raise ValueError('durable decision envelope is invalid')
    data = dict(payload['value'])
    if set(data) != {f.name for f in fields(DecisionCandidateV1)}:
        raise ValueError('durable decision fields are invalid')
    def timestamp(value):
        result = datetime.fromisoformat(value.replace('Z','+00:00'))
        if result.tzinfo is None or result.utcoffset().total_seconds() != 0:
            raise ValueError('durable decision timestamp must be UTC')
        return result.astimezone(timezone.utc)
    for name in ('decision_id','evaluation_id','jev_review_id','supersedes_decision_id'):
        data[name] = UUID(data[name]) if data[name] is not None else None
    for name in ('created_at','valid_until'):
        data[name] = timestamp(data[name])
    for name,enum in (('direction_bias',DecisionDirectionBiasV1),('confidence_band',ConfidenceBandV1),
                      ('pattern_status',PatternMatchStatusV1)):
        data[name] = enum(data[name])
    for name in ('supporting_evidence_ids','conflicting_evidence_ids','degraded_evidence_ids'):
        data[name] = tuple(UUID(value) for value in data[name])
    missing = []
    for value in data['missing_evidence']:
        missing.append(MissingEvidenceV1(**{**value,'evidence_type':evidence_type_from_value(value['evidence_type']),
            'source_status':PolicyDataStatusV1(value['source_status']),
            **{name:timestamp(value[name]) if value[name] is not None else None
               for name in ('source_timestamp','captured_at')}}))
    data['missing_evidence'] = tuple(missing)
    data['veto_reasons'] = tuple(data['veto_reasons'])
    data['reason_codes'] = tuple(data['reason_codes'])
    return DecisionCandidateV1(**data)


def append_status(conn, event):
    data = _payload('PHASE9_DECISION_STATUS_EVENT_V1',event)
    _insert_exact(conn,sql='''INSERT INTO phase9_decision_status_events
        (event_id,decision_id,evaluation_id,status,event_time,reason_code,payload)
        VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
        values=(event.event_id,event.decision_id,event.evaluation_id,event.status,event.event_time,
                event.reason_code,Jsonb(data)),table='phase9_decision_status_events',
        id_column='event_id',row_id=event.event_id,expected_payload=data)


def expire_decisions(conn, *, now, limit=32):
    if isinstance(limit,bool) or not 1 <= limit <= 32:
        raise ValueError('lifecycle page limit exceeds ceiling')
    rows = conn.execute('''SELECT d.decision_id,d.payload FROM phase9_decision_candidates d
        WHERE d.valid_until<=%s AND (SELECT status FROM phase9_decision_status_events s
            WHERE s.decision_id=d.decision_id ORDER BY event_time DESC,created_at DESC,event_id DESC LIMIT 1)='ACTIVE'
        ORDER BY d.valid_until,d.decision_id LIMIT %s''',(now,limit)).fetchall()
    expired = 0
    for decision_id,payload in rows:
        if not acquire_decision_advisory_lock(conn, decision_id, wait=False):
            continue
        latest = conn.execute('''SELECT status FROM phase9_decision_status_events
            WHERE decision_id=%s ORDER BY event_time DESC,created_at DESC,event_id DESC LIMIT 1''',
            (decision_id,)).fetchone()
        if latest is None or latest[0] != 'ACTIVE':
            continue
        decision = load_decision(payload)
        append_status(conn,build_status_event(decision=decision,status='EXPIRED',
            event_time=decision.valid_until,reason_code='DECISION_TTL_EXPIRED'))
        expired += 1
    return expired
