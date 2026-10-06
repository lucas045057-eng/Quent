from datetime import datetime, timedelta, timezone
from decimal import Decimal
from dataclasses import replace
import os
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from quant_phase1.db import apply_migrations
from quant_phase4.aggregation import LiquidationWindow
from quant_phase4.contracts import (
    BasisObservation,
    BasisType,
    CanonicalLiquidation,
    CoverageSemantics,
    DataStatus,
    LongShortMetricType,
    LongShortObservation,
    LongShortPopulationSemantics,
    LiquidationSide,
    QuantityUnit,
    ReasonCode,
    SourceGranularity,
)
from quant_phase4.persistence import Phase4Repository, Phase4Retention
from quant_phase4.cross_exchange import build_phase4_context
from quant_phase4.enrichment import enrich_stage1_phase4
from quant_phase1.stage1 import Stage1Result


BASE = datetime(2026, 9, 21, 1, 0, tzinfo=timezone.utc)


class RecordingCursor:
    def __init__(self):
        self.calls = []

    def executemany(self, sql, rows):
        self.calls.append((sql, list(rows)))


class Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def fetchall(self):
        return self.rows


class RecordingConnection:
    def __init__(self, results=()):
        self.cursor_value = RecordingCursor()
        self.calls = []
        self.results = iter(results)

    def cursor(self):
        return self.cursor_value

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        try:
            return next(self.results)
        except StopIteration:
            return Result()


def event(event_id="evt-1"):
    return CanonicalLiquidation(
        event_id=event_id,
        exchange="bitget",
        exchange_symbol="BTCUSDT",
        canonical_symbol="BTC-USDT-PERP",
        event_timestamp=BASE,
        received_at=BASE + timedelta(seconds=1),
        processed_at=BASE + timedelta(seconds=2),
        side=LiquidationSide.LIQUIDATED_LONG,
        raw_side="buy",
        raw_side_semantics="exchange side",
        price=Decimal("100"),
        raw_quantity=Decimal("2"),
        quantity_unit=QuantityUnit.BASE_ASSET,
        quantity_base=Decimal("2"),
        notional_usd=Decimal("200"),
        source_endpoint="/ws/liquidation",
        source_channel="liquidation",
        source_granularity=SourceGranularity.AGGREGATED_MAX_PER_SECOND,
        coverage_semantics=CoverageSemantics.PARTIAL_AGGREGATED,
        status=DataStatus.AVAILABLE,
        raw_reference="trace/evt-1",
        raw_payload={"debug": "must not persist"},
    )


def window():
    return LiquidationWindow(
        exchange="bitget",
        canonical_symbol="BTC-USDT-PERP",
        timeframe="1m",
        window_open=BASE,
        window_close=BASE + timedelta(minutes=1),
        observed_event_count=1,
        observed_liquidated_long_count=1,
        observed_liquidated_short_count=0,
        observed_notional_usd=Decimal("200"),
        largest_observed_notional_usd=Decimal("200"),
        source_exchange_count=1,
        source_granularity=SourceGranularity.AGGREGATED_MAX_PER_SECOND,
        coverage_semantics=CoverageSemantics.PARTIAL_AGGREGATED,
        status=DataStatus.AVAILABLE,
        reason=None,
        processed_at=BASE + timedelta(minutes=1),
    )


def long_short():
    return LongShortObservation(
        exchange="bybit",
        exchange_symbol="BTCUSDT",
        canonical_symbol="BTC-USDT-PERP",
        metric_type=LongShortMetricType.ACCOUNT_HOLDER_RATIO,
        population_semantics=LongShortPopulationSemantics.HOLDER_COUNT_RATIO,
        period="5m",
        long_value=Decimal("60"),
        short_value=Decimal("40"),
        ratio=Decimal("1.5"),
        exchange_timestamp=BASE,
        fetched_at=BASE + timedelta(seconds=1),
        received_at=BASE + timedelta(seconds=1),
        processed_at=BASE + timedelta(seconds=2),
        source_endpoint="/v5/market/account-ratio",
        status=DataStatus.AVAILABLE,
        raw_reference="trace/ls-1",
        raw_payload={"debug": "must not persist"},
    )


