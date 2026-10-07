"""Deterministic admission between research and the existing durable candidate."""
from decimal import Decimal as D
from typing import Literal
from pydantic import Field, model_validator
from strategies.contracts import Record, ExecutionPolicyResult, AnalysisSnapshot, discard_retired_news_mode
from strategies.evidence.evidence_chain import build_evidence_chain, MarketHypothesis
from strategies.analysis.deep_analyzer import HORIZONS

class ExecutionPolicyV2(Record):
    strategy_version: Literal["QUANT_PAPER_V2"] = "QUANT_PAPER_V2"
    mode: Literal["PAPER"] = "PAPER"
    allowed_symbols: tuple[str,...] = ("BTCUSDT","ETHUSDT")
    data_source: Literal["REAL_PUBLIC_DATA","SYNTHETIC_FIXTURE"] = "REAL_PUBLIC_DATA"
    minimum_confidence: Literal["HIGH","MEDIUM"] = "HIGH"
    horizon_priority: tuple[str,...] = ("1_3H","3_8H","8_24H")
    max_view_age_seconds: int = Field(default=5,gt=0,le=60,strict=True)
    max_spread_bps: D = Field(default=D(8),ge=0)
    max_trigger_chase_ratio: D = Field(default=D(".003"),ge=0)
    min_reward_risk_ratio: D = Field(default=D("1.5"),gt=0)
    candidate_ttl_seconds: int = Field(default=30,gt=0,le=600,strict=True)
    @model_validator(mode="before")
    @classmethod
    def discard_legacy_news_mode(cls,value):
        return discard_retired_news_mode(value)
    @model_validator(mode="after")
    def valid_scope(self):
        if not self.allowed_symbols or len(set(self.allowed_symbols))!=len(self.allowed_symbols):
            raise ValueError("explicit unique symbol allowlist required")
        if len(self.horizon_priority)!=3 or set(self.horizon_priority)!={h for h,_ in HORIZONS}:
            raise ValueError("each horizon must have exactly one priority")
        return self

