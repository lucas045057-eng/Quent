from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

import quant_phase1.db as db
from quant_phase1.db import apply_migrations


MIGRATION = Path(__file__).parents[1] / "migrations" / "012_phase7_onchain_spot_context.sql"
MIGRATION_014 = Path(__file__).parents[1] / "migrations" / "014_phase7_exact_amount_constraint.sql"
TABLES = (
    "phase7_asset_registry",
    "phase7_address_labels",
    "phase7_onchain_transfer_events",
    "phase7_onchain_flow_windows",
    "phase7_whale_flow_windows",
    "phase7_spot_flow_windows",
    "phase7_stablecoin_context",
    "phase7_ingestion_checkpoints",
    "stage1_phase7_context_enrichment",
)
INDEXES = (
    "phase7_asset_native_uq",
    "phase7_asset_erc20_uq",
    "phase7_asset_lookup_idx",
    "phase7_transfer_log_identity_uq",
    "phase7_transfer_tx_value_identity_uq",
    "phase7_transfer_vout_identity_uq",
    "phase7_spot_symbol_time_idx",
    "phase7_whale_threshold_lookup_idx",
    "phase7_stablecoin_category_time_idx",
    "phase7_checkpoint_status_idx",
    "phase7_enrichment_retention_idx",
)


def test_phase7_migration_is_additive_complete_and_domain_safe():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    for table in TABLES:
        assert f"create table if not exists {table}" in sql
    for index in INDEXES:
        assert f"create {'unique ' if index.endswith('_uq') else ''}index if not exists {index}" in sql
    assert "timestamptz" in sql
    assert "market_kind = 'spot'" in sql
    assert "event_index_kind = 'tx_value'" in sql
    assert "event_index_kind = 'vout_index'" in sql
    assert "aggregation_scope = 'stablecoin'" in sql
    assert "aggregation_eligible" in sql and "bridge_leg_id" in sql
    assert "known_address_count" in sql
    assert "labeled_address_count" in sql
    assert "source_quality" in sql
    assert "coverage_status" in sql
    assert "symbol in ('btcusdt','ethusdt')" in sql
    assert "foreign key (screening_run_id, symbol)" in sql
    assert "address = btrim(address)" in sql
    assert "dac17f958d2ee523a2206206994597c13d831ec7" in sql
    assert "a0b86991c6218b36c1d19d4a2e9eb0ce3606eb48" in sql
    assert "aggregation_eligible or (" in sql
    assert "amount_normalized = amount_raw::numeric" in sql
    assert "valuation_reason is not null" in sql
    assert "last_finalized_cursor" in sql
    assert "phase7_checkpoint_monotonicity_trigger" in sql
    assert "cursor_kind in ('block','log_index','trade_id','trade_sequence')" in sql
    assert "screening_run_id bigint" in sql
    assert "onchain_transfer_events" in sql
    for forbidden in ("drop ", "truncate", "delete from", "alter table"):
        assert forbidden not in sql
    for forbidden in ("create table if not exists orders", "create table if not exists positions", "private_api", "live_executor"):
        assert forbidden not in sql


def test_phase7_exact_amount_migration_is_forward_only_and_uses_exact_scaling():
    sql = " ".join(MIGRATION_014.read_text(encoding="utf-8").lower().split())
    assert "add constraint phase7_transfer_amount_exact_check" in sql
    assert "amount_normalized * power(10::numeric, decimals::numeric) = amount_raw::numeric" in sql
    assert "drop constraint %i" in sql
    assert "drop table" not in sql
    assert "truncate" not in sql
    assert "delete from" not in sql
    assert "update " not in sql


def test_phase7_exact_amount_schema_validator_rejects_division_check():
    class Result:
        def fetchall(self):
            return [("CHECK ((amount_normalized = amount_raw::numeric / power(10::numeric, decimals)))", True)]

    class Connection:
        def execute(self, _sql):
            return Result()

    with pytest.raises(RuntimeError, match="exact amount constraint is invalid"):
        db._validate_phase7_exact_amount_schema(Connection())


def test_phase7_exact_amount_schema_validator_accepts_multiplicative_check():
    class Result:
        def fetchall(self):
            return [("CHECK ((amount_normalized * power(10::numeric, decimals::numeric)) = amount_raw::numeric)", True)]

    class Connection:
        def execute(self, _sql):
            return Result()

    db._validate_phase7_exact_amount_schema(Connection())


