"""Explicit operator-confirmed source units; never inferred or called official."""
from hashlib import sha256
from pathlib import Path
from datetime import date,datetime,timezone
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

class BitgetOIUnitContract(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    version: Literal['BITGET_OI_UNIT_CONTRACT_V1']='BITGET_OI_UNIT_CONTRACT_V1'
    provider: Literal['bitget']='bitget'
    endpoint: Literal['/api/v3/market/open-interest']='/api/v3/market/open-interest'
    category: Literal['USDT-FUTURES']='USDT-FUTURES'
    symbols: tuple[str,...]=Field(min_length=1,max_length=1000)
    reported_unit: Literal['USDT']='USDT'
    confirmation_basis: Literal['USER_CONFIRMED']='USER_CONFIRMED'
    confirmed_on: date
    confirmation: str=Field(min_length=1,max_length=500)

    @property
    def digest(self):
        return sha256(self.model_dump_json().encode()).hexdigest()

def load_bitget_oi_contract(path=None):
    selected=Path(path) if path else Path(__file__).resolve().parents[2]/'config/bitget_oi_unit_contract_full_market.json'
    if path or selected.is_file():
        if selected.is_symlink() or selected.stat().st_size>32768:raise ValueError('OI_UNIT_CONTRACT_INVALID')
        return BitgetOIUnitContract.model_validate_json(selected.read_text(encoding='utf-8-sig'))
    return None

def verified_user_unit(row):
    try:
        raw=row['raw_payload'];proof=raw['unit_contract_proof']
        contract=BitgetOIUnitContract.model_validate(proof['contract'])
        binding=raw['mark_price_binding']
        ticker_clock=datetime.fromisoformat(binding['exchange_timestamp'])
        ticker_fetch=datetime.fromisoformat(binding['fetched_at'])
        event=datetime.fromtimestamp(int(raw['data']['ts'])/1000,timezone.utc)
        admission=row['fetched_at']
        if event > admission:
            quarantine=raw['source_clock_admission']
            admission=datetime.fromisoformat(quarantine['admitted_at'])
            if not (quarantine['rule']=='WAIT_UNTIL_SOURCE_CLOCK_NOT_FUTURE_MAX_1_SECOND'
                and datetime.fromisoformat(quarantine['received_at'])==row['fetched_at']
                and datetime.fromisoformat(quarantine['source_event_at'])==event
                and admission==row['processed_at']
                and 0 < (event-row['fetched_at']).total_seconds() <= 1
                and event <= admission
                and 0 <= (admission-row['fetched_at']).total_seconds() <= 1.1):
                return False
        return (proof['digest']==contract.digest and row['symbol'] in contract.symbols
            and row['exchange']=='bitget' and row['source_endpoint']==contract.endpoint
            and row['raw_unit']=='QUOTE_NOTIONAL' and raw['category']==contract.category
            and raw['data']['list'][0]['symbol']==row['symbol']
            and Decimal(raw['data']['list'][0]['openInterest'])==row['raw_open_interest']
            and event==row['exchange_timestamp'] and raw['unit_contract']=='USER_CONFIRMED'
            and binding['endpoint']=='/api/v3/market/tickers'
            and Decimal(binding['mark_price'])==row['mark_price']
            and 0<=(admission-ticker_clock).total_seconds()<=30
            and 0<=(row['fetched_at']-ticker_fetch).total_seconds()<=30
            and 0<=(admission-event).total_seconds()<=30)
    except (KeyError,TypeError,ValueError,ArithmeticError):return False
