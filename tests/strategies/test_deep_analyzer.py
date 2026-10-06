from datetime import timedelta
import json
from tests.strategies.test_evidence_chain import NOW, snapshot, complete_facts, observation
from quant_phase6.ai import AIService, FakeAIProvider, AIResponse, AIUsage, BudgetLedger, BudgetConfig
from strategies.analysis.deep_analyzer import analyze_candidate, AnalysisPolicyV2

def complete_snapshot():
    facts=[]
    for tf in ("15m","1H","4H"):
        facts.extend(o for o in complete_facts(tf) if ":" in o.kind)
        facts.extend(observation(k,v,"USDT") for k,v in (
            ("SUPPORT:"+tf,"95"),("RESISTANCE:"+tf,"105"),("ATR:"+tf,"10")))
    facts.extend(o for o in complete_facts() if ":" not in o.kind)
    facts.append(observation("PRICE","100","USDT"))
    return snapshot(facts)

def service(snap, *, extra=None, false_fact=False):
    def respond(request):
        proposals=[]
        for horizon in ("1_3H","3_8H","8_24H"):
            proposals.append({"horizon":horizon,"bias":"LONG","confidence":"HIGH",
                "market_structure":"TREND_CONTINUATION","summary":"Structure and buying agree; wait if invalidated.",
                "source_refs":[snap.observations[0].source_ref],
                "fact_digests":["f"*64 if false_fact else snap.observations[0].digest],"contradictions":[]})
        body={"proposals":proposals}
        if extra:body.update(extra)
        return AIResponse(provider="fixture",model="fixture",structured_output=body,usage=AIUsage(estimated_cost=__import__("decimal").Decimal("0")))
    fake=FakeAIProvider(respond)
    return AIService({"fixture":fake},budget=BudgetLedger(BudgetConfig()),now=lambda:NOW),fake

def test_configured_gateway_can_analyze_three_horizons():
    snap=complete_snapshot();gateway,fake=service(snap)
    theses=analyze_candidate(snap,provider=gateway,policy=AnalysisPolicyV2(provider_name="fixture",model="fixture"))
    assert len(theses)==3 and all(t.bias=="LONG" and t.confidence=="HIGH" for t in theses)
    assert all(t.target_price==120 and t.invalidation_price==95 for t in theses)
    assert all(t.evidence_chain.confirmed for t in theses)
    assert fake.calls==1

def test_llm_cannot_create_execution_intent_and_false_facts_fail_closed():
    for params in ({"extra":{"execution_intent":{"quantity":"10"}}},{"false_fact":True}):
        snap=complete_snapshot();gateway,_=service(snap,**params)
        theses=analyze_candidate(snap,provider=gateway,policy=AnalysisPolicyV2(provider_name="fixture",model="fixture"))
        assert all(t.bias=="WAIT" for t in theses)

def test_test_only_ai_provider_cannot_be_used_with_real_public_runtime():
    snap=complete_snapshot().model_copy(update={"data_source":"REAL_PUBLIC_DATA"})
    gateway,fake=service(snap)
    theses=analyze_candidate(snap,provider=gateway,policy=AnalysisPolicyV2(provider_name="fixture",model="fixture"))
    assert all(t.bias=="WAIT" for t in theses) and fake.calls==0

def test_scenarios_contain_fresh_numeric_boundaries():
    snap=complete_snapshot();gateway,_=service(snap)
    theses=analyze_candidate(snap,provider=gateway,policy=AnalysisPolicyV2(provider_name="fixture",model="fixture"))
    for thesis in theses:
        assert next(s for s in thesis.scenarios if s.kind=="UP").trigger.price==105
        assert next(s for s in thesis.scenarios if s.kind=="DOWN").trigger.price==95
