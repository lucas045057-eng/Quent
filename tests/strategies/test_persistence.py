from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import pytest
from tests.quant_phase9.test_persistence import db
from tests.quant_phase9.test_snapshot import _candidate, _identity, _seed_evaluation, _begin_snapshot
from tests.strategies.test_execution_policy import NOW, setup
from quant_phase9.snapshot import build_snapshot, persist_evaluation_snapshot
from quant_phase9.canonical import evaluation_id_for
from quant_phase9.intake import claim_pending
from strategies.execution.execution_policy import evaluate_execution

def seeded_many(db, timeframes=("15m", "1H", "4H")):
    from strategies.integration.phase9_bridge import StrategyV2SourceReader
    theses,view,policy=setup()
    result=evaluate_execution(theses,view,policy=policy,now=NOW)
    event=_candidate()
    event=replace(event,symbol="SOLUSDT",candidate_created_at=NOW-timedelta(minutes=1),
        source_as_of=NOW,created_at=NOW,stage1_policy_version="QUANT_PAPER_V2",
        candidate_valid_until=NOW+timedelta(hours=1))
    # Use a coherent canonical event built by the normal writer.
    from quant_phase1.stage1 import Stage1Result
    from quant_phase1.contracts import DataStatus
    from quant_phase9.intake import build_stage1_candidate_event
    stage=Stage1Result("SOLUSDT","A","V2_EDGE",DataStatus.AVAILABLE,(),{},"TREND_CONTINUATION",
        ("V2_EDGE",),timestamp=NOW)
    event=build_stage1_candidate_event(screening_result_id=event.stage1_candidate_id,run_id=22,screening_result=stage,
        market="USDT_PERPETUAL",instrument_scope={"category":"USDT-FUTURES","quote_coin":"USDT","contract_type":"perpetual","status":"online","in_scope":"true"},
        candidate_created_at=NOW,candidate_valid_until=NOW+timedelta(hours=1),
        stage1_policy_version="QUANT_PAPER_V2",source_as_of=NOW)
    windows={"15m":timedelta(minutes=15),"1H":timedelta(hours=1),"4H":timedelta(hours=4)}
    identities=tuple(replace(_identity(event),timeframe=timeframe,
        evaluation_window_start=NOW-windows[timeframe],evaluation_window_end=NOW,
        policy_generation="2.0.0:"+policy.digest) for timeframe in timeframes)
    for identity in identities:
        _seed_evaluation(db,event,identity)
    claimed=claim_pending(db,consumer_name="v2-test",limit=1,now=NOW,lease_until=NOW+timedelta(minutes=2))
    assert claimed
    for identity in identities:
        db.execute("UPDATE phase9_evaluations SET lease_owner=%s,lease_expires_at=%s,evaluation_state='RUNNING' WHERE evaluation_id=%s",
            ("v2-test",NOW+timedelta(minutes=2),evaluation_id_for(identity)))
    db.commit()
    manifest=SimpleNamespace(manifest_version="2.0.0",manifest_digest=policy.digest,
        pattern_policy_version="2.0.0",decision_policy_version="2.0.0")
    reader=StrategyV2SourceReader(analysis=view.analysis_snapshots[0],theses=theses,view=view,policy=policy,result=result)
    _begin_snapshot(db)
    snapshots=tuple(build_snapshot(db,identity=identity,stage1_candidate=event,as_of=NOW,source_reader=reader,
        policy_manifest=manifest,code_version="b"*40) for identity in identities)
    for snapshot in snapshots:
        persist_evaluation_snapshot(db,snapshot=snapshot)
    db.commit()
    return result,snapshots


def seeded(db):
    result,snapshots=seeded_many(db,("15m",))
    return result,snapshots[0]

