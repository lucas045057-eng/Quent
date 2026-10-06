import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_data_layer.db_admission import PostgresWriteAdmission
from quant_phase1.contracts import DataStatus
from quant_phase1.stage1 import Stage1Result
from quant_phase9.config import Phase9RuntimeConfig
from quant_phase9.intake import Phase9OutboxWriter, build_stage1_candidate_event
from quant_phase9.policy import load_approved_policy_manifest
from quant_phase9.runtime import Phase9DeterministicEvaluator, Phase9EngineRuntime
from tests.quant_phase9.test_runtime import runtime_database
from tests.quant_phase9.test_policy import _files


@pytest.fixture
def runtime_case(runtime_database, tmp_path):
    dsn, factory = runtime_database
    paths = _files(tmp_path)
    policy = load_approved_policy_manifest(*paths, expected_commit='b'*40)
    now = datetime.now(timezone.utc)-timedelta(seconds=2)
    result = Stage1Result(symbol='BTCUSDT', category='A', reason='fixture',
        status=DataStatus.AVAILABLE, inputs_used=('price',), indicators={'atr': Decimal('1')},
        structure='BULLISH', reason_codes=('STRUCTURE_ALIGNED',), timestamp=now)
    event = build_stage1_candidate_event(screening_result_id=31, run_id=32,
        screening_result=result, market='USDT_PERPETUAL', instrument_scope={
            'category':'USDT-FUTURES','quote_coin':'USDT','contract_type':'perpetual',
            'status':'online','in_scope':'true'}, candidate_created_at=now,
        candidate_valid_until=now+timedelta(hours=1), stage1_policy_version='phase1-basic-v1',
        source_as_of=now)
    with factory(dsn) as conn:
        conn.execute('''INSERT INTO symbols(symbol,category,base_coin,quote_coin,symbol_type,
            contract_type,status,price_precision,quantity_precision,min_order_qty,source,exchange,fetched_at)
            VALUES ('BTCUSDT','USDT-FUTURES','BTC','USDT','PERPETUAL','perpetual','online',2,3,.001,'fixture','bitget',%s)''', (now,))
        Phase9OutboxWriter().emit(conn,event=event)
    admission = PostgresWriteAdmission(connection_factory=factory)
    args = dict(dsn=dsn,policy_manifest=policy,connection_factory=factory,db_admission=admission)
    yield args, event
    admission.close(timeout_seconds=2)


def test_hard_contract_failure_is_durable_and_never_automatically_retried(runtime_case):
    args, event = runtime_case
    def invalid(*_):
        raise ValueError('invalid fixture contract')
    runtime = Phase9EngineRuntime(config=Phase9RuntimeConfig(enabled=True),evaluator=invalid,**args)
    with pytest.raises(ValueError):
        asyncio.run(runtime.poll_once())
    with args['connection_factory'](args['dsn']) as conn:
        states = conn.execute('SELECT evaluation_state,reason_code FROM phase9_evaluations').fetchall()
        assert states == [('FAILED','CONTRACT_INVALID')]*3
        assert conn.execute('SELECT phase9_state,attempt_count FROM outbox_events').fetchone() == ('ACKNOWLEDGED',1)
        assert conn.execute('SELECT count(*) FROM phase9_decision_candidates').fetchone()[0] == 0
    assert asyncio.run(runtime.poll_once()) == 0


def test_runtime_timeout_setting_prevents_late_final_persistence(runtime_case):
    import time
    args, _ = runtime_case
    class SlowLocalReader:
        def read(self,*_,**__):
            time.sleep(1.1)
            return ()
    evaluator = Phase9DeterministicEvaluator(source_reader=SlowLocalReader(),**args)
    runtime = Phase9EngineRuntime(config=Phase9RuntimeConfig(enabled=True,evaluation_timeout_seconds=1),
                                  evaluator=evaluator,**args)
    with pytest.raises(TimeoutError):
        asyncio.run(runtime.poll_once())
    with args['connection_factory'](args['dsn']) as conn:
        assert conn.execute('SELECT count(*) FROM phase9_decision_candidates').fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM phase9_evaluations WHERE evaluation_state='DEGRADED' AND reason_code='EVALUATION_TIMEOUT'").fetchone()[0] == 3


