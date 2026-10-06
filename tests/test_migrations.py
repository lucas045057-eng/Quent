from pathlib import Path
from contextlib import contextmanager

from quant_phase1.db import apply_migrations
from tests._migration_helpers import normalized_sha256


MIGRATION = Path(__file__).parents[1] / "migrations" / "001_phase1_core.sql"
MIGRATIONS = MIGRATION.parent
PHASE8_BASELINE_HASHES = {
    "001_phase1_core.sql": "1847ede5653c8eaf340604f42963fc1782e4dca4bf42237be449b0fbbc1c6ded",
    "002_runtime_health_and_explainability.sql": "70053152dc7bde1363ae93e5b4d8e919e5335261fcf9b8d456e44f3fb435342d",
    "003_kline_source_exchange.sql": "5740d6ce87b9010b2b9e9ff9be1f876e4b1174950b76fbf6f45a59ce3dc97bab",
    "004_phase2_derivatives.sql": "66dc492a3b342d19a0242e5bc218f5fb515c847a9f18a81cba0375601d2012e6",
    "005_phase2_observation_idempotency.sql": "57109d9554e548af6a4fcd949977a44d54482583f132378b67df2574110d5f37",
    "006_phase2_stage1_enrichment.sql": "b290e9656a403678f6d000530554102a94dcfd63400a9e80690cd37c8291a3f2",
    "007_phase1_runtime_query_indexes.sql": "57937096b8a32f7afc06f983969a715a3aeae28f21b1de020b824cf105d3b39d",
    "008_phase3_flow.sql": "3c1cc3e0b50cc0aa0a8329ab7da8f9f704c9f23dde620a44f949502c4ac430b2",
    "009_phase4_metrics.sql": "67873867bf2d7c2e888864f26fb33fe2e9543464b43b681280d65c9319864940",
    "010_phase5_context.sql": "87d40887e0c86fb9a5923b23f9628264dc0085020eba24ac4720f027d4708c66",
    "011_phase6_external_context.sql": "ae90195271fad35ec97b3fa7ad0da984961f707cf8d05e768b92afa08c456c00",
    "012_phase7_onchain_spot_context.sql": "b25f32a6750b590a08f93a6173332a8290b9fcbd0fe64aa5cfac4c7fb68e2f72",
    "013_phase6_ai_contract_runtime.sql": "85c76b9c815da415d664620d509c8aa64b6bf538b2eade2c9a18e27d5d094c84",
    "014_phase7_exact_amount_constraint.sql": "049ec603654effba802655aff0a040f344ca76018cdb5c4fc347f1e938d423f5",
}


def test_phase8_migration_leaves_migrations_001_through_014_unchanged():
    actual = {
        name: normalized_sha256(MIGRATIONS / name)
        for name in PHASE8_BASELINE_HASHES
    }

    assert actual == PHASE8_BASELINE_HASHES


def test_phase1_migration_contains_core_tables():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    for table in (
        "symbols",
        "ingest_batches",
        "market_observations",
        "klines",
        "market_snapshots",
        "universe_runs",
        "universe_members",
        "screening_runs",
        "screening_results",
        "system_health",
        "outbox_events",
    ):
        assert f"create table if not exists {table}" in sql


def test_phase1_migration_keeps_utc_status_and_kline_idempotency():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "timestamptz" in sql
    assert "available" in sql and "stale" in sql and "not_available" in sql and "error" in sql
    assert "unique (symbol, interval, bar_open_timestamp)" in sql


def test_phase1_migration_has_no_order_or_position_runtime_tables():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "create table if not exists orders" not in sql
    assert "create table if not exists positions" not in sql


def test_runtime_migration_has_explainability_and_outage_events():
    migration = MIGRATION.parent / "002_runtime_health_and_explainability.sql"
    sql = migration.read_text(encoding="utf-8").lower()
    assert "screening_results" in sql
    assert "reason_codes" in sql
    assert "key_metrics" in sql
    assert "data_snapshot_reference" in sql
    assert "runtime_health_events" in sql


def test_followup_kline_contract_migration_is_idempotent():
    migration = MIGRATION.parent / "003_kline_source_exchange.sql"
    sql = migration.read_text(encoding="utf-8").lower()
    assert "alter table klines" in sql
    assert "add column if not exists exchange" in sql


def test_phase2_stage1_enrichment_is_context_only():
    migration = MIGRATION.parent / "006_phase2_stage1_enrichment.sql"
    sql = migration.read_text(encoding="utf-8").lower()
    assert "stage1_derivative_enrichment" in sql
    assert "screening_run_id" in sql
    assert "derivative_status" in sql
    assert "orders" not in sql and "positions" not in sql


def test_runtime_query_indexes_cover_latest_batch_lookups():
    migration = MIGRATION.parent / "007_phase1_runtime_query_indexes.sql"
    sql = migration.read_text(encoding="utf-8").lower()
    assert "market_snapshots_available_latest_idx" in sql
    assert "klines_available_symbol_interval_open_idx" in sql
    assert "where status = 'available'" in sql


def test_migration_runner_serializes_concurrent_startup():
    runner = (MIGRATION.parent.parent / "src" / "quant_phase1" / "db.py").read_text(encoding="utf-8")
    assert "pg_advisory_xact_lock" in runner


def test_phase3_flow_migration_is_additive_utc_and_idempotent():
    migration = MIGRATION.parent / "008_phase3_flow.sql"
    sql = migration.read_text(encoding="utf-8").lower()
    for table in (
        "trade_flow_windows",
        "cvd_snapshots",
        "cross_exchange_flow_snapshots",
        "trade_gap_events",
        "stage1_flow_enrichment",
    ):
        assert f"create table if not exists {table}" in sql
    assert "timestamptz" in sql
    assert "partial" in sql
    assert "unique (exchange, canonical_symbol, timeframe, window_open)" in sql
    assert "create table if not exists raw_trades" not in sql
    assert "create table if not exists orders" not in sql
    assert "create table if not exists positions" not in sql


class _RecordedMigrationConnection:
    def __init__(self):
        self.statements = []
        self.versions = {"009_phase4_metrics.sql"}

    def execute(self, sql, params=None):
        self.statements.append((sql, params))
        if sql.startswith("SELECT version FROM schema_migrations"):
            return [(version,) for version in sorted(self.versions)]
        if sql.startswith("INSERT INTO schema_migrations") and params:
            self.versions.add(params[0])
        return []

    @contextmanager
    def transaction(self):
        yield self


def test_recorded_old_009_schema_receives_population_semantics_repair():
    connection = _RecordedMigrationConnection()

    assert apply_migrations(connection, MIGRATION.parent / "__no_migrations__") == []
    repairs = [sql for sql, _ in connection.statements if "population_semantics" in sql]
    assert len(repairs) == 1
    assert "ADD COLUMN IF NOT EXISTS" in repairs[0]
    assert "UNCONFIRMED_PUBLIC_SOURCE" in repairs[0]
    statements = " ".join("\n".join(sql for sql, _ in connection.statements).lower().split())
    assert "row_number() over" in statements
    assert "partition by canonical_symbol, metric, coalesce(timeframe, ''), snapshot_timestamp" in statements
    assert "order by id desc" in statements
    assert "set timeframe = '' where timeframe is null" in statements
    assert "alter column timeframe set not null" in statements
    first_repair_count = len([sql for sql, _ in connection.statements if "population_semantics" in sql])
    assert apply_migrations(connection, MIGRATION.parent / "__no_migrations__") == []
    second_repair_count = len([sql for sql, _ in connection.statements if "population_semantics" in sql])
    assert second_repair_count == first_repair_count
