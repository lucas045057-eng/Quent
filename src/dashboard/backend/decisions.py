from uuid import UUID
from .models import envelope, payload


class DecisionReader:
    def __init__(self, reader):
        self.reader = reader

    def list(self, limit=100, symbol=None, eligible=None):
        source = self.reader.snapshot()
        rows = []
        statuses = source['tables'].get('phase9_decision_status_events', [])
        for row in source['tables'].get('phase9_decision_candidates', []):
            if symbol and row['symbol'] != symbol:
                continue
            if eligible is not None and row['eligible'] != eligible:
                continue
            original = payload(row)
            latest = next((item for item in statuses if item['decision_id'] == row['decision_id']), None)
            rows.append({**original, **{key:row.get(key) for key in ('decision_id','evaluation_id','symbol','market','timeframe','eligible','direction_bias','confidence_band','valid_until','created_at')},
                'recorded_lifecycle_status':latest['status'] if latest else None,
                'source':'phase9_decision_candidates', 'risk_status':'NOT_QUERIED'})
        return envelope(dict(items=rows[:limit], database=source['state']),
            'PARTIAL' if source['state'] in {'ERROR','PARTIAL'} else ('AVAILABLE' if rows else 'NO_DATA'),
            ['phase9_decision_candidates','phase9_decision_status_events'], source['warnings'])

    def detail(self, decision_id):
        try:
            decision_id = str(UUID(decision_id))
        except (ValueError, TypeError):
            return envelope(None, 'NO_DATA')
        source = self.reader.decision_graph(decision_id)
        tables = source['tables']
        candidates = tables.get('phase9_decision_candidates', [])
        if not candidates:
            return envelope(None, 'ERROR' if source['state'] == 'ERROR' else 'NO_DATA', warnings=source['warnings'])
        row = candidates[0]
        decision = {**payload(row), **{key:row[key] for key in ('decision_id','evaluation_id','symbol','market','timeframe','eligible','valid_until')}}
        def values(table):
            return [payload(item) for item in tables.get(table, [])]
        snapshots = values('phase9_evaluation_snapshots')
        evidence = values('phase9_evidence_items')
        chains = values('phase9_evidence_chains')
        patterns = values('phase9_pattern_matches')
        reviews = values('phase9_jev_reviews')
        intents = values('execution_intents')
        audit = values('execution_results')
        risk = dict(status='APPROVED' if intents else 'NOT_RECORDED',
            basis='PERSISTED_EXECUTION_INTENT' if intents else 'NO_SEPARATE_PERSISTED_RISK_REJECTION',
            risk_policy_version=intents[0].get('risk_policy_version') if intents else None,
            risk_budget=intents[0].get('risk_budget') if intents else None,
            rejection_reason=None)
        chain = [
            dict(id='market', label='Market Data',status='RECORDED' if snapshots else 'NO_DATA', detail='snapshot'),
            dict(id='screening',label='Screening',status='RECORDED_REFERENCE',detail='screening'),
            dict(id='evidence',label='Evidence',status='RECORDED' if evidence else 'NO_DATA',detail='evidence'),
            dict(id='strategy',label='Strategy / Pattern',status='RECORDED' if patterns else 'NO_DATA',detail='patterns'),
            dict(id='decision',label='DecisionCandidate',status='PASS' if row['eligible'] else 'REJECT',detail='decision'),
            dict(id='risk',label='Risk',status=risk['status'],detail='risk'),
            dict(id='intent',label='ExecutionIntent',status='CREATED' if intents else 'NOT_RECORDED',detail='intents'),
            dict(id='nautilus',label='Nautilus',status='RECORDED' if audit else 'NO_DATA',detail='execution_audit'),
        ]
        return envelope(dict(decision=decision, snapshot=snapshots[0] if snapshots else None,
            screening=dict(stage1_candidate_id=row['stage1_candidate_id'], status='REFERENCE_ONLY'),
            evidence=evidence, evidence_chain=chains[0] if chains else None, patterns=patterns,
            jev_reviews=reviews, jev_status=reviews[0].get('status') if reviews else 'NOT_RECORDED',
            lifecycle=values('phase9_decision_status_events'), risk=risk, intents=intents,
            execution_audit=audit, chain=chain, missing_tables=source['missing']),
            'PARTIAL' if source['missing'] else 'AVAILABLE', list(tables),
            [*source['warnings'],'RISK_ABSENCE_DOES_NOT_PROVE_REJECTION','CUMULATIVE_EXECUTION_AUDIT'])
