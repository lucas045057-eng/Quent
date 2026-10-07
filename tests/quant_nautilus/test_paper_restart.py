import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from quant_phase1.db import apply_migrations
from quant_nautilus.acceptance import fixture_case, install_fixture_decision
from quant_execution.persistence import ExecutionStore
from quant_execution.persistence import ReservationRejected
from quant_nautilus.paper import LocalPaper


@pytest.fixture
def paper_db():
    dsn = os.environ.get('TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('TEST_POSTGRES_DSN is required for isolated database tests')
    schema = 'paper_restart_'+uuid4().hex
    with psycopg.connect(dsn,autocommit=True) as setup:
        setup.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    with psycopg.connect(dsn,options=f'-c search_path={schema}',autocommit=True) as conn:
        apply_migrations(conn)
        yield conn,dsn,schema
    with psycopg.connect(dsn,autocommit=True) as cleanup:
        cleanup.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def launch(dsn,schema,intent_id,ready,*,restore=False,crash=False):
    env = {**os.environ,'QUANT_PAPER_DSN':dsn,'PYTHONPATH':'src','PYTHONDONTWRITEBYTECODE':'1'}
    command = [sys.executable,'-m','quant_nautilus.paper','--schema',schema,'--intent-id',str(intent_id),
        '--ready-file',str(ready),'--fixture-restore' if restore else '--fixture-run']
    if crash:
        command += ['--fixture-crash-after-submit']
    # No inherited output containing private state; diagnostics remain test-local.
    log = ready.with_suffix('.log').open('w')
    process = subprocess.Popen(command,env=env,stdout=log,stderr=log)
    return process,log


def wait_ready(process,path):
    deadline = time.monotonic()+15
    while time.monotonic()<deadline:
        if path.exists():
            return json.loads(path.read_text())
        if process.poll() is not None:
            raise AssertionError(f'worker exit={process.returncode}; {path.with_suffix(".log").read_text()[-3000:]}')
        time.sleep(.1)
    raise AssertionError('worker readiness deadline exceeded')


def stop(process,log):
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=10)
    log.close()


@pytest.mark.parametrize('symbol,side',[
    ('BTCUSDT','LONG'),('BTCUSDT','SHORT'),('ETHUSDT','LONG'),('ETHUSDT','SHORT'),
])
def test_real_process_stop_start_restores_native_order_position_funding_and_audit(paper_db,tmp_path,symbol,side):
    conn,dsn,schema = paper_db
    case = fixture_case(tmp_path/'fixture',symbol=symbol,side=side,mode='PAPER',now=datetime.now(timezone.utc))
    install_fixture_decision(conn,case)
    store = ExecutionStore(conn)
    store.record_account(case.account)
    assert store.reserve(case.intent,case.risk_policy,now=case.intent.created_at)
    first_path,second_path = tmp_path/'first.json',tmp_path/'second.json'
    first,log = launch(dsn,schema,case.intent.intent_id,first_path)
    try:
        initial = wait_ready(first,first_path)
        assert first.poll() is None and initial['state']=='RECONCILED'
        assert initial['native_engine']=='SandboxExecutionClient'
        assert initial['position']['side']==side
        assert D(initial['position']['quantity'])==case.intent.approved_quantity
        assert initial['position']['protection_status']=='ACTIVE'
        assert D(initial['position']['funding_cash']) != 0
        rows = conn.execute('SELECT execution_id,content_digest FROM execution_results ORDER BY execution_id').fetchall()
        assert rows and conn.execute('SELECT count(*) FROM execution_funding_payments').fetchone()[0]==1
    finally:
        stop(first,log)
    second,log = launch(dsn,schema,case.intent.intent_id,second_path,restore=True)
    try:
        restored = wait_ready(second,second_path)
        assert restored['pid'] != initial['pid']
        assert restored['state']=='RECONCILED' and restored['restored'] is True
        assert restored['state_digest']==initial['state_digest']
        assert restored['position']==initial['position']
        assert restored['order_states']==initial['order_states']
        assert restored['entry_order_count']==1
        assert restored['repair_count']==0
        assert conn.execute('SELECT execution_id,content_digest FROM execution_results ORDER BY execution_id').fetchall()==rows
        assert conn.execute('SELECT count(*) FROM execution_funding_payments').fetchone()[0]==1
    finally:
        stop(second,log)


