from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from quant_phase9.config import Phase9RuntimeConfig, load_phase9_runtime_config
from quant_phase9.runtime import Phase9EngineRuntime


@pytest.mark.parametrize(
    ("field", "too_high"),
    (
        ("max_inflight_evaluations", 3),
        ("max_queued_ids", 33),
        ("max_queued_bytes", 8 * 1024 * 1024 + 1),
        ("evaluation_timeout_seconds", 31),
        ("max_concurrent_jev_calls", 2),
        ("max_revalidations_per_minute", 17),
    ),
)
def test_runtime_policy_ceiling_cannot_be_raised(field, too_high):
    with pytest.raises(ValueError):
        replace(Phase9RuntimeConfig(), **{field: too_high})


def test_runtime_disabled_by_default_and_env_rejects_invalid_values():
    config = load_phase9_runtime_config({})
    assert config.enabled is False
    assert config.max_inflight_evaluations == 2
    assert config.max_queued_ids == 32
    assert config.max_queued_bytes == 8 * 1024 * 1024
    assert config.evaluation_timeout_seconds == 30
    assert config.max_concurrent_jev_calls == 1
    assert config.max_revalidations_per_minute == 16
    for value in ("true", "2", "yes"):
        with pytest.raises(ValueError):
            load_phase9_runtime_config({"PHASE9_ENABLED": value})


@pytest.mark.asyncio
async def test_disabled_runtime_does_not_open_database():
    runtime = Phase9EngineRuntime(
        config=Phase9RuntimeConfig(),
        dsn="unused",
        connection_factory=lambda *_args, **_kwargs: pytest.fail("disabled runtime opened DB"),
    )
    assert runtime.health()["state"] == "NOT_CONFIGURED"
    await runtime.run(asyncio.Event())
    assert runtime.health()["state"] == "NOT_CONFIGURED"


def test_enabled_runtime_requires_approved_policy():
    with pytest.raises((ValueError, TypeError)):
        Phase9EngineRuntime(
            config=replace(Phase9RuntimeConfig(), enabled=True),
            dsn="unused",
            connection_factory=lambda *_args, **_kwargs: None,
        )


