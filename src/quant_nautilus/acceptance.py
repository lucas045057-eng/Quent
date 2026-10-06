"""Explicitly synthetic local acceptance, through the actual Phase9 and risk gates.

These semantic fixture policies validate integration plumbing, not market edge.
They are never loaded by the non-fixture Quant runtime.
"""
from dataclasses import dataclass, fields, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path
import json

from nautilus_trader.test_kit.providers import TestInstrumentProvider

from quant_phase1.contracts import DataStatus
from quant_phase1.stage1 import Stage1Result
from quant_phase9.canonical import canonical_json, canonical_sha256, evaluation_id_for
from quant_phase9.contracts import (
    EvaluationIdentityV1, EvaluationSnapshotV1, EvidenceChainV1,
    EvidenceFreshnessV1, EvidenceQualityV1, PolicyDataStatusV1, SourcePhaseV1,
)
from quant_phase9.decision import build_decision_candidate, input_snapshot_hash_for
from quant_phase9.evidence import build_evidence, build_chain_draft
from quant_phase9.intake import build_stage1_candidate_event
from quant_phase9.patterns import match_patterns
from quant_phase9.policy import (
    PolicyApprovalStatusV1, PolicyApprovalV1, PolicyContentV1,
    load_approved_policy_manifest,
)
from quant_phase9.sources import make_projection
from quant_phase9.validator import validate_evidence
from quant_execution.contracts import AccountSnapshotV1, DecisionEnvelopeV1, InstrumentSpecV1, QuoteV1
from quant_execution.funding import FundingObservationV1
from quant_execution.risk import RiskPolicyV1, approve_intent
from quant_nautilus.adapter import run_intent_backtest

FIXTURE_TIME = datetime(2026,9,28,0,15,tzinfo=timezone.utc)


@dataclass(frozen=True)
class AcceptanceCase:
    instrument: object
    policy: object
    snapshot: object
    evidence: tuple
    chain: object
    matches: tuple
    decision: object
    lifecycle: object
    risk_policy: object
    account: object
    intent: object
    funding: object