def basis():
    return BasisObservation(
        exchange="bitget",
        exchange_symbol="BTCUSDT",
        canonical_symbol="BTC-USDT-PERP",
        basis_type=BasisType.MARK_INDEX,
        perpetual_price=Decimal("100"),
        reference_price=Decimal("99"),
        absolute_basis=Decimal("1"),
        basis_bps=Decimal("101.0101"),
        basis_pct=Decimal("1.0101"),
        exchange_timestamp=BASE,
        fetched_at=BASE + timedelta(seconds=1),
        received_at=BASE + timedelta(seconds=1),
        processed_at=BASE + timedelta(seconds=2),
        max_timestamp_skew=timedelta(seconds=5),
        timestamp_skew=timedelta(seconds=1),
        source_endpoint="/api/v3/market/tickers",
        status=DataStatus.AVAILABLE,
        raw_reference="trace/basis-1",
        raw_payload={"debug": "must not persist"},
    )


def test_liquidation_persistence_strips_raw_payload_and_preserves_normalized_fields():
    connection = RecordingConnection()
    repository = Phase4Repository(connection)

    assert repository.insert_liquidation_events([event()]) == 1
    sql, rows = connection.cursor_value.calls[0]

    assert "raw_payload" not in sql.lower()
    assert "liquidation_events" in sql
    assert "on conflict (exchange, source_endpoint, source_event_id)" in sql.lower()
    assert rows[0].source_granularity == "AGGREGATED_MAX_PER_SECOND"
    assert rows[0].coverage_semantics == "PARTIAL_AGGREGATED"
    assert rows[0].quantity_unit == "BASE_ASSET"
    assert rows[0].raw_reference == "trace/evt-1"
    assert rows[0].event_timestamp == BASE
    assert rows[0].received_at == BASE + timedelta(seconds=1)


def test_recent_liquidation_window_loader_is_normalized_and_bounded():
    connection = RecordingConnection([Result([(
        "bitget", "BTC-USDT-PERP", "1m", BASE, BASE + timedelta(minutes=1),
        1, 1, 0, Decimal("200"), Decimal("200"), 1,
        "AGGREGATED_MAX_PER_SECOND", "PARTIAL_AGGREGATED", "AVAILABLE", None,
        BASE + timedelta(minutes=1),
    )])])
    rows = Phase4Repository(connection).load_recent_liquidation_windows(per_key_limit=240, max_rows=400)
    assert len(rows) == 1
    assert rows[0].timeframe == "1m"
    assert rows[0].status is DataStatus.AVAILABLE
    sql, params = connection.calls[0]
    assert "row_number() over" in sql.lower()
    assert "timeframe = '1m'" in sql.lower()
    assert params == (240, 400)


def test_liquidation_window_scopes_use_bounded_keyset_pages():
    connection = RecordingConnection([
        Result([("bitget", "BTC-USDT-PERP"), ("bitget", "ETH-USDT-PERP")]),
        Result([("bybit", "BTC-USDT-PERP")]),
    ])
    repository = Phase4Repository(connection)

    assert tuple(repository.iter_liquidation_window_scopes(page_size=2)) == (
        ("bitget", "BTC-USDT-PERP"),
        ("bitget", "ETH-USDT-PERP"),
        ("bybit", "BTC-USDT-PERP"),
    )
    assert len(connection.calls) == 2
    assert all("limit %s" in sql.lower() for sql, _ in connection.calls)
    assert connection.calls[0][1] == (2,)
    assert connection.calls[1][1] == ("bitget", "ETH-USDT-PERP", 2)


