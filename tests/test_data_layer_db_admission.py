"""PostgreSQL-backed contract tests for cross-process write admission."""

from __future__ import annotations

from multiprocessing import get_context
import os
import queue
import threading
import time
import uuid

import pytest

from quant_data_layer.db_admission import (
    DbAdmissionDeferred,
    DbAdmissionDeferredReason,
    DbAdmissionLimits,
    DbWorkClass,
    PostgresWriteAdmission,
)


DSN = os.environ.get("TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_POSTGRES_DSN is required for isolated PostgreSQL admission tests")


def _limits(slot_count: int = 2, *, max_waiters: int = 4) -> DbAdmissionLimits:
    return DbAdmissionLimits(
        slot_count=slot_count,
        max_waiters=max_waiters,
        poll_interval_seconds=0.02,
    )


def _hold_transaction(dsn: str, namespace: int, work_class: str, events, release, *, admission_pids=None):
    import psycopg

    def factory(connection_dsn, **kwargs):
        connection = psycopg.connect(connection_dsn, **kwargs)
        if kwargs.get("autocommit") and admission_pids is not None:
            admission_pids.put(connection.info.backend_pid)
        return connection

    controller = PostgresWriteAdmission(
        connection_factory=factory,
        limits=_limits(),
        lock_namespace=namespace,
    )
    try:
        with controller.transaction(
            dsn,
            work_class=DbWorkClass(work_class),
            timeout_seconds=2.0,
            identity="test-holder",
        ) as connection:
            events.put(("acquired", os.getpid(), connection.info.backend_pid))
            release.wait(8)
            connection.execute("SELECT 1")
        events.put(("committed", os.getpid()))
    except DbAdmissionDeferred as exc:
        events.put(("deferred", exc.reason.value))
    except BaseException as exc:  # reported to parent without exception text
        events.put(("error", type(exc).__name__))


def _compete_once(dsn: str, namespace: int, output):
    controller = PostgresWriteAdmission(
        limits=_limits(),
        lock_namespace=namespace,
    )
    try:
        with controller.transaction(
            dsn,
            work_class=DbWorkClass.HEAVY,
            timeout_seconds=0.5,
            identity="test-contender",
        ) as connection:
            connection.execute("SELECT 1")
            output.put(("acquired", None))
    except DbAdmissionDeferred as exc:
        output.put(("deferred", exc.reason.value))


def _abrupt_exit_worker(dsn, lock_namespace, ready_queue, release_event):
    controller = PostgresWriteAdmission(limits=_limits(), lock_namespace=lock_namespace)
    with controller.transaction(
        dsn,
        work_class=DbWorkClass.HEAVY,
        timeout_seconds=1,
        identity="crash",
    ) as conn:
        conn.execute("CREATE TEMP TABLE admission_crash_probe (value integer)")
        ready_queue.put("acquired")
        release_event.wait(5)
        os._exit(0)


def _new_namespace() -> int:
    return 1_200_000_000 + (uuid.uuid4().int % 900_000_000)


def _start_holder(ctx, dsn, namespace, work_class, *, admission_pids=None):
    events = ctx.Queue()
    release = ctx.Event()
    process = ctx.Process(
        target=_hold_transaction,
        args=(dsn, namespace, work_class, events, release),
        kwargs={"admission_pids": admission_pids},
    )
    process.start()
    message = events.get(timeout=5)
    assert message[0] == "acquired", message
    return process, events, release, message


def test_two_processes_can_hold_distinct_slots_for_mixed_classes():
    ctx = get_context("spawn")
    namespace = _new_namespace()
    events = ctx.Queue()
    release = ctx.Event()
    workers = [
        ctx.Process(target=_hold_transaction, args=(DSN, namespace, work_class, events, release))
        for work_class in (DbWorkClass.LIGHT.value, DbWorkClass.HEAVY.value)
    ]
    try:
        for worker in workers:
            worker.start()
        acquired = [events.get(timeout=8) for _ in workers]
        assert all(item[0] == "acquired" for item in acquired), acquired
        assert len({item[2] for item in acquired}) == 2
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=5)
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=2)