def evaluate_execution(theses, view, *, policy, now):
    def outcome(disposition,reason,thesis=None,snapshot_digest=None):
        return ExecutionPolicyResult(disposition=disposition,reason_codes=(reason,),evaluated_at=now,
            policy_digest=policy.digest,thesis=thesis,snapshot_digest=snapshot_digest,recheck_digest=view.digest)
    if len(theses)!=3 or {(t.horizon,t.timeframe) for t in theses}!=set(HORIZONS):
        return outcome("NO_TRADE","THREE_HORIZONS_REQUIRED")
    if any(t.strategy_version!="QUANT_PAPER_V2" or t.schema_version!="STRATEGY_V2" for t in theses):
        return outcome("NO_TRADE","STRATEGY_VERSION_INVALID")
    if len({t.symbol for t in theses})!=1 or len({t.snapshot_digest for t in theses})!=1:
        return outcome("NO_TRADE","ANALYSIS_IDENTITY_CONFLICT")
    symbol=theses[0].symbol
    if symbol not in policy.allowed_symbols:return outcome("NO_TRADE","SYMBOL_NOT_ALLOWED")
    if view.data_source!=policy.data_source:return outcome("NO_TRADE","DATA_SOURCE_NOT_ALLOWED")
    if not 0<=(now-view.as_of).total_seconds()<=policy.max_view_age_seconds:
        return outcome("NO_TRADE","NO_TRADE_BY_DATA")
    if any(t.generated_at>now or t.valid_until<=now for t in theses):
        return outcome("NO_TRADE","THESIS_EXPIRED_OR_FUTURE")
    if any(len(t.scenarios)!=3 for t in theses):return outcome("NO_TRADE","THREE_SCENARIOS_REQUIRED")
    sources=[s for s in view.analysis_snapshots if s.symbol==symbol and s.digest==theses[0].snapshot_digest]
    if len(sources)!=1:return outcome("NO_TRADE","BOUND_ANALYSIS_UNAVAILABLE")
    original=sources[0]
    if original.data_source!=policy.data_source or original.refresh_status!="REFRESHED":
        return outcome("NO_TRADE","NO_TRADE_BY_DATA")
    confidence={"INSUFFICIENT":0,"LOW":1,"MEDIUM":2,"HIGH":3}
    hypothesis_for=lambda t:MarketHypothesis(direction=t.bias,timeframe=t.timeframe,structure=t.market_structure)
    directional=[t for t in theses if t.bias in {"LONG","SHORT"} and t.no_trade_reason is None
        and confidence[t.confidence]>=confidence[policy.minimum_confidence]
        and confidence[t.confidence]>=2]
    if len({t.bias for t in directional})>1:return outcome("NO_TRADE","NO_TRADE_BY_CONFLICT")
    if not directional:return outcome("NO_TRADE","NO_TRADE_BY_STRATEGY",theses[0],original.digest)
    thesis=min(directional,key=lambda t:policy.horizon_priority.index(t.horizon))
    hypothesis=hypothesis_for(thesis)
    original_chain=build_evidence_chain(original,hypothesis=hypothesis)
    if (thesis.evidence_chain is None or thesis.evidence_chain.digest!=original_chain.digest
        or not original_chain.confirmed or thesis.contradictions or thesis.risk_events):
        return outcome("NO_TRADE","ANALYSIS_EVIDENCE_INVALID",thesis,original.digest)
    markets=[m for m in view.symbols if m.symbol==symbol]
    if len(markets)!=1:return outcome("NO_TRADE","CURRENT_MARKET_UNAVAILABLE",thesis,original.digest)
    market=markets[0]
    if (market.instrument is None or market.instrument.symbol!=symbol or market.instrument.venue!="bitget"
        or market.instrument.market!="USDT-FUTURES" or market.instrument.settlement_currency!="USDT"
        or market.instrument.tick is None or market.instrument.lot is None or market.instrument.min_quantity is None):
        return outcome("NO_TRADE","INSTRUMENT_CAPABILITY_UNAVAILABLE",thesis,original.digest)
    if market.spread_bps is None or not 0<=market.spread_bps<=policy.max_spread_bps:
        return outcome("NO_TRADE","NO_TRADE_BY_LIQUIDITY",thesis,original.digest)
    recheck=AnalysisSnapshot(symbol=symbol,requested_at=original.requested_at,captured_at=view.as_of,
        screening_digest=original.screening_digest,observations=market.observations,refresh_status="REFRESHED",
        data_source=policy.data_source,candidate=original.candidate)
    current=build_evidence_chain(recheck,hypothesis=hypothesis)
    if current.missing_kinds:return outcome("NO_TRADE","NO_TRADE_BY_DATA",thesis,original.digest)
    if not current.confirmed:return outcome("NO_TRADE","NO_TRADE_BY_CONFLICT",thesis,original.digest)
    quotes=[o for o in market.observations if o.kind=="PRICE" and o.symbol==symbol and o.usable
        and o.unit=="USDT" and o.fetched_at is not None and original.requested_at<=o.fetched_at<=now
        and 0<=(now-o.observed_at).total_seconds()<=policy.max_view_age_seconds]
    if len(quotes)!=1 or market.price!=quotes[0].value:
        return outcome("NO_TRADE","QUOTE_PROVENANCE_INVALID",thesis,original.digest)
    if thesis.trigger is None or thesis.invalidation_price is None or thesis.target_price is None:
        return outcome("NO_TRADE","TRADE_GEOMETRY_UNAVAILABLE",thesis,original.digest)
    if thesis.trigger.valid_until is not None and thesis.trigger.valid_until<=now:
        return outcome("NO_TRADE","TRIGGER_EXPIRED",thesis,original.digest)
    sign=D(1) if thesis.bias=="LONG" else D(-1)
    if sign*(market.price-thesis.invalidation_price)<=0:
        return outcome("NO_TRADE","THESIS_INVALIDATED",thesis,original.digest)
    if sign*(market.price-thesis.trigger.price)<0:
        return outcome("WAITING_FOR_TRIGGER","WAITING_FOR_TRIGGER",thesis,original.digest)
    if sign*(market.price-thesis.trigger.price)/thesis.trigger.price>policy.max_trigger_chase_ratio:
        return outcome("NO_TRADE","TRIGGER_CHASE_LIMIT",thesis,original.digest)
    reward=sign*(thesis.target_price-market.price);risk=sign*(market.price-thesis.invalidation_price)
    if reward<=0 or reward/risk<policy.min_reward_risk_ratio:
        return outcome("NO_TRADE","INSUFFICIENT_REWARD_RISK",thesis,original.digest)
    return outcome("PASS","EXECUTION_POLICY_PASS",thesis,original.digest)
