from dataclasses import fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from quant_phase5.contracts import ContextStatus
from quant_phase1.config import Settings
from quant_phase1.contracts import DataStatus, Ticker
from quant_phase1.pipeline import MarketDataBatch, run_stage1
from quant_phase1.stage1 import Stage1Result
from quant_phase1.entrypoints.engine import run_phase5_context_hook
from quant_phase5.runtime import Phase5Runtime, run_phase5_data_cycle, summarize_phase5_health


NOW = datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc)


def test_runtime_rebuild_is_bounded_and_preserves_source_status():
    runtime = Phase5Runtime(cache_capacity=2)
    loaded = runtime.rebuild_loaded_context(
        ({"key": key, "status": ContextStatus.STALE.value} for key in ("a", "b", "c"))
    )
    assert loaded == 2
    assert len(runtime.loaded_context) == 2
    assert runtime.loaded_context.get("a") is None
    assert runtime.loaded_context.get("c")["status"] == "STALE"


def test_runtime_health_degrades_on_db_outage_and_recovers():
    runtime = Phase5Runtime(cache_capacity=2)
    attempts = []
    events = []

    runtime = Phase5Runtime(cache_capacity=2, health_event_writer=lambda snapshot, event: events.append(event))

    def persist(_rows):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("db unavailable")

    failed = runtime.run_cycle(({"key": "a", "status": "AVAILABLE"},), persist=persist, now=NOW)
    assert failed.persisted == 0
    assert runtime.health.snapshot("phase5_context").status is ContextStatus.ERROR
    recovered = runtime.run_cycle(({"key": "a", "status": "STALE"},), persist=persist, now=NOW)
    assert recovered.persisted == 1
    assert runtime.health.snapshot("phase5_context").status is ContextStatus.AVAILABLE
    assert runtime.loaded_context.get("a")["status"] == "STALE"
    assert events == [{"event": "recovered", "outage_started_at": NOW, "reason": "db unavailable"}]


def test_runtime_cycle_does_not_create_trading_actions_or_retry_forever():
    runtime = Phase5Runtime(cache_capacity=1)
    calls = []
    result = runtime.run_cycle(({"key": "a", "status": "AVAILABLE"},), persist=lambda rows: calls.append(rows), now=NOW)
    assert result.errors == 0
    assert len(calls) == 1
    assert not hasattr(runtime, "orders")


def test_phase5_health_cycle_keeps_source_freshness_evidence_without_rewriting_event_time():
    published = []
    runtime = Phase5Runtime(health_writer=published.append)
    source_timestamp = NOW - timedelta(minutes=15)
    fetched_at = NOW - timedelta(minutes=1)
    evidence = {
        "BTCUSDT:5m": {
            "status": "STALE",
            "source_timestamp": source_timestamp.isoformat(),
            "fetched_at": fetched_at.isoformat(),
            "checked_at": NOW.isoformat(),
            "freshness_age_seconds": 900,
            "freshness_threshold_seconds": 330,
            "reason": "SOURCE_NOT_ADVANCING",
        },
    }

    runtime.run_cycle(
        (),
        now=NOW,
        context_status=ContextStatus.STALE,
        health_details={"freshness_evidence": evidence},
    )

    assert published[-1].details["freshness_evidence"]["BTCUSDT:5m"]["source_timestamp"] == source_timestamp.isoformat()
    assert published[-1].details["freshness_evidence"]["BTCUSDT:5m"]["fetched_at"] == fetched_at.isoformat()
    assert published[-1].details["freshness_evidence"]["BTCUSDT:5m"]["reason"] == "SOURCE_NOT_ADVANCING"


def test_phase5_health_summary_exposes_actual_partial_outputs_and_reason_counts():
    rows = (
        SimpleNamespace(
            status=ContextStatus.PARTIAL,
            reason_code="INSUFFICIENT_COVERAGE",
            missing_evidence=("coverage:below_minimum", "context:missing"),
        ),
        SimpleNamespace(
            status=ContextStatus.PARTIAL,
            reason_code=None,
            missing_evidence=("benchmark_context",),
        ),
        SimpleNamespace(
            status=ContextStatus.AVAILABLE,
            reason_code=None,
            missing_evidence=(),
        ),
    )

    evidence = summarize_phase5_health(rows)

    assert evidence == {
        "phase5_status": "PARTIAL",
        "data_quality": "PARTIAL",
        "reason": "PARTIAL_CONTEXT_OUTPUTS",
        "reason_code": "INSUFFICIENT_COVERAGE",
        "output_count": 3,
        "available_output_count": 1,
        "partial_output_count": 2,
        "stale_output_count": 0,
        "not_available_output_count": 0,
        "error_output_count": 0,
        "missing_evidence_count": 3,
    }


class Cursor:
    def execute(self, sql, params):
        assert "phase5_market_leader_context" in sql
        assert params[0] <= 2

    def fetchall(self):
        return [
            ("BTCUSDT", "15m", NOW, "STALE"),
            ("ETHUSDT", "15m", NOW, "AVAILABLE"),
        ]


class Connection:
    def cursor(self):
        return Cursor()


def test_restart_rebuild_reads_canonical_rows_and_keeps_status():
    runtime = Phase5Runtime(cache_capacity=2)
    rows = runtime.rebuild_from_connection(Connection(), limit=100)
    assert len(rows) == 2
    assert runtime.loaded_context.get("BTCUSDT:15m:2026-09-22T01:00:00+00:00")["status"] == "STALE"


