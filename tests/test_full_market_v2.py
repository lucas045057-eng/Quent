"""Synthetic regressions for scope expansion, isolation and capacity guards."""
import asyncio
from decimal import Decimal
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import pytest

from tests.strategies.test_batch_screener import market, NOW
from tests.test_bitget_sbe_flow import frame, NOW as FLOW_NOW
from strategies.market_view import MarketView
from strategies.screener.batch_screener import screen_market, ScreeningPolicyV2
from quant_phase1.config import Settings
from quant_phase1.pipeline import MarketDataBatch, split_market_batch
from quant_phase2.runtime import bounded_symbol_map
from quant_phase7.flow_scope import FlowScope,load_flow_scope
from quant_phase7.bitget_sbe import decode_trades,FlowBucket,validate_flow_proof,BitgetSbeFlowWorker

ROOT=Path(__file__).parents[1]

def test_full_market_no_top200_truncation_and_no_promotion_of_missing_data():
    rows=[]
    for i in range(478):
        item=market(symbol=f'X{i}USDT')
        if i<473:item=item.model_copy(update={'turnover':Decimal(1),'observations':()})
        rows.append(item)
    result=screen_market(MarketView(as_of=NOW,symbols=tuple(rows)),policy=ScreeningPolicyV2())
    assert len(result.universe)==478 and len(result.candidates)==478
    assert {c.symbol for c in result.top_candidates}=={f'X{i}USDT' for i in range(473,478)}
    assert all(c.category=='D' and c.reason_codes==('NO_TRADE_BY_LIQUIDITY',) for c in result.candidates[:473])
    unknown=market().model_copy(update={'observations':()})
    assert not screen_market(MarketView(as_of=NOW,symbols=(unknown,)),policy=ScreeningPolicyV2()).top_candidates

def test_large_backfill_split_preserves_every_source_record_and_original_clocks():
    candles={f'X{i}USDT':{'15m':list(range(i*400,(i+1)*400))} for i in range(478)}
    batch=MarketDataBatch(NOW,['original instrument'],['original ticker'],tuple(candles),candles)
    parts=list(split_market_batch(batch,candle_budget=8000))
    assert parts[0].tickers is batch.tickers and parts[0].instruments is batch.instruments
    assert all(p.collected_at==NOW and p.selected_symbols==batch.selected_symbols for p in parts)
    restored={}
    for part in parts[1:]:
        assert not part.tickers and not part.instruments
        assert sum(len(v) for by in part.candles_by_symbol.values() for v in by.values())<=8000
        for symbol,by in part.candles_by_symbol.items():
            for interval,values in by.items():restored.setdefault(symbol,{}).setdefault(interval,[]).extend(values)
    assert restored==candles

def test_catalog_flow_scope_is_explicit_and_fits_existing_connection_budget(monkeypatch):
    from quant_phase1.entrypoints.collector import CollectorService
    path=ROOT/'config/full_market_flow_scope_v2.json'
    scope=load_flow_scope(str(path))
    assert len(scope.perpetual_symbols)>200 and len(scope.spot_symbols)>200
    assert all(len(shard)<=40 for shard in scope.shards())
    collector=CollectorService(Settings.from_env({'UNIVERSE_LIMIT':'1000','BITGET_SBE_FLOW_ENABLED':'true',
        'BITGET_SBE_FLOW_SCOPE_PATH':str(path)}))
    collector.selected_symbols=tuple(scope.perpetual_symbols)
    assert len(collector._ws_topic_groups())+len(scope.shards())<=90
    with pytest.raises(ValueError):
        FlowScope(catalog_checked_at='TEST',perpetual_symbols=('SOLUSDT',),spot_symbols=('BTCUSDT',))

