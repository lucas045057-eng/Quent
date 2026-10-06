"""Bounded, configuration-driven cleanup for Phase 5 derived tables."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from quant_phase1.config import Settings
from quant_phase1.time import ensure_utc


_CONTEXT_TABLES = (
    "phase5_market_leader_context",
    "phase5_market_regime_snapshots",
    "phase5_relative_strength_snapshots",
    "phase5_sector_context_snapshots",
)
_TIMEFRAMES = {
    "phase5_market_leader_context": ("5m", "15m", "1H", "4H"),
    "phase5_market_regime_snapshots": ("5m", "15m", "1H", "4H"),
    "phase5_relative_strength_snapshots": ("15m", "1H", "4H"),
    "phase5_sector_context_snapshots": ("15m", "1H", "4H"),
}
_ENRICHMENT_TABLE = "stage1_phase5_context_enrichment"


def _delete_batch(connection: Any, table: str, cutoff: datetime, batch_size: int, timeframe: str | None) -> int:
    if table == _ENRICHMENT_TABLE:
        sql = f"""
            DELETE FROM {table}
            WHERE id IN (
                SELECT child.id
                FROM {table} AS child
                JOIN screening_runs AS parent ON parent.id = child.screening_run_id
                WHERE parent.run_timestamp < %s
                ORDER BY parent.run_timestamp, child.id
                LIMIT %s
            )
        """
        params = (cutoff, batch_size)
    elif timeframe is None:
        sql = f"""
            DELETE FROM {table}
            WHERE id IN (
                SELECT id FROM {table}
                WHERE processed_at < %s
                ORDER BY processed_at, id
                LIMIT %s
            )
        """
        params = (cutoff, batch_size)
    else:
        sql = f"""
            DELETE FROM {table}
            WHERE id IN (
                SELECT id FROM {table}
                WHERE processed_at < %s AND timeframe = %s
                ORDER BY processed_at, id
                LIMIT %s
            )
        """
        params = (cutoff, timeframe, batch_size)
    cursor = connection.cursor()
    cursor.execute(sql, params)
    return max(0, int(cursor.rowcount))


def cleanup_phase5_retention(
    connection: Any,
    now: datetime,
    *,
    settings: Settings,
    batch_size: int = 1_000,
    max_batches_per_target: int = 100,
) -> dict[str, int]:
    """Delete only Phase 5 rows in finite indexed batches."""
    if batch_size <= 0 or max_batches_per_target <= 0:
        raise ValueError("retention batch limits must be positive")
    now = ensure_utc(now)
    totals = {table: 0 for table in (*_CONTEXT_TABLES, _ENRICHMENT_TABLE)}
    for table in _CONTEXT_TABLES:
        for timeframe in _TIMEFRAMES[table]:
            retention_days = settings.phase5_context_retention_days[timeframe]
            cutoff = now - timedelta(days=retention_days)
            for _ in range(max_batches_per_target):
                deleted = _delete_batch(connection, table, cutoff, batch_size, timeframe)
                totals[table] += deleted
                if deleted == 0:
                    break
    cutoff = now - timedelta(days=settings.phase5_enrichment_retention_days)
    for _ in range(max_batches_per_target):
        deleted = _delete_batch(connection, _ENRICHMENT_TABLE, cutoff, batch_size, None)
        totals[_ENRICHMENT_TABLE] += deleted
        if deleted == 0:
            break
    if hasattr(connection, "commit"):
        connection.commit()
    return totals
