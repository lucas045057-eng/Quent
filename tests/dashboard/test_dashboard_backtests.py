import hashlib
import json

import pytest


def report_file(tmp_path, **changes):
    body = {'schema':'QUANT_BACKTEST_ACCEPTANCE_V1','acceptance_kind':'FIXTURE_DRIVEN_ACCEPTANCE',
        'strategy_edge_status':'NOT_VALIDATED','passed':True,'nautilus_version':'1.231.0',
        'cases':[{'canonical_symbol':'BTC-USDT-PERP','side':'LONG','net_pnl':'-2.39366142',
            'fees':'0.34166142','funding_cash':'-0.09500000','quantity':'0.019'}]}
    body.update(changes)
    from quant_phase9.canonical import canonical_sha256
    body['digest'] = str(canonical_sha256(body))
    scope = tmp_path/'artifacts/phase9'
    scope.mkdir(parents=True,exist_ok=True)
    target = scope/'backtest.json'
    target.write_text(json.dumps(body))
    return target


def reader(tmp_path):
    from dashboard.backend.files import FileCatalog
    from dashboard.backend.backtests import BacktestReader
    return BacktestReader(FileCatalog(tmp_path))


def test_actual_acceptance_pnl_with_no_invented_curve_or_metrics(tmp_path):
    report_file(tmp_path)
    runs = reader(tmp_path).list()['data']['items']
    assert len(runs) == 1
    detail = reader(tmp_path).detail(runs[0]['run_id'])['data']
    assert detail['metrics']['net_pnl'] == '-2.39366142'
    assert detail['acceptance_kind'] == 'FIXTURE_DRIVEN_ACCEPTANCE'
    assert detail['strategy_edge_status'] == 'NOT_VALIDATED'
    assert detail['equity_curve'] == [] and detail['drawdown_curve'] == []
    assert detail['trades'] == []
    for key in ('initial_equity','final_equity','total_return','max_drawdown','sharpe','sortino','win_rate','profit_factor','trade_count'):
        assert detail['metrics'][key] is None


def test_actual_timestamped_curve_has_sampled_drawdown_only(tmp_path):
    report_file(tmp_path,schema='QUANT_BACKTEST_REPORT_V1',cases=[],metrics={'sharpe':'0.7'},
        equity_curve=[{'time':'2026-09-01T00:00:00Z','equity':'1000'},
            {'time':'2026-09-01T01:00:00Z','equity':'900'},
            {'time':'2026-09-01T02:00:00Z','equity':'1100'}])
    run = reader(tmp_path).list()['data']['items'][0]
    detail = reader(tmp_path).detail(run['run_id'])['data']
    assert [row['drawdown_pct'] for row in detail['drawdown_curve']] == ['0','-10.0','0']
    assert detail['metrics']['sharpe'] == '0.7'
    assert detail['curve_basis'] == 'RECORDED_TIMESTAMPED_EQUITY_SAMPLED_DRAWDOWN'


def test_independent_cases_are_independent_runs_not_a_curve(tmp_path):
    report_file(tmp_path,cases=[{'canonical_symbol':'BTC-USDT-PERP','side':'LONG','net_pnl':'-2'},
        {'canonical_symbol':'ETH-USDT-PERP','side':'SHORT','net_pnl':'-5'}])
    runs = reader(tmp_path).list()['data']['items']
    assert len(runs) == 2 and runs[0]['run_id'] != runs[1]['run_id']
    for run in runs:
        assert reader(tmp_path).detail(run['run_id'])['data']['equity_curve'] == []


def test_corrupt_digest_is_error_and_not_a_valid_run(tmp_path):
    path = report_file(tmp_path)
    data = json.loads(path.read_text())
    data['cases'][0]['net_pnl'] = '1000000'
    path.write_text(json.dumps(data))
    listing = reader(tmp_path).list()
    assert listing['data']['items'] == []
    assert 'BACKTEST_DIGEST_MISMATCH' in listing['warnings']


@pytest.mark.parametrize('curve', [
    [{'time':'2026-01-01T00:00:00Z','equity':'NaN'}],
    [{'time':'2026-01-01T01:00:00Z','equity':'2'},{'time':'2026-01-01T00:00:00Z','equity':'1'}],
    [{'time':'invalid','equity':'100'}],
])
def test_invalid_or_out_of_order_equity_is_not_charted(tmp_path, curve):
    report_file(tmp_path,schema='QUANT_BACKTEST_REPORT_V1',cases=[],equity_curve=curve)
    run = reader(tmp_path).list()['data']['items'][0]
    detail = reader(tmp_path).detail(run['run_id'])
    assert detail['data']['equity_curve'] == []
    assert 'INVALID_EQUITY_SERIES' in detail['warnings']


def test_backtest_path_cannot_be_used_as_run_id(tmp_path):
    report_file(tmp_path)
    assert reader(tmp_path).detail('../../secret.json')['availability'] == 'NO_DATA'
