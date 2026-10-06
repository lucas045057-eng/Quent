from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from quant_phase1.db import apply_migrations


ROOT = Path(__file__).parents[1]
MIGRATIONS = ROOT / "migrations"
PHASE8_MIGRATION = MIGRATIONS / "015_phase8_options_context.sql"
PHASE8_TABLES = (
    "phase8_option_instruments",
    "phase8_option_instrument_events",
    "phase8_option_market_snapshots",
    "phase8_option_context_snapshots",
)
PHASE8_INDEXES = (
    "phase8_option_instruments_lookup_idx",
    "phase8_option_instrument_events_retention_idx",
    "phase8_option_instrument_events_symbol_idx",
    "phase8_option_market_snapshots_summary_retention_idx",
    "phase8_option_market_snapshots_markprice_retention_idx",
    "phase8_option_market_snapshots_ticker_retention_idx",
    "phase8_option_market_snapshots_latest_idx",
    "phase8_option_market_snapshots_dedup_uq",
    "phase8_option_context_snapshots_retention_idx",
    "phase8_option_context_snapshots_lookup_idx",
)


@pytest.fixture
def isolated_phase8_schema():
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is not configured for isolated PostgreSQL migration tests")

    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    from psycopg import sql

    info = conninfo_to_dict(dsn)
    assert info.get("host") in {"127.0.0.1", "localhost", "::1"}, "migration test DSN must use loopback"
    assert info.get("dbname") == "quant_phase8_test", "migration test requires the disposable quant_phase8_test database"

    schema = f"phase8_migration_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        yield dsn, schema
    finally:
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_phase8_migration_defines_additive_option_storage_contract():
    sql = PHASE8_MIGRATION.read_text(encoding="utf-8").lower()
    for table in PHASE8_TABLES:
        assert f"create table if not exists {table}" in sql
    for index in PHASE8_INDEXES:
        if index.endswith("_uq"):
            assert f"constraint {index}" in sql
        else:
            assert f"create index if not exists {index}" in sql
    for field in (
        "exchange_timestamp",
        "fetched_at",
        "received_at",
        "processed_at",
        "field_last_updated_at",
        "unit_status",
        "timestamp_semantics",
        "provenance",
        "raw_reference",
        "payload_hash",
        "context_timestamp",
        "calculation_version",
    ):
        assert field in sql
    assert "unique nulls not distinct" in sql
    assert "references phase8_option_instruments" in sql
    assert "timestamptz" in sql
    for status in ("available", "stale", "not_available", "partial", "error"):
        assert status in sql
    for forbidden in ("drop ", "truncate", "delete from", "alter table", "create table if not exists orders", "create table if not exists positions"):
        assert forbidden not in sql


def test_phase8_migration_only_adds_the_approved_four_tables():
    sql = PHASE8_MIGRATION.read_text(encoding="utf-8").lower()
    created_tables = [
        line.split("create table if not exists", 1)[1].strip().split()[0]
        for line in sql.splitlines()
        if "create table if not exists" in line
    ]
    assert tuple(created_tables) == PHASE8_TABLES