def test_full_slot_pool_defers_without_entering_business_transaction():
    ctx = get_context("spawn")
    namespace = _new_namespace()
    holders = [_start_holder(ctx, DSN, namespace, DbWorkClass.HEAVY.value) for _ in range(2)]
    try:
        controller = PostgresWriteAdmission(limits=_limits(), lock_namespace=namespace)
        with pytest.raises(DbAdmissionDeferred) as raised:
            with controller.transaction(
                DSN,
                work_class=DbWorkClass.MEDIUM,
                timeout_seconds=0.12,
                identity="bounded-wait-test",
            ):
                pytest.fail("a writer without a slot must never enter its transaction")
        assert raised.value.reason is DbAdmissionDeferredReason.SLOT_TIMEOUT
    finally:
        for process, _, release, _ in holders:
            release.set()
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)


def test_slot_wait_uses_one_dedicated_admission_session():
    import psycopg

    ctx = get_context("spawn")
    namespace = _new_namespace()
    holders = [_start_holder(ctx, DSN, namespace, DbWorkClass.HEAVY.value) for _ in range(2)]
    opened: list[bool] = []

    def factory(connection_dsn, **kwargs):
        opened.append(bool(kwargs.get("autocommit")))
        return psycopg.connect(connection_dsn, **kwargs)

    controller = PostgresWriteAdmission(
        connection_factory=factory,
        limits=_limits(),
        lock_namespace=namespace,
    )
    try:
        with pytest.raises(DbAdmissionDeferred) as raised:
            with controller.transaction(
                DSN,
                work_class=DbWorkClass.MEDIUM,
                timeout_seconds=0.15,
                identity="single-admission-session",
            ):
                pytest.fail("a full pool must not enter a business transaction")
        assert raised.value.reason is DbAdmissionDeferredReason.SLOT_TIMEOUT
        assert opened == [True]
    finally:
        for process, _, release, _ in holders:
            release.set()
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)


def test_admission_and_business_connections_are_distinct_and_slot_is_reusable():
    import psycopg

    dsn = DSN
    namespace = _new_namespace()
    admission_pids: list[int] = []
    business_pids: list[int] = []

    def factory(connection_dsn, **kwargs):
        connection = psycopg.connect(connection_dsn, **kwargs)
        (admission_pids if kwargs.get("autocommit") else business_pids).append(connection.info.backend_pid)
        return connection

    controller = PostgresWriteAdmission(
        connection_factory=factory,
        limits=_limits(slot_count=2),
        lock_namespace=namespace,
    )
    with controller.transaction(dsn, work_class=DbWorkClass.HEAVY, timeout_seconds=1, identity="distinct") as conn:
        snapshot = controller.snapshot()
        assert snapshot.pending_by_class[DbWorkClass.HEAVY] == 0
        assert snapshot.active_by_class[DbWorkClass.HEAVY] == 1
        conn.execute("SELECT 1")
    assert admission_pids and business_pids
    assert set(admission_pids).isdisjoint(business_pids)
    assert controller.snapshot().active_by_class[DbWorkClass.HEAVY] == 0

    with controller.transaction(dsn, work_class=DbWorkClass.LIGHT, timeout_seconds=1, identity="reuse") as conn:
        conn.execute("SELECT 1")


def test_supplied_idle_business_connection_is_separate_and_remains_caller_owned():
    import psycopg

    dsn = DSN
    admission_pids: list[int] = []

    def factory(connection_dsn, **kwargs):
        connection = psycopg.connect(connection_dsn, **kwargs)
        admission_pids.append(connection.info.backend_pid)
        return connection

    controller = PostgresWriteAdmission(
        connection_factory=factory,
        limits=_limits(),
        lock_namespace=_new_namespace(),
    )
    with psycopg.connect(dsn, autocommit=True) as business_connection:
        business_pid = business_connection.info.backend_pid
        with controller.transaction(
            dsn,
            work_class=DbWorkClass.HEAVY,
            timeout_seconds=1,
            identity="caller-business-connection",
            business_connection=business_connection,
        ) as admitted_connection:
            assert admitted_connection is business_connection
            assert admitted_connection.execute("SELECT 1").fetchone() == (1,)
        assert not business_connection.closed
    assert len(admission_pids) == 1
    assert business_pid not in admission_pids


