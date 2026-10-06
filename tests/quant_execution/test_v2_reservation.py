from tests.quant_nautilus.test_paper_restart import paper_db
from dataclasses import replace
from decimal import Decimal as D
from datetime import timedelta
import psycopg
import pytest
from tests.quant_execution.test_persistence import store_db,seeded_intent
from tests.quant_execution.fixtures import NOW
from quant_execution.contracts import ExecutionResultV1,PositionSnapshotV1,intent_body,make_intent
from quant_execution.persistence import ExecutionStore,ReservationRejected
from quant_phase9.canonical import canonical_sha256

def configured(conn,limit=1):
    store,intent,policy=seeded_intent(conn)
    policy=replace(policy,risk_config_digest="a"*64,max_open_positions=limit,max_open_intents=limit)
    body=intent_body(intent);body["risk_policy_hash"]=str(canonical_sha256(policy))
    return store,make_intent(**body),policy

def filled(store,intent):
    store.record_result(ExecutionResultV1(intent.intent_id,"fill-"+intent.intent_id.hex,intent.client_order_id,(),
        "FILLED",intent.approved_quantity,intent.approved_quantity,D(0),intent.reference_price,D(0),"USDT",None,
        "ACTIVE",NOW,NOW,"fixture","fixture"))

def position(intent,quantity,at=NOW):
    return PositionSnapshotV1(intent.account_id,intent.venue,intent.canonical_symbol,"PAPER",
        "LONG" if quantity else "FLAT",quantity,"BASE",intent.reference_price if quantity else None,
        D(10000),D(9000),intent.max_margin if quantity else D(0),intent.reference_price,D(0),D(0),D(0),None,
        "ACTIVE" if quantity else "NOT_REQUIRED",(intent.client_order_id,),("fixture-fill",),"RECONCILED",at,"fixture")

def test_filled_but_open_position_keeps_risk_reserved(store_db):
    from quant_execution.persistence import active_execution_state
    with psycopg.connect(store_db) as conn:
        store,intent,policy=configured(conn);store.reserve(intent,policy,now=NOW);filled(store,intent)
        store.record_position(position(intent,intent.approved_quantity))
        state=active_execution_state(conn,account_id=intent.account_id,now=NOW)
        assert len(state.active_positions)==1 and state.reserved_risk==intent.risk_budget
        assert conn.execute("SELECT state FROM execution_reservations").fetchone()[0]=="ACTIVE"

def test_terminal_history_allows_next_trade(store_db):
    with psycopg.connect(store_db) as conn:
        store,intent,policy=configured(conn);store.reserve(intent,policy,now=NOW);filled(store,intent)
        store.record_position(position(intent,intent.approved_quantity))
        store.record_position(position(intent,D(0),NOW+timedelta(seconds=1)))
        assert conn.execute("SELECT state FROM execution_reservations").fetchone()[0]=="RELEASED"
        store,second,policy=configured(conn)
        assert store.reserve(second,policy,now=NOW+timedelta(seconds=1))

def test_unknown_submission_blocks_new_exposure(store_db):
    with psycopg.connect(store_db) as conn:
        store,intent,policy=configured(conn,2);store.reserve(intent,policy,now=NOW)
        owner=store.acquire_owner(intent.account_id,"fixture",now=NOW,lease_seconds=5)
        store.begin_submission(intent.intent_id,"fixture",owner,now=NOW);store.recover_pending(intent.account_id)
        _,second,policy=configured(conn,2)
        with pytest.raises(ReservationRejected,match="UNRESOLVED"):store.reserve(second,policy,now=NOW)

def test_tighter_config_blocks_new_risk_without_forced_liquidation(store_db):
    with psycopg.connect(store_db) as conn:
        store,intent,policy=configured(conn,2);store.reserve(intent,policy,now=NOW);filled(store,intent)
        store.record_position(position(intent,intent.approved_quantity))
        _,second,policy=configured(conn,2)
        policy=replace(policy,max_reserved_risk=D(".01"));body=intent_body(second)
        body["risk_policy_hash"]=str(canonical_sha256(policy));second=make_intent(**body)
        with pytest.raises(ReservationRejected):store.reserve(second,policy,now=NOW)
        assert conn.execute("SELECT count(*) FROM execution_reservations WHERE state='ACTIVE'").fetchone()[0]==1