def test_runtime_deadline_cancels_a_real_slow_postgres_statement(runtime_case):
    import time
    args,_ = runtime_case
    class SlowDatabaseReader:
        def read(self,conn,**_):
            conn.execute('SELECT pg_sleep(8)')
            return ()
    evaluator = Phase9DeterministicEvaluator(source_reader=SlowDatabaseReader(),**args)
    runtime = Phase9EngineRuntime(config=Phase9RuntimeConfig(enabled=True,evaluation_timeout_seconds=1),
                                  evaluator=evaluator,**args)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(runtime.poll_once())
    assert time.monotonic()-started < 4


def test_append_only_expiry_is_written_once_without_mutating_decision(runtime_case):
    from tests.quant_phase9.test_persistence import _seed, _call, NOW
    args, _ = runtime_case
    with args['connection_factory'](args['dsn']) as conn:
        data = _seed(conn, candidate_id=99,event_id='9'*64)
        _call(conn,data,event_id='9'*64)
        before = conn.execute('SELECT payload FROM phase9_decision_candidates').fetchall()
    runtime = Phase9EngineRuntime(config=Phase9RuntimeConfig(enabled=True),
                                  evaluator=lambda *_:None,**args)
    runtime.maintain_lifecycle(now=NOW+timedelta(hours=2))
    runtime.maintain_lifecycle(now=NOW+timedelta(hours=2))
    with args['connection_factory'](args['dsn']) as conn:
        assert conn.execute("SELECT count(*) FROM phase9_decision_status_events WHERE status='EXPIRED'").fetchone()[0] == 1
        assert conn.execute('SELECT payload FROM phase9_decision_candidates').fetchall() == before


def test_revalidation_deduplicates_numeric_drift_and_atomically_supersedes_semantic_change(runtime_case,tmp_path):
    from quant_phase9.sources import make_projection, context_evaluation_id
    from quant_phase9.contracts import SourcePhaseV1, PolicyDataStatusV1, EvidenceFreshnessV1, EvidenceQualityV1
    from tests.quant_phase9.test_decision import _policy
    args, _ = runtime_case
    args['policy_manifest'] = _policy(tmp_path,enabled=True)
    with args['connection_factory'](args['dsn']) as conn:
        conn.execute('CREATE TABLE fixture_revalidation_input(value integer NOT NULL)')
        conn.execute('INSERT INTO fixture_revalidation_input VALUES (12)')
    class Reader:
        def read(self,conn,*,candidate,timeframe,as_of):
            value = conn.execute('SELECT value FROM fixture_revalidation_input').fetchone()[0]
            return (make_projection(evaluation_id=context_evaluation_id(candidate,timeframe,as_of),
                source_phase=SourcePhaseV1.PHASE2,source_type='OPEN_INTEREST',source_ref='phase2:fixture_revalidation_input/1',
                symbol=candidate.symbol,market=candidate.market,event_time=as_of,observed_at=as_of,
                captured_at=as_of,processed_at=as_of,available_at=as_of,availability_status=PolicyDataStatusV1.AVAILABLE,
                freshness=EvidenceFreshnessV1.FRESH,quality=EvidenceQualityV1.VALID if value>=0 else EvidenceQualityV1.PARTIAL,coverage=None,
                payload={'open_interest_usd':Decimal(abs(value)),
                         'normalization_method':'exchange_reported_quote_notional'}),)
    evaluator = Phase9DeterministicEvaluator(source_reader=Reader(),**args)
    runtime = Phase9EngineRuntime(config=Phase9RuntimeConfig(enabled=True),evaluator=evaluator,**args)
    assert asyncio.run(runtime.poll_once()) == 1
    with args['connection_factory'](args['dsn']) as conn:
        before = conn.execute("SELECT decision_id FROM phase9_decision_candidates WHERE timeframe='1H'").fetchone()[0]
        conn.execute('UPDATE fixture_revalidation_input SET value=13')
        conn.execute("UPDATE phase9_revalidation_cursors SET next_due=now()-interval '1 second'")
    assert asyncio.run(runtime.poll_revalidations()) == 1
    with args['connection_factory'](args['dsn']) as conn:
        assert conn.execute('SELECT count(*) FROM phase9_decision_candidates').fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM phase9_evaluations WHERE reason_code='SEMANTIC_UNCHANGED'").fetchone()[0] == 1
        conn.execute('UPDATE fixture_revalidation_input SET value=-1')
        conn.execute("UPDATE phase9_revalidation_cursors SET next_due=now()-interval '1 second'")
    assert asyncio.run(runtime.poll_revalidations()) == 1
    with args['connection_factory'](args['dsn']) as conn:
        assert conn.execute('SELECT count(*) FROM phase9_decision_candidates').fetchone()[0] == 4
        replaced = conn.execute("SELECT payload->'value'->>'supersedes_decision_id' FROM phase9_decision_candidates WHERE timeframe='1H' ORDER BY created_at DESC LIMIT 1").fetchone()[0]
        assert replaced == str(before)
        assert conn.execute("SELECT count(*) FROM phase9_decision_status_events WHERE status='SUPERSEDED' AND decision_id=%s",(before,)).fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM phase9_evidence_chains').fetchone()[0] == 5
    assert asyncio.run(runtime.poll_revalidations()) == 0


