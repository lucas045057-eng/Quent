from datetime import timedelta
from decimal import Decimal as D
from tests.strategies.test_execution_policy import setup,NOW

def test_default_runtime_uses_v2_only():
    from strategies.runtime import StrategyRuntimeV2
    view=setup()[1]
    runtime=StrategyRuntimeV2(view_loader=lambda now:view)
    screening=runtime.evaluate_cycle(now=NOW)
    assert screening.strategy_version=="QUANT_PAPER_V2"
    assert screening.candidates[0].category=="D" # no invented structure windows

def test_old_breakout_producer_is_not_called(monkeypatch):
    from quant_phase1 import pipeline
    monkeypatch.setattr(pipeline,"run_stage1",lambda *a,**k:(_ for _ in ()).throw(AssertionError("legacy producer")))
    from strategies.runtime import stage1_results
    from strategies.screener.batch_screener import screen_market,ScreeningPolicyV2
    batch=screen_market(setup()[1],policy=ScreeningPolicyV2())
    results=stage1_results(batch)
    assert len(results)==1 and results[0].category=="D" and results[0].strategy_version=="QUANT_PAPER_V2"

def test_invalid_risk_reload_shows_blocker(tmp_path):
    from strategies.runtime import StrategyRuntimeV2
    from quant_execution.risk_config import RiskConfigLoader
    path=tmp_path/"risk.json";path.write_text("{")
    runtime=StrategyRuntimeV2(view_loader=lambda now:setup()[1],risk_loader=RiskConfigLoader(path))
    runtime.evaluate_cycle(now=NOW)
    assert runtime.risk_state.status=="ERROR" and runtime.new_risk_allowed is False

def test_position_management_continues_when_analysis_unavailable():
    from strategies.runtime import StrategyRuntimeV2
    calls=[]
    runtime=StrategyRuntimeV2(view_loader=lambda now:setup()[1],position_manager=lambda now:calls.append(now))
    runtime.evaluate_cycle(now=NOW)
    assert calls==[NOW]


def test_production_entrypoints_do_not_route_legacy_strategy():
    import ast
    from pathlib import Path
    root=Path(__file__).resolve().parents[2]
    for path in ('src/quant_phase1/entrypoints/engine.py','src/quant_realtime_paper/runtime.py'):
        tree=ast.parse((root/path).read_text())
        assert not any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='run_stage1' for n in ast.walk(tree))

def test_closed_structure_refresh_uses_timeframe_age():
    from strategies.refresh import refresh_candidate
    from strategies.contracts import AnalysisRequest
    from tests.strategies.test_deep_analyzer import complete_snapshot
    s=complete_snapshot(); old=s.observations[0].model_copy(update={'kind':'SUPPORT:1H','observed_at':NOW-timedelta(minutes=20),'fetched_at':NOW})
    class Source:
        data_source='SYNTHETIC_FIXTURE'
        def refresh(self,request):return (old,)
    request=AnalysisRequest(candidate=s.candidate,requested_at=NOW,deadline=NOW+timedelta(seconds=10),screening_digest=s.screening_digest,required_kinds=('SUPPORT:1H',))
    result=refresh_candidate(request,source=Source(),now=NOW)
    assert result.observations[0].freshness=='FRESH'


def test_fresh_reclassification_can_find_existing_v2_research_without_same_receipt_id():
    from quant_realtime_paper.assembly import _stage1_projection_matches
    from quant_phase1.stage1 import Stage1Result
    from quant_phase1.contracts import DataStatus
    stage=Stage1Result('SOLUSDT','A','V2_EDGE',DataStatus.AVAILABLE,('ticker:new-receipt',),{},
        'TREND_CONTINUATION',('V2_EDGE',),key_metrics={'direction':'LONG','screening_digest':'b'*64},
        timestamp=NOW,strategy_version='QUANT_PAPER_V2')
    projection=dict(symbol=stage.symbol,category='A',classification=stage.classification,status='AVAILABLE',
        reason=stage.reason,reason_codes=list(stage.reason_codes),inputs_used=['ticker:previous-receipt'],
        indicators={},structure=stage.structure,key_metrics={'direction':'LONG','screening_digest':'a'*64})
    assert _stage1_projection_matches(stage,projection)
    assert not _stage1_projection_matches(stage,{**projection,'key_metrics':{'direction':'SHORT'}})
    assert not _stage1_projection_matches(stage,{**projection,'structure':'BREAKOUT_FORMING'})
    assert not _stage1_projection_matches(stage,{**projection,'category':'B'})
