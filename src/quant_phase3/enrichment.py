"""Context-only flow enrichment at the Phase 1/Phase 3 boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping

from quant_phase1.stage1 import Stage1Result

from .contracts import FlowStatus


@dataclass(frozen=True, slots=True)
class Stage1FlowEnrichment:
    phase1_result: Stage1Result
    symbol: str
    canonical_symbol: str | None
    flow_status: FlowStatus
    flow_context: Mapping[str, Any]
    processed_at: datetime
    reason: str
    context_only: bool = True

    @property
    def classification(self) -> str:
        return self.phase1_result.classification


def _status_for(windows: list[Any]) -> tuple[FlowStatus, str]:
    if not windows:
        return FlowStatus.NOT_AVAILABLE, "FLOW_NOT_AVAILABLE"
    for status in (FlowStatus.ERROR, FlowStatus.PARTIAL, FlowStatus.STALE):
        for row in windows:
            if row.status is status or row.freshness is status:
                return status, row.status_reason or status.value
    return FlowStatus.AVAILABLE, "FLOW_AVAILABLE"


def enrich_stage1_flow(
    result: Stage1Result,
    *,
    canonical_symbol: str | None,
    flow_windows: Iterable[Any],
    cvd_points: Iterable[Any],
    cross_exchange_snapshot: Any | None,
    processed_at: datetime,
) -> Stage1FlowEnrichment:
    if processed_at.tzinfo is None or processed_at.utcoffset() is None:
        raise ValueError("processed_at must be timezone-aware UTC")
    windows = list(flow_windows)
    status, reason = _status_for(windows)
    context: dict[str, Any] = {"flow_status": status.value, "flow_reason": reason}
    for window in windows:
        if window.delta_base is not None:
            context[f"delta_{window.timeframe}"] = window.delta_base
        if window.delta_ratio is not None:
            context[f"delta_ratio_{window.timeframe}"] = window.delta_ratio
    for point in cvd_points:
        context[f"cvd_{point.timeframe}"] = point.value if point.status is FlowStatus.AVAILABLE else None
        context[f"cvd_{point.timeframe}_status"] = point.status.value
    if cross_exchange_snapshot is not None:
        context.update(
            {
                "cross_exchange_delta_ratio": cross_exchange_snapshot.directional_delta_ratio,
                "cross_exchange_directional_status": cross_exchange_snapshot.directional_status,
                "volume_exchange_count": cross_exchange_snapshot.volume_exchange_count,
                "directional_exchange_count": cross_exchange_snapshot.directional_exchange_count,
            }
        )
    return Stage1FlowEnrichment(
        phase1_result=result,
        symbol=result.symbol,
        canonical_symbol=canonical_symbol,
        flow_status=status,
        flow_context=context,
        processed_at=processed_at.astimezone(timezone.utc),
        reason=reason,
    )
