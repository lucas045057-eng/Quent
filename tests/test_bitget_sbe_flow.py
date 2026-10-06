"""Synthetic contract tests; never evidence of live coverage."""
from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
import struct
import pytest
from quant_phase7.bitget_sbe import decode_trades, FlowBucket, validate_flow_proof

NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)

def frame(*, side=0, category=0, symbol='BTCUSDT', size=100, version=4):
    stamp = int(NOW.timestamp()*1000000)
    return (struct.pack('<HHHHbbQB5xHH',16,1003,1,version,-2,-4,stamp,category,40,1)
            +struct.pack('<QQqqBB6x',stamp,123,10000,size,side,0)
            +bytes([len(symbol)])+symbol.encode())

def test_schema_xml_decodes_taker_and_microsecond_clocks():
    buy=decode_trades(frame())[0]
    sell=decode_trades(frame(side=1,category=1))[0]
    assert buy.quantity == D('.01') and buy.price == D('100')
    assert buy.side == 'BUY' and sell.side == 'SELL'
    assert buy.event_time == NOW and sell.category == 'USDT-FUTURES'

@pytest.mark.parametrize('payload',[frame(side=2),frame(category=3),frame(version=5),frame(size=0),frame()[:-1],frame()+b'x'])
def test_unsupported_or_malformed_frame_rejected(payload):
    with pytest.raises(ValueError):decode_trades(payload)

def test_reconciliation_requires_full_session_positive_volume_and_exact_candle():
    trade=decode_trades(frame())[0]
    b=FlowBucket(NOW,'BTCUSDT','SPOT',NOW-timedelta(seconds=1))
    b.add(trade,NOW)
    assert b.add(trade,NOW) is False
    p=b.proof(candle=['1791072000000','1','1','1','1','.01','1'],checked_at=NOW+timedelta(seconds=331))
    assert p['coverage']=='COMPLETE'
    row=b.row_values()
    assert validate_flow_proof(p,row,now=NOW+timedelta(seconds=331))
    row['delta']='0'
    assert not validate_flow_proof(p,row,now=NOW+timedelta(seconds=331))
    assert b.proof(candle=['1791072000000','1','1','1','1','.02','1'],checked_at=NOW+timedelta(seconds=331))['coverage']=='PARTIAL'
    b.fault='DISCONNECTED'
    assert b.proof(candle=['1791072000000','1','1','1','1','.01','1'],checked_at=NOW+timedelta(seconds=331))['coverage']=='PARTIAL'

def test_midwindow_subscription_and_overflow_never_complete():
    b=FlowBucket(NOW,'BTCUSDT','SPOT',NOW+timedelta(seconds=1),capacity=1)
    b.add(decode_trades(frame())[0],NOW)
    p=b.proof(candle=['1791072000000','1','1','1','1','.01','1'],checked_at=NOW+timedelta(seconds=331))
    assert p['coverage']=='PARTIAL'
    b2=FlowBucket(NOW,'BTCUSDT','SPOT',NOW-timedelta(seconds=1),capacity=1)
    b2.add(decode_trades(frame())[0],NOW)
    from dataclasses import replace
    b2.add(replace(decode_trades(frame())[0],exec_id=124),NOW)
    assert b2.fault=='TRADE_CAPACITY_EXCEEDED'

def test_proof_cannot_relabel_category_symbol_or_window():
    b=FlowBucket(NOW,'BTCUSDT','SPOT',NOW-timedelta(seconds=1))
    b.add(decode_trades(frame())[0],NOW)
    p=b.proof(candle=['1791072000000','1','1','1','1','.01','1'],checked_at=NOW+timedelta(seconds=331))
    row=b.row_values();row['category']='USDT-FUTURES'
    assert not validate_flow_proof(p,row,now=NOW+timedelta(seconds=331))


@pytest.mark.asyncio
async def test_collector_starts_and_joins_sbe_worker_without_rpc_enabled(monkeypatch):
    import asyncio
    from quant_phase1.config import Settings
    from quant_phase1.entrypoints.collector import CollectorService
    collector=CollectorService(Settings.from_env({'BITGET_SBE_FLOW_ENABLED':'true','PHASE7_ENABLED':'false','TRADING_MODE':'paper'}))
    state={'started':False,'joined':False}
    async def worker(stop):
        state['started']=True
        try:await stop.wait()
        finally:state['joined']=True
    async def market():
        await asyncio.sleep(0)
        assert state['started']
        collector.stop_event.set()
    monkeypatch.setattr(collector.phase7_runtime,'run',worker)
    monkeypatch.setattr(collector,'_run_market_data',market)
    await collector.run()
    assert state['joined'] and collector.phase7_task is None


def test_sbe_owner_suppresses_only_legacy_bitget_trade_routes():
    from quant_phase1.config import Settings
    from quant_phase1.entrypoints.collector import CollectorService
    collector=CollectorService(Settings.from_env({'BITGET_SBE_FLOW_ENABLED':'true','PHASE3_ENABLED':'true','TRADING_MODE':'paper'}))
    collector.selected_symbols=('BTCUSDT','ETHUSDT')
    routes=collector.phase3_symbols_by_exchange()
    assert routes['bitget']==() and routes['bybit']


@pytest.mark.asyncio
async def test_closed_kline_reconciliation_runs_on_a_healthy_socket(monkeypatch):
    from quant_phase1.config import Settings
    from quant_phase1.entrypoints.collector import CollectorService
    collector=CollectorService(Settings.from_env({'TRADING_MODE':'paper'}))
    calls=[]
    async def recover(client):
        calls.append(client)
        collector.stop_event.set()
    monkeypatch.setattr(collector,'_recover_gaps',recover)
    await collector._kline_reconciliation_loop('SYNTHETIC_PUBLIC_CLIENT')
    assert calls==['SYNTHETIC_PUBLIC_CLIENT']


@pytest.mark.asyncio
async def test_candle_reconciliation_releases_original_admission_after_transport_failure():
    from quant_phase7.bitget_sbe import BitgetSbeFlowWorker
    from quant_phase7.runtime import Phase7CollectorRuntime
    from quant_phase1.config import Settings
    owner=Phase7CollectorRuntime(Settings.from_env({'BITGET_SBE_FLOW_ENABLED':'true','TRADING_MODE':'paper'}))
    worker=BitgetSbeFlowWorker(owner)
    class FailedSession:
        def get(self,*args,**kwargs):raise ValueError('SYNTHETIC_TRANSPORT_FAILURE')
    with pytest.raises(ValueError,match='SYNTHETIC_TRANSPORT_FAILURE'):
        await worker.fetch_candle(FailedSession(),FlowBucket(NOW,'BTCUSDT','SPOT',NOW))
    assert await owner.admission.shutdown(timeout_seconds=.1)
    owner._sbe_worker=worker
    worker.metrics['complete']=2
    assert owner.diagnostics_snapshot()['phase7_bitget_sbe_complete']==2
