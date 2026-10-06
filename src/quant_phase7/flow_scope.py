"""Explicit source catalog scope; an absent spot listing never becomes flow."""
from functools import lru_cache
import os
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_SYMBOLS=('BTCUSDT','ETHUSDT')

class FlowScope(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    version: Literal['BITGET_FLOW_SCOPE_V1']='BITGET_FLOW_SCOPE_V1'
    catalog_checked_at: str
    perpetual_symbols: tuple[str,...]=Field(min_length=1,max_length=1000)
    spot_symbols: tuple[str,...]=Field(max_length=1000)

    @model_validator(mode='after')
    def valid_symbols(self):
        for symbols in (self.perpetual_symbols,self.spot_symbols):
            if len(set(symbols))!=len(symbols):raise ValueError('FLOW_SCOPE_DUPLICATE')
            if any(not s.isascii() or not 1<=len(s)<=24 or not s.isalnum()
                   or s!=s.upper() or not s.endswith('USDT') for s in symbols):
                raise ValueError('FLOW_SCOPE_UNSUPPORTED_WIRE_SYMBOL')
        if not set(self.spot_symbols)<=set(self.perpetual_symbols):
            raise ValueError('FLOW_SCOPE_SPOT_NOT_IN_UNIVERSE')
        return self

    @property
    def subscriptions(self):
        return tuple((category,symbol) for category,symbols in
            (('USDT-FUTURES',self.perpetual_symbols),('SPOT',self.spot_symbols)) for symbol in symbols)

    def shards(self,limit=40):
        if not 1<=limit<=40:raise ValueError('FLOW_SHARD_CAP_INVALID')
        topics=self.subscriptions
        return tuple(topics[i:i+limit] for i in range(0,len(topics),limit))

@lru_cache(maxsize=8)
def _load(path):
    selected=Path(path)
    if selected.is_symlink() or selected.stat().st_size>65536:
        raise ValueError('FLOW_SCOPE_FILE_INVALID')
    return FlowScope.model_validate_json(selected.read_text(encoding='utf-8-sig'))

def load_flow_scope(path=None):
    selected=path or os.environ.get('BITGET_SBE_FLOW_SCOPE_PATH')
    if selected:return _load(str(Path(selected).resolve()))
    return FlowScope(catalog_checked_at='LEGACY_EXPLICIT_SCOPE',
                     perpetual_symbols=DEFAULT_SYMBOLS,spot_symbols=DEFAULT_SYMBOLS)

def approved_sbe_spot_window(symbol, *, exchange, aggregation_version, normalization_version, source_reference):
    if (exchange!='bitget' or aggregation_version!='bitget-sbe-candle-reconciled-v1'
        or normalization_version!='bitget-sbe-candle-reconciled-v1'
        or not isinstance(source_reference,str) or len(source_reference)!=64
        or any(c not in '0123456789abcdef' for c in source_reference)):
        return False
    return symbol in load_flow_scope().spot_symbols
