"""Original native isolated-DB tests; never Goal trading evidence."""
from datetime import datetime,timezone,timedelta
from dataclasses import replace
from decimal import Decimal as D
import pytest
from psycopg.types.json import Jsonb
from quant_execution.persistence import FencedOwner,ReservationRejected
from quant_nautilus.owned_runtime import OwnedLocalPaperRuntime
from quant_nautilus.realtime_paper_adapter import NautilusLocalPaperExecutionAdapter
from quant_phase9.canonical import canonical_sha256
from tests.quant_nautilus.test_realtime_paper_adapter import adapter_db,_seed_adapter_state,_real_quote

def prepared(adapter_db,tmp_path):
    conn,_,_=adapter_db
    case,store,_=_seed_adapter_state(conn,tmp_path,now=datetime.now(timezone.utc))
    assert store.reserve(case.intent,case.risk_policy,now=case.intent.created_at)
    runtime=OwnedLocalPaperRuntime(conn,case.intent.account_id)
    def adapter():
        t=datetime.now(timezone.utc)
        quote=replace(_real_quote(case.intent),exchange_timestamp=t,fetched_at=t,processed_at=t)
        return NautilusLocalPaperExecutionAdapter(conn,case.intent,instrument=case.instrument,
            quote=quote,data_source="REAL_PUBLIC_DATA",quote_max_age_seconds=5,native_runtime=runtime)
    adapter().submit(case.intent)
    return conn,case,store,runtime,adapter

def test_repeated_native_management_and_no_order_preflight_reuse_one_original_core(adapter_db,tmp_path,monkeypatch):
    conn,case,store,runtime,adapter=prepared(adapter_db,tmp_path)
    try:
        first_core=runtime._runner.session
        original_ids=tuple(r[0] for r in conn.execute('SELECT execution_id FROM execution_results ORDER BY execution_id'))
        def forbidden(*a,**k):raise AssertionError("unchanged journal must not rebuild native core")
        monkeypatch.setattr('quant_nautilus.owned_runtime.LocalPaper',forbidden)
        for _ in range(8):
            a=adapter();pos=a.manage_position(now=datetime.now(timezone.utc))
            assert pos.reconciliation_status=="RECONCILED"
            assert runtime._runner.session is first_core
            before=conn.execute('SELECT count(*) FROM execution_local_events').fetchone()[0]
            a.preflight_existing_account()
            assert conn.execute('SELECT count(*) FROM execution_local_events').fetchone()[0]==before
            assert conn.execute('SELECT lease_expires_at FROM execution_accounts').fetchone()[0] is None
        assert tuple(r[0] for r in conn.execute('SELECT execution_id FROM execution_results ORDER BY execution_id'))==original_ids
        assert conn.execute('SELECT count(*) FROM execution_intents').fetchone()[0]==1
    finally:runtime.close()

def test_changed_durable_journal_discards_cache_and_fully_replays(adapter_db,tmp_path,monkeypatch):
    conn,case,store,runtime,adapter=prepared(adapter_db,tmp_path)
    old_core=runtime._runner.session
    state=runtime._runner.state()
    store.append_local_event(account_id=case.intent.account_id,event_key='external-native-checkpoint',
        kind='CHECKPOINT',payload=state,intent_id=case.intent.intent_id)
    import quant_nautilus.owned_runtime as module
    original=module.LocalPaper;loads=[]
    def counted(*a,**k):loads.append(1);return original(*a,**k)
    monkeypatch.setattr(module,'LocalPaper',counted)
    try:
        adapter().preflight_existing_account()
        assert len(loads)==1 and runtime._runner.session is not old_core
        assert runtime._runner.state()==state
        adapter().preflight_existing_account()
        assert len(loads)==1
    finally:runtime.close()

