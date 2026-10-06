from datetime import datetime, timezone

from quant_phase1.config import Settings
from quant_phase5.retention import cleanup_phase5_retention


class Cursor:
    def __init__(self, rowcounts):
        self.rowcounts = iter(rowcounts)
        self.statements = []

    def execute(self, sql, params):
        self.statements.append((sql, params))

    @property
    def rowcount(self):
        return next(self.rowcounts)


class Connection:
    def __init__(self, rowcounts):
        self.cursor_obj = Cursor(rowcounts)

    def cursor(self):
        return self.cursor_obj


def test_retention_uses_configured_cutoffs_and_bounded_delete_batches():
    settings = Settings.from_env({
        "PHASE5_CONTEXT_RETENTION_5M_DAYS": "30",
        "PHASE5_CONTEXT_RETENTION_15M_DAYS": "60",
        "PHASE5_CONTEXT_RETENTION_1H_DAYS": "120",
        "PHASE5_CONTEXT_RETENTION_4H_DAYS": "240",
        "PHASE5_ENRICHMENT_RETENTION_DAYS": "30",
    })
    connection = Connection([0] * 15)
    result = cleanup_phase5_retention(
        connection, datetime(2026, 9, 22, tzinfo=timezone.utc), settings=settings, batch_size=25,
    )
    assert result == {
        "phase5_market_leader_context": 0,
        "phase5_market_regime_snapshots": 0,
        "phase5_relative_strength_snapshots": 0,
        "phase5_sector_context_snapshots": 0,
        "stage1_phase5_context_enrichment": 0,
    }
    for sql, params in connection.cursor_obj.statements:
        assert "LIMIT %s" in sql
        assert params[-1] == 25
        assert "screening_results" not in sql
        assert "DELETE FROM symbols" not in sql


def test_retention_is_configured_not_fixed_to_one_global_window():
    settings = Settings.from_env({"PHASE5_CONTEXT_RETENTION_5M_DAYS": "7", "PHASE5_ENRICHMENT_RETENTION_DAYS": "14"})
    connection = Connection([0] * 15)
    cleanup_phase5_retention(connection, datetime(2026, 9, 22, tzinfo=timezone.utc), settings=settings, batch_size=10)
    leader_cutoffs = [
        params[0]
        for sql, params in connection.cursor_obj.statements
        if sql.split("DELETE FROM ")[1].split()[0] == "phase5_market_leader_context"
    ]
    enrichment_cutoffs = [
        params[0]
        for sql, params in connection.cursor_obj.statements
        if sql.split("DELETE FROM ")[1].split()[0] == "stage1_phase5_context_enrichment"
    ]
    assert any(value.day == 15 for value in leader_cutoffs)
    assert enrichment_cutoffs[0].day == 8


def test_enrichment_retention_joins_parent_run_timestamp_and_batches_are_idempotent():
    settings = Settings.from_env({})
    connection = Connection([0] * 14 + [25, 5, 0])
    result = cleanup_phase5_retention(
        connection, datetime(2026, 9, 22, tzinfo=timezone.utc), settings=settings, batch_size=25,
    )
    assert result["stage1_phase5_context_enrichment"] == 30
    enrichment_sql = [sql for sql, _ in connection.cursor_obj.statements if "stage1_phase5_context_enrichment" in sql][-1]
    assert "JOIN screening_runs" in enrichment_sql
    assert "parent.run_timestamp" in enrichment_sql
    assert "DELETE FROM screening_runs" not in enrichment_sql
