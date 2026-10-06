from decimal import Decimal as D
import pytest
from quant_execution.costs import cost_report


def test_fill_based_pnl_does_not_subtract_spread_and_slippage_twice():
    report = cost_report(gross_mid_pnl=D('100'),trading_pnl=D('97'),fees=D('1'),
        funding_cash=D('-2'),spread_cost=D('2'),slippage_cost=D('1'))
    assert report.net_pnl == D('94')
    assert report.gross_mid_pnl-report.spread_cost-report.slippage_cost == report.trading_pnl
    assert report.resolved is True


def test_unmodelled_cost_is_null_instead_of_zero():
    report = cost_report(gross_mid_pnl=D('100'),trading_pnl=D('97'),fees=D('1'),
        funding_cash=None,spread_cost=D('2'),slippage_cost=D('1'))
    assert report.net_pnl is None
    assert report.unresolved == ('FUNDING',)


def test_inconsistent_execution_attribution_fails_closed():
    with pytest.raises(ValueError,match='reconciliation'):
        cost_report(gross_mid_pnl=D('100'),trading_pnl=D('95'),fees=D('1'),
            funding_cash=D('-2'),spread_cost=D('2'),slippage_cost=D('1'))