def test_phase8_migration_applies_and_repeats_without_schema_changes(isolated_phase8_schema):
    import psycopg
    dsn, schema = isolated_phase8_schema

    with psycopg.connect(dsn, autocommit=True, options=f"-c search_path={schema},public") as connection:
        first_run = apply_migrations(connection, MIGRATIONS)
        assert "015_phase8_options_context.sql" in first_run

        table_rows = connection.execute(
            """SELECT table_name FROM information_schema.tables
               WHERE table_schema = current_schema() AND table_name = ANY(%s)
               ORDER BY table_name""",
            (list(PHASE8_TABLES),),
        ).fetchall()
        assert tuple(row[0] for row in table_rows) == tuple(sorted(PHASE8_TABLES))

        index_rows = connection.execute(
            """SELECT indexname FROM pg_indexes
               WHERE schemaname = current_schema() AND indexname = ANY(%s)
               ORDER BY indexname""",
            (list(PHASE8_INDEXES),),
        ).fetchall()
        assert tuple(row[0] for row in index_rows) == tuple(sorted(PHASE8_INDEXES))

        column_rows = connection.execute(
            """SELECT table_name, column_name, data_type, is_nullable
               FROM information_schema.columns
               WHERE table_schema = current_schema() AND table_name = ANY(%s)""",
            (list(PHASE8_TABLES),),
        ).fetchall()
        columns = {
            (table, column): (data_type, nullable)
            for table, column, data_type, nullable in column_rows
        }
        expected_columns = {
            ("phase8_option_instruments", "exchange_timestamp"): ("timestamp with time zone", "YES"),
            ("phase8_option_instruments", "fetched_at"): ("timestamp with time zone", "NO"),
            ("phase8_option_instruments", "processed_at"): ("timestamp with time zone", "NO"),
            ("phase8_option_instrument_events", "exchange_timestamp"): ("timestamp with time zone", "YES"),
            ("phase8_option_instrument_events", "received_at"): ("timestamp with time zone", "NO"),
            ("phase8_option_instrument_events", "processed_at"): ("timestamp with time zone", "NO"),
            ("phase8_option_market_snapshots", "exchange_timestamp"): ("timestamp with time zone", "YES"),
            ("phase8_option_market_snapshots", "fetched_at"): ("timestamp with time zone", "YES"),
            ("phase8_option_market_snapshots", "received_at"): ("timestamp with time zone", "YES"),
            ("phase8_option_market_snapshots", "processed_at"): ("timestamp with time zone", "NO"),
            ("phase8_option_market_snapshots", "field_metadata"): ("jsonb", "NO"),
            ("phase8_option_context_snapshots", "context_timestamp"): ("timestamp with time zone", "NO"),
            ("phase8_option_context_snapshots", "processed_at"): ("timestamp with time zone", "NO"),
            ("phase8_option_context_snapshots", "source_timestamps"): ("jsonb", "NO"),
            ("phase8_option_context_snapshots", "source_capture_times"): ("jsonb", "NO"),
        }
        for key, shape in expected_columns.items():
            assert columns.get(key) == shape

        constraint_rows = connection.execute(
            """SELECT c.conrelid::regclass::text, c.conname, c.contype, c.convalidated,
                      pg_get_constraintdef(c.oid)
               FROM pg_constraint c
               WHERE c.conrelid::regclass::text = ANY(%s)""",
            (list(PHASE8_TABLES),),
        ).fetchall()
        constraints = {(table, name): (kind, validated, definition.lower()) for table, name, kind, validated, definition in constraint_rows}
        for table in PHASE8_TABLES:
            assert any(key[0] == table and kind == "c" and validated for key, (kind, validated, _) in constraints.items())
            status_constraint = (table, f"{table}_status_check")
            assert constraints[status_constraint][0:2] == ("c", True)
            assert "available" in constraints[status_constraint][2]
        assert any(kind == "f" and "references phase8_option_instruments" in definition
                   for (table, _), (kind, _, definition) in constraints.items()
                   if table == "phase8_option_instrument_events")
        assert any(kind == "f" and "references phase8_option_instruments" in definition
                   for (table, _), (kind, _, definition) in constraints.items()
                   if table == "phase8_option_market_snapshots")
        dedup_nulls_not_distinct = connection.execute(
            """SELECT i.indnullsnotdistinct
               FROM pg_index i JOIN pg_class idx ON idx.oid = i.indexrelid
               WHERE idx.relname = 'phase8_option_market_snapshots_dedup_uq'"""
        ).fetchone()
        assert dedup_nulls_not_distinct == (True,)

        fetched_at = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
        processed_at = datetime(2026, 9, 26, 12, 0, 1, tzinfo=timezone.utc)
        insert_snapshot = """INSERT INTO phase8_option_market_snapshots (
            exchange, source, symbol, underlying, observation_kind, exchange_timestamp,
            timestamp_semantics, fetched_at, received_at, processed_at, status, schema_version,
            metrics, field_metadata, payload_hash
        ) VALUES (
            'DERIBIT', 'deribit', 'BTC-26SEP26-100000-C', 'BTC', 'REST_CHAIN_SUMMARY',
            NULL, 'NOT_PROVIDED', %s, NULL, %s, 'AVAILABLE', 'phase8.v1',
            '{"open_interest": 1}'::jsonb, '{}'::jsonb,
            'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'
        ) ON CONFLICT ON CONSTRAINT phase8_option_market_snapshots_dedup_uq DO NOTHING"""
        assert connection.execute(insert_snapshot, (fetched_at, processed_at)).rowcount == 1
        assert connection.execute(insert_snapshot, (fetched_at, processed_at)).rowcount == 0
        stored_times = connection.execute(
            """SELECT exchange_timestamp, fetched_at, received_at, processed_at
               FROM phase8_option_market_snapshots
               WHERE symbol = 'BTC-26SEP26-100000-C'"""
        ).fetchone()
        assert stored_times == (None, fetched_at, None, processed_at)

        migration_count = connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version = '015_phase8_options_context.sql'"
        ).fetchone()[0]
        assert migration_count == 1

        schema_counts_before_repeat = connection.execute(
            """SELECT
                   (SELECT count(*) FROM information_schema.tables
                    WHERE table_schema = current_schema() AND table_name = ANY(%s)),
                   (SELECT count(*) FROM pg_indexes
                    WHERE schemaname = current_schema() AND indexname = ANY(%s)),
                   (SELECT count(*) FROM pg_constraint
                    WHERE conrelid::regclass::text = ANY(%s)),
                   (SELECT count(*) FROM phase8_option_market_snapshots)""",
            (list(PHASE8_TABLES), list(PHASE8_INDEXES), list(PHASE8_TABLES)),
        ).fetchone()

        second_run = apply_migrations(connection, MIGRATIONS)
        assert second_run == []
        assert connection.execute(
            "SELECT count(*) FROM schema_migrations WHERE version = '015_phase8_options_context.sql'"
        ).fetchone()[0] == 1
        schema_counts_after_repeat = connection.execute(
            """SELECT
                   (SELECT count(*) FROM information_schema.tables
                    WHERE table_schema = current_schema() AND table_name = ANY(%s)),
                   (SELECT count(*) FROM pg_indexes
                    WHERE schemaname = current_schema() AND indexname = ANY(%s)),
                   (SELECT count(*) FROM pg_constraint
                    WHERE conrelid::regclass::text = ANY(%s)),
                   (SELECT count(*) FROM phase8_option_market_snapshots)""",
            (list(PHASE8_TABLES), list(PHASE8_INDEXES), list(PHASE8_TABLES)),
        ).fetchone()
        assert schema_counts_after_repeat == schema_counts_before_repeat


