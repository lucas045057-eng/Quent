"""Synthetic regression evidence only, never actual Goal trades/acceptance."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace as NS
import pytest
from quant_realtime_paper.config import RuntimeConfig
from quant_realtime_paper.runtime import RealtimePaperMonitor
from quant_realtime_paper.store import SessionStore
from quant_phase1.contracts import DataStatus

LOCK={'TRADING_MODE':'paper','PAPER_ONLY':'true','LIVE_ALLOWED':'false'}

def monitor_fixture(tmp_path, *, category='B', source='REAL_PUBLIC_DATA',
                    failure=None, preflight_failure=False, management_failure=False):
    now=datetime.now(timezone.utc)
    stage=NS(symbol='BTCUSDT',category=category,status=DataStatus.AVAILABLE)
    snapshot={'source':'bitget_v3_ws','symbol':'BTCUSDT'}
    row={'symbol':'BTCUSDT','feeds':[{'kind':'TICKER_MARK','status':'HEALTHY'},
         {'kind':'FUNDING','status':'HEALTHY'}]+[{'kind':'KLINE','status':'HEALTHY'}]*4,
         'market_snapshot':snapshot,'features':{},'_input':snapshot,'_stage1_result':stage,
         'stage1':{'category':category,'classification':'WAIT_TRIGGER','structure':'BULLISH',
                   'reason_codes':[]}}
    class Pipeline:
        operational_preflight=True
        event_sink=None
        calls=0
        def operational_readiness(self, *, now):
            if preflight_failure:raise ValueError('unreconciled')
            return {'paper_engine':True,'reconciliation':True}
        def manage_positions(self, *, now):
            if management_failure:raise ValueError('failed exit management')
            return ()
        def process_stage1(self, stage, **kwargs):
            self.calls+=1
            if failure=='EXCEPTION':raise ValueError('pipeline fault')
            reason=failure or 'STAGE1_NOT_ELIGIBLE'
            return NS(disposition='NO_TRADE_BY_SYSTEM_NOT_READY' if failure else 'NO_TRADE_BY_STRATEGY',
                reason_code=reason,decision_candidate=None,execution_intent=None,execution_results=(),
                position_snapshot=None,reconciliation='NOT_READY',
                events=({'stage':'phase9','reason_code':reason},))
    pipeline=Pipeline()
    store=SessionStore(tmp_path/'state.sqlite3')
    session=store.start_session({'symbols':['BTCUSDT'],'data_source':'UNKNOWN'})
    monitor=RealtimePaperMonitor(RuntimeConfig('dsn',tmp_path/'state.sqlite3',2,('BTCUSDT',),tmp_path),
        store=store,environ=LOCK,execution_pipeline=pipeline,risk_readiness=lambda:True)
    monitor._session_id=session.session_id
    monitor._read_canonical=lambda at:(None,{},[],{'data_type_rows':{}})
    monitor._market=lambda *args:([row],[stage],source,True,[])
    monitor._supplemental_freshness=lambda *args:[]
    monitor._check_collector=lambda *args:(True,'COLLECTOR_HEALTHY')
    monitor._fresh_market_data_status=lambda *args:'HEALTHY'
    monitor._snapshot_strategy=lambda:(True,'APPROVED_POLICY_ACTIVE')
    return monitor,pipeline,now

def test_no_candidate_proves_operation_but_never_entry(tmp_path):
    monitor,pipeline,now=monitor_fixture(tmp_path)
    result=monitor.cycle(now=now)
    assert result['readiness']=='PAPER READY'
    assert result['readiness_detail']['operational_ready'] is True
    assert result['readiness_detail']['entry_ready'] is False
    assert result['readiness_detail']['execution_ready'] is False
    assert result['readiness_detail']['candidate_validity'] is False
    assert result['readiness_detail']['reason_code']=='OPERATIONAL_READY_NO_ELIGIBLE_ENTRY'
    assert result['counts']['orders']==result['counts']['trades']==0
    assert result['symbols'][0]['execution_intent'] is None
    assert pipeline.calls==1

@pytest.mark.parametrize('source',['UNKNOWN','MIXED_REJECTED','SYNTHETIC_FIXTURE'])
def test_bad_real_source_stays_not_ready(tmp_path,source):
    monitor,_,now=monitor_fixture(tmp_path,source=source)
    result=monitor.cycle(now=now)
    assert result['readiness']=='PAPER NOT READY'
    assert 'real_public_source' in result['blockers']
    assert result['readiness_detail']['entry_ready'] is False

@pytest.mark.parametrize('failure',['EVALUATION_TIMEOUT','SECURITY_EVENT_VETO','STALE_ACCOUNT','EXCEPTION'])
def test_actual_a_fault_is_not_hidden_by_successful_operational_probe(tmp_path,failure):
    monitor,_,now=monitor_fixture(tmp_path,category='A',failure=failure)
    result=monitor.cycle(now=now)
    assert result['readiness']=='PAPER NOT READY'
    assert 'candidate_pipeline' in result['blockers']
    assert result['readiness_detail']['entry_ready'] is False
    assert result['counts']['orders']==result['counts']['trades']==0

@pytest.mark.parametrize('kwargs',[{'preflight_failure':True},{'management_failure':True}])
def test_native_or_exit_fault_is_not_ready(tmp_path,kwargs):
    monitor,_,now=monitor_fixture(tmp_path,**kwargs)
    result=monitor.cycle(now=now)
    assert result['readiness']=='PAPER NOT READY'
    assert result['readiness_detail']['operational_ready'] is False
    assert result['readiness_detail']['entry_ready'] is False

def instrument():
    from strategies.market_view import InstrumentMetadata
    from quant_nautilus.instruments import build_public_instrument
    return build_public_instrument(InstrumentMetadata(symbol='SOLUSDT',tick='.01',lot='.01',
        min_quantity='.01',max_quantity='1000',min_notional='5',settlement_currency='USDT',
        source_ref='bitget:instruments',source_digest='a'*64))

def test_original_native_probe_runs_without_intent_or_order(monkeypatch):
    from quant_nautilus.sandbox import SandboxSession
    closed=[]
    original=SandboxSession.close
    def close(session):
        assert not session.cache.orders() and not session.cache.positions_open()
        assert session.intent is None and session.adapters=={}
        original(session)
        closed.append(session.loop.is_closed())
    monkeypatch.setattr(SandboxSession,'close',close)
    proof=SandboxSession.preflight_empty_account(instrument(),starting_balance=Decimal('1000'),
        now=datetime.now(timezone.utc),max_leverage=Decimal('3'))
    assert proof=={'paper_engine':True,'reconciliation':True,'native_orders':0}
    assert closed==[True]

@pytest.mark.parametrize('balance',[Decimal('0'),Decimal('-1'),Decimal('NaN')])
def test_native_probe_rejects_invalid_funded_balance(balance):
    from quant_nautilus.sandbox import SandboxSession
    with pytest.raises(ValueError,match='PAPER_OPERATIONAL_PREFLIGHT_INVALID'):
        SandboxSession.preflight_empty_account(instrument(),starting_balance=balance,
            now=datetime.now(timezone.utc),max_leverage=Decimal('3'))


def test_completed_candidate_proof_is_not_lost_among_observation_rows(tmp_path):
    monitor,pipeline,now=monitor_fixture(tmp_path,category='A')
    original_market=monitor._market
    def market(*args):
        rows,results,source,ready,details=original_market(*args)
        from copy import deepcopy
        d=deepcopy(rows[0]);d['symbol']='ETHUSDT'
        d['_stage1_result']=NS(symbol='ETHUSDT',category='D',status=DataStatus.NOT_AVAILABLE)
        d['stage1']['category']='D'
        return rows+[d],results+[d['_stage1_result']],source,ready,details
    monitor._market=market
    pipeline.process_stage1=lambda *args,**kwargs:NS(disposition='PAPER_RESULT_RECORDED',
        reason_code='PAPER_RESULT_RECORDED',decision_candidate=NS(eligible=True),
        execution_intent={'mode':'PAPER'},execution_results=(),position_snapshot=None,
        reconciliation='RECONCILED',events=())
    result=monitor.cycle(now=now)
    assert result['readiness_detail']['entry_ready'] is True
    assert result['readiness_detail']['execution_ready'] is True
    assert result['symbols'][0]['no_trade_classification'] is None
    assert result['readiness_detail']['legacy_entry_checks']['stage1_ready'] is False

def test_missing_pipeline_outcome_is_a_failure_not_absent_signal(tmp_path):
    monitor,pipeline,now=monitor_fixture(tmp_path,category='A')
    pipeline.process_stage1=lambda *args,**kwargs:None
    result=monitor.cycle(now=now)
    assert result['readiness']=='PAPER NOT READY'
    assert result['readiness_detail']['reason_code']=='RUNTIME_PIPELINE_FAILED'

@pytest.mark.parametrize('invalid',[False,1,'true',None])
def test_pipeline_never_accepts_non_boolean_operating_proof(invalid):
    from quant_realtime_paper.wiring import RealtimePaperExecutionPipeline
    p=RealtimePaperExecutionPipeline(approved_policy=None,current_revision='b'*40,
        phase9_evaluate=None,risk_inputs=None,execution_store=None,adapter_factory=None,
        environ=LOCK,operational_preflight=lambda now:{'paper_engine':invalid,'reconciliation':True})
    with pytest.raises(ValueError,match='PAPER_OPERATIONAL_PREFLIGHT_FAILED'):
        p.operational_readiness(now=datetime.now(timezone.utc))


def test_native_probe_closes_partial_initialization(monkeypatch):
    import asyncio
    from quant_nautilus.sandbox import SandboxSession
    loops=[]
    def failed(self,*args,**kwargs):
        self.loop=asyncio.new_event_loop();loops.append(self.loop)
        raise ValueError('native initialization failed')
    monkeypatch.setattr(SandboxSession,'_initialize_core',failed)
    with pytest.raises(ValueError,match='native initialization failed'):
        SandboxSession.preflight_empty_account(instrument(),starting_balance=Decimal('1000'),
            now=datetime.now(timezone.utc),max_leverage=Decimal('3'))
    assert loops[0].is_closed()

def test_native_operating_probe_rejects_external_venue():
    from quant_nautilus.sandbox import SandboxSession
    from nautilus_trader.test_kit.providers import TestInstrumentProvider
    with pytest.raises(ValueError,match='PAPER_OPERATIONAL_PREFLIGHT_INVALID'):
        SandboxSession.preflight_empty_account(TestInstrumentProvider.btcusdt_perp_binance(),
            starting_balance=Decimal('1000'),now=datetime.now(timezone.utc),max_leverage=Decimal('3'))


def test_no_order_probe_uses_existing_observation_clock_not_execution_quote_clock():
    from datetime import timedelta
    from quant_realtime_paper.assembly import _operating_public_ticker_ready
    from quant_data_layer.freshness import FRESHNESS_POLICY
    at=datetime.now(timezone.utc)
    ticker=NS(exchange='bitget',source='bitget_v3_ws',status=DataStatus.AVAILABLE,
        exchange_timestamp=at-timedelta(seconds=60),fetched_at=at-timedelta(seconds=30),processed_at=at)
    assert FRESHNESS_POLICY['PRICE_EXECUTION'].hard_seconds==5
    assert _operating_public_ticker_ready(ticker,now=at,
        max_age_seconds=FRESHNESS_POLICY['PRICE_STAGE1'].hard_seconds)
    ticker.exchange_timestamp=at-timedelta(seconds=60.001)
    assert not _operating_public_ticker_ready(ticker,now=at,max_age_seconds=60)
    ticker.exchange_timestamp=at+timedelta(seconds=1.5)
    assert _operating_public_ticker_ready(ticker,now=at,max_age_seconds=60)
    ticker.exchange_timestamp=at+timedelta(seconds=2.001)
    assert not _operating_public_ticker_ready(ticker,now=at,max_age_seconds=60)
    ticker.exchange_timestamp=at
    ticker.fetched_at=at+timedelta(seconds=.001)
    assert not _operating_public_ticker_ready(ticker,now=at,max_age_seconds=60)
    ticker.fetched_at=at
    ticker.processed_at=at+timedelta(seconds=.001)
    assert not _operating_public_ticker_ready(ticker,now=at,max_age_seconds=60)

def test_stale_base_market_cannot_pass_with_healthy_native_probe(tmp_path):
    monitor,_,now=monitor_fixture(tmp_path)
    market=monitor._market
    def stale(*args):
        rows,results,source,_,details=market(*args)
        return rows,results,source,False,details
    monitor._market=stale
    result=monitor.cycle(now=now)
    assert result['readiness']=='PAPER NOT READY'
    assert 'market_data' in result['blockers']
    assert result['readiness_detail']['operational_ready'] is False
