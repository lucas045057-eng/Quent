"""One bounded durable scheduler; only admitted candidates can enter it."""
from dataclasses import replace
from datetime import datetime, timedelta
import json

from psycopg.types.json import Jsonb
from .canonical import canonical_json, canonical_sha256, evaluation_id_for
from .contracts import EvaluationIdentityV1
from .intake import _event_envelope, _event_from_envelope


def semantic_digest(decision, items, review):
    # Exclude evidence IDs, numeric interpretation text, clocks and raw-row hashes.
    evidence = tuple(sorted(set((str(i.evidence_type),i.semantic_code,str(i.direction),
        str(i.strength),str(i.availability_status),str(i.freshness_status),str(i.quality_status),
        str(i.coverage_status),str(i.source_phase),i.provenance.split(';',1)[0]) for i in items)))
    return str(canonical_sha256(dict(schema='PHASE9_MATERIAL_SEMANTICS_V1',
        direction=decision.direction_bias,eligible=decision.eligible,confidence=decision.confidence_band,
        pattern=decision.matched_pattern,pattern_status=decision.pattern_status,
        vetoes=tuple(sorted(decision.veto_reasons)),core_evidence=evidence,
        jev=None if review is None else (review.status,review.relation,review.conflict_severity,
            review.dominant_context,tuple(n.reason_code for n in review.unresolved_conflicts)))))


def rule_for(policy, timeframe, pattern=None):
    rules = tuple(r for r in policy.manifest.policy_content.revalidation_rules
                  if r.timeframe==timeframe and (pattern is None or r.pattern_type==pattern))
    return min(rules,key=lambda r:r.cadence_seconds) if rules else None