class BrokenConnection:
    class BrokenCursor:
        def execute(self, sql, params):
            raise RuntimeError("db unavailable")

    def cursor(self):
        return self.BrokenCursor()


def test_db_failure_publishes_error_to_independent_health_writer():
    published = []
    runtime = Phase5Runtime(health_writer=published.append)
    result = runtime.run_from_connection(BrokenConnection(), now=NOW)
    assert result.errors == 1
    assert published[-1].status is ContextStatus.ERROR


class Phase5Cursor:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((sql, params))

    def executemany(self, sql, values):
        self.statements.append((sql, tuple(values)))

    def fetchone(self):
        return (42,)


class Phase5Connection:
    def __init__(self):
        self.cursor_instance = Phase5Cursor()

    def cursor(self):
        return self.cursor_instance

    def execute(self, sql, params=None):
        self.cursor_instance.execute(sql, params)


class Phase5RepositoryStub:
    last_screening_run_id = 9

    def __init__(self):
        self.health_rows = []

    def upsert_system_health(self, *args, **kwargs):
        self.health_rows.append(args)

    def insert_runtime_health_event(self, *args, **kwargs):
        return None


def test_enabled_and_disabled_engine_paths_preserve_all_stage1_fields():
    tickers = [
        Ticker(
            symbol, Decimal("100"), Decimal("99.9"), Decimal("100.1"), Decimal("1"), Decimal("1"),
            Decimal("1000"), Decimal("100000"), Decimal("100"), Decimal("100"), NOW, NOW, NOW,
            DataStatus.AVAILABLE, {},
        )
        for symbol in ("BTCUSDT", "ETHUSDT")
    ]
    batch = MarketDataBatch(NOW, [], tickers, ("BTCUSDT", "ETHUSDT"), {"BTCUSDT": {}, "ETHUSDT": {}})

    def engine_path(enabled: bool):
        settings = Settings.from_env({"PHASE5_ENABLED": "1" if enabled else "0"})
        results = run_stage1(batch, now=NOW)
        repository = Phase5RepositoryStub()
        run_phase5_context_hook(
            settings, NOW, connection=Phase5Connection(), repository=repository,
            batch=batch, results=results,
        )
        return (
            tuple(
                tuple(getattr(result, field.name) for field in fields(Stage1Result))
                for result in results
            ),
            repository.health_rows,
        )

    disabled_results, disabled_health = engine_path(False)
    enabled_results, enabled_health = engine_path(True)
    assert disabled_results == enabled_results
    assert disabled_health == []
    freshness_rows = [row for row in enabled_health if row[0].startswith("phase5-freshness-")]
    assert len(freshness_rows) == 8
    assert all("source_timestamp" in row[3] and "checked_at" in row[3] for row in freshness_rows)
    assert all(row[3]["phase5_status"] in {"STALE", "NOT_AVAILABLE", "AVAILABLE"} for row in freshness_rows)


def test_phase5_data_cycle_executes_and_persists_all_context_layers():
    connection = Phase5Connection()
    batch = MarketDataBatch(
        collected_at=NOW,
        instruments=[],
        tickers=[],
        selected_symbols=("BTCUSDT", "ETHUSDT"),
        candles_by_symbol={"BTCUSDT": {}, "ETHUSDT": {}},
    )
    result = Stage1Result(
        symbol="BTCUSDT", category="B", reason="WAIT_FOR_TRIGGER", status=DataStatus.NOT_AVAILABLE,
        inputs_used=(), indicators={}, structure=None, reason_codes=("MISSING",), timestamp=NOW,
    )
    before = tuple(getattr(result, field.name) for field in fields(Stage1Result))

    counts = run_phase5_data_cycle(
        batch, [result], Settings.from_env({}), connection=connection,
        screening_run_id=9, processed_at=NOW,
    )

    assert counts["leader_context"] == 8
    assert counts["breadth"] == 4
    assert counts["regime"] == 4
    assert counts["relative_strength"] >= 3
    assert counts["sector_membership"] == 2
    assert counts["sector_context"] >= 4
    assert counts["stage1_enrichment"] == 1
    after = tuple(getattr(result, field.name) for field in fields(Stage1Result))
    assert before == after
    assert counts["phase5_status"] in {ContextStatus.STALE, ContextStatus.NOT_AVAILABLE}
    assert counts["health_diagnostics"]["phase5_status"] == counts["phase5_status"].value
    assert counts["health_diagnostics"]["data_quality"] == counts["phase5_status"].value
    assert counts["health_diagnostics"]["output_count"] > 0
    assert set(counts["freshness_evidence"]) >= {
        "BTCUSDT:5m", "ETHUSDT:5m", "BTCUSDT:15m", "ETHUSDT:15m",
        "BTCUSDT:1H", "ETHUSDT:1H", "BTCUSDT:4H", "ETHUSDT:4H",
        "timeframe:5m", "timeframe:15m", "timeframe:1H", "timeframe:4H",
    }
    assert all(
        "source_timestamp" in evidence and "checked_at" in evidence
        for evidence in counts["freshness_evidence"].values()
    )
    sql_text = "\n".join(statement for statement, _ in connection.cursor_instance.statements)
    for table in (
        "phase5_market_leader_context", "phase5_market_regime_snapshots",
        "phase5_relative_strength_snapshots", "phase5_sector_membership",
        "phase5_sector_context_snapshots", "stage1_phase5_context_enrichment",
    ):
        assert table in sql_text
