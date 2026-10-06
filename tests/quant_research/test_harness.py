from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

from quant_features.core import project_features, research_frame
from quant_research.harness import ResearchSampleV1, HypothesisConfigV1, directions, walk_forward, compare_hypotheses
from tests.quant_research.test_features import observation
from tests.quant_execution.fixtures import NOW


def frame(when, *, price='50000',oi='1000000',flow='10',cvd='100',liq='1000',funding='0.0001'):
    values = {'PRICE':price,'OI':oi,'FUNDING':funding,'TAKER':flow,'CVD':cvd,'LIQUIDATION':liq}
    rows = [replace(observation(kind,value=D(value)),window_start=when-timedelta(minutes=1),
        window_end=when,known_at=when,received_at=when,processed_at=when)
        for kind,value in values.items()]
    return research_frame(project_features(rows),when)


def sample(when=NOW):
    return ResearchSampleV1('BTC-USDT-PERP',when,when+timedelta(minutes=15),
        frame(when-timedelta(minutes=1)),frame(when,price='51000',oi='1020000',cvd='110'),
        D('0.01'),D('0.0001'),'TREND')


def test_five_predeclared_hypotheses_and_price_control_have_exact_direction():
    config = HypothesisConfigV1(D('0.001'),D('0.01'),D('0.0005'),D('1000'))
    actual = directions(sample(),config)
    assert actual == {'PRICE_ONLY':1,'PRICE_OI':1,'PRICE_OI_FUNDING':1,
        'PRICE_OI_TAKER':1,'BREAKOUT_OI_CVD':1,'LIQUIDATION_OI_RESET_FLOW_REVERSAL':0}
    reversal = replace(sample(),previous=frame(NOW-timedelta(minutes=1),flow='-10'),
        current=frame(NOW,price='51000',oi='900000',flow='10'))
    assert directions(reversal,config)['LIQUIDATION_OI_RESET_FLOW_REVERSAL'] == 1


def test_common_eligible_cohort_excludes_missing_feature_for_every_hypothesis():
    good = sample()
    bad_time = NOW+timedelta(hours=1)
    bad_features = tuple(feature for feature in sample(bad_time).current.features if feature.kind != 'OI')
    bad = replace(sample(bad_time),current=research_frame(bad_features,bad_time))
    report = compare_hypotheses((good,bad),HypothesisConfigV1(D('0.001'),D('0.01'),D('0.0005'),D('1000')),
        maker_bps=D('2'),taker_bps=D('5'),spread_bps=D('2'),slippage_bps=D('3'))
    assert report['eligible_count'] == 1
    assert len({row['cohort_digest'] for row in report['hypotheses'].values()}) == 1
    assert report['hypotheses']['PRICE_ONLY']['net_return_sum'] == D('0.0079')
    assert report['production_policy_changed'] is False


def test_walk_forward_purges_overlapping_labels_and_enforces_embargo():
    rows = tuple(sample(NOW+timedelta(minutes=10*index)) for index in range(12))
    folds = walk_forward(rows,test_size=3,min_train=2,embargo_seconds=300)
    assert folds
    for training,testing in folds:
        assert all(row.label_end < testing[0].as_of-timedelta(seconds=300) for row in training)
        assert max(row.as_of for row in training) < min(row.as_of for row in testing)


def test_unknown_funding_cost_cannot_be_treated_as_zero_net_return():
    row = replace(sample(),settled_funding_rate=None)
    report = compare_hypotheses((row,),HypothesisConfigV1(D('0.001'),D('0.01'),D('0.0005'),D('1000')),
        maker_bps=D('2'),taker_bps=D('5'),spread_bps=D('2'),slippage_bps=D('3'))
    assert report['hypotheses']['PRICE_ONLY']['net_return_sum'] is None
    assert report['hypotheses']['PRICE_ONLY']['unresolved_costs'] == 1


def test_breakout_checks_the_known_prior_range_not_only_the_last_price():
    row=sample()
    high=replace(observation('PRICE',value=D('52000')),window_start=NOW-timedelta(minutes=3),
        window_end=NOW-timedelta(minutes=2),known_at=NOW-timedelta(minutes=2),
        received_at=NOW-timedelta(minutes=2),processed_at=NOW-timedelta(minutes=2),
        source_ref='fixture:price/high')
    previous=research_frame(row.previous.features+project_features((high,)),row.previous.as_of)
    row=replace(row,previous=previous)
    actual=directions(row,HypothesisConfigV1(D('0.001'),D('0.01'),D('0.0005'),D('1000')))
    assert actual['PRICE_OI']==1
    assert actual['BREAKOUT_OI_CVD']==0  # 51000 remains inside the known range.
