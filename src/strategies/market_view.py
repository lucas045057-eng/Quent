"""Canonical research view over the existing collector; no network or execution."""
from datetime import datetime, timedelta
from decimal import Decimal as D
from hashlib import sha256
import json
from pydantic import Field
from .contracts import Record, MarketObservation, AnalysisSnapshot

class InstrumentMetadata(Record):
    symbol: str
    market: str = "USDT-FUTURES"
    venue: str = "bitget"
    tick: D | None = Field(default=None, gt=0)
    lot: D | None = Field(default=None, gt=0)
    min_quantity: D | None = Field(default=None, ge=0)
    max_quantity: D | None = Field(default=None, gt=0)
    min_notional: D | None = Field(default=None, ge=0)
    settlement_currency: str
    source_ref: str
    source_digest: str

class StructureWindow(Record):
    timeframe: str
    trend: str = "UNKNOWN"
    support: D | None = None
    resistance: D | None = None
    atr: D | None = None
    volume_ratio: D | None = None
    as_of: datetime | None = None
    coverage: str = "UNKNOWN"

class SymbolMarket(Record):
    symbol: str
    price: D | None = None
    turnover: D | None = None
    spread_bps: D | None = None
    instrument: InstrumentMetadata | None = None
    windows: tuple[StructureWindow,...] = ()
    observations: tuple[MarketObservation,...] = ()

class MarketView(Record):
    as_of: datetime
    symbols: tuple[SymbolMarket,...]
    data_source: str = "REAL_PUBLIC_DATA"
    analysis_snapshots: tuple[AnalysisSnapshot,...] = ()

def observation_from_projection(*, symbol, kind, provider, payload, source_ref):
    # Receipt time cannot repair a missing source event or observation clock.
    names=set(MarketObservation.model_fields)-{"symbol","kind","provider","source_ref","schema_version"}
    fields={k:v for k,v in payload.items() if k in names}
    fields.setdefault("source_group", kind)
    return MarketObservation(symbol=symbol,kind=kind,provider=provider,source_ref=source_ref,**fields)

def structure_window(candles, *, timeframe, as_of):
    seconds={"5m":300,"15m":900,"1H":3600,"4H":14400}[timeframe]
    rows=sorted((c for c in candles if c.is_closed and c.status.value=="AVAILABLE"
        and c.bar_open_timestamp+timedelta(seconds=seconds)<=as_of),key=lambda c:c.bar_open_timestamp)
    if len(rows)<22:
        return StructureWindow(timeframe=timeframe)
    rows=rows[-22:]
    if any(b.bar_open_timestamp-a.bar_open_timestamp!=timedelta(seconds=seconds) for a,b in zip(rows,rows[1:])):
        return StructureWindow(timeframe=timeframe,coverage="PARTIAL")
    close_time=rows[-1].bar_open_timestamp+timedelta(seconds=seconds)
    if as_of-close_time>timedelta(seconds=seconds):
        return StructureWindow(timeframe=timeframe,as_of=close_time,coverage="STALE")
    ranges=[max(c.high-c.low,abs(c.high-p.close),abs(c.low-p.close)) for p,c in zip(rows,rows[1:])]
    recent=rows[-5:]; previous=rows[-10:-5]
    hh=max(c.high for c in recent)>max(c.high for c in previous)
    hl=min(c.low for c in recent)>min(c.low for c in previous)
    lh=max(c.high for c in recent)<max(c.high for c in previous)
    ll=min(c.low for c in recent)<min(c.low for c in previous)
    avg=sum(c.volume for c in rows[-21:-1])/D(20)
    return StructureWindow(timeframe=timeframe,trend="LONG" if hh and hl else "SHORT" if lh and ll else "RANGE",
        support=min(c.low for c in rows[:-1]),resistance=max(c.high for c in rows[:-1]),
        atr=sum(ranges[-14:])/D(14),volume_ratio=rows[-1].volume/avg if avg>0 else None,
        as_of=close_time,coverage="COMPLETE")

def build_market_view(batch, projections=(), *, as_of):
    by_symbol={t.symbol:t for t in batch.tickers}
    instruments={i.symbol:i for i in batch.instruments}
    result=[]
    for symbol in batch.selected_symbols:
        ticker=by_symbol.get(symbol); instrument=instruments.get(symbol)
        observations=[]; metadata=None
        if instrument and instrument.raw_payload.get("status")=="online" and instrument.raw_payload.get("category")=="USDT-FUTURES" and instrument.raw_payload.get("type")=="perpetual":
            raw=instrument.raw_payload
            digest=sha256(json.dumps(dict(raw),sort_keys=True,default=str).encode()).hexdigest()
            if all(raw.get(k) is not None for k in ('priceMultiplier','quantityMultiplier','maxMarketOrderQty','minOrderAmount')):
                try:
                    metadata=InstrumentMetadata(symbol=symbol,settlement_currency=instrument.quote_coin,
                        min_quantity=instrument.min_order_qty,max_quantity=raw.get("maxMarketOrderQty"),
                        tick=raw.get("priceMultiplier"),lot=raw.get("quantityMultiplier"),
                        min_notional=raw.get("minOrderAmount"),source_ref=instrument.source,source_digest=digest)
                except ValueError:
                    # One unavailable/unsupported limit cannot abort the universe.
                    metadata=None
        if ticker:
            fresh=0<=(as_of-ticker.exchange_timestamp).total_seconds()<=60
            observations.append(MarketObservation(symbol=symbol,kind="PRICE",provider=ticker.source,
                source_ref=f"ticker:{symbol}:{ticker.exchange_timestamp.isoformat()}",source_group="PRICE",
                value=ticker.last_price,unit="USDT",source_event_time=ticker.exchange_timestamp,
                observed_at=ticker.exchange_timestamp,fetched_at=ticker.fetched_at,processed_at=ticker.processed_at,
                availability="AVAILABLE",freshness="FRESH" if fresh else "STALE",quality="VALID",coverage="COMPLETE"))
        for projection in projections:
            if projection.symbol!=symbol: continue
            payload=dict(projection.canonical_payload)
            # A projection is a bundle, not automatically a scalar observation.
            for raw in payload.get("observations",()):
                observations.append(observation_from_projection(symbol=symbol,kind=raw["kind"],
                    provider=raw.get("provider",projection.source_type),payload=raw,source_ref=projection.source_ref))
        candles=batch.candles_by_symbol.get(symbol,{})
        result.append(SymbolMarket(symbol=symbol,price=ticker.last_price if ticker else None,
            turnover=ticker.turnover24h if ticker else None,
            spread_bps=(ticker.ask_price-ticker.bid_price)/ticker.last_price*D(10000)
                if ticker and ticker.last_price>0 and ticker.ask_price>=ticker.bid_price>0 else None,
            instrument=metadata,observations=tuple(observations),
            windows=tuple(structure_window(candles.get(tf,()),timeframe=tf,as_of=as_of) for tf in ("15m","1H","4H"))))
    return MarketView(as_of=as_of,symbols=tuple(result))
