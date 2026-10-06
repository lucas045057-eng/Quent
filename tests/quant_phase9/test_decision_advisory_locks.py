from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from quant_phase1.db import apply_migrations
from quant_phase9.lifecycle import (
    acquire_decision_advisory_lock,
    decision_advisory_lock_key,
)


@pytest.fixture
def lock_database():
    migration_dsn = os.environ.get("TEST_POSTGRES_DSN")
    app_dsn = os.environ.get("TEST_POSTGRES_APP_DSN")
    if not migration_dsn or not app_dsn:
        pytest.skip("isolated migration and app TEST_POSTGRES DSNs are required")
    for value in (migration_dsn, app_dsn):
        info = conninfo_to_dict(value)
        assert info.get("host") in {"localhost", "127.0.0.1", "::1"}
        assert info.get("dbname") == "quant_phase9_test"
    schema = f"phase9_advisory_test_{uuid4().hex}"

    def connect(*, app=False, autocommit=False):
        dsn = app_dsn if app else migration_dsn
        return psycopg.connect(
            dsn,
            autocommit=autocommit,
            # Preserve an explicit restricted role in the supplied DSN when
            # adding this fixture's isolated schema search path.
            options=conninfo_to_dict(dsn).get('options','')+f" -c search_path={schema},public",
        )

    with psycopg.connect(migration_dsn, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        with connect() as migration:
            apply_migrations(migration)
        from scripts.phase9_migrate import grant_runtime_privileges
        with connect() as migration:
            grant_runtime_privileges(migration, schema_name=schema)
        yield connect, schema
    finally:
        with psycopg.connect(migration_dsn, autocommit=True) as cleanup:
            cleanup.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema)
                )
            )


def test_decision_lock_key_is_stable_and_scoped_per_id():
    first = uuid4()
    second = uuid4()
    assert decision_advisory_lock_key(first) == decision_advisory_lock_key(str(first))
    assert decision_advisory_lock_key(first) != decision_advisory_lock_key(second)


def test_same_decision_id_serializes_concurrent_transactions(lock_database):
    connect, _ = lock_database
    decision_id = uuid4()
    owner = connect()
    entered = Event()
    started = Event()

    def contender():
        with connect(app=True) as conn:
            started.set()
            acquire_decision_advisory_lock(conn, decision_id)
            entered.set()

    try:
        acquire_decision_advisory_lock(owner, decision_id)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(contender)
            assert started.wait(2)
            assert not entered.wait(0.2)
            owner.commit()
            assert entered.wait(2)
            future.result(timeout=2)
    finally:
        owner.close()


def test_different_decision_ids_do_not_share_a_global_lock(lock_database):
    connect, _ = lock_database
    first = connect()
    second = connect(app=True)
    try:
        assert acquire_decision_advisory_lock(first, uuid4())
        assert acquire_decision_advisory_lock(second, uuid4(), wait=False)
    finally:
        first.rollback()
        second.rollback()
        first.close()
        second.close()


@pytest.mark.parametrize("finish", ["commit", "rollback"])
def test_transaction_lock_is_released_at_transaction_end(lock_database, finish):
    connect, _ = lock_database
    decision_id = uuid4()
    owner = connect()
    contender = connect(app=True)
    try:
        assert acquire_decision_advisory_lock(owner, decision_id)
        assert not acquire_decision_advisory_lock(
            contender, decision_id, wait=False
        )
        getattr(owner, finish)()
        assert acquire_decision_advisory_lock(
            contender, decision_id, wait=False
        )
    finally:
        owner.rollback()
        contender.rollback()
        owner.close()
        contender.close()


def test_supersede_persistence_and_duplicate_retry_without_candidate_update(
    lock_database,
):
    from dataclasses import replace

    from tests.quant_phase9.test_persistence import NOW, _call, _seed

    connect, schema = lock_database
    event_one = uuid4().hex * 2
    event_two = uuid4().hex * 2
    with connect(app=True) as conn:
        first = _seed(conn, candidate_id=910001, event_id=event_one)
        _call(conn, first, event_id=event_one)
        conn.commit()

        second = list(_seed(
            conn, candidate_id=910001, event_id=event_two,
            material_generation="following",
        ))
        second[4] = replace(
            second[4], supersedes_decision_id=first[4].decision_id
        )
        _call(conn, second, event_id=event_two)
        conn.commit()

        _call(conn, second, event_id=event_two)
        conn.commit()

        has_update = conn.execute(
            "SELECT has_table_privilege(current_user, %s, 'UPDATE')",
            (f"{schema}.phase9_decision_candidates",),
        ).fetchone()[0]
        assert has_update is False
        assert conn.execute(
            "SELECT count(*) FROM phase9_decision_candidates"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT count(*) FROM phase9_decision_status_events "
            "WHERE status='SUPERSEDED' AND decision_id=%s",
            (first[4].decision_id,),
        ).fetchone()[0] == 1


def test_expiry_reconciliation_is_idempotent_without_candidate_update(
    lock_database,
):
    from tests.quant_phase9.test_persistence import NOW, _call, _seed
    from quant_phase9.lifecycle import expire_decisions

    connect, schema = lock_database
    event_id = uuid4().hex * 2
    with connect(app=True) as conn:
        fixture = _seed(conn, candidate_id=910003, event_id=event_id)
        _call(conn, fixture, event_id=event_id)
        conn.commit()

        before = conn.execute(
            "SELECT payload FROM phase9_decision_candidates"
        ).fetchall()
        assert expire_decisions(
            conn, now=NOW + timedelta(hours=2)
        ) == 1
        conn.commit()
        assert expire_decisions(
            conn, now=NOW + timedelta(hours=2)
        ) == 0
        conn.commit()

        has_update = conn.execute(
            "SELECT has_table_privilege(current_user, %s, 'UPDATE')",
            (f"{schema}.phase9_decision_candidates",),
        ).fetchone()[0]
        assert has_update is False
        assert conn.execute(
            "SELECT count(*) FROM phase9_decision_status_events "
            "WHERE status='EXPIRED'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT payload FROM phase9_decision_candidates"
        ).fetchall() == before
