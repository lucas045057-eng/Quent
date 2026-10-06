"""Stage A admission, causal prerequisites and stable ranking; no voting score."""
from datetime import timedelta
from decimal import Decimal as D
from pydantic import Field
from strategies.contracts import Record, ScreeningCandidate, ScreeningBatch, TriggerRule, WaitingTrigger, EvidenceNode
from strategies.structures.breakout import breakout_direction
from strategies.structures.trend_continuation import continuation_direction
from strategies.structures.range import boundary
from strategies.refresh import MAX_AGE_SECONDS

class ScreeningPolicyV2(Record):
    max_candidates: int = Field(default=5,ge=1,le=5,strict=True)
    min_turnover: D = Field(default=D("5000000"),gt=0)
    max_spread_bps: D = Field(default=D("15"),ge=0)
    min_space_ratio: D = Field(default=D(".005"),gt=0)
    min_volume_ratio: D = Field(default=D("1.2"),gt=0)
    min_flow_ratio: D = Field(default=D(".05"),ge=0)
    max_abs_funding_ratio: D = Field(default=D(".001"),gt=0)
    max_trigger_distance: D = Field(default=D(".03"),gt=0,le=D(".1"))
    trigger_ttl_seconds: int = Field(default=600,gt=0,strict=True)
    required_kinds: tuple[str,...] = ("PRICE","PERP_FLOW","SPOT_FLOW","OI","FUNDING","BENCHMARK")

def current_observations(market, as_of):
    out={}
    for o in market.observations:
        if not o.usable or o.observed_at>as_of:continue
        if (as_of-o.observed_at).total_seconds()>MAX_AGE_SECONDS.get(o.kind,60):continue
        if o.symbol!=market.symbol and not (o.kind=="BENCHMARK" and o.symbol in {"BTCUSDT","ETHUSDT","GLOBAL"}):continue
        if o.kind in out: return {} # ambiguous duplicate inputs cannot establish confirmation
        out[o.kind]=o
    return out

def classify(market, *, as_of, policy):
    fields=dict(symbol=market.symbol,turnover=market.turnover,spread_bps=market.spread_bps)
    def result(category,reason,**extra):
        return ScreeningCandidate(category=category,reason_codes=(reason,),**fields,**extra)
    observations=current_observations(market,as_of)
    if (market.turnover is not None and market.turnover<policy.min_turnover
        or market.spread_bps is not None and (market.spread_bps<0 or market.spread_bps>policy.max_spread_bps)):
        return result("D","NO_TRADE_BY_LIQUIDITY")
    if (market.price is None or market.price<=0 or any(k not in observations for k in policy.required_kinds)):
        return result("D","MISSING_OR_STALE_SCREENING_DATA")
    if observations["PRICE"].value!=market.price or observations["PRICE"].unit!="USDT":
        return result("D","QUOTE_PROVENANCE_CONFLICT")
    if (market.turnover is None or market.turnover<policy.min_turnover or market.spread_bps is None
        or market.spread_bps<0 or market.spread_bps>policy.max_spread_bps):
        return result("D","NO_TRADE_BY_LIQUIDITY")
    windows=[w for w in market.windows if w.coverage=="COMPLETE" and w.as_of is not None
        and 0<=(as_of-w.as_of).total_seconds()<={"15m":900,"1H":3600,"4H":14400}.get(w.timeframe,0)]
    if not windows:return result("D","STRUCTURE_UNAVAILABLE")
    window=next((w for w in windows if w.timeframe=="15m"),windows[0])
    if window.support is None or window.resistance is None or window.support>=window.resistance:
        return result("D","STRUCTURE_INVALID")
    if window.atr is None or window.atr<=0 or (window.resistance-window.support)/market.price<policy.min_space_ratio:
        return result("C","INSUFFICIENT_SPACE")
    perp,spot=observations["PERP_FLOW"],observations["SPOT_FLOW"]
    if perp.unit!="RATIO" or spot.unit!="RATIO" or observations["OI"].unit!="CHANGE_RATIO":
        return result("D","INCOMPATIBLE_FLOW_OR_OI_UNITS")
    if observations["FUNDING"].unit!="RATE_RATIO" or observations["BENCHMARK"].unit!="RETURN_RATIO":
        return result("D","INCOMPATIBLE_CONTEXT_UNITS")
    side="LONG" if perp.value>policy.min_flow_ratio and spot.value>policy.min_flow_ratio else "SHORT" if perp.value<-policy.min_flow_ratio and spot.value<-policy.min_flow_ratio else None
    breach=breakout_direction(market.price,window)
    if side is None or (breach is not None and side!=breach):
        return result("C","FLOW_STRUCTURE_CONFLICT")
    sign=D(1) if side=="LONG" else D(-1)
    if observations["OI"].value<=0:
        return result("B","POSITIONING_CONFIRMATION_REQUIRED",direction=side)
    if abs(observations["FUNDING"].value)>policy.max_abs_funding_ratio:
        return result("B","FUNDING_OVERHEATED",direction=side)
    if sign*observations["BENCHMARK"].value<0:
        return result("C","BENCHMARK_CONFLICT")
    if window.volume_ratio is None or window.volume_ratio<policy.min_volume_ratio:
        return result("B","VOLUME_CONFIRMATION_REQUIRED",direction=side)
    level=boundary(window,side)
    distance=sign*(level-market.price)/market.price
    stop=window.support if side=="LONG" else window.resistance
    nodes=tuple(EvidenceNode(observation=observations[k],role="SUPPORTING",
        interpretation="Stage A prerequisite: "+k,depends_on=("PRICE_STRUCTURE",) if k!="PRICE" else ())
        for k in policy.required_kinds)
    trigger=TriggerRule(side=side,price=level,operator="GTE" if side=="LONG" else "LTE",
        required_kinds=policy.required_kinds,valid_until=as_of+timedelta(seconds=policy.trigger_ttl_seconds))
    if breach:
        return result("A","STRUCTURE_FLOW_POSITIONING_CHAIN",direction=side,structure="BREAKOUT_FORMING",
            trigger=trigger,invalidation_price=stop,potential_space_ratio=window.atr*D(2)/market.price,evidence=nodes)
    if 0<distance<=policy.max_trigger_distance:
        return result("B","WAITING_FOR_TRIGGER",direction=side,structure="RANGE_BOUNDARY",
            trigger=trigger,invalidation_price=stop,potential_space_ratio=window.atr*D(2)/market.price,evidence=nodes)
    if continuation_direction(market.price,window)==side:
        target=window.resistance if side=="LONG" else window.support
        if sign*(target-market.price)/market.price>=policy.min_space_ratio:
            return result("A","TREND_FLOW_POSITIONING_CHAIN",direction=side,structure="TREND_CONTINUATION",
                trigger=trigger.model_copy(update={"price":market.price}),invalidation_price=stop,potential_space_ratio=abs(target-market.price)/market.price,evidence=nodes)
    return result("C","NO_CLEAR_DIRECTION")

