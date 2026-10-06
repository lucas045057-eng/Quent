"""Explicit database migration entrypoint; never imported by runtime services."""

from __future__ import annotations

import os
import re
from pathlib import Path

import psycopg
from psycopg import Connection, sql

from quant_phase1.db import apply_migrations, assert_schema_ready


ROOT = Path(__file__).resolve().parents[1]
MIGRATION_VERSION = "016_phase9_evidence_chain.sql"
_RUNTIME_ROLE = "quant_app"
_MIGRATION_ROLE = "quant_migration"
_IMMUTABLE_AUDIT = frozenset({
    "phase9_evaluation_snapshots",
    "phase9_evidence_items",
    "phase9_evidence_chains",
    "phase9_pattern_matches",
    "phase9_jev_reviews",
    "phase9_decision_candidates",
    "phase9_decision_status_events",
})


def grant_runtime_privileges(
    conn: Connection, *, schema_name: str = "public", runtime_role: str = _RUNTIME_ROLE,
) -> None:
    """Grant DML only; immutable Phase 9 audit tables never receive UPDATE/DELETE."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", runtime_role):
        raise ValueError("runtime role name is invalid")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema_name):
        raise ValueError("schema name is invalid")
    role = conn.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (runtime_role,)
    ).fetchone()
    if role is None:
        raise ValueError("runtime database role is absent")
    conn.execute(
        sql.SQL("REVOKE CREATE ON SCHEMA {} FROM {}").format(
            sql.Identifier(schema_name), sql.Identifier(runtime_role),
        )
    )
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(schema_name), sql.Identifier(runtime_role),
        )
    )
    tables = conn.execute(
        """SELECT tablename FROM pg_tables WHERE schemaname = %s ORDER BY tablename""",
        (schema_name,),
    ).fetchall()
    for (table_name,) in tables:
        privileges = (
            "SELECT" if table_name == "schema_migrations"
            else "SELECT, INSERT" if table_name in _IMMUTABLE_AUDIT
            else "SELECT, INSERT, UPDATE" if table_name == "phase9_evaluations"
            else "SELECT, INSERT, UPDATE, DELETE"
        )
        conn.execute(
            sql.SQL("GRANT " + privileges + " ON TABLE {}.{} TO {}").format(
                sql.Identifier(schema_name), sql.Identifier(table_name),
                sql.Identifier(runtime_role),
            )
        )
    conn.execute(
        sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(
            sql.Identifier(schema_name), sql.Identifier(runtime_role),
        )
    )
    if conn.execute(
        "SELECT has_schema_privilege(%s, %s, 'CREATE')",
        (runtime_role, schema_name),
    ).fetchone()[0]:
        raise ValueError("runtime role still has schema CREATE privilege")


def main() -> int:
    migration_dsn = os.environ.get("QUANT_MIGRATION_DSN", "")
    if not migration_dsn:
        raise SystemExit("QUANT_MIGRATION_DSN is required for explicit migrations")
    try:
        with psycopg.connect(migration_dsn) as conn:
            role = conn.execute("SELECT current_user").fetchone()[0]
            if role != _MIGRATION_ROLE:
                raise ValueError("migration connection must use quant_migration")
            applied = apply_migrations(conn, ROOT / "migrations")
            assert_schema_ready(conn, required_version=MIGRATION_VERSION)
            grant_runtime_privileges(conn)
            conn.commit()
    except Exception as exc:
        # psycopg exception text can include a DSN. Never forward it to a
        # terminal log, health endpoint, or deployment output.
        raise SystemExit(f"Phase 9 migration failed: {type(exc).__name__}") from None
    print(f"Phase 9 schema ready: {MIGRATION_VERSION}; applied {len(applied)} migration(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
