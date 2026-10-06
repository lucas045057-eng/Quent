"""OI repair: actual receipt clocks, source isolation and bounded batch scope."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from quant_phase2.adapters.base import AdapterSchemaError
from quant_phase2.adapters.bitget import BitgetUTAAdapter
from quant_phase2.runtime import default_registry
from quant_phase2.unit_contracts import load_bitget_oi_contract, verified_user_unit
from quant_phase9.sources.phase2 import _valid_oi_normalization

NOW=datetime(2026,10,4,10,tzinfo=timezone.utc)

def payload(at=NOW, symbols=('BTCUSDT',)):
    return {'code':'00000','data':{'ts':str(int(at.timestamp()*1000)),
        'list':[{'symbol':s,'openInterest':'420000'} for s in symbols]}}

def adapter():
    return BitgetUTAAdapter(registry=default_registry(),oi_unit_contract=load_bitget_oi_contract())

def test_small_future_clock_requires_real_admission_and_preserves_wire_receipt():
    a=adapter();event=NOW+timedelta(milliseconds=270)
    with pytest.raises(AdapterSchemaError):a.parse_open_interest(payload(event),NOW,symbol='BTCUSDT')
    row=a.parse_open_interest(payload(event),NOW,symbol='BTCUSDT',admitted_at=event)
    ticker=replace(row,source_endpoint=a.TICKERS,mark_price=D(60000),exchange_timestamp=NOW)
    bound=a.bind_oi_mark_price(row,ticker)
    assert bound.fetched_at==NOW and bound.exchange_timestamp==event and bound.processed_at==event
    assert verified_user_unit(asdict(bound)) and _valid_oi_normalization(asdict(bound))
    assert bound.raw_payload['source_clock_admission']['received_at']==NOW.isoformat()
    changed=asdict(bound);changed['processed_at']=NOW
    assert not verified_user_unit(changed)
    changed=asdict(bound);changed['raw_payload']={**changed['raw_payload']}
    del changed['raw_payload']['source_clock_admission']
    assert not verified_user_unit(changed)

@pytest.mark.parametrize('offset',[1.001,30,600])
def test_large_skew_does_not_become_valid_after_wait(offset):
    event=NOW+timedelta(seconds=offset)
    with pytest.raises(AdapterSchemaError):
        adapter().parse_open_interest(payload(event),NOW,symbol='BTCUSDT',admitted_at=event)

@pytest.mark.asyncio
async def test_async_clock_quarantine_waits_only_once_and_keeps_receipt(monkeypatch):
    import quant_phase2.adapters.bitget as module
    clock=[NOW];waits=[]
    class Clock:
        @staticmethod
        def now(_tz):return clock[0]
    async def sleep(seconds):
        waits.append(seconds);clock[0]+=timedelta(seconds=seconds)
    monkeypatch.setattr(module,'datetime',Clock)
    monkeypatch.setattr(module.asyncio,'sleep',sleep)
    event=NOW+timedelta(milliseconds=270)
    receipt,admitted=await adapter()._oi_receipt_clocks(payload(event))
    assert receipt==NOW and admitted>=event and waits==[.271]
    waits.clear()
    with pytest.raises(AdapterSchemaError):await adapter()._oi_receipt_clocks(payload(clock[0]+timedelta(seconds=2)))
    assert not waits

@pytest.mark.asyncio
async def test_batch_oi_is_one_request_and_rejects_duplicate_missing_foreign_scope(monkeypatch):
    a=adapter();calls=[]
    async def get(path,params):
        calls.append((path,params))
        return payload(symbols=('BTCUSDT','ETHUSDT','ETHUSDT','FOREIGNUSDT'))
    async def clocks(_payload):return NOW,NOW
    monkeypatch.setattr(a,'get_json',get);monkeypatch.setattr(a,'_oi_receipt_clocks',clocks)
    rows,errors=await a.fetch_open_interest_batch(('BTCUSDT','ETHUSDT','SOLUSDT'))
    assert calls==[(a.OPEN_INTEREST,{'category':'USDT-FUTURES'})]
    assert [r.symbol for r in rows]==['BTCUSDT']
    assert len(errors)==2 and all('SCOPE_MISSING_OR_DUPLICATE' in e for e in errors)
    assert rows[0].raw_payload['data']['list']==[{'symbol':'BTCUSDT','openInterest':'420000'}]

@pytest.mark.asyncio
async def test_batch_funding_isolated_symbol_failures(monkeypatch):
    a=adapter();calls=[]
    async def get(path,params):
        calls.append(path)
        return {'code':'00000','data':[{'symbol':'BTCUSDT','fundingRate':'.0001','fundingRateInterval':'8'},
            {'symbol':'ETHUSDT','fundingRate':'bad','fundingRateInterval':'8'}]}
    monkeypatch.setattr(a,'get_json',get)
    rows,errors=await a.fetch_current_funding_batch(('BTCUSDT','ETHUSDT','MISSINGUSDT'))
    assert calls==[a.CURRENT_FUNDING] and [r.symbol for r in rows]==['BTCUSDT'] and len(errors)==2

@pytest.mark.asyncio
async def test_batch_ticker_filters_foreign_and_isolates_invalid_symbols(monkeypatch):
    a=adapter();calls=[]
    async def get(path,params):
        calls.append(path)
        return {'code':'00000','data':[{'symbol':'BTCUSDT','ts':str(int(NOW.timestamp()*1000)),
            'openInterest':'100','markPrice':'60000','fundingRate':'.0001'},
            {'symbol':'ETHUSDT','openInterest':'bad'}, {'symbol':'FOREIGNUSDT','openInterest':'bad'}]}
    monkeypatch.setattr(a,'get_json',get)
    rows,rates,errors=await a.fetch_tickers_batch(('BTCUSDT','ETHUSDT','MISSINGUSDT'))
    assert calls==[a.TICKERS] and [r.symbol for r in rows]==['BTCUSDT']
    assert len(rates)==1 and len(errors)==2 and rows[0].raw_unit=='UNCONFIRMED'

@pytest.mark.asyncio
async def test_runtime_retains_dedicated_oi_when_funding_and_catalog_fail(monkeypatch):
    import quant_phase2.runtime as module
    from quant_phase1.config import Settings
    a=adapter();dedicated=a.parse_open_interest(payload(),NOW,symbol='BTCUSDT')
    ticker=replace(dedicated,source_endpoint=a.TICKERS,mark_price=D(60000),raw_unit='UNCONFIRMED')
    calls=[]
    class Bitget:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def fetch_instruments(self,now):raise ValueError('catalog unavailable')
        async def fetch_tickers_batch(self,symbols):calls.append('ticker');return [ticker],[],[]
        async def fetch_open_interest_batch(self,symbols):calls.append('oi');return [dedicated],[]
        bind_oi_mark_price=staticmethod(BitgetUTAAdapter.bind_oi_mark_price)
        async def fetch_current_funding_batch(self,symbols):calls.append('funding');raise TimeoutError()
    class Other:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def fetch_instruments(self,now):return []
        async def fetch_meta_and_contexts(self,now):return [],[],[]
    monkeypatch.setattr(module,'BitgetUTAAdapter',Bitget)
    monkeypatch.setattr(module,'BybitV5Adapter',Other);monkeypatch.setattr(module,'HyperliquidAdapter',Other)
    monkeypatch.setattr(module,'utc_now',lambda:NOW)
    runtime=module.Phase2DerivativeRuntime(Settings.from_env({'TRADING_MODE':'paper'}))
    _,rows,_,errors=await runtime._fetch(NOW,('BTCUSDT',))
    assert calls==['ticker','oi','funding'] and len(rows)==1
    assert rows[0].source_endpoint==a.OPEN_INTEREST and _valid_oi_normalization(asdict(rows[0]))
    assert any('funding:TimeoutError' in e for e in errors) and any('instruments:ValueError' in e for e in errors)

def oi_row(at,base,endpoint='/api/v3/market/open-interest',unit='BASE_ASSET'):
    return dict(canonical_symbol='SOL-USDT-PERP',exchange='bitget',symbol='SOLUSDT',
        exchange_timestamp=at,fetched_at=at,processed_at=at,status='AVAILABLE',raw_unit=unit,
        source_endpoint=endpoint,normalization_method='base_quantity_times_mark_price' if unit=='BASE_ASSET' else 'UNCONFIRMED_UNIT',
        raw_open_interest=D(base),open_interest_base=D(base) if unit=='BASE_ASSET' else None,
        mark_price=D(100),open_interest_usd=D(base)*100 if unit=='BASE_ASSET' else None)

def fact_for(monkeypatch,rows):
    from tests.strategies.test_execution_policy import setup
    from strategies.sources import enrich_view
    view=setup()[1]
    view=view.model_copy(update={'as_of':NOW})
    monkeypatch.setattr('strategies.sources.read_rows',lambda conn,query,args:rows if 'FROM open_interest' in query else [])
    result=enrich_view(None,view,batch=SimpleNamespace(tickers=[],candles_by_symbol={}))
    return next(o for o in result.symbols[0].observations if o.kind=='OI' and o.source_ref.startswith('canonical:'))

def test_newer_unknown_ticker_cannot_replace_valid_dedicated_stream(monkeypatch):
    last=NOW-timedelta(seconds=1)
    rows=[oi_row(last-timedelta(minutes=15),'100'),oi_row(last,'110'),
        oi_row(NOW,'999','/api/v3/market/tickers','UNCONFIRMED')]
    fact=fact_for(monkeypatch,rows)
    assert fact.usable and fact.value==D('.1') and fact.observed_at==last

def test_baseline_selects_valid_same_stream_instead_of_closer_unknown(monkeypatch):
    rows=[oi_row(NOW-timedelta(seconds=901),'100'),oi_row(NOW,'110'),
        oi_row(NOW-timedelta(seconds=900),'999','/api/v3/market/tickers','UNCONFIRMED')]
    fact=fact_for(monkeypatch,rows)
    assert fact.usable and fact.value==D('.1') and '901.0S' in fact.reason

@pytest.mark.parametrize('mode',['stale','wrong_source','wrong_unit','outside_window','zero_base','invalid_base'])
def test_source_or_baseline_failure_still_blocks(monkeypatch,mode):
    latest=oi_row(NOW,'110');base=oi_row(NOW-timedelta(seconds=900),'100')
    if mode=='stale':
        latest['exchange_timestamp']=NOW-timedelta(seconds=601)
        base['exchange_timestamp']=latest['exchange_timestamp']-timedelta(seconds=900)
    if mode=='wrong_source':base['source_endpoint']='/api/v3/market/tickers'
    if mode=='wrong_unit':base['raw_unit']='UNCONFIRMED'
    if mode=='outside_window':base['exchange_timestamp']=NOW-timedelta(seconds=931)
    if mode=='zero_base':base=oi_row(NOW-timedelta(seconds=900),'0')
    if mode=='invalid_base':base['open_interest_base']=D(999)
    assert not fact_for(monkeypatch,[base,latest]).usable
