from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from quant_phase1.db import apply_migrations


ROOT = Path(__file__).parents[2]
MIGRATIONS = ROOT / "migrations"
MIGRATION = MIGRATIONS / "016_phase9_evidence_chain.sql"
TABLES = (
    "phase9_evaluations",
    "phase9_evaluation_snapshots",
    "phase9_evidence_items",
    "phase9_evidence_chains",
    "phase9_pattern_matches",
    "phase9_jev_reviews",
    "phase9_decision_candidates",
    "phase9_decision_status_events",
)
IMMUTABLE_TABLES = TABLES[1:]
INDEXES = (
    "outbox_events_phase9_identity_uq",
    "outbox_events_phase9_due_claim_idx",
    "outbox_events_phase9_lease_recovery_idx",
    "phase9_evaluations_stage1_latest_idx",
    "phase9_evaluations_symbol_latest_idx",
    "phase9_evaluations_state_lease_idx",
    "phase9_evaluation_snapshots_asof_idx",
    "phase9_evidence_items_type_idx",
    "phase9_evidence_chains_snapshot_idx",
    "phase9_pattern_matches_lookup_idx",
    "phase9_jev_reviews_request_uq",
    "phase9_decision_candidates_input_uq",
    "phase9_decision_candidates_symbol_latest_idx",
    "phase9_decision_status_events_decision_idx",
)


@pytest.fixture
def isolated_phase9_schema(tmp_path):
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for isolated PostgreSQL migration tests")

    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    from psycopg import sql

    info = conninfo_to_dict(dsn)
    assert info.get("host") in {"127.0.0.1", "localhost", "::1"}, "migration test DSN must use loopback"
    assert info.get("dbname") == "quant_phase9_test", "migration test requires disposable quant_phase9_test database"

    schema = f"phase9_migration_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    pre016 = tmp_path / "pre016"
    pre016.mkdir()
    for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
        if path.name < MIGRATION.name:
            (pre016 / path.name).symlink_to(path)
    try:
        yield dsn, schema, pre016
    finally:
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_migration_is_additive_and_creates_only_the_frozen_phase9_tables():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    created = tuple(
        line.split("create table if not exists", 1)[1].strip().split()[0]
        for line in sql.splitlines()
        if "create table if not exists" in line
    )
    assert created == TABLES
    for index in INDEXES:
        assert index in sql
    assert "alter table outbox_events" in sql
    assert "add column if not exists event_id text" in sql
    assert "references phase9_evaluations" in sql
    assert "jev_review_id uuid references phase9_jev_reviews(review_id) on delete restrict" in sql
    review_definition = sql.split("create table if not exists phase9_jev_reviews", 1)[1].split("create table if not exists", 1)[0]
    assert "references phase9_evidence_chains" not in review_definition
    assert "on delete cascade" not in sql
    assert "drop database" not in sql
    for forbidden in (
        "create table if not exists orders",
        "create table if not exists positions",
        "create table if not exists accounts",
        "truncate ",
    ):
        assert forbidden not in sql


