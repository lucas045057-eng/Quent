from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from quant_execution.contracts import ExecutionIntentV1, intent_digest, intent_from_json, intent_json
from quant_execution.risk import RiskRejected, approve_intent
from tests.quant_execution.fixtures import NOW, risk_inputs


def test_risk_produces_exact_quantity_budget_and_reproducible_intent():
    inputs = risk_inputs()
    intent = approve_intent(**inputs)
    assert isinstance(intent, ExecutionIntentV1)
    assert intent.approved_quantity == D('0.019')
    assert intent.reference_price == D('50001')
    assert intent.risk_budget == D('2.850019')
    assert intent.max_notional == D('950.969019')
    assert intent.stop_price == D('49901')
    assert intent.protection_required is True
    assert intent.valid_until == NOW + timedelta(seconds=60)
    assert intent.intent_id == approve_intent(**inputs).intent_id
    assert intent.content_digest == intent_digest(intent)
    assert intent_from_json(intent_json(intent)) == intent
    with pytest.raises(ValueError, match='digest'):
        replace(intent, approved_quantity=D('0.020'))


@pytest.mark.parametrize('field,value,code', [
    ('eligible', False, 'INELIGIBLE'), ('valid_until', NOW, 'EXPIRED'),
    ('created_at', NOW + timedelta(seconds=1), 'FUTURE_DECISION'),
    ('decision_policy_version', 'unknown', 'UNSUPPORTED_VERSION'),
    ('pattern_policy_version', 'unknown', 'UNSUPPORTED_VERSION'),
    ('freshness_policy_version', 'unknown', 'UNSUPPORTED_VERSION'),
    ('ttl_policy_version', 'unknown', 'UNSUPPORTED_VERSION'),
    ('evidence_schema_version', 'unknown', 'UNSUPPORTED_VERSION'),
    ('prompt_version', 'unknown', 'UNSUPPORTED_VERSION'),
    ('code_version', 'c' * 40, 'UNSUPPORTED_VERSION'),
    ('input_snapshot_hash', 'f' * 64, 'INPUT_HASH_MISMATCH'),
    ('veto_reasons', ('RISK_VETO',), 'INELIGIBLE'),
])
def test_candidate_safety_gates_fail_closed(field, value, code):
    inputs = risk_inputs()
    inputs['envelope'] = replace(inputs['envelope'], candidate=replace(inputs['envelope'].candidate, **{field: value}))
    with pytest.raises(RiskRejected, match=code):
        approve_intent(**inputs)


@pytest.mark.parametrize('part,changes,code', [
    ('envelope', {'effective_status': 'INVALIDATED'}, 'INACTIVE'),
    ('quote', {'as_of': NOW - timedelta(seconds=6)}, 'STALE_QUOTE'),
    ('quote', {'as_of': NOW + timedelta(seconds=1)}, 'STALE_QUOTE'),
    ('quote', {'status': 'UNKNOWN'}, 'QUOTE_UNAVAILABLE'),
    ('quote', {'ask': D('51000')}, 'SPREAD'),
    ('account', {'as_of': NOW - timedelta(seconds=6)}, 'STALE_ACCOUNT'),
    ('account', {'reconciliation_status': 'UNKNOWN'}, 'ACCOUNT_UNRESOLVED'),
    ('account', {'mode': 'LIVE'}, 'UNSUPPORTED_MODE'),
    ('account', {'open_intents': 2}, 'CONCURRENCY_LIMIT'),
    ('account', {'reserved_risk': D('20')}, 'RISK_LIMIT'),
    ('account', {'exposure': D('2000')}, 'EXPOSURE_LIMIT'),
    ('account', {'available_balance': D('1')}, 'BELOW_MINIMUM'),
    ('instrument', {'tradable': False}, 'INSTRUMENT_DISABLED'),
    ('instrument', {'venue': 'OTHER'}, 'BINDING_MISMATCH'),
])
def test_freshness_account_and_capacity_gates(part, changes, code):
    inputs = risk_inputs()
    inputs[part] = replace(inputs[part], **changes)
    with pytest.raises(RiskRejected, match=code):
        approve_intent(**inputs)


def test_stop_required_and_short_budget_is_conservative():
    inputs = risk_inputs()
    inputs['stop_price'] = D('50010')
    with pytest.raises(RiskRejected, match='STOP_WRONG_SIDE'):
        approve_intent(**inputs)
    inputs['envelope'] = replace(inputs['envelope'], candidate=replace(
        inputs['envelope'].candidate, direction_bias='BEARISH'))
    inputs['stop_price'] = D('50101')
    intent = approve_intent(**inputs)
    assert intent.side == 'SHORT'
    assert intent.approved_quantity == D('0.019')
    assert intent.risk_budget <= D('10')


def test_unknown_or_extra_execution_schema_fields_are_rejected():
    import json
    payload = json.loads(intent_json(approve_intent(**risk_inputs())))
    payload['nautilus_order'] = 'forbidden'
    with pytest.raises(ValueError, match='fields'):
        intent_from_json(json.dumps(payload))


def test_intent_cannot_claim_more_quantity_than_its_notional_and_risk_budget():
    from quant_execution.contracts import intent_body, make_intent
    body = intent_body(approve_intent(**risk_inputs()))
    body['approved_quantity'] = D('0.038')
    with pytest.raises(ValueError, match='economic constraints'):
        make_intent(**body)


def test_execution_quote_ttl_cannot_be_relaxed_past_five_seconds():
    inputs = risk_inputs()
    inputs['policy'] = replace(inputs['policy'], quote_max_age_seconds=30)
    inputs['quote'] = replace(inputs['quote'], as_of=NOW - timedelta(seconds=6))
    with pytest.raises(RiskRejected, match='STALE_QUOTE'):
        approve_intent(**inputs)


def test_bitget_execution_quote_uses_fresh_receipt_and_bounds_exchange_clock_skew():
    inputs = risk_inputs()
    inputs['quote'] = replace(inputs['quote'], received_at=NOW,
        source_as_of=NOW + timedelta(seconds=1.5), source='bitget_v3_ws', exchange='bitget')
    intent = approve_intent(**inputs)
    assert intent.quote_as_of == NOW

    inputs['quote'] = replace(inputs['quote'], source_as_of=NOW + timedelta(seconds=2.001))
    with pytest.raises(RiskRejected, match='STALE_QUOTE'):
        approve_intent(**inputs)

    inputs['quote'] = replace(inputs['quote'], source_as_of=NOW - timedelta(seconds=5.001))
    with pytest.raises(RiskRejected, match='STALE_QUOTE'):
        approve_intent(**inputs)


def test_unapproved_execution_quote_source_gets_no_future_skew_tolerance():
    inputs = risk_inputs()
    inputs['quote'] = replace(inputs['quote'], received_at=NOW,
        source_as_of=NOW + timedelta(milliseconds=1), source='other_exchange_ws', exchange='other')
    with pytest.raises(RiskRejected, match='STALE_QUOTE'):
        approve_intent(**inputs)
