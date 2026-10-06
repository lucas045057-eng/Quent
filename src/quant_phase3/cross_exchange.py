"""Capability-aware cross-exchange flow aggregation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable

from .contracts import FlowStatus
from .flow import TradeFlowWindow


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class CrossExchangeFlowSnapshot:
    canonical_symbol: str
    timeframe: str
    snapshot_timestamp: datetime
    volume_exchange_count: int
    directional_exchange_count: int
    total_volume_base: Decimal | None
    total_notional_usd: Decimal | None
    directional_delta_base: Decimal | None
    directional_delta_ratio: Decimal | None
    status: FlowStatus
    reason: str | None
    directional_status: str
    snapshot: dict[str, Any]
    processed_at: datetime


def build_cross_exchange_flow_snapshot(
    windows: Iterable[TradeFlowWindow],
    *,
    snapshot_timestamp: datetime,
    processed_at: datetime,
    min_directional_sources: int,
) -> CrossExchangeFlowSnapshot:
    if min_directional_sources <= 0:
        raise ValueError("min_directional_sources must be positive")
    rows = list(windows)
    if not rows:
        raise ValueError("at least one flow window is required")
    available = [row for row in rows if row.status is FlowStatus.AVAILABLE and row.freshness is FlowStatus.AVAILABLE]
    directional = [row for row in available if row.delta_base is not None]
    volume_total = sum((row.total_volume_base for row in available), Decimal("0")) if available else None
    notional_total = (
        sum((row.total_notional_usd for row in available), Decimal("0"))
        if available and all(row.total_notional_usd is not None for row in available)
        else None
    )
    delta = sum((row.delta_base for row in directional if row.delta_base is not None), Decimal("0")) if directional else None
    directional_volume = sum((row.total_volume_base for row in directional), Decimal("0")) if directional else None
    ratio = delta / directional_volume if delta is not None and directional_volume else None
    directional_status = "AVAILABLE" if len(directional) >= min_directional_sources else "INSUFFICIENT_DIRECTIONAL_SOURCES"
    reason = None if directional_status == "AVAILABLE" else directional_status
    status = FlowStatus.AVAILABLE if available else FlowStatus.NOT_AVAILABLE
    if not available:
        reason = "NO_FRESH_VOLUME"
    per_exchange = {
        row.exchange: {
            "status": row.status.value,
            "total_volume_base": str(row.total_volume_base),
            "total_notional_usd": str(row.total_notional_usd) if row.total_notional_usd is not None else None,
            "delta_base": str(row.delta_base) if row.delta_base is not None else None,
            "directional": row.delta_base is not None and row.status is FlowStatus.AVAILABLE,
        }
        for row in rows
    }
    return CrossExchangeFlowSnapshot(
        canonical_symbol=rows[0].canonical_symbol,
        timeframe=rows[0].timeframe,
        snapshot_timestamp=_utc(snapshot_timestamp, "snapshot_timestamp"),
        volume_exchange_count=len(available),
        directional_exchange_count=len(directional),
        total_volume_base=volume_total,
        total_notional_usd=notional_total,
        directional_delta_base=delta,
        directional_delta_ratio=ratio,
        status=status,
        reason=reason,
        directional_status=directional_status,
        snapshot=per_exchange,
        processed_at=_utc(processed_at, "processed_at"),
    )