def test_batch_upserts_use_migration_conflict_keys_and_explicit_utc_values():
    connection = RecordingConnection()
    repository = Phase4Repository(connection)

    assert repository.insert_liquidation_windows([window()]) == 1
    assert repository.insert_long_short([long_short()]) == 1
    assert repository.insert_basis([basis()]) == 1

    statements = "\n".join(call[0] for call in connection.cursor_value.calls).lower()
    assert "on conflict (exchange, canonical_symbol, timeframe, window_open)" in statements
    assert "on conflict (exchange, canonical_symbol, metric_type, period, exchange_timestamp)" in statements
    assert "on conflict (exchange, canonical_symbol, basis_type, exchange_timestamp)" in statements
    assert "raw_payload" not in statements
    assert "processed_at" in statements
    for _, rows in connection.cursor_value.calls:
        assert all(value.tzinfo is not None and value.utcoffset() == timedelta(0) for value in rows[0] if isinstance(value, datetime))


def test_long_short_upsert_persists_population_semantics():
    connection = RecordingConnection()
    Phase4Repository(connection).insert_long_short([long_short()])
    sql, rows = connection.cursor_value.calls[0]
    assert "population_semantics" in sql
    assert rows[0].population_semantics == "HOLDER_COUNT_RATIO"


def test_cross_exchange_and_stage1_upserts_persist_bounded_context_and_are_idempotent():
    connection = RecordingConnection()
    repository = Phase4Repository(connection)
    context = build_phase4_context([event()], [long_short()], [basis()], BASE + timedelta(minutes=1))
    result = Stage1Result("BTCUSDT", "A", "ok", DataStatus.AVAILABLE, (), {}, "BULLISH")
    enrichment = enrich_stage1_phase4(result, context, BASE + timedelta(minutes=1))

    assert repository.insert_cross_exchange([context]) == 3
    assert repository.insert_cross_exchange([context]) == 3
    assert repository.insert_stage1_enrichment(7, [enrichment]) == 1
    assert repository.insert_stage1_enrichment(7, [enrichment]) == 1
    statements = "\n".join(sql for sql, _ in connection.cursor_value.calls).lower()
    assert "on conflict (canonical_symbol, metric, timeframe, snapshot_timestamp)" in statements
    assert "on conflict (screening_run_id, symbol)" in statements
    assert "raw_payload" not in statements
    cross_rows = connection.cursor_value.calls[0][1]
    assert cross_rows[0].context.obj["observations"][0]["exchange"] == "bitget"


def test_cross_exchange_absent_timeframe_uses_non_null_retry_key():
    connection = RecordingConnection()
    repository = Phase4Repository(connection)
    context = build_phase4_context([event()], [], [], BASE + timedelta(minutes=1))

    assert repository.insert_cross_exchange([context]) == 3
    assert repository.insert_cross_exchange([context]) == 3
    rows = connection.cursor_value.calls[0][1]
    assert all(row.timeframe == "" for row in rows if row.metric != "long_short")
    assert all(row.timeframe is not None for row in rows)


def test_cross_exchange_provenance_arrays_match_each_row_status():
    unavailable = replace(
        event("evt-missing"), exchange="hyperliquid", status=DataStatus.NOT_AVAILABLE, price=None,
        raw_quantity=None, quantity_unit=QuantityUnit.UNKNOWN,
        quantity_base=None, notional_usd=None,
    )
    stale = replace(event("evt-stale"), exchange="bybit", status=DataStatus.STALE)
    context = build_phase4_context([event("evt-available"), stale, unavailable], [], [], BASE)
    connection = RecordingConnection()

    Phase4Repository(connection).insert_cross_exchange([context])
    rows = connection.cursor_value.calls[0][1]
    liquidation_row = next(row for row in rows if row.metric == "liquidation")
    assert liquidation_row.missing_sources == ["hyperliquid"]
    assert liquidation_row.stale_sources == ["bybit"]


