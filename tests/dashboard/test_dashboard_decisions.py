import os
from dataclasses import replace
from decimal import Decimal
from datetime import timedelta
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest


@pytest.fixture
def decision_db(tmp_path):
    from dashboard.backend.config import DashboardConfig
    from quant_phase1.db import apply_migrations
    from tests.quant_execution.test_persistence import seeded_intent
    dsn = os.environ['TEST_POSTGRES_DSN']
    schema = 'dashboard_chain_'+uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    isolated = make_conninfo(dsn, options=f'-c search_path={schema}')
    try:
        with psycopg.connect(isolated) as conn:
            apply_migrations(conn)
            store, intent, policy = seeded_intent(conn)
            conn.commit()
            yield DashboardConfig(root=tmp_path, dsn=dsn, schema=schema), conn, store, intent, policy
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_real_decision_graph_preserves_evidence_and_absent_risk_record(decision_db):
    from dashboard.backend.db import DatabaseReader
    from dashboard.backend.decisions import DecisionReader
    config, _, _, intent, _ = decision_db
    reader = DecisionReader(DatabaseReader(config))
    listing = reader.list()['data']['items']
    assert listing[0]['decision_id'] == str(intent.decision_id)
    detail = reader.detail(str(intent.decision_id))['data']
    assert detail['decision']['reason_codes']
    assert detail['evidence'][0]['quality_status'] == 'VALID'
    assert detail['evidence'][0]['freshness_status'] == 'FRESH'
    assert detail['patterns'][0]['pattern_policy_version']
    assert detail['risk']['status'] == 'NOT_RECORDED'
    assert detail['intents'] == []
    assert [step['id'] for step in detail['chain']] == ['market','screening','evidence','strategy','decision','risk','intent','nautilus']
    assert 'REJECTED' != detail['risk']['status']


def test_persisted_approved_intent_and_cumulative_results_are_audit_only(decision_db):
    from dashboard.backend.db import DatabaseReader
    from dashboard.backend.decisions import DecisionReader
    from dashboard.backend.service import DashboardService
    from quant_execution.contracts import ExecutionResultV1
    from tests.quant_execution.fixtures import NOW
    config, conn, store, intent, policy = decision_db
    store.reserve(intent, policy, now=NOW)
    first = ExecutionResultV1(intent.intent_id,'audit-1',intent.client_order_id,('venue-order-1',),
        'FILLED',intent.approved_quantity,intent.approved_quantity,Decimal('0'),Decimal('50001'),
        Decimal('0.25'),'USDT',None,'PENDING',NOW,NOW,'fixture','test-v1')
    store.record_result(first)
    store.record_result(replace(first, execution_id='protection-2', protection_status='ACTIVE',
        event_time=NOW+timedelta(seconds=1),received_at=NOW+timedelta(seconds=1)))
    conn.commit()
    detail = DecisionReader(DatabaseReader(config)).detail(str(intent.decision_id))['data']
    assert detail['risk']['status'] == 'APPROVED'
    assert detail['risk']['risk_policy_version'] == intent.risk_policy_version
    assert detail['intents'][0]['approved_quantity'] == str(intent.approved_quantity)
    assert len(detail['execution_audit']) == 2
    assert detail['execution_audit'][0]['filled_quantity'] == str(intent.approved_quantity)
    assert DashboardService(config).trades()['data']['items'] == []


def test_unknown_decision_is_no_data_and_detail_uuid_is_validated(decision_db):
    from fastapi.testclient import TestClient
    from dashboard.backend.app import create_app
    config, _, _, _, _ = decision_db
    api = TestClient(create_app(config),base_url='http://127.0.0.1:3000')
    assert api.get('/api/decisions/'+str(uuid4())).json()['availability'] == 'NO_DATA'
    response = api.get('/api/decisions/password=do-not-echo')
    assert response.status_code == 422
    assert 'do-not-echo' not in response.text