def test_third_asset_flow_requires_catalog_approval_and_same_category_candle():
    scope=FlowScope(catalog_checked_at='TEST',perpetual_symbols=('SOLUSDT',),spot_symbols=('SOLUSDT',))
    with pytest.raises(ValueError,match='NOT_APPROVED'):decode_trades(frame(symbol='SOLUSDT'))
    trade=decode_trades(frame(symbol='SOLUSDT'),allowed_symbols=scope.perpetual_symbols)[0]
    bucket=FlowBucket(FLOW_NOW,'SOLUSDT','SPOT',FLOW_NOW-timedelta(seconds=1))
    bucket.add(trade,FLOW_NOW)
    proof=bucket.proof(candle=[str(int(FLOW_NOW.timestamp()*1000)),'1','1','1','1','.01','1'],
                       checked_at=FLOW_NOW+timedelta(seconds=331))
    assert validate_flow_proof(proof,bucket.row_values(),now=FLOW_NOW+timedelta(seconds=331),scope=scope)
    assert not validate_flow_proof(proof,bucket.row_values(),now=FLOW_NOW+timedelta(seconds=331))
    proof['candle_category']='USDT-FUTURES'
    assert not validate_flow_proof(proof,bucket.row_values(),now=FLOW_NOW+timedelta(seconds=331),scope=scope)

def test_one_disconnected_shard_does_not_relabel_other_asset_proofs():
    worker=BitgetSbeFlowWorker(SimpleNamespace(settings=Settings.from_env({})))
    scopes=(('SPOT','BTCUSDT'),('SPOT','ETHUSDT'))
    worker.acks=dict.fromkeys(scopes,FLOW_NOW)
    for category,symbol in scopes:
        worker.buckets[(category,symbol,FLOW_NOW)]=FlowBucket(FLOW_NOW,symbol,category,FLOW_NOW)
    worker.invalidate('DISCONNECTED',(scopes[0],))
    assert scopes[0] not in worker.acks and scopes[1] in worker.acks
    assert worker.buckets[(*scopes[0],FLOW_NOW)].fault=='DISCONNECTED'
    assert worker.buckets[(*scopes[1],FLOW_NOW)].fault is None

@pytest.mark.asyncio
async def test_parallel_derivatives_isolate_failure_timeout_and_preserve_symbol_order():
    active=0;maximum=0
    async def operation(symbol):
        nonlocal active,maximum
        active+=1;maximum=max(maximum,active)
        try:
            if symbol=='FAIL':raise ValueError('SYNTHETIC')
            await asyncio.sleep(.1 if symbol=='SLOW' else .001)
            return symbol
        finally:active-=1
    results=await bounded_symbol_map(('BTC','FAIL','ETH','SLOW','SOL'),operation,concurrency=2,timeout_seconds=.02)
    assert maximum<=2 and active==0
    assert results[0]=='BTC' and results[2]=='ETH' and results[4]=='SOL'
    assert isinstance(results[1],ValueError) and isinstance(results[3],TimeoutError)

def test_research_factory_reads_expanded_scope_beyond_old_top200(monkeypatch):
    import psycopg
    from strategies.runtime import CanonicalResearchFactory
    from strategies.analysis.deep_analyzer import AnalysisPolicyV2
    from quant_phase1.repositories import Phase1Repository
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):return False
        def execute(self,*args):pass
    calls=[]
    monkeypatch.setattr(psycopg,'connect',lambda *a,**k:Connection())
    monkeypatch.setattr(Phase1Repository,'load_latest_market_batch',lambda self,**kw:calls.append(kw))
    factory=CanonicalResearchFactory('SYNTHETIC_DSN',policy=SimpleNamespace(allowed_symbols=tuple(str(i) for i in range(478))),
                                     analysis_policy=AnalysisPolicyV2(),environ={})
    with pytest.raises(ValueError,match='CANONICAL_MARKET_UNAVAILABLE'):factory.view(NOW)
    assert calls==[{'limit':478,'candle_limit':100}]

def test_expanded_setting_retains_default_and_rejects_unbounded_capacity():
    assert Settings.from_env({}).universe_limit==200
    assert Settings.from_env({'UNIVERSE_LIMIT':'1000','PHASE2_SYMBOL_CONCURRENCY':'8'}).universe_limit==1000
    for env in ({'UNIVERSE_LIMIT':'1001'},{'PHASE2_SYMBOL_CONCURRENCY':'17'}):
        with pytest.raises(ValueError):Settings.from_env(env)