def register(conn, *, event, identity, decision, items, review, policy, now):
    rule = rule_for(policy,identity.timeframe,decision.matched_pattern)
    if rule is None or event.candidate_valid_until is None or event.candidate_valid_until<=now:
        return
    conn.execute('''INSERT INTO phase9_revalidation_cursors(stage1_candidate_id,timeframe,
        policy_generation,event_payload,next_due,decision_id,semantic_digest)
        VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
        (identity.stage1_candidate_id,identity.timeframe,identity.policy_generation,
         Jsonb(json.loads(canonical_json(_event_envelope(event)))),now+timedelta(seconds=rule.cadence_seconds),
         decision.decision_id,semantic_digest(decision,items,review)))


def request(conn, *, stage1_candidate_id, timeframe, policy_generation, now):
    return conn.execute('''UPDATE phase9_revalidation_cursors SET requested=TRUE,
        next_due=LEAST(next_due,%s) WHERE stage1_candidate_id=%s AND timeframe=%s
        AND policy_generation=%s AND active RETURNING stage1_candidate_id''',
        (now,stage1_candidate_id,timeframe,policy_generation)).fetchone() is not None


def _identity(payload):
    data = dict(payload)
    for name in ('evaluation_window_start','evaluation_window_end'):
        data[name] = datetime.fromisoformat(data[name].replace('Z','+00:00'))
    return EvaluationIdentityV1(**data)


def claim(conn, *, policy, consumer, now, limit, max_per_minute):
    minute = now.replace(second=0,microsecond=0)
    conn.execute('DELETE FROM phase9_revalidation_budget WHERE minute<%s',(minute,))
    conn.execute('INSERT INTO phase9_revalidation_budget VALUES(%s,0) ON CONFLICT DO NOTHING',(minute,))
    used = conn.execute('SELECT runs FROM phase9_revalidation_budget WHERE minute=%s FOR UPDATE',(minute,)).fetchone()[0]
    slots = min(limit,max_per_minute-used)
    if slots<=0:
        return ()
    rows = conn.execute('''SELECT stage1_candidate_id,timeframe,policy_generation,event_payload,
        generation,current_evaluation_id,current_identity,requested FROM phase9_revalidation_cursors
        WHERE active AND policy_generation=%s AND next_due<=%s
        ORDER BY requested DESC,next_due,stage1_candidate_id,timeframe
        LIMIT %s FOR UPDATE SKIP LOCKED''',
        (f'{policy.manifest_version}:{policy.manifest_digest}',now,limit)).fetchall()
    jobs = []
    for candidate_id,timeframe,generation_key,envelope,generation,eid,identity_payload,requested in rows:
        if len(jobs)>=slots:
            break
        event = _event_from_envelope(envelope)
        key = (candidate_id,timeframe,generation_key)
        rule = rule_for(policy,timeframe)
        if rule is None or event.candidate_valid_until is None or event.candidate_valid_until<=now:
            conn.execute('UPDATE phase9_revalidation_cursors SET active=FALSE WHERE stage1_candidate_id=%s AND timeframe=%s AND policy_generation=%s',key)
            continue
        count = conn.execute('''SELECT count(*) FROM phase9_evaluations WHERE stage1_candidate_id=%s
            AND timeframe=%s AND policy_generation=%s AND material_change_generation LIKE 'revalidate:%%'
            AND created_at>=%s''',(*key,minute)).fetchone()[0]
        if count>=rule.max_runs_per_minute:
            continue
        recovering = eid is not None
        if recovering:
            state,owner,lease,attempt = conn.execute('''SELECT evaluation_state,lease_owner,
                lease_expires_at,attempt_count FROM phase9_evaluations WHERE evaluation_id=%s FOR UPDATE''',(eid,)).fetchone()
            if lease is not None and lease>now:
                continue
            if state not in {'RUNNING','QUEUED','WAITING_JEV'} or attempt>=3:
                conn.execute("UPDATE phase9_evaluations SET evaluation_state='FAILED',reason_code='REVALIDATION_ATTEMPTS_EXHAUSTED',lease_owner=NULL,lease_expires_at=NULL WHERE evaluation_id=%s",(eid,))
                conn.execute('UPDATE phase9_revalidation_cursors SET current_evaluation_id=NULL,current_identity=NULL,next_due=%s WHERE stage1_candidate_id=%s AND timeframe=%s AND policy_generation=%s',
                             (now+timedelta(seconds=rule.cadence_seconds),*key))
                continue
            identity = _identity(identity_payload)
        else:
            generation += 1
            minutes = {'15m':15,'1H':60,'4H':240}[timeframe]
            identity = EvaluationIdentityV1(candidate_id,event.market,event.symbol,timeframe,
                now-timedelta(minutes=minutes),now,generation_key,f'revalidate:{generation}')
            eid = evaluation_id_for(identity)
            conn.execute('''INSERT INTO phase9_evaluations(evaluation_id,stage1_candidate_id,symbol,
                market,timeframe,evaluation_state,intake_disposition,candidate_valid_until,policy_generation,
                material_change_generation,reason_code,created_at) VALUES(%s,%s,%s,%s,%s,'QUEUED','ADMITTED',%s,%s,%s,%s,%s)''',
                (eid,candidate_id,event.symbol,event.market,timeframe,event.candidate_valid_until,generation_key,
                 identity.material_change_generation,'MATERIAL_REVALIDATION' if requested else 'SCHEDULED_REVALIDATION',now))
        conn.execute('''UPDATE phase9_evaluations SET evaluation_state='RUNNING',lease_owner=%s,
            lease_expires_at=%s,attempt_count=attempt_count+1,updated_at=%s WHERE evaluation_id=%s''',
            (consumer,now+timedelta(seconds=120),now,eid))
        conn.execute('''UPDATE phase9_revalidation_cursors SET generation=%s,current_evaluation_id=%s,
            current_identity=%s,requested=%s WHERE stage1_candidate_id=%s AND timeframe=%s AND policy_generation=%s''',
            (generation,eid,Jsonb(json.loads(canonical_json(identity))),requested if recovering else False,*key))
        jobs.append((event,(identity,)))
    conn.execute('UPDATE phase9_revalidation_budget SET runs=runs+%s WHERE minute=%s',(len(jobs),minute))
    return tuple(jobs)


def finish(conn, *, identity, decision, digest, policy, now):
    rule = rule_for(policy,identity.timeframe)
    conn.execute('''UPDATE phase9_revalidation_cursors SET current_evaluation_id=NULL,
        current_identity=NULL,decision_id=%s,semantic_digest=%s,
        next_due=CASE WHEN requested THEN %s ELSE %s END
        WHERE stage1_candidate_id=%s AND timeframe=%s AND policy_generation=%s''',
        (decision.decision_id,digest,now,now+timedelta(seconds=rule.cadence_seconds),
         identity.stage1_candidate_id,identity.timeframe,identity.policy_generation))
