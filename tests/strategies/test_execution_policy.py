from datetime import timedelta
from decimal import Decimal as D
from tests.strategies.test_deep_analyzer import NOW, complete_snapshot, service
from strategies.analysis.deep_analyzer import analyze_candidate, AnalysisPolicyV2
from strategies.market_view import MarketView, SymbolMarket, InstrumentMetadata

def setup():
    snap=complete_snapshot();gateway,_=service(snap)
    theses=analyze_candidate(snap,provider=gateway,policy=AnalysisPolicyV2(provider_name="fixture",model="fixture"))
    metadata=InstrumentMetadata(symbol="SOLUSDT",tick=".01",lot=".01",min_quantity=".01",max_quantity="1000",
        min_notional="5",settlement_currency="USDT",source_ref="bitget:instruments",source_digest="a"*64)
    market=SymbolMarket(symbol="SOLUSDT",price="100",turnover="10000000",spread_bps="2",instrument=metadata,
        observations=snap.observations)
    view=MarketView(as_of=NOW,symbols=(market,),data_source="SYNTHETIC_FIXTURE",analysis_snapshots=(snap,))
    from strategies.execution.execution_policy import ExecutionPolicyV2
    policy=ExecutionPolicyV2(allowed_symbols=("SOLUSDT",),data_source="SYNTHETIC_FIXTURE")
    return theses,view,policy

def test_waiting_trigger_does_not_create_eligible_candidate():
    from strategies.execution.execution_policy import evaluate_execution
    theses,view,policy=setup()
    from strategies.contracts import TriggerRule
    theses=tuple(t.model_copy(update={"trigger":TriggerRule(side="LONG",price="101",operator="GTE")}) for t in theses)
    result=evaluate_execution(theses,view,policy=policy,now=NOW)
    assert result.disposition=="WAITING_FOR_TRIGGER"

def test_pass_is_bound_to_original_analysis_and_current_view():
    from strategies.execution.execution_policy import evaluate_execution
    theses,view,policy=setup()
    result=evaluate_execution(theses,view,policy=policy,now=NOW)
    assert result.disposition=="PASS" and result.recheck_digest==view.digest
    assert result.snapshot_digest==view.analysis_snapshots[0].digest

def test_expiry_invalidation_and_conflict_block_execution():
    from strategies.execution.execution_policy import evaluate_execution
    theses,view,policy=setup()
    assert evaluate_execution(theses,view,policy=policy,now=NOW+timedelta(minutes=6)).disposition=="NO_TRADE"
    market=view.symbols[0]
    bad=market.model_copy(update={"price":D("94"),"observations":tuple(
        o.model_copy(update={"value":D("94")}) if o.kind=="PRICE" else o for o in market.observations)})
    assert evaluate_execution(theses,view.model_copy(update={"symbols":(bad,)}),policy=policy,now=NOW).disposition=="NO_TRADE"
    from strategies.contracts import TriggerRule
    short=theses[1].model_copy(update={"bias":"SHORT","trigger":TriggerRule(side="SHORT",price="100",operator="LTE"),
        "invalidation_price":D("105"),"target_price":D("80")})
    assert "NO_TRADE_BY_CONFLICT" in evaluate_execution((theses[0],short,theses[2]),view,policy=policy,now=NOW).reason_codes


def test_thesis_cannot_replace_actual_evidence_or_analysis():
    from strategies.execution.execution_policy import evaluate_execution
    theses,view,policy=setup()
    bad=tuple(t.model_copy(update={"snapshot_digest":"f"*64}) for t in theses)
    assert evaluate_execution(bad,view,policy=policy,now=NOW).disposition=="NO_TRADE"
    assert evaluate_execution(theses,view.model_copy(update={"analysis_snapshots":()}),policy=policy,now=NOW).disposition=="NO_TRADE"

def test_removed_event_sources_do_not_veto_when_missing_or_flagged():
    from strategies.contracts import MarketObservation
    from strategies.execution.execution_policy import evaluate_execution
    theses,view,policy=setup();market=view.symbols[0]
    legacy_kinds=("EVENT_COVERAGE","EXCHANGE_EVENT_COVERAGE","MACRO_COVERAGE")
    without=market.model_copy(update={"observations":tuple(o for o in market.observations if o.kind not in legacy_kinds)})
    base=view.model_copy(update={"symbols":(without,)})
    assert evaluate_execution(theses,base,policy=policy,now=NOW).disposition=="PASS"
    for kind in legacy_kinds:
        flagged=MarketObservation(symbol=market.symbol,kind=kind,provider="legacy",source_ref="historic-risk",
            source_group="legacy",value=D(1),unit="RISK_FLAG",observed_at=NOW,fetched_at=NOW,processed_at=NOW,
            availability="AVAILABLE",freshness="FRESH",quality="VALID",coverage="COMPLETE")
        bad=without.model_copy(update={"observations":(*without.observations,flagged)})
        result=evaluate_execution(theses,base.model_copy(update={"symbols":(bad,)}),policy=policy,now=NOW)
        assert result.disposition=="PASS"

def test_fixture_data_cannot_be_accepted_by_real_policy():
    from strategies.execution.execution_policy import evaluate_execution, ExecutionPolicyV2
    theses,view,_=setup()
    result=evaluate_execution(theses,view,policy=ExecutionPolicyV2(allowed_symbols=("SOLUSDT",)),now=NOW)
    assert result.disposition=="NO_TRADE"