def test_loaded_liquidation_observation_has_context_contract_fields():
    connection = RecordingConnection([
        Result([(
            "bitget", "evt-1", "BTCUSDT", "BTC-USDT-PERP", BASE, BASE + timedelta(seconds=1),
            "LIQUIDATED_LONG", "buy", "exchange side", Decimal("100"), Decimal("2"),
            "BASE_ASSET", Decimal("2"), Decimal("200"), "/ws/liquidation", "liquidation",
            "AGGREGATED_MAX_PER_SECOND", "PARTIAL_AGGREGATED", "AVAILABLE", None, "trace/evt-1",
            BASE + timedelta(seconds=2),
        )]),
        Result(), Result(),
    ])
    loaded = Phase4Repository(connection).load_latest_observations(
        "BTC-USDT-PERP",
        long_short_metric_type=LongShortMetricType.ACCOUNT_HOLDER_RATIO,
        long_short_period="5m",
        basis_type=BasisType.MARK_INDEX,
    )
    row = loaded[0][0]
    assert row.quantity_unit is QuantityUnit.BASE_ASSET
    assert row.source_granularity is SourceGranularity.AGGREGATED_MAX_PER_SECOND
    assert row.coverage_semantics is CoverageSemantics.PARTIAL_AGGREGATED
    assert row.side is LiquidationSide.LIQUIDATED_LONG
    assert row.event_timestamp == BASE
    assert row.received_at == BASE + timedelta(seconds=1)
    assert row.processed_at == BASE + timedelta(seconds=2)


def test_loaded_observations_restore_reason_codes_and_reason_text_for_context_provenance():
    connection = RecordingConnection([
        Result(),
        Result([(
            "bybit", "BTCUSDT", "BTC-USDT-PERP", "ACCOUNT_HOLDER_RATIO", "UNCONFIRMED_PUBLIC_SOURCE",
            "5m", BASE, "NOT_AVAILABLE", "UNCONFIRMED_PUBLIC_SOURCE", "/public", BASE, BASE, BASE,
        )]),
        Result([(
            "bitget", "BTCUSDT", "BTC-USDT-PERP", "MARK_INDEX", BASE, "STALE",
            "UNCONFIRMED_SEMANTICS", "/public", BASE, BASE, BASE,
        )]),
    ])
    loaded = Phase4Repository(connection).load_latest_observations(
        "BTC-USDT-PERP",
        long_short_metric_type=LongShortMetricType.ACCOUNT_HOLDER_RATIO,
        long_short_period="5m",
        basis_type=BasisType.MARK_INDEX,
    )

    assert loaded[1][0].reason_code is ReasonCode.UNCONFIRMED_PUBLIC_SOURCE
    assert loaded[1][0].reason == "UNCONFIRMED_PUBLIC_SOURCE"
    assert loaded[2][0].reason_code is ReasonCode.UNCONFIRMED_SEMANTICS
    assert loaded[2][0].reason == "UNCONFIRMED_SEMANTICS"


def test_persisted_liquidation_gap_marks_loaded_context_stale_and_engine_enrichment():
    gap = (
        "bitget", "BTC-USDT-PERP", "1m", BASE, BASE + timedelta(minutes=1),
        0, 0, 0, None, None, 0, "NOT_AVAILABLE", "NOT_AVAILABLE", "STALE",
        "LIQUIDATION_GAP_NO_BACKFILL", BASE + timedelta(minutes=1),
    )
    connection = RecordingConnection([Result([(
        "bitget", "evt-before-gap", "BTCUSDT", "BTC-USDT-PERP", BASE - timedelta(minutes=2),
        BASE - timedelta(minutes=1), "LIQUIDATED_LONG", "buy", "public", Decimal("100"), Decimal("1"),
        "BASE_ASSET", Decimal("1"), Decimal("100"), "/ws", "liquidation", "AGGREGATED_MAX_PER_SECOND",
        "PARTIAL_AGGREGATED", "AVAILABLE", None, None, BASE - timedelta(minutes=1),
    )]), Result(), Result(), Result([gap])])
    repository = Phase4Repository(connection)
    loaded = repository.load_latest_observations(
        "BTC-USDT-PERP", long_short_metric_type=LongShortMetricType.ACCOUNT_HOLDER_RATIO,
        long_short_period="5m", basis_type=BasisType.MARK_INDEX,
    )
    context = build_phase4_context(*loaded, BASE + timedelta(minutes=2))
    assert context.liquidation.status is DataStatus.STALE
    assert "LIQUIDATION_GAP_NO_BACKFILL" in context.liquidation.reason_codes

    from quant_phase1.entrypoints.engine import persist_phase4_enrichment

    engine_connection = RecordingConnection([Result(), Result(), Result(), Result([gap])])
    run_id = persist_phase4_enrichment(
        type("Repository", (), {"connection": engine_connection})(), 7,
        [Stage1Result("BTCUSDT", "A", "ok", DataStatus.AVAILABLE, (), {}, None)],
        BASE + timedelta(minutes=2),
    )
    assert run_id == 1
    stage1_rows = engine_connection.cursor_value.calls[-1][1]
    assert stage1_rows[0][3] == "STALE"
    cross_exchange_rows = engine_connection.cursor_value.calls[0][1]
    assert isinstance(cross_exchange_rows[0][10], Jsonb)
    assert isinstance(stage1_rows[0][6], Jsonb)


