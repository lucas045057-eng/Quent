from datetime import datetime, timezone, timedelta
from decimal import Decimal as D
import pytest
from strategies.contracts import AnalysisSnapshot, ScreeningCandidate, MarketObservation
NOW=datetime(2026,10,3,tzinfo=timezone.utc)

def observation(kind,value,unit,group=None):
    return MarketObservation(symbol="SOLUSDT",kind=kind,provider="bitget",source_ref=kind,source_group=group or kind,
        value=value,unit=unit,observed_at=NOW,fetched_at=NOW,
        availability="AVAILABLE",freshness="FRESH",quality="VALID",coverage="COMPLETE")

def snapshot(extra=()):
    return AnalysisSnapshot(symbol="SOLUSDT",requested_at=NOW,captured_at=NOW,screening_digest="a"*64,
        observations=tuple(extra),refresh_status="REFRESHED",data_source="SYNTHETIC_FIXTURE",
        candidate=ScreeningCandidate(symbol="SOLUSDT",category="A",direction="LONG",structure="TREND_CONTINUATION",reason_codes=("EDGE",)))

def complete_facts(tf="15m"):
    return tuple(observation(k,v,u,g).model_copy(update={"provider":"coinalyze" if k.startswith("CROSS_") else "bitget"}) for k,v,u,g in (
        (f"PRICE_STRUCTURE:{tf}","1","DIRECTION","STRUCTURE"),
        (f"VOLUME:{tf}","1.5","EXPANSION_RATIO","STRUCTURE"),
        ("SPOT_FLOW",".2","RATIO","SPOT_FLOW"),("PERP_FLOW",".3","RATIO","PERP_FLOW"),
        ("OI",".04","CHANGE_RATIO","POSITIONING"),("FUNDING",".0001","RATE_RATIO","POSITIONING"),
        ("BENCHMARK",".01","RETURN_RATIO","BENCHMARK"),
        ("CROSS_OI",".03","CHANGE_RATIO","CROSS_POSITIONING"),("CROSS_FUNDING",".0001","RATE_RATIO","CROSS_POSITIONING")))

def test_price_oi_alone_does_not_choose_direction():
    from strategies.evidence.evidence_chain import build_evidence_chain, MarketHypothesis
    result=build_evidence_chain(snapshot((observation("PRICE","100","USDT"),observation("OI",".1","CHANGE_RATIO"))),
        hypothesis=MarketHypothesis(direction="LONG",timeframe="15m",structure="TREND_CONTINUATION"))
    assert not result.confirmed and result.missing_kinds

def test_missing_and_stale_support_never_confirm_hypothesis():
    from strategies.evidence.evidence_chain import build_evidence_chain, MarketHypothesis
    facts=tuple(o.model_copy(update={"freshness":"STALE"}) if o.kind=="SPOT_FLOW" else o for o in complete_facts())
    result=build_evidence_chain(snapshot(facts),hypothesis=MarketHypothesis(direction="LONG",timeframe="15m",structure="TREND_CONTINUATION"))
    assert not result.confirmed and "SPOT_FLOW" in result.missing_kinds
    assert next(n for n in result.nodes if n.observation.kind=="SPOT_FLOW").role=="STALE"

def test_cvd_and_delta_do_not_count_as_independent_sources():
    from strategies.evidence.evidence_chain import build_evidence_chain, MarketHypothesis
    facts=tuple(o.model_copy(update={"source_group":"SAME_FLOW"}) if o.kind in {"SPOT_FLOW","PERP_FLOW"} else o for o in complete_facts())
    result=build_evidence_chain(snapshot(facts),hypothesis=MarketHypothesis(direction="LONG",timeframe="15m",structure="TREND_CONTINUATION"))
    assert not result.confirmed and "FLOW_SOURCE_DEPENDENCY" in result.contradictions

def test_complete_chain_confirms_and_opposing_spot_flow_is_contradiction():
    from strategies.evidence.evidence_chain import build_evidence_chain, MarketHypothesis
    hypothesis=MarketHypothesis(direction="LONG",timeframe="15m",structure="TREND_CONTINUATION")
    assert build_evidence_chain(snapshot(complete_facts()),hypothesis=hypothesis).confirmed
    facts=tuple(o.model_copy(update={"value":D("-.3")}) if o.kind=="SPOT_FLOW" else o for o in complete_facts())
    result=build_evidence_chain(snapshot(facts),hypothesis=hypothesis)
    assert not result.confirmed and any("SPOT_FLOW" in c for c in result.contradictions)

def test_removed_event_sources_do_not_enter_chain_or_invent_neutrality():
    from strategies.evidence.evidence_chain import build_evidence_chain, MarketHypothesis
    base=complete_facts()
    source=observation("EVENT_COVERAGE","1","RISK_FLAG","LEGACY_EVENT")
    legacy=(source,source.model_copy(update={"kind":"EXCHANGE_EVENT_COVERAGE","provider":"bitget_official"}),
        source.model_copy(update={"kind":"MACRO_COVERAGE","provider":"xoomar","symbol":"GLOBAL"}))
    result=build_evidence_chain(snapshot((*base,*legacy)),hypothesis=MarketHypothesis(
        direction="LONG",timeframe="15m",structure="TREND_CONTINUATION"))
    assert result.confirmed
    assert not set(("EVENT_COVERAGE","EXCHANGE_EVENT_COVERAGE","MACRO_COVERAGE")) & set(result.required_kinds)
    assert not set(("EVENT_COVERAGE","EXCHANGE_EVENT_COVERAGE","MACRO_COVERAGE")) & set(result.missing_kinds)
    assert result.advisories==()
    assert [o.value for o in legacy]==[D(1),D(1),D(1)]


@pytest.mark.parametrize("missing", [
    "PRICE_STRUCTURE:15m","VOLUME:15m","SPOT_FLOW","PERP_FLOW","OI","FUNDING",
    "BENCHMARK","CROSS_OI","CROSS_FUNDING",
])
def test_non_calendar_prerequisites_remain_required(missing):
    from strategies.evidence.evidence_chain import build_evidence_chain, MarketHypothesis
    rows=tuple(o for o in complete_facts() if o.kind!=missing)
    result=build_evidence_chain(snapshot(rows),hypothesis=MarketHypothesis(
        direction="LONG",timeframe="15m",structure="TREND_CONTINUATION"))
    assert not result.confirmed and missing in result.missing_kinds

def test_three_horizons_have_complete_scenarios_and_unconfigured_provider_is_unavailable():
    from strategies.analysis.deep_analyzer import analyze_candidate, AnalysisPolicyV2
    result=analyze_candidate(snapshot(),provider=None,policy=AnalysisPolicyV2())
    assert {t.horizon for t in result}=={"1_3H","3_8H","8_24H"}
    assert all({s.kind for s in t.scenarios}=={"UP","DOWN","RANGE"} for t in result)
    assert all(t.bias=="WAIT" and t.confidence=="INSUFFICIENT" and t.no_trade_reason for t in result)

def test_llm_fact_not_in_snapshot_is_rejected():
    from strategies.analysis.deep_analyzer import validate_proposal
    proposal={"horizon":"1_3H","bias":"LONG","confidence":"HIGH","market_structure":"TREND_CONTINUATION",
        "summary":"buy pressure","source_refs":["invented"],"fact_digests":["f"*64],"contradictions":[]}
    with pytest.raises(ValueError):validate_proposal(proposal,snapshot(complete_facts()))
