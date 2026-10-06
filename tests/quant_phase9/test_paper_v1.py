from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import pytest

from quant_phase9.paper_v1 import (
    PROFILE, BarV1, breakout_retest, confirmed_pivots, flow_confirmation,
    oi_confirmation, funding_confirmation, event_risk, market_vetoes,
    conservative_policy_content,
)
from quant_phase9.patterns import _evaluate
from quant_phase9.policy import PredicateRuleV1
from quant_phase9.contracts import EvidenceDirectionV1, EvidenceTypeV1, SourcePhaseV1
from tests.quant_phase9.test_patterns import _item, NOW

def bars(side='LONG'):
    result=[BarV1(NOW+timedelta(minutes=15*i),D('99'),D('100'),D('98'),D('99'),D('100')) for i in range(21)]
    result += [BarV1(NOW+timedelta(minutes=315),D('99'),D('101'),D('99'),D('100.4'),D('130')),
               BarV1(NOW+timedelta(minutes=330),D('100.4'),D('100.5'),D('99.9'),D('100.2'),D('100'))]
    if side=='SHORT':
        result=[replace(b,open=D('200')-b.open,high=D('200')-b.low,low=D('200')-b.high,close=D('200')-b.close) for b in result]
    return result

@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_breakout_requires_separate_retest_and_frozen_level(side):
    values=bars(side); at=values[-1].closed_at
    setup=breakout_retest(values,side,as_of=at)
    assert setup is not None and setup.level == (D('100') if side=='LONG' else D('100'))
    assert setup.trigger_at < setup.retest_at
    assert setup.atr == D('2') and setup.volume_ratio == D('1.3')
    assert breakout_retest(values[:-1],side,as_of=at) is None

@pytest.mark.parametrize('failure',['low_volume','gap','future','invalidate','timeout'])
def test_bad_breakout_cannot_confirm(failure):
    values=bars(); at=values[-1].closed_at
    if failure=='low_volume': values[-2]=replace(values[-2],turnover=D('124'))
    elif failure=='gap': values[6]=replace(values[6],closed_at=values[5].closed_at)
    elif failure=='future': at=values[-1].closed_at-timedelta(seconds=1)
    elif failure=='invalidate': values[-1]=replace(values[-1],low=D('99.4'),close=D('99.5'))
    else:
        trigger=values[-2]; values=values[:-1]+[replace(trigger,closed_at=trigger.closed_at+timedelta(minutes=15*i),open=D('100.7'),low=D('100.6'),close=D('100.7'),turnover=D('100')) for i in range(1,6)]
    assert breakout_retest(values,'LONG',as_of=at if failure!='timeout' else values[-1].closed_at) is None

def test_pivot_is_confirmed_only_after_two_right_bars():
    values=bars()[:8]
    values[4]=replace(values[4],high=D('105'))
    assert confirmed_pivots(values[:6],high=True)==()
    assert confirmed_pivots(values[:7],high=True)==((values[4].closed_at,D('105')),)

def flow_rows(side='LONG'):
    sign=1 if side=='LONG' else -1
    return [dict(exchange='VERIFIED',window_open=NOW+timedelta(minutes=15*i),window_close=NOW+timedelta(minutes=15*(i+1)),
                 delta_base=D(sign*6),total_volume_base=D('100'),unknown_trade_count=0,cvd=D(sign*(i+1)*6),session='s',coverage_complete=True) for i in range(3)]

@pytest.mark.parametrize('side',['LONG','SHORT'])
def test_flow_two_ratios_and_cvd_increments(side):
    rows=flow_rows(side)
    assert flow_confirmation(rows,side,as_of=rows[-1]['window_close']) == D('0.06')
    rows[-1]['cvd']=rows[-2]['cvd']
    assert flow_confirmation(rows,side,as_of=rows[-1]['window_close']) is None

@pytest.mark.parametrize('field,value',[('unknown_trade_count',1),('session','reset'),('coverage_complete',False),('delta_base',D('4.9'))])
def test_flow_unknown_partial_or_reset_is_not_directional(field,value):
    rows=flow_rows(); rows[-1][field]=value
    assert flow_confirmation(rows,'LONG',as_of=rows[-1]['window_close']) is None

