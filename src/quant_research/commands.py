"""Small, bounded PostgreSQL export and Parquet research command entrypoints."""
import argparse
from dataclasses import fields
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
from uuid import UUID

from quant_execution.contracts import utc
from quant_features.core import research_frame
from quant_phase9.canonical import canonical_json, canonical_sha256
from quant_phase9.features import snapshot_features
from quant_research.harness import HypothesisConfigV1, ResearchSampleV1, compare_hypotheses, walk_forward
from quant_research.storage import export_features, load_features


def export_snapshot_features(conn,evaluation_ids,directory):
    from quant_phase9.runtime import _load_snapshot
    identifiers=tuple(evaluation_ids)
    if not 0<len(identifiers)<=256 or len(set(identifiers))!=len(identifiers):
        raise ValueError('export requires 1..256 unique evaluation UUIDs')
    identifiers=tuple(UUID(str(value)) for value in identifiers)
    features,lineage=[],[]
    with conn.transaction():
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        schema=conn.execute('SELECT current_schema()').fetchone()[0]
        rows=conn.execute('SELECT evaluation_id,snapshot_digest,payload FROM phase9_evaluation_snapshots '
            'WHERE evaluation_id=ANY(%s) ORDER BY evaluation_id',(list(identifiers),)).fetchall()
        if len(rows)!=len(identifiers):
            raise ValueError('an immutable evaluation snapshot is unavailable')
        for eid,digest,data in rows:
            snapshot=_load_snapshot(data)
            if snapshot.evaluation_id!=eid or str(snapshot.snapshot_digest)!=digest:
                raise ValueError('stored snapshot identity/digest mismatch')
            if snapshot.symbol not in {'BTCUSDT','ETHUSDT'}:
                raise ValueError('first research export accepts BTC/ETH USDT perpetuals only')
            canonical=snapshot.symbol[:-4]+'-USDT-PERP'
            features.extend(snapshot_features(snapshot,canonical_symbol=canonical))
            lineage.append(dict(evaluation_id=str(eid),snapshot_digest=digest))
    from quant_features.core import project_features
    # Re-sort across snapshots with the SAME shared definitions.
    projected=project_features(feature.observation for feature in features)
    origin=dict(schema='QUANT_POSTGRES_FEATURE_LINEAGE_V1',postgres_schema=schema,
        table='phase9_evaluation_snapshots',snapshots=lineage)
    digest=str(canonical_sha256(origin))
    directory=Path(directory)
    manifest=export_features(projected,directory,
        postgres_source_ref=f'postgres:{schema}.phase9_evaluation_snapshots/bundle-sha256:{digest}')
    raw=canonical_json(origin)+'\n'
    path=directory/(digest+'.lineage.json')
    if path.exists() and path.read_text()!=raw:
        raise ValueError('immutable export lineage conflict')
    path.write_text(raw)
    return manifest


def _timestamp(value):
    if not isinstance(value,str):
        raise ValueError('research timestamp must be an explicit UTC string')
    result=datetime.fromisoformat(value.replace('Z','+00:00'))
    utc(result)
    return result


def _read_labels(path):
    path=Path(path)
    if path.stat().st_size>4*1024*1024:
        raise ValueError('research label byte bound exceeded')
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:
                raise ValueError('duplicate research label field')
            value[key]=item
        return value
    doc=json.loads(path.read_text(),object_pairs_hook=unique)
    if set(doc)!={'schema','rows'} or doc['schema']!='QUANT_RESEARCH_LABELS_V1':
        raise ValueError('unsupported research label schema')
    if not isinstance(doc['rows'],list) or not 0<len(doc['rows'])<=16384:
        raise ValueError('research label row bound exceeded')
    expected={'symbol','as_of','previous_as_of','label_end','future_return','settled_funding_rate','regime','source_ref'}
    identities=set()
    for row in doc['rows']:
        if set(row)!=expected or not isinstance(row['source_ref'],str) or not row['source_ref']:
            raise ValueError('research label provenance/fields invalid')
        if row['symbol'] not in {'BTC-USDT-PERP','ETH-USDT-PERP'}:
            raise ValueError('unsupported research symbol')
        key=(row['symbol'],row['as_of'])
        if key in identities:
            raise ValueError('duplicate research label identity')
        identities.add(key)
        for field in ('future_return','settled_funding_rate'):
            if row[field] is not None and (not isinstance(row[field],str) or not Decimal(row[field]).is_finite()):
                raise ValueError('label return/cost must be a finite exact string')
    return doc


