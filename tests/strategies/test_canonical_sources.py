from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
import pytest
from tests.strategies.test_execution_policy import setup,NOW

def test_public_v3_quantity_multiplier_and_market_order_limit_are_used():
    from strategies.market_view import build_market_view
    instrument=SimpleNamespace(symbol='SOLUSDT',quote_coin='USDT',min_order_qty=D('.01'),max_order_qty=D('500'),source='bitget_v3_rest',raw_payload={'priceMultiplier':'0.01','quantityMultiplier':'0.01','minOrderAmount':'5','maxMarketOrderQty':'50','status':'online','category':'USDT-FUTURES','type':'perpetual'})
    batch=SimpleNamespace(tickers=[],instruments=[instrument],selected_symbols=['SOLUSDT'],candles_by_symbol={})
    meta=build_market_view(batch,as_of=NOW).symbols[0].instrument
    assert meta.lot==D('.01') and meta.max_quantity==50 and meta.min_notional==5

def test_canonical_scalar_without_stream_coverage_does_not_admit_a(monkeypatch):
    from strategies.sources import enrich_view
    from strategies.screener.batch_screener import screen_market,ScreeningPolicyV2
    view=setup()[1].model_copy(update={'symbols':(setup()[1].symbols[0].model_copy(update={'observations':tuple(o for o in setup()[1].symbols[0].observations if o.kind=='PRICE')}),)})
    monkeypatch.setattr('strategies.sources.read_rows',lambda *a:[])
    result=enrich_view(None,view,batch=SimpleNamespace(tickers=[],candles_by_symbol={}))
    screening=screen_market(result,policy=ScreeningPolicyV2())
    assert not screening.top_candidates and screening.candidates[0].category=='D'
    assert all(o.value is None for o in result.symbols[0].observations if o.kind in {'PERP_FLOW','SPOT_FLOW','OI','FUNDING','BENCHMARK'})

def test_external_raw_receipt_is_normalized_through_existing_aggregator():
    from strategies.providers import receipt_observation,ProviderReceipt
    import json
    markets=('SOLUSDT_PERP.A','SOLUSDT_PERP.6')
    payload=[{'symbol':sym,'history':[{'t':int((NOW-timedelta(hours=2)).timestamp()),'c':100},
        {'t':int((NOW-timedelta(hours=1)).timestamp()),'c':110}]} for sym in markets]
    receipt=ProviderReceipt(provider='coinalyze',symbol='SOLUSDT',kind='CROSS_OI',endpoint='https://api.coinalyze.net/v1/open-interest-history',
        fetched_at=NOW,availability='AVAILABLE',payload_json=json.dumps(payload),market_symbols=markets,
        market_catalog_digest='a'*64,source_scope='SELECTED_NON_BITGET_USDT_PERPETUALS',
        request_parameters_json=json.dumps({'symbols':','.join(markets),'interval':'1hour','convert_to_usd':'true'}))
    observation=receipt_observation(receipt)
    assert observation.value==D('.1') and observation.source_event_time==NOW and observation.source_digest==receipt.digest

def test_invalid_order_metadata_scope_cannot_enter_new_paper_plan():
    from strategies.market_view import build_market_view
    instrument=SimpleNamespace(symbol='SOLUSDT',quote_coin='USDT',min_order_qty=D('.01'),max_order_qty=D('50'),source='bitget_v3_rest',raw_payload={'priceMultiplier':'0.01','quantityMultiplier':'0.01','minOrderAmount':'5','maxMarketOrderQty':'50','status':'limit_close','category':'USDT-FUTURES','type':'perpetual'})
    batch=SimpleNamespace(tickers=[],instruments=[instrument],selected_symbols=['SOLUSDT'],candles_by_symbol={})
    assert build_market_view(batch,as_of=NOW).symbols[0].instrument is None


def test_oi_window_uses_fresh_endpoint_and_finds_fifteen_minute_baseline(monkeypatch):
    from strategies.sources import enrich_view
    view=setup()[1]
    def row(at,base):
        return dict(canonical_symbol='SOL-USDT-PERP',exchange='bitget',exchange_timestamp=at,
            fetched_at=at,processed_at=at,status='AVAILABLE',raw_unit='BASE_ASSET',
            normalization_method='base_quantity_times_mark_price',raw_open_interest=D(base),
            open_interest_base=D(base),mark_price=D(100),open_interest_usd=D(base)*D(100))
    rows=[row(NOW-timedelta(minutes=15),'100'),row(NOW-timedelta(minutes=5),'105'),row(NOW,'110')]
    monkeypatch.setattr('strategies.sources.read_rows',lambda conn,query,args: rows if 'FROM open_interest' in query else [])
    enriched=enrich_view(None,view,batch=SimpleNamespace(tickers=[],candles_by_symbol={}))
    oi=next(o for o in enriched.symbols[0].observations if o.kind=='OI' and o.source_ref.startswith('canonical:'))
    assert oi.value==D('.1') and oi.usable and oi.observed_at==NOW and oi.fetched_at==NOW