def test_reserved_intent_restart_does_not_submit_before_explicit_command(paper_db, tmp_path):
    conn, _, _ = paper_db
    case = fixture_case(tmp_path / 'fixture', mode='PAPER', now=datetime.now(timezone.utc))
    install_fixture_decision(conn, case)
    store = ExecutionStore(conn)
    store.record_account(case.account)
    assert store.reserve(case.intent, case.risk_policy, now=case.intent.created_at)

    first = LocalPaper(conn, case.intent.intent_id)
    first.close()
    assert store.submission_state(case.intent.intent_id) == 'RESERVED'
    assert conn.execute('SELECT count(*) FROM execution_results').fetchone()[0] == 0

    restored = LocalPaper(conn, case.intent.intent_id)
    try:
        assert restored.restored is True
        assert store.submission_state(case.intent.intent_id) == 'RESERVED'
        assert conn.execute('SELECT count(*) FROM execution_results').fetchone()[0] == 0
    finally:
        restored.close()


def test_restart_recovers_atomic_result_snapshot_write_failure(paper_db, tmp_path):
    conn, _, _ = paper_db
    case = fixture_case(tmp_path / 'fixture', mode='PAPER', now=datetime.now(timezone.utc))
    install_fixture_decision(conn, case)
    store = ExecutionStore(conn)
    store.record_account(case.account)
    assert store.reserve(case.intent, case.risk_policy, now=case.intent.created_at)

    runner = LocalPaper(conn, case.intent.intent_id)
    mid = case.intent.reference_price + (-1 if case.intent.side == 'LONG' else 1)
    runner.command('QUOTE', dict(at=case.intent.created_at.isoformat(), mid=str(mid), size='100'))

    def fail_before_snapshot(*_args, **_kwargs):
        raise RuntimeError('injected failure between result and position writes')

    runner.store.record_position = fail_before_snapshot
    with pytest.raises(RuntimeError, match='between result and position'):
        runner.command(
            'SUBMIT',
            dict(intent_id=str(case.intent.intent_id), client_order_id=case.intent.client_order_id),
        )
    runner.close()
    assert conn.execute(
        'SELECT count(*) FROM execution_results WHERE intent_id=%s', (case.intent.intent_id,),
    ).fetchone()[0] == 0
    assert conn.execute(
        'SELECT count(*) FROM execution_positions WHERE account_id=%s', (case.intent.account_id,),
    ).fetchone()[0] == 0
    assert store.submission_state(case.intent.intent_id) == 'SUBMITTING'

    restored = LocalPaper(conn, case.intent.intent_id)
    try:
        position = restored.session.snapshot()
        assert position.reconciliation_status == 'RECONCILED'
        assert D(position.quantity) == case.intent.approved_quantity
        assert conn.execute(
            'SELECT count(*) FROM execution_results WHERE intent_id=%s', (case.intent.intent_id,),
        ).fetchone()[0] > 0
        assert conn.execute(
            'SELECT count(*) FROM execution_positions WHERE account_id=%s', (case.intent.account_id,),
        ).fetchone()[0] > 0
        assert store.submission_state(case.intent.intent_id) == 'FILLED'
    finally:
        restored.close()