def test_oi_uses_same_venue_base_and_exact_window():
    rows=[dict(exchange='A',at=NOW,base=D('100'),unit_verified=True),dict(exchange='A',at=NOW+timedelta(minutes=15),base=D('100.25'),unit_verified=True)]
    assert oi_confirmation(rows,as_of=rows[-1]['at'])==D('0.0025')
    rows[-1]['base']=D('100'); rows[-1]['usd']=D('20000')
    assert oi_confirmation(rows,as_of=rows[-1]['at'])==D('0')
    rows[-1]['exchange']='B'
    assert oi_confirmation(rows,as_of=rows[-1]['at']) is None

def test_funding_interval_and_direction_boundary():
    assert funding_confirmation(D('0.00025'),14400,D('0.0005'),'LONG')==D('0.0005')
    assert funding_confirmation(D('0.000251'),14400,D('0.000502'),'LONG') is None
    assert funding_confirmation(D('-0.0005'),28800,D('-0.0005'),'SHORT')==D('-0.0005')
    assert funding_confirmation(D('0.0005'),None,D('0.0005'),'LONG') is None

def health():
    return dict(status='AVAILABLE',checked_at=NOW,calendar_complete=True,security_complete=True,coverage_start=NOW-timedelta(hours=1),coverage_end=NOW+timedelta(hours=3))

@pytest.mark.parametrize('seconds',[-3600,0,1800])
def test_macro_blackout_inclusive_and_no_future_actual(seconds):
    at=NOW+timedelta(seconds=seconds)
    h=health(); h.update(checked_at=at,coverage_start=at-timedelta(hours=1),coverage_end=at+timedelta(hours=3))
    assert 'SCHEDULED_MACRO_BLACKOUT' in event_risk(h,[dict(scheduled_at=NOW,importance='HIGH',known_at=NOW-timedelta(hours=2))],[],now=at)

def test_no_events_is_not_health_and_security_needs_explicit_clear():
    assert event_risk(None,[],[],now=NOW)==('EVENT_RISK_UNKNOWN',)
    h=health(); assert event_risk(h,[],[],now=NOW)==()
    assert event_risk(h,[],[dict(event_type='HACK',known_at=NOW)],now=NOW)==('SECURITY_EVENT_VETO',)
    h['checked_at']=NOW-timedelta(seconds=61)
    assert event_risk(h,[],[],now=NOW)==('EVENT_RISK_UNKNOWN',)

def test_benchmark_and_extreme_have_no_strength_override():
    assert market_vetoes('LONG','BEARISH','BEARISH','NORMAL')==('BENCHMARK_OPPOSITION',)
    assert market_vetoes('SHORT','BULLISH','BEARISH','EXTREME')==('EXTREME_VOLATILITY',)

@pytest.mark.parametrize('op,threshold,upper,expected',[
    ('GT','1',None,'FALSE'),('GTE','1',None,'TRUE'),('LT','1',None,'FALSE'),
    ('LTE','1',None,'TRUE'),('BETWEEN','0','1','TRUE'),('BETWEEN','2','3','FALSE')])
def test_numeric_predicate_truth_table(op,threshold,upper,expected):
    item=replace(_item(SourcePhaseV1.PHASE2,EvidenceTypeV1.OPEN_INTEREST_STRUCTURE,'OI_CHANGE','OI_BASE_CHANGE',EvidenceDirectionV1.NEUTRAL),numeric_value=D('1'),numeric_unit='RATIO')
    rule=PredicateRuleV1(predicate_id='n',source_phase='PHASE2',source_type='OI_CHANGE',evidence_type='OPEN_INTEREST_STRUCTURE',accepted_semantic_codes=('OI_BASE_CHANGE',),operator=op,threshold=D(threshold),upper_threshold=D(upper) if upper else None,unit='RATIO',missing_behavior='FAIL_CLOSED')
    assert _evaluate(rule,(item,),frozenset()).truth==expected
    assert _evaluate(rule,(replace(item,numeric_unit='USD'),),frozenset()).truth=='UNKNOWN'
    assert _evaluate(rule,(item,replace(item,numeric_value=D('2'))),frozenset()).truth=='UNKNOWN'

