"""Conjunctive market hypotheses with provenance; no indicator vote or probability."""
from decimal import Decimal as D
from pydantic import Field, model_validator
from strategies.contracts import Record, EvidenceChain, EvidenceNode, MarketObservation, discard_retired_news_mode
from strategies.refresh import MAX_AGE_SECONDS
from strategies.canonical_validation import fresh_receipt

class MarketHypothesis(Record):
    direction: str
    timeframe: str
    structure: str
    min_flow_ratio: D = Field(default=D(".05"),ge=0)
    min_oi_change_ratio: D = Field(default=D(".01"),ge=0)
    min_volume_ratio: D = Field(default=D("1.2"),gt=0)
    max_abs_funding_ratio: D = Field(default=D(".001"),gt=0)
    max_benchmark_conflict: D = Field(default=D(".002"),ge=0)

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_news_mode(cls, value):
        return discard_retired_news_mode(value)

    @property
    def required_kinds(self):
        base=(f"PRICE_STRUCTURE:{self.timeframe}",f"VOLUME:{self.timeframe}",
            "SPOT_FLOW","PERP_FLOW","OI","FUNDING","BENCHMARK","CROSS_OI","CROSS_FUNDING")
        return (*base,f"RETEST:{self.timeframe}") if self.structure=="BREAKOUT_FORMING" else base

def _valid(o,snapshot,hypothesis):
    if not o.usable or o.observed_at>snapshot.captured_at:return False
    if not fresh_receipt(o,requested_at=snapshot.requested_at,captured_at=snapshot.captured_at):return False
    allowed={snapshot.symbol}
    if o.kind=="BENCHMARK":allowed|={"BTCUSDT","ETHUSDT","GLOBAL"}
    if o.symbol not in allowed:return False
    age=(snapshot.captured_at-o.observed_at).total_seconds()
    limit={"15m":900,"1H":3600,"4H":14400}.get(hypothesis.timeframe,0) if ":" in o.kind else MAX_AGE_SECONDS.get(o.kind,60)
    return 0<=age<=limit

def _predicate(o,h):
    sign=D(1) if h.direction=="LONG" else D(-1)
    if o.kind.startswith("PRICE_STRUCTURE:"):
        return o.unit=="DIRECTION" and sign*o.value==1
    if o.kind.startswith("VOLUME:"):
        return o.unit=="EXPANSION_RATIO" and o.value>=h.min_volume_ratio
    if o.kind.startswith("RETEST:"):
        expected=f"RETEST_{h.direction}_{h.timeframe}"
        return o.unit=="BOOLEAN" and o.value==1 and o.source_group==expected
    if o.kind in {"SPOT_FLOW","PERP_FLOW"}:
        return o.unit=="RATIO" and sign*o.value>=h.min_flow_ratio
    if o.kind in {"OI","CROSS_OI"}:
        return o.unit=="CHANGE_RATIO" and o.value>=h.min_oi_change_ratio
    if o.kind in {"FUNDING","CROSS_FUNDING"}:
        return o.unit=="RATE_RATIO" and abs(o.value)<=h.max_abs_funding_ratio
    if o.kind=="BENCHMARK":
        return o.unit=="RETURN_RATIO" and sign*o.value>=-h.max_benchmark_conflict
    return False

def build_evidence_chain(snapshot, *, hypothesis):
    if hypothesis.direction not in {"LONG","SHORT"} or hypothesis.timeframe not in {"15m","1H","4H"}:
        raise ValueError("unsupported hypothesis direction/timeframe")
    nodes=[];missing=[];contradictions=[];selected={}
    name=hypothesis.structure+":"+hypothesis.direction+":"+hypothesis.timeframe
    for kind in hypothesis.required_kinds:
        facts=[o for o in snapshot.observations if o.kind==kind]
        valid=[o for o in facts if _valid(o,snapshot,hypothesis)]
        if len(valid)!=1:
            o=facts[0] if facts else MarketObservation(symbol=snapshot.symbol,kind=kind,provider="evidence",
                source_ref="missing:"+kind,reason="REQUIRED_EVIDENCE_UNAVAILABLE")
            if len(valid)>1:contradictions.append("AMBIGUOUS_SOURCE:"+kind)
            missing.append(kind)
            role="STALE" if o.freshness=="STALE" or (o.usable and not _valid(o,snapshot,hypothesis)) else "MISSING"
        else:
            o=valid[0];selected[kind]=o
            role="SUPPORTING" if _predicate(o,hypothesis) else "CONTRADICTING"
            if role=="CONTRADICTING":contradictions.append("PREDICATE_FAILED:"+kind)
        dependencies=() if kind.startswith("PRICE_STRUCTURE:") else (f"PRICE_STRUCTURE:{hypothesis.timeframe}",)
        nodes.append(EvidenceNode(observation=o,role=role,interpretation=f"{name} prerequisite {kind}: {role}",
            depends_on=dependencies,hypothesis=name))
    spot,perp=selected.get("SPOT_FLOW"),selected.get("PERP_FLOW")
    if spot and perp and spot.source_group==perp.source_group:
        contradictions.append("FLOW_SOURCE_DEPENDENCY")
    cross,local=selected.get("CROSS_OI"),selected.get("OI")
    if cross and local and (cross.source_group==local.source_group or cross.provider==local.provider):
        contradictions.append("CROSS_MARKET_SOURCE_DEPENDENCY")
    confirmed=not missing and not contradictions and snapshot.refresh_status=="REFRESHED"
    return EvidenceChain(hypothesis=name,direction=hypothesis.direction,nodes=tuple(nodes),
        required_kinds=hypothesis.required_kinds,missing_kinds=tuple(missing),contradictions=tuple(contradictions),
        confirmed=confirmed,
        explanation="Structure, volume, spot and perpetual flows, local and cross-market positioning, funding, benchmark context, and conditional retest evidence form one causal chain.")