def test_revalidation_budget_and_material_notifications_are_durable_and_coalesced(runtime_case,tmp_path):
    from tests.quant_phase9.test_decision import _policy
    args,_ = runtime_case
    args['policy_manifest'] = _policy(tmp_path,enabled=True)
    evaluator = Phase9DeterministicEvaluator(**args)
    runtime = Phase9EngineRuntime(config=Phase9RuntimeConfig(enabled=True,max_revalidations_per_minute=1),
                                  evaluator=evaluator,**args)
    asyncio.run(runtime.poll_once())
    for _ in range(3):
        runtime.request_revalidation(stage1_candidate_id=31,timeframe='1H')
    assert asyncio.run(runtime.poll_revalidations()) == 1
    runtime.request_revalidation(stage1_candidate_id=31,timeframe='1H')
    assert asyncio.run(runtime.poll_revalidations()) == 0
    with args['connection_factory'](args['dsn']) as conn:
        assert conn.execute('SELECT requested FROM phase9_revalidation_cursors').fetchone()[0] is True
        assert conn.execute('SELECT runs FROM phase9_revalidation_budget').fetchone()[0] == 1


@pytest.mark.parametrize('mode',['fake','recorded'])
def test_runtime_conflict_reviewer_uses_local_phase6_and_strict_nested_validation(mode):
    from quant_phase9.runtime_jev import JevConflictReviewer
    from tests.quant_phase9.test_jev import _context
    reviewer = JevConflictReviewer.for_fixture(mode)
    first = reviewer(_context())
    second = reviewer(_context())
    assert first == second
    assert first.status.value == 'COMPLETED'
    assert first.relation.value == 'INDETERMINATE'
    assert first.conflict_severity.value == 'HIGH'
    assert reviewer.external_calls == 0
    assert reviewer.calls == 2


