from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from quant_nautilus.acceptance import fixture_case
from quant_nautilus.sandbox import SandboxSession

def test_two_positions_share_one_equity_and_exposure_budget(tmp_path):
    first=fixture_case(tmp_path/"one",mode="PAPER")
    second=fixture_case(tmp_path/"two",mode="PAPER",symbol="ETHUSDT",now=first.intent.created_at+timedelta(seconds=1))
    from quant_execution.contracts import intent_body,make_intent
    body=intent_body(second.intent);body["account_id"]=first.intent.account_id
    second_intent=make_intent(**body)
    with SandboxSession(first.instrument,first.intent) as session:
        session.quote(first.intent.created_at,first.intent.reference_price,D(100));session.submit()
        session.add_intent(second.instrument,second_intent)
        session.select_intent(second_intent.intent_id)
        session.quote(second_intent.created_at,second_intent.reference_price,D(100));session.submit()
        assert len(session.cache.positions_open())==2
        from nautilus_trader.model.objects import Currency
        account=session.portfolio.account(first.instrument.id.venue)
        assert account.balance_total(Currency.from_str("USDT")).as_decimal()<D(10000)
        assert account.balance_total(Currency.from_str("USDT")).as_decimal()>D(9900)
        assert len(session.adapters)==2 and session.execution is not None

def test_public_instrument_uses_actual_tick_and_lot():
    from strategies.market_view import InstrumentMetadata
    from quant_nautilus.instruments import build_public_instrument
    instrument=build_public_instrument(InstrumentMetadata(symbol="SOLUSDT",tick=".005",lot=".01",
        min_quantity=".1",max_quantity="1000",min_notional="5",settlement_currency="USDT",
        source_ref="bitget:instruments",source_digest="a"*64))
    assert instrument.price_increment.as_decimal()==D(".005")
    assert instrument.size_increment.as_decimal()==D(".01")
    assert str(instrument.id)=="SOLUSDT-PERP.BITGET_PAPER"


def test_public_native_instrument_binds_explicit_paper_fees():
    from strategies.market_view import InstrumentMetadata
    from quant_nautilus.instruments import build_public_instrument
    from quant_execution.paper_v1 import PaperCostsV1
    import pytest
    metadata=InstrumentMetadata(symbol='SOLUSDT',tick='.01',lot='.01',min_quantity='.01',
        max_quantity='1000',min_notional='5',settlement_currency='USDT',source_ref='bitget:public',source_digest='a'*64)
    costs=PaperCostsV1(D('.0006'),D('.0006'),D('.0002'))
    instrument=build_public_instrument(metadata,costs=costs)
    assert instrument.taker_fee==D('.0006') and instrument.maker_fee==D('.0006')
    with pytest.raises(ValueError,match='UNSUPPORTED_PAPER_FEE_SCHEDULE'):
        build_public_instrument(metadata,costs=PaperCostsV1(D('.0006'),D('.0004'),D('.0002')))
