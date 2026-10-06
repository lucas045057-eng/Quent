from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import pytest

from quant_execution.funding import FundingObservationV1, FundingPositionV1, calculate_funding, FundingLedger
from tests.quant_execution.fixtures import NOW


def facts(quantity=D('0.02'), rate=D('0.0001')):
    observation = FundingObservationV1('BTC-USDT-PERP',NOW,rate,D('50000'),'USDT',
        NOW,NOW,'SETTLED','AVAILABLE','VALID','EXCHANGE_PUBLIC','phase2:funding/1','fixture:provenance')
    position = FundingPositionV1('fixture-account','BTC-USDT-PERP',quantity,NOW,'RECONCILED','BASE')
    return observation,position


@pytest.mark.parametrize('quantity,rate,cash', [
    ('0.02','0.0001','-0.1'),('-0.02','0.0001','0.1'),
    ('0.02','-0.0001','0.1'),('-0.02','-0.0001','-0.1'),
    ('0','0.0001','0'),
])
def test_funding_payment_sign_uses_actual_position_at_boundary(quantity,rate,cash):
    observation,position = facts(D(quantity),D(rate))
    payment = calculate_funding(observation,position,now=NOW)
    assert payment.status == 'RESOLVED'
    assert payment.cash == D(cash)
    assert payment.signed_quantity == D(quantity)


@pytest.mark.parametrize('change,reason', [
    ({'rate':None},'RATE_UNAVAILABLE'),({'mark_price':None},'MARK_UNAVAILABLE'),
    ({'status':'PARTIAL'},'SOURCE_UNAVAILABLE'),({'quality':'UNKNOWN'},'SOURCE_UNAVAILABLE'),
    ({'authority':''},'SOURCE_UNAVAILABLE'),
    ({'classification':'PREDICTED'},'UNSETTLED_RATE'),
    ({'known_at':NOW+timedelta(seconds=1)},'NOT_YET_KNOWN'),
])
def test_missing_or_future_funding_is_unresolved_not_zero(change,reason):
    observation,position = facts()
    payment = calculate_funding(replace(observation,**change),position,now=NOW)
    assert payment.cash is None
    assert payment.status == 'UNRESOLVED'
    assert payment.reason == reason


def test_old_or_unknown_position_cannot_be_assumed_held_at_boundary():
    observation,position = facts()
    assert calculate_funding(observation,None,now=NOW).cash is None
    assert calculate_funding(observation,replace(position,as_of=NOW-timedelta(seconds=1)),now=NOW).cash is None
    assert calculate_funding(observation,replace(position,reconciliation_status='UNKNOWN'),now=NOW).cash is None


def test_payment_key_and_memory_ledger_are_idempotent_across_replay():
    observation,position = facts()
    first = calculate_funding(observation,position,now=NOW)
    later = calculate_funding(observation,position,now=NOW+timedelta(seconds=5))
    assert first == later
    ledger = FundingLedger()
    assert ledger.add(first) is True
    assert ledger.add(later) is False
    assert ledger.total_cash == D('-0.1')
    with pytest.raises(ValueError,match='conflict'):
        ledger.add(calculate_funding(replace(observation,rate=D('0.0002')),position,now=NOW))