def screen_market(view, *, policy):
    candidates=[classify(m,as_of=view.as_of,policy=policy) for m in view.symbols]
    admitted=sorted((c for c in candidates if c.category=="A"),
        key=lambda c:(-(c.potential_space_ratio or D(0)),c.spread_bps or D(0),-(c.turnover or D(0)),c.symbol))
    keep={c.symbol for c in admitted[:policy.max_candidates]}
    candidates=[c.model_copy(update={"category":"B","reason_codes":("A_CAPACITY_DEFERRED",),"trigger":None})
        if c.category=="A" and c.symbol not in keep else c for c in candidates]
    markets={m.symbol:m for m in view.symbols}
    waiting=tuple(WaitingTrigger(symbol=c.symbol,trigger=c.trigger,invalidation_price=c.invalidation_price,
        distance_ratio=abs(c.trigger.price-markets[c.symbol].price)/markets[c.symbol].price,generated_at=view.as_of)
        for c in candidates if "WAITING_FOR_TRIGGER" in c.reason_codes and c.trigger is not None)
    return ScreeningBatch(generated_at=view.as_of,universe=tuple(m.symbol for m in view.symbols),
        candidates=tuple(candidates),top_candidates=tuple(admitted[:policy.max_candidates]),
        waiting_triggers=waiting,policy_digest=policy.digest,market_digest=view.digest)

def reevaluate_trigger(trigger, view, *, policy):
    market=next((m for m in view.symbols if m.symbol==trigger.symbol),None)
    reason="TRIGGER_SOURCE_UNAVAILABLE"
    if trigger.trigger.valid_until is not None and view.as_of>trigger.trigger.valid_until:
        reason="TRIGGER_EXPIRED"
    elif market is not None:
        if market.price is not None and (
            trigger.trigger.side=="LONG" and market.price<=trigger.invalidation_price or
            trigger.trigger.side=="SHORT" and market.price>=trigger.invalidation_price):
            reason="TRIGGER_INVALIDATED"
        else:return classify(market,as_of=view.as_of,policy=policy)
    return ScreeningCandidate(symbol=trigger.symbol,category="D",reason_codes=(reason,))
