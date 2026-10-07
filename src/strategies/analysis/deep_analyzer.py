"""LLM research proposals pass the existing bounded gateway; Python owns facts and geometry."""
from datetime import timedelta
from decimal import Decimal as D
from typing import Literal
from pydantic import Field, model_validator
from quant_phase6.ai import AIService, AIRequest, complete_strategy_analysis
from strategies.contracts import Record, StructuredTradeThesis, Scenario, TriggerRule, discard_retired_news_mode
from strategies.evidence.evidence_chain import build_evidence_chain, MarketHypothesis, _valid
from .prompts import analysis_prompt

HORIZONS=(("1_3H","15m"),("3_8H","1H"),("8_24H","4H"))

class AnalysisPolicyV2(Record):
    provider_name: str = "UNCONFIGURED"
    model: str = "UNCONFIGURED"
    timeout_seconds: float = Field(default=10,gt=0,le=60)
    max_output_bytes: int = Field(default=32768,gt=0,le=65536)
    thesis_ttl_seconds: int = Field(default=300,gt=0,le=600)
    estimated_cost: D = Field(default=D(".01"),ge=0)

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_news_mode(cls,value):
        return discard_retired_news_mode(value)

class AnalystProposal(Record):
    horizon: Literal["1_3H","3_8H","8_24H"]
    bias: Literal["LONG","SHORT","RANGE","WAIT"]
    confidence: Literal["HIGH","MEDIUM","LOW","INSUFFICIENT"]
    market_structure: str
    summary: str = Field(max_length=4000)
    source_refs: tuple[str,...]
    fact_digests: tuple[str,...]
    contradictions: tuple[str,...] = ()

class ProposalResponse(Record):
    proposals: tuple[AnalystProposal,...]
    @model_validator(mode="after")
    def all_horizons(self):
        if len(self.proposals)!=3 or {p.horizon for p in self.proposals}!={h for h,_ in HORIZONS}:
            raise ValueError("exactly three horizon proposals required")
        return self

def validate_proposal(raw,snapshot):
    proposal=AnalystProposal.model_validate(raw)
    facts={o.digest:o for o in snapshot.observations}
    refs={o.source_ref for o in snapshot.observations}
    if not set(proposal.fact_digests)<=set(facts) or not set(proposal.source_refs)<=refs:
        raise ValueError("LLM cited a fact absent from this snapshot")
    if proposal.bias in {"LONG","SHORT"} and (not proposal.fact_digests or not proposal.source_refs):
        raise ValueError("directional proposal must cite facts")
    if proposal.market_structure!=snapshot.candidate.structure:
        raise ValueError("unverified market structure proposal")
    return proposal

class ProposalSchema:
    def validate(self,raw):
        return ProposalResponse.model_validate(raw).model_dump(mode="json")

def _scenarios(timeframe, geometry=None):
    up=TriggerRule(side="LONG",price=geometry[1],operator="GTE") if geometry else None
    down=TriggerRule(side="SHORT",price=geometry[0],operator="LTE") if geometry else None
    return (
        Scenario(kind="UP",conditions=(f"Fresh {timeframe} bullish structure and supporting spot/perpetual flows","No contradictory funding or benchmark context"),trigger=up),
        Scenario(kind="DOWN",conditions=(f"Fresh {timeframe} bearish structure and supporting spot/perpetual selling","No contradictory funding or benchmark context"),trigger=down),
        Scenario(kind="RANGE",conditions=("No complete directional causal chain","Wait for boundary confirmation or invalidation")),
    )

def _geometry(snapshot, timeframe, bias):
    hypothesis=MarketHypothesis(direction=bias,timeframe=timeframe,structure=snapshot.candidate.structure)
    def fact(kind,unit):
        values=[o for o in snapshot.observations if o.kind==kind and o.unit==unit and _valid(o,snapshot,hypothesis)]
        return values[0].value if len(values)==1 else None
    support=fact("SUPPORT:"+timeframe,"USDT");resistance=fact("RESISTANCE:"+timeframe,"USDT")
    price=fact("PRICE","USDT");atr=fact("ATR:"+timeframe,"USDT")
    if any(v is None or v<=0 for v in (support,resistance,price,atr)) or support>=resistance:return None
    sign=D(1) if bias=="LONG" else D(-1)
    trigger=resistance if bias=="LONG" else support
    if snapshot.candidate.structure=="TREND_CONTINUATION":trigger=price
    stop=support if bias=="LONG" else resistance
    target=trigger+sign*atr*D(2)
    if sign*(trigger-stop)<=0 or target<=0:return None
    return support,resistance,trigger,stop,target