def test_active_other_owner_blocks_cached_native_side_effects(adapter_db,tmp_path):
    conn,case,store,runtime,adapter=prepared(adapter_db,tmp_path)
    before=conn.execute('SELECT count(*) FROM execution_local_events').fetchone()[0]
    epoch=store.acquire_owner(case.intent.account_id,'other-live-owner',now=datetime.now(timezone.utc),lease_seconds=30)
    try:
        with pytest.raises(FencedOwner):adapter().manage_position(now=datetime.now(timezone.utc))
        row=conn.execute('SELECT owner_id,owner_epoch,lease_expires_at FROM execution_accounts').fetchone()
        assert row[0:2]==('other-live-owner',epoch) and row[2] is not None
        assert conn.execute('SELECT count(*) FROM execution_local_events').fetchone()[0]==before
        assert runtime._runner is None
    finally:store.release_owner(case.intent.account_id,'other-live-owner',epoch);runtime.close()

def test_large_history_replays_once_then_manages_without_replay(adapter_db,tmp_path,monkeypatch):
    conn,case,store,runtime,adapter=prepared(adapter_db,tmp_path)
    state=runtime._runner.state();runtime.close()
    conn.execute("""INSERT INTO execution_local_events(account_id,event_key,event_kind,payload,content_digest,intent_id)
        SELECT %s,'owned-runtime-fixture-'||n::text,'CHECKPOINT',%s,%s,%s FROM generate_series(1,4097) n""",
        (case.intent.account_id,Jsonb(state),str(canonical_sha256(state)),case.intent.intent_id))
    runtime=OwnedLocalPaperRuntime(conn,case.intent.account_id)
    try:
        with runtime.operation(case.intent.intent_id,instrument=case.instrument,verification=True,recover_pending_on_start=False):pass
        original_core=runtime._runner.session
        def forbidden(*a,**k):raise AssertionError("4097 cached checkpoints must not be re-replayed")
        monkeypatch.setattr('quant_nautilus.owned_runtime.LocalPaper',forbidden)
        for _ in range(6):
            t=datetime.now(timezone.utc);quote=replace(_real_quote(case.intent),exchange_timestamp=t,fetched_at=t,processed_at=t)
            a=NautilusLocalPaperExecutionAdapter(conn,case.intent,instrument=case.instrument,
                quote=quote,data_source="REAL_PUBLIC_DATA",quote_max_age_seconds=5,native_runtime=runtime)
            a.manage_position(now=t);a.preflight_existing_account()
            assert runtime._runner.session is original_core and runtime._runner.event_count>4096
        assert conn.execute('SELECT count(*) FROM execution_intents').fetchone()[0]==1
        assert runtime._runner.session.snapshot().quantity==case.intent.approved_quantity
    finally:runtime.close()

def test_corrupt_added_event_invalidates_cache_and_cannot_skip_digest_validation(adapter_db,tmp_path):
    conn,case,store,runtime,adapter=prepared(adapter_db,tmp_path)
    state=runtime._runner.state()
    conn.execute("""INSERT INTO execution_local_events(account_id,event_key,event_kind,payload,content_digest,intent_id)
        VALUES(%s,'corrupt-cache-fixture','CHECKPOINT',%s,%s,%s)""",
        (case.intent.account_id,Jsonb(state),'f'*64,case.intent.intent_id))
    try:
        with pytest.raises(ReservationRejected,match='DIGEST_MISMATCH'):adapter().preflight_existing_account()
        assert runtime._runner is None
    finally:runtime.close()

