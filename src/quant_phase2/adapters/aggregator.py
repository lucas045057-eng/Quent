"""Cross-source normalization; legacy CoinGlass decoder is historical only."""
from datetime import datetime, timezone
from decimal import Decimal as D
from strategies.contracts import MarketObservation

def normalize_cross_oi(payload, *, symbol, fetched_at):
    missing=MarketObservation(symbol=symbol,kind="CROSS_OI",provider="coinglass",
        source_ref="coinglass:aggregated-oi",fetched_at=fetched_at,reason="INSUFFICIENT_AGGREGATED_OI_HISTORY")
    if str(payload.get("code"))!="0":return missing
    rows=payload.get("data")
    if not isinstance(rows,list) or len(rows)<2:return missing
    try:
        rows=sorted(rows,key=lambda r:int(r["time"]))
        previous,current=rows[-2:]
        before,after=D(str(previous["close"])),D(str(current["close"]))
        observed=datetime.fromtimestamp(int(current["time"])/1000,timezone.utc)
        prior=datetime.fromtimestamp(int(previous["time"])/1000,timezone.utc)
        if not before.is_finite() or not after.is_finite() or before<=0 or after<0 or prior>=observed:
            return missing
        age=(fetched_at-observed).total_seconds()
        return MarketObservation(symbol=symbol,kind="CROSS_OI",provider="coinglass",
            source_ref="coinglass:aggregated-oi:"+str(current["time"]),source_group="CROSS_POSITIONING",
            value=(after-before)/before,unit="CHANGE_RATIO",source_event_time=observed,
            observed_at=observed,fetched_at=fetched_at,processed_at=fetched_at,
            availability="AVAILABLE",freshness="FRESH" if 0<=age<=3600 else "STALE",
            quality="VALID",coverage="COMPLETE")
    except (KeyError,TypeError,ValueError,ArithmeticError,OverflowError,OSError):
        return missing


import json
from datetime import timedelta

def _coinalyze_missing(receipt,reason):
    return MarketObservation(symbol=receipt.symbol,kind=receipt.kind,provider='coinalyze',
        source_ref=receipt.endpoint,source_digest=receipt.digest,fetched_at=receipt.fetched_at,
        source_group='COINALYZE_EXTERNAL_SUBSET',reason=reason)


def _coinalyze_rows(receipt):
    data=json.loads(receipt.payload_json)
    if (receipt.source_scope!='SELECTED_NON_BITGET_USDT_PERPETUALS' or not receipt.market_catalog_digest
        or len(receipt.market_symbols)<2 or len(set(receipt.market_symbols))!=len(receipt.market_symbols)
        or not isinstance(data,list) or len(data)!=len(receipt.market_symbols)):
        raise ValueError('scope')
    rows={r['symbol']:r for r in data}
    if set(rows)!=set(receipt.market_symbols):raise ValueError('markets')
    return rows


def normalize_coinalyze_cross_oi(receipt):
    try:
        rows=_coinalyze_rows(receipt);params=json.loads(receipt.request_parameters_json)
        if params.get('convert_to_usd')!='true' or params.get('interval')!='1hour':raise ValueError('unit/interval')
        if set(params['symbols'].split(','))!=set(rows):raise ValueError('query scope')
        histories={}
        for symbol,row in rows.items():
            history={}
            for bar in row['history']:
                opened=datetime.fromtimestamp(int(bar['t']),timezone.utc)
                closed=opened+timedelta(hours=1)
                value=D(str(bar['c']))
                if not value.is_finite() or value<0:raise ValueError('value')
                if opened in history:raise ValueError('duplicate bar')
                if closed<=receipt.fetched_at:history[opened]=value
            histories[symbol]=history
        common=sorted(set.intersection(*(set(h) for h in histories.values())))
        if len(common)<2:raise ValueError('history')
        previous,current=common[-2:]
        if current-previous!=timedelta(hours=1):raise ValueError('gap')
        before=sum(h[previous] for h in histories.values());after=sum(h[current] for h in histories.values())
        if before<=0:raise ValueError('baseline')
        observed=current+timedelta(hours=1);age=(receipt.fetched_at-observed).total_seconds()
        return MarketObservation(symbol=receipt.symbol,kind='CROSS_OI',provider='coinalyze',
            source_ref=receipt.endpoint+':'+receipt.digest,source_digest=receipt.digest,source_group='COINALYZE_EXTERNAL_SUBSET',
            value=after/before-1,unit='CHANGE_RATIO',source_event_time=observed,observed_at=observed,
            fetched_at=receipt.fetched_at,processed_at=receipt.fetched_at,availability='AVAILABLE',
            freshness='FRESH' if 0<=age<=3600 else 'STALE',quality='VALID',coverage='COMPLETE',
            reason='SELECTED_MARKET_SUBSET_NOT_WHOLE_MARKET')
    except (KeyError,TypeError,ValueError,ArithmeticError,OverflowError,OSError):
        return _coinalyze_missing(receipt,'COINALYZE_OI_SCOPE_UNIT_OR_HISTORY_INCOMPLETE')


def normalize_coinalyze_funding(receipt):
    try:
        rows=_coinalyze_rows(receipt)
        rates=[D(str(r['value']))/D(100) for r in rows.values()]  # official current-rate schema: percent
        if not all(r.is_finite() for r in rates):raise ValueError('rate')
        clocks=[datetime.fromtimestamp(int(r['update'])/1000,timezone.utc) for r in rows.values()]
        if any(t>receipt.fetched_at for t in clocks):raise ValueError('future funding clock')
        observed=min(clocks)
        age=(receipt.fetched_at-observed).total_seconds()
        from strategies.native_periods import verified_period
        bindings=json.loads(receipt.funding_periods_json) if receipt.funding_periods_json else {}
        if not isinstance(bindings,dict):bindings={}
        periods=[verified_period(bindings.get(market,{}),market=market,
            catalog_digest=receipt.market_catalog_digest,fetched_at=receipt.fetched_at) for market in rows]
        verified=all(periods)
        normalized=[rate*D(28800)/D(period) for rate,period in zip(rates,periods)] if verified else rates
        return MarketObservation(symbol=receipt.symbol,kind='CROSS_FUNDING',provider='coinalyze',
            source_ref=receipt.endpoint+':'+receipt.digest,source_digest=receipt.digest,source_group='COINALYZE_EXTERNAL_SUBSET',
            value=max(normalized,key=abs),unit='RATE_RATIO',source_event_time=observed,observed_at=observed,
            fetched_at=receipt.fetched_at,processed_at=receipt.fetched_at,availability='AVAILABLE',
            freshness='FRESH' if 0<=age<=3600 else 'STALE',quality='VALID' if verified else 'PARTIAL',coverage='COMPLETE',
            reason='SELECTED_MARKET_SUBSET_MAX_ABSOLUTE_8H_RATE_NOT_WEIGHTED_AVERAGE' if verified else 'FUNDING_PERIOD_NOT_VERIFIED;MAX_ABSOLUTE_NATIVE_RATE_NOT_8H_WEIGHTED_AVERAGE')
    except (KeyError,TypeError,ValueError,ArithmeticError,OverflowError,OSError):
        return _coinalyze_missing(receipt,'COINALYZE_FUNDING_SCOPE_UNIT_OR_CLOCK_INCOMPLETE')
