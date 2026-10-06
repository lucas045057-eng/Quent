"""Actual report PnL and recorded series; never derives a curve from case PnL."""
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from .models import envelope, timestamp

METRICS = ('initial_equity','final_equity','total_return','net_pnl','max_drawdown',
    'sharpe','sortino','win_rate','profit_factor','trade_count','fees','funding_cash','spread_cost','slippage_cost')


class BacktestReader:
    def __init__(self, catalog):
        self.catalog = catalog

    def _scan(self):
        runs, warnings = [], []
        for ref in self.catalog.scan():
            if not ref.endswith('.json'):
                continue
            try:
                report = self.catalog.read_json(ref)
            except ValueError:
                continue
            if report.get('schema') not in {'QUANT_BACKTEST_ACCEPTANCE_V1','QUANT_BACKTEST_REPORT_V1'}:
                continue
            body = {key:value for key,value in report.items() if key != 'digest'}
            digest = hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
            if not isinstance(report.get('digest'),str) or report['digest'] != digest:
                warnings.append('BACKTEST_DIGEST_MISMATCH')
                continue
            cases = report.get('cases')
            if report['schema'] == 'QUANT_BACKTEST_ACCEPTANCE_V1' and (not isinstance(cases,list) or not cases):
                warnings.append('INVALID_BACKTEST_CASES')
                continue
            records = cases if report['schema'] == 'QUANT_BACKTEST_ACCEPTANCE_V1' else [report]
            for index, record in enumerate(records[:200]):
                if not isinstance(record, dict):
                    continue
                raw_metrics = record.get('metrics', record)
                if not isinstance(raw_metrics, dict):
                    warnings.append('INVALID_BACKTEST_METRICS')
                    continue
                identifier = hashlib.sha256(f'{ref}:{digest}:{index}'.encode()).hexdigest()[:24]
                metrics = {key:raw_metrics.get(key) for key in METRICS}
                runs.append(dict(run_id=identifier, source=ref, report_digest=digest,
                    canonical_symbol=record.get('canonical_symbol'), side=record.get('side'),
                    strategy=record.get('pattern',record.get('strategy')), started_at=record.get('started_at'),
                    ended_at=record.get('ended_at'), timeframe=record.get('timeframe'),
                    acceptance_kind=report.get('acceptance_kind'), strategy_edge_status=report.get('strategy_edge_status'),
                    status='PASSED' if report.get('passed') is True else ('FAILED' if report.get('passed') is False else 'NOT_RECORDED'),
                    nautilus_version=report.get('nautilus_version'), metrics=metrics,
                    quantity=record.get('quantity'), cost_assumption=record.get('cost_assumption'),
                    fee_assumption=record.get('fee_assumption'), policy_scope=report.get('policy_scope'),
                    decision_id=record.get('decision_id'), intent_id=record.get('intent_id'),
                    _record=record, _acceptance=report['schema'] == 'QUANT_BACKTEST_ACCEPTANCE_V1'))
        return runs[:200], sorted(set(warnings))

    def list(self, limit=100):
        runs, warnings = self._scan()
        rows = [{key:value for key,value in run.items() if not key.startswith('_')} for run in runs]
        return envelope(dict(items=rows[:limit]), 'AVAILABLE' if rows else ('ERROR' if warnings else 'NO_DATA'),
            sorted({row['source'] for row in rows}), warnings)

    def detail(self, run_id):
        if not re.fullmatch(r'[0-9a-f]{24}',run_id):
            return envelope(None, 'NO_DATA')
        runs, warnings = self._scan()
        run = next((item for item in runs if item['run_id'] == run_id), None)
        if run is None:
            return envelope(None, 'NO_DATA', warnings=warnings)
        record = run.pop('_record')
        acceptance = run.pop('_acceptance')
        points, drawdown = [], []
        series = [] if acceptance else record.get('equity_curve', [])
        if series:
            try:
                if not isinstance(series,list) or len(series) > 2000:
                    raise ValueError()
                previous, peak = None, None
                for point in series:
                    at = timestamp(point.get('time'))
                    equity = Decimal(str(point.get('equity')))
                    if at is None or not equity.is_finite() or equity < 0 or (previous and at <= previous):
                        raise ValueError()
                    peak = equity if peak is None else max(peak,equity)
                    if peak <= 0:
                        raise ValueError()
                    sampled = (equity/peak-1)*100
                    points.append(dict(time=at.isoformat(),equity=str(equity)))
                    drawdown.append(dict(time=at.isoformat(),drawdown_pct=str(sampled)))
                    previous = at
            except (ValueError,InvalidOperation,TypeError,AttributeError):
                points, drawdown = [], []
                warnings.append('INVALID_EQUITY_SERIES')
        trades = record.get('trades',[]) if not acceptance else []
        run.update(equity_curve=points,drawdown_curve=drawdown,
            curve_basis='RECORDED_TIMESTAMPED_EQUITY_SAMPLED_DRAWDOWN' if points else 'NOT_RECORDED',
            trades=trades[:200] if isinstance(trades,list) else [], record=record)
        return envelope(run, 'AVAILABLE', [run['source']], [*warnings,
            *([] if points else ['EQUITY_CURVE_NOT_RECORDED']), *([] if trades else ['PER_TRADE_RECORDS_NOT_RECORDED'])])
