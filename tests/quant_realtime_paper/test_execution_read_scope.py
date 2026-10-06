"""Scope regression only; no synthetic order counts towards Goal."""
from types import SimpleNamespace
from pathlib import Path
import pytest
from quant_realtime_paper.config import RuntimeConfig
from quant_realtime_paper.assembly import _latest_market_batch

@pytest.mark.parametrize('symbols',[('SOLUSDT',),('SOLUSDT','BTCUSDT','ETHUSDT'),None])
def test_quote_read_is_explicit_and_preserves_original_candle_depth(tmp_path,monkeypatch,symbols):
    import psycopg
    from quant_realtime_paper import assembly
    calls=[]
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):return False
    def connect(dsn,**kwargs):
        assert dsn=='SYNTHETIC_TEST_DSN'
        assert kwargs['autocommit'] and 'default_transaction_read_only=on' in kwargs['options']
        return Connection()
    monkeypatch.setattr(psycopg,'connect',connect)
    monkeypatch.setattr(assembly.Phase1Repository,'load_latest_market_batch',
        lambda self,**kwargs:calls.append(kwargs) or 'batch')
    scope=('SOLUSDT','BTCUSDT','ETHUSDT')+tuple('COIN'+str(i)+'USDT' for i in range(475))
    cfg=RuntimeConfig('SYNTHETIC_TEST_DSN',tmp_path/'state',15,scope,tmp_path)
    assert _latest_market_batch(cfg,symbols=symbols)=='batch'
    assert calls==[{'limit':len(symbols) if symbols else 478,'candle_limit':100,'requested_symbols':symbols}]
    assert len(cfg.symbols)==478

@pytest.mark.parametrize('scope',[(),('UNAPPROVEDUSDT',)])
def test_invalid_execution_symbol_never_calls_database(tmp_path,monkeypatch,scope):
    import psycopg
    monkeypatch.setattr(psycopg,'connect',lambda *args,**kwargs:pytest.fail('must reject scope before read'))
    cfg=RuntimeConfig('SYNTHETIC',tmp_path/'state',15,('BTCUSDT',),tmp_path)
    with pytest.raises(ValueError,match='EXECUTION_READ_SCOPE_INVALID'):
        _latest_market_batch(cfg,symbols=scope)