def test_restore_delay_revalidates_quote_before_any_submission_state_change(adapter_db,tmp_path,monkeypatch):
    conn,_,_=adapter_db;t=datetime.now(timezone.utc)
    case,store,_=_seed_adapter_state(conn,tmp_path,now=t)
    assert store.reserve(case.intent,case.risk_policy,now=t)
    runtime=OwnedLocalPaperRuntime(conn,case.intent.account_id)
    calls=[]
    def advancing():
        calls.append(1);return t if len(calls)==1 else t+timedelta(seconds=6)
    monkeypatch.setattr('quant_nautilus.realtime_paper_adapter.wall_now',advancing)
    a=NautilusLocalPaperExecutionAdapter(conn,case.intent,instrument=case.instrument,
        quote=_real_quote(case.intent),data_source="REAL_PUBLIC_DATA",quote_max_age_seconds=5,native_runtime=runtime)
    try:
        with pytest.raises(ValueError,match='STALE_MARKET_DATA'):a.submit(case.intent)
        assert store.submission_state(case.intent.intent_id)=='RESERVED'
        assert conn.execute("SELECT count(*) FROM execution_local_events WHERE event_kind='SUBMIT'").fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM execution_results').fetchone()[0]==0
        assert runtime._runner is None
    finally:runtime.close()

def test_operation_failure_discards_cache_and_preserves_durable_native_checkpoint(adapter_db,tmp_path):
    conn,case,store,runtime,adapter=prepared(adapter_db,tmp_path)
    old_core=runtime._runner.session
    with pytest.raises(RuntimeError,match='scope fault'):
        with runtime.operation(case.intent.intent_id,instrument=case.instrument) as runner:
            runner.checkpoint();raise RuntimeError('scope fault')
    assert runtime._runner is None
    try:
        adapter().preflight_existing_account()
        assert runtime._runner.session is not old_core
        assert runtime._runner.session.snapshot().quantity==case.intent.approved_quantity
    finally:runtime.close()

def test_runtime_rejects_reentrant_or_cross_account_operation(adapter_db,tmp_path):
    conn,case,store,runtime,adapter=prepared(adapter_db,tmp_path)
    try:
        with runtime.operation(case.intent.intent_id,instrument=case.instrument) as runner:
            with pytest.raises(ReservationRejected,match='OWNER_CONTEXT_INVALID'):
                with runtime.operation(case.intent.intent_id,instrument=case.instrument):pass
            assert runner.session.snapshot().reconciliation_status=='RECONCILED'
        other=OwnedLocalPaperRuntime(conn,'different-account')
        with pytest.raises(ReservationRejected,match='ACCOUNT_BINDING_MISMATCH'):
            with other.operation(case.intent.intent_id,instrument=case.instrument):pass
        other.close()
    finally:runtime.close()

def two_position_fixture(case):
    # Explicit disposable fixture capacity includes fill-price spread on the
    # first 1000-USDT reservation. Production risk policy is never changed.
    from quant_execution.contracts import intent_body,make_intent
    policy=replace(case.risk_policy,max_exposure=D(3000))
    body=intent_body(case.intent)
    body['risk_policy_hash']=str(canonical_sha256(policy))
    return replace(case,risk_policy=policy,intent=make_intent(**body))

def test_new_intent_uses_same_native_portfolio_and_durable_config(adapter_db,tmp_path):
    conn,first,store,runtime,adapter=prepared(adapter_db,tmp_path/'one')
    from quant_nautilus.acceptance import fixture_case,install_fixture_decision
    second=two_position_fixture(fixture_case(tmp_path/'two',symbol='ETHUSDT',pattern='BREAKOUT_CONFIRMATION',mode='PAPER',now=datetime.now(timezone.utc)))
    assert second.intent.account_id==first.intent.account_id
    install_fixture_decision(conn,second)
    assert store.reserve(second.intent,second.risk_policy,now=second.intent.created_at)
    core=runtime._runner.session
    try:
        a=NautilusLocalPaperExecutionAdapter(conn,second.intent,instrument=second.instrument,
            quote=_real_quote(second.intent),data_source='REAL_PUBLIC_DATA',quote_max_age_seconds=5,native_runtime=runtime)
        a.submit(second.intent)
        assert runtime._runner.session is core and len(core.cache.positions_open())==2
        assert store.submission_state(first.intent.intent_id)=='FILLED'
        assert store.submission_state(second.intent.intent_id)=='FILLED'
        assert conn.execute("SELECT count(*) FROM execution_local_events WHERE event_kind='CONFIG'").fetchone()[0]==2
        adapter().preflight_existing_account();a.preflight_existing_account()
        assert runtime._runner.session is core
        runtime.close()
        runtime=OwnedLocalPaperRuntime(conn,first.intent.account_id)
        with runtime.operation(first.intent.intent_id,instrument=first.instrument,verification=True,recover_pending_on_start=False) as r:
            assert len(r.session.cache.positions_open())==2
    finally:runtime.close()

