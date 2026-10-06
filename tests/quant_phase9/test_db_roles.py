from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from quant_phase1.config import Settings
from quant_phase1.db import SchemaNotReadyError, apply_migrations, assert_schema_ready
from scripts.phase9_migrate import grant_runtime_privileges, main as migration_main


@pytest.fixture
def schema_db():
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for schema readiness tests")
    info = conninfo_to_dict(dsn)
    assert info.get("host") in {"127.0.0.1", "localhost", "::1"}
    assert info.get("dbname") == "quant_phase9_test"
    schema = f"phase9_roles_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    # A full-suite run may already have a migrated public schema. Keep the
    # readiness probe isolated so search_path cannot silently find its tables.
    conn = psycopg.connect(dsn, options=f"-c search_path={schema}")
    try:
        yield conn, schema
    finally:
        conn.close()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def test_absent_and_old_schema_fail_readiness_without_ddl(schema_db, tmp_path):
    conn, _ = schema_db
    with pytest.raises(SchemaNotReadyError, match="schema_migrations"):
        assert_schema_ready(conn, required_version="016_phase9_evidence_chain.sql")
    conn.rollback()
    migration_dir = Path(__file__).parents[2] / "migrations"
    pre016 = tmp_path / "pre016"
    pre016.mkdir()
    for path in migration_dir.glob("[0-9][0-9][0-9]_*.sql"):
        if path.name < "016_phase9_evidence_chain.sql":
            (pre016 / path.name).symlink_to(path)
    apply_migrations(conn, pre016)
    conn.commit()
    with pytest.raises(SchemaNotReadyError, match="016"):
        assert_schema_ready(conn, required_version="016_phase9_evidence_chain.sql")
    conn.rollback()
    apply_migrations(conn, migration_dir)
    conn.commit()
    assert_schema_ready(conn, required_version="016_phase9_evidence_chain.sql")
    conn.rollback()


def test_settings_repr_does_not_expose_runtime_dsn():
    secret = "postgresql://user:very-secret@localhost:5432/test"
    settings = Settings(
        trading_mode="paper", rest_base_url="https://example.com",
        ws_public_url="wss://example.com", postgres_dsn=secret,
        kline_retention_days={}, kline_ingestion_grace_seconds={},
    )
    assert "very-secret" not in repr(settings)


def test_runtime_role_cannot_create_or_mutate_immutable_audit(schema_db):
    conn, schema = schema_db
    apply_migrations(conn)
    conn.commit()
    role = f"phase9_app_test_{uuid4().hex}"
    conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
    conn.commit()
    grant_runtime_privileges(conn, schema_name=schema, runtime_role=role)
    conn.commit()
    try:
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("CREATE TABLE runtime_must_not_create (id bigint)")
        conn.rollback()
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE phase9_evidence_items SET semantic_code = 'tamper'")
        conn.rollback()
        conn.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE schema_migrations SET version = version")
        conn.rollback()
    finally:
        conn.execute("RESET ROLE")
        conn.commit()
        conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        conn.commit()

def test_migration_runner_never_echoes_dsn(monkeypatch):
    monkeypatch.setenv(
        "QUANT_MIGRATION_DSN",
        "postgresql://bad:supersecret@127.0.0.1:1/unused?connect_timeout=1",
    )
    with pytest.raises(SystemExit) as exc:
        migration_main()
    assert "supersecret" not in str(exc.value)
    assert "OperationalError" in str(exc.value)
