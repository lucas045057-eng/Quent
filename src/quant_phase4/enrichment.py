"""Context-only enrichment at the Phase 1 / Phase 4 boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any

from quant_phase1.stage1 import Stage1Result

from .contracts import DataStatus
from .cross_exchange import CrossExchangePhase4Context


class Phase4EnrichmentState(StrEnum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class Stage1Phase4Enrichment:
    phase1_result: Stage1Result
    symbol: str
    canonical_symbol: str | None
    state: Phase4EnrichmentState
    phase4_context: CrossExchangePhase4Context
    processed_at: datetime
    reason: str
    context_only: bool = True

    @property
    def classification(self) -> str:
        return self.phase1_result.classification

    @property
    def phase4_status(self) -> Phase4EnrichmentState:
        return self.state


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("processed_at must be UTC-aware")
    return value.astimezone(timezone.utc)


def enrich_stage1_phase4(
    result: Stage1Result,
    context: CrossExchangePhase4Context,
    processed_at: datetime,
) -> Stage1Phase4Enrichment:
    processed_at = _utc(processed_at)
    statuses = context.statuses
    available = sum(status is DataStatus.AVAILABLE for status in statuses)
    if available == len(statuses):
        state, reason = Phase4EnrichmentState.AVAILABLE, "PHASE4_AVAILABLE"
    elif available:
        state, reason = Phase4EnrichmentState.PARTIAL, "PHASE4_PARTIAL"
    else:
        state, reason = Phase4EnrichmentState.UNAVAILABLE, "PHASE4_NOT_AVAILABLE"
    return Stage1Phase4Enrichment(
        phase1_result=result, symbol=result.symbol, canonical_symbol=context.canonical_symbol,
        state=state, phase4_context=context, processed_at=processed_at, reason=reason,
    )