def test_projection_uses_real_execution_quantity_names(decision_db, monkeypatch):
    from dashboard.backend.service import DashboardService
    from quant_execution.contracts import ExecutionResultV1
    from tests.quant_execution.fixtures import NOW
    config, conn, store, intent, policy = decision_db
    store.reserve(intent, policy, now=NOW)
    store.record_result(ExecutionResultV1(intent.intent_id,'typed-result',intent.client_order_id,('venue-order-1',),
        'FILLED',intent.approved_quantity,intent.approved_quantity,Decimal('0'),Decimal('50001'),
        Decimal('0.25'),'USDT',None,'ACTIVE',NOW,NOW,'fixture','test-v1'))
    conn.commit()
    service = DashboardService(config)
    paper = {'state':'RUNNING','currents':[{'position':{'canonical_symbol':'BTC-USDT-PERP',
        'account_id':str(intent.account_id)},
        'source':'artifacts/phase9/ready.json','session_id':'a'*24,'freshness':'CURRENT',
        'order_states':{intent.client_order_id:'FILLED'}}],'sessions':[]}
    monkeypatch.setattr(service,'sources',lambda: (service.database.snapshot(),paper))
    order = service.orders()['data']['items'][0]
    assert order['approved_quantity'] == str(intent.approved_quantity)
    assert order['requested_quantity'] == str(intent.approved_quantity)
    assert order['filled_quantity'] == str(intent.approved_quantity)
    assert order['remaining_quantity'] == '0'
    assert order['order_type'] == 'MARKET'
    assert order['limit_price'] is None


def test_orders_do_not_return_cumulative_results_without_a_native_session(decision_db, monkeypatch):
    from dashboard.backend.service import DashboardService
    from quant_execution.contracts import ExecutionResultV1
    from tests.quant_execution.fixtures import NOW

    config, conn, store, intent, policy = decision_db
    store.reserve(intent, policy, now=NOW)
    store.record_result(ExecutionResultV1(intent.intent_id, 'unscoped-result', intent.client_order_id,
        ('venue-order-1',), 'FILLED', intent.approved_quantity, intent.approved_quantity, Decimal('0'),
        Decimal('50001'), Decimal('0.25'), 'USDT', None, 'ACTIVE', NOW, NOW, 'fixture', 'test-v1'))
    conn.commit()
    service = DashboardService(config)
    paper = {'state': 'STOPPED', 'currents': [], 'sessions': []}
    monkeypatch.setattr(service, 'sources', lambda: (service.database.snapshot(), paper))

    response = service.orders()
    assert response['data']['items'] == []
    assert response['data']['execution_audit'] == []


def test_orders_audit_only_joins_the_selected_paper_order(decision_db, monkeypatch):
    from dashboard.backend.service import DashboardService
    from quant_execution.contracts import ExecutionResultV1
    from tests.quant_execution.fixtures import NOW
    from tests.quant_execution.test_persistence import seeded_intent

    config, conn, store, intent, policy = decision_db
    store.reserve(intent, policy, now=NOW)
    store.record_result(ExecutionResultV1(intent.intent_id, 'selected-result', intent.client_order_id,
        ('venue-order-1',), 'FILLED', intent.approved_quantity, intent.approved_quantity, Decimal('0'),
        Decimal('50001'), Decimal('0.25'), 'USDT', None, 'ACTIVE', NOW, NOW, 'fixture', 'test-v1'))
    other_store, other_intent, other_policy = seeded_intent(conn)
    other_store.reserve(other_intent, other_policy, now=NOW)
    other_store.record_result(ExecutionResultV1(other_intent.intent_id, 'unrelated-result',
        other_intent.client_order_id, ('venue-order-2',), 'FILLED', other_intent.approved_quantity,
        other_intent.approved_quantity, Decimal('0'), Decimal('51000'), Decimal('0.25'), 'USDT', None,
        'ACTIVE', NOW, NOW, 'fixture', 'test-v1'))
    conn.commit()
    service = DashboardService(config)
    paper = {'state': 'RUNNING', 'currents': [{
        'position': {'canonical_symbol': 'BTC-USDT-PERP', 'account_id': str(intent.account_id)},
        'source': 'artifacts/phase9/ready.json', 'session_id': 'a' * 24, 'freshness': 'CURRENT',
        'order_states': {intent.client_order_id: 'FILLED'},
    }], 'sessions': []}
    monkeypatch.setattr(service, 'sources', lambda: (service.database.snapshot(), paper))

    response = service.orders()['data']
    assert [row['client_order_id'] for row in response['items']] == [intent.client_order_id]
    assert [row['execution_id'] for row in response['execution_audit']] == ['selected-result']
    assert response['execution_audit'][0]['session_id'] == 'a' * 24
