import os
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from quant_phase1.db import apply_migrations
from quant_execution.persistence import ExecutionStore, ReservationRejected, FencedOwner
from quant_execution.risk import approve_intent
from tests.quant_execution.fixtures import NOW, risk_inputs
from tests.quant_phase9.test_persistence import _seed, _call
from quant_execution.contracts import ExecutionResultV1


@pytest.fixture
def store_db():
    dsn = os.environ['TEST_POSTGRES_DSN']
    assert '127.0.0.1' in dsn and 'quant_phase9_test' in dsn
    schema = 'execution_test_' + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    isolated = make_conninfo(dsn, options=f'-c search_path={schema}')
    try:
        with psycopg.connect(isolated) as conn:
            apply_migrations(conn)
        yield isolated
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def seeded_intent(conn, *, max_open=2):
    number = 91 + conn.execute('SELECT count(*) FROM phase9_evaluations').fetchone()[0]
    event_id = str(number).zfill(64)
    data = _seed(conn, candidate_id=number, event_id=event_id)
    _call(conn, data, event_id=event_id)
    conn.commit()
    inputs = risk_inputs()
    inputs['envelope'] = replace(inputs['envelope'], candidate=data[4])
    inputs['policy'] = replace(inputs['policy'], max_open_intents=max_open)
    store = ExecutionStore(conn)
    store.record_account(inputs['account'])
    conn.commit()
    return store, approve_intent(**inputs), inputs['policy']


def test_intent_reservation_idempotence_and_immutable_audit(store_db):
    with psycopg.connect(store_db) as conn:
        store, intent, policy = seeded_intent(conn)
        assert store.reserve(intent, policy, now=NOW) is True
        assert store.reserve(intent, policy, now=NOW) is False
        assert store.intent(intent.intent_id) == intent
        assert conn.execute('SELECT count(*) FROM execution_reservations').fetchone()[0] == 1
        with pytest.raises(psycopg.errors.RaiseException):
            with conn.transaction():
                conn.execute("UPDATE execution_intents SET payload='{}'::jsonb")


def test_concurrent_risk_reservations_cannot_overbook_one_account(store_db):
    with psycopg.connect(store_db) as conn:
        _, first, policy = seeded_intent(conn, max_open=1)
        conn.commit()
    def reserve():
        with psycopg.connect(store_db) as conn:
            return ExecutionStore(conn).reserve(first, policy, now=NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: reserve(), range(2))) == [False, True]
    with psycopg.connect(store_db) as conn:
        store, second, policy = seeded_intent(conn, max_open=1)
        with pytest.raises(ReservationRejected, match='CONCURRENCY_LIMIT'):
            store.reserve(second, policy, now=NOW)


def test_unknown_send_requires_query_before_retry_and_old_owner_is_fenced(store_db):
    with psycopg.connect(store_db) as conn:
        store, intent, policy = seeded_intent(conn)
        store.reserve(intent, policy, now=NOW)
        epoch = store.acquire_owner(intent.account_id, 'first', now=NOW, lease_seconds=5)
        assert store.begin_submission(intent.intent_id, 'first', epoch, now=NOW) is True
        assert store.begin_submission(intent.intent_id, 'first', epoch, now=NOW) is False
        with pytest.raises(FencedOwner):
            store.acquire_owner(intent.account_id, 'second', now=NOW, lease_seconds=5)
        conn.commit()
    with psycopg.connect(store_db) as conn:
        store = ExecutionStore(conn)
        new_epoch = store.acquire_owner(intent.account_id, 'second', now=NOW + timedelta(seconds=6), lease_seconds=5)
        assert new_epoch > epoch
        with pytest.raises(FencedOwner):
            store.begin_submission(intent.intent_id, 'first', epoch, now=NOW + timedelta(seconds=6))
        assert store.recover_pending(intent.account_id) == (intent.intent_id,)
        assert store.submission_state(intent.intent_id) == 'UNKNOWN'
        assert store.begin_submission(intent.intent_id, 'second', new_epoch, now=NOW + timedelta(seconds=6)) is False