def fixture_case(directory: Path, *, symbol='BTCUSDT', side='LONG',
                 pattern='TREND_CONTINUATION', mode='BACKTEST', now=FIXTURE_TIME,
                 code_version='b'*40, account_id='fixture-account'):
    if symbol not in {'BTCUSDT','ETHUSDT'} or side not in {'LONG','SHORT'}:
        raise ValueError('unsupported fixture instrument/direction')
    if pattern not in {'TREND_CONTINUATION','BREAKOUT_CONFIRMATION'} or mode not in {'BACKTEST','PAPER'}:
        raise ValueError('fixture accepts local modes and two declared patterns only')
    content = {name:[] for name in PolicyContentV1.model_fields}
    for name,phase,source,kind,code in (
        ('setup','PHASE1','STAGE1_CANDIDATE','PRICE_STRUCTURE','TREND_UP' if side=='LONG' else 'TREND_DOWN'),
        ('oi','PHASE2','OPEN_INTEREST','OPEN_INTEREST_STRUCTURE','OI_OBSERVED'),
        ('flow','PHASE3','TRADE_FLOW_WINDOW','TRADE_FLOW','BUY_FLOW_DOMINANT' if side=='LONG' else 'SELL_FLOW_DOMINANT'),
    ):
        content['predicates'].append(dict(predicate_id=name,source_phase=phase,source_type=source,
            evidence_type=kind,accepted_semantic_codes=[code],operator='PRESENT',threshold=None,
            upper_threshold=None,unit=None,missing_behavior='FAIL_CLOSED'))
    content['ttl_rules'] = [dict(rule_id='ttl',pattern_type=pattern,timeframe='1H',ttl_seconds=1200)]
    content['revalidation_rules'] = [dict(rule_id='revalidate',pattern_type=pattern,timeframe='1H',
        cadence_seconds=300,max_runs_per_minute=3)]
    content['enabled_patterns'] = [dict(pattern_type=pattern,timeframe='1H',direction=side,
        required_predicates=['setup','oi','flow'],supporting_predicates=[],contradicting_predicates=[],
        hard_conflict_predicates=[],freshness_rule_ids=[],coverage_rule_ids=[],confidence_ceiling='HIGH',
        jev_required_conflict_classes=[],ttl_rule_id='ttl',revalidation_rule_id='revalidate',
        approval_reference='FIXTURE_DRIVEN_ACCEPTANCE:semantic-route-only')]
    digest = str(canonical_sha256(content))
    directory = Path(directory)
    directory.mkdir(parents=True,exist_ok=True)
    manifest_path,approval_path = directory/'manifest.json',directory/'approval.json'
    manifest_path.write_text(json.dumps(dict(schema='PHASE9_POLICY_MANIFEST_V1',manifest_version='1.0.0',
        created_at='2026-09-28T00:00:00Z',policy_content=content,manifest_digest=digest)))
    fixture_approval = PolicyApprovalV1(
        schema='PHASE9_POLICY_APPROVAL_V1', manifest_version='1.0.0',
        manifest_digest=digest, approval_status=PolicyApprovalStatusV1.APPROVED,
        approved_at=FIXTURE_TIME-timedelta(minutes=14), approved_by='fixture-human',
        approved_commit=code_version,
    )
    approval_path.write_text(canonical_json(fixture_approval.model_dump(mode='python'))+'\n')
    policy = load_approved_policy_manifest(manifest_path,approval_path,expected_commit=code_version)
    stage = Stage1Result(symbol=symbol,category='A',reason='FIXTURE_DRIVEN_ACCEPTANCE',
        status=DataStatus.AVAILABLE,inputs_used=('price',),indicators={'atr':D('1')},
        structure='BULLISH' if side=='LONG' else 'BEARISH',reason_codes=('STRUCTURE_ALIGNED',),
        timestamp=now-timedelta(minutes=1))
    event = build_stage1_candidate_event(screening_result_id=91,run_id=22,screening_result=stage,
        market='USDT_PERPETUAL',instrument_scope=dict(category='USDT-FUTURES',quote_coin='USDT',
        contract_type='perpetual',status='online',in_scope='true'),candidate_created_at=now-timedelta(minutes=1),
        candidate_valid_until=now+timedelta(minutes=20),stage1_policy_version='phase1-basic-v1',
        source_as_of=now-timedelta(minutes=1))
    identity = EvaluationIdentityV1(stage1_candidate_id=91,market='USDT_PERPETUAL',symbol=symbol,
        timeframe='1H',evaluation_window_start=now-timedelta(hours=1),evaluation_window_end=now,
        policy_generation=f'1.0.0:{digest}',material_change_generation='initial')
    eid = evaluation_id_for(identity)
    projections = tuple(make_projection(evaluation_id=eid,source_phase=phase,source_type=kind,
        source_ref=f'{phase.value}:{kind}:fixture/1',symbol=symbol,market='USDT_PERPETUAL',
        event_time=now-timedelta(seconds=30),observed_at=now-timedelta(seconds=30),
        captured_at=now-timedelta(seconds=20),processed_at=now-timedelta(seconds=20),
        available_at=now-timedelta(seconds=20),availability_status=PolicyDataStatusV1.AVAILABLE,
        freshness=EvidenceFreshnessV1.FRESH,quality=EvidenceQualityV1.VALID,coverage=None,payload=payload)
        for phase,kind,payload in (
            (SourcePhaseV1.PHASE2,'OPEN_INTEREST',dict(open_interest_usd=D('100000000'),normalization_method='CONTRACTS_TO_USD')),
            (SourcePhaseV1.PHASE3,'TRADE_FLOW_WINDOW',dict(delta_base=D('10') if side=='LONG' else D('-10'),unknown_trade_count=0)),
            (SourcePhaseV1.PHASE4,'LIQUIDATION_WINDOW',dict(acceptance_kind='SYNTHETIC_FIXTURE',total_usd=D('100'))),
            (SourcePhaseV1.PHASE5,'MARKET_CONTEXT',dict(acceptance_kind='SYNTHETIC_FIXTURE',regime='fixture')),
            (SourcePhaseV1.PHASE8,'OPTIONS_CONTEXT',dict(acceptance_kind='SYNTHETIC_FIXTURE',iv=D('0.5'))),
        ))
    snapshot = EvaluationSnapshotV1(identity=identity,evaluation_id=eid,stage1_candidate_id=91,
        symbol=symbol,market='USDT_PERPETUAL',timeframe='1H',evaluation_time=now,as_of=now,created_at=now,
        candidate_event=event,source_projections=projections,stage1_policy_version='phase1-basic-v1',
        evidence_schema_version='PHASE9_EVIDENCE_CHAIN_V1',freshness_policy_version='1.0.0',
        pattern_policy_version='1.0.0',decision_policy_version='1.0.0',ttl_policy_version='1.0.0',
        code_version=code_version,snapshot_digest=canonical_sha256(dict(identity=identity,event=event,projections=projections)))
    body = {'schema':'PHASE9_EVALUATION_SNAPSHOT_V1',
        **{f.name:getattr(snapshot,f.name) for f in fields(snapshot) if f.name!='snapshot_digest'}}
    snapshot = replace(snapshot,snapshot_digest=canonical_sha256(body))
    evidence = build_evidence(snapshot=snapshot,policy_manifest=policy)
    validation = validate_evidence(snapshot=snapshot,evidence_items=evidence,policy_manifest=policy)
    draft = build_chain_draft(snapshot=snapshot,evidence_items=evidence,validation=validation)
    matches = match_patterns(evidence_items=evidence,validation=validation,policy_manifest=policy,timeframe='1H')
    chain = EvidenceChainV1(stage1_candidate_id=91,symbol=symbol,evaluation_id=eid,evaluation_time=now,
        timeframe='1H',supporting=draft.supporting,conflicting=draft.conflicting,neutral=draft.neutral,
        missing=draft.missing,degraded=draft.degraded,hard_vetoes=draft.hard_vetoes,
        matched_patterns=tuple(m.pattern_match_id for m in matches if m.status=='MATCHED'),
        jev_review_required=False,evidence_schema_version=snapshot.evidence_schema_version,
        evaluation_snapshot_hash=snapshot.snapshot_digest,
        input_snapshot_hash=input_snapshot_hash_for(snapshot_hash=snapshot.snapshot_digest,jev_review=None))
    decision,lifecycle = build_decision_candidate(snapshot=snapshot,evidence_chain=chain,pattern_matches=matches,
        jev_review=None,policy_manifest=policy,now=now)
    instrument = (TestInstrumentProvider.btcusdt_perp_binance() if symbol=='BTCUSDT'
        else TestInstrumentProvider.ethusdt_perp_binance())
    canonical = f'{instrument.base_currency.code}-USDT-PERP'
    mid = D('50000') if symbol=='BTCUSDT' else D('3000')
    spec = InstrumentSpecV1(symbol,canonical,'BINANCE',str(instrument.id),'USDT',
        instrument.size_increment.as_decimal(),instrument.min_quantity.as_decimal(),
        instrument.max_quantity.as_decimal(),instrument.price_increment.as_decimal(),D('5'),True)
    account = AccountSnapshotV1(account_id,'BINANCE',mode,D('10000'),D('10000'),D('0'),D('0'),0,now,'RECONCILED')
    risk_policy = RiskPolicyV1('risk-v1',D('1000'),D('1000'),D('1'),D('10'),D('2000'),D('20'),2,
        5,5,D('10'),D('10'),60,('1.0.0',),(code_version,))
    intent = approve_intent(envelope=DecisionEnvelopeV1(decision,'ACTIVE',str(snapshot.snapshot_digest),str(chain.input_snapshot_hash)),
        instrument=spec,quote=QuoteV1(canonical,mid-1,mid+1,now,'AVAILABLE'),account=account,
        policy=risk_policy,stop_price=mid-99 if side=='LONG' else mid+101,now=now)
    boundary = now+timedelta(seconds=2)
    funding = FundingObservationV1(canonical,boundary,D('0.0001'),mid,'USDT',boundary,boundary,
        'SETTLED','AVAILABLE','VALID','EXCHANGE_PUBLIC','fixture:canonical-funding/1','FIXTURE_DRIVEN_ACCEPTANCE')
    return AcceptanceCase(instrument,policy,snapshot,evidence,chain,matches,decision,lifecycle,risk_policy,account,intent,funding)


