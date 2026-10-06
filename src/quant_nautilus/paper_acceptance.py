"""Measured two-process local Paper acceptance in a disposable PostgreSQL schema."""
import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from quant_execution.persistence import ExecutionStore
from quant_nautilus.acceptance import fixture_case, install_fixture_decision
from quant_phase1.db import apply_migrations
from quant_phase9.canonical import canonical_json, canonical_sha256


def _launch(dsn,schema,intent,ready,*,restore):
    env={**os.environ,'QUANT_PAPER_DSN':dsn,'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':'src'}
    log=ready.with_suffix('.log').open('w')
    proc=subprocess.Popen([sys.executable,'-m','quant_nautilus.paper','--schema',schema,
        '--intent-id',str(intent.intent_id),'--ready-file',str(ready),
        '--fixture-restore' if restore else '--fixture-run'],env=env,stdout=log,stderr=log)
    return proc,log


def _stop(proc,log):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            raise RuntimeError('local Paper did not stop gracefully')
    log.close()
    if proc.returncode != 0:
        raise RuntimeError('local Paper exited unsuccessfully; inspect local diagnostic artifact')


def _ready(proc,path):
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        if proc.poll() is not None:
            raise RuntimeError('local Paper terminated before readiness')
        if path.exists():
            report=json.loads(path.read_text())
            if report['pid']==proc.pid and report['state']=='RECONCILED':
                return report
        time.sleep(.1)
    raise RuntimeError('local Paper readiness deadline exceeded')


def run_acceptance(dsn,output_dir,*,duration_seconds=60,formal=True):
    if not 0 < duration_seconds <= 3600 or (formal and duration_seconds<60):
        raise ValueError('formal Paper measures at least 60 continuous seconds per case')
    info=conninfo_to_dict(dsn)
    if info.get('host') not in {'localhost','127.0.0.1','::1'} or info.get('dbname')!='quant_phase9_test':
        raise ValueError('isolated local acceptance database required')
    output_dir=Path(output_dir).resolve()
    output_dir.mkdir(parents=True,exist_ok=True)
    rows=[]
    for symbol,side in (('BTCUSDT','LONG'),('ETHUSDT','SHORT')):
        schema='quant_paper_fixture_'+uuid4().hex
        root=output_dir/f'{symbol}-{side}'
        root.mkdir(parents=True,exist_ok=True)
        with psycopg.connect(dsn,autocommit=True) as setup:
            setup.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        try:
            with psycopg.connect(dsn,options=f'-c search_path={schema}',autocommit=True) as conn:
                apply_migrations(conn)
                case=fixture_case(root/'policy',symbol=symbol,side=side,mode='PAPER',now=datetime.now(timezone.utc))
                install_fixture_decision(conn,case)
                store=ExecutionStore(conn)
                store.record_account(case.account)
                store.reserve(case.intent,case.risk_policy,now=case.intent.created_at)
                first_path,second_path=root/'initial.json',root/'restored.json'
                first,log=_launch(dsn,schema,case.intent,first_path,restore=False)
                try:
                    initial=_ready(first,first_path)
                    started=time.monotonic_ns()
                    peak,rss,samples=initial['peak_rss_bytes'],initial['rss_bytes'],0
                    while time.monotonic_ns()-started<duration_seconds*1_000_000_000:
                        if first.poll() is not None:
                            raise RuntimeError('continuous Paper process stopped')
                        report=json.loads(first_path.read_text())
                        if report['state_digest']!=initial['state_digest'] or report['state']!='RECONCILED':
                            raise RuntimeError('continuous Paper state changed unexpectedly')
                        stamp=datetime.fromisoformat(report['as_of'].replace('Z','+00:00'))
                        if (datetime.now(timezone.utc)-stamp).total_seconds()>2:
                            raise RuntimeError('Paper health heartbeat became stale')
                        peak=max(peak,report['peak_rss_bytes'])
                        rss=max(rss,report['rss_bytes'])
                        samples+=1
                        time.sleep(.2)
                    elapsed_ns=time.monotonic_ns()-started
                    result_before=conn.execute('SELECT execution_id,content_digest FROM execution_results ORDER BY execution_id').fetchall()
                finally:
                    _stop(first,log)
                second,log=_launch(dsn,schema,case.intent,second_path,restore=True)
                try:
                    restored=_ready(second,second_path)
                    if initial['state_digest']!=restored['state_digest'] or first.pid==second.pid:
                        raise RuntimeError('fresh-process state reconciliation failed')
                    if conn.execute('SELECT execution_id,content_digest FROM execution_results ORDER BY execution_id').fetchall()!=result_before:
                        raise RuntimeError('recovery changed immutable execution result audit')
                    payment_count=conn.execute('SELECT count(*) FROM execution_funding_payments WHERE applied').fetchone()[0]
                    if payment_count!=1 or restored['entry_order_count']!=1 or restored['repair_count']!=0:
                        raise RuntimeError('recovery duplicated command or funding payment')
                    peak=max(peak,restored['peak_rss_bytes'])
                    rows.append(dict(symbol=symbol,side=side,first_pid=first.pid,second_pid=second.pid,
                        intent_id=str(case.intent.intent_id),decision_id=str(case.decision.decision_id),
                        initial_digest=initial['state_digest'],restored_digest=restored['state_digest'],
                        result_count=len(result_before),funding_payment_count=payment_count,
                        funding_cash=restored['position']['funding_cash'],
                        quantity=restored['position']['quantity'],protection=restored['position']['protection_status'],
                        elapsed_seconds=str(Decimal(elapsed_ns)/1_000_000_000),elapsed_ns=elapsed_ns,
                        health_samples=samples,peak_rss_bytes=peak,rss_bytes=rss,
                        cap_pass=peak<=384*1024*1024,
                        audit_digest=str(canonical_sha256(result_before)),
                        position=restored['position']))
                finally:
                    _stop(second,log)
        finally:
            with psycopg.connect(dsn,autocommit=True) as cleanup:
                cleanup.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
    body=dict(schema='QUANT_LOCAL_PAPER_ACCEPTANCE_V1',acceptance_kind='FIXTURE_DRIVEN_ACCEPTANCE',
        nautilus_version='1.231.0',cases=rows,checks_passed=True,
        engine_peak_rss_bytes=max(row['peak_rss_bytes'] for row in rows),engine_cap_bytes=384*1024*1024,
        formal_acceptance_pass=bool(formal and all(row['cap_pass'] and row['elapsed_ns']>=60_000_000_000 for row in rows)),
        restart_status='LOCAL_SANDBOX_REPLAY_RECONCILED',network_order_routes=0,
        production_policy_status='NOT_CONFIGURED',strategy_edge_status='NOT_VALIDATED')
    report={**body,'digest':str(canonical_sha256(body))}
    (output_dir/'paper-acceptance.json').write_text(canonical_json(report)+'\n')
    return report


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--duration-seconds',type=int,default=60)
    parser.add_argument('--smoke',action='store_true')
    args=parser.parse_args()
    report=run_acceptance(os.environ['TEST_POSTGRES_DSN'],args.output_dir,
        duration_seconds=args.duration_seconds,formal=not args.smoke)
    print(canonical_json(report))


if __name__=='__main__':
    main()
