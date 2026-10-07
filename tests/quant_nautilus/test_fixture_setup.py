import os
import json
from pathlib import Path
import psycopg
import pytest
from psycopg import sql
from quant_nautilus.fixture_setup import prepare_fixture


def test_separate_fixture_installer_prepares_ready_paper_schema_and_record(tmp_path):
    dsn=os.environ.get('TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('TEST_POSTGRES_DSN is required for isolated database tests')
    result=prepare_fixture(dsn,tmp_path,symbol='ETHUSDT',side='SHORT')
    assert result['acceptance_kind']=='FIXTURE_DRIVEN_ACCEPTANCE'
    assert result['schema'].startswith('quant_paper_fixture_')
    try:
        with psycopg.connect(dsn,options=f"-c search_path={result['schema']}") as conn:
            assert conn.execute('SELECT count(*) FROM execution_intents').fetchone()[0]==1
            assert conn.execute('SELECT count(*) FROM phase9_decision_candidates WHERE eligible').fetchone()[0]==1
        assert json.loads((tmp_path/'fixture.json').read_text())==result
        assert 'dsn' not in result and 'password' not in result
    finally:
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(result['schema'])))