@pytest.mark.parametrize('global_source,symbol_source,expected_calls',[
    ('REAL_PUBLIC_DATA','REAL_PUBLIC_DATA',1),
    ('MIXED_REJECTED','REAL_PUBLIC_DATA',1),
    ('MIXED_REJECTED','SYNTHETIC_FIXTURE',0),
    ('MIXED_REJECTED','UNKNOWN',0),
])
def test_incomplete_foreign_symbol_cannot_block_valid_symbol_or_be_sent_to_execution(tmp_path,global_source,symbol_source,expected_calls):
    from quant_realtime_paper.config import RuntimeConfig
    from quant_realtime_paper.runtime import RealtimePaperMonitor
    from quant_realtime_paper.store import SessionStore
    from quant_phase1.contracts import DataStatus
    def row(symbol,healthy):
        stage=SimpleNamespace(status=DataStatus.AVAILABLE if healthy else DataStatus.NOT_AVAILABLE)
        return {'symbol':symbol,'feeds':[{'kind':k,'status':'HEALTHY' if healthy else 'STALE'} for k in
            ('TICKER_MARK','KLINE','KLINE','KLINE','KLINE','FUNDING')],
            'market_snapshot':{'source':'bitget_v3_ws'},'features':{},
            'stage1':{'category':'B' if healthy else 'D','classification':'WAIT','reason':'TEST','reason_codes':[],
                      'structure':None,'status':stage.status.value,'key_metrics':{}},
            '_stage1_result':stage,'_input':{'source':'bitget_v3_ws'}}
    rows=[row('SOLUSDT',True),row('OTHERUSDT',False)]
    rows[0]['_source_class']=symbol_source
    rows[1]['_source_class']='UNKNOWN'
    calls=[]
    class Pipeline:
        event_sink=None
        def process_stage1(self,stage,**kwargs):
            calls.append(stage)
            return SimpleNamespace(disposition='NO_TRADE_BY_STRATEGY',reason_code='STAGE1_NOT_ELIGIBLE',
                decision_candidate=None,execution_intent=None,execution_results=(),position_snapshot=None,
                reconciliation='NOT_READY',events=())
    cfg=RuntimeConfig('SYNTHETIC_DSN',tmp_path/'session.sqlite3',15,('SOLUSDT','OTHERUSDT'),tmp_path)
    store=SessionStore(cfg.state_db);session=store.start_session({'symbols':list(cfg.symbols)})
    monitor=RealtimePaperMonitor(cfg,store=store,environ={'TRADING_MODE':'paper','PAPER_ONLY':'true','LIVE_ALLOWED':'false'},
                                execution_pipeline=Pipeline(),risk_readiness=lambda:True)
    monitor._session_id=session.session_id
    monitor._read_canonical=lambda now:(None,{},[],{'data_type_rows':{}})
    monitor._market=lambda *args:(rows,[r['_stage1_result'] for r in rows],global_source,False,[])
    monitor._check_collector=lambda *args:(True,'OK')
    monitor._snapshot_strategy=lambda:(True,'APPROVED_POLICY_ACTIVE')
    snapshot=monitor.cycle(now=NOW)
    assert len(calls)==expected_calls
    assert all(c.status is DataStatus.AVAILABLE for c in calls)
    assert snapshot['data_source']==global_source
    assert snapshot['readiness_detail']['data_ready'] is False
    assert snapshot['counts']['orders']==snapshot['counts']['trades']==0
    assert snapshot['symbols'][1]['execution_intent'] is None


def test_trim_bounds_hot_path_and_keeps_original_late_event_retention():
    worker=BitgetSbeFlowWorker(SimpleNamespace(settings=Settings.from_env({})))
    bucket=FlowBucket(FLOW_NOW,'BTCUSDT','SPOT',FLOW_NOW)
    bucket.add(decode_trades(frame())[0],FLOW_NOW);bucket.persisted=True
    worker.buckets[('SPOT','BTCUSDT',FLOW_NOW)]=bucket;worker.pending_trades=1
    worker.trim(FLOW_NOW+timedelta(minutes=14,seconds=59))
    worker.trim(FLOW_NOW+timedelta(minutes=15))
    assert not worker.buckets and worker.pending_trades==0