def test_simultaneous_reservations_respect_configured_position_limit(store_db):
    from concurrent.futures import ThreadPoolExecutor
    with psycopg.connect(store_db) as conn:
        _,one,policy=configured(conn,1);_,two,_=configured(conn,1);conn.commit()
    def attempt(intent):
        with psycopg.connect(store_db) as conn:
            try:return ExecutionStore(conn).reserve(intent,policy,now=NOW)
            except ReservationRejected:return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt,(one,two)))==[False,True]

def test_add_position_requires_configured_rule_and_updated_protection(store_db):
    with psycopg.connect(store_db) as conn:
        store,intent,policy=configured(conn,2);store.reserve(intent,policy,now=NOW);filled(store,intent)
        store.record_position(position(intent,intent.approved_quantity))
        _,second,policy=configured(conn,2);policy=replace(policy,allow_pyramiding=True)
        body=intent_body(second);body["risk_policy_hash"]=str(canonical_sha256(policy));second=make_intent(**body)
        with pytest.raises(ReservationRejected,match="UNSUPPORTED_EXECUTION_CAPABILITY"):
            store.reserve(second,policy,now=NOW)


def test_latest_portfolio_checkpoint_disambiguates_shared_account_positions(store_db):
    import json
    from quant_execution.persistence import active_execution_state
    from quant_phase9.canonical import canonical_json
    with psycopg.connect(store_db) as conn:
        store,intent,_=seeded_intent(conn);flat=position(intent,D(0));store.record_position(flat)
        second=replace(flat,equity=flat.equity-D('1'));store.record_position(second)
        raw=json.loads(canonical_json(second))
        store.append_local_event(account_id=intent.account_id,event_key='a'*64,kind='CHECKPOINT',payload={'position':raw,'portfolio':[raw]})
        state=active_execution_state(conn,account_id=intent.account_id,now=NOW)
        assert state.reconciliation_status=='RECONCILED'


def test_recovery_selects_checkpoint_when_open_and_flat_share_timestamp(store_db):
    import json
    from quant_realtime_paper.assembly import _recovery_positions
    from quant_phase9.canonical import canonical_json
    with psycopg.connect(store_db) as conn:
        store,intent,_=seeded_intent(conn)
        open_position=position(intent,intent.approved_quantity);flat=position(intent,D(0))
        store.record_position(open_position);store.record_position(flat)
        raw=json.loads(canonical_json(open_position))
        store.append_local_event(account_id=intent.account_id,event_key='b'*64,kind='CHECKPOINT',payload={'position':raw,'portfolio':[raw]})
        assert _recovery_positions(conn,intent.account_id)==(raw,)


def test_partial_fill_and_its_pending_remainder_use_one_position_slot(paper_db,tmp_path):
    from dataclasses import replace
    from quant_nautilus.acceptance import fixture_case,install_fixture_decision
    first=fixture_case(tmp_path/'partial-a',symbol='ETHUSDT',mode='PAPER',now=NOW)
    second=fixture_case(tmp_path/'partial-b',symbol='BTCUSDT',pattern='BREAKOUT_CONFIRMATION',mode='PAPER',now=NOW)
    conn=paper_db[0]
    store=ExecutionStore(conn);store.record_account(first.account)
    intents=[];policies=[]
    for case in (first,second):
        policy=replace(case.risk_policy,risk_config_digest='d'*64,max_open_positions=2,max_open_intents=2,max_exposure=D(3000),max_reserved_risk=D(100))
        body=intent_body(case.intent);body['account_id']=first.intent.account_id;body['risk_policy_hash']=str(canonical_sha256(policy))
        intent=make_intent(**body);case=replace(case,intent=intent,risk_policy=policy)
        install_fixture_decision(conn,case);intents.append(intent);policies.append(policy)
    one,two=intents
    store.reserve(one,policies[0],now=NOW)
    half=one.approved_quantity/2
    store.record_result(ExecutionResultV1(one.intent_id,'partial-'+one.intent_id.hex,one.client_order_id,(),
        'PARTIALLY_FILLED',one.approved_quantity,half,half,one.reference_price,D(0),'USDT',None,'ACTIVE',NOW,NOW,'fixture','fixture'))
    store.record_position(position(one,half))
    assert store.reserve(two,policies[1],now=NOW)

def test_pending_same_symbol_entry_cannot_bypass_add_position_rule(store_db):
    with psycopg.connect(store_db) as conn:
        store,first,policy=configured(conn,2);store.reserve(first,policy,now=NOW)
        _,second,policy=configured(conn,2)
        with pytest.raises(ReservationRejected,match='ADD_POSITION_DISABLED'):
            store.reserve(second,policy,now=NOW)