def test_admission_failure_is_fail_closed_and_does_not_run_writer():
    import psycopg

    controller = PostgresWriteAdmission(
        connection_factory=psycopg.connect,
        limits=_limits(),
        lock_namespace=_new_namespace(),
    )
    writer_ran = False
    with pytest.raises(DbAdmissionDeferred) as raised:
        with controller.transaction(
            "postgresql://quant_test@127.0.0.1:1/unavailable",
            work_class=DbWorkClass.HEAVY,
            timeout_seconds=0.1,
            identity="unavailable-db",
        ):
            writer_ran = True
    assert raised.value.reason is DbAdmissionDeferredReason.DATABASE_UNAVAILABLE
    assert writer_ran is False


def test_pending_and_active_transaction_classes_are_observable():
    ctx = get_context("spawn")
    namespace = _new_namespace()
    holders = [_start_holder(ctx, DSN, namespace, DbWorkClass.HEAVY.value) for _ in range(2)]
    controller = PostgresWriteAdmission(limits=_limits(max_waiters=2), lock_namespace=namespace)
    result: queue.Queue[object] = queue.Queue()

    def wait_for_slot():
        try:
            with controller.transaction(
                DSN,
                work_class=DbWorkClass.LIGHT,
                timeout_seconds=0.3,
                identity="observable-waiter",
            ):
                result.put("unexpected-acquire")
        except DbAdmissionDeferred as exc:
            result.put(exc.reason)

    thread = threading.Thread(target=wait_for_slot)
    try:
        thread.start()
        deadline = time.monotonic() + 1
        while controller.snapshot().pending_by_class[DbWorkClass.LIGHT] == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        snapshot = controller.snapshot()
        assert snapshot.pending_by_class[DbWorkClass.LIGHT] == 1
        assert snapshot.pending_count <= 2
        thread.join(timeout=2)
        assert result.get_nowait() is DbAdmissionDeferredReason.SLOT_TIMEOUT
    finally:
        for process, _, release, _ in holders:
            release.set()
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)


def test_transaction_body_failure_rolls_back_and_releases_slot():
    import psycopg

    marker = f"dlv1_admission_{uuid.uuid4().hex[:12]}"
    namespace = _new_namespace()
    controller = PostgresWriteAdmission(limits=_limits(), lock_namespace=namespace)
    with psycopg.connect(DSN) as setup:
        setup.execute(f'CREATE TABLE "{marker}" (value integer PRIMARY KEY)')
    try:
        with pytest.raises(RuntimeError, match="injected"):
            with controller.transaction(DSN, work_class=DbWorkClass.HEAVY, timeout_seconds=1, identity="rollback") as conn:
                conn.execute(f'INSERT INTO "{marker}" VALUES (1)')
                raise RuntimeError("injected rollback")
        with psycopg.connect(DSN) as verify:
            assert verify.execute(f'SELECT count(*) FROM "{marker}"').fetchone()[0] == 0
        with controller.transaction(DSN, work_class=DbWorkClass.MEDIUM, timeout_seconds=1, identity="reuse-after-failure") as conn:
            conn.execute(f'INSERT INTO "{marker}" VALUES (2)')
        with psycopg.connect(DSN) as verify:
            assert verify.execute(f'SELECT value FROM "{marker}"').fetchall() == [(2,)]
    finally:
        with psycopg.connect(DSN) as cleanup:
            cleanup.execute(f'DROP TABLE IF EXISTS "{marker}"')