def test_profile_content_cannot_enable_v2_and_freezes_ttl():
    content=conservative_policy_content()
    assert {(p.pattern_type,p.timeframe,p.direction) for p in content.enabled_patterns}=={('BREAKOUT_CONFIRMATION','15m','LONG'),('BREAKOUT_CONFIRMATION','15m','SHORT')}
    assert all(p.strategy_profile==PROFILE.name for p in content.enabled_patterns)
    assert content.ttl_rules[0].ttl_seconds==600
    assert content.revalidation_rules[0].cadence_seconds==60

def test_profile_cannot_drop_a_core_predicate():
    from quant_phase9.policy import PolicyContentV1
    content=conservative_policy_content().model_dump(mode='json')
    content['enabled_patterns'][0]['required_predicates']=['setup_LONG']
    with pytest.raises(ValueError,match='core'):
        PolicyContentV1.model_validate(content)

def approved_conservative(tmp_path):
    from quant_phase9.canonical import canonical_json, canonical_sha256
    from quant_phase9.policy import load_approved_policy_manifest
    content=conservative_policy_content().model_dump(mode='json')
    digest=str(canonical_sha256(content))
    manifest=dict(schema='PHASE9_POLICY_MANIFEST_V1',manifest_version='1.0.0',created_at=NOW,policy_content=content,manifest_digest=digest)
    approval=dict(schema='PHASE9_POLICY_APPROVAL_V1',manifest_version='1.0.0',manifest_digest=digest,approval_status='APPROVED',approved_at=NOW,approved_by='test-fixture',approved_commit='b'*40)
    (tmp_path/'p.json').write_text(canonical_json(manifest))
    (tmp_path/'a.json').write_text(canonical_json(approval))
    return load_approved_policy_manifest(tmp_path/'p.json',tmp_path/'a.json',expected_commit='b'*40)

def core_snapshot(tmp_path, *, bad=None, side='LONG'):
    from tests.quant_phase9.test_evidence import _snapshot, _projection, AS_OF
    from quant_phase9.contracts import PolicyDataStatusV1
    b=bars(side); shift=AS_OF-b[-1].closed_at
    b=[replace(v,closed_at=v.closed_at+shift) for v in b]
    fs=flow_rows(side); delta=AS_OF-fs[-1]['window_close']
    fs=[dict(r,window_open=r['window_open']+delta,window_close=r['window_close']+delta) for r in fs]
    h=health(); h.update(checked_at=AS_OF,coverage_start=AS_OF-timedelta(hours=1),coverage_end=AS_OF+timedelta(hours=3))
    payloads=[('PHASE1','PAPER_BREAKOUT',dict(bars_15m=[dict(closed_at=v.closed_at,open=v.open,high=v.high,low=v.low,close=v.close,turnover=v.turnover) for v in b],bars_1h=[])),
        ('PHASE3','PAPER_PERP_FLOW',dict(rows=fs)),('PHASE7','PAPER_SPOT_FLOW',dict(rows=fs)),
        ('PHASE2','PAPER_OI_CHANGE',dict(rows=[dict(exchange='A',at=AS_OF-timedelta(minutes=15),base=D('100'),unit_verified=True),dict(exchange='A',at=AS_OF,base=D('100.3'),unit_verified=True)])),
        ('PHASE2','PAPER_FUNDING',dict(rate=D('0.0001'),interval=28800,normalized=D('0.0001'))),
        ('PHASE5','PAPER_MARKET_CONTEXT',dict(benchmark_1h='BULLISH' if side=='LONG' else 'BEARISH',benchmark_4h='BULLISH' if side=='LONG' else 'BEARISH',volatility='NORMAL')),
        ('PHASE6','PAPER_EVENT_RISK',dict(health=h,macro=[],security=[]))]
    if bad=='extreme': payloads[5][2]['volatility']='EXTREME'
    if bad=='unknown_flow': payloads[1][2]['rows'][-1]['coverage_complete']=False
    factories=[_projection(SourcePhaseV1(p),t,v) for p,t,v in payloads]
    factories.append(_projection(SourcePhaseV1.PHASE8,'OPTIONS_CONTEXT',{},status=PolicyDataStatusV1.NOT_CONFIGURED))
    approved=approved_conservative(tmp_path)
    return approved,_snapshot(factories,policy_manifest=approved)