def test_v2_missing_stored_result_projection_falls_back_to_research(monkeypatch):
    from contextlib import contextmanager
    from types import SimpleNamespace

    import quant_phase9.runtime as runtime_module
    from quant_phase9.runtime import Phase9StrategyV2Evaluator

    class ResearchCalled(Exception):
        pass

    class Result:
        def fetchone(self):
            return ("stored-snapshot",)

    class Connection:
        def execute(self, *_args, **_kwargs):
            return Result()

    class Admission:
        @contextmanager
        def transaction(self, *_args, **_kwargs):
            yield Connection()

    monkeypatch.setattr(runtime_module, "assert_schema_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_module, "_load_snapshot", lambda _payload: SimpleNamespace(source_projections=()))
    monkeypatch.setattr(runtime_module, "evaluation_id_for", lambda _identity: "evaluation")

    event = SimpleNamespace(event_id="event")
    research_calls = []

    def research(candidate, *, deadline):
        research_calls.append(candidate)
        raise ResearchCalled

    evaluator = object.__new__(Phase9StrategyV2Evaluator)
    evaluator.dsn = "unused"
    evaluator.db_admission = Admission()
    evaluator.input_factory = research
    evaluator.evaluation_timeout_seconds = 30

    with pytest.raises(ResearchCalled):
        evaluator(event, (object(),), "v2-test")
    assert research_calls == [event]


def test_v2_supervisor_drains_old_b_without_research_and_still_dispatches_a(runtime_database, tmp_path):
    from datetime import datetime, timedelta, timezone
    from quant_data_layer.db_admission import PostgresWriteAdmission
    from quant_phase1.contracts import DataStatus
    from quant_phase1.stage1 import Stage1Result
    from quant_phase9.intake import Phase9OutboxWriter, build_stage1_candidate_event
    from quant_phase9.approval import create_approval_artifact
    from quant_phase9.policy import load_approved_policy_manifest
    from quant_phase9.runtime import Phase9StrategyV2Evaluator
    from strategies.execution.execution_policy import ExecutionPolicyV2
    from strategies.integration.policy_manifest import build_v2_manifest

    dsn, factory = runtime_database
    now = datetime.now(timezone.utc) - timedelta(seconds=1)
    manifest_path, approval_path = tmp_path / "v2.json", tmp_path / "approval.json"
    manifest_path.write_text(build_v2_manifest(ExecutionPolicyV2(), created_at=now).model_dump_json(by_alias=True))
    create_approval_artifact(manifest_path=manifest_path, output_path=approval_path,
        approved_by="explicit unit-test fixture, not runtime approval", approved_commit="b" * 40)
    approved = load_approved_policy_manifest(manifest_path, approval_path, expected_commit="b" * 40)
    events = []
    for row_id, category in ((91, "B"), (92, "A")):
        stage = Stage1Result("BTCUSDT", category, "fixture", DataStatus.AVAILABLE,
            (), {}, "BULLISH", ("FIXTURE",), timestamp=now, strategy_version="QUANT_PAPER_V2")
        events.append(build_stage1_candidate_event(screening_result_id=row_id, run_id=32,
            screening_result=stage, market="USDT_PERPETUAL",
            instrument_scope={"category":"USDT-FUTURES", "quote_coin":"USDT", "contract_type":"perpetual", "status":"online", "in_scope":"true"},
            candidate_created_at=now, candidate_valid_until=now + timedelta(minutes=10),
            stage1_policy_version="QUANT_PAPER_V2", source_as_of=now))
    with factory(dsn) as conn:
        for event in events:
            Phase9OutboxWriter().emit(conn, event=event)
    calls = []
    def research(event, *, deadline):
        calls.append(event.event_id)
        raise RuntimeError("A_RESEARCH_PATH_REACHED_IN_TEST")
    db_admission = PostgresWriteAdmission(connection_factory=factory)
    evaluator = Phase9StrategyV2Evaluator(dsn=dsn, policy_manifest=approved,
        input_factory=research, connection_factory=factory, db_admission=db_admission)
    runtime = Phase9EngineRuntime(config=replace(Phase9RuntimeConfig(), enabled=True),
        dsn=dsn, policy_manifest=approved, evaluator=evaluator,
        connection_factory=factory, db_admission=db_admission)
    try:
        with pytest.raises(RuntimeError, match="A_RESEARCH_PATH_REACHED_IN_TEST"):
            asyncio.run(runtime.poll_once())
        assert calls == [events[1].event_id]
        assert runtime.health()["observations_skipped"] == 1
        with factory(dsn) as conn:
            assert conn.execute("SELECT phase9_state FROM outbox_events WHERE event_id=%s", (events[0].event_id,)).fetchone() == ("ACKNOWLEDGED",)
            assert conn.execute("SELECT count(*) FROM phase9_evaluations WHERE stage1_candidate_id=%s", (events[0].stage1_candidate_id,)).fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM phase9_decision_candidates").fetchone()[0] == 0
    finally:
        runtime.db_admission.close()


@pytest.fixture
def runtime_database():
    import os
    from uuid import uuid4

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    from quant_phase1.db import apply_migrations

    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is required")
    info = conninfo_to_dict(dsn)
    assert info.get("host") in {"127.0.0.1", "localhost", "::1"}
    assert info.get("dbname") == "quant_phase9_test"
    schema = f"phase9_runtime_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    def factory(value, **kwargs):
        return psycopg.connect(value, options=f"-c search_path={schema},public", **kwargs)
    try:
        with factory(dsn) as conn:
            apply_migrations(conn)
        yield dsn, factory
    finally:
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_runtime_restarts_from_durable_event_and_persists_ineligible_decisions(
    runtime_database, tmp_path,
):
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    from quant_data_layer.db_admission import PostgresWriteAdmission
    from quant_phase1.contracts import DataStatus
    from quant_phase1.stage1 import Stage1Result
    from quant_phase9.intake import Phase9OutboxWriter, build_stage1_candidate_event
    from quant_phase9.policy import load_approved_policy_manifest
    from quant_phase9.runtime import Phase9DeterministicEvaluator
    from tests.quant_phase9.test_policy import _files

    dsn, factory = runtime_database
    manifest_path, approval_path = _files(tmp_path)
    approved = load_approved_policy_manifest(
        manifest_path, approval_path, expected_commit="b" * 40,
    )
    now = datetime.now(timezone.utc) - timedelta(seconds=2)
    result = Stage1Result(
        symbol="BTCUSDT", category="A", reason="fixture", status=DataStatus.AVAILABLE,
        inputs_used=("price",), indicators={"atr": Decimal("1.25")},
        structure="BULLISH", reason_codes=("STRUCTURE_ALIGNED",), timestamp=now,
    )
    event = build_stage1_candidate_event(
        screening_result_id=31, run_id=32, screening_result=result,
        market="USDT_PERPETUAL",
        instrument_scope={
            "category": "USDT-FUTURES", "quote_coin": "USDT",
            "contract_type": "perpetual", "status": "online", "in_scope": "true",
        },
        candidate_created_at=now,
        candidate_valid_until=now + timedelta(hours=1),
        stage1_policy_version="phase1-basic-v1", source_as_of=now,
    )
    with factory(dsn) as conn:
        conn.execute(
            """INSERT INTO symbols
               (symbol, category, base_coin, quote_coin, symbol_type,
                contract_type, status, price_precision, quantity_precision,
                min_order_qty, source, exchange, fetched_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            ("BTCUSDT", "USDT-FUTURES", "BTC", "USDT", "PERPETUAL",
             "perpetual", "online", 2, 3, Decimal("0.001"),
             "fixture", "bitget", now),
        )
        Phase9OutboxWriter().emit(conn, event=event)

    db_admission = PostgresWriteAdmission(connection_factory=factory)
    evaluator = Phase9DeterministicEvaluator(
        dsn=dsn, policy_manifest=approved,
        connection_factory=factory, db_admission=db_admission,
    )
    runtime = Phase9EngineRuntime(
        config=replace(Phase9RuntimeConfig(), enabled=True),
        dsn=dsn, policy_manifest=approved, evaluator=evaluator,
        connection_factory=factory, db_admission=db_admission,
    )
    claimed = runtime._claim_and_admit()
    assert len(claimed) == 1
    with factory(dsn) as conn:
        conn.execute("UPDATE symbols SET status='offline' WHERE symbol='BTCUSDT'")
    with pytest.raises(ValueError, match="eligible USDT perpetual"):
        evaluator._snapshot(event, claimed[0][1][0])
    with factory(dsn) as conn:
        conn.execute("UPDATE symbols SET status='online' WHERE symbol='BTCUSDT'")
    evaluator._snapshot(event, claimed[0][1][0])
    with factory(dsn) as conn:
        conn.execute(
            "UPDATE outbox_events SET lease_expires_at=%s WHERE event_id=%s",
            (datetime.now(timezone.utc) - timedelta(seconds=1), event.event_id),
        )
    restarted = Phase9EngineRuntime(
        config=replace(Phase9RuntimeConfig(), enabled=True),
        dsn=dsn, policy_manifest=approved, evaluator=evaluator,
        connection_factory=factory, db_admission=db_admission,
    )
    assert asyncio.run(restarted.poll_once()) == 1
    with factory(dsn) as conn:

        assert conn.execute(
            "SELECT phase9_state FROM outbox_events WHERE event_id=%s", (event.event_id,)
        ).fetchone() == ("ACKNOWLEDGED",)
        assert conn.execute(
            "SELECT count(*) FROM phase9_evaluations WHERE evaluation_state='COMPLETED'"
        ).fetchone()[0] == 3
        assert conn.execute(
            "SELECT count(*) FROM phase9_decision_candidates WHERE eligible=false"
        ).fetchone()[0] == 3
    assert asyncio.run(restarted.poll_once()) == 0

    expired_event = build_stage1_candidate_event(
        screening_result_id=33, run_id=34, screening_result=result,
        market="USDT_PERPETUAL",
        instrument_scope={
            "category": "USDT-FUTURES", "quote_coin": "USDT",
            "contract_type": "perpetual", "status": "online", "in_scope": "true",
        },
        candidate_created_at=now, candidate_valid_until=now,
        stage1_policy_version="phase1-basic-v1", source_as_of=now,
    )
    with factory(dsn) as conn:
        Phase9OutboxWriter().emit(conn, event=expired_event)
    assert asyncio.run(restarted.poll_once()) == 0
    with factory(dsn) as conn:
        assert conn.execute(
            "SELECT phase9_state FROM outbox_events WHERE event_id=%s",
            (expired_event.event_id,),
        ).fetchone() == ("ACKNOWLEDGED",)
        assert conn.execute(
            """SELECT count(*) FROM phase9_evaluations
               WHERE stage1_candidate_id=%s AND evaluation_state='CANCELLED'
                 AND intake_disposition='EXPIRED' AND reason_code='STAGE1_EXPIRED'""",
            (expired_event.stage1_candidate_id,),
        ).fetchone()[0] == 3


@pytest.mark.asyncio
async def test_engine_owned_supervisor_stops_without_closing_shared_admission(
    runtime_database, tmp_path,
):
    from quant_data_layer.admission import WorkAdmissionController
    from quant_data_layer.observability import ProcessRole
    from quant_phase9.policy import load_approved_policy_manifest
    from tests.quant_phase9.test_policy import _files

    dsn, factory = runtime_database
    manifest_path, approval_path = _files(tmp_path)
    approved = load_approved_policy_manifest(
        manifest_path, approval_path, expected_commit="b" * 40,
    )
    admission = WorkAdmissionController(role=ProcessRole.ENGINE)
    runtime = Phase9EngineRuntime(
        config=replace(Phase9RuntimeConfig(), enabled=True),
        dsn=dsn, policy_manifest=approved,
        evaluator=lambda *_args: None,
        connection_factory=factory, admission_controller=admission,
        poll_seconds=0.01,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(runtime.run(stop))
    await asyncio.sleep(0.05)
    stop.set()
    await task
    assert runtime.health()["state"] == "STOPPED"
    assert admission.snapshot().accepting is True
    await admission.shutdown(timeout_seconds=1)


def test_v2_evaluator_checks_shared_event_lease_once_before_three_horizons(monkeypatch):
    from contextlib import contextmanager
    from datetime import datetime, timezone
    from types import SimpleNamespace

    import quant_phase9.runtime as runtime_module
    import quant_phase9.revalidation as revalidation
    import strategies.integration.phase9_bridge as phase9_bridge
    import strategies.persistence as strategy_persistence
    from quant_phase9.runtime import Phase9StrategyV2Evaluator
    from strategies.integration.phase9_bridge import StrategyExecutionInputs
    from tests.strategies.test_execution_policy import setup

    theses, view, policy = setup()
    inputs = StrategyExecutionInputs(
        analysis=view.analysis_snapshots[0], theses=theses, view=view, policy=policy,
    )
    manifest = SimpleNamespace(manifest=SimpleNamespace(
        manifest_version="2.0.0",
        manifest_digest="a" * 64,
        policy_content=SimpleNamespace(
            enabled_patterns=(SimpleNamespace(strategy_policy_digest=policy.digest),),
        ),
    ))
    event = SimpleNamespace(event_id="shared-event")
    identities = tuple(
        SimpleNamespace(timeframe=timeframe, material_change_generation="0")
        for timeframe in ("15m", "1H", "4H")
    )

    class QueryResult:
        def fetchone(self):
            return None

    class Connection:
        def execute(self, *_args, **_kwargs):
            return QueryResult()

    class Admission:
        @contextmanager
        def transaction(self, *_args, **_kwargs):
            yield Connection()

    class SnapshotBuilder:
        def __init__(self, **_kwargs):
            pass

        def _snapshot(self, _event, identity, *, deadline):
            return SimpleNamespace(
                identity=identity,
                evaluation_id=f"evaluation-{identity.timeframe}",
            )

    lease_checks = []
    persisted = []
    registered = []

    def check_lease(_conn, *, event_id, evaluation_ids, consumer_name, now):
        lease_checks.append((event_id, tuple(evaluation_ids), consumer_name))
        return "shared-owner"

    def persist(_conn, *, result, snapshot, now, validated_event_lease_owner):
        persisted.append((snapshot.evaluation_id, validated_event_lease_owner))
        return SimpleNamespace(decision_id=f"decision-{snapshot.identity.timeframe}")

    monkeypatch.setattr(runtime_module, "assert_schema_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_module, "evaluation_id_for", lambda identity: identity.timeframe)
    monkeypatch.setattr(runtime_module, "Phase9DeterministicEvaluator", SnapshotBuilder)
    monkeypatch.setattr(strategy_persistence, "assert_event_lease_held", check_lease)
    monkeypatch.setattr(strategy_persistence, "persist_strategy_decision", persist)
    monkeypatch.setattr(
        phase9_bridge, "build_v2_candidate",
        lambda snapshot, _result, _inputs: (
            SimpleNamespace(decision_id=f"decision-{snapshot.identity.timeframe}"), [], [], [], None,
        ),
    )
    monkeypatch.setattr(
        revalidation, "register",
        lambda _conn, *, identity, **_kwargs: registered.append(identity.timeframe),
    )

    evaluator = object.__new__(Phase9StrategyV2Evaluator)
    evaluator.dsn = "unused"
    evaluator.policy_manifest = manifest
    evaluator.input_factory = lambda _event, *, deadline: inputs
    evaluator.connection_factory = lambda *_args, **_kwargs: Connection()
    evaluator.db_admission = Admission()
    evaluator.evaluation_timeout_seconds = 30

    evaluator(event, identities, "v2-test")

    expected_ids = tuple(f"evaluation-{identity.timeframe}" for identity in identities)
    assert lease_checks == [("shared-event", expected_ids, "v2-test")]
    assert persisted == [(evaluation_id, "shared-owner") for evaluation_id in expected_ids]
    assert registered == ["15m", "1H", "4H"]
