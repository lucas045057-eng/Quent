from datetime import datetime,timezone,timedelta
from decimal import Decimal as D
import pytest
from quant_nautilus.paper import LocalPaper
from quant_execution.persistence import ExecutionStore
from quant_nautilus.acceptance import fixture_case,install_fixture_decision
from tests.quant_nautilus.test_paper_restart import paper_db

def test_multiple_terminal_histories_do_not_block_local_restore(paper_db,tmp_path):
    conn,_,_=paper_db
    first=fixture_case(tmp_path/"a",mode="PAPER",now=datetime.now(timezone.utc))
    install_fixture_decision(conn,first);store=ExecutionStore(conn);store.record_account(first.account)
    store.reserve(first.intent,first.risk_policy,now=first.intent.created_at)
    runner=LocalPaper(conn,first.intent.intent_id)
    try:
        runner.command("QUOTE",dict(at=first.intent.created_at.isoformat(),mid=str(first.intent.reference_price),size="100"))
        runner.command("SUBMIT",dict(intent_id=str(first.intent.intent_id),client_order_id=first.intent.client_order_id))
        runner.command("QUOTE",dict(at=(first.intent.created_at+timedelta(seconds=1)).isoformat(),mid=str(first.intent.stop_price-D(10)),size="100"))
    finally:runner.close()
    second=fixture_case(tmp_path/"b",symbol="ETHUSDT",pattern="BREAKOUT_CONFIRMATION",mode="PAPER",now=datetime.now(timezone.utc)+timedelta(seconds=2))
    from dataclasses import replace
    from quant_execution.contracts import intent_body,make_intent
    from quant_phase9.canonical import canonical_sha256
    body=intent_body(second.intent);body["account_id"]=first.intent.account_id
    second=replace(second,intent=make_intent(**body))
    install_fixture_decision(conn,second)
    store.record_account(replace(first.account,as_of=second.intent.created_at))
    store.reserve(second.intent,second.risk_policy,now=second.intent.created_at)
    runner=LocalPaper(conn,second.intent.intent_id)
    try:
        assert runner.restored and len(runner.session.adapters)==2
        runner.command("QUOTE",dict(at=second.intent.created_at.isoformat(),mid=str(second.intent.reference_price),size="100"))
        runner.command("SUBMIT",dict(intent_id=str(second.intent.intent_id),client_order_id=second.intent.client_order_id))
        assert runner.session.snapshot().quantity==second.intent.approved_quantity
    finally:runner.close()

