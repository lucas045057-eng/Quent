from dataclasses import replace
from decimal import Decimal as D

import pytest
from nautilus_trader.test_kit.providers import TestInstrumentProvider

from quant_nautilus.adapter import NautilusIntentAdapter, run_intent_backtest
from quant_execution.risk import approve_intent
from quant_execution.contracts import make_intent, intent_body
from tests.quant_execution.fixtures import NOW, risk_inputs


def bound_intent(*, short=False):
    instrument = TestInstrumentProvider.btcusdt_perp_binance()
    inputs = risk_inputs()
    inputs['instrument'] = replace(inputs['instrument'], venue='BINANCE', instrument_id=str(instrument.id))
    inputs['account'] = replace(inputs['account'], venue='BINANCE', mode='BACKTEST')
    if short:
        inputs['envelope'] = replace(inputs['envelope'], candidate=replace(inputs['envelope'].candidate, direction_bias='BEARISH'))
        inputs['stop_price'] = D('50101')
    return instrument, approve_intent(**inputs)


@pytest.mark.parametrize('short', [False, True])
def test_real_pinned_order_flow_keeps_approved_quantity_and_protects_position(short):
    instrument, intent = bound_intent(short=short)
    result = run_intent_backtest(intent, instrument)
    filled = [event for event in result.results if event.status == 'FILLED']
    assert filled and filled[-1].filled_quantity == D('0.019')
    assert filled[-1].remaining_quantity == D('0')
    assert filled[-1].fees > 0
    assert filled[-1].intent_id == intent.intent_id
    assert all(event.requested_quantity == intent.approved_quantity for event in result.results)
    assert result.position.quantity == D('0.019')
    assert result.position.side == intent.side
    assert result.position.protection_status == 'ACTIVE'
    assert intent.client_order_id in result.position.source_order_ids
    assert result.stop_reduce_only is True


def test_adapter_rejects_changed_quantity_binding_and_unsupported_amend():
    instrument, intent = bound_intent()
    adapter = NautilusIntentAdapter(instrument, intent)
    body = intent_body(intent)
    body['instrument_id'] = 'ETHUSDT-PERP.BINANCE'
    with pytest.raises(ValueError, match='binding'):
        adapter.preflight(make_intent(**body), NOW)
    with pytest.raises(NotImplementedError):
        adapter.amend(intent.intent_id, quantity=D('0.020'))


def test_adapter_never_submits_an_intent_twice():
    instrument, intent = bound_intent()
    result = run_intent_backtest(intent, instrument, duplicate_submit=True)
    assert result.entry_order_count == 1
    assert result.position.quantity == D('0.019')


def test_real_ioc_partial_fill_retains_exact_protection_after_remainder_cancel():
    instrument, intent = bound_intent()
    result = run_intent_backtest(intent, instrument, quote_size=D('0.010'))
    assert result.position.quantity == D('0.010')
    assert result.position.protection_status == 'ACTIVE'
    assert any(event.status == 'CANCELLED' and event.filled_quantity == D('0.010')
        and event.remaining_quantity == D('0.009') for event in result.results)
