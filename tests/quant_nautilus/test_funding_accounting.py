from datetime import timedelta
from decimal import Decimal as D
import pytest

from quant_execution.funding import FundingObservationV1
from quant_nautilus.adapter import run_intent_backtest
from tests.quant_nautilus.test_adapter import bound_intent
from tests.quant_execution.fixtures import NOW


@pytest.mark.parametrize('short,rate,expected', [
    (False,'0.0001','-0.095'),(True,'0.0001','0.095'),
    (False,'-0.0001','0.095'),(True,'-0.0001','-0.095'),
])
def test_native_account_cash_changes_once_and_funding_is_separate_from_position_pnl(short,rate,expected):
    instrument,intent = bound_intent(short=short)
    boundary = NOW+timedelta(seconds=2)
    observation = FundingObservationV1(intent.canonical_symbol,boundary,D(rate),D('50000'),
        'USDT',boundary,boundary,'SETTLED','AVAILABLE','VALID','EXCHANGE_PUBLIC',
        'fixture:canonical-funding/1','FIXTURE_DRIVEN_ACCEPTANCE')
    baseline = run_intent_backtest(intent,instrument)
    funded = run_intent_backtest(intent,instrument,funding=(observation,observation))
    assert funded.position.equity-baseline.position.equity == D(expected)
    assert funded.position.funding_cash == D(expected)
    assert funded.position.realized_trade_pnl == baseline.position.realized_trade_pnl
    assert len(funded.funding_payments) == 1
    assert funded.funding_payments[0].signed_quantity == (-D('0.019') if short else D('0.019'))


def test_missing_rate_leaves_cost_unresolved_and_does_not_adjust_account():
    instrument,intent = bound_intent()
    boundary = NOW+timedelta(seconds=2)
    observation = FundingObservationV1(intent.canonical_symbol,boundary,None,D('50000'),
        'USDT',boundary,boundary,'SETTLED','NOT_AVAILABLE','UNKNOWN','EXCHANGE_PUBLIC',
        'fixture:canonical-funding/1','FIXTURE_DRIVEN_ACCEPTANCE')
    baseline = run_intent_backtest(intent,instrument)
    unresolved = run_intent_backtest(intent,instrument,funding=(observation,))
    assert unresolved.position.funding_cash is None
    assert unresolved.position.equity == baseline.position.equity
    assert len(unresolved.unresolved_funding) == 1


def test_closed_native_trade_reconciles_gross_fees_funding_drag_and_final_balance():
    instrument,intent = bound_intent()
    boundary = NOW+timedelta(seconds=2)
    observation = FundingObservationV1(intent.canonical_symbol,boundary,D('0.0001'),D('50000'),
        'USDT',boundary,boundary,'SETTLED','AVAILABLE','VALID','EXCHANGE_PUBLIC',
        'fixture:canonical-funding/1','FIXTURE_DRIVEN_ACCEPTANCE')
    result = run_intent_backtest(intent,instrument,funding=(observation,),close_at_final=True)
    assert result.position.side == 'FLAT'
    assert result.economics.resolved is True
    assert result.economics.fees > 0 and result.economics.spread_cost > 0
    assert result.economics.funding_cash == D('-0.095')
    assert result.economics.net_pnl < 0
    assert abs(result.position.equity-D('10000')-result.economics.net_pnl) <= D('0.00000001')