@pytest.mark.parametrize("checkpoint_copies",[0,4097])
def test_v2_public_instrument_and_risk_binding_survive_restart(paper_db,tmp_path,monkeypatch,checkpoint_copies):
    conn,_,_=paper_db
    from tests.strategies.test_persistence import seeded
    from strategies.persistence import persist_strategy_decision
    from quant_execution.contracts import DecisionEnvelopeV1
    from tests.quant_execution.test_v2_risk import inputs
    from quant_execution.risk import approve_intent
    from quant_execution.risk_config import resolve_risk_policy
    from quant_nautilus.instruments import build_public_instrument
    from strategies.market_view import InstrumentMetadata
    from dataclasses import replace
    conn.autocommit=False
    result,snapshot=seeded(conn)
    decision=persist_strategy_decision(conn,result=result,snapshot=snapshot,now=result.evaluated_at);conn.commit()
    data=inputs()
    from quant_execution.paper_v1 import PaperCostsV1
    costs=PaperCostsV1(D('.0006'),D('.0006'),D(0))
    native=build_public_instrument(InstrumentMetadata(symbol="SOLUSDT",tick=".01",lot=".01",
        min_quantity=".01",max_quantity="1000",min_notional="5",settlement_currency="USDT",
        source_ref="bitget:fixture-public-spec",source_digest="a"*64),costs=costs)
    data["instrument"]=replace(data["instrument"],venue="BITGET_PAPER",instrument_id=str(native.id))
    data["account"]=replace(data["account"],venue="BITGET_PAPER")
    from quant_execution.trade_plan import build_trade_plan
    from quant_execution.risk_config import RiskConfigV2
    from quant_execution.paper_v1 import PaperCostsV1
    config=RiskConfigV2(max_slippage_bps="0")
    plan=build_trade_plan(result.thesis,data["instrument"],data["quote"],costs=costs,config=config,now=data["now"])
    data["trade_plan"]=plan;data["stop_price"]=plan.stop_price
    data["envelope"]=DecisionEnvelopeV1(decision,"ACTIVE",str(snapshot.snapshot_digest),str(decision.input_snapshot_hash),
        result.thesis.digest,result.thesis.snapshot_digest,config.digest)
    intent=approve_intent(**data);store=ExecutionStore(conn);store.record_account(data["account"])
    store.reserve(intent,data["policy"],now=data["now"]);conn.commit()
    monkeypatch.setattr("quant_nautilus.paper.wall_now",lambda:data["now"])
    runner=LocalPaper(conn,intent.intent_id,instrument=native)
    try:
        runner.command("QUOTE",dict(at=intent.created_at.isoformat(),bid="100",ask="100",bid_size="100",ask_size="100"))
        runner.command("SUBMIT",dict(intent_id=str(intent.intent_id),client_order_id=intent.client_order_id))
        before=runner.state()
        assert D(before["position"]["fees"])>0
        assert conn.execute('SELECT count(*) FROM execution_local_events WHERE account_id=%s AND intent_id IS NULL',(intent.account_id,)).fetchone()[0]==0
    finally:runner.close();conn.commit()
    if checkpoint_copies:
        from psycopg.types.json import Jsonb
        from quant_phase9.canonical import canonical_sha256
        conn.execute("""INSERT INTO execution_local_events(account_id,event_key,event_kind,payload,content_digest,intent_id)
            SELECT %s,'v2-restore-fixture-'||n::text,'CHECKPOINT',%s,%s,%s FROM generate_series(1,%s) n""",
            (intent.account_id,Jsonb(before),str(canonical_sha256(before)),intent.intent_id,checkpoint_copies))
        conn.commit()
    runner=LocalPaper(conn,intent.intent_id)
    try:
        assert runner.state()==before
        if checkpoint_copies:assert runner.event_count>4096
        assert runner.session.instrument.size_increment.as_decimal()==D(".01")
        assert runner.store.intent(intent.intent_id).risk_config_digest==config.digest
    finally:runner.close()


def test_two_open_symbols_restore_one_portfolio_checkpoint(paper_db,tmp_path,monkeypatch):
    from dataclasses import replace
    from quant_execution.contracts import intent_body,make_intent
    from quant_execution.persistence import active_execution_state,checkpoint_positions
    from quant_phase9.canonical import canonical_sha256
    conn,_,_=paper_db
    at=datetime.now(timezone.utc)
    clock=[at]
    monkeypatch.setattr('quant_nautilus.paper.wall_now',lambda:clock[0])
    first=fixture_case(tmp_path/'multi-a',mode='PAPER',now=at)
    second=fixture_case(tmp_path/'multi-b',mode='PAPER',symbol='ETHUSDT',pattern='BREAKOUT_CONFIRMATION',now=at+timedelta(seconds=1))
    store=ExecutionStore(conn)
    for index,case in enumerate((first,second)):
        clock[0]=case.intent.created_at
        policy=replace(case.risk_policy,risk_config_digest='c'*64,max_open_positions=2,max_open_intents=2,
            max_exposure=D(3000),max_reserved_risk=D(100),cooldown_seconds=0)
        body=intent_body(case.intent);body['account_id']=first.intent.account_id;body['risk_policy_hash']=str(canonical_sha256(policy))
        intent=make_intent(**body);case=replace(case,intent=intent,risk_policy=policy)
        install_fixture_decision(conn,case)
        if index==0:store.record_account(case.account)
        store.reserve(intent,policy,now=clock[0])
        runner=LocalPaper(conn,intent.intent_id)
        try:
            runner.command('QUOTE',dict(at=clock[0].isoformat(),mid=str(intent.reference_price),size='100'))
            runner.command('SUBMIT',dict(intent_id=str(intent.intent_id),client_order_id=intent.client_order_id))
            if index==1:
                before=runner.state()
                assert len(before['portfolio'])==2
                assert len(runner.session.cache.positions_open())==2
        finally:runner.close()
    clock[0]+=timedelta(seconds=1)
    runner=LocalPaper(conn,intent.intent_id)
    try:
        assert runner.state()==before
        assert len(checkpoint_positions(conn,intent.account_id))==2
        context=active_execution_state(conn,account_id=intent.account_id,now=clock[0])
        assert context.reconciliation_status=='RECONCILED' and len(context.active_positions)==2
        assert context.total_exposure==first.intent.max_notional+second.intent.max_notional
    finally:runner.close()
