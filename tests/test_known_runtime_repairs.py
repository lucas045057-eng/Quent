"""Regression fixtures are not real data or candidate fill evidence."""
import asyncio
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
import pytest

@pytest.mark.asyncio
async def test_full_market_work_does_not_block_clock_and_cancellation_joins_before_release(monkeypatch):
    from quant_phase1.entrypoints import engine
    from quant_phase1.config import Settings
    entered=threading.Event();finished=threading.Event();release=threading.Event();state=[]
    def work(*args,**kwargs):
        entered.set();assert release.wait(2);finished.set();return {'symbols':478}
    class Admission:
        @asynccontextmanager
        async def admit(self,request):
            state.append('held')
            try:yield
            finally:assert finished.is_set();state.append('released')
    monkeypatch.setattr(engine,'run_database_cycle',work)
    task=asyncio.create_task(engine._run_admitted_database_cycle(Settings.from_env({'TRADING_MODE':'paper'}),object(),None,Admission()))
    assert await asyncio.to_thread(entered.wait,1)
    # A heartbeat on the same event loop can execute while the DB worker waits.
    await asyncio.sleep(0)
    task.cancel();await asyncio.sleep(.01)
    assert state==['held'] and not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):await task
    assert state==['held','released']

def test_latest_flow_query_is_bounded_by_symbol_metric_and_keeps_clocks(monkeypatch):
    from strategies.sources import enrich_view
    from tests.strategies.test_execution_policy import setup
    queries=[]
    monkeypatch.setattr('strategies.sources.read_rows',lambda conn,q,p:queries.append((q,p)) or [])
    enrich_view(None,setup()[1],batch=SimpleNamespace(tickers=[],candles_by_symbol={}))
    q,p=next((q,p) for q,p in queries if 'FROM market_observations' in q)
    assert 'CROSS JOIN LATERAL' in q and 'LIMIT 1' in q
    assert "source='bitget:uta:sbe:xml-v4'" in q
    assert 'fetched_at<=%s' in q and 'processed_at<=%s' in q and p[0]==['SOLUSDT']

@pytest.mark.asyncio
async def test_failed_candle_is_durable_partial_and_db_retry_keeps_receipt_no_http_retry(monkeypatch):
    from quant_phase7.bitget_sbe import FlowBucket,BitgetSbeFlowWorker
    from quant_phase7.runtime import Phase7CollectorRuntime
    from quant_phase1.config import Settings
    now=datetime.now(timezone.utc)
    bucket=FlowBucket(now-timedelta(seconds=335),'BTCUSDT','SPOT',now-timedelta(seconds=336))
    owner=Phase7CollectorRuntime(Settings.from_env({'BITGET_SBE_FLOW_ENABLED':'true','TRADING_MODE':'paper'}))
    worker=BitgetSbeFlowWorker(owner);calls=[];writes=[]
    async def fail(*args):calls.append(1);raise TimeoutError('SYNTHETIC_SECRET')
    def persist(b,proof,at):
        writes.append((proof,at))
        if len(writes)==1:raise ValueError('SYNTHETIC_DB_FAILURE')
    async def health(*args):pass
    monkeypatch.setattr(worker,'fetch_candle',fail);monkeypatch.setattr(worker,'persist',persist);monkeypatch.setattr(worker,'health',health)
    await worker.finalize_bucket(None,bucket)
    assert not bucket.persisted and bucket.reconciliation_proof['coverage']=='PARTIAL'
    await worker.finalize_bucket(None,bucket);await worker.finalize_bucket(None,bucket)
    assert len(calls)==1 and bucket.persisted and len(writes)==2
    assert writes[0][0]==writes[1][0] and writes[1][1]>=writes[0][1]
    assert datetime.fromisoformat(writes[0][0]['checked_at'])<=writes[0][1]
    assert 'SYNTHETIC_SECRET' not in str(writes)
    worker.buckets[('SPOT','BTCUSDT',bucket.window_open)]=bucket
    worker.trim(bucket.window_close+timedelta(minutes=10),force=True)
    assert not worker.buckets

def test_timeout_provenance_is_whitelisted_and_deadline_remains_fail_closed():
    from strategies.runtime import ResearchDeadlineExceeded,research_stage
    from quant_phase9.runtime import Phase9EngineRuntime
    for stage in ('SOURCE_REFRESH','AI_ANALYSIS','FINAL_CANONICAL_READ'):
        with pytest.raises(ResearchDeadlineExceeded) as e:
            with research_stage(stage,'SOLUSDT',time.monotonic()-1):pytest.fail('expired work ran')
        assert Phase9EngineRuntime._timeout_reason(e.value)=='EVALUATION_TIMEOUT_'+stage
    assert Phase9EngineRuntime._timeout_reason(TimeoutError('SYNTHETIC_SECRET'))=='EVALUATION_TIMEOUT'
    assert Phase9EngineRuntime._timeout_reason(ResearchDeadlineExceeded('SYNTHETIC_SECRET'))=='EVALUATION_TIMEOUT'