def run_backtest_acceptance(cases):
    trades = []
    for case in cases:
        result = run_intent_backtest(case.intent,case.instrument,funding=(case.funding,),close_at_final=True)
        costs = result.economics
        change = result.position.equity-D('10000')
        if costs is None or not costs.resolved or abs(change-costs.net_pnl)>D('0.00000001'):
            raise RuntimeError('native account/cost reconciliation failed')
        trades.append(dict(canonical_symbol=case.intent.canonical_symbol,side=case.intent.side,
            pattern=case.decision.matched_pattern,decision_id=str(case.decision.decision_id),
            intent_id=str(case.intent.intent_id),quantity=str(case.intent.approved_quantity),
            gross_mid_pnl=str(costs.gross_mid_pnl),trading_pnl=str(costs.trading_pnl),fees=str(costs.fees),
            funding_cash=str(costs.funding_cash),spread_cost=str(costs.spread_cost),slippage_cost=str(costs.slippage_cost),
            net_pnl=str(costs.net_pnl),account_change=str(change),
            maker_fee=str(case.instrument.maker_fee),taker_fee=str(case.instrument.taker_fee),
            fee_assumption='market IOC and stop orders are taker fills; fixture instrument fee schedule',
            cost_assumption='quoted spread; native fills with 20ms latency, deterministic fill model',
            position=result.position.side,protection=result.position.protection_status))
    body = dict(schema='QUANT_BACKTEST_ACCEPTANCE_V1',acceptance_kind='FIXTURE_DRIVEN_ACCEPTANCE',
        nautilus_version='1.231.0',cases=trades,passed=bool(trades),
        strategy_edge_status='NOT_VALIDATED',policy_scope='fixture semantic routing only')
    return {**body,'digest':str(canonical_sha256(body))}


