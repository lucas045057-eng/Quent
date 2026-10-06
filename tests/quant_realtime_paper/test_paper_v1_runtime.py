from datetime import timedelta
from decimal import Decimal as D
from dataclasses import replace
import pytest
from tests.quant_realtime_paper.test_execution_wiring import Store, PaperAdapter, _process
from tests.quant_execution.test_paper_v1_risk import inputs
from tests.quant_phase9.test_paper_v1 import approved_conservative
from tests.quant_phase9.test_evidence import AS_OF
from quant_execution.paper_v1 import build_trade_plan,PaperCostsV1
from quant_realtime_paper.wiring import RealtimePaperExecutionPipeline

@pytest.mark.parametrize('late_veto',[False,True])
def test_event_recheck_is_mandatory_and_repeated_immediately_before_submit(tmp_path,late_veto):
    snapshot,kw=inputs(tmp_path)
    candidate=kw['envelope'].candidate
    plan=build_trade_plan(snapshot,candidate,kw['instrument'],kw['quote'],PaperCostsV1(D('0.0004'),D('0.0004'),D('0.0002')),now=AS_OF)
    kw.update(trade_plan=plan,stop_price=plan.stop_price);kw.pop('envelope')
    adapter=PaperAdapter();adapter.capabilities=lambda:frozenset({'MARKET_IOC','STOP_MARKET_REDUCE_ONLY','POSITION_SNAPSHOT','LOCAL_RECONCILE','PAPER_STRATEGY_EXITS','PAPER_POSITION_MANAGEMENT'})
    checks=[]
    def recheck(*args):
        checks.append(args[-1])
        if late_veto and len(checks)==2: raise ValueError('SECURITY_EVENT_VETO')
    pipeline=RealtimePaperExecutionPipeline(approved_policy=approved_conservative(tmp_path),current_revision='b'*40,
        phase9_evaluate=lambda *args:(snapshot,candidate,'ACTIVE'),risk_inputs=lambda *args:kw,
        execution_store=(store:=Store()),adapter_factory=lambda intent:adapter,entry_recheck=recheck,clock=lambda:AS_OF,
        environ={'TRADING_MODE':'paper','PAPER_ONLY':'true','LIVE_ALLOWED':'false'})
    stage=type('Stage',(),dict(category='A',symbol='BTCUSDT',status=type('Status',(),{'value':'AVAILABLE'})()))()
    result=_process(pipeline,stage,now=AS_OF)
    assert len(checks)==2 and adapter.submit_calls==(0 if late_veto else 1)
    if not late_veto:
        assert result.disposition == 'PAPER_RESULT_RECORDED'
        assert result.execution_intent.strategy_profile == 'QUANT_PAPER_V1_CONSERVATIVE'
        assert result.execution_results and store.results
    if late_veto:
        assert store.states[result.execution_intent.intent_id]=='REJECTED'
        assert store.results[-1].filled_quantity==0
        assert result.reason_code=='SECURITY_EVENT_VETO'

def test_position_management_does_not_require_new_entry_candidate():
    pipeline=RealtimePaperExecutionPipeline(approved_policy=None,current_revision='b'*40,phase9_evaluate=None,risk_inputs=None,
        execution_store=None,adapter_factory=None,position_manager=lambda now:('managed',),
        environ={'TRADING_MODE':'paper','PAPER_ONLY':'true','LIVE_ALLOWED':'false'})
    assert pipeline.manage_positions(now=AS_OF)==('managed',)


def test_startup_block_retains_a_reduce_only_manager(tmp_path,monkeypatch):
    from tests.quant_realtime_paper.test_runtime_assembly import _config,_env
    from quant_realtime_paper import assembly
    calls=[]
    monkeypatch.setattr(assembly,'manage_persisted_positions',lambda cfg,env,now:calls.append(now),raising=False)
    monitor=assembly.build_default_runtime(_config(tmp_path),_env(tmp_path))
    assert monitor.execution_pipeline is None and monitor.startup_blocker=='APPROVAL_MISSING'
    assert monitor.position_manager is not None
    monitor.position_manager(AS_OF)
    assert calls==[AS_OF]


@pytest.mark.parametrize('missing', ['trade_plan', 'entry_recheck', 'both'])
def test_enabled_paper_v1_missing_required_capability_blocks_before_adapter(tmp_path, missing):
    snapshot, kw = inputs(tmp_path)
    candidate = kw['envelope'].candidate
    if missing == 'entry_recheck':
        plan = build_trade_plan(
            snapshot, candidate, kw['instrument'], kw['quote'],
            PaperCostsV1(D('0.0004'), D('0.0004'), D('0.0002')), now=AS_OF,
        )
        kw.update(trade_plan=plan, stop_price=plan.stop_price)
    kw.pop('envelope')
    approved = approved_conservative(tmp_path)
    assert {(p.pattern_type, p.timeframe, p.direction.value)
            for p in approved.manifest.policy_content.enabled_patterns} == {
                ('BREAKOUT_CONFIRMATION', '15m', 'LONG'),
                ('BREAKOUT_CONFIRMATION', '15m', 'SHORT'),
            }
    calls = []
    def adapter_factory(intent):
        calls.append(intent)
        pytest.fail('missing capability must not reach execution adapter')
    pipeline = RealtimePaperExecutionPipeline(
        approved_policy=approved, current_revision='b' * 40,
        phase9_evaluate=lambda *args: (snapshot, candidate, 'ACTIVE'),
        risk_inputs=lambda *args: kw, execution_store=(store := Store()),
        adapter_factory=adapter_factory,
        entry_recheck=(lambda *args: None) if missing == 'trade_plan' else None,
        clock=lambda: AS_OF,
        environ={'TRADING_MODE': 'paper', 'PAPER_ONLY': 'true', 'LIVE_ALLOWED': 'false'},
    )
    stage = type('Stage', (), dict(
        category='A', symbol='BTCUSDT',
        status=type('Status', (), {'value': 'AVAILABLE'})(),
    ))()
    result = _process(pipeline, stage, now=AS_OF)
    assert result.reason_code == 'PAPER_V1_REQUIRED_CAPABILITY_MISSING'
    assert result.disposition == 'NO_TRADE_BY_SYSTEM_NOT_READY'
    assert result.execution_intent is None
    assert calls == [] and store.intents == {} and store.results == []