def test_missing_required_flow_skips_paid_ai_but_retired_event_sources_do_not():
    from tests.strategies.test_deep_analyzer import complete_snapshot, service
    from strategies.analysis.deep_analyzer import analyze_candidate, AnalysisPolicyV2
    from strategies.evidence.evidence_chain import build_evidence_chain, MarketHypothesis

    snap = complete_snapshot()
    gateway, fake = service(snap)
    result = analyze_candidate(snap, provider=gateway, policy=AnalysisPolicyV2(
        provider_name='fixture', model='fixture'))
    assert fake.calls == 1 and all(t.bias == 'LONG' for t in result)

    partial = snap.model_copy(update={
        'observations': tuple(o for o in snap.observations if o.kind != 'SPOT_FLOW')
    })
    gateway, fake = service(partial)
    result = analyze_candidate(partial, provider=gateway, policy=AnalysisPolicyV2(
        provider_name='fixture', model='fixture'))
    assert fake.calls == 0 and all(t.bias == 'WAIT' and t.no_trade_reason == 'NO_TRADE_BY_DATA' for t in result)

    legacy = tuple(o.model_copy(update={'kind': kind, 'value': 1, 'unit': 'RISK_FLAG'})
        for kind in ('EVENT_COVERAGE', 'EXCHANGE_EVENT_COVERAGE', 'MACRO_COVERAGE')
        for o in snap.observations if o.kind == 'OI')
    with_legacy = snap.model_copy(update={'observations': (*snap.observations, *legacy)})
    chain = build_evidence_chain(with_legacy, hypothesis=MarketHypothesis(
        direction='LONG', timeframe='15m', structure='TREND_CONTINUATION'))
    assert chain.confirmed and chain.advisories == ()


def test_cached_complete_proof_is_invalidated_by_late_conflicting_trade():
    from quant_phase7.bitget_sbe import FlowBucket,decode_trades
    from tests.test_bitget_sbe_flow import frame,NOW
    from dataclasses import replace
    from decimal import Decimal
    b=FlowBucket(NOW,'BTCUSDT','SPOT',NOW-timedelta(seconds=1))
    trade=decode_trades(frame())[0];b.add(trade,NOW)
    b.persisted=True;b.reconciliation_proof={'coverage':'COMPLETE'}
    b.add(replace(trade,quantity=Decimal('2')),NOW+timedelta(seconds=340))
    assert b.fault=='DUPLICATE_ID_CONFLICT' and not b.persisted and b.reconciliation_proof is None


@pytest.mark.asyncio
async def test_midwindow_partial_does_not_request_candle(monkeypatch):
    from quant_phase7.bitget_sbe import FlowBucket,BitgetSbeFlowWorker
    from quant_phase7.runtime import Phase7CollectorRuntime
    from quant_phase1.config import Settings
    now=datetime.now(timezone.utc)
    bucket=FlowBucket(now-timedelta(seconds=335),'BTCUSDT','SPOT',now-timedelta(seconds=200))
    owner=Phase7CollectorRuntime(Settings.from_env({'BITGET_SBE_FLOW_ENABLED':'true','TRADING_MODE':'paper'}))
    worker=BitgetSbeFlowWorker(owner);writes=[]
    async def candle(*args):pytest.fail('known partial window made a public request')
    async def health(*args):pass
    monkeypatch.setattr(worker,'fetch_candle',candle)
    monkeypatch.setattr(worker,'persist',lambda b,p,at:writes.append(p))
    monkeypatch.setattr(worker,'health',health)
    await worker.finalize_bucket(None,bucket)
    assert bucket.persisted and writes[0]['coverage']=='PARTIAL'
    assert writes[0]['reason']=='SUBSCRIBED_MIDWINDOW' and writes[0]['candle'] is None


def test_sbe_failure_reasons_keep_contract_detail_without_arbitrary_exception_text():
    from quant_phase7.bitget_sbe import _failure_reason
    assert _failure_reason(ValueError('SBE_BEFORE_SUBSCRIPTION_ACK'))=='SBE_BEFORE_SUBSCRIPTION_ACK'
    assert _failure_reason(ValueError('SBE_DISCONNECTED'))=='SBE_DISCONNECTED'
    assert _failure_reason(ValueError('SYNTHETIC_SECRET'))=='ValueError'
    assert _failure_reason(ValueError('SBE_DISCONNECTED SYNTHETIC_SECRET'))=='ValueError'
    assert _failure_reason(TimeoutError('SYNTHETIC_SECRET'))=='TimeoutError'