@pytest.mark.parametrize('bad',[None,'extreme','unknown_flow'])
def test_core_aux_scope_and_hard_veto_consumer(tmp_path,bad):
    from quant_phase9.evidence import build_evidence
    from quant_phase9.validator import validate_evidence
    from quant_phase9.patterns import match_patterns
    approved,snapshot=core_snapshot(tmp_path,bad=bad)
    items=build_evidence(snapshot=snapshot,policy_manifest=approved)
    validation=validate_evidence(snapshot=snapshot,evidence_items=items,policy_manifest=approved)
    if bad is None:
        assert not validation.degraded and not validation.missing and not validation.hard_vetoes
        matches=match_patterns(evidence_items=items,validation=validation,policy_manifest=approved,timeframe='15m')
        assert [m.direction for m in matches if m.status=='MATCHED']==['LONG']
    elif bad=='extreme': assert 'EXTREME_VOLATILITY' in validation.hard_vetoes
    else: assert validation.degraded

def test_source_bundle_keeps_historical_clocks_and_unknown_coverage():
    from quant_phase9.sources.paper_v1 import bundle_projection
    from tests.quant_phase9.test_evidence import _projection, AS_OF
    from uuid import uuid4
    from quant_phase9.contracts import PolicyCoverageStatusV1
    raw=_projection(SourcePhaseV1.PHASE3,'TRADE_FLOW_WINDOW',dict(delta_base='1'))(uuid4())
    raw=replace(raw,coverage_status=PolicyCoverageStatusV1.UNKNOWN)
    result=bundle_projection((raw,),source_type='PAPER_PERP_FLOW',payload=dict(rows=[]),as_of=AS_OF)
    assert result.coverage_status=='UNKNOWN'
    assert result.canonical_payload['sources'][0]['observed_at']==raw.observed_at
    assert result.canonical_payload['sources'][0]['digest']==raw.canonical_digest


def source_reader_fixture(tmp_path,monkeypatch,*,regime_age=0,cvd_partial=False,missing_session=False,cvd_duplicate=False,funding_stale=False,context_partial=False):
    from types import SimpleNamespace
    from quant_phase9.sources import paper_v1 as reader
    from quant_phase9.sources import phase2,phase3,phase7
    import quant_instruments
    from tests.quant_phase9.test_evidence import AS_OF,_projection
    from quant_phase9.contracts import PolicyCoverageStatusV1,PolicyDataStatusV1
    from uuid import uuid4
    candidate=core_snapshot(tmp_path)[1].candidate_event
    def read(conn,query,params):
        if 'phase5_market_leader_context' in query:
            return [dict(trend_state='BULLISH',volatility_state='NORMAL',context_timestamp=AS_OF,status='AVAILABLE',freshness_status='AVAILABLE',data_quality={'source_status':'PARTIAL' if context_partial else 'AVAILABLE'},missing_count=0)]
        if 'phase5_market_regime_snapshots' in query:
            return [dict(volatility_regime='NORMAL',volatility_status='AVAILABLE',context_timestamp=AS_OF-timedelta(seconds=regime_age))]
        return []
    monkeypatch.setattr(reader,'read_rows',read)
    monkeypatch.setattr(quant_instruments,'resolve_core_instrument',lambda *args:SimpleNamespace(canonical_symbol='BTC-USDT-PERP'))
    funding=[]
    for venue in ('A','B'):
        item=_projection(SourcePhaseV1.PHASE2,'FUNDING_RATE',dict(exchange=venue,funding_rate=D('0.0001'),funding_interval_seconds=28800,normalized_8h_rate=D('0.0001')))(uuid4())
        if funding_stale and venue=='B': item=replace(item,freshness_status=__import__('quant_phase9.contracts',fromlist=['EvidenceFreshnessV1']).EvidenceFreshnessV1.STALE)
        funding.append(item)
    monkeypatch.setattr(phase2,'select_phase2',lambda *args:tuple(funding))
    raw=[]
    for i in range(3):
        end=AS_OF-timedelta(minutes=15*(2-i))
        for kind,payload in [('TRADE_FLOW_WINDOW',dict(exchange='A',window_open=end-timedelta(minutes=15),window_close=end,total_volume_base=D('100'),delta_base=D('6'),unknown_trade_count=0,session='s')),
                             ('CVD_SNAPSHOT',dict(exchange='A',window_end=end,value=D(6*(i+1)),session='s'))]:
            if missing_session: payload.pop('session',None);payload['aggregation_version']='v1'
            status=PolicyDataStatusV1.PARTIAL if cvd_partial and kind=='CVD_SNAPSHOT' else PolicyDataStatusV1.AVAILABLE
            item=_projection(SourcePhaseV1.PHASE3,kind,payload,status=status)(uuid4())
            item=replace(item,source_ref=f'{kind}:{i}',coverage_status=PolicyCoverageStatusV1.SOURCE_DECLARED_COMPLETE)
            raw.append(item)
            if cvd_duplicate and kind=='CVD_SNAPSHOT': raw.insert(len(raw)-1,replace(item,source_ref='duplicate:'+item.source_ref,canonical_payload=dict(item.canonical_payload,value=D('999'))))
    monkeypatch.setattr(phase3,'select_phase3',lambda *args:tuple(raw))
    monkeypatch.setattr(phase7,'select_phase7',lambda *args:())
    return {r.source_type:r for r in reader.PaperV1SourceReader().read(None,candidate=candidate,timeframe='15m',as_of=AS_OF)}

