"""Canonical public research normalization. Missing attestations stay UNKNOWN."""
from datetime import timedelta
from decimal import Decimal as D
from .contracts import MarketObservation
from .refresh import MAX_AGE_SECONDS
from quant_phase9.sources import read_rows
from quant_phase9.canonical import canonical_sha256

def observation(symbol,kind,value,unit,rows,*,now,coverage='UNKNOWN',provider='bitget',group=None,clock_rows=None):
    clocks=rows if clock_rows is None else clock_rows
    observed=[r.get('exchange_timestamp') or r.get('window_close') or r.get('checked_at') for r in clocks]
    fetched=[r.get('fetched_at') or r.get('created_at') for r in clocks]
    processed=[r.get('processed_at') for r in clocks]
    good=bool(rows) and all(r.get('status')=='AVAILABLE' for r in rows)
    event=min(observed) if observed and all(observed) else None
    receipt=min(fetched) if fetched and all(fetched) else None
    fresh=event is not None and 0<=(now-event).total_seconds()<=MAX_AGE_SECONDS.get(kind,60)
    value=value if good else None
    digest=str(canonical_sha256(rows))
    return MarketObservation(symbol=symbol,kind=kind,provider=provider,source_group=group or kind,
        source_ref='canonical:'+kind+':'+digest,source_digest=digest,
        value=value,unit=unit if value is not None else None,observed_at=event,source_event_time=event,
        fetched_at=receipt,processed_at=max(processed) if processed and all(processed) else None,
        availability='AVAILABLE' if good else 'UNAVAILABLE',freshness='FRESH' if fresh else 'STALE' if event else 'UNKNOWN',
        quality='VALID' if good and value is not None else 'UNKNOWN',coverage=coverage,
        reason=None if good and coverage=='COMPLETE' else 'CANONICAL_SOURCE_OR_COVERAGE_INCOMPLETE')

