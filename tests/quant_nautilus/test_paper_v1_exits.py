from dataclasses import replace
from datetime import timedelta, datetime, timezone
from decimal import Decimal as D
import pytest
from quant_nautilus.acceptance import fixture_case
from quant_nautilus.sandbox import SandboxSession
from quant_execution.contracts import intent_body,make_intent

def case_with_exits(tmp_path,side='LONG',now=None):
    case=fixture_case(tmp_path,mode='PAPER',side=side,**({'now':now} if now else {}))
    body=intent_body(case.intent)
    body.update(strategy_profile='QUANT_PAPER_V1_CONSERVATIVE',target_price=case.intent.reference_price+(D('100') if side=='LONG' else D('-100')),
        max_hold_seconds=10800,setup_id='a'*64,setup_trigger_at=case.intent.created_at-timedelta(minutes=15))
    return case.instrument,make_intent(**body)

@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('exit_kind',['target','max_hold','emergency','stop'])
def test_native_full_exits_preserve_protection_and_survive_expired_intent(tmp_path,side,exit_kind):
    instrument,intent=case_with_exits(tmp_path,side)
    with SandboxSession(instrument,intent) as session:
        session.quote(intent.created_at,intent.reference_price,D('100'))
        session.submit()
        assert session.snapshot().quantity>0 and session.snapshot().protection_status=='ACTIVE'
        at=intent.created_at+timedelta(seconds=10800 if exit_kind=='max_hold' else 90)
        mid=intent.reference_price
        if exit_kind=='target': mid=intent.target_price+(D('2') if side=='LONG' else D('-2'))
        if exit_kind=='stop': mid=intent.stop_price+(D('-2') if side=='LONG' else D('2'))
        session.quote(at,mid,D('100'))
        if exit_kind=='emergency': session.adapter.emergency_exit()
        assert session.snapshot().side=='FLAT'
        exits=[o for o in session.cache.orders() if str(o.client_order_id).startswith(intent.client_order_id+'-X')]
        if exit_kind!='stop': assert exits and all(o.is_reduce_only for o in exits)
        count=len(session.cache.orders())
        session.quote(at+timedelta(seconds=1),mid,D('100'))
        assert len(session.cache.orders())==count and session.adapter.entry_order_count==1

def test_partial_target_exit_keeps_stop_and_retries_remaining_native_quantity(tmp_path):
    instrument,intent=case_with_exits(tmp_path)
    with SandboxSession(instrument,intent) as session:
        session.quote(intent.created_at,intent.reference_price,D('100'));session.submit()
        prior=session.snapshot().quantity
        at=intent.created_at+timedelta(seconds=90)
        session.quote(at,intent.target_price+D('2'),D('0.001'))
        residual=session.snapshot()
        assert D('0')<residual.quantity<prior and residual.protection_status=='ACTIVE'
        session.quote(at+timedelta(seconds=1),intent.target_price+D('2'),D('100'))
        assert session.snapshot().quantity==0
        assert session.adapter.entry_order_count==1

@pytest.mark.parametrize('entry_size',['100','0.001'])
def test_dynamic_exit_replay_has_no_second_order(paper_db,tmp_path,entry_size):
    from quant_nautilus.acceptance import install_fixture_decision
    from quant_nautilus.paper import LocalPaper
    from quant_execution.persistence import ExecutionStore
    now=datetime.now(timezone.utc)
    case=fixture_case(tmp_path/'fixture',mode='PAPER',now=now)
    instrument,intent=case_with_exits(tmp_path/'exit',now=now)
    conn,_,_=paper_db
    install_fixture_decision(conn,case)
    store=ExecutionStore(conn);store.record_account(case.account)
    store.reserve(intent,case.risk_policy,now=intent.created_at)
    runner=LocalPaper(conn,intent.intent_id)
    try:
        runner.command('QUOTE',dict(at=intent.created_at.isoformat(),mid=str(intent.reference_price),size=entry_size))
        runner.command('SUBMIT',dict(intent_id=str(intent.intent_id),client_order_id=intent.client_order_id))
        runner.command('QUOTE',dict(at=(intent.created_at+timedelta(seconds=90)).isoformat(),mid=str(intent.target_price+D('2')),size='100'))
        state=runner.state();assert state['position']['side']=='FLAT'
    finally: runner.close()
    runner=LocalPaper(conn,intent.intent_id)
    try:
        assert runner.state()==state
        assert runner.session.adapter.entry_order_count==1
        assert len([k for k in state['order_states'] if '-X' in k])==1
    finally: runner.close()

from tests.quant_nautilus.test_paper_restart import paper_db