def test_phase9_migration_applies_to_empty_schema_repeats_and_preserves_legacy_outbox(isolated_phase9_schema):
    import psycopg

    dsn, schema, pre016 = isolated_phase9_schema
    with psycopg.connect(dsn, autocommit=True, options=f"-c search_path={schema},public") as connection:
        assert apply_migrations(connection, pre016) == [path.name for path in sorted(pre016.glob("*.sql"))]
        legacy = connection.execute(
            """INSERT INTO outbox_events (event_type, aggregate_key, payload)
               VALUES ('legacy.test', 'legacy-1', '{}'::jsonb) RETURNING id"""
        ).fetchone()[0]

        added = apply_migrations(connection, MIGRATIONS)
        assert added == ["016_phase9_evidence_chain.sql", "017_execution_boundary.sql", "018_phase9_revalidation.sql", "019_strategy_v2.sql", "020_execution_intent_journal.sql", "021_bitget_full_market_spot_scope.sql"]
        assert connection.execute(
            "SELECT count(*) FROM phase9_evaluations"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM phase9_evaluation_snapshots"
        ).fetchone() == (0,)

        tables = connection.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema = current_schema() AND table_name = ANY(%s)
               ORDER BY table_name""",
            (list(TABLES),),
        ).fetchall()
        assert tuple(row[0] for row in tables) == tuple(sorted(TABLES))

        columns = {
            (table, column): (data_type, nullable)
            for table, column, data_type, nullable in connection.execute(
                """SELECT table_name, column_name, data_type, is_nullable
                   FROM information_schema.columns
                   WHERE table_schema = current_schema()
                     AND table_name IN ('outbox_events', 'phase9_evaluations', 'phase9_decision_candidates')"""
            ).fetchall()
        }
        assert columns[("outbox_events", "event_id")] == ("text", "YES")
        assert columns[("outbox_events", "phase9_state")] == ("text", "YES")
        assert columns[("outbox_events", "attempt_count")] == ("integer", "NO")
        assert columns[("outbox_events", "attempt_limit")] == ("integer", "YES")
        assert columns[("outbox_events", "next_attempt_at")] == ("timestamp with time zone", "NO")
        for column in ("lease_owner", "lease_expires_at", "acknowledged_at", "last_error_code", "terminal_reason"):
            assert columns[("outbox_events", column)][1] == "YES"
        assert columns[("phase9_decision_candidates", "jev_review_id")] == ("uuid", "YES")

        index_rows = connection.execute(
            """SELECT indexname FROM pg_indexes
               WHERE schemaname = current_schema() AND indexname = ANY(%s)
               ORDER BY indexname""",
            (list(INDEXES),),
        ).fetchall()
        assert tuple(row[0] for row in index_rows) == tuple(sorted(INDEXES))

        legacy_row = connection.execute(
            "SELECT event_id, phase9_state, attempt_count, attempt_limit, next_attempt_at FROM outbox_events WHERE id = %s",
            (legacy,),
        ).fetchone()
        assert legacy_row[:4] == (None, None, 0, None)
        assert legacy_row[4] is not None

        now = connection.execute("SELECT now()").fetchone()[0]
        event_id = "a" * 64
        insert_event = """INSERT INTO outbox_events
            (event_type, aggregate_key, payload, event_id, phase9_state,
             attempt_count, attempt_limit, next_attempt_at)
            VALUES ('phase9.stage1_candidate', '42:' || %s, '{}'::jsonb, %s,
                    'PENDING', 0, NULL, %s)"""
        pending_id = connection.execute(
            insert_event + " RETURNING id", (event_id, event_id, now)
        ).fetchone()[0]
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(insert_event, (event_id, event_id, now))
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                """INSERT INTO outbox_events
                   (event_type, aggregate_key, payload, event_id, phase9_state,
                    attempt_count, attempt_limit, next_attempt_at)
                   VALUES ('phase9.stage1_candidate', 'bad', '{}'::jsonb, %s,
                           'PENDING', 1, NULL, %s)""",
                ("b" * 64, now),
            )

        valid_states = (
            ("LEASED", 1, 3, "worker-a", now, None, None),
            ("RETRY_WAIT", 1, 3, None, None, None, None),
            ("ACKNOWLEDGED", 1, 3, None, None, now, None),
            ("DEAD_LETTER", 3, 3, None, None, None, "ATTEMPT_LIMIT_EXHAUSTED"),
        )
        for index, (state, attempts, limit, owner, expiry, acknowledged, terminal) in enumerate(valid_states, start=2):
            state_event_id = f"{index:064x}"
            connection.execute(
                """INSERT INTO outbox_events
                   (event_type, aggregate_key, payload, event_id, phase9_state,
                    attempt_count, attempt_limit, next_attempt_at, lease_owner,
                    lease_expires_at, acknowledged_at, terminal_reason)
                   VALUES ('phase9.stage1_candidate', %s, '{}'::jsonb, %s, %s,
                           %s, %s, %s, %s, %s, %s, %s)""",
                (f"state-{state}", state_event_id, state, attempts, limit, now,
                 owner, expiry, acknowledged, terminal),
            )

        connection.execute(
            """UPDATE outbox_events
               SET phase9_state = 'LEASED', attempt_count = 1, attempt_limit = 3,
                   lease_owner = 'worker-a', lease_expires_at = %s
               WHERE id = %s""",
            (now, pending_id),
        )
        assert connection.execute(
            "SELECT phase9_state, attempt_count, attempt_limit FROM outbox_events WHERE id = %s",
            (pending_id,),
        ).fetchone() == ("LEASED", 1, 3)

        before_repeat = connection.execute(
            """SELECT
                   (SELECT count(*) FROM information_schema.tables
                    WHERE table_schema = current_schema() AND table_name = ANY(%s)),
                   (SELECT count(*) FROM pg_indexes
                    WHERE schemaname = current_schema() AND indexname = ANY(%s)),
                   (SELECT count(*) FROM outbox_events)""",
            (list(TABLES), list(INDEXES)),
        ).fetchone()
        assert apply_migrations(connection, MIGRATIONS) == []
        after_repeat = connection.execute(
            """SELECT
                   (SELECT count(*) FROM information_schema.tables
                    WHERE table_schema = current_schema() AND table_name = ANY(%s)),
                   (SELECT count(*) FROM pg_indexes
                    WHERE schemaname = current_schema() AND indexname = ANY(%s)),
                   (SELECT count(*) FROM outbox_events)""",
            (list(TABLES), list(INDEXES)),
        ).fetchone()
        assert after_repeat == before_repeat


def test_phase9_audit_rows_reject_update_delete_but_operational_state_is_mutable(isolated_phase9_schema):
    import psycopg

    dsn, schema, _ = isolated_phase9_schema
    with psycopg.connect(dsn, autocommit=True, options=f"-c search_path={schema},public") as connection:
        assert "016_phase9_evidence_chain.sql" in apply_migrations(connection, MIGRATIONS)
        evaluation_id = uuid4()
        row = connection.execute(
            """INSERT INTO phase9_evaluations
               (evaluation_id, stage1_candidate_id, symbol, market, timeframe, evaluation_state,
                intake_disposition, policy_generation, material_change_generation)
               VALUES (%s, 1, 'BTCUSDT', 'USDT_PERPETUAL', '15m', 'QUEUED', 'ADMITTED', 'p1', '0')
               RETURNING evaluation_id""",
            (evaluation_id,),
        ).fetchone()[0]
        connection.execute(
            "UPDATE phase9_evaluations SET evaluation_state = 'RUNNING' WHERE evaluation_id = %s",
            (row,),
        )
        assert connection.execute(
            "SELECT evaluation_state FROM phase9_evaluations WHERE evaluation_id = %s", (row,)
        ).fetchone() == ("RUNNING",)

        connection.execute(
            """INSERT INTO phase9_evaluation_snapshots
               (evaluation_id, snapshot_digest, as_of, payload, code_version)
               VALUES (%s, %s, now(), '{}'::jsonb, %s) RETURNING evaluation_id""",
            (row, "c" * 64, "1" * 40),
        )
        chain_id = uuid4()
        review_id = uuid4()
        connection.execute(
            """INSERT INTO phase9_jev_reviews
               (review_id, evaluation_id, evidence_chain_id, status, reason_code,
                request_digest, prompt_version, payload)
               VALUES (%s, %s, %s, 'NOT_CONFIGURED', 'PROVIDER_LIFECYCLE_NOT_CONFIGURED',
                       %s, 'prompt-v1', '{}'::jsonb)""",
            (review_id, row, chain_id, "f" * 64),
        )
        connection.execute(
            """INSERT INTO phase9_evidence_chains
               (chain_id, evaluation_id, evaluation_snapshot_hash, input_snapshot_hash, payload)
               VALUES (%s, %s, %s, %s, '{}'::jsonb)""",
            (chain_id, row, "c" * 64, "d" * 64),
        )
        evidence_id = uuid4()
        connection.execute(
            """INSERT INTO phase9_evidence_items
               (evidence_id, evaluation_id, evidence_type, semantic_code, source_ref,
                availability_status, canonical_digest, payload)
               VALUES (%s, %s, 'TRADE_FLOW', 'FLOW_UNKNOWN', 'fixture:flow',
                       'NOT_AVAILABLE', %s, '{}'::jsonb)""",
            (evidence_id, row, "e" * 64),
        )
        pattern_id = uuid4()
        connection.execute(
            """INSERT INTO phase9_pattern_matches
               (pattern_match_id, evaluation_id, pattern_type, direction, status,
                pattern_policy_version, payload)
               VALUES (%s, %s, 'TREND_CONTINUATION', 'LONG', 'NOT_CONFIGURED', 'p1', '{}'::jsonb)""",
            (pattern_id, row),
        )
        decision_id = uuid4()
        connection.execute(
            """INSERT INTO phase9_decision_candidates
               (decision_id, evaluation_id, stage1_candidate_id, symbol, market, timeframe,
                valid_until, eligible, direction_bias, confidence_band, input_snapshot_hash,
                jev_review_id, payload)
               VALUES (%s, %s, 1, 'BTCUSDT', 'USDT_PERPETUAL', '15m', now(), false,
                       'NEUTRAL', 'INSUFFICIENT', %s, %s, '{}'::jsonb)""",
            (decision_id, row, "d" * 64, review_id),
        )
        status_event_id = uuid4()
        connection.execute(
            """INSERT INTO phase9_decision_status_events
               (event_id, decision_id, evaluation_id, status, event_time, reason_code, payload)
               VALUES (%s, %s, %s, 'ACTIVE', now(), 'INITIAL', '{}'::jsonb)""",
            (status_event_id, decision_id, row),
        )

        immutable_rows = (
            ("phase9_evaluation_snapshots", "evaluation_id", row, "snapshot_digest = snapshot_digest"),
            ("phase9_evidence_items", "evidence_id", evidence_id, "semantic_code = semantic_code"),
            ("phase9_evidence_chains", "chain_id", chain_id, "input_snapshot_hash = input_snapshot_hash"),
            ("phase9_pattern_matches", "pattern_match_id", pattern_id, "status = status"),
            ("phase9_jev_reviews", "review_id", review_id, "reason_code = reason_code"),
            ("phase9_decision_candidates", "decision_id", decision_id, "eligible = eligible"),
            ("phase9_decision_status_events", "event_id", status_event_id, "status = status"),
        )
        for table, key, identity, no_op_update in immutable_rows:
            with pytest.raises(psycopg.Error) as update_error:
                connection.execute(f"UPDATE {table} SET {no_op_update} WHERE {key} = %s", (identity,))
            assert update_error.value.sqlstate == "55000"
            with pytest.raises(psycopg.Error) as delete_error:
                connection.execute(f"DELETE FROM {table} WHERE {key} = %s", (identity,))
            assert delete_error.value.sqlstate == "55000"


def test_phase9_unique_evaluation_identity_and_snapshot_singleton_contract(isolated_phase9_schema):
    import psycopg

    dsn, schema, _ = isolated_phase9_schema
    with psycopg.connect(dsn, autocommit=True, options=f"-c search_path={schema},public") as connection:
        apply_migrations(connection, MIGRATIONS)
        evaluation_id = "00000000-0000-0000-0000-000000000001"
        connection.execute(
            """INSERT INTO phase9_evaluations
               (evaluation_id, stage1_candidate_id, symbol, market, timeframe,
                evaluation_state, intake_disposition, policy_generation, material_change_generation)
               VALUES (%s, 1, 'BTCUSDT', 'USDT_PERPETUAL', '15m', 'QUEUED', 'ADMITTED', 'p1', '0')""",
            (evaluation_id,),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                """INSERT INTO phase9_evaluations
                   (evaluation_id, stage1_candidate_id, symbol, market, timeframe,
                    evaluation_state, intake_disposition, policy_generation, material_change_generation)
                   VALUES ('00000000-0000-0000-0000-000000000002', 1, 'BTCUSDT', 'USDT_PERPETUAL',
                           '15m', 'QUEUED', 'ADMITTED', 'p1', '0')"""
            )
        connection.execute(
            """INSERT INTO phase9_evaluation_snapshots
               (evaluation_id, snapshot_digest, as_of, payload, code_version)
               VALUES (%s, %s, now(), '{}'::jsonb, %s)""",
            (evaluation_id, "a" * 64, "1" * 40),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            connection.execute(
                """INSERT INTO phase9_evaluation_snapshots
                   (evaluation_id, snapshot_digest, as_of, payload, code_version)
                   VALUES (%s, %s, now(), '{}'::jsonb, %s)""",
                (evaluation_id, "b" * 64, "1" * 40),
            )