def test_durable_result_is_idempotent_and_hash_bound(db):
    from strategies.persistence import persist_strategy_decision
    result,snapshot=seeded(db)
    first=persist_strategy_decision(db,result=result,snapshot=snapshot,now=NOW);db.commit()
    second=persist_strategy_decision(db,result=result,snapshot=snapshot,now=NOW);db.commit()
    assert first==second and first.eligible and first.matched_pattern=="MARKET_EVIDENCE_CHAIN"
    assert db.execute("SELECT count(*) FROM strategy_v2_decision_bindings").fetchone()[0]==1
    with pytest.raises(ValueError):
        persist_strategy_decision(db,result=result.model_copy(update={"policy_digest":"f"*64}),snapshot=snapshot,now=NOW)
    db.rollback()

def test_old_profile_cannot_produce_new_candidate(db):
    from strategies.persistence import persist_strategy_decision
    result,snapshot=seeded(db)
    with pytest.raises(ValueError):
        persist_strategy_decision(db,result=result,snapshot=replace(snapshot,pattern_policy_version="1.0.0"),now=NOW)
    db.rollback()

def test_only_persisted_v2_result_can_reach_active_projection(db):
    from strategies.persistence import persist_strategy_decision
    result,snapshot=seeded(db)
    decision=persist_strategy_decision(db,result=result,snapshot=snapshot,now=NOW);db.commit()
    with pytest.raises(Exception):
        db.execute("DELETE FROM strategy_v2_decision_bindings WHERE decision_id=%s",(decision.decision_id,))
    db.rollback()
    binding=db.execute("SELECT execution_result_digest,input_snapshot_hash FROM strategy_v2_decision_bindings WHERE decision_id=%s",(decision.decision_id,)).fetchone()
    assert binding==(result.digest,str(decision.input_snapshot_hash))

def test_raw_candidate_without_v2_binding_is_rejected_at_commit(db):
    import psycopg
    from strategies.integration.phase9_bridge import bound_inputs, build_v2_candidate
    from quant_phase9.persistence import _persist_final_records
    from quant_phase9.decision import build_status_event
    result,snapshot=seeded(db)
    inputs=bound_inputs(snapshot,result)
    decision,items,chain,matches,_=build_v2_candidate(snapshot,result,inputs)
    _persist_final_records(db,evaluation_id=snapshot.evaluation_id,snapshot_digest=snapshot.snapshot_digest,
        evidence_items=items,evidence_chain=chain,pattern_matches=matches,jev_review=None,decision=decision,
        lifecycle_event=build_status_event(decision=decision,status="ACTIVE",event_time=NOW,reason_code="UNBOUND_TEST"),
        event_id=snapshot.candidate_event.event_id,consumer_name="v2-test",now=NOW,emit_decision=True)
    with pytest.raises(psycopg.errors.CheckViolation):db.commit()
    db.rollback()
    assert db.execute("SELECT count(*) FROM phase9_decision_candidates WHERE decision_id=%s",(decision.decision_id,)).fetchone()[0]==0


def test_v2_persists_all_horizons_before_releasing_shared_event_lease(db):
    from strategies.persistence import assert_event_lease_held, persist_strategy_decision

    result,snapshots=seeded_many(db)
    lease_owner=assert_event_lease_held(db,event_id=snapshots[0].candidate_event.event_id,
        evaluation_ids=[snapshot.evaluation_id for snapshot in snapshots],
        consumer_name="v2-test",now=NOW)
    decisions=tuple(
        persist_strategy_decision(db,result=result,snapshot=snapshot,now=NOW,
            validated_event_lease_owner=lease_owner)
        for snapshot in snapshots
    )
    db.commit()

    assert len(decisions)==3
    assert db.execute("SELECT count(*) FROM phase9_evaluations WHERE evaluation_state='COMPLETED'").fetchone()[0]==3
    assert db.execute("SELECT count(*) FROM strategy_v2_decision_bindings").fetchone()[0]==3
    assert db.execute("SELECT phase9_state FROM outbox_events WHERE event_id=%s",
        (snapshots[0].candidate_event.event_id,)).fetchone()==("ACKNOWLEDGED",)
