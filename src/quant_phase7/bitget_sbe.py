"""Collector-owned UTA SBE flow with closed-candle reconciliation.

Wire contract: Bitget sbe-intro XML schema 1/version 4, root 16, entry 40.
The different sbe-trade example layout is intentionally not accepted. Legacy
JSON side semantics remain UNKNOWN. Execution IDs need not be contiguous.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import hashlib
import json
import struct

WS_URL = 'wss://ws.bitget.com/v3/ws/public/sbe'
SOURCE = 'bitget:uta:sbe:xml-v4'
VERSION = 'bitget-sbe-candle-reconciled-v1'
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
SYMBOLS = ('BTCUSDT', 'ETHUSDT')
ROW_FIELDS = ('symbol','category','window_open','window_close','base_volume',
              'buy_volume','sell_volume','unknown_volume','delta','trade_count')

_SAFE_FAILURE_REASONS = frozenset({
    'SBE_FRAME_SIZE_INVALID', 'SBE_SCHEMA_NOT_VERIFIED', 'SBE_GROUP_OR_CATEGORY_INVALID',
    'SBE_EXPONENT_INVALID', 'SBE_SYMBOL_LENGTH_INVALID', 'SBE_SYMBOL_NOT_APPROVED',
    'SBE_TRADE_INVALID', 'SBE_TIMESTAMP_INVALID', 'SBE_SCOPE_MISMATCH',
    'SBE_WINDOW_MISMATCH', 'SBE_SUBSCRIPTION_ERROR', 'SBE_BEFORE_SUBSCRIPTION_ACK',
    'SBE_CLOCK_OUT_OF_BOUNDS', 'SBE_BUCKET_CAPACITY_EXCEEDED', 'SBE_DISCONNECTED',
    'SBE_CANDLE_HTTP_ERROR', 'SBE_CANDLE_SCHEMA_ERROR', 'SBE_CANDLE_BYTE_CAP_EXCEEDED',
})

def _failure_reason(exc):
    # Record only fixed contract codes, never arbitrary HTTP bodies or secrets.
    reason = str(exc)
    return reason if reason in _SAFE_FAILURE_REASONS else type(exc).__name__

def _text(value):
    if isinstance(value, datetime): return value.isoformat()
    if isinstance(value, D): return format(value.normalize(), 'f')
    return value

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=_text).encode()).hexdigest()

@dataclass(frozen=True)
class SbeTrade:
    symbol: str
    category: str
    exec_id: int
    event_time: datetime
    push_time: datetime
    price: D
    quantity: D
    side: str
    is_rpi: bool

def decode_trades(data: bytes, *, allowed_symbols=SYMBOLS) -> tuple[SbeTrade, ...]:
    if not isinstance(data, bytes) or not 28 <= len(data) <= 65536:
        raise ValueError('SBE_FRAME_SIZE_INVALID')
    if struct.unpack_from('<HHHH', data) != (16, 1003, 1, 4):
        raise ValueError('SBE_SCHEMA_NOT_VERIFIED')
    price_exp, size_exp, push, category = struct.unpack_from('<bbQB', data, 8)
    entry, count = struct.unpack_from('<HH', data, 24)
    if entry != 40 or not 1 <= count <= 1000 or category not in (0, 1):
        raise ValueError('SBE_GROUP_OR_CATEGORY_INVALID')
    if not -18 <= price_exp <= 0 or not -18 <= size_exp <= 0:
        raise ValueError('SBE_EXPONENT_INVALID')
    end = 28 + count * entry
    if len(data) <= end or not 1 <= data[end] <= 24 or len(data) != end + 1 + data[end]:
        raise ValueError('SBE_SYMBOL_LENGTH_INVALID')
    symbol = data[end+1:].decode('ascii')
    if symbol not in allowed_symbols: raise ValueError('SBE_SYMBOL_NOT_APPROVED')
    result = []
    for index in range(count):
        stamp, identity, price, size, side, rpi = struct.unpack_from('<QQqqBB', data, 28+index*entry)
        if not stamp or not push or not identity or price <= 0 or size <= 0 or side not in (0,1) or rpi not in (0,1):
            raise ValueError('SBE_TRADE_INVALID')
        try:
            event_time, push_time = EPOCH+timedelta(microseconds=stamp), EPOCH+timedelta(microseconds=push)
        except OverflowError: raise ValueError('SBE_TIMESTAMP_INVALID') from None
        result.append(SbeTrade(symbol, 'SPOT' if category == 0 else 'USDT-FUTURES', identity,
            event_time, push_time, D(price).scaleb(price_exp), D(size).scaleb(size_exp),
            'BUY' if side == 0 else 'SELL', bool(rpi)))
    return tuple(result)

@dataclass
class FlowBucket:
    window_open: datetime
    symbol: str
    category: str
    subscribed_at: datetime
    capacity: int = 20000
    buy: D = D(0)
    sell: D = D(0)
    quote: D = D(0)
    buy_count: int = 0
    sell_count: int = 0
    first: datetime | None = None
    last: datetime | None = None
    fault: str | None = None
    persisted: bool = False
    reconciliation_proof: dict | None = None
    seen: dict = field(default_factory=dict)
    trade_hash: object = field(default_factory=hashlib.sha256)

    @property
    def window_close(self): return self.window_open + timedelta(minutes=5)

    def add(self, trade, fetched_at):
        if (trade.symbol, trade.category) != (self.symbol, self.category): raise ValueError('SBE_SCOPE_MISMATCH')
        if not self.window_open <= trade.event_time < self.window_close: raise ValueError('SBE_WINDOW_MISMATCH')
        fingerprint = digest(trade.__dict__)
        if trade.exec_id in self.seen:
            if self.seen[trade.exec_id] != fingerprint:
                self.fault='DUPLICATE_ID_CONFLICT';self.persisted=False;self.reconciliation_proof=None
            return False
        if len(self.seen) >= self.capacity:
            self.fault='TRADE_CAPACITY_EXCEEDED';self.persisted=False;self.reconciliation_proof=None
            return False
        if fetched_at > self.window_close+timedelta(seconds=30) or trade.event_time > fetched_at+timedelta(seconds=5):
            self.fault='LATE_OR_FUTURE_EVENT';self.persisted=False;self.reconciliation_proof=None
        self.seen[trade.exec_id] = fingerprint
        self.trade_hash.update(fingerprint.encode())
        if trade.side == 'BUY': self.buy += trade.quantity;self.buy_count += 1
        else: self.sell += trade.quantity;self.sell_count += 1
        self.quote += trade.quantity*trade.price
        self.first = min(self.first or trade.event_time, trade.event_time)
        self.last = max(self.last or trade.event_time, trade.event_time)
        return True

    def row_values(self):
        return {k:_text(v) for k,v in dict(symbol=self.symbol,category=self.category,
            window_open=self.window_open,window_close=self.window_close,base_volume=self.buy+self.sell,
            buy_volume=self.buy,sell_volume=self.sell,unknown_volume=D(0),delta=self.buy-self.sell,
            trade_count=len(self.seen)).items()}

    def proof(self, *, candle, checked_at):
        reason=self.fault
        if self.subscribed_at > self.window_open: reason=reason or 'SUBSCRIBED_MIDWINDOW'
        if checked_at < self.window_close+timedelta(seconds=30): reason=reason or 'WINDOW_NOT_FINAL'
        candle_volume=None
        try:
            if not isinstance(candle,list) or len(candle)<7 or str(candle[0]) != str(int(self.window_open.timestamp()*1000)):
                raise ValueError()
            candle_volume=D(candle[5])
            if not candle_volume.is_finite() or candle_volume<=0 or candle_volume != self.buy+self.sell:
                reason=reason or 'CANDLE_VOLUME_MISMATCH'
        except (ValueError, ArithmeticError, TypeError):reason=reason or 'CANDLE_MISSING_OR_INVALID'
        if not self.seen: reason=reason or 'NO_TRADES'
        row=self.row_values()
        proof=dict(version=VERSION,source=SOURCE,coverage='COMPLETE' if reason is None else 'PARTIAL',
            reason=reason,row=row,row_digest=digest(row),trade_digest=self.trade_hash.hexdigest(),
            subscribed_at=self.subscribed_at.isoformat(),checked_at=checked_at.isoformat(),
            candle=candle,candle_digest=digest(candle),candle_base_volume=_text(candle_volume),
            candle_endpoint='/api/v3/market/candles',candle_category=self.category,
            candle_symbol=self.symbol,side_semantics='TAKER',continuity=reason is None)
        proof['proof_digest']=digest(proof)
        return proof

def validate_flow_proof(proof, row, *, now, scope=None):
    """Bind the persisted flow, source, category, clocks and exact-volume receipt."""
    try:
        if not isinstance(proof,dict) or proof['version']!=VERSION or proof['source']!=SOURCE:
            return False
        if proof['coverage']!='COMPLETE' or proof['reason'] is not None or proof['continuity'] is not True or proof['side_semantics']!='TAKER':
            return False
        if digest({k:v for k,v in proof.items() if k!='proof_digest'})!=proof['proof_digest']:
            return False
        normalized={k:_text(row[k]) for k in ROW_FIELDS}
        for k in ('base_volume','buy_volume','sell_volume','unknown_volume','delta'):
            normalized[k]=_text(D(normalized[k]))
        if normalized != proof['row'] or digest(normalized)!=proof['row_digest']:return False
        start=datetime.fromisoformat(normalized['window_open']);end=datetime.fromisoformat(normalized['window_close'])
        subscribed=datetime.fromisoformat(proof['subscribed_at']);checked=datetime.fromisoformat(proof['checked_at'])
        if any(t.tzinfo is None or t.utcoffset()!=timedelta(0) for t in (start,end,subscribed,checked,now)):return False
        if end-start!=timedelta(minutes=5) or subscribed>start or not end+timedelta(seconds=30)<=checked<=now:return False
        from .flow_scope import load_flow_scope
        approved=scope or load_flow_scope()
        if (normalized['category'],normalized['symbol']) not in approved.subscriptions:return False
        if proof['candle_symbol']!=normalized['symbol'] or proof['candle_category']!=normalized['category'] or proof['candle_endpoint']!='/api/v3/market/candles':return False
        candle=proof['candle'];total=D(normalized['base_volume']);buy=D(normalized['buy_volume']);sell=D(normalized['sell_volume'])
        if digest(candle)!=proof['candle_digest'] or str(candle[0])!=str(int(start.timestamp()*1000)):return False
        if not total>0 or buy<0 or sell<0 or buy+sell!=total or D(normalized['unknown_volume'])!=0:return False
        if D(normalized['delta'])!=buy-sell or D(candle[5])!=total or D(proof['candle_base_volume'])!=total:return False
        if normalized['trade_count']<=0 or len(proof['trade_digest'])!=64:return False
        return True
    except (KeyError, ValueError, TypeError, ArithmeticError, IndexError):return False

class BitgetSbeFlowWorker:
    """A bounded worker owned and stopped by the original Collector lifecycle."""
    def __init__(self, owner):
        from .flow_scope import load_flow_scope
        self.owner=owner
        self.scope=load_flow_scope(getattr(owner.settings,'bitget_sbe_flow_scope_path',None))
        self.window_capacity=max(20,len(self.scope.subscriptions)*4)
        self.pending_trades=0
        self._trim_at=None
        self._allowed_symbols=frozenset(self.scope.perpetual_symbols)
        self.total_trade_capacity=500000
        self.rejected_scopes=set()
        self._candle_lock=asyncio.Lock()
        self._next_candle=0.0
        self.buckets={}
        self.acks={}
        self.metrics=dict(frames=0,trades=0,duplicates=0,complete=0,partial=0,reconnects=0)

    def invalidate(self, reason, scopes=None):
        affected=set(self.scope.subscriptions if scopes is None else scopes)
        for scope in affected:self.acks.pop(scope,None)
        for key,bucket in self.buckets.items():
            if key[:2] in affected:
                bucket.fault=reason;bucket.persisted=False;bucket.reconciliation_proof=None

    async def run(self, stop_event):
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            tasks=[asyncio.create_task(self.reconcile(session,stop_event))]
            tasks.extend(asyncio.create_task(self.run_shard(session,stop_event,shard)) for shard in self.scope.shards())
            try:await asyncio.gather(*tasks)
            finally:
                for task in tasks:task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
                await self.health('NOT_AVAILABLE','STOPPED')

    async def run_shard(self,session,stop_event,shard):
        import aiohttp
        from quant_phase1.time import utc_now
        while not stop_event.is_set():
            active=tuple(scope for scope in shard if scope not in self.rejected_scopes)
            if not active:return
            try:
                async with session.ws_connect(WS_URL,timeout=10,max_msg_size=65536) as ws:
                    await ws.send_json({'op':'subscribe','args':[{'instType':{'SPOT':'spot','USDT-FUTURES':'usdt-futures'}[k],
                        'topic':'publicTrade','symbol':symbol} for k,symbol in active]})
                    last_ping=asyncio.get_running_loop().time()
                    while not stop_event.is_set():
                        if asyncio.get_running_loop().time()-last_ping>=20:
                            await ws.send_str('ping');last_ping=asyncio.get_running_loop().time()
                        try:message=await asyncio.wait_for(ws.receive(),10)
                        except asyncio.TimeoutError:await ws.send_str('ping');continue
                        now=utc_now()
                        if message.type==aiohttp.WSMsgType.TEXT:
                            if message.data=='pong':continue
                            payload=json.loads(message.data)
                            arg=payload.get('arg',{})
                            category={'spot':'SPOT','usdt-futures':'USDT-FUTURES'}.get(arg.get('instType'))
                            scope=(category,arg.get('symbol'))
                            if payload.get('event')=='error':
                                if scope in active:
                                    self.rejected_scopes.add(scope);self.invalidate('SBE_SUBSCRIPTION_REJECTED',(scope,))
                                    await self.health('ERROR','SBE_SUBSCRIPTION_REJECTED')
                                    continue
                                raise ValueError('SBE_SUBSCRIPTION_ERROR')
                            if payload.get('event')=='subscribe' and scope in active and arg.get('topic')=='publicTrade':
                                self.acks[scope]=now
                        elif message.type==aiohttp.WSMsgType.BINARY:
                            self.metrics['frames']+=1
                            for trade in decode_trades(message.data,allowed_symbols=self._allowed_symbols):
                                scope=(trade.category,trade.symbol)
                                if scope not in active or scope not in self.acks:raise ValueError('SBE_BEFORE_SUBSCRIPTION_ACK')
                                start=EPOCH+timedelta(seconds=int((trade.event_time-EPOCH).total_seconds())//300*300)
                                if not -5<=(now-trade.event_time).total_seconds()<=900:raise ValueError('SBE_CLOCK_OUT_OF_BOUNDS')
                                key=(*scope,start)
                                self.trim(now)
                                if key not in self.buckets and len(self.buckets)>=self.window_capacity:
                                    self.trim(now,force=True)
                                if key not in self.buckets and len(self.buckets)>=self.window_capacity:
                                    raise ValueError('SBE_BUCKET_CAPACITY_EXCEEDED')
                                bucket=self.buckets.setdefault(key,FlowBucket(start,trade.symbol,trade.category,self.acks[scope]))
                                if self.pending_trades>=self.total_trade_capacity and trade.exec_id not in bucket.seen:
                                    bucket.fault='TOTAL_TRADE_CAPACITY_EXCEEDED';bucket.persisted=False;bucket.reconciliation_proof=None;continue
                                accepted=bucket.add(trade,now)
                                self.pending_trades+=int(accepted)
                                self.metrics['trades' if accepted else 'duplicates']+=1
                        elif message.type in (aiohttp.WSMsgType.CLOSED,aiohttp.WSMsgType.CLOSE,aiohttp.WSMsgType.ERROR):
                            raise ValueError('SBE_DISCONNECTED')
            except asyncio.CancelledError:raise
            except Exception as exc:
                reason=_failure_reason(exc)
                self.invalidate(reason,shard)
                self.metrics['reconnects']+=1
                await self.health('ERROR',reason)
                try:await asyncio.wait_for(stop_event.wait(),max(1,self.owner.settings.ws_reconnect_seconds))
                except asyncio.TimeoutError:pass

    async def health(self,status,reason=None):
        from quant_phase1.contracts import DataStatus
        from quant_phase1.time import utc_now
        await self.owner._write_health('bitget_sbe_flow',DataStatus(status),utc_now(),{
            'runtime_state':'RUNNING' if status=='AVAILABLE' else 'DEGRADED','reason':reason,
            'source_contract':SOURCE,'pending_windows':len(self.buckets),
            'approved_subscriptions':len(self.scope.subscriptions),'subscriptions':len(self.acks),
            'socket_shards':len(self.scope.shards()),'rejected_subscriptions':len(self.rejected_scopes),**self.metrics})

    def diagnostics_snapshot(self):
        return {'phase7_bitget_sbe_'+key:value for key,value in {
            **self.metrics,'pending_windows':len(self.buckets),'subscriptions':len(self.acks),
            'pending_trades':sum(len(bucket.seen) for bucket in self.buckets.values()),
            'window_capacity':self.window_capacity,'trades_per_window_capacity':20000,
            'total_trade_capacity':self.total_trade_capacity,'approved_subscriptions':len(self.scope.subscriptions),
            'socket_shards':len(self.scope.shards()),'rejected_subscriptions':len(self.rejected_scopes)}.items()}

    async def fetch_candle(self,session,bucket):
        from quant_data_layer.admission import make_work_request,ReplayClass
        from quant_data_layer.observability import SourcePhase,SourceId,WorkClass,ProcessRole
        request=make_work_request(phase=SourcePhase.PHASE7,source_id=SourceId.PHASE7_SPOT,
            work_class=WorkClass.MEDIUM,estimated_items=5,estimated_bytes=65536,
            replay_class=ReplayClass.DERIVED_REPLACEABLE,cancellation_owner=ProcessRole.COLLECTOR,
            timeout_seconds=2)
        async with self.owner.admission.admit(request):
            async with self._candle_lock:
                loop=asyncio.get_running_loop()
                await asyncio.sleep(max(0,self._next_candle-loop.time()))
                self._next_candle=loop.time()+.125
            from quant_phase1.adapters.bitget_v3.rate_limit import acquire_public_candle_slot
            await acquire_public_candle_slot()
            params={'category':bucket.category,'symbol':bucket.symbol,'interval':'5m','limit':'5',
                'startTime':str(int(bucket.window_open.timestamp()*1000)-1),'endTime':str(int(bucket.window_close.timestamp()*1000))}
            async with session.get('https://api.bitget.com/api/v3/market/candles',params=params) as response:
                if response.status!=200:raise ValueError('SBE_CANDLE_HTTP_ERROR')
                payload=json.loads(await _bounded_response(response))
                if payload.get('code')!='00000' or not isinstance(payload.get('data'),list) or len(payload['data'])>5:
                    raise ValueError('SBE_CANDLE_SCHEMA_ERROR')
                matches=[r for r in payload['data'] if isinstance(r,list) and r and str(r[0])==str(int(bucket.window_open.timestamp()*1000))]
                return matches[0] if len(matches)==1 else None

    def trim(self,now,*,force=False):
        # Full-market trade ingestion must not scan every window per trade.
        if not force and self._trim_at is not None and now-self._trim_at<timedelta(seconds=1):return
        self._trim_at=now
        for key,bucket in tuple(self.buckets.items()):
            if bucket.persisted and now>=bucket.window_close+timedelta(minutes=10):
                self.pending_trades-=len(bucket.seen);self.buckets.pop(key,None)

    async def reconcile(self,session,stop_event):
        from quant_phase1.time import utc_now
        semaphore=asyncio.Semaphore(2)
        async def finalize(bucket):
            async with semaphore:
                if stop_event.is_set():return
                await self.finalize_bucket(session,bucket)
        while not stop_event.is_set():
            now=utc_now()
            pending=[b for b in self.buckets.values() if now>=b.window_close+timedelta(seconds=30) and not b.persisted]
            await asyncio.gather(*(finalize(b) for b in pending))
            self.trim(utc_now())
            try:await asyncio.wait_for(stop_event.wait(),2)
            except asyncio.TimeoutError:pass

    async def finalize_bucket(self,session,bucket):
        from quant_phase1.time import utc_now
        if bucket.persisted:return
        try:
            if bucket.reconciliation_proof is None:
                checked=utc_now();candle=None
                if checked>bucket.window_close+timedelta(seconds=300):
                    bucket.fault=bucket.fault or 'RECONCILIATION_DEADLINE_EXCEEDED'
                elif bucket.subscribed_at>bucket.window_open:
                    bucket.fault=bucket.fault or 'SUBSCRIBED_MIDWINDOW'
                elif bucket.fault is None:
                    try:candle=await self.fetch_candle(session,bucket)
                    except asyncio.CancelledError:raise
                    except Exception as exc:
                        # A failed source is an unavailable closed window, not
                        # a reason to repeatedly call it and retain trades forever.
                        bucket.fault=bucket.fault or 'CANDLE_FETCH_FAILED_'+_failure_reason(exc)
                checked=utc_now()
                bucket.reconciliation_proof=bucket.proof(candle=candle,checked_at=checked)
            proof=bucket.reconciliation_proof
            # On a DB failure retain this exact proof/receipt for the existing
            # persistence retry; never fetch or advance its original source clock.
            self.persist(bucket,proof,utc_now())
            bucket.persisted=True
            self.metrics['complete' if proof['coverage']=='COMPLETE' else 'partial']+=1
            await self.health('AVAILABLE' if proof['coverage']=='COMPLETE' else 'ERROR',proof['reason'])
        except asyncio.CancelledError:raise
        except Exception as exc:await self.health('ERROR',type(exc).__name__)

    def persist(self,bucket,proof,now):
        from quant_phase1.contracts import Observation,DataStatus
        from quant_phase1.repositories import Phase1Repository
        from quant_phase3.persistence import Phase3Repository
        from quant_phase3.flow import TradeFlowWindow
        from quant_phase3.contracts import FlowStatus
        from .spot_flow import SpotWindowResult
        from .contracts import DataStatus as SpotStatus,MarketKind
        complete=proof['coverage']=='COMPLETE';n=len(bucket.seen);total=bucket.buy+bucket.sell
        receipt_at=datetime.fromisoformat(proof['checked_at'])
        with self.owner._repository_scope() as repository:
            with self.owner._database_write_scope(repository,'bitget-sbe-flow') as admitted:
                connection=admitted.connection
                with connection.transaction():
                    if bucket.category=='SPOT':
                        row=SpotWindowResult(exchange='bitget',source_id=SOURCE,symbol=bucket.symbol,market_kind=MarketKind.SPOT,
                            timeframe='5m',window_open=bucket.window_open,window_close=bucket.window_close,aggregation_version=VERSION,
                            base_volume=total,quote_volume=bucket.quote,buy_volume=bucket.buy,sell_volume=bucket.sell,unknown_volume=D(0),
                            delta=bucket.buy-bucket.sell,cvd=None,trade_count=n,directional_trade_count=n,event_time_first=bucket.first,event_time_last=bucket.last,
                            cursor_first=str(min(bucket.seen)) if n else None,cursor_last=str(max(bucket.seen)) if n else None,
                            sample_count=n,source_count=1,available_count=n,missing_count=0,coverage_ratio=D(1) if complete else D(0),
                            status=SpotStatus.AVAILABLE if complete else SpotStatus.PARTIAL,reason=proof['reason'] or 'CANDLE_RECONCILED',
                            source_reference=proof['proof_digest'],normalization_version=VERSION).to_row(processed_at=now,created_at=now)
                        admitted.upsert_windows('phase7_spot_flow_windows',[row])
                    elif n:
                        Phase3Repository(connection).insert_flow_windows([TradeFlowWindow('bitget',bucket.symbol[:-4]+'-USDT-PERP','5m',
                            bucket.window_open,bucket.window_close,n,bucket.buy_count,bucket.sell_count,0,total,bucket.buy,bucket.sell,D(0),bucket.quote,
                            total/D(n),D(n)/D(300),bucket.buy-bucket.sell,(bucket.buy-bucket.sell)/total,
                            bucket.first,bucket.last,FlowStatus.AVAILABLE,FlowStatus.AVAILABLE if complete else FlowStatus.PARTIAL,
                            proof['reason'],now)])
                    Phase1Repository(connection).insert_market_observations('v2_sbe_'+('spot' if bucket.category=='SPOT' else 'perp')+'_flow_proof',[
                        Observation(symbol=bucket.symbol,value=D(1) if complete else None,unit='COVERAGE_ATTESTATION',source=SOURCE,
                            exchange='bitget',exchange_timestamp=bucket.window_close,fetched_at=receipt_at,processed_at=now,
                            status=DataStatus.AVAILABLE if complete else DataStatus.NOT_AVAILABLE,raw_payload=proof)])

async def _bounded_response(response):
    body=bytearray()
    async for part in response.content.iter_chunked(4096):
        body.extend(part)
        if len(body)>65536:raise ValueError('SBE_CANDLE_BYTE_CAP_EXCEEDED')
    return bytes(body)
