from datetime import datetime, timedelta, timezone
from decimal import Decimal
import os

import pytest

from quant_phase1.db import apply_migrations
from quant_phase3.contracts import FlowStatus
from quant_phase3.flow import TradeFlowWindow
from quant_phase3.persistence import Phase3Repository


BASE = datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc)


def window(
    opened=BASE,
    *,
    exchange="bybit",
    canonical_symbol="BTC-USDT-PERP",
    timeframe="1m",
    status=FlowStatus.AVAILABLE,
):
    return TradeFlowWindow(
        exchange=exchange,
        canonical_symbol=canonical_symbol,
        timeframe=timeframe,
        window_open=opened,
        window_close=opened + timedelta(minutes=1),
        total_trade_count=1,
        buy_trade_count=1,
        sell_trade_count=0,
        unknown_trade_count=0,
        total_volume_base=Decimal("1"),
        buy_volume_base=Decimal("1"),
        sell_volume_base=Decimal("0"),
        unknown_volume_base=Decimal("0"),
        total_notional_usd=Decimal("100"),
        average_trade_size=Decimal("1"),
        trade_frequency=Decimal(1) / Decimal(60),
        delta_base=Decimal("1"),
        delta_ratio=Decimal("1"),
        first_trade_at=opened,
        last_trade_at=opened,
        freshness=status,
        status=status,
        status_reason="fixture partial" if status is FlowStatus.PARTIAL else None,
        processed_at=opened + timedelta(minutes=1),
    )


class RecordingCursor:
    def __init__(self):
        self.calls = []

    def executemany(self, sql, rows):
        self.calls.append((sql, list(rows)))


class RecordingConnection:
    def __init__(self):
        self.cursor_value = RecordingCursor()
        self.calls = []

    def cursor(self):
        return self.cursor_value

    def execute(self, sql, params=None):
        self.calls.append((sql, params))


def test_repository_uses_stable_window_identity_and_idempotent_upsert():
    connection = RecordingConnection()
    repository = Phase3Repository(connection)

    assert repository.insert_flow_windows([window()]) == 1
    sql, rows = connection.cursor_value.calls[0]

    assert "trade_flow_windows" in sql
    assert "on conflict (exchange, canonical_symbol, timeframe, window_open)" in sql.lower()
    assert rows[0][0:4] == ("bybit", "BTC-USDT-PERP", "1m", BASE)
    assert rows[0][21] == "AVAILABLE"


def test_repository_persists_cvd_with_explicit_status_and_reason():
    connection = RecordingConnection()
    repository = Phase3Repository(connection)
    point = type(
        "CVD",
        (),
        {
            "exchange": "bybit",
            "canonical_symbol": "BTC-USDT-PERP",
            "timeframe": "15m",
            "window_end": BASE,
            "value": Decimal("2"),
            "status": FlowStatus.AVAILABLE,
            "reason": None,
            "processed_at": BASE,
        },
    )()

    assert repository.insert_cvd_snapshots([point]) == 1
    sql, rows = connection.cursor_value.calls[0]
    assert "cvd_snapshots" in sql
    assert rows[0][0:4] == ("bybit", "BTC-USDT-PERP", "15m", BASE)
    assert rows[0][5] == "AVAILABLE"