def test_one_incomplete_instrument_does_not_abort_other_symbols():
    from strategies.market_view import build_market_view
    raw={'priceMultiplier':'0.01','quantityMultiplier':'0.01','minOrderAmount':'5','maxMarketOrderQty':'50','status':'online','category':'USDT-FUTURES','type':'perpetual'}
    good=SimpleNamespace(symbol='SOLUSDT',quote_coin='USDT',min_order_qty=D('.01'),source='bitget_v3_rest',raw_payload=raw)
    bad=SimpleNamespace(symbol='BADUSDT',quote_coin='USDT',min_order_qty=D('.01'),source='bitget_v3_rest',raw_payload={**raw,'quantityMultiplier':None})
    batch=SimpleNamespace(tickers=[],instruments=[bad,good],selected_symbols=['BADUSDT','SOLUSDT'],candles_by_symbol={})
    view=build_market_view(batch,as_of=NOW)
    assert view.symbols[0].instrument is None and view.symbols[1].instrument.lot==D('.01')


def test_oi_polling_jitter_is_bounded_and_actual_duration_is_reported(monkeypatch):
    from strategies.sources import enrich_view
    view=setup()[1]
    def row(at,base):
        return dict(canonical_symbol='SOL-USDT-PERP',exchange='bitget',exchange_timestamp=at,
            fetched_at=at,processed_at=at,status='AVAILABLE',raw_unit='BASE_ASSET',source_endpoint='/api/v3/market/tickers',
            normalization_method='base_quantity_times_mark_price',raw_open_interest=D(base),
            open_interest_base=D(base),mark_price=D(100),open_interest_usd=D(base)*D(100))
    rows=[row(NOW-timedelta(seconds=901),'100'),row(NOW,'110')]
    monkeypatch.setattr('strategies.sources.read_rows',lambda conn,query,args: rows if 'FROM open_interest' in query else [])
    result=enrich_view(None,view,batch=SimpleNamespace(tickers=[],candles_by_symbol={}))
    fact=next(o for o in result.symbols[0].observations if o.kind=='OI' and o.source_ref.startswith('canonical:'))
    assert fact.usable and fact.value==D('.1') and fact.reason=='OI_REQUESTED_900S_ACTUAL_ELAPSED_901.0S'
    rows[0]=row(NOW-timedelta(seconds=931),'100')
    result=enrich_view(None,view,batch=SimpleNamespace(tickers=[],candles_by_symbol={}))
    assert not next(o for o in result.symbols[0].observations if o.kind=='OI' and o.source_ref.startswith('canonical:')).usable


@pytest.mark.parametrize('receipt_mode',['verified','absent','altered_row','altered_clock'])
def test_canonical_complete_flow_requires_bound_proof_and_actual_receipt_clock(monkeypatch,receipt_mode):
    from strategies.sources import enrich_view
    from quant_phase7.bitget_sbe import FlowBucket,SbeTrade
    start=NOW.replace(minute=NOW.minute//5*5,second=0,microsecond=0)
    checked=start+timedelta(seconds=331)
    bucket=FlowBucket(start,'BTCUSDT','SPOT',start-timedelta(seconds=1))
    bucket.add(SbeTrade('BTCUSDT','SPOT',123,start,start,D(100),D('.01'),'BUY',False),start)
    candle=[str(int(start.timestamp()*1000)),'100','100','100','100','.01','1']
    proof=bucket.proof(candle=candle,checked_at=checked)
    row={**bucket.row_values(),'window_open':start,'window_close':bucket.window_close,
        'status':'AVAILABLE','coverage_status':'COMPLETE','processed_at':checked}
    for field in ('base_volume','buy_volume','sell_volume','unknown_volume','delta'):row[field]=D(row[field])
    if receipt_mode=='altered_row':row['delta']=D(0)
    receipt=dict(symbol='BTCUSDT',metric='v2_sbe_spot_flow_proof',status='AVAILABLE',
        exchange_timestamp=bucket.window_close,fetched_at=checked,processed_at=checked,raw_payload=proof)
    if receipt_mode=='altered_clock':receipt['fetched_at']=checked-timedelta(seconds=1)
    def read(conn,query,args):
        if 'FROM phase7_spot_flow_windows' in query:return [row]
        if 'FROM market_observations' in query and receipt_mode!='absent':return [receipt]
        return []
    monkeypatch.setattr('strategies.sources.read_rows',read)
    original=setup()[1]
    view=original.model_copy(update={'as_of':checked,'symbols':(original.symbols[0].model_copy(update={'symbol':'BTCUSDT','observations':()}),)})
    result=enrich_view(None,view,batch=SimpleNamespace(tickers=[],candles_by_symbol={}))
    fact=next(o for o in result.symbols[0].observations if o.kind=='SPOT_FLOW')
    assert fact.usable==(receipt_mode=='verified')
    assert fact.coverage==('COMPLETE' if receipt_mode=='verified' else 'UNKNOWN')
    if receipt_mode=='verified':
        assert fact.value==1 and fact.source_event_time==bucket.window_close and fact.fetched_at==checked
