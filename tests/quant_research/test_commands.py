from datetime import timedelta, datetime, timezone
from decimal import Decimal as D
import json

from quant_research.commands import evaluate_history, export_snapshot_features
from quant_research.storage import export_features, load_features
from quant_features.core import project_features
from quant_nautilus.acceptance import fixture_case, install_fixture_decision
from tests.quant_research.test_features import observation
from tests.quant_execution.fixtures import NOW
from tests.quant_nautilus.test_paper_restart import paper_db
from dataclasses import replace


def test_cli_research_evaluates_purged_out_of_sample_folds_with_one_shared_cohort(tmp_path):
    observations=[]
    labels=[]
    for index in range(14):
        at=NOW+timedelta(minutes=30*index)
        for kind,value in {'PRICE':D('50000')+index*100,'OI':D('1000000')+index*20000,
            'FUNDING':D('0.0001'),'TAKER':D('10'),'CVD':D('100')+index,'LIQUIDATION':D('1000')}.items():
            observations.append(replace(observation(kind,value=value),window_start=at,window_end=at,
                known_at=at,received_at=at,processed_at=at,source_ref=f'fixture:{kind}/{index}'))
        if index:
            labels.append(dict(symbol='BTC-USDT-PERP',as_of=at.isoformat(),
                previous_as_of=(at-timedelta(minutes=30)).isoformat(),label_end=(at+timedelta(minutes=15)).isoformat(),
                future_return='0.001',settled_funding_rate='0.0001',regime='SYNTHETIC_TREND',source_ref=f'fixture:label/{index}'))
    manifest=export_features(project_features(observations),tmp_path/'data',
        postgres_source_ref='postgres:NOT_APPLICABLE:synthetic_fixture')
    path=tmp_path/'labels.json'
    path.write_text(json.dumps({'schema':'QUANT_RESEARCH_LABELS_V1','rows':labels}))
    report=evaluate_history(manifest,path,allow_fixture=True,min_train=2,test_size=3,embargo_seconds=300)
    assert report['oos_folds'] and report['strategy_edge_status']=='NOT_VALIDATED'
    assert report['production_policy_changed'] is False
    for fold in report['oos_folds']:
        assert fold['training_latest_label_end'] < fold['test_start']
        comparison=fold['comparison']
        assert len(comparison['hypotheses'])==6
        assert len({row['cohort_digest'] for row in comparison['hypotheses'].values()})==1
    strict=evaluate_history(manifest,path,allow_fixture=False,min_train=2,test_size=3,embargo_seconds=300)
    assert strict['descriptive_comparison']['eligible_count']==0


def test_actual_postgres_snapshot_export_preserves_declared_quality_and_digest(paper_db,tmp_path):
    conn,_,_=paper_db
    case=fixture_case(tmp_path/'fixture',mode='PAPER',now=datetime.now(timezone.utc))
    install_fixture_decision(conn,case)
    first=export_snapshot_features(conn,(case.snapshot.evaluation_id,),tmp_path/'export')
    second=export_snapshot_features(conn,(case.snapshot.evaluation_id,),tmp_path/'export')
    assert first==second
    features=load_features(first)
    oi=next(f for f in features if f.kind=='OI')
    assert oi.value==D('100000000')
    assert oi.pit_status=='PIT_UNVERIFIED' and oi.authority=='UNVERIFIED'
    assert oi.source_ref.startswith('PHASE2:OPEN_INTEREST')
