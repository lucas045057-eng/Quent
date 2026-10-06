"""Separate local-only acceptance installer. Paper itself performs no migrations."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from quant_execution.persistence import ExecutionStore
from quant_nautilus.acceptance import fixture_case, install_fixture_decision
from quant_phase1.db import apply_migrations
from quant_phase9.canonical import canonical_json


def prepare_fixture(dsn,directory,*,symbol='BTCUSDT',side='LONG'):
    info=conninfo_to_dict(dsn)
    if info.get('host') not in {'localhost','127.0.0.1','::1'} or info.get('dbname')!='quant_phase9_test':
        raise ValueError('disposable loopback acceptance database required')
    directory=Path(directory).resolve()
    if directory.exists() and any(directory.iterdir()):
        raise ValueError('fixture output directory must be empty; never overwrite existing files')
    directory.mkdir(parents=True,exist_ok=True)
    if symbol not in {'BTCUSDT','ETHUSDT'} or side not in {'LONG','SHORT'}:
        raise ValueError('unsupported acceptance instrument/direction')
    code=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    schema='quant_paper_fixture_'+uuid4().hex
    with psycopg.connect(dsn,autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        with psycopg.connect(dsn,options=f'-c search_path={schema}',autocommit=True) as conn:
            apply_migrations(conn)
            case=fixture_case(directory/'policy',symbol=symbol,side=side,mode='PAPER',
                now=datetime.now(timezone.utc),code_version=code)
            install_fixture_decision(conn,case)
            store=ExecutionStore(conn)
            store.record_account(case.account)
            store.reserve(case.intent,case.risk_policy,now=case.intent.created_at)
        record=dict(schema=schema,intent_id=str(case.intent.intent_id),decision_id=str(case.decision.decision_id),
            evaluation_id=str(case.snapshot.evaluation_id),acceptance_kind='FIXTURE_DRIVEN_ACCEPTANCE',
            symbol=symbol,side=side,code_version=code,
            valid_until=case.intent.valid_until.isoformat(),production_policy_status='NOT_CONFIGURED')
        (directory/'fixture.json').write_text(canonical_json(record)+'\n')
        return record
    except BaseException:
        with psycopg.connect(dsn,autocommit=True) as admin:
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        raise


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--symbol',choices=['BTCUSDT','ETHUSDT'],default='BTCUSDT')
    parser.add_argument('--side',choices=['LONG','SHORT'],default='LONG')
    args=parser.parse_args()
    print(canonical_json(prepare_fixture(os.environ['TEST_POSTGRES_DSN'],args.output_dir,symbol=args.symbol,side=args.side)))


if __name__=='__main__':
    main()