def test_crash_after_native_send_is_restored_from_journal_before_unknown_resolution(paper_db,tmp_path):
    conn,dsn,schema = paper_db
    case = fixture_case(tmp_path/'fixture',mode='PAPER',now=datetime.now(timezone.utc))
    install_fixture_decision(conn,case)
    store=ExecutionStore(conn)
    store.record_account(case.account)
    store.reserve(case.intent,case.risk_policy,now=case.intent.created_at)
    first,log=launch(dsn,schema,case.intent.intent_id,tmp_path/'crash.json',crash=True)
    first.wait(timeout=15)
    log.close()
    assert first.returncode==91
    assert store.submission_state(case.intent.intent_id)=='SUBMITTING'
    # Simulate the real expired owner lease, without a DB mock or bypass.
    time.sleep(3.2)
    second,log=launch(dsn,schema,case.intent.intent_id,tmp_path/'restore.json',restore=True)
    try:
        restored=wait_ready(second,tmp_path/'restore.json')
        assert restored['state']=='RECONCILED' and restored['entry_order_count']==1
        assert restored['repair_count']>0
        assert store.submission_state(case.intent.intent_id)=='FILLED'
        assert D(restored['position']['quantity'])==case.intent.approved_quantity
    finally:
        stop(second,log)


def test_unknown_without_durable_local_command_never_retries(paper_db,tmp_path):
    conn,_,_=paper_db
    case=fixture_case(tmp_path,mode='PAPER',now=datetime.now(timezone.utc))
    install_fixture_decision(conn,case)
    store=ExecutionStore(conn)
    store.record_account(case.account)
    store.reserve(case.intent,case.risk_policy,now=case.intent.created_at)
    now=datetime.now(timezone.utc)
    epoch=store.acquire_owner(case.intent.account_id,'lost-worker',now=now,lease_seconds=3)
    assert store.begin_submission(case.intent.intent_id,'lost-worker',epoch,now=now)
    store.release_owner(case.intent.account_id,'lost-worker',epoch)
    with pytest.raises(ValueError,match='UNKNOWN_WITHOUT_DURABLE_LOCAL_COMMAND'):
        LocalPaper(conn,case.intent.intent_id)
    assert store.submission_state(case.intent.intent_id)=='UNKNOWN'
    assert conn.execute('SELECT count(*) FROM execution_results').fetchone()[0]==0


def test_restart_rejects_extra_funding_payment_absent_from_native_replay(paper_db,tmp_path):
    conn,dsn,schema = paper_db
    case = fixture_case(tmp_path/'fixture',mode='PAPER',now=datetime.now(timezone.utc))
    install_fixture_decision(conn,case)
    store = ExecutionStore(conn)
    store.record_account(case.account)
    store.reserve(case.intent,case.risk_policy,now=case.intent.created_at)
    process,log = launch(dsn,schema,case.intent.intent_id,tmp_path/'first.json')
    try:
        wait_ready(process,tmp_path/'first.json')
    finally:
        stop(process,log)
    conn.execute('''INSERT INTO execution_funding_payments(payment_key,account_id,canonical_symbol,
        boundary,cash,payload,applied) SELECT %s,account_id,canonical_symbol,boundary+interval '8 hours',
        cash,payload,TRUE FROM execution_funding_payments LIMIT 1''',('f'*64,))
    with pytest.raises(ReservationRejected,match='LOCAL_FUNDING_RECONCILIATION_MISMATCH'):
        LocalPaper(conn,case.intent.intent_id)


def test_local_paper_exact_quote_tick_is_journaled_and_replayed(paper_db, tmp_path):
    conn, _, _ = paper_db
    now = datetime.now(timezone.utc)
    case = fixture_case(tmp_path / "fixture", mode="PAPER", now=now)
    install_fixture_decision(conn, case)
    store = ExecutionStore(conn)
    store.record_account(case.account)
    assert store.reserve(case.intent, case.risk_policy, now=case.intent.created_at)
    runner = LocalPaper(conn, case.intent.intent_id)
    quote_data = {
        "at": case.intent.created_at.isoformat(),
        "processed_at": case.intent.created_at.isoformat(),
        "bid": "49998",
        "ask": "50002",
        "bid_size": "1",
        "ask_size": "2",
    }
    try:
        runner.command("QUOTE", quote_data)
        tick = runner.session.adapter._last_tick
        assert tick.bid_price.as_decimal() == D(quote_data["bid"])
        assert tick.ask_price.as_decimal() == D(quote_data["ask"])
        assert tick.bid_size.as_decimal() == D(quote_data["bid_size"])
        assert tick.ask_size.as_decimal() == D(quote_data["ask_size"])
    finally:
        runner.close()