class _MigrationConnection:
    def __init__(self, versions: set[str]):
        self.versions = set(versions)
        self.statements: list[tuple[str, object]] = []

    def execute(self, sql, params=None):
        self.statements.append((str(sql), params))
        if str(sql).startswith("SELECT version FROM schema_migrations"):
            return [(version,) for version in sorted(self.versions)]
        if str(sql).startswith("INSERT INTO schema_migrations") and params:
            self.versions.add(params[0])
        return []

    @contextmanager
    def transaction(self):
        yield self


def test_runner_records_012_once_after_validation(monkeypatch):
    old_versions = {
        path.name for path in MIGRATION.parent.glob("*.sql") if path.name != MIGRATION.name
    }
    old_versions.update({"009_phase4_metrics.sql", "009_phase4_metrics.repair.v1"})
    connection = _MigrationConnection(old_versions)
    monkeypatch.setattr(db, "_validate_phase5_schema", lambda _: None)
    monkeypatch.setattr(db, "_validate_phase6_schema", lambda _: None)
    monkeypatch.setattr(db, "_validate_phase7_schema", lambda _: None)
    monkeypatch.setattr(db, "_validate_phase6_ai_runtime_schema", lambda _: None)
    monkeypatch.setattr(db, "_validate_phase7_exact_amount_schema", lambda _: None)
    monkeypatch.setattr(db, "_validate_phase8_schema", lambda _: None)

    assert apply_migrations(connection, MIGRATION.parent) == [MIGRATION.name]
    assert MIGRATION.name in connection.versions
    assert apply_migrations(connection, MIGRATION.parent) == []
    assert sum(params == (MIGRATION.name,) for _, params in connection.statements) == 1


def test_runner_validation_failure_does_not_record_012(monkeypatch):
    old_versions = {
        path.name for path in MIGRATION.parent.glob("*.sql") if path.name != MIGRATION.name
    }
    old_versions.update({"009_phase4_metrics.sql", "009_phase4_metrics.repair.v1"})
    connection = _MigrationConnection(old_versions)
    monkeypatch.setattr(db, "_validate_phase5_schema", lambda _: None)
    monkeypatch.setattr(db, "_validate_phase6_schema", lambda _: None)
    monkeypatch.setattr(db, "_validate_phase7_schema", lambda _: (_ for _ in ()).throw(RuntimeError("phase7 schema mismatch")))
    monkeypatch.setattr(db, "_validate_phase6_ai_runtime_schema", lambda _: None)
    monkeypatch.setattr(db, "_validate_phase7_exact_amount_schema", lambda _: None)

    with pytest.raises(RuntimeError, match="phase7 schema mismatch"):
        apply_migrations(connection, MIGRATION.parent)
    assert MIGRATION.name not in connection.versions


def test_runner_records_014_once_and_revalidates_existing_schema(monkeypatch):
    old_versions = {
        path.name for path in MIGRATION_014.parent.glob("*.sql") if path.name != MIGRATION_014.name
    }
    old_versions.add("009_phase4_metrics.repair.v1")
    connection = _MigrationConnection(old_versions)
    monkeypatch.setattr(db, "_validate_phase5_schema", lambda _: None)
    monkeypatch.setattr(db, "_validate_phase6_schema", lambda _: None)
    monkeypatch.setattr(db, "_validate_phase7_schema", lambda _: None)
    monkeypatch.setattr(db, "_validate_phase6_ai_runtime_schema", lambda _: None)
    validation_calls = []
    monkeypatch.setattr(db, "_validate_phase7_exact_amount_schema", lambda _: validation_calls.append("validated"))
    monkeypatch.setattr(db, "_validate_phase8_schema", lambda _: None)

    assert apply_migrations(connection, MIGRATION_014.parent) == [MIGRATION_014.name]
    assert MIGRATION_014.name in connection.versions
    assert apply_migrations(connection, MIGRATION_014.parent) == []
    assert validation_calls == ["validated", "validated"]
    assert sum(params == (MIGRATION_014.name,) for _, params in connection.statements) == 1