@pytest.mark.parametrize('mode',['unconfigured','fake','recorded'])
def test_runtime_persists_conflict_review_before_decision_reference(runtime_case,mode):
    from quant_phase9.runtime_jev import JevConflictReviewer
    from quant_phase9.sources import make_projection,context_evaluation_id
    from quant_phase9.contracts import SourcePhaseV1,PolicyDataStatusV1,EvidenceFreshnessV1,EvidenceQualityV1,PolicyCoverageStatusV1
    args,_ = runtime_case
    class ConflictingFlow:
        def read(self,conn,*,candidate,timeframe,as_of):
            return (make_projection(evaluation_id=context_evaluation_id(candidate,timeframe,as_of),
                source_phase=SourcePhaseV1.PHASE3,source_type='TRADE_FLOW_WINDOW',
                source_ref='phase3:trade_flow_windows/fixture',symbol=candidate.symbol,market=candidate.market,
                event_time=as_of,observed_at=as_of,captured_at=as_of,processed_at=as_of,available_at=as_of,
                availability_status=PolicyDataStatusV1.AVAILABLE,freshness=EvidenceFreshnessV1.FRESH,
                quality=EvidenceQualityV1.VALID,coverage=PolicyCoverageStatusV1.SOURCE_DECLARED_COMPLETE,
                payload={'delta_base':Decimal('-10'),'unknown_trade_count':0}),)
    reviewer = JevConflictReviewer(mode=mode)
    evaluator = Phase9DeterministicEvaluator(source_reader=ConflictingFlow(),conflict_reviewer=reviewer,**args)
    runtime = Phase9EngineRuntime(config=Phase9RuntimeConfig(enabled=True),evaluator=evaluator,**args)
    assert asyncio.run(runtime.poll_once()) == 1
    with args['connection_factory'](args['dsn']) as conn:
        rows = conn.execute('''SELECT r.status,d.eligible FROM phase9_decision_candidates d
            JOIN phase9_jev_reviews r ON r.review_id=d.jev_review_id''').fetchall()
        assert rows == [('NOT_CONFIGURED' if mode=='unconfigured' else 'COMPLETED',False)]*3
        assert conn.execute('SELECT count(*) FROM phase9_jev_reviews').fetchone()[0] == 3
    assert reviewer.external_calls == 0


def test_revalidation_restart_reuses_exact_durable_snapshot_and_generation(runtime_case,tmp_path):
    from quant_phase9.revalidation import claim
    from tests.quant_phase9.test_decision import _policy
    args,_ = runtime_case
    args['policy_manifest'] = _policy(tmp_path,enabled=True)
    evaluator = Phase9DeterministicEvaluator(**args)
    runtime = Phase9EngineRuntime(config=Phase9RuntimeConfig(enabled=True),evaluator=evaluator,**args)
    asyncio.run(runtime.poll_once())
    now = datetime.now(timezone.utc)
    with args['connection_factory'](args['dsn']) as conn:
        conn.execute("UPDATE phase9_revalidation_cursors SET next_due=now()-interval '1 second'")
        jobs = claim(conn,policy=args['policy_manifest'],consumer=runtime.consumer_name,now=now,limit=1,max_per_minute=16)
    event,identities = jobs[0]
    frozen = evaluator._snapshot(event,identities[0])
    for _ in range(3):
        runtime.request_revalidation(stage1_candidate_id=31,timeframe='1H')
    with args['connection_factory'](args['dsn']) as conn:
        conn.execute("UPDATE phase9_evaluations SET lease_expires_at=now()-interval '1 second' WHERE evaluation_id=%s",(frozen.evaluation_id,))
        conn.execute("UPDATE symbols SET status='offline'")
    restarted = Phase9EngineRuntime(config=Phase9RuntimeConfig(enabled=True),
        evaluator=Phase9DeterministicEvaluator(**args),**args)
    assert asyncio.run(restarted.poll_revalidations()) == 1
    with args['connection_factory'](args['dsn']) as conn:
        assert conn.execute('SELECT snapshot_digest FROM phase9_evaluation_snapshots WHERE evaluation_id=%s',(frozen.evaluation_id,)).fetchone()[0] == frozen.snapshot_digest
        assert conn.execute('SELECT attempt_count,evaluation_state FROM phase9_evaluations WHERE evaluation_id=%s',(frozen.evaluation_id,)).fetchone() == (2,'COMPLETED')
        assert conn.execute("SELECT count(*) FROM phase9_evaluations WHERE material_change_generation LIKE 'revalidate:%'").fetchone()[0] == 1
        assert conn.execute('SELECT requested,next_due<=now() FROM phase9_revalidation_cursors').fetchone() == (True,True)
