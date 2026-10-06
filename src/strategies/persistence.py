"""Append V2 strategy records atomically alongside the existing candidate lifecycle."""
from uuid import NAMESPACE_URL, uuid5
from psycopg.types.json import Jsonb
from quant_phase9.persistence import _persist_final_records
from quant_phase9.decision import build_status_event
from quant_phase9.lifecycle import acquire_decision_advisory_lock
from strategies.integration.phase9_bridge import bound_inputs, build_v2_candidate

def persist_artifact(connection, *, kind, record, now):
    if connection.autocommit:raise ValueError("caller-owned transaction required")
    artifact_id=uuid5(NAMESPACE_URL,"v2-artifact:"+kind+":"+record.digest)
    payload=record.model_dump(mode="json")
    connection.execute("""INSERT INTO strategy_v2_artifacts
        (artifact_id,artifact_kind,canonical_digest,schema_version,payload,created_at)
        VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
        (artifact_id,kind,record.digest,record.schema_version,Jsonb(payload),now))
    stored=connection.execute("SELECT canonical_digest,payload FROM strategy_v2_artifacts WHERE artifact_id=%s",
        (artifact_id,)).fetchone()
    if stored is None or stored!=(record.digest,payload):raise ValueError("immutable strategy artifact conflict")
    return artifact_id

def assert_event_lease_held(connection, *, event_id, evaluation_ids, consumer_name, now):
    """Lock and validate one shared outbox lease plus its running evaluations."""
    if connection.autocommit:
        raise ValueError("caller-owned transaction required")
    lease = connection.execute(
        """SELECT phase9_state, lease_owner, lease_expires_at
           FROM outbox_events WHERE event_id=%s FOR UPDATE""",
        (event_id,),
    ).fetchone()
    if (
        lease is None or lease[0] != "LEASED" or lease[1] != consumer_name
        or lease[2] is None or lease[2] <= now
    ):
        raise ValueError("V2 writer lost durable evaluation lease")
    owner = lease[1]
    for evaluation_id in evaluation_ids:
        evaluation = connection.execute(
            """SELECT evaluation_state, lease_owner, lease_expires_at
               FROM phase9_evaluations WHERE evaluation_id=%s FOR UPDATE""",
            (evaluation_id,),
        ).fetchone()
        if (
            evaluation is None or evaluation[0] != "RUNNING" or evaluation[1] != owner
            or evaluation[2] is None or evaluation[2] <= now
        ):
            raise ValueError("V2 writer lost durable evaluation lease")
    return owner


def persist_strategy_decision(
    connection, *, result, snapshot, now, validated_event_lease_owner=None,
):
    if connection.autocommit:raise ValueError("caller-owned transaction required")
    inputs=bound_inputs(snapshot,result)
    decision,items,chain,matches,thesis=build_v2_candidate(snapshot,result,inputs)
    acquire_decision_advisory_lock(connection,decision.decision_id)
    previous=connection.execute("SELECT payload FROM phase9_decision_candidates WHERE decision_id=%s",
        (decision.decision_id,)).fetchone()
    if previous is None and snapshot.identity.material_change_generation.startswith("revalidate:"):
        from dataclasses import replace
        cursor=connection.execute("""SELECT decision_id FROM phase9_revalidation_cursors
            WHERE stage1_candidate_id=%s AND timeframe=%s AND policy_generation=%s FOR UPDATE""",
            (snapshot.stage1_candidate_id,snapshot.timeframe,snapshot.identity.policy_generation)).fetchone()
        if cursor is not None:
            decision=replace(decision,supersedes_decision_id=cursor[0])
    elif previous is not None:
        from quant_phase9.lifecycle import load_decision
        from dataclasses import replace
        old=load_decision(previous[0])
        decision=replace(decision,supersedes_decision_id=old.supersedes_decision_id)
    if previous is None:
        if now<result.evaluated_at or now>=decision.valid_until:
            raise ValueError("execution result expired before persistence")
        if validated_event_lease_owner is None:
            lease=connection.execute("""SELECT phase9_state,lease_owner,lease_expires_at FROM outbox_events
                WHERE event_id=%s FOR UPDATE""",(snapshot.candidate_event.event_id,)).fetchone()
            owner=lease[1] if lease is not None else None
            event_lease_held=(
                lease is not None and lease[0]=="LEASED" and lease[2] is not None
                and lease[2]>now
            )
        else:
            owner=validated_event_lease_owner
            event_lease_held=isinstance(owner,str) and bool(owner)
        evaluation=connection.execute("""SELECT evaluation_state,lease_owner,lease_expires_at
            FROM phase9_evaluations WHERE evaluation_id=%s FOR UPDATE""",(snapshot.evaluation_id,)).fetchone()
        if (not event_lease_held or evaluation is None or evaluation[0]!="RUNNING"
            or evaluation[1]!=owner or evaluation[2] is None or evaluation[2]<=now):
            raise ValueError("V2 writer lost durable evaluation lease")
    else:owner="v2-idempotent"
    records={"execution_result_id":("EXECUTION_RESULT",result),"analysis_id":("ANALYSIS",inputs.analysis),
        "thesis_id":("THESIS",thesis),"inputs_id":("EXECUTION_INPUTS",inputs),"policy_id":("EXECUTION_POLICY",inputs.policy)}
    ids={name:persist_artifact(connection,kind=kind,record=record,now=result.evaluated_at)
        for name,(kind,record) in records.items()}
    _persist_final_records(connection,evaluation_id=snapshot.evaluation_id,snapshot_digest=snapshot.snapshot_digest,
        evidence_items=items,evidence_chain=chain,pattern_matches=matches,jev_review=None,decision=decision,
        lifecycle_event=build_status_event(decision=decision,status="ACTIVE",event_time=result.evaluated_at,
            reason_code="V2_POLICY_OUTCOME"),event_id=snapshot.candidate_event.event_id,consumer_name=owner,now=now,emit_decision=True)
    values=(decision.decision_id,snapshot.evaluation_id,ids["execution_result_id"],ids["analysis_id"],
        ids["thesis_id"],ids["inputs_id"],ids["policy_id"],result.digest,inputs.policy.digest,
        inputs.view.digest,str(decision.input_snapshot_hash),result.evaluated_at)
    connection.execute("""INSERT INTO strategy_v2_decision_bindings(decision_id,evaluation_id,
        execution_result_id,analysis_id,thesis_id,inputs_id,policy_id,execution_result_digest,policy_digest,
        recheck_digest,input_snapshot_hash,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT DO NOTHING""",values)
    stored=connection.execute("""SELECT decision_id,evaluation_id,execution_result_id,analysis_id,
        thesis_id,inputs_id,policy_id,execution_result_digest,policy_digest,recheck_digest,input_snapshot_hash,created_at
        FROM strategy_v2_decision_bindings WHERE decision_id=%s""",(decision.decision_id,)).fetchone()
    if stored!=values:raise ValueError("immutable V2 decision binding conflict")
    connection.execute("""UPDATE phase9_evaluations SET evaluation_state='COMPLETED',
        lease_owner=NULL,lease_expires_at=NULL,updated_at=%s WHERE evaluation_id=%s""",(now,snapshot.evaluation_id))
    return decision