def test_cached_native_state_corruption_is_not_hidden_by_unchanged_journal(adapter_db,tmp_path):
    conn,case,store,runtime,adapter=prepared(adapter_db,tmp_path)
    runtime._runner.session.adapter.entry_order_count+=1
    try:
        with pytest.raises(ReservationRejected,match='RECONCILIATION_MISMATCH'):adapter().preflight_existing_account()
        assert runtime._runner is None
    finally:runtime.close()

def test_existing_live_owner_cannot_be_adopted_to_prepare_entry(adapter_db,tmp_path):
    conn,_,_=adapter_db;t=datetime.now(timezone.utc)
    case,store,_=_seed_adapter_state(conn,tmp_path,now=t)
    assert store.reserve(case.intent,case.risk_policy,now=t)
    owner='local-paper-'+case.intent.intent_id.hex[:40]
    epoch=store.acquire_owner(case.intent.account_id,owner,now=t,lease_seconds=30)
    a=NautilusLocalPaperExecutionAdapter(conn,case.intent,instrument=case.instrument,
        quote=_real_quote(case.intent),data_source='REAL_PUBLIC_DATA',quote_max_age_seconds=5)
    try:
        with pytest.raises(FencedOwner):a.submit(case.intent)
        assert conn.execute('SELECT count(*) FROM execution_local_events').fetchone()[0]==0
        assert store.submission_state(case.intent.intent_id)=='RESERVED'
    finally:store.release_owner(case.intent.account_id,owner,epoch)

def test_cached_preflight_does_not_scan_unchanged_full_history(adapter_db,tmp_path,monkeypatch):
    conn,case,store,runtime,adapter=prepared(adapter_db,tmp_path)
    def forbidden(*a,**k):raise AssertionError('unchanged verified native checkpoint needs no history rescan')
    monkeypatch.setattr('quant_nautilus.realtime_paper_adapter.checkpoint_positions',forbidden)
    monkeypatch.setattr('quant_execution.persistence.ExecutionStore.iter_local_events',forbidden)
    try:
        adapter().preflight_existing_account()
        assert runtime._runner.session.snapshot().quantity==case.intent.approved_quantity
    finally:runtime.close()

def test_cached_original_stop_closes_position_and_restart_preserves_fees_and_results(adapter_db,tmp_path):
    conn,case,store,runtime,adapter=prepared(adapter_db,tmp_path)
    core=runtime._runner.session
    t=datetime.now(timezone.utc);bid=case.intent.stop_price-D(10)
    q=replace(_real_quote(case.intent),exchange_timestamp=t,fetched_at=t,processed_at=t,
        bid_price=bid,ask_price=bid+D(2),last_price=bid+D(1))
    a=NautilusLocalPaperExecutionAdapter(conn,case.intent,instrument=case.instrument,
        quote=q,data_source='REAL_PUBLIC_DATA',quote_max_age_seconds=5,native_runtime=runtime)
    try:
        position=a.manage_position(now=t)
        assert position.quantity==0 and position.fees>0
        assert position.realized_trade_pnl<0 and runtime._runner.session is core
        assert core.adapter.entry_order_count==1
        before=runtime._runner.state()
        results=tuple(r[0] for r in conn.execute('SELECT execution_id FROM execution_results ORDER BY execution_id'))
        runtime.close();runtime=OwnedLocalPaperRuntime(conn,case.intent.account_id)
        with runtime.operation(case.intent.intent_id,instrument=case.instrument,verification=True,recover_pending_on_start=False) as r:
            assert r.state()==before and r.session.snapshot()==position
        assert tuple(r[0] for r in conn.execute('SELECT execution_id FROM execution_results ORDER BY execution_id'))==results
    finally:runtime.close()