def enrich_view(connection,view,*,batch):
    symbols=list(view_symbol.symbol for view_symbol in view.symbols)
    canonical=[s[:-4]+'-USDT-PERP' for s in symbols]
    now=view.as_of
    # One bounded bulk read per existing source, rather than 200 independent HTTP calls.
    oi=read_rows(connection,"""WITH latest AS (
        SELECT DISTINCT ON(canonical_symbol,source_endpoint,raw_unit) * FROM open_interest WHERE canonical_symbol=ANY(%s) AND exchange='bitget'
        AND source_endpoint IN ('/api/v3/market/open-interest','/api/v3/market/tickers')
        AND exchange_timestamp BETWEEN %s-interval '600 seconds' AND %s AND fetched_at<=%s AND processed_at<=%s
        ORDER BY canonical_symbol,source_endpoint,raw_unit,exchange_timestamp DESC,id DESC)
        SELECT * FROM latest UNION ALL SELECT baseline.* FROM latest newest CROSS JOIN LATERAL (
        SELECT old.* FROM open_interest old WHERE old.canonical_symbol=newest.canonical_symbol AND old.exchange='bitget'
        AND old.source_endpoint=newest.source_endpoint AND old.raw_unit=newest.raw_unit
        AND old.exchange_timestamp BETWEEN newest.exchange_timestamp-interval '930 seconds' AND newest.exchange_timestamp-interval '870 seconds'
        AND old.fetched_at<=%s AND old.processed_at<=%s
        ORDER BY abs(extract(epoch FROM (newest.exchange_timestamp-old.exchange_timestamp))-900),old.exchange_timestamp DESC,old.id DESC LIMIT 4) baseline""",(canonical,now,now,now,now,now,now))
    funding=read_rows(connection,"""SELECT DISTINCT ON(canonical_symbol) * FROM funding_rates WHERE canonical_symbol=ANY(%s) AND exchange='bitget'
        AND exchange_timestamp<=%s AND fetched_at<=%s AND processed_at<=%s ORDER BY canonical_symbol,exchange_timestamp DESC,id DESC""",(canonical,now,now,now))
    flows=read_rows(connection,"""SELECT DISTINCT ON(canonical_symbol) * FROM trade_flow_windows WHERE canonical_symbol=ANY(%s) AND exchange='bitget'
        AND timeframe='5m' AND window_close<=%s AND processed_at<=%s ORDER BY canonical_symbol,window_close DESC,id DESC""",(canonical,now,now))
    spots=read_rows(connection,"""SELECT DISTINCT ON(symbol) * FROM phase7_spot_flow_windows WHERE symbol=ANY(%s) AND exchange='bitget'
        AND timeframe='5m' AND window_close<=%s AND processed_at<=%s ORDER BY symbol,window_close DESC,window_id DESC""",(symbols,now,now))
    proofs=read_rows(connection,"""SELECT latest.* FROM unnest(%s::text[]) AS scope(symbol)
        CROSS JOIN (VALUES ('v2_sbe_spot_flow_proof'),('v2_sbe_perp_flow_proof')) AS kinds(metric)
        CROSS JOIN LATERAL (SELECT * FROM market_observations
        WHERE symbol=scope.symbol AND metric=kinds.metric AND source='bitget:uta:sbe:xml-v4'
        AND exchange_timestamp<=%s AND fetched_at<=%s AND processed_at<=%s
        ORDER BY exchange_timestamp DESC,id DESC LIMIT 1) AS latest""",(symbols,now,now,now))
    from quant_phase9.sources.phase2 import _valid_oi_normalization
    result=[]
    for m in view.symbols:
        key=m.symbol[:-4]+'-USDT-PERP'; facts=list(m.observations)
        history=sorted((r for r in oi if r['canonical_symbol']==key),key=lambda r:r['exchange_timestamp'])
        # Select a verified stream, never the newest unconfirmed fallback.
        # Both ends retain real clocks, freshness and exact source/unit scope.
        current=[r for r in history if r.get('status')=='AVAILABLE'
            and 0<=(now-r['exchange_timestamp']).total_seconds()<=MAX_AGE_SECONDS['OI']
            and _valid_oi_normalization(r)]
        current.sort(key=lambda r:(r.get('source_endpoint')=='/api/v3/market/open-interest',r['exchange_timestamp'],r.get('id',0)))
        endpoint=current[-1] if current else (history[-1] if history else None)
        eligible=[r for r in history if 870<=(endpoint['exchange_timestamp']-r['exchange_timestamp']).total_seconds()<=930
            and r.get('source_endpoint')==endpoint.get('source_endpoint') and r['raw_unit']==endpoint['raw_unit']
            and r.get('status')=='AVAILABLE' and _valid_oi_normalization(r)] if endpoint else []
        baseline=min(eligible,key=lambda r:abs((endpoint['exchange_timestamp']-r['exchange_timestamp']).total_seconds()-900)) if eligible else None
        rows=[baseline,endpoint] if baseline else ([endpoint] if endpoint else [])
        valid=baseline is not None and all(_valid_oi_normalization(r) for r in rows) and baseline['open_interest_base']>0 and baseline['raw_unit']==endpoint['raw_unit'] and baseline.get('source_endpoint')==endpoint.get('source_endpoint')
        change=endpoint['open_interest_base']/baseline['open_interest_base']-1 if valid else None
        # The digest binds both source endpoints; freshness and second-stage receipt
        # belong to the newest observation, not the historical baseline.
        oi_fact=observation(m.symbol,'OI',change,'CHANGE_RATIO',rows,now=now,coverage='COMPLETE' if valid else 'UNKNOWN',group='BITGET_OI',clock_rows=[endpoint] if endpoint else [])
        if not valid:
            reason=('OI_SOURCE_UNAVAILABLE' if not endpoint else
                'OI_UNIT_NOT_PROVIDED_BY_SOURCE' if endpoint.get('raw_unit')=='UNCONFIRMED' else
                'OI_15M_BASELINE_UNAVAILABLE' if not baseline else 'OI_SCOPE_UNIT_OR_NORMALIZATION_MISMATCH')
            oi_fact=oi_fact.model_copy(update={'reason':reason})
        if valid and endpoint['exchange_timestamp']-baseline['exchange_timestamp']!=timedelta(minutes=15):
            oi_fact=oi_fact.model_copy(update={'reason':'OI_REQUESTED_900S_ACTUAL_ELAPSED_'+str((endpoint['exchange_timestamp']-baseline['exchange_timestamp']).total_seconds())+'S'})
        facts.append(oi_fact)
        rows=[r for r in funding if r['canonical_symbol']==key]
        valid=len(rows)==1 and rows[0]['funding_interval_seconds'] and rows[0]['funding_rate'] is not None and rows[0]['normalized_8h_rate']==rows[0]['funding_rate']*D(28800)/D(rows[0]['funding_interval_seconds'])
        facts.append(observation(m.symbol,'FUNDING',rows[0]['normalized_8h_rate'] if valid else None,'RATE_RATIO',rows,now=now,coverage='COMPLETE' if valid else 'UNKNOWN',group='BITGET_FUNDING'))
        for kind,data in (('PERP_FLOW',[r for r in flows if r['canonical_symbol']==key]),('SPOT_FLOW',[r for r in spots if r['symbol']==m.symbol])):
            row=data[0] if len(data)==1 else {};volume=row.get('total_volume_base',row.get('base_volume'))
            delta=row.get('delta_base',row.get('delta'));unknown=row.get('unknown_volume_base',row.get('unknown_volume'))
            value=delta/volume if volume and volume>0 and delta is not None and unknown==0 else None
            # AVAILABLE/coverage_ratio=1 alone cannot prove a complete trade stream.
            from quant_phase7.bitget_sbe import validate_flow_proof
            receipt=next((p for p in proofs if p['symbol']==m.symbol and p['metric']=='v2_sbe_'+('spot' if kind=='SPOT_FLOW' else 'perp')+'_flow_proof'),None)
            bound=dict(symbol=m.symbol,category='SPOT' if kind=='SPOT_FLOW' else 'USDT-FUTURES',
                window_open=row.get('window_open'),window_close=row.get('window_close'),base_volume=volume,
                buy_volume=row.get('buy_volume_base',row.get('buy_volume')),sell_volume=row.get('sell_volume_base',row.get('sell_volume')),
                unknown_volume=unknown,delta=delta,trade_count=row.get('total_trade_count',row.get('trade_count')))
            proof=receipt.get('raw_payload') if receipt else None
            attested=bool(receipt and receipt['status']=='AVAILABLE' and receipt['exchange_timestamp']==row.get('window_close')
                and proof and proof.get('checked_at')==receipt['fetched_at'].isoformat()
                and validate_flow_proof(proof,bound,now=now))
            # The source receipt supplies actual fetch/process clocks; the digest
            # binds both the row and its closed-candle reconciliation proof.
            clocks=[dict(exchange_timestamp=row.get('window_close'),fetched_at=receipt['fetched_at'],processed_at=receipt['processed_at'])] if attested else None
            facts.append(observation(m.symbol,kind,value,'RATIO',data+([receipt] if receipt else []),now=now,coverage='COMPLETE' if attested else 'UNKNOWN',group='BITGET_'+kind,clock_rows=clocks))
        benchmark='ETHUSDT' if m.symbol=='BTCUSDT' else 'BTCUSDT'
        ticker=next((t for t in batch.tickers if t.symbol==benchmark),None)
        bars=[b for b in batch.candles_by_symbol.get(benchmark,{}).get('1H',()) if b.is_closed and b.status.value=='AVAILABLE' and b.bar_open_timestamp+timedelta(hours=1)<=now]
        last=max(bars,key=lambda b:b.bar_open_timestamp) if bars else None
        good=ticker is not None and ticker.status.value=='AVAILABLE' and last is not None and last.close>0 and 0<=(now-last.bar_open_timestamp-timedelta(hours=1)).total_seconds()<=3600
        raw=[dict(status='AVAILABLE',exchange_timestamp=ticker.exchange_timestamp,fetched_at=min(ticker.fetched_at,last.fetched_at),processed_at=max(ticker.processed_at,last.processed_at),benchmark=benchmark,close=last.close)] if good else []
        facts.append(observation(m.symbol,'BENCHMARK',ticker.last_price/last.close-1 if good else None,'RETURN_RATIO',raw,now=now,coverage='COMPLETE' if good else 'UNKNOWN',group='BENCHMARK_'+benchmark))
        # BREAKOUT_FORMING only receives retest facts when the same 15m
        # boundary determines a direction. Evidence is bound to its side and
        # timeframe and uses the original closed-candle clocks.
        from strategies.structures.breakout import breakout_direction
        from strategies.structures.retest import INTERVALS, retest_confirmation
        base_window=m.windows[0] if m.windows else None
        retest_side=breakout_direction(m.price,base_window) if base_window else None
        if retest_side:
            for timeframe,seconds in INTERVALS.items():
                bar_rows=batch.candles_by_symbol.get(m.symbol,{}).get(timeframe,())
                confirmed=retest_confirmation(bar_rows,retest_side,timeframe,as_of=now)
                eligible=sorted((bar for bar in bar_rows if bar.is_closed and bar.status.value=='AVAILABLE'
                    and bar.bar_open_timestamp+timedelta(seconds=seconds)<=now),key=lambda bar:bar.bar_open_timestamp)[-26:]
                if confirmed is None or not eligible:continue
                newest=eligible[-1];close_time=newest.bar_open_timestamp+timedelta(seconds=seconds)
                raw=[dict(opened=bar.bar_open_timestamp,open=bar.open,high=bar.high,low=bar.low,close=bar.close,
                    volume=bar.volume,turnover=bar.turnover,exchange_timestamp=bar.exchange_timestamp,
                    fetched_at=bar.fetched_at,processed_at=bar.processed_at,source=bar.source,exchange=bar.exchange)
                    for bar in eligible]
                digest=str(canonical_sha256(raw))
                fresh=0<=(now-close_time).total_seconds()<=seconds
                facts.append(MarketObservation(symbol=m.symbol,kind='RETEST:'+timeframe,provider='bitget',
                    source_group='RETEST_'+retest_side+'_'+timeframe,
                    source_ref='canonical:retest:'+m.symbol+':'+timeframe+':'+retest_side+':'+digest,
                    source_digest=digest,value=D(1 if confirmed else 0),unit='BOOLEAN',
                    observed_at=close_time,source_event_time=close_time,fetched_at=newest.fetched_at,
                    processed_at=newest.processed_at,availability='AVAILABLE',freshness='FRESH' if fresh else 'STALE',
                    quality='VALID',coverage='COMPLETE',reason='VERIFIED_CLOSED_CANDLE_RETEST' if confirmed else 'NO_QUALIFYING_RETEST_IN_COMPLETE_WINDOW'))
        for window in m.windows:
            if window.coverage!='COMPLETE' or window.as_of is None:continue
            bars=[b for b in batch.candles_by_symbol.get(m.symbol,{}).get(window.timeframe,()) if b.is_closed and b.status.value=='AVAILABLE' and b.bar_open_timestamp+timedelta(seconds={'15m':900,'1H':3600,'4H':14400}[window.timeframe])<=now]
            if not bars:continue
            newest=max(bars,key=lambda b:b.bar_open_timestamp)
            receipt=newest.fetched_at
            for kind,value,unit in (('PRICE_STRUCTURE',D(1) if window.trend=='LONG' else D(-1) if window.trend=='SHORT' else D(0),'DIRECTION'),('VOLUME',window.volume_ratio,'EXPANSION_RATIO'),('SUPPORT',window.support,'USDT'),('RESISTANCE',window.resistance,'USDT'),('ATR',window.atr,'USDT')):
                if value is None:continue
                facts.append(MarketObservation(symbol=m.symbol,kind=kind+':'+window.timeframe,provider='bitget',source_group='STRUCTURE_'+window.timeframe,
                    source_ref='canonical:bars:'+m.symbol+':'+window.timeframe+':'+window.as_of.isoformat(),value=value,unit=unit,
                    observed_at=window.as_of,source_event_time=window.as_of,fetched_at=receipt,processed_at=newest.processed_at,
                    availability='AVAILABLE',freshness='FRESH',quality='VALID',coverage='COMPLETE'))
        # External sources are requested freshly for A candidates; no global receipt is relabeled.
        result.append(m.model_copy(update={'observations':tuple(facts)}))
    return view.model_copy(update={'symbols':tuple(result)})