def test_repository_retention_covers_all_phase3_tables():
    connection = RecordingConnection()
    repository = Phase3Repository(connection)

    repository.cleanup_flow(
        retention_days={"1m": 1, "5m": 1, "15m": 1, "1H": 1, "4H": 1},
        cvd_retention_days=2,
        gap_retention_days=3,
        cross_exchange_retention_days=4,
        enrichment_retention_days=5,
    )

    statements = "\n".join(sql for sql, _ in connection.calls).lower()
    assert "trade_flow_windows" in statements
    assert "cvd_snapshots" in statements
    assert "trade_gap_events" in statements
    assert "cross_exchange_flow_snapshots" in statements
    assert "stage1_flow_enrichment" in statements


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN is not configured")
def test_phase3_postgres_migration_insert_duplicate_utc_and_retention():
    import psycopg

    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as connection:
        apply_migrations(connection)
        apply_migrations(connection)
        repository = Phase3Repository(connection)
        row = window()
        assert repository.insert_flow_windows([row]) == 1
        assert repository.insert_flow_windows([row]) == 1
        count = connection.execute(
            "SELECT count(*) FROM trade_flow_windows WHERE exchange = 'bybit' AND canonical_symbol = 'BTC-USDT-PERP'"
        ).fetchone()[0]
        assert count == 1
        stored = connection.execute(
            "SELECT window_open, processed_at FROM trade_flow_windows WHERE exchange = 'bybit' AND canonical_symbol = 'BTC-USDT-PERP'"
        ).fetchone()
        assert stored[0].tzinfo is not None
        assert stored[1].tzinfo is not None
        old = window(BASE - timedelta(days=10))
        repository.insert_flow_windows([old])
        repository.cleanup_flow(
            retention_days={"1m": 1, "5m": 1, "15m": 1, "1H": 1, "4H": 1},
            cvd_retention_days=1,
            gap_retention_days=1,
        )
        remaining = connection.execute(
            "SELECT count(*) FROM trade_flow_windows WHERE window_open < now() - interval '1 day'"
        ).fetchone()[0]
        assert remaining == 0


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN is not configured")
def test_hydration_loads_closed_available_minutes_per_active_route_without_global_starvation():
    import psycopg

    from quant_phase3.rollup import FlowRollupBuilder

    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    target_anchor = now - timedelta(days=7)
    target_open = target_anchor.replace(hour=(target_anchor.hour // 4) * 4, minute=0)
    busy_anchor = now - timedelta(days=1)
    busy_open = busy_anchor.replace(hour=(busy_anchor.hour // 4) * 4, minute=0)
    target_route = ("bybit", "BTC-USDT-PERP")
    missing_route = ("bybit", "MISSING-USDT-PERP")
    busy_routes = [("bitget", "BTC-USDT-PERP"), ("bybit", "ETH-USDT-PERP")]
    busy_routes.extend((f"venue-{index:02d}", f"BUSY{index:02d}-USDT-PERP") for index in range(80))
    routes = [target_route, *busy_routes, missing_route]

    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as connection:
        connection.execute(
            """
            CREATE TEMP TABLE trade_flow_windows (
                exchange TEXT NOT NULL,
                canonical_symbol TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                window_open TIMESTAMPTZ NOT NULL,
                window_close TIMESTAMPTZ NOT NULL,
                total_trade_count INTEGER NOT NULL,
                buy_trade_count INTEGER NOT NULL,
                sell_trade_count INTEGER NOT NULL,
                unknown_trade_count INTEGER NOT NULL,
                total_volume_base NUMERIC NOT NULL,
                buy_volume_base NUMERIC NOT NULL,
                sell_volume_base NUMERIC NOT NULL,
                unknown_volume_base NUMERIC NOT NULL,
                total_notional_usd NUMERIC,
                average_trade_size NUMERIC NOT NULL,
                trade_frequency NUMERIC NOT NULL,
                delta_base NUMERIC,
                delta_ratio NUMERIC,
                first_trade_at TIMESTAMPTZ NOT NULL,
                last_trade_at TIMESTAMPTZ NOT NULL,
                freshness TEXT NOT NULL,
                status TEXT NOT NULL,
                status_reason TEXT,
                processed_at TIMESTAMPTZ NOT NULL,
                UNIQUE (exchange, canonical_symbol, timeframe, window_open)
            ) ON COMMIT DROP
            """
        )
        repository = Phase3Repository(connection)
        fixtures = [
            window(target_open + timedelta(minutes=index), exchange=target_route[0], canonical_symbol=target_route[1])
            for index in range(240)
        ]
        for exchange, symbol in busy_routes:
            fixtures.extend(
                window(busy_open + timedelta(minutes=index), exchange=exchange, canonical_symbol=symbol)
                for index in range(240)
            )
        fixtures.extend(
            window(
                busy_open + timedelta(minutes=index),
                exchange=missing_route[0],
                canonical_symbol=missing_route[1],
            )
            for index in range(240)
            if index != 17
        )
        fixtures.extend(
            (
                window(
                    busy_open + timedelta(minutes=17),
                    exchange=missing_route[0],
                    canonical_symbol=missing_route[1],
                    status=FlowStatus.PARTIAL,
                ),
                window(now + timedelta(minutes=5), exchange=target_route[0], canonical_symbol=target_route[1]),
                window(busy_open, exchange=target_route[0], canonical_symbol=target_route[1], timeframe="5m"),
            )
        )
        inserted = repository.insert_flow_windows(fixtures)
        assert inserted > 20_000
        legacy_global_rows = repository.load_recent_flow_windows(limit=20_000)
        legacy_target_count = sum(
            row.exchange == target_route[0]
            and row.canonical_symbol == target_route[1]
            and row.timeframe == "1m"
            and row.status is FlowStatus.AVAILABLE
            for row in legacy_global_rows
        )
        assert legacy_target_count < 240

        restored = repository.load_recent_available_minute_windows(routes=routes, per_route_limit=300)

    by_route = {}
    for row in restored:
        by_route.setdefault((row.exchange, row.canonical_symbol), []).append(row)
        assert row.timeframe == "1m"
        assert row.status is FlowStatus.AVAILABLE
        assert row.window_close <= now
    assert len(by_route[target_route]) == 240
    assert all(len(by_route[route]) == 240 for route in busy_routes)
    assert len(by_route[missing_route]) == 239
    assert by_route[target_route] == sorted(by_route[target_route], key=lambda row: row.window_open)

    rollups = FlowRollupBuilder().build(by_route[target_route], timeframe="4H", processed_at=now)
    target_rollup = next(row for row in rollups if row.window_open == target_open)
    assert target_rollup.status is FlowStatus.AVAILABLE

    partial_rollups = FlowRollupBuilder().build(by_route[missing_route], timeframe="4H", processed_at=now)
    missing_rollup = next(row for row in partial_rollups if row.window_open == busy_open)
    assert missing_rollup.status is FlowStatus.PARTIAL
    assert missing_rollup.status_reason == "MISSING_MINUTE_WINDOWS"
