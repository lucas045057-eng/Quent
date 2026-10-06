import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql


@pytest.fixture
def db_config(tmp_path):
    from dashboard.backend.config import DashboardConfig
    dsn = os.environ['TEST_POSTGRES_DSN']
    schema = 'dashboard_test_'+uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        conn.execute(sql.SQL('CREATE TABLE {}.execution_positions (content_digest text, account_id text, canonical_symbol text, as_of timestamptz, payload jsonb)').format(sql.Identifier(schema)))
        conn.execute(sql.SQL("INSERT INTO {}.execution_positions VALUES ('d','a','BTCUSDT',now(),%s)").format(sql.Identifier(schema)), (psycopg.types.json.Jsonb({'qty':'0.25','mark_price':'60000','api_key':'never-expose'}),))
    try:
        yield DashboardConfig(root=tmp_path, dsn=dsn, schema=schema)
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_database_partial_schema_keeps_actual_rows_readonly_and_safe(db_config):
    from dashboard.backend.db import DatabaseReader
    reader = DatabaseReader(db_config)
    result = reader.snapshot()
    assert result['state'] == 'PARTIAL'
    assert result['tables']['execution_positions'][0]['payload']['qty'] == '0.25'
    assert 'never-expose' not in str(result)
    assert result['transaction_read_only'] is True
    with reader.connect() as conn:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            conn.execute(sql.SQL('DELETE FROM {}.execution_positions').format(sql.Identifier(db_config.schema)))


def test_database_no_dsn_is_not_configured(tmp_path):
    from dashboard.backend.config import DashboardConfig
    from dashboard.backend.db import DatabaseReader
    assert DatabaseReader(DashboardConfig(root=tmp_path)).snapshot()['state'] == 'NOT_CONFIGURED'


def test_artifact_read_keeps_current_screening_and_research_despite_screening_volume(db_config):
    from dashboard.backend.db import DatabaseReader
    with psycopg.connect(db_config.dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("""CREATE TABLE {}.strategy_v2_artifacts (
            artifact_id text, artifact_kind text, canonical_digest text,
            schema_version text, payload jsonb, created_at timestamptz)""").format(sql.Identifier(db_config.schema)))
        conn.execute(sql.SQL("""INSERT INTO {}.strategy_v2_artifacts
            SELECT i::text,'SCREENING',i::text,'V2','{{}}'::jsonb,
                   now()+i*interval '1 second' FROM generate_series(1,250) i""").format(sql.Identifier(db_config.schema)))
        conn.execute(sql.SQL("""INSERT INTO {}.strategy_v2_artifacts
            VALUES ('analysis','ANALYSIS','analysis','V2','{{}}',now())""").format(sql.Identifier(db_config.schema)))
    reader=DatabaseReader(db_config)
    with reader.connect() as conn:
        rows=reader.read_table(conn,'strategy_v2_artifacts')
    assert len(rows)==2
    assert next(r for r in rows if r['artifact_kind']=='SCREENING')['canonical_digest']=='250'
    assert any(r['artifact_kind']=='ANALYSIS' for r in rows)


def test_concurrent_dashboard_polls_share_one_bounded_read(tmp_path):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from dashboard.backend.config import DashboardConfig
    from dashboard.backend.db import DatabaseReader
    reader=DatabaseReader(DashboardConfig(root=tmp_path))
    calls=[]
    gate=threading.Barrier(4)
    def read():
        calls.append(1)
        time.sleep(.02)
        return {'state':'NOT_CONFIGURED','tables':{}}
    reader._read_snapshot=read
    def poll(_):
        gate.wait(timeout=1)
        return reader.snapshot()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(poll,range(4)))
    assert len(calls)==1 and all(r is results[0] for r in results)
    reader._snapshot_at=time.monotonic()-3
    reader.snapshot()
    assert len(calls)==2
