"""Thesis-bound Paper geometry; only RiskPolicy may choose quantity."""
from decimal import Decimal as D, ROUND_FLOOR, ROUND_CEILING
from datetime import datetime
from pydantic import Field
from strategies.contracts import Record, StructuredTradeThesis, Horizon
from quant_execution.risk_config import RiskConfigV2
from quant_execution.paper_v1 import PaperCostsV1
from quant_execution.contracts import utc
from quant_phase9.canonical import canonical_sha256

class PaperTradePlanV2(Record):
    schema_version: str = "PAPER_TRADE_PLAN_V2"
    profile: str = "QUANT_PAPER_V2"
    symbol: str
    canonical_symbol: str
    thesis_digest: str
    snapshot_digest: str
    risk_config_digest: str
    horizon: Horizon
    side: str
    stop_price: D = Field(gt=0)
    target_price: D = Field(gt=0)
    reference_price: D = Field(gt=0)
    net_R: D = Field(gt=0)
    risk_per_unit: D = Field(gt=0)
    max_hold_seconds: int = Field(gt=0,strict=True)
    setup_id: str
    setup_trigger_at: datetime
    valid_until: datetime
    quote_as_of: datetime
    costs_digest: str

def build_trade_plan(thesis, instrument, quote, *, costs, config, now):
    utc(now)
    if not isinstance(thesis,StructuredTradeThesis) or not isinstance(config,RiskConfigV2) or not isinstance(costs,PaperCostsV1):
        raise ValueError("V2_TYPED_PLAN_INPUTS_REQUIRED")
    if (thesis.bias not in {"LONG","SHORT"} or thesis.no_trade_reason or thesis.generated_at>now
        or thesis.valid_until<=now or thesis.evidence_chain is None or not thesis.evidence_chain.confirmed
        or thesis.contradictions or thesis.risk_events or thesis.trigger is None
        or thesis.symbol!=instrument.core_symbol or quote.canonical_symbol!=instrument.canonical_symbol):
        raise ValueError("V2_THESIS_INVALID")
    sign=D(1) if thesis.bias=="LONG" else D(-1)
    ref=quote.ask if sign==1 else quote.bid
    tick=instrument.price_tick
    rounding=ROUND_FLOOR if sign==1 else ROUND_CEILING
    stop=(thesis.invalidation_price/tick).to_integral_value(rounding=rounding)*tick
    target=(thesis.target_price/tick).to_integral_value(rounding=rounding)*tick
    if stop<=0 or sign*(ref-stop)<=0 or sign*(target-ref)<=0 or sign*(ref-thesis.trigger.price)<0:
        raise ValueError("V2_PLAN_GEOMETRY_INVALID")
    if thesis.trigger.valid_until is not None and thesis.trigger.valid_until<=now:
        raise ValueError("V2_TRIGGER_EXPIRED")
    slip=config.max_slippage_bps/D(10000)
    entry=ref*(1+sign*slip);exit_stop=stop*(1-sign*slip);exit_target=target*(1-sign*slip)
    funding=entry*costs.funding_cost_rate_max_hold
    risk=sign*(entry-exit_stop)+entry*costs.entry_fee_rate+exit_stop*costs.exit_fee_rate+funding
    reward=sign*(exit_target-entry)-entry*costs.entry_fee_rate-exit_target*costs.exit_fee_rate-funding
    if risk<=0 or reward<=0:raise ValueError("V2_COSTS_EXCEED_REWARD")
    setup_id=str(canonical_sha256(dict(symbol=thesis.symbol,side=thesis.bias,trigger=thesis.trigger.model_dump(mode="python"),
        at=thesis.generated_at,horizon=thesis.horizon)))
    return PaperTradePlanV2(symbol=thesis.symbol,canonical_symbol=instrument.canonical_symbol,
        thesis_digest=thesis.digest,snapshot_digest=thesis.snapshot_digest,risk_config_digest=config.digest,
        horizon=thesis.horizon,side=thesis.bias,stop_price=stop,target_price=target,reference_price=ref,
        net_R=reward/risk,risk_per_unit=risk,max_hold_seconds=config.hold_seconds(thesis.horizon),
        setup_id=setup_id,setup_trigger_at=thesis.generated_at,valid_until=thesis.valid_until,
        quote_as_of=quote.as_of,costs_digest=str(canonical_sha256(costs)))

def validate_execution_capabilities(config, capabilities):
    required={"MARKET_IOC","STOP_MARKET_REDUCE_ONLY","POSITION_SNAPSHOT","LOCAL_RECONCILE",
        "PAPER_STRATEGY_EXITS","PAPER_POSITION_MANAGEMENT","V2_CONFIGURED_HOLD"}
    if config.max_leverage>1:required.add("V2_CONFIGURED_LEVERAGE")
    if config.max_open_positions>1 or config.max_open_intents>1:required.add("V2_SHARED_ACCOUNT_MULTI_POSITION")
    if config.allow_pyramiding or config.allow_averaging_down:required.add("V2_UPDATED_ADD_PROTECTION")
    if not required<=set(capabilities):raise ValueError("UNSUPPORTED_EXECUTION_CAPABILITY")