def test_phase8_migration_runner_validates_schema_before_recording_and_on_repeat(monkeypatch, tmp_path):
    import quant_phase1.db as db

    migration = tmp_path / "015_phase8_options_context.sql"
    migration.write_text("CREATE TABLE phase8_test (id INTEGER);", encoding="utf-8")

    class Connection:
        def __init__(self):
            self.versions = {f"{number:03d}_existing.sql" for number in range(1, 15)}
            self.versions.add("009_phase4_metrics.repair.v1")
            self.events = []

        def execute(self, sql, params=None):
            if str(sql).startswith("SELECT version FROM schema_migrations"):
                return [(version,) for version in sorted(self.versions)]
            if str(sql).startswith("INSERT INTO schema_migrations") and params:
                self.versions.add(params[0])
                self.events.append("record")
            return []

        class _Transaction:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def transaction(self):
            return self._Transaction()

    validation_calls: list[str] = []
    def validate(_connection):
        validation_calls.append("validated")

    monkeypatch.setattr(db, "_validate_phase8_schema", validate)
    connection = Connection()

    assert db.apply_migrations(connection, tmp_path) == [migration.name]
    assert migration.name in connection.versions
    assert db.apply_migrations(connection, tmp_path) == []
    assert validation_calls == ["validated", "validated"]
    assert connection.events == ["record"]

    rejected_connection = Connection()
    monkeypatch.setattr(
        db,
        "_validate_phase8_schema",
        lambda _connection: (_ for _ in ()).throw(RuntimeError("phase8 schema mismatch")),
    )
    with pytest.raises(RuntimeError, match="phase8 schema mismatch"):
        db.apply_migrations(rejected_connection, tmp_path)
    assert migration.name not in rejected_connection.versions
    assert rejected_connection.events == []
