from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
import pytest
from strategies.contracts import MarketObservation, TriggerRule, WaitingTrigger
from strategies.market_view import MarketView, SymbolMarket, StructureWindow
NOW=datetime(2026,10,3,tzinfo=timezone.utc)

def market(symbol="SOLUSDT",side="LONG",price=None,trend=None):
    sign=D(1) if side=="LONG" else D(-1)
    price=D(price) if price else (D(110) if side=="LONG" else D(90))
    obs=[]
    for kind,val,unit in (("PRICE",price,"USDT"),("PERP_FLOW",sign*D(".3"),"RATIO"),
        ("SPOT_FLOW",sign*D(".2"),"RATIO"),("OI",D(".04"),"CHANGE_RATIO"),("FUNDING",D(".0001"),"RATE_RATIO"),
        ("BENCHMARK",sign*D(".1"),"RETURN_RATIO")):
        obs.append(MarketObservation(symbol=symbol,kind=kind,provider="bitget",source_ref=symbol+kind,
            source_group=kind,value=val,unit=unit,observed_at=NOW,fetched_at=NOW,
            availability="AVAILABLE",freshness="FRESH",quality="VALID",coverage="COMPLETE"))
    window=StructureWindow(timeframe="15m",trend=trend or side,support="95",resistance="105",
        atr="2",volume_ratio="1.5",as_of=NOW,coverage="COMPLETE")
    return SymbolMarket(symbol=symbol,price=price,turnover="10000000",spread_bps="2",
        windows=(window,),observations=tuple(obs))

def test_no_edge_returns_zero_candidates():
    from strategies.screener.batch_screener import screen_market, ScreeningPolicyV2
    view=MarketView(as_of=NOW,symbols=(market(price="100",trend="RANGE"),))
    result=screen_market(view,policy=ScreeningPolicyV2())
    assert not result.top_candidates and result.candidates[0].category=="C"

def test_candidate_cap_never_promotes_unqualified_symbols():
    from strategies.screener.batch_screener import screen_market, ScreeningPolicyV2
    view=MarketView(as_of=NOW,symbols=tuple(market(symbol=f"X{i}USDT") for i in range(7)))
    result=screen_market(view,policy=ScreeningPolicyV2())
    assert len(result.top_candidates)==5
    assert [c.reason_codes for c in result.candidates if c.category=="B"]==[("A_CAPACITY_DEFERRED",)]*2

def test_long_short_are_symmetric():
    from strategies.screener.batch_screener import screen_market, ScreeningPolicyV2
    result=screen_market(MarketView(as_of=NOW,symbols=(market(),market(symbol="ADAUSDT",side="SHORT"))),policy=ScreeningPolicyV2())
    assert {c.direction for c in result.top_candidates}=={"LONG","SHORT"}

def test_trigger_requires_reclassification_before_deep_analysis():
    from strategies.screener.batch_screener import screen_market, ScreeningPolicyV2, reevaluate_trigger
    policy=ScreeningPolicyV2()
    waiting=screen_market(MarketView(as_of=NOW,symbols=(market(price="103"),)),policy=policy).waiting_triggers
    assert len(waiting)==1
    current=market(price="106").model_copy(update={"observations":()})
    result=reevaluate_trigger(waiting[0],MarketView(as_of=NOW,symbols=(current,)),policy=policy)
    assert result.category=="D"

def test_range_structure_not_rejected_by_legacy_trend_filter():
    from strategies.screener.batch_screener import screen_market, ScreeningPolicyV2
    result=screen_market(MarketView(as_of=NOW,symbols=(market(price="106",trend="RANGE"),)),policy=ScreeningPolicyV2())
    assert result.top_candidates[0].structure=="BREAKOUT_FORMING"

def test_capacity_deferred_is_not_a_price_trigger():
    from strategies.screener.batch_screener import screen_market, ScreeningPolicyV2
    result=screen_market(MarketView(as_of=NOW,symbols=tuple(market(symbol=f"X{i}USDT") for i in range(7))),policy=ScreeningPolicyV2())
    assert not result.waiting_triggers
    assert all(c.trigger is None for c in result.candidates if c.category=="B")

def test_old_or_missing_inputs_are_not_screening_confirmation():
    from strategies.screener.batch_screener import screen_market, ScreeningPolicyV2
    m=market();m=m.model_copy(update={"observations":tuple(o.model_copy(update={"freshness":"STALE"}) for o in m.observations)})
    result=screen_market(MarketView(as_of=NOW,symbols=(m,)),policy=ScreeningPolicyV2())
    assert result.candidates[0].category=="D" and not result.top_candidates

def test_waiting_level_is_actual_boundary_not_current_quote():
    from strategies.screener.batch_screener import screen_market, ScreeningPolicyV2, reevaluate_trigger
    policy=ScreeningPolicyV2()
    view=MarketView(as_of=NOW,symbols=(market(price="103"),))
    wait=screen_market(view,policy=policy).waiting_triggers[0]
    assert wait.trigger.price==D("105")
    assert reevaluate_trigger(wait,view,policy=policy).category=="B"
    expired=view.model_copy(update={"as_of":NOW+timedelta(seconds=601)})
    assert reevaluate_trigger(wait,expired,policy=policy).reason_codes==("TRIGGER_EXPIRED",)

def test_quote_and_observation_disagreement_cannot_admit():
    from strategies.screener.batch_screener import screen_market, ScreeningPolicyV2
    bad=market().model_copy(update={"price":D("120")})
    result=screen_market(MarketView(as_of=NOW,symbols=(bad,)),policy=ScreeningPolicyV2())
    assert result.candidates[0].category=="D"
