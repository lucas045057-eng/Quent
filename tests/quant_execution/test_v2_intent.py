from decimal import Decimal as D
import pytest
from tests.quant_execution.test_v2_risk import inputs, updated
from tests.quant_execution.fixtures import risk_inputs
from quant_execution.risk import approve_intent
from quant_execution.contracts import intent_json,intent_from_json

def test_v2_intent_allows_configured_hold_and_leverage():
    config=updated(max_hold_seconds_by_horizon={"1_3H":20000,"3_8H":30000,"8_24H":40000})
    intent=approve_intent(**inputs(config))
    assert intent.max_hold_seconds==20000 and intent.max_leverage==D(2)
    assert intent.strategy_profile=="QUANT_PAPER_V2" and intent.horizon=="1_3H"
    assert intent_from_json(intent_json(intent))==intent

def test_historical_v1_intent_still_decodes():
    intent=approve_intent(**risk_inputs())
    assert intent_from_json(intent_json(intent))==intent
    assert "risk_config_digest" not in intent_json(intent)

def test_unsupported_capability_never_silently_downgrades_config():
    from quant_execution.trade_plan import validate_execution_capabilities
    with pytest.raises(ValueError,match="UNSUPPORTED_EXECUTION_CAPABILITY"):
        validate_execution_capabilities(updated(max_open_positions=2),frozenset({"MARKET_IOC"}))


@pytest.mark.parametrize('field',['allow_pyramiding','allow_averaging_down'])
def test_same_symbol_add_modes_require_native_protection_capability(field):
    from quant_execution.trade_plan import validate_execution_capabilities
    from quant_nautilus.realtime_paper_adapter import NautilusLocalPaperExecutionAdapter
    with pytest.raises(ValueError,match='UNSUPPORTED_EXECUTION_CAPABILITY'):
        validate_execution_capabilities(updated(**{field:True}),NautilusLocalPaperExecutionAdapter._CAPABILITIES)
