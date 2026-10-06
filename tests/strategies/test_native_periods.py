"""Synthetic native-period receipts, explicitly not a live service test."""
from datetime import datetime,timezone,timedelta
from decimal import Decimal as D
import json
from strategies.providers import ProviderReceipt,receipt_observation,public_json_transport
from strategies.native_periods import verified_period
import pytest
NOW=datetime(2026,10,4,tzinfo=timezone.utc)

def receipt():
    bindings={}
    for market,venue,period in (('BTCUSDT_PERP.A','binance',8),('BTCUSDT.6','bybit',240)):
        catalog=dict(symbol=market,symbol_on_exchange='BTCUSDT',base_asset='BTC',quote_asset='USDT',margined='STABLE',is_perpetual=True)
        endpoint='https://fapi.binance.com/fapi/v1/fundingInfo' if venue=='binance' else 'https://api.bybit.com/v5/market/instruments-info'
        payload=[dict(symbol='BTCUSDT',fundingIntervalHours=period)] if venue=='binance' else dict(retCode=0,result=dict(category='linear',list=[dict(symbol='BTCUSDT',contractType='LinearPerpetual',status='Trading',quoteCoin='USDT',settleCoin='USDT',fundingInterval=period)]))
        bindings[market]=dict(market=market,catalog_digest='a'*64,venue=venue,exchange_name=venue,catalog_row=catalog,endpoint=endpoint,
            parameters={} if venue=='binance' else {'category':'linear','symbol':'BTCUSDT'},payload=payload,fetched_at=NOW.isoformat())
    return ProviderReceipt(provider='coinalyze',symbol='BTCUSDT',kind='CROSS_FUNDING',endpoint='https://api.coinalyze.net/v1/funding-rate',
        fetched_at=NOW,availability='AVAILABLE',payload_json=json.dumps([dict(symbol=m,value='.01',update=int(NOW.timestamp()*1000)) for m in bindings]),
        market_symbols=tuple(bindings),market_catalog_digest='a'*64,source_scope='SELECTED_NON_BITGET_USDT_PERPETUALS',
        request_parameters_json=json.dumps({'symbols':','.join(bindings)}),funding_periods_json=json.dumps(bindings))

def test_native_four_and_eight_hour_rates_are_normalized_before_comparison():
    o=receipt_observation(receipt())
    assert o.value==D('.0002') and o.usable and o.quality=='VALID'
    assert '8H_RATE' in o.reason

@pytest.mark.parametrize('mutation',['missing','stale','symbol','host','period','catalog','venue','malformed','malformed_row'])
def test_incomplete_metadata_never_claims_verified_funding(mutation):
    r=receipt();b=json.loads(r.funding_periods_json);market=r.market_symbols[0]
    if mutation=='missing':b.pop(market)
    elif mutation=='stale':b[market]['fetched_at']=(NOW-timedelta(seconds=61)).isoformat()
    elif mutation=='symbol':b[market]['payload'][0]['symbol']='ETHUSDT'
    elif mutation=='host':b[market]['endpoint']='https://invalid.example/fundingInfo'
    elif mutation=='period':b[market]['payload'][0]['fundingIntervalHours']=0
    elif mutation=='catalog':b[market]['catalog_digest']='b'*64
    elif mutation=='venue':b[market]['exchange_name']='Bitget'
    elif mutation=='malformed_row':b[market]['payload']=[1]
    else:b=[]
    o=receipt_observation(r.model_copy(update={'funding_periods_json':json.dumps(b)}))
    assert not o.usable and o.quality=='PARTIAL'

def test_native_metadata_route_rejects_keys_and_unapproved_paths():
    with pytest.raises(ValueError):public_json_transport('https://fapi.binance.com/fapi/v1/fundingInfo',{}, {'api_key':'SYNTHETIC_FIXTURE'})
    with pytest.raises(ValueError):public_json_transport('https://fapi.binance.com/fapi/v1/order',{}, {})