def test_result_resolves_submitting_when_simulated_event_time_precedes_submit_clock(store_db):
    with psycopg.connect(store_db) as conn:
        store, intent, policy = seeded_intent(conn)
        store.reserve(intent, policy, now=NOW)
        epoch = store.acquire_owner(intent.account_id, 'paper-worker', now=NOW, lease_seconds=60)
        submit_at = NOW + timedelta(seconds=2)
        assert store.begin_submission(intent.intent_id, 'paper-worker', epoch, now=submit_at)

        result = ExecutionResultV1(
            intent.intent_id, 'simulated-result', intent.client_order_id, ('paper-order',),
            'FILLED', intent.approved_quantity, intent.approved_quantity, D('0'),
            intent.reference_price, D('0'), intent.settlement_currency, None, 'ACTIVE',
            NOW, NOW, 'LocalSandboxFill', 'test-v1',
        )
        store.record_result(result)

        assert store.submission_state(intent.intent_id) == 'FILLED'
        updated_at = conn.execute(
            'SELECT updated_at FROM execution_submission_states WHERE intent_id=%s',
            (intent.intent_id,),
        ).fetchone()[0]
        assert updated_at == submit_at


def test_reservation_rechecks_persisted_lifecycle_ttl_and_hash(store_db):
    with psycopg.connect(store_db) as conn:
        store, intent, policy = seeded_intent(conn)
        conn.execute("INSERT INTO phase9_decision_status_events (event_id,decision_id,evaluation_id,status,event_time,reason_code,payload) VALUES (%s,%s,%s,'INVALIDATED',%s,'fixture','{}'::jsonb)",
            (uuid4(), intent.decision_id, intent.evaluation_id, NOW + timedelta(seconds=1)))
        with pytest.raises(ReservationRejected, match='DECISION_UNAVAILABLE'):
            store.reserve(intent, policy, now=NOW + timedelta(seconds=2))


def test_stale_persisted_account_blocks_new_reservation(store_db):
    with psycopg.connect(store_db) as conn:
        store, intent, policy = seeded_intent(conn)
        with pytest.raises(ReservationRejected, match='STALE_ACCOUNT'):
            store.reserve(intent, policy, now=NOW + timedelta(seconds=6))


def test_execution_audit_rejects_decreasing_fills_and_keeps_partial_cancel_reserved(store_db):
    with psycopg.connect(store_db) as conn:
        store, intent, policy = seeded_intent(conn)
        store.reserve(intent, policy, now=NOW)
        partial = ExecutionResultV1(intent.intent_id, 'fill-1', intent.client_order_id, ('venue-1',),
            'PARTIALLY_FILLED', D('0.019'), D('0.010'), D('0.009'), D('50001'),
            D('0.25'), 'USDT', None, 'ACTIVE', NOW, NOW, 'fixture', 'test-v1')
        store.record_result(partial)
        assert store.submission_state(intent.intent_id) == 'PARTIALLY_FILLED'
        with pytest.raises(ReservationRejected, match='DECREASING_FILL'):
            store.record_result(replace(partial, execution_id='bad-fill', filled_quantity=D('0.005'),
                remaining_quantity=D('0.014'), received_at=NOW + timedelta(seconds=1)))
        store.record_result(replace(partial, execution_id='cancel-1', status='CANCELLED',
            received_at=NOW + timedelta(seconds=2)))
        assert conn.execute('SELECT state FROM execution_reservations').fetchone()[0] == 'ACTIVE'
        assert conn.execute("SELECT count(*) FROM execution_results WHERE execution_id='bad-fill'").fetchone()[0] == 0


def test_duplicate_earlier_fill_is_idempotent_after_later_cumulative_fill(store_db):
    with psycopg.connect(store_db) as conn:
        store, intent, policy = seeded_intent(conn)
        store.reserve(intent, policy, now=NOW)
        partial = ExecutionResultV1(intent.intent_id, 'fill-first', intent.client_order_id, ('venue-1',),
            'PARTIALLY_FILLED', D('0.019'), D('0.010'), D('0.009'), D('50001'),
            D('0.25'), 'USDT', None, 'ACTIVE', NOW, NOW, 'fixture', 'test-v1')
        final = replace(partial, execution_id='fill-last', status='FILLED',
            filled_quantity=D('0.019'), remaining_quantity=D('0'), fees=D('0.40'),
            event_time=NOW+timedelta(seconds=1), received_at=NOW+timedelta(seconds=1))
        store.record_result(partial)
        store.record_result(final)
        store.record_result(partial)
        assert store.submission_state(intent.intent_id) == 'FILLED'
        assert conn.execute('SELECT count(*) FROM execution_results').fetchone()[0] == 2