def analyze_candidate(snapshot, *, provider, policy):
    proposals={};provider_reason="AI_PROVIDER_UNAVAILABLE"
    # Python already owns mandatory evidence and numeric geometry. A model
    # cannot repair a missing hard source; record WAIT without spending a call.
    direction=snapshot.candidate.direction or 'LONG'
    actionable_data=any(not build_evidence_chain(snapshot,hypothesis=MarketHypothesis(
        direction=direction,timeframe=tf,structure=snapshot.candidate.structure)).missing_kinds and _geometry(snapshot,tf,direction)
        for _,tf in HORIZONS)
    if not actionable_data:provider_reason='NO_TRADE_BY_DATA'
    if provider is not None and actionable_data:
        if not isinstance(provider,AIService):raise TypeError("V2 must use the existing AIService gateway")
        if snapshot.data_source=="REAL_PUBLIC_DATA" and getattr(provider.providers.get(policy.provider_name),"test_only",False):
            provider_reason="AI_TEST_PROVIDER_FORBIDDEN"
        elif policy.provider_name in provider.providers:
            request=AIRequest(purpose="V2_DEEP_ANALYSIS",prompt_id="QUANT_PAPER_V2_DEEP_ANALYSIS",
                prompt_version="2.0.3",schema_version="STRATEGY_V2",model_policy_version=policy.digest,
                provider=policy.provider_name,model=policy.model,envelope=analysis_prompt(snapshot,policy),
                context_hash=snapshot.digest,timeout_seconds=policy.timeout_seconds,max_output_bytes=policy.max_output_bytes,
                estimated_cost=policy.estimated_cost)
            def evidence_check(output):
                for raw in output["proposals"]:validate_proposal(raw,snapshot)
            result=complete_strategy_analysis(provider,request,ProposalSchema(),evidence_validator=evidence_check)
            if result.output is not None:
                proposals={p.horizon:p for p in (validate_proposal(r,snapshot) for r in result.output["proposals"])}
                provider_reason=""
            else:provider_reason="AI_"+str(result.error_code or result.status)
    out=[]
    for horizon,timeframe in HORIZONS:
        proposal=proposals.get(horizon)
        direction=proposal.bias if proposal and proposal.bias in {"LONG","SHORT"} else snapshot.candidate.direction or "LONG"
        chain=build_evidence_chain(snapshot,hypothesis=MarketHypothesis(direction=direction,timeframe=timeframe,structure=snapshot.candidate.structure))
        geometry=_geometry(snapshot,timeframe,direction)
        reason=provider_reason or ("NO_TRADE_BY_DATA" if chain.missing_kinds or not geometry else
            "NO_TRADE_BY_CONFLICT" if chain.contradictions or proposal.contradictions else
            "NO_TRADE_BY_STRATEGY" if not chain.confirmed or proposal.bias not in {"LONG","SHORT"} else None)
        bias="WAIT" if reason else direction
        confidence="INSUFFICIENT" if reason else proposal.confidence
        trigger=None;stop=None;target=None;support=();resistance=()
        if geometry:
            sup,res,entry,st,tgt=geometry;support=(sup,);resistance=(res,)
            if not reason:
                trigger=TriggerRule(side=direction,price=entry,operator="GTE" if direction=="LONG" else "LTE",
                    required_kinds=chain.required_kinds,valid_until=snapshot.captured_at+timedelta(seconds=policy.thesis_ttl_seconds))
                stop,target=st,tgt
        out.append(StructuredTradeThesis(symbol=snapshot.symbol,horizon=horizon,timeframe=timeframe,
            generated_at=snapshot.captured_at,valid_until=snapshot.captured_at+timedelta(seconds=policy.thesis_ttl_seconds),
            snapshot_digest=snapshot.digest,bias=bias,market_structure=snapshot.candidate.structure,confidence=confidence,
            evidence_chain=chain,contradictions=(*chain.contradictions,*(proposal.contradictions if proposal else ())),
            support_levels=support,resistance_levels=resistance,scenarios=_scenarios(timeframe,geometry),trigger=trigger,
            invalidation_price=stop,target_price=target,no_trade_reason=reason,
            risk_events=tuple(c for c in chain.contradictions if "COVERAGE" in c),
            source_refs=tuple(sorted({n.observation.source_ref for n in chain.nodes})),
            advisories=chain.advisories,
            summary=proposal.summary if proposal else "Current data or analysis service cannot establish a high-confidence direction; wait for confirmation."))
    return tuple(out)
