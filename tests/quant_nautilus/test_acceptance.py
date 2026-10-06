from decimal import Decimal as D
import pytest

from quant_nautilus.acceptance import fixture_case, run_backtest_acceptance
from quant_phase9.runtime import _load_snapshot
from quant_phase9.canonical import canonical_json
import json


@pytest.mark.parametrize('symbol,side,pattern', [
    ('BTCUSDT','LONG','TREND_CONTINUATION'),
    ('BTCUSDT','SHORT','BREAKOUT_CONFIRMATION'),
    ('ETHUSDT','LONG','BREAKOUT_CONFIRMATION'),
    ('ETHUSDT','SHORT','TREND_CONTINUATION'),
])
def test_full_fixture_phase9_risk_native_cost_chain(tmp_path,symbol,side,pattern):
    case = fixture_case(tmp_path,symbol=symbol,side=side,pattern=pattern)
    assert case.decision.eligible and case.decision.matched_pattern == pattern
    assert case.intent.decision_id == case.decision.decision_id
    assert len(case.evidence) == 6
    assert not case.chain.missing and not case.chain.degraded
    report = run_backtest_acceptance((case,))
    assert report['acceptance_kind'] == 'FIXTURE_DRIVEN_ACCEPTANCE'
    assert report['passed'] is True
    trade = report['cases'][0]
    assert trade['canonical_symbol'] == case.intent.canonical_symbol
    assert trade['side'] == side and trade['pattern'] == pattern
    assert D(trade['fees']) > 0 and D(trade['funding_cash']) != 0
    assert D(trade['net_pnl']) == D(trade['account_change'])
    again = run_backtest_acceptance((case,))
    assert again['digest'] == report['digest']


def test_acceptance_snapshot_uses_the_real_durable_snapshot_schema_and_digest(tmp_path):
    case=fixture_case(tmp_path)
    raw=json.loads(canonical_json(case.snapshot))
    raw['schema']='PHASE9_EVALUATION_SNAPSHOT_V1'
    assert canonical_json(_load_snapshot(raw))==canonical_json(case.snapshot)