def evaluate_history(manifest_path,labels_path,*,allow_fixture=False,min_train=8,test_size=4,embargo_seconds=300,
                     config=None,maker_bps=Decimal('2'),taker_bps=Decimal('5'),
                     spread_bps=Decimal('2'),slippage_bps=Decimal('3')):
    features=load_features(manifest_path)
    doc=_read_labels(labels_path)
    config=config or HypothesisConfigV1(Decimal('0.001'),Decimal('0.01'),Decimal('0.0005'),Decimal('1000'))
    costs=dict(maker_bps=maker_bps,taker_bps=taker_bps,spread_bps=spread_bps,slippage_bps=slippage_bps)
    samples=[]
    for row in doc['rows']:
        at=_timestamp(row['as_of'])
        previous=_timestamp(row['previous_as_of'])
        samples.append(ResearchSampleV1(row['symbol'],at,_timestamp(row['label_end']),
            research_frame(features,previous,require_pit=not allow_fixture),
            research_frame(features,at,require_pit=not allow_fixture),Decimal(row['future_return']),
            Decimal(row['settled_funding_rate']) if row['settled_funding_rate'] is not None else None,row['regime']))
    samples=tuple(sorted(samples,key=lambda row:(row.as_of,row.symbol)))
    folds=[]
    for train,test in walk_forward(samples,test_size=test_size,min_train=min_train,embargo_seconds=embargo_seconds):
        folds.append(dict(training_count=len(train),test_count=len(test),
            training_latest_label_end=max(row.label_end for row in train),test_start=min(row.as_of for row in test),
            comparison=compare_hypotheses(test,config,**costs)))
    body=dict(schema='QUANT_RESEARCH_RUN_V1',feature_manifest_digest=json.loads(Path(manifest_path).read_text())['manifest_digest'],
        label_digest=str(canonical_sha256(doc)),config_digest=str(canonical_sha256(config)),
        allow_fixture=allow_fixture,embargo_seconds=embargo_seconds,oos_folds=folds,
        descriptive_comparison=compare_hypotheses(samples,config,**costs),
        strategy_edge_status='NOT_VALIDATED',production_policy_changed=False,
        inference='predeclared hypotheses; no fitting or automatic policy activation',
        label_source_refs=tuple(row['source_ref'] for row in doc['rows']))
    return {**body,'digest':str(canonical_sha256(body))}


def main():
    parser=argparse.ArgumentParser()
    commands=parser.add_subparsers(dest='command',required=True)
    export=commands.add_parser('export')
    export.add_argument('--evaluation-id',action='append',type=UUID,required=True)
    export.add_argument('--output-dir',type=Path,required=True)
    export.add_argument('--schema',required=True)
    research=commands.add_parser('evaluate')
    research.add_argument('--manifest',type=Path,required=True)
    research.add_argument('--labels',type=Path,required=True)
    research.add_argument('--output',type=Path,required=True)
    research.add_argument('--allow-fixture',action='store_true')
    research.add_argument('--min-train',type=int,default=8)
    research.add_argument('--test-size',type=int,default=4)
    research.add_argument('--embargo-seconds',type=int,default=300)
    args=parser.parse_args()
    if args.command=='export':
        import re
        import psycopg
        if not re.fullmatch(r'[a-z_][a-z0-9_]{0,62}',args.schema):
            raise ValueError('invalid PostgreSQL schema identifier')
        with psycopg.connect(os.environ['QUANT_RESEARCH_DSN'],options=f'-c search_path={args.schema}',autocommit=True) as conn:
            manifest=export_snapshot_features(conn,args.evaluation_id,args.output_dir)
        print(str(manifest))
    else:
        report=evaluate_history(args.manifest,args.labels,allow_fixture=args.allow_fixture,
            min_train=args.min_train,test_size=args.test_size,embargo_seconds=args.embargo_seconds)
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(canonical_json(report)+'\n')
        print(canonical_json(report))


if __name__=='__main__':
    main()
