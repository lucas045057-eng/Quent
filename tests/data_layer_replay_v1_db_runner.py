#!/usr/bin/env python3
"""Run the Phase 8 replay and database snapshot inside the isolated replay network."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import ModuleType

import psycopg
from psycopg import sql
from quant_phase8.persistence import Phase8Repository

TESTS = Path(__file__).resolve().parent
HASH_TABLE_PATTERNS = (
    "kline", "market_snapshot", "market_observation", "open_interest", "funding",
    "screening", "stage1", "universe", "trade_flow", "liquidation", "basis",
    "phase5_", "phase6_", "phase7_", "phase8_", "exchange_instrument", "symbol",
)
VOLATILE_TABLE_NAMES = {"runtime_health_events", "system_health_events"}


def _migration_versions(rows) -> list[str]:
    return [str(row[0]) for row in rows]


def _load_phase8_replay_module():
    module_spec = importlib.util.spec_from_file_location("phase8_replay_support", TESTS / "test_phase8_replay.py")
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError("Phase 8 deterministic replay support could not be loaded")
    module = importlib.util.module_from_spec(module_spec)
    sys.modules[module_spec.name] = module
    pytest_missing = sys.modules.get("pytest") is None
    previous_pytest = sys.modules.get("pytest")
    if pytest_missing:
        compatibility = ModuleType("pytest")
        compatibility.fixture = lambda function: function
        compatibility.skip = lambda reason: (_ for _ in ()).throw(
            RuntimeError(f"pytest-only fixture called by standalone replay: {reason}")
        )
        sys.modules["pytest"] = compatibility
    try:
        module_spec.loader.exec_module(module)
    finally:
        if pytest_missing:
            if previous_pytest is None:
                sys.modules.pop("pytest", None)
            else:
                sys.modules["pytest"] = previous_pytest
    return module


def _phase8_replay(dsn: str) -> dict:
    module = _load_phase8_replay_module()
    settings = module._settings()
    with psycopg.connect(dsn, autocommit=True) as connection:
        repository = Phase8Repository(connection, settings=settings)
        first = module._replay_once(repository, settings)
        counts_first, digest_first = module._persisted_counts_and_digest(connection)
        second = module._replay_once(repository, settings)
        counts_second, digest_second = module._persisted_counts_and_digest(connection)
        if counts_second != counts_first or digest_second != digest_first:
            raise RuntimeError("Phase 8 persistence replay is not idempotent")
        context_statuses = {
            key: {name: metric.status.value for name, metric in snapshot.metrics.items()}
            for key, snapshot in second["contexts"].items()
        }
    return {
        "persisted_counts": counts_first,
        "persisted_sha256": digest_first,
        "selected_ticker_count": len(first["selected"]),
        "context_statuses": context_statuses,
        "idempotency_pass": True,
    }


def _database_snapshot(dsn: str) -> dict:
    with psycopg.connect(dsn) as connection:
        current_database = connection.execute("SELECT current_database()").fetchone()[0]
        timezone_name = connection.execute("SHOW timezone").fetchone()[0]
        postgres_version = connection.execute("SHOW server_version").fetchone()[0]
        database_size = connection.execute("SELECT pg_database_size(current_database())").fetchone()[0]
        table_names = [row[0] for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ).fetchall()]
        counts = {
            name: connection.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name))).fetchone()[0]
            for name in table_names
        }
        stable_hashes: dict[str, str] = {}
        for name in table_names:
            if name in VOLATILE_TABLE_NAMES or not any(token in name for token in HASH_TABLE_PATTERNS):
                continue
            digest = hashlib.sha256()
            query = sql.SQL(
                "SELECT (to_jsonb(row_value) - ARRAY['created_at','updated_at'])::text "
                "FROM {} AS row_value ORDER BY 1"
            ).format(sql.Identifier(name))
            with connection.cursor(name=f"replay_hash_{len(stable_hashes)}") as cursor:
                cursor.execute(query)
                while rows := cursor.fetchmany(512):
                    for (serialized,) in rows:
                        digest.update(serialized.encode("utf-8"))
                        digest.update(b"\n")
            stable_hashes[name] = digest.hexdigest()
        migrations = connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        btc_block_count = connection.execute(
            "SELECT count(*) FROM phase7_onchain_transfer_events WHERE chain='BITCOIN' AND block_number=968443"
        ).fetchone()[0]
        chain_counts = connection.execute(
            "SELECT chain,count(*) FROM phase7_onchain_transfer_events GROUP BY chain ORDER BY chain"
        ).fetchall()
        cursors = connection.execute(
            "SELECT source_id,scope_kind,scope_key,cursor_value,status FROM phase7_ingestion_checkpoints "
            "ORDER BY source_id,scope_kind,scope_key"
        ).fetchall()
    return {
        "database": current_database,
        "timezone": timezone_name,
        "postgres_version": postgres_version,
        "database_size_bytes": database_size,
        "table_counts": counts,
        "stable_table_sha256": stable_hashes,
        "migration_versions": _migration_versions(migrations),
        "bitcoin_large_block_event_count": btc_block_count,
        "chain_event_counts": {str(chain): int(count) for chain, count in chain_counts},
        "cursors": [list(row) for row in cursors],
    }


def main() -> int:
    dsn = os.environ.get("POSTGRES_DSN")
    results_dir = Path(os.environ.get("REPLAY_RESULTS_DIR", "/replay-results"))
    if not dsn:
        raise RuntimeError("isolated replay database DSN is required")
    results_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "phase8": _phase8_replay(dsn),
        "database": _database_snapshot(dsn),
    }
    marker = results_dir / "phase8-db-replay.json"
    marker.write_text(json.dumps(result, sort_keys=True, separators=(",", ":"), default=str), encoding="utf-8")
    print("DATA_LAYER_REPLAY_V1_DB_READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
