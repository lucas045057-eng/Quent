from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from quant_phase1.db import apply_migrations


MIGRATION = Path(__file__).parents[1] / "migrations" / "011_phase6_external_context.sql"
RUNTIME_MIGRATION = Path(__file__).parents[1] / "migrations" / "013_phase6_ai_contract_runtime.sql"
TABLES = (
    "phase6_source_registry",
    "phase6_news_events",
    "phase6_macro_events",
    "phase6_unlock_events",
    "phase6_ai_analyses",
    "phase6_ai_extractions",
    "phase6_ai_usage",
    "phase6_prompt_versions",
)


def test_phase6_migration_is_additive_bounded_and_utc():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    for table in TABLES:
        assert f"create table if not exists {table}" in sql
    assert "timestamptz" in sql
    assert "jsonb" in sql
    assert "on conflict" not in sql  # DDL remains idempotent through IF NOT EXISTS/indexes.
    for status in ("available", "partial", "stale", "not_available", "error"):
        assert status in sql
    for forbidden in ("drop ", "truncate", "delete from", "alter table"):
        assert forbidden not in sql
    for forbidden in ("orders", "positions", "private_api", "live_executor"):
        assert forbidden not in sql


def test_phase6_migration_has_natural_keys_and_retention_indexes():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    for index_name in (
        "phase6_news_events_fingerprint_uq",
        "phase6_macro_events_fingerprint_uq",
        "phase6_unlock_events_fingerprint_uq",
        "phase6_ai_cache_uq",
        "phase6_news_events_retention_idx",
        "phase6_macro_events_retention_idx",
        "phase6_unlock_events_retention_idx",
        "phase6_ai_usage_retention_idx",
        "phase6_ai_extractions_retention_idx",
    ):
        assert index_name in sql
    for field in (
        "event_id", "source_ref", "published_at", "observed_at", "event_at",
        "fetched_at", "processed_at", "content_hash", "raw_reference",
        "provenance", "prompt_version", "schema_version", "estimated_cost",
    ):
        assert field in sql


def test_phase6_runtime_migration_is_additive_idempotent_and_fails_closed():
    sql = RUNTIME_MIGRATION.read_text(encoding="utf-8").lower()
    assert "add column if not exists analysis_id bigint" in sql
    assert "add column if not exists execution_id uuid" in sql
    assert "group by request_hash" in sql and "having count(*) > 1" in sql
    assert "usage row does not map to exactly one analysis" in sql
    assert "md5('phase6-ai-usage:' || id::text)::uuid" in sql
    assert "create unique index if not exists phase6_ai_request_hash_uq" in sql
    assert "create unique index if not exists phase6_ai_usage_execution_uq" in sql
    assert "foreign key (analysis_id)" in sql
    assert "references phase6_ai_analyses(id)" in sql
    assert "on delete restrict" in sql
    assert "drop index if exists phase6_ai_cache_uq" in sql
    assert "drop table" not in sql
    assert "truncate" not in sql
    assert "delete from" not in sql
    runner = (MIGRATION.parents[1] / "src" / "quant_phase1" / "db.py").read_text(encoding="utf-8").lower()
    assert "_validate_phase6_ai_runtime_schema" in runner


def test_runner_validates_and_records_011_once(monkeypatch, tmp_path):
    import quant_phase1.db as db

    migration = tmp_path / "011_phase6_external_context.sql"
    migration.write_text("CREATE TABLE phase6_test (id INTEGER);", encoding="utf-8")

    class Connection:
        def __init__(self):
            self.versions = set()
            self.inserts = []

        def execute(self, sql, params=None):
            if str(sql).startswith("SELECT version FROM schema_migrations"):
                return [(version,) for version in sorted(self.versions)]
            if str(sql).startswith("INSERT INTO schema_migrations"):
                self.inserts.append(params[0])
                self.versions.add(params[0])
            return []

        @contextmanager
        def transaction(self):
            yield self

    connection = Connection()
    monkeypatch.setattr(db, "_validate_phase6_schema", lambda _: (_ for _ in ()).throw(RuntimeError("schema mismatch")))
    try:
        apply_migrations(connection, tmp_path)
    except RuntimeError as exc:
        assert "schema mismatch" in str(exc)
    else:
        raise AssertionError("incompatible migration must fail")
    assert "011_phase6_external_context.sql" not in connection.inserts

    monkeypatch.setattr(db, "_validate_phase6_schema", lambda _: None)
    assert apply_migrations(connection, tmp_path) == ["011_phase6_external_context.sql"]
    assert apply_migrations(connection, tmp_path) == []
