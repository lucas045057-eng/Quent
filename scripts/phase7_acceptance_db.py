#!/usr/bin/env python3
"""Secret-safe PostgreSQL migration and evidence snapshots for local Phase 7 acceptance."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any

import psycopg

from quant_phase1.db import assert_schema_ready


PHASE7_TABLES = (
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


def _dsn() -> str:
    value = os.environ.get("POSTGRES_DSN")
    if not value:
        raise RuntimeError("POSTGRES_DSN is required")
    return value


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def check_ready() -> None:
    with psycopg.connect(_dsn()) as connection:
        assert_schema_ready(connection, required_version="014_phase7_exact_amount_constraint.sql")
        timezone = connection.execute("SHOW TIME ZONE").fetchone()[0]
        migration_count = connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
        if timezone != "UTC":
            raise RuntimeError("UTC validation failed")
        print(_json({
            "schema_ready": True,
            "required_version": "014_phase7_exact_amount_constraint.sql",
            "recorded_migration_rows": migration_count,
            "database_timezone": timezone,
        }))


def snapshot() -> None:
    with psycopg.connect(_dsn()) as connection:
        assert_schema_ready(connection, required_version="014_phase7_exact_amount_constraint.sql")
        result: dict[str, Any] = {
            "database": connection.execute("SELECT current_database()").fetchone()[0],
            "timezone": connection.execute("SHOW TIME ZONE").fetchone()[0],
            "migration_versions": [row[0] for row in connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )],
            "row_counts": {},
        }
        for table in PHASE7_TABLES + ("system_health", "runtime_health_events"):
            result["row_counts"][table] = connection.execute(
                f"SELECT count(*) FROM {table}"
            ).fetchone()[0]
        sizes = connection.execute(
            """
            SELECT pg_database_size(current_database()),
                   pg_total_relation_size('phase7_onchain_transfer_events'),
                   pg_total_relation_size('phase7_spot_flow_windows')
            """
        ).fetchone()
        result["storage_bytes"] = {
            "database": sizes[0],
            "onchain_transfer_events": sizes[1],
            "spot_flow_windows": sizes[2],
        }
        result["event_ranges"] = [dict(zip(
            ("chain", "status", "rows", "min_block", "max_block", "min_event_time", "max_event_time"), row
        )) for row in connection.execute(
            """
            SELECT chain, status, count(*), min(block_number), max(block_number),
                   min(event_time), max(event_time)
            FROM phase7_onchain_transfer_events
            GROUP BY chain, status ORDER BY chain, status
            """
        )]
        result["checkpoints"] = [dict(zip(
            ("source_id", "scope_kind", "scope_key", "cursor_kind", "cursor_value",
             "last_observed_cursor", "last_finalized_cursor", "status", "updated_at"), row
        )) for row in connection.execute(
            """
            SELECT source_id, scope_kind, scope_key, cursor_kind, cursor_value,
                   last_observed_cursor, last_finalized_cursor, status, updated_at
            FROM phase7_ingestion_checkpoints ORDER BY source_id, scope_key
            """
        )]
        result["spot_latest"] = [dict(zip(
            ("exchange", "symbol", "timeframe", "window_open", "window_close", "trade_count", "status", "processed_at"), row
        )) for row in connection.execute(
            """
            SELECT DISTINCT ON (exchange, symbol, timeframe)
                   exchange, symbol, timeframe, window_open, window_close, trade_count, status, processed_at
            FROM phase7_spot_flow_windows
            ORDER BY exchange, symbol, timeframe, window_open DESC
            """
        )]
        result["health"] = [dict(zip(("component", "status", "checked_at"), row)) for row in connection.execute(
            "SELECT component, status, checked_at FROM system_health ORDER BY component"
        )]
        print(_json(result))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("check", "snapshot"))
    args = parser.parse_args()
    if args.action == "check":
        check_ready()
    else:
        snapshot()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
