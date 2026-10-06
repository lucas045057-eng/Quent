from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import pytest
from tests.quant_execution.fixtures import risk_inputs
from tests.strategies.test_execution_policy import setup, NOW
from quant_execution.risk import approve_intent, RiskRejected
from quant_execution.risk_config import RiskConfigV2, resolve_risk_policy
from quant_execution.contracts import DecisionEnvelopeV1, intent_from_json, intent_json
from quant_execution.paper_v1 import PaperCostsV1

def inputs(config=None, costs=None):
    from quant_execution.trade_plan import build_trade_plan
    config=config or RiskConfigV2(max_slippage_bps="0")
    thesis=setup()[0][0].model_copy(update={"invalidation_price":D(99),"target_price":D(110)})
    data=risk_inputs(NOW)
    instrument=replace(data["instrument"], core_symbol="SOLUSDT",canonical_symbol="SOL-USDT-PERP",
        instrument_id="SOLUSDT-PERP.LOCAL",price_tick=D(".01"),quantity_step=D(".01"),max_quantity=D(1000))
    quote=replace(data["quote"],canonical_symbol=instrument.canonical_symbol,bid=D(100),ask=D(100))
    candidate=replace(data["envelope"].candidate,symbol="SOLUSDT",timeframe="15m",matched_pattern="MARKET_EVIDENCE_CHAIN",
        decision_policy_version="2.0.0",pattern_policy_version="2.0.0",prompt_version="2.0.0",freshness_policy_version="2.0.0",ttl_policy_version="2.0.0")
    plan=build_trade_plan(thesis,instrument,quote,costs=costs or PaperCostsV1(D(0),D(0),D(0)),config=config,now=NOW)
    envelope=DecisionEnvelopeV1(candidate,"ACTIVE","d"*64,"e"*64,
        thesis_digest=thesis.digest,analysis_snapshot_digest=thesis.snapshot_digest,risk_config_digest=config.digest)
    return dict(envelope=envelope,instrument=instrument,quote=quote,account=data["account"],
        policy=resolve_risk_policy(config,data["account"],code_version="b"*40,decision_versions=("2.0.0",)),
        stop_price=plan.stop_price,now=NOW,trade_plan=plan)

def updated(**changes):
    return RiskConfigV2(risk_per_trade_equity_ratio=".005",max_reserved_risk_equity_ratio=".005",
        max_position_notional_equity_ratio=".20",max_total_exposure_equity_ratio=".20",max_leverage="2",
        max_slippage_bps="0",**changes)

def test_config_changes_actual_approved_quantity():
    assert approve_intent(**inputs()).approved_quantity==D(10)
    assert approve_intent(**inputs(updated())).approved_quantity==D(20)

def test_fixed_notional_mode_still_obeys_stop_risk():
    config=updated(sizing_mode="FIXED_NOTIONAL_RATIO",fixed_notional_equity_ratio=".08")
    assert approve_intent(**inputs(config)).approved_quantity==D(8)
    data=inputs(config);data["policy"]=replace(data["policy"],max_risk=D(2))
    assert approve_intent(**data).approved_quantity==D(2)

def test_new_policy_does_not_mutate_existing_intent():
    intent=approve_intent(**inputs());serialized=intent_json(intent)
    approve_intent(**inputs(updated()))
    assert intent_from_json(serialized)==intent and intent.risk_config_digest==RiskConfigV2(max_slippage_bps="0").digest

def test_quote_hard_freshness_survives_risk_edits():
    data=inputs(updated(quote_max_age_seconds=30));data["quote"]=replace(data["quote"],as_of=NOW-timedelta(seconds=6))
    data["trade_plan"]=data["trade_plan"].model_copy(update={"quote_as_of":data["quote"].as_of})
    with pytest.raises(RiskRejected,match="STALE_QUOTE"):approve_intent(**data)

def test_v2_candidate_cannot_bypass_required_plan_or_binding():
    data=inputs();data["trade_plan"]=None
    with pytest.raises(RiskRejected,match="V2_PLAN_REQUIRED"):approve_intent(**data)
    data=inputs();data["envelope"]=replace(data["envelope"],thesis_digest="f"*64)
    with pytest.raises(RiskRejected,match="V2_PLAN_BINDING"):approve_intent(**data)

def test_costs_reduce_actual_quantity_and_reward():
    base=inputs();costly=inputs(costs=PaperCostsV1(D(".01"),D(".01"),D(".001")))
    assert costly["trade_plan"].net_R<base["trade_plan"].net_R
    assert approve_intent(**costly).approved_quantity<approve_intent(**base).approved_quantity


@pytest.mark.parametrize('changes,quantity',[
    ({'max_risk_amount':'2'},D('2')),
    ({'max_position_notional_amount':'500'},D('5')),
    ({'max_total_exposure_amount':'600'},D('6')),
    ({'max_margin_amount':'200'},D('2')),
    ({'max_position_notional_equity_ratio':'.05','max_total_exposure_equity_ratio':'.05'},D('5')),
    ({'risk_per_trade_equity_ratio':'.0002','max_reserved_risk_equity_ratio':'.0002'},D('2')),
])
def test_configured_absolute_and_relative_budgets_change_actual_quantity(changes,quantity):
    config=RiskConfigV2(max_slippage_bps='0',**changes)
    assert approve_intent(**inputs(config)).approved_quantity==quantity

def test_configured_account_age_and_intent_ttl_reach_risk_gate():
    data=inputs(RiskConfigV2(max_slippage_bps='0',account_max_age_seconds=1,intent_ttl_seconds=2))
    assert approve_intent(**data).valid_until==NOW+timedelta(seconds=2)
    data['account']=replace(data['account'],as_of=NOW-timedelta(seconds=2))
    with pytest.raises(RiskRejected,match='STALE_ACCOUNT'):approve_intent(**data)
    data=inputs(RiskConfigV2(max_slippage_bps='0',account_max_age_seconds=3))
    data['account']=replace(data['account'],as_of=NOW-timedelta(seconds=2))
    assert approve_intent(**data).approved_quantity==D(10)

def test_configured_quote_age_can_be_stricter_than_hard_limit():
    data=inputs(RiskConfigV2(max_slippage_bps='0',quote_max_age_seconds=1))
    data['quote']=replace(data['quote'],as_of=NOW-timedelta(seconds=2))
    data['trade_plan']=data['trade_plan'].model_copy(update={'quote_as_of':data['quote'].as_of})
    with pytest.raises(RiskRejected,match='STALE_QUOTE'):approve_intent(**data)

def test_configured_spread_and_slippage_affect_admission_and_quantity():
    from quant_execution.trade_plan import build_trade_plan
    config=RiskConfigV2(max_slippage_bps='0',max_spread_bps='1')
    data=inputs(config)
    data['quote']=replace(data['quote'],bid=D('99.98'))
    data['trade_plan']=build_trade_plan(setup()[0][0].model_copy(update={'invalidation_price':D(99),'target_price':D(110)}),data['instrument'],data['quote'],costs=PaperCostsV1(D(0),D(0),D(0)),config=config,now=NOW)
    with pytest.raises(RiskRejected,match='SPREAD'):approve_intent(**data)
    baseline=approve_intent(**inputs()).approved_quantity
    assert approve_intent(**inputs(RiskConfigV2(max_slippage_bps='10'))).approved_quantity<baseline
