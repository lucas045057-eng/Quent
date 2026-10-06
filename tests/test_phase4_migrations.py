from pathlib import Path

from tests._migration_helpers import normalized_sha256


MIGRATIONS = Path(__file__).parents[1] / "migrations"
MIGRATION = MIGRATIONS / "009_phase4_metrics.sql"
PHASE4_TABLES = (
    "liquidation_events",
    "liquidation_windows",
    "long_short_observations",
    "basis_snapshots",
    "cross_exchange_phase4_snapshots",
    "stage1_phase4_enrichment",
)
UNCHANGED_MIGRATION_HASHES = {
    "001_phase1_core.sql": "1847ede5653c8eaf340604f42963fc1782e4dca4bf42237be449b0fbbc1c6ded",
    "002_runtime_health_and_explainability.sql": "70053152dc7bde1363ae93e5b4d8e919e5335261fcf9b8d456e44f3fb435342d",
    "003_kline_source_exchange.sql": "5740d6ce87b9010b2b9e9ff9be1f876e4b1174950b76fbf6f45a59ce3dc97bab",
    "004_phase2_derivatives.sql": "66dc492a3b342d19a0242e5bc218f5fb515c847a9f18a81cba0375601d2012e6",
    "005_phase2_observation_idempotency.sql": "57109d9554e548af6a4fcd949977a44d54482583f132378b67df2574110d5f37",
    "006_phase2_stage1_enrichment.sql": "b290e9656a403678f6d000530554102a94dcfd63400a9e80690cd37c8291a3f2",
    "007_phase1_runtime_query_indexes.sql": "57937096b8a32f7afc06f983969a715a3aeae28f21b1de020b824cf105d3b39d",
    "008_phase3_flow.sql": "3c1cc3e0b50cc0aa0a8329ab7da8f9f704c9f23dde620a44f949502c4ac430b2",
}


def test_phase4_migration_creates_only_required_additive_tables():
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    for table in PHASE4_TABLES:
        assert f"create table if not exists {table}" in sql
    assert "create table if not exists raw_liquidations" not in sql
    assert "raw_payload" not in sql
    assert "create table if not exists orders" not in sql
    assert "create table if not exists positions" not in sql


def test_phase4_migration_uses_utc_statuses_and_lookup_indexes():
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "timestamptz" in sql
    assert "('available','stale','not_available','error')" in sql.replace(" ", "")
    for table in PHASE4_TABLES:
        assert f"create index if not exists {table}_" in sql


def test_phase4_migration_includes_required_domain_fields_and_unique_identities():
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    for field in (
        "source_granularity",
        "coverage_semantics",
        "metric_type",
        "period",
        "basis_type",
        "timestamp_skew",
        "reason",
    ):
        assert field in sql
    for identity in (
        "unique (exchange, source_endpoint, source_event_id)",
        "unique (exchange, canonical_symbol, timeframe, window_open)",
        "unique (exchange, canonical_symbol, metric_type, period, exchange_timestamp)",
        "unique (exchange, canonical_symbol, basis_type, exchange_timestamp)",
        "unique (screening_run_id, symbol)",
    ):
        assert identity in sql
    assert "timeframe text not null default ''" in sql


def test_phase4_migration_does_not_modify_migrations_001_through_008():
    actual = {
        name: normalized_sha256(MIGRATIONS / name)
        for name in UNCHANGED_MIGRATION_HASHES
    }

    assert actual == UNCHANGED_MIGRATION_HASHES