def install_fixture_decision(conn,case):
    original = conn.autocommit
    if original:
        conn.autocommit = False
    try:
        with conn.transaction():
            _install_fixture_decision_transactional(conn,case)
    finally:
        if original:
            conn.autocommit = True


def _install_fixture_decision_transactional(conn,case):
    """Seed only a disposable, explicitly requested acceptance schema.

    Persistence of the completed Phase9 graph still uses its real atomic writer.
    This function neither approves a production policy nor modifies data quality.
    """
    from psycopg.types.json import Jsonb
    from quant_phase9.persistence import persist_final
    schema = conn.execute('SELECT current_schema()').fetchone()[0]
    if not schema.startswith(('paper_restart_','quant_paper_fixture_')):
        raise ValueError('fixture installation requires a private acceptance schema')
    snap = case.snapshot
    owner = 'fixture-installer'
    with conn.transaction():
        conn.execute('''INSERT INTO phase9_evaluations(evaluation_id,stage1_candidate_id,symbol,market,
            timeframe,evaluation_state,intake_disposition,candidate_valid_until,policy_generation,material_change_generation)
            VALUES(%s,91,%s,'USDT_PERPETUAL','1H','RUNNING','ADMITTED',%s,%s,'initial')''',
            (snap.evaluation_id,snap.symbol,case.decision.valid_until,snap.identity.policy_generation))
        conn.execute('''INSERT INTO phase9_evaluation_snapshots(evaluation_id,snapshot_digest,as_of,payload,code_version)
            VALUES(%s,%s,%s,%s,%s)''',(snap.evaluation_id,str(snap.snapshot_digest),snap.as_of,
            Jsonb({'schema':'PHASE9_EVALUATION_SNAPSHOT_V1',**json.loads(canonical_json(snap))}),snap.code_version))
        conn.execute('''INSERT INTO outbox_events(event_type,aggregate_key,payload,event_id,phase9_state,
            attempt_count,attempt_limit,next_attempt_at,lease_owner,lease_expires_at)
            VALUES('phase9.stage1_candidate',%s,%s,%s,'LEASED',1,3,%s,%s,%s)''',
            (str(snap.candidate_event.event_id),Jsonb(json.loads(canonical_json(snap.candidate_event))),
            str(snap.candidate_event.event_id),snap.as_of,owner,snap.as_of+timedelta(hours=1)))
        persist_final(conn,evaluation_id=snap.evaluation_id,snapshot_digest=snap.snapshot_digest,
            evidence_items=case.evidence,evidence_chain=case.chain,pattern_matches=case.matches,jev_review=None,
            decision=case.decision,lifecycle_event=case.lifecycle,event_id=str(snap.candidate_event.event_id),
            consumer_name=owner,now=snap.as_of)


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    rows = [('BTCUSDT','LONG','TREND_CONTINUATION'),('BTCUSDT','SHORT','BREAKOUT_CONFIRMATION'),
        ('ETHUSDT','LONG','BREAKOUT_CONFIRMATION'),('ETHUSDT','SHORT','TREND_CONTINUATION')]
    cases = tuple(fixture_case(args.fixture_dir/str(i),symbol=symbol,side=side,pattern=pattern)
        for i,(symbol,side,pattern) in enumerate(rows))
    report = run_backtest_acceptance(cases)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(canonical_json(report)+'\n')
    print(canonical_json(report))


if __name__=='__main__':
    main()