@pytest.mark.parametrize("failure", ["bitcoin_chunk_n", "bitcoin_checkpoint", "ethereum_checkpoint"])
def test_phase7_event_and_cursor_failures_rollback_inside_db_admission(failure):
    import psycopg

    from quant_phase7.persistence import MAX_TRANSFER_BATCH, Phase7Repository

    suffix = uuid.uuid4().hex[:12]
    event_table = f"dlv1_events_{suffix}"
    cursor_table = f"dlv1_cursor_{suffix}"
    controller = PostgresWriteAdmission(limits=_limits(), lock_namespace=_new_namespace())
    with psycopg.connect(DSN) as setup:
        setup.execute(f'CREATE TABLE "{event_table}" (event_id text PRIMARY KEY)')
        setup.execute(f'CREATE TABLE "{cursor_table}" (id integer PRIMARY KEY, value integer NOT NULL)')
        setup.execute(f'INSERT INTO "{cursor_table}" VALUES (1, 100)')

    class ProbeRepository(Phase7Repository):
        def __init__(self, connection):
            super().__init__(connection)
            self.chunk_number = 0

        def upsert_transfer_events(self, events):
            batch = tuple(events)
            self.chunk_number += 1
            if failure == "bitcoin_chunk_n" and self.chunk_number == 2:
                partial = batch[:max(1, len(batch) // 2)]
                with self.connection.cursor() as cursor:
                    cursor.executemany(
                        f'INSERT INTO "{event_table}" VALUES (%s)', [(value,) for value in partial]
                    )
                raise RuntimeError("injected transfer chunk failure")
            with self.connection.cursor() as cursor:
                cursor.executemany(
                    f'INSERT INTO "{event_table}" VALUES (%s) ON CONFLICT DO NOTHING',
                    [(value,) for value in batch],
                )
            return len(batch)

        def upsert_checkpoint(self, row):
            if failure.endswith("checkpoint"):
                return 0
            result = self.connection.execute(
                f'UPDATE "{cursor_table}" SET value = %s WHERE id = 1',
                (int(row["cursor_value"]),),
            )
            return result.rowcount

    checkpoint = {"cursor_value": "101"}
    try:
        with psycopg.connect(DSN, autocommit=True) as business_connection:
            with pytest.raises((RuntimeError, ValueError)):
                with controller.transaction(
                    DSN,
                    work_class=DbWorkClass.HEAVY,
                    timeout_seconds=2,
                    identity=f"phase7-{failure}",
                    business_connection=business_connection,
                ) as connection:
                    repository = ProbeRepository(connection)
                    if failure == "ethereum_checkpoint":
                        repository.persist_transfer_batch_and_checkpoint(
                            (f"event-{index}" for index in range(12)), checkpoint
                        )
                    else:
                        repository.persist_transfer_chunks_and_checkpoint(
                            (f"event-{index}" for index in range(MAX_TRANSFER_BATCH + 1)), checkpoint
                        )
            assert not business_connection.closed
        with psycopg.connect(DSN) as verify:
            event_count = verify.execute(f'SELECT count(*) FROM "{event_table}"').fetchone()[0]
            cursor_value = verify.execute(f'SELECT value FROM "{cursor_table}" WHERE id = 1').fetchone()[0]
        assert event_count == 0
        assert cursor_value == 100
    finally:
        with psycopg.connect(DSN) as cleanup:
            cleanup.execute(f'DROP TABLE IF EXISTS "{event_table}"')
            cleanup.execute(f'DROP TABLE IF EXISTS "{cursor_table}"')


def test_admission_session_loss_fence_prevents_overlapping_business_writer():
    ctx = get_context("spawn")
    namespace = _new_namespace()
    admission_pids = ctx.Queue()
    process, events, release, acquired = _start_holder(
        ctx,
        DSN,
        namespace,
        DbWorkClass.HEAVY.value,
        admission_pids=admission_pids,
    )
    admission_pid = admission_pids.get(timeout=3)
    import psycopg

    with psycopg.connect(DSN) as admin:
        admin.execute("SELECT pg_terminate_backend(%s)", (admission_pid,))
    output = ctx.Queue()
    contender = ctx.Process(target=_compete_once, args=(DSN, namespace, output))
    try:
        contender.start()
        message = output.get(timeout=5)
        assert message == ("deferred", DbAdmissionDeferredReason.FENCE_BUSY.value)
    finally:
        release.set()
        process.join(timeout=5)
        contender.join(timeout=5)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
        if contender.is_alive():
            contender.terminate()
            contender.join(timeout=2)


def test_controller_close_rejects_new_work_and_waiter_limit_is_bounded():
    ctx = get_context("spawn")
    namespace = _new_namespace()
    holders = [_start_holder(ctx, DSN, namespace, DbWorkClass.HEAVY.value) for _ in range(2)]
    controller = PostgresWriteAdmission(limits=_limits(max_waiters=1), lock_namespace=namespace)
    started = threading.Event()
    outcomes: queue.Queue[object] = queue.Queue()

    def first_waiter():
        started.set()
        try:
            with controller.transaction(DSN, work_class=DbWorkClass.LIGHT, timeout_seconds=5, identity="one"):
                outcomes.put("unexpected")
        except DbAdmissionDeferred as exc:
            outcomes.put(exc.reason)

    thread = threading.Thread(target=first_waiter)
    try:
        thread.start()
        assert started.wait(1)
        deadline = time.monotonic() + 1
        while controller.snapshot().pending_count == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        with pytest.raises(DbAdmissionDeferred) as raised:
            with controller.transaction(DSN, work_class=DbWorkClass.MEDIUM, timeout_seconds=0.1, identity="overflow"):
                pytest.fail("bounded waiter limit must reject before business work")
        assert raised.value.reason is DbAdmissionDeferredReason.WAITER_LIMIT
        assert controller.close(timeout_seconds=0) is False
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert outcomes.get_nowait() is DbAdmissionDeferredReason.CONTROLLER_CLOSED
        assert controller.snapshot().pending_count == 0
        assert controller.close(timeout_seconds=1) is True
        with pytest.raises(DbAdmissionDeferred) as closed:
            with controller.transaction(DSN, work_class=DbWorkClass.LIGHT, timeout_seconds=0.1, identity="closed"):
                pytest.fail("closed controller must reject work")
        assert closed.value.reason is DbAdmissionDeferredReason.CONTROLLER_CLOSED
    finally:
        for process, _, release, _ in holders:
            release.set()
            process.join(timeout=5)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)


def test_close_reports_active_transaction_then_confirms_drain():
    controller = PostgresWriteAdmission(limits=_limits(), lock_namespace=_new_namespace())
    with controller.transaction(DSN, work_class=DbWorkClass.HEAVY, timeout_seconds=1, identity="active-on-close") as conn:
        conn.execute("SELECT 1")
        assert controller.snapshot().active_by_class[DbWorkClass.HEAVY] == 1
        assert controller.close(timeout_seconds=0) is False
    assert controller.snapshot().active_count == 0
    assert controller.close(timeout_seconds=1) is True


def test_process_exit_releases_session_slot_and_rolls_back_business_transaction():
    # The worker deliberately exits without context cleanup; PostgreSQL must
    # release both its session advisory lock and open transaction on disconnect.
    ctx = get_context("spawn")
    namespace = _new_namespace()
    ready = ctx.Queue()
    release = ctx.Event()

    process = ctx.Process(target=_abrupt_exit_worker, args=(DSN, namespace, ready, release))
    process.start()
    try:
        assert ready.get(timeout=5) == "acquired"
        process.terminate()
        process.join(timeout=5)
        controller = PostgresWriteAdmission(limits=_limits(), lock_namespace=namespace)
        with controller.transaction(DSN, work_class=DbWorkClass.LIGHT, timeout_seconds=1, identity="after-crash") as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)
