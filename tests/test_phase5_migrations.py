from pathlib import Path


MIGRATIONS = Path(__file__).parents[1] / "migrations"
MIGRATION = MIGRATIONS / "010_phase5_context.sql"
PHASE5_TABLES = (
    "phase5_market_leader_context",
    "phase5_market_regime_snapshots",
    "phase5_relative_strength_snapshots",
    "phase5_sector_membership",
    "phase5_sector_context_snapshots",
    "stage1_phase5_context_enrichment",
)


def test_phase5_migration_is_additive_and_creates_only_approved_tables():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "create table if not exists" in sql
    for table in PHASE5_TABLES:
        assert f"create table if not exists {table}" in sql
    for forbidden in ("drop table", "truncate", "delete from", "orders", "positions", "raw_klines"):
        assert forbidden not in sql
    assert "timestamptz" in sql
    assert "on delete cascade" in sql
    assert "coalesce(universe_run_id, 0)" in sql


def test_phase5_migration_has_required_replay_indexes_and_retention_indexes():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    for index_name in (
        "phase5_regime_replay_uq",
        "phase5_rs_leader_replay_uq",
        "phase5_rs_market_replay_uq",
        "phase5_sector_context_replay_uq",
        "phase5_market_leader_context_lookup_idx",
        "phase5_market_leader_context_retention_idx",
        "stage1_phase5_context_enrichment_retention_idx",
        "screening_runs_phase5_retention_idx",
    ):
        assert index_name in sql
    assert "where benchmark in" in sql
    assert "btcusdt" in sql and "ethusdt" in sql
    assert "where benchmark = 'market_universe_equal_weight'" in sql


def test_phase5_migration_contains_all_context_statuses_and_identity_columns():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    for status in ("available", "partial", "stale", "not_available", "error"):
        assert status in sql
    for field in (
        "context_timestamp",
        "processed_at",
        "calculation_version",
        "input_reference",
        "mapping_version",
        "context_only",
    ):
        assert field in sql


def test_phase5_migration_does_not_change_previous_files():
    assert tuple(sorted(path.name for path in MIGRATIONS.glob("00[1-9]_*.sql"))) == (
        "001_phase1_core.sql",
        "002_runtime_health_and_explainability.sql",
        "003_kline_source_exchange.sql",
        "004_phase2_derivatives.sql",
        "005_phase2_observation_idempotency.sql",
        "006_phase2_stage1_enrichment.sql",
        "007_phase1_runtime_query_indexes.sql",
        "008_phase3_flow.sql",
        "009_phase4_metrics.sql",
    )


def test_phase5_runner_validates_before_recording_version(monkeypatch, tmp_path):
    from contextlib import contextmanager

    import quant_phase1.db as db

    migration = tmp_path / "010_phase5_context.sql"
    migration.write_text("CREATE TABLE phase5_test (id INTEGER);", encoding="utf-8")

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
    monkeypatch.setattr(db, "_validate_phase5_schema", lambda _: (_ for _ in ()).throw(RuntimeError("schema mismatch")))
    try:
        db.apply_migrations(connection, tmp_path)
    except RuntimeError as exc:
        assert "schema mismatch" in str(exc)
    else:
        raise AssertionError("incompatible migration must fail")
    assert "010_phase5_context.sql" not in connection.inserts


def test_phase5_runner_records_validated_010_once(monkeypatch, tmp_path):
    from contextlib import contextmanager

    import quant_phase1.db as db

    migration = tmp_path / "010_phase5_context.sql"
    migration.write_text("CREATE TABLE phase5_test (id INTEGER);", encoding="utf-8")

    class Connection:
        def __init__(self):
            self.versions = set()

        def execute(self, sql, params=None):
            if str(sql).startswith("SELECT version FROM schema_migrations"):
                return [(version,) for version in sorted(self.versions)]
            if str(sql).startswith("INSERT INTO schema_migrations"):
                self.versions.add(params[0])
            return []

        @contextmanager
        def transaction(self):
            yield self

    connection = Connection()
    monkeypatch.setattr(db, "_validate_phase5_schema", lambda _: None)
    assert db.apply_migrations(connection, tmp_path) == ["010_phase5_context.sql"]
    assert db.apply_migrations(connection, tmp_path) == []


def test_postgres_sector_diagnostic_check_normalization_is_accepted():
    from quant_phase1.db import _phase5_sector_diagnostic_matches

    postgres_definition = (
        "CHECK (((universe_run_id IS NOT NULL) OR ((status = 'NOT_AVAILABLE'::text) "
        "AND (reason_code = ANY (ARRAY['MISSING_INPUT'::text, "
        "'INSUFFICIENT_COVERAGE'::text, 'NO_ELIGIBLE_MEMBERS'::text])))))"
    )
    assert _phase5_sector_diagnostic_matches((postgres_definition,)) is True


def test_postgres_separate_foreign_keys_are_validated_independently():
    from quant_phase1.db import _phase5_foreign_keys_match

    definitions = (
        "FOREIGN KEY (symbol) REFERENCES symbols(symbol)",
        "FOREIGN KEY (universe_run_id) REFERENCES universe_runs(id)",
    )
    assert _phase5_foreign_keys_match(
        "phase5_relative_strength_snapshots", definitions
    ) is True


def test_postgres_expression_index_cast_normalization_is_accepted():
    from quant_phase1.db import _phase5_expression_index_has_universe_coalesce

    postgres_definition = (
        "CREATE UNIQUE INDEX phase5_regime_replay_uq ON public.phase5_market_regime_snapshots "
        "USING btree (timeframe, context_timestamp, COALESCE(universe_run_id, (0)::bigint), calculation_version)"
    )
    assert _phase5_expression_index_has_universe_coalesce(postgres_definition) is True
    assert _phase5_expression_index_has_universe_coalesce(
        postgres_definition.replace("COALESCE(universe_run_id, (0)::bigint)", "universe_run_id")
    ) is False


def test_postgres_expression_index_keys_normalize_integer_casts():
    from quant_phase1.db import _index_keys

    postgres_definition = (
        "CREATE UNIQUE INDEX phase5_regime_replay_uq ON public.phase5_market_regime_snapshots "
        "USING btree (timeframe, context_timestamp, COALESCE(universe_run_id, (0)::bigint), calculation_version)"
    )
    assert _index_keys(postgres_definition) == (
        "timeframe",
        "context_timestamp",
        "coalesce(universe_run_id, 0)",
        "calculation_version",
    )
