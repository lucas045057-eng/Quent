from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from uuid import uuid4

import pytest

from quant_features.core import FeatureObservationV1, project_features, research_frame, as_of_features
from quant_phase9.features import snapshot_features
from quant_phase9.contracts import SourceProjectionV1
from quant_phase9.canonical import canonical_sha256
from tests.quant_execution.fixtures import NOW


def observation(kind='OI', *, value=D('1000000'), known=NOW, status='AVAILABLE'):
    units = {'PRICE':'USDT','OI':'USD','FUNDING':'RATE','TAKER':'BASE','CVD':'BASE',
        'LIQUIDATION':'USD','NEWS':'COUNT','OPTIONS':'CONTEXT','ONCHAIN':'CONTEXT'}
    return FeatureObservationV1(kind, 'BTC-USDT-PERP', value, units[kind],
        NOW - timedelta(minutes=15), NOW, known, NOW, known, status, 'FRESH', 'VALID',
        'COMPLETE', 'EXCHANGE_PUBLIC', 'fixture:provenance', 'phase2:open_interest/1',
        '1', 'SYNTHETIC_FIXTURE')


@pytest.mark.parametrize('kind', ['PRICE','OI','FUNDING','TAKER','CVD','LIQUIDATION','NEWS','OPTIONS','ONCHAIN'])
def test_every_feature_hides_future_event_and_future_knowledge(kind):
    item = observation(kind)
    future = replace(item, known_at=NOW+timedelta(seconds=1), processed_at=NOW+timedelta(seconds=1))
    assert as_of_features(project_features((future,)), NOW) == ()
    future = replace(item, window_end=NOW+timedelta(seconds=1))
    assert as_of_features(project_features((future,)), NOW) == ()
    assert len(as_of_features(project_features((item,)), NOW)) == 1


def test_backward_asof_never_selects_future_revision_or_turns_missing_into_zero():
    old_time = NOW-timedelta(minutes=1)
    old = replace(observation(), window_end=old_time, known_at=old_time,
        received_at=old_time, processed_at=old_time)
    newer = replace(observation(value=D('2000000')), known_at=NOW+timedelta(seconds=1), revision='2')
    rows = project_features((old,newer))
    assert research_frame(rows,NOW).value('BTC-USDT-PERP','OI') == D('1000000')
    missing = project_features((observation(value=None,status='NOT_AVAILABLE'),))
    assert research_frame(missing,NOW).value('BTC-USDT-PERP','OI') is None
    unknown = project_features((replace(observation(value=D('0')),quality='UNKNOWN'),))
    assert research_frame(unknown,NOW).value('BTC-USDT-PERP','OI') is None
    partial = project_features((replace(observation(),coverage='PARTIAL',kind='LIQUIDATION'),))
    assert research_frame(partial,NOW).value('BTC-USDT-PERP','LIQUIDATION') is None


def test_snapshot_boundary_and_research_use_the_identical_feature_definition():
    from types import SimpleNamespace
    payload = {'open_interest_usd':D('1000000'),'normalization_method':'exchange_reported_quote_notional',
        'authority':'EXCHANGE_PUBLIC','provenance':'fixture:provenance','pit_status':'SYNTHETIC_FIXTURE'}
    projection = SourceProjectionV1(uuid4(),uuid4(),'PHASE2','OPEN_INTEREST',
        'phase2:open_interest/1','BTCUSDT','USDT_PERPETUAL',NOW,NOW,NOW,NOW,NOW,
        'AVAILABLE','FRESH','VALID','COMPLETE',payload,'PHASE9_SOURCE_PROJECTION_V1','1',
        canonical_sha256(payload))
    snapshot = SimpleNamespace(source_projections=(projection,),as_of=NOW)
    actual = snapshot_features(snapshot, canonical_symbol='BTC-USDT-PERP')
    assert actual[0].value == D('1000000')
    assert actual[0].definition_digest == project_features((observation(),))[0].definition_digest
    frame = research_frame(actual,NOW)
    assert frame.value('BTC-USDT-PERP','OI') == D('1000000')
    assert actual[0].authority == 'EXCHANGE_PUBLIC'
    assert actual[0].source_ref == 'phase2:open_interest/1'


def test_retrospective_rows_are_explicitly_unverified_for_research():
    features = project_features((replace(observation(),pit_status='PIT_UNVERIFIED'),))
    assert research_frame(features,NOW,require_pit=True).value('BTC-USDT-PERP','OI') is None


@pytest.mark.parametrize(('source_type','data','expected'), [
    ('PRICE_OBSERVATION', {'value': '50000', 'unit': 'USDT'}, D('50000')),
    ('PRICE_TICKER', {'last_price': None, 'lastPr': '50000'}, D('50000')),
    ('CLOSED_KLINE', {'close': '50000', 'bar_open_timestamp': NOW-timedelta(minutes=15),
                     'bar_close_timestamp': NOW}, D('50000')),
    ('TRADE_FLOW_WINDOW', {'delta_base': '10', 'unknown_trade_count': 0,
                           'window_open': NOW-timedelta(minutes=15), 'window_close': NOW}, D('10')),
    ('CVD_SNAPSHOT', {'value': '12', 'window_end': NOW}, D('12')),
    ('LIQUIDATION_WINDOW', {'convertible_notional_usd': '1000', 'window_end': NOW}, D('1000')),
])
def test_real_producer_payload_survives_durable_json_round_trip(source_type, data, expected):
    import json
    from types import SimpleNamespace
    from quant_phase9.canonical import canonical_json

    payload = {**data, 'authority': 'EXCHANGE_PUBLIC', 'provenance': 'fixture:source',
               'pit_status': 'SYNTHETIC_FIXTURE'}
    projection = SourceProjectionV1(uuid4(),uuid4(),'PHASE1',source_type,
        'phase1:fixture/1','BTCUSDT','USDT_PERPETUAL',NOW,NOW,NOW,NOW,NOW,
        'AVAILABLE','FRESH','VALID','COMPLETE',payload,'PHASE9_SOURCE_PROJECTION_V1','1',
        canonical_sha256(payload))
    snapshot = SimpleNamespace(source_projections=(projection,),as_of=NOW)
    before = snapshot_features(snapshot, canonical_symbol='BTC-USDT-PERP')
    restored = replace(projection, canonical_payload=json.loads(canonical_json(payload)))
    after = snapshot_features(SimpleNamespace(source_projections=(restored,),as_of=NOW),
                              canonical_symbol='BTC-USDT-PERP')
    assert len(before) == len(after) == 1
    assert before[0].value == after[0].value == expected
    assert canonical_json(before) == canonical_json(after)


def test_unknown_directional_coverage_never_defaults_to_no_unknown_trades():
    from types import SimpleNamespace
    payload = {'delta_base': '10', 'authority': 'EXCHANGE_PUBLIC'}
    projection = SourceProjectionV1(uuid4(),uuid4(),'PHASE3','TRADE_FLOW_WINDOW',
        'phase3:trade_flow_windows/1','BTCUSDT','USDT_PERPETUAL',NOW,NOW,NOW,NOW,NOW,
        'AVAILABLE','FRESH','VALID','COMPLETE',payload,'PHASE9_SOURCE_PROJECTION_V1','1',
        canonical_sha256(payload))
    actual = snapshot_features(SimpleNamespace(source_projections=(projection,),as_of=NOW),
                               canonical_symbol='BTC-USDT-PERP')
    assert actual[0].value is None