def test_stale_volatility_regime_cannot_be_promoted_by_fresh_benchmark(tmp_path,monkeypatch):
    result=source_reader_fixture(tmp_path,monkeypatch,regime_age=61)
    assert result['PAPER_MARKET_CONTEXT'].availability_status!='AVAILABLE'

def test_cvd_quality_and_digest_are_required_flow_provenance(tmp_path,monkeypatch):
    result=source_reader_fixture(tmp_path,monkeypatch,cvd_partial=True)
    flow=result['PAPER_PERP_FLOW']
    assert flow.availability_status!='AVAILABLE'
    assert len([s for s in flow.canonical_payload['sources'] if s['ref'].startswith('CVD_SNAPSHOT')])==3


def test_stale_funding_venue_is_core_even_when_another_is_fresh(tmp_path,monkeypatch):
    row=source_reader_fixture(tmp_path,monkeypatch,funding_stale=True)['PAPER_FUNDING']
    assert row.availability_status!='AVAILABLE' or row.freshness_status!='FRESH'

@pytest.mark.parametrize('case',['missing_session','cvd_duplicate'])
def test_flow_requires_real_session_and_unambiguous_cvd(tmp_path,monkeypatch,case):
    from tests.quant_phase9.test_evidence import AS_OF
    flow=source_reader_fixture(tmp_path,monkeypatch,**{case:True})['PAPER_PERP_FLOW']
    assert flow_confirmation(flow.canonical_payload['groups'][0],'LONG',as_of=AS_OF) is None

@pytest.mark.parametrize('security_bounds',['missing','expired'])
def test_security_health_needs_its_own_coverage_bounds(monkeypatch,security_bounds):
    from quant_phase9.sources import paper_v1 as reader
    details=dict(calendar_complete=True,security_complete=True,coverage_start=NOW-timedelta(hours=1),coverage_end=NOW+timedelta(hours=3))
    security=dict(details)
    if security_bounds=='missing': security.pop('coverage_end')
    else: security['coverage_end']=NOW-timedelta(seconds=1)
    rows=[dict(component='phase6-macro-ingestion',status='AVAILABLE',checked_at=NOW,details=details),dict(component='phase6-news-ingestion',status='AVAILABLE',checked_at=NOW,details=security)]
    monkeypatch.setattr(reader,'read_rows',lambda conn,q,p:rows if 'system_health' in q else [])
    state=reader.read_event_risk(None,symbol='BTCUSDT',now=NOW)
    assert event_risk(state['health'],state['macro'],state['security'],now=NOW)==('EVENT_RISK_UNKNOWN',)


def test_benchmark_source_quality_is_core(tmp_path,monkeypatch):
    row=source_reader_fixture(tmp_path,monkeypatch,context_partial=True)['PAPER_MARKET_CONTEXT']
    assert row.availability_status!='AVAILABLE'

def test_real_phase5_trend_vocabulary_reaches_opposition_veto():
    assert market_vetoes('LONG','TREND_DOWN','TREND_DOWN','NORMAL')==('BENCHMARK_OPPOSITION',)
    assert market_vetoes('SHORT','TREND_UP','TREND_UP','NORMAL')==('BENCHMARK_OPPOSITION',)
