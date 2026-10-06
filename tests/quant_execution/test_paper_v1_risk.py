from dataclasses import replace
from decimal import Decimal as D
from datetime import timedelta
import pytest

from quant_execution.paper_v1 import build_trade_plan, PaperCostsV1, normalize_stop_key
from quant_execution.risk import approve_intent, RiskRejected
from quant_execution.contracts import QuoteV1, intent_json, intent_from_json
from quant_phase9.contracts import ConfidenceBandV1
from tests.quant_execution.fixtures import risk_inputs
from tests.quant_phase9.test_paper_v1 import core_snapshot, bars
from tests.quant_phase9.test_evidence import AS_OF
from quant_phase9.sources import make_projection

def inputs(tmp_path,short=False):
    _,snapshot=core_snapshot(tmp_path,side='SHORT' if short else 'LONG')
    source=snapshot.source_projections[0]
    values=bars('SHORT' if short else 'LONG'); shift=AS_OF-values[-1].closed_at
    values=[dict(closed_at=v.closed_at+shift,open=v.open,high=v.high,low=v.low,close=v.close,turnover=v.turnover) for v in values]
    hour=[dict(closed_at=AS_OF-timedelta(hours=23-i),open='99',high='100',low='98',close='99',turnover='100') for i in range(24)]
    hour[10]['high']='108';hour[10]['low']='92'
    p=dict(source.canonical_payload);p.update(bars_15m=values,bars_1h=hour)
    from quant_phase9.canonical import canonical_sha256
    from quant_phase9.sources import projection_id_for
    digest=canonical_sha256(p)
    source=replace(source,canonical_payload=p,canonical_digest=digest,projection_id=projection_id_for(snapshot.evaluation_id,source.source_ref,digest))
    snapshot=replace(snapshot,source_projections=(source,*snapshot.source_projections[1:]))
    result=risk_inputs(AS_OF)
    candidate=replace(result['envelope'].candidate,evaluation_id=snapshot.evaluation_id,timeframe='15m',matched_pattern='BREAKOUT_CONFIRMATION',direction_bias='BEARISH' if short else 'BULLISH')
    result['envelope']=replace(result['envelope'],candidate=candidate,evaluation_snapshot_hash=str(snapshot.snapshot_digest))
    result['quote']=QuoteV1('BTC-USDT-PERP',D('100.1') if not short else D('99.7'),D('100.15') if not short else D('99.75'),AS_OF,'AVAILABLE')
    result['instrument']=replace(result['instrument'],price_tick=D('0.01'))
    result['policy']=replace(result['policy'],max_spread_bps=D('8'),max_slippage_bps=D('10'))
    result['account']=replace(result['account'],equity=D('1000'),available_balance=D('1000'))
    return snapshot,result

@pytest.mark.parametrize('short',[False,True])
def test_dynamic_plan_risk_equity_and_digest_roundtrip(tmp_path,short):
    snapshot,kw=inputs(tmp_path,short)
    plan=build_trade_plan(snapshot,kw['envelope'].candidate,kw['instrument'],kw['quote'],PaperCostsV1(D('0.0004'),D('0.0004'),D('0.0002')),now=AS_OF)
    assert plan.net_R>=D('1.5')
    assert plan.stop_price<kw['quote'].ask if not short else plan.stop_price>kw['quote'].bid
    intent=approve_intent(**dict(kw,stop_price=plan.stop_price,trade_plan=plan))
    assert intent.max_notional<=D('100') and intent.risk_budget<=D('2.5')
    assert intent.max_leverage==D('1') and intent.valid_until<=AS_OF+timedelta(seconds=30)
    assert intent.target_price==plan.target_price and intent.max_hold_seconds==10800
    assert intent_from_json(intent_json(intent))==intent

@pytest.mark.parametrize('failure',['medium','chase','netR','cost_unknown','core_changed','event_stale'])
def test_plan_fail_closed(tmp_path,failure):
    snapshot,kw=inputs(tmp_path)
    costs=PaperCostsV1(D('0.0004'),D('0.0004'),D('0.0002'))
    now=AS_OF
    if failure=='medium': kw['envelope']=replace(kw['envelope'],candidate=replace(kw['envelope'].candidate,confidence_band=ConfidenceBandV1.MEDIUM))
    if failure=='chase': kw['quote']=replace(kw['quote'],bid=D('102'),ask=D('102.05'))
    if failure=='netR': costs=PaperCostsV1(D('0.05'),D('0.05'),D('0.01'))
    if failure=='cost_unknown': costs=None
    if failure=='core_changed': snapshot=replace(snapshot,evaluation_id=kw['envelope'].candidate.decision_id)
    if failure=='event_stale': now+=timedelta(seconds=61)
    with pytest.raises((ValueError,RiskRejected)):
        build_trade_plan(snapshot,kw['envelope'].candidate,kw['instrument'],kw['quote'],costs,now=now)

@pytest.mark.parametrize('direction,expected',[('BULLISH','LONG'),('BEARISH','SHORT'),('LONG','LONG'),('SHORT','SHORT')])
def test_stop_direction_key_normalization(direction,expected):
    assert normalize_stop_key('BREAKOUT_CONFIRMATION|15m|'+direction)=='BREAKOUT_CONFIRMATION|15m|'+expected

def test_plan_rechecks_core_numeric_fact(tmp_path):
    snapshot,kw=inputs(tmp_path)
    source=snapshot.source_projections[3]; p=dict(source.canonical_payload)
    p['rows']=[dict(r,base='100') for r in p['rows']]
    snapshot=replace(snapshot,source_projections=(*snapshot.source_projections[:3],replace(source,canonical_payload=p),*snapshot.source_projections[4:]))
    with pytest.raises(ValueError,match='CORE'):
        build_trade_plan(snapshot,kw['envelope'].candidate,kw['instrument'],kw['quote'],PaperCostsV1(D('0'),D('0'),D('0')),now=AS_OF)
