"""Public venue metadata supplements for Coinalyze's period-free rate schema."""
from datetime import datetime, timedelta
import json
from decimal import Decimal

PERIOD_ROUTES = {
    'binance': 'https://fapi.binance.com/fapi/v1/fundingInfo',
    'bybit': 'https://api.bybit.com/v5/market/instruments-info',
}

def verified_period(binding, *, market, catalog_digest, fetched_at):
    """Derive the period from a native field; never assume a default interval."""
    try:
        if not isinstance(binding,dict):return None
        if binding['market']!=market or binding['catalog_digest']!=catalog_digest:return None
        mapped=binding['catalog_row'];symbol=mapped['symbol_on_exchange']
        if mapped['symbol']!=market or mapped['is_perpetual'] is not True or mapped['quote_asset']!='USDT' or mapped['margined']!='STABLE':return None
        if symbol!=mapped['base_asset']+'USDT':return None
        venue=binding['venue'];payload=binding['payload'];endpoint=binding['endpoint']
        received=datetime.fromisoformat(binding['fetched_at'])
        if received.tzinfo is None or received.utcoffset()!=timedelta(0) or not 0 <= (fetched_at-received).total_seconds()<=60:return None
        if endpoint!=PERIOD_ROUTES.get(venue) or binding['exchange_name'].lower()!=venue:return None
        if venue=='binance':
            if binding['parameters']!={} or not isinstance(payload,list) or len(payload)>2000:return None
            rows=[r for r in payload if r.get('symbol')==symbol]
            if len(rows)!=1:return None
            duration=Decimal(str(rows[0]['fundingIntervalHours']))*3600
        elif venue=='bybit':
            if binding['parameters']!={'category':'linear','symbol':symbol} or payload['retCode']!=0:return None
            rows=payload['result']['list']
            if payload['result']['category']!='linear' or len(rows)!=1:return None
            row=rows[0]
            if row['symbol']!=symbol or row['contractType']!='LinearPerpetual' or row['status']!='Trading' or row['quoteCoin']!='USDT' or row['settleCoin']!='USDT':return None
            duration=Decimal(str(row['fundingInterval']))*60
        else:return None
        if not duration.is_finite() or duration not in (3600,7200,14400,21600,28800,43200,86400):return None
        return int(duration)
    except (KeyError,TypeError,ValueError,ArithmeticError,AttributeError):return None

def fetch_period_bindings(bindings, *, catalog_digest, transport, clock, deadline=None):
    result={}
    for market,metadata in bindings.items():
        venue=metadata['exchange_name'].lower()
        if venue not in PERIOD_ROUTES:continue
        symbol=metadata['catalog_row'].get('symbol_on_exchange')
        if not isinstance(symbol,str) or not symbol.isalnum() or len(symbol)>24:continue
        params={'category':'linear','symbol':symbol} if venue=='bybit' else {}
        timeout=5
        if deadline is not None:
            timeout=min(timeout,(deadline-clock()).total_seconds()-.1)
            if timeout<=0:continue
        try:
            payload=transport(PERIOD_ROUTES[venue],params,{},timeout_seconds=timeout)
            result[market]={'market':market,'catalog_digest':catalog_digest,'venue':venue,
                'exchange_name':metadata['exchange_name'],'catalog_row':metadata['catalog_row'],
                'endpoint':PERIOD_ROUTES[venue],'parameters':params,'payload':payload,'fetched_at':clock().isoformat()}
        except Exception:continue  # Missing metadata leaves the native rate PARTIAL.
    return json.dumps(result,sort_keys=True,separators=(',',':'))