@pytest.mark.parametrize('side',['LONG','SHORT'])
@pytest.mark.parametrize('exit_kind',['target','stop','emergency'])
def test_partial_ioc_entry_can_exit_without_falsifying_entry_fill(tmp_path,side,exit_kind):
    instrument,intent=case_with_exits(tmp_path,side)
    with SandboxSession(instrument,intent) as session:
        session.quote(intent.created_at,intent.reference_price,D('0.001'));session.submit()
        quantity=session.snapshot().quantity
        assert D('0')<quantity<intent.approved_quantity
        mid=intent.reference_price
        if exit_kind=='target': mid=intent.target_price+(D('2') if side=='LONG' else D('-2'))
        if exit_kind=='stop': mid=intent.stop_price+(D('-2') if side=='LONG' else D('2'))
        session.quote(intent.created_at+timedelta(seconds=90),mid,D('100'))
        if exit_kind=='emergency': session.adapter.emergency_exit()
        assert session.snapshot().side=='FLAT'
        assert session.adapter.results[-1].filled_quantity==quantity
        assert session.adapter.results[-1].status=='CANCELLED'
        assert session.adapter.results[-1].protection_status=='NOT_REQUIRED'


def test_current_event_rejection_is_fenced_durable_and_releases_reservation(paper_db,tmp_path):
    from quant_nautilus.acceptance import install_fixture_decision
    from quant_execution.persistence import ExecutionStore,FencedOwner
    from quant_execution.contracts import ExecutionResultV1
    conn,_,_=paper_db
    now=datetime.now(timezone.utc)
    case=fixture_case(tmp_path,mode='PAPER',now=now)
    install_fixture_decision(conn,case)
    store=ExecutionStore(conn);store.record_account(case.account)
    store.reserve(case.intent,case.risk_policy,now=now)
    epoch=store.acquire_owner(case.intent.account_id,'event-recheck',now=now,lease_seconds=30)
    assert store.begin_submission(case.intent.intent_id,'event-recheck',epoch,now=now)
    result=ExecutionResultV1(case.intent.intent_id,'known-no-submit',case.intent.client_order_id,(),'REJECTED',case.intent.approved_quantity,D('0'),case.intent.approved_quantity,None,D('0'),'USDT',None,'NOT_REQUIRED',now,now,'SECURITY_EVENT_VETO','paper-v1-event-recheck')
    with pytest.raises(FencedOwner): store.reject_before_submit(result,'wrong-owner',epoch,now=now)
    assert store.submission_state(case.intent.intent_id)=='SUBMITTING'
    store.reject_before_submit(result,'event-recheck',epoch,now=now)
    assert store.submission_state(case.intent.intent_id)=='REJECTED'
    assert conn.execute('SELECT state FROM execution_reservations WHERE intent_id=%s',(case.intent.intent_id,)).fetchone()[0]=='RELEASED'
    assert store.recover_pending(case.intent.account_id)==()

def test_missing_approval_recovery_uses_durable_plan_and_real_quote_only(paper_db,tmp_path,monkeypatch):
    import json
    from types import SimpleNamespace
    from contextlib import contextmanager
    from quant_realtime_paper import assembly
    from quant_realtime_paper.config import RuntimeConfig
    from quant_nautilus.acceptance import install_fixture_decision
    from quant_nautilus.paper import LocalPaper
    from quant_execution.persistence import ExecutionStore
    from tests.quant_nautilus.test_realtime_paper_adapter import _real_quote
    now=datetime.now(timezone.utc)
    case=fixture_case(tmp_path/'case',mode='PAPER',now=now)
    _,intent=case_with_exits(tmp_path/'intent',now=now)
    conn,dsn,_=paper_db
    install_fixture_decision(conn,case)
    store=ExecutionStore(conn);store.record_account(case.account);store.reserve(intent,case.risk_policy,now=now)
    runner=LocalPaper(conn,intent.intent_id,acceptance_kind='REAL_PUBLIC_DATA_LOCAL_PAPER_V1')
    try:
        runner.command('QUOTE',dict(at=now.isoformat(),mid=str(intent.reference_price),size='100'))
        runner.command('SUBMIT',dict(intent_id=str(intent.intent_id),client_order_id=intent.client_order_id))
    finally: runner.close()
    profile=tmp_path/'profile.json';profile.write_text(json.dumps(dict(account_id=intent.account_id,venue=intent.venue)))
    config=RuntimeConfig(dsn,tmp_path/'state.sqlite3',2.0,('BTCUSDT','ETHUSDT'),tmp_path)
    env=dict(TRADING_MODE='paper',PAPER_ONLY='true',LIVE_ALLOWED='false',QUANT_BUILD_REVISION='b'*40,QUANT_REALTIME_PAPER_EXECUTION_PROFILE_PATH=str(profile))
    at=now+timedelta(seconds=90)
    quote=replace(_real_quote(intent),bid_price=intent.target_price+D('1'),ask_price=intent.target_price+D('2'),exchange_timestamp=at,fetched_at=at,processed_at=at)
    monkeypatch.setattr(assembly,'_latest_market_batch',lambda cfg,**kwargs:SimpleNamespace(tickers=(quote,)))
    @contextmanager
    def connect(*args,**kwargs): yield conn
    monkeypatch.setattr(__import__('psycopg'),'connect',connect)
    monitor=assembly.build_default_runtime(config,env)
    assert monitor.startup_blocker=='APPROVAL_MISSING' and monitor.execution_pipeline is None
    managed=monitor.position_manager(at)
    assert managed[0].side=='FLAT'
    assert monitor.position_manager(at+timedelta(seconds=1))==()
    assert len(conn.execute('SELECT intent_id FROM execution_intents').fetchall())==1