def test_latest_context_is_bounded_and_missing_metrics_are_typed_not_available():
    connection = RecordingConnection([Result(), Result(), Result()])
    context = Phase4Repository(connection).load_latest_context(
        "BTC-USDT-PERP",
        long_short_metric_type=LongShortMetricType.ACCOUNT_HOLDER_RATIO,
        long_short_period="5m",
        basis_type=BasisType.MARK_INDEX,
    )

    assert context.liquidation.status is DataStatus.NOT_AVAILABLE
    assert context.long_short.status is DataStatus.NOT_AVAILABLE
    assert context.basis.status is DataStatus.NOT_AVAILABLE
    assert len(connection.calls) == 3
    assert "limit 64" in connection.calls[0][0].lower()
    assert all("limit 1" in sql.lower() for sql, _ in connection.calls[1:])
    assert all("select * from" not in sql.lower() for sql, _ in connection.calls)
    assert all("canonical_symbol = %s" in sql.lower() for sql, _ in connection.calls)


def test_latest_context_scopes_long_short_and_basis_families_independently():
    connection = RecordingConnection([
        Result(),
        Result([("AVAILABLE", "ls-5m")]),
        Result([("AVAILABLE", "basis-index")]),
        Result(),
        Result([("AVAILABLE", "ls-1h")]),
        Result([("AVAILABLE", "basis-oracle")]),
    ])
    repository = Phase4Repository(connection)

    first = repository.load_latest_context(
        "BTC-USDT-PERP",
        long_short_metric_type=LongShortMetricType.ACCOUNT_HOLDER_RATIO,
        long_short_period="5m",
        basis_type=BasisType.MARK_INDEX,
    )
    second = repository.load_latest_context(
        "BTC-USDT-PERP",
        long_short_metric_type=LongShortMetricType.ACCOUNT_HOLDER_RATIO,
        long_short_period="1h",
        basis_type=BasisType.MARK_ORACLE,
    )

    assert first.long_short.reason == "ls-5m"
    assert first.basis.reason == "basis-index"
    assert second.long_short.reason == "ls-1h"
    assert second.basis.reason == "basis-oracle"
    assert len(connection.calls) == 6
    assert connection.calls[1][1] == ("BTC-USDT-PERP", "ACCOUNT_HOLDER_RATIO", "5m")
    assert connection.calls[2][1] == ("BTC-USDT-PERP", "MARK_INDEX")
    assert connection.calls[4][1] == ("BTC-USDT-PERP", "ACCOUNT_HOLDER_RATIO", "1h")
    assert connection.calls[5][1] == ("BTC-USDT-PERP", "MARK_ORACLE")
    assert "metric_type = %s" in connection.calls[1][0]
    assert "period = %s" in connection.calls[1][0]
    assert "basis_type = %s" in connection.calls[2][0]
    assert "limit 64" in connection.calls[0][0].lower()
    assert "limit 64" in connection.calls[3][0].lower()
    assert all("limit 1" in sql.lower() for sql, _ in (connection.calls[1:3] + connection.calls[4:6]))


