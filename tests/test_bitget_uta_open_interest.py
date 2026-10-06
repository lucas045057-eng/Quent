"""Synthetic dedicated UTA OI scope and clock boundaries."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from decimal import Decimal as D
from dataclasses import asdict
import pytest
from quant_phase2.adapters.bitget import BitgetUTAAdapter
from quant_phase2.adapters.base import AdapterSchemaError
from quant_phase2.runtime import default_registry

NOW=datetime(2026,10,3,18,tzinfo=timezone.utc)

def payload(unit=None):
    row={'symbol':'BTCUSDT','openInterest':'42'}
    if unit:row['openInterestUnit']=unit
    return {'code':'00000','data':{'ts':str(int(NOW.timestamp()*1000)),'list':[row]}}

def test_native_unit_absence_is_preserved_even_with_mark_price():
    a=BitgetUTAAdapter(registry=default_registry())
    row=a.parse_open_interest(payload(),NOW,symbol='BTCUSDT')
    ticker=replace(row,source_endpoint=a.TICKERS,mark_price=D(60000),raw_unit='BASE_ASSET')
    bound=a.bind_oi_mark_price(row,ticker)
    assert bound==row and row.raw_unit=='UNCONFIRMED' and row.open_interest_base is None
    assert row.raw_payload['unit_contract']=='NOT_PROVIDED_BY_SOURCE'
    assert row.source_endpoint==a.OPEN_INTEREST and row.exchange_timestamp==NOW

def test_explicit_source_unit_binds_only_same_symbol_and_current_mark():
    a=BitgetUTAAdapter(registry=default_registry())
    row=a.parse_open_interest(payload('BASE_ASSET'),NOW,symbol='BTCUSDT')
    ticker=replace(row,source_endpoint=a.TICKERS,mark_price=D(60000))
    bound=a.bind_oi_mark_price(row,ticker)
    assert bound.open_interest_base==D(42) and bound.open_interest_usd==D(2520000)
    assert bound.normalization_method=='base_quantity_times_mark_price'
    assert a.bind_oi_mark_price(row,replace(ticker,symbol='ETHUSDT'))==row
    assert a.bind_oi_mark_price(row,replace(ticker,exchange_timestamp=NOW-timedelta(seconds=31)))==row
    assert a.bind_oi_mark_price(row,replace(ticker,exchange_timestamp=NOW+timedelta(seconds=1)))==row

@pytest.mark.parametrize('value',['NaN','Infinity','-1'])
def test_invalid_oi_value_is_rejected(value):
    data=payload();data['data']['list'][0]['openInterest']=value
    with pytest.raises(AdapterSchemaError):BitgetUTAAdapter().parse_open_interest(data,NOW,symbol='BTCUSDT')

def test_foreign_symbol_duplicate_scope_and_future_clock_are_rejected():
    a=BitgetUTAAdapter()
    with pytest.raises(AdapterSchemaError):a.parse_open_interest(payload(),NOW,symbol='ETHUSDT')
    data=payload();data['data']['list']*=2
    with pytest.raises(AdapterSchemaError):a.parse_open_interest(data,NOW,symbol='BTCUSDT')
    with pytest.raises(AdapterSchemaError):a.parse_open_interest(payload(),NOW-timedelta(seconds=1),symbol='BTCUSDT')

def test_user_confirmed_usdt_is_converted_and_honestly_labeled():
    from quant_phase2.unit_contracts import load_bitget_oi_contract,verified_user_unit
    from quant_phase9.sources.phase2 import _valid_oi_normalization
    a=BitgetUTAAdapter(registry=default_registry(),oi_unit_contract=load_bitget_oi_contract())
    row=a.parse_open_interest(payload(),NOW,symbol='BTCUSDT')
    ticker=replace(row,source_endpoint=a.TICKERS,mark_price=D(60000))
    bound=a.bind_oi_mark_price(row,ticker)
    assert bound.raw_unit=='QUOTE_NOTIONAL' and bound.open_interest_base==D(42)/D(60000)
    assert bound.open_interest_quote==bound.open_interest_usd==D(42)
    assert bound.normalization_method=='user_confirmed_quote_notional'
    assert bound.raw_payload['unit_contract']=='USER_CONFIRMED'
    assert verified_user_unit(asdict(bound)) and _valid_oi_normalization(asdict(bound))
    damaged=asdict(bound);damaged['raw_payload']={**damaged['raw_payload'],'unit_contract_proof':{'digest':'bad'}}
    assert not _valid_oi_normalization(damaged)
    with pytest.raises(AdapterSchemaError):a.parse_open_interest(payload('BASE_ASSET'),NOW,symbol='BTCUSDT')

def test_contract_does_not_cover_another_symbol_or_revive_old_mark():
    from quant_phase2.unit_contracts import load_bitget_oi_contract
    a=BitgetUTAAdapter(registry=default_registry(),oi_unit_contract=load_bitget_oi_contract())
    data=payload();data['data']['list'][0]['symbol']='NOTACOINUSDT'
    assert a.parse_open_interest(data,NOW,symbol='NOTACOINUSDT').raw_unit=='UNCONFIRMED'
    row=a.parse_open_interest(payload(),NOW,symbol='BTCUSDT')
    old=replace(row,source_endpoint=a.TICKERS,mark_price=D(60000),exchange_timestamp=NOW-timedelta(seconds=31))
    assert a.bind_oi_mark_price(row,old).open_interest_base is None

def test_default_user_confirmed_contract_covers_original_full_universe():
    import json
    from pathlib import Path
    from quant_phase2.unit_contracts import load_bitget_oi_contract
    contract=load_bitget_oi_contract()
    policy=json.loads((Path(__file__).resolve().parents[1]/'config/strategy_policy_v2_full_market.json').read_text())
    assert len(contract.symbols)==478
    assert set(contract.symbols)==set(policy['allowed_symbols'])
    assert {'BTCUSDT','ETHUSDT','SOLUSDT','ADAUSDT'} <= set(contract.symbols)
    assert contract.confirmed_on.isoformat()=='2026-10-06'
    assert '478' in contract.confirmation and 'USDT-FUTURES' in contract.confirmation

def test_full_universe_confirmation_normalizes_non_major_and_verifies_proof():
    from quant_phase2.unit_contracts import load_bitget_oi_contract,verified_user_unit
    from quant_phase9.sources.phase2 import _valid_oi_normalization
    a=BitgetUTAAdapter(registry=default_registry(),oi_unit_contract=load_bitget_oi_contract())
    data=payload();data['data']['list'][0]['symbol']='SOLUSDT'
    row=a.parse_open_interest(data,NOW,symbol='SOLUSDT')
    ticker=replace(row,source_endpoint=a.TICKERS,mark_price=D(150))
    bound=a.bind_oi_mark_price(row,ticker)
    assert bound.raw_unit=='QUOTE_NOTIONAL'
    assert bound.open_interest_quote==bound.open_interest_usd==D(42)
    assert verified_user_unit(asdict(bound)) and _valid_oi_normalization(asdict(bound))

def test_btc_eth_historical_unit_contract_remains_loadable():
    from pathlib import Path
    from quant_phase2.unit_contracts import load_bitget_oi_contract,verified_user_unit
    old_path=Path(__file__).resolve().parents[1]/'config/bitget_oi_unit_contract.json'
    contract=load_bitget_oi_contract(old_path)
    assert contract.symbols==('BTCUSDT','ETHUSDT')
    assert contract.confirmed_on.isoformat()=='2026-10-04'
    assert contract.digest=='7b541b64a9a54ff8c0ae6420d3e1f86df628d20708d803e0720aec0ade58b0d2'
    a=BitgetUTAAdapter(registry=default_registry(),oi_unit_contract=contract)
    row=a.parse_open_interest(payload(),NOW,symbol='BTCUSDT')
    bound=a.bind_oi_mark_price(row,replace(row,source_endpoint=a.TICKERS,mark_price=D(60000)))
    assert verified_user_unit(asdict(bound))