def test_pending_unknown_submission_cannot_use_cached_preflight_or_new_entry_scope(adapter_db,tmp_path):
    conn,case,store,runtime,adapter=prepared(adapter_db,tmp_path)
    from quant_nautilus.acceptance import fixture_case,install_fixture_decision
    second=two_position_fixture(fixture_case(tmp_path/'pending',symbol='ETHUSDT',pattern='BREAKOUT_CONFIRMATION',mode='PAPER',now=datetime.now(timezone.utc)))
    install_fixture_decision(conn,second)
    assert store.reserve(second.intent,second.risk_policy,now=second.intent.created_at)
    epoch=store.acquire_owner(case.intent.account_id,'pending-fixture-owner',now=datetime.now(timezone.utc),lease_seconds=30)
    assert store.begin_submission(second.intent.intent_id,'pending-fixture-owner',epoch,now=datetime.now(timezone.utc))
    store.release_owner(case.intent.account_id,'pending-fixture-owner',epoch)
    before=conn.execute('SELECT count(*) FROM execution_local_events').fetchone()[0]
    try:
        with pytest.raises(ReservationRejected,match='SUBMISSION_UNRESOLVED'):
            with runtime.operation(second.intent.intent_id,instrument=second.instrument,recover_pending_on_start=False):pass
        assert runtime._runner is None
        assert conn.execute('SELECT count(*) FROM execution_local_events').fetchone()[0]==before
        assert store.submission_state(second.intent.intent_id)=='SUBMITTING'
    finally:runtime.close()

def test_cold_manager_restores_before_capturing_quote_and_uses_actual_command_clock(adapter_db,tmp_path,monkeypatch):
    from types import SimpleNamespace
    import json
    from quant_realtime_paper import assembly
    conn,case,store,runtime,adapter=prepared(adapter_db,tmp_path/'initial')
    runtime.close();runtime=OwnedLocalPaperRuntime(conn,case.intent.account_id)
    profile=tmp_path/'profile.json'
    profile.write_text(json.dumps({'account_id':case.intent.account_id,'venue':case.intent.venue}))
    config=SimpleNamespace(dsn='FIXTURE_NO_SECOND_CONNECTION',project_root=tmp_path,execution_profile_path=profile)
    events=[];started=datetime.now(timezone.utc)
    original=runtime.prepare
    def prepare(*a,**kw):
        assert runtime._runner is None
        events.append('restore');original(*a,**kw)
    def market(*a,**kw):
        assert runtime._runner is not None and events==['restore']
        assert kw['symbols']==('BTCUSDT',)
        events.append('quote')
        t=datetime.now(timezone.utc)
        assert t>started
        q=replace(_real_quote(case.intent),exchange_timestamp=t,fetched_at=t,processed_at=t)
        return SimpleNamespace(tickers=(q,))
    monkeypatch.setattr(runtime,'prepare',prepare)
    monkeypatch.setattr(assembly,'_latest_market_batch',market)
    before=conn.execute('SELECT count(*) FROM execution_results').fetchone()[0]
    try:
        result=assembly.manage_persisted_positions(config,{'TRADING_MODE':'paper','PAPER_ONLY':'true','LIVE_ALLOWED':'false'},
            started,execution_connection=conn,native_runtime=runtime)
        assert events==['restore','quote'] and len(result)==1
        assert result[0].quantity==case.intent.approved_quantity
        assert conn.execute('SELECT count(*) FROM execution_results').fetchone()[0]==before
        assert conn.execute('SELECT count(*) FROM execution_intents').fetchone()[0]==1
    finally:runtime.close()