@pytest.mark.parametrize('symbol,side',[
    ('BTCUSDT','LONG'),('BTCUSDT','SHORT'),('ETHUSDT','LONG'),('ETHUSDT','SHORT'),
])
def test_released_owner_restart_after_wall_clock_rollback(paper_db,tmp_path,monkeypatch,symbol,side):
    import quant_execution.persistence as persistence
    from quant_execution.persistence import FencedOwner
    from quant_execution.funding import FundingObservationV1
    from quant_nautilus.paper import payload

    conn,_,_=paper_db
    now=datetime.now(timezone.utc)
    case=fixture_case(tmp_path/'fixture',symbol=symbol,side=side,mode='PAPER',now=now)
    install_fixture_decision(conn,case)
    store=ExecutionStore(conn)
    store.record_account(case.account)
    assert store.reserve(case.intent,case.risk_policy,now=now)
    first=LocalPaper(conn,case.intent.intent_id,owner='original-process')
    second=None
    try:
        # A concurrently live second owner is still illegal.
        with pytest.raises(FencedOwner):
            LocalPaper(conn,case.intent.intent_id,owner='competing-process')
        mid=case.intent.reference_price+(-1 if side=='LONG' else 1)
        first.command('QUOTE',dict(at=now.isoformat(),mid=str(mid),size='100'))
        first.command('SUBMIT',dict(intent_id=str(case.intent.intent_id),client_order_id=case.intent.client_order_id))
        boundary=now+timedelta(seconds=2)
        first.command('QUOTE',dict(at=boundary.isoformat(),mid=str(mid),size='100'))
        observation=FundingObservationV1(case.intent.canonical_symbol,boundary,D('0.0001'),mid,'USDT',
            boundary,boundary,'SETTLED','AVAILABLE','VALID','EXCHANGE_PUBLIC',
            'fixture:canonical-funding/1','FIXTURE_DRIVEN_ACCEPTANCE')
        first.command('FUNDING',payload(observation))
        before=first.state()
        rows=conn.execute('SELECT execution_id,content_digest FROM execution_results ORDER BY execution_id').fetchall()
        journal=store.local_events(case.intent.account_id)
        # Model a backwards wall-clock step between explicit release and restart,
        # without changing the OS clock or replacing any database/store operation.
        release_at=datetime.now(timezone.utc)+timedelta(seconds=5)
        class ReleaseClock:
            @staticmethod
            def now(tz):return release_at
        with monkeypatch.context() as clock:
            clock.setattr(persistence,'datetime',ReleaseClock)
            first.close()
        assert conn.execute(
            'SELECT owner_id,owner_epoch FROM execution_accounts WHERE account_id=%s',
            (case.intent.account_id,),
        ).fetchone()==(first.owner,first.epoch)
        second=LocalPaper(conn,case.intent.intent_id,owner='restart-process')
        assert second.epoch>first.epoch and second.restored
        assert second.state()==before and second.repair_count==0
        assert second.session.adapter.entry_order_count==1
        assert conn.execute('SELECT execution_id,content_digest FROM execution_results ORDER BY execution_id').fetchall()==rows
        assert conn.execute('SELECT count(*) FROM execution_funding_payments').fetchone()[0]==1
        assert store.local_events(case.intent.account_id)[:len(journal)]==journal
        with pytest.raises(FencedOwner):
            store.assert_owner(case.intent.account_id,first.owner,first.epoch,now=datetime.now(timezone.utc))
        # A stale release must not invalidate the current execution owner.
        store.release_owner(case.intent.account_id,first.owner,first.epoch)
        store.assert_owner(case.intent.account_id,second.owner,second.epoch,now=datetime.now(timezone.utc))
        with pytest.raises(FencedOwner):
            LocalPaper(conn,case.intent.intent_id,owner='arbitrary-second-process')
    finally:
        if second is not None:second.close()
        first.close()
