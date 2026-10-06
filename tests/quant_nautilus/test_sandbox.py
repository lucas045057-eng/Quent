from datetime import timedelta
from decimal import Decimal as D
import pytest

from quant_nautilus.acceptance import fixture_case
from quant_nautilus.sandbox import SandboxSession


@pytest.mark.parametrize('symbol,side', [('BTCUSDT','LONG'),('ETHUSDT','SHORT')])
def test_actual_sandbox_keeps_native_order_position_and_funding(tmp_path,symbol,side):
    case = fixture_case(tmp_path,symbol=symbol,side=side,mode='PAPER')
    with SandboxSession(case.instrument,case.intent) as session:
        session.quote(case.intent.created_at,case.intent.reference_price, D('100'))
        session.submit()
        session.quote(case.intent.created_at+timedelta(seconds=1),case.intent.reference_price,D('100'))
        before = session.snapshot()
        assert before.quantity == case.intent.approved_quantity
        assert before.side == side and before.protection_status == 'ACTIVE'
        session.quote(case.funding.boundary,case.intent.reference_price,D('100'))
        payment = session.fund(case.funding)
        after = session.snapshot()
        assert payment.cash != 0 and after.equity-before.equity == payment.cash
        assert after.funding_cash == payment.cash
        session.fund(case.funding)
        assert session.snapshot().equity == after.equity
        assert session.adapter.entry_order_count == 1
        assert session.native_order_states()[case.intent.client_order_id] == 'FILLED'


def test_sandbox_partial_ioc_native_remainder_cancel_keeps_protection(tmp_path):
    case=fixture_case(tmp_path,mode='PAPER')
    with SandboxSession(case.instrument,case.intent) as session:
        session.quote(case.intent.created_at,case.intent.reference_price,D('0.010'))
        session.submit()
        snapshot=session.snapshot()
        assert snapshot.quantity==D('0.010')
        assert snapshot.protection_status=='ACTIVE'
        assert session.native_order_states()[case.intent.client_order_id]=='CANCELED'
        assert any(r.status=='CANCELLED' and r.remaining_quantity==case.intent.approved_quantity-D('0.010')
            for r in session.adapter.results)


def test_position_equity_and_margin_follow_native_account_after_mark_change(tmp_path):
    from nautilus_trader.model.objects import Currency
    case = fixture_case(tmp_path, mode='PAPER')
    with SandboxSession(case.instrument, case.intent) as session:
        session.quote(case.intent.created_at, case.intent.reference_price, D('100'))
        session.submit()
        session.quote(case.intent.created_at+timedelta(seconds=1),
                      case.intent.reference_price+D('100'), D('100'))
        snapshot = session.snapshot()
        account = session.adapter.portfolio.account(case.instrument.id.venue)
        currency = Currency.from_str('USDT')
        cash = account.balance_total(currency).as_decimal()
        locked = account.balance_locked(currency).as_decimal()
        assert snapshot.unrealized_pnl > 0
        assert snapshot.equity == cash + snapshot.unrealized_pnl
        assert snapshot.margin == locked
        assert snapshot.available_balance == account.balance_free(currency).as_decimal()