def test_retention_is_deterministic_configured_and_phase4_only():
    connection = RecordingConnection()
    repository = Phase4Repository(connection)
    retention = Phase4Retention(
        liquidation_event_hours=24,
        liquidation_window_days={"1m": 7, "5m": 30},
        long_short_days=90,
        basis_days=91,
        cross_exchange_days=92,
        enrichment_days=93,
    )

    repository.cleanup(retention)
    statements = "\n".join(sql for sql, _ in connection.calls).lower()
    assert [params for _, params in connection.calls] == [
        (24,), ("1m", 7), ("5m", 30), (90,), (91,), (92,), (93,)
    ]
    for table in (
        "liquidation_events",
        "liquidation_windows",
        "long_short_observations",
        "basis_snapshots",
        "cross_exchange_phase4_snapshots",
        "stage1_phase4_enrichment",
    ):
        assert table in statements
    assert not any(table in statements for table in ("market_snapshots", "trade_flow_windows", "open_interest", "funding_rates"))


@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="RUNTIME_ACCEPTANCE_REQUIRED: TEST_POSTGRES_DSN is not configured",
)
def test_phase4_postgres_runtime_acceptance_is_explicitly_gated():
    import psycopg

    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as connection:
        apply_migrations(connection)
        apply_migrations(connection)
        repository = Phase4Repository(connection)
        row = event("phase4-task6-runtime-event")
        assert repository.insert_liquidation_events([row]) == 1
        assert repository.insert_liquidation_events([row]) == 1
        stored = connection.execute(
            "SELECT event_timestamp, received_at, processed_at FROM liquidation_events "
            "WHERE exchange = 'bitget' AND source_event_id = %s",
            (row.event_id,),
        ).fetchone()
        assert stored[0].tzinfo is not None
        assert stored[1].tzinfo is not None
        assert stored[2].tzinfo is not None
        assert connection.execute(
            "SELECT count(*) FROM liquidation_events WHERE source_event_id = %s", (row.event_id,)
        ).fetchone()[0] == 1
        repository.cleanup(
            Phase4Retention(
                liquidation_event_hours=1,
                liquidation_window_days={"1m": 1},
                long_short_days=1,
                basis_days=1,
                cross_exchange_days=1,
                enrichment_days=1,
            )
        )

        column = connection.execute(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'cross_exchange_phase4_snapshots' AND column_name = 'timeframe'"
        ).fetchone()
        assert column[0] == "NO"
        assert column[1] is not None and "''" in column[1]

    # Exercise the upgrade path against a temporary old-009 schema without
    # changing any shared test tables. The greatest id is the deterministic
    # survivor when mixed NULL/blank identities collide after normalization.
    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as upgrade_connection:
        upgrade_connection.execute(
            "CREATE TEMP TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        upgrade_connection.execute(
            "INSERT INTO schema_migrations (version) VALUES ('009_phase4_metrics.sql')"
        )
        upgrade_connection.execute(
            "CREATE TEMP TABLE long_short_observations (id BIGSERIAL PRIMARY KEY)"
        )
        upgrade_connection.execute(
            """
            CREATE TEMP TABLE cross_exchange_phase4_snapshots (
                id BIGSERIAL PRIMARY KEY,
                canonical_symbol TEXT NOT NULL,
                metric TEXT NOT NULL,
                timeframe TEXT,
                snapshot_timestamp TIMESTAMPTZ NOT NULL,
                UNIQUE (canonical_symbol, metric, timeframe, snapshot_timestamp)
            )
            """
        )
        upgrade_connection.execute(
            "INSERT INTO cross_exchange_phase4_snapshots "
            "(canonical_symbol, metric, timeframe, snapshot_timestamp) "
            "VALUES ('LEGACY', 'basis', '', %s), ('LEGACY', 'basis', NULL, %s), "
            "('LEGACY', 'basis', '1m', %s)",
            (BASE, BASE, BASE),
        )

        migration_dir = Path(__file__).parents[1] / "migrations" / "__no_migrations__"
        assert apply_migrations(upgrade_connection, migration_dir) == []
        assert upgrade_connection.execute(
            "SELECT id, timeframe FROM cross_exchange_phase4_snapshots ORDER BY id"
        ).fetchall() == [(2, ""), (3, "1m")]
        assert apply_migrations(upgrade_connection, migration_dir) == []
        assert upgrade_connection.execute(
            "SELECT count(*) FROM cross_exchange_phase4_snapshots WHERE timeframe = ''"
        ).fetchone()[0] == 1
