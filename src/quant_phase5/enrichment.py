"""Context-only enrichment attached to an existing Stage1 screening result."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from quant_phase1.config import Settings
from quant_phase1.stage1 import Stage1Result
from quant_phase1.time import ensure_utc

from .contracts import (
    ContextStatus,
    MarketLeaderContext,
    MarketRegimeSnapshot,
    ReasonCode,
    RelativeStrengthSnapshot,
    SectorContextSnapshot,
    SectorMembership,
    SectorRelation,
    Stage1Phase5Enrichment,
)


def _status(value: Any) -> ContextStatus:
    if value is None:
        return ContextStatus.NOT_AVAILABLE
    status = getattr(value, "status", getattr(value, "context_status", None))
    if status is None:
        status = getattr(value, "freshness_status", None)
    if status is None:
        return ContextStatus.NOT_AVAILABLE
    return status if isinstance(status, ContextStatus) else ContextStatus(status)


def _ref(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {"source": name, "status": ContextStatus.NOT_AVAILABLE.value}
    status = _status(value)
    result = {"source": name, "status": status.value}
    for field in ("symbol", "benchmark", "sector", "timeframe", "context_timestamp", "universe_run_id", "calculation_version"):
        if hasattr(value, field):
            field_value = getattr(value, field)
            result[field] = field_value.isoformat() if isinstance(field_value, datetime) else field_value
    return result


def enrich_stage1_context(
    stage1_result: Stage1Result,
    *,
    screening_run_id: int,
    leader_context: MarketLeaderContext | None,
    regime_context: MarketRegimeSnapshot | None,
    relative_strength: RelativeStrengthSnapshot | None,
    sector_context: SectorContextSnapshot | None,
    sector_membership: SectorMembership | None,
    timeframe: str,
    processed_at: datetime,
    settings: Settings,
) -> Stage1Phase5Enrichment:
    """Create an additive enrichment row without modifying ``stage1_result``."""
    if not isinstance(stage1_result, Stage1Result):
        raise TypeError("stage1_result must be the canonical Stage1Result")
    if leader_context is not None and (
        leader_context.symbol != stage1_result.symbol or leader_context.timeframe != timeframe
    ):
        raise ValueError("leader context identity does not match Stage1 result")
    if relative_strength is not None and relative_strength.symbol != stage1_result.symbol:
        raise ValueError("relative-strength identity does not match Stage1 result")
    if sector_membership is not None and sector_membership.symbol != stage1_result.symbol:
        raise ValueError("sector membership identity does not match Stage1 result")
    processed_at = ensure_utc(processed_at)
    anchors = [value.context_timestamp for value in (leader_context, regime_context, relative_strength, sector_context) if value is not None]
    anchor_timestamp = anchors[0] if anchors else None
    for value in (leader_context, regime_context, relative_strength, sector_context):
        if value is not None and (
            getattr(value, "timeframe", timeframe) != timeframe
            or (anchor_timestamp is not None and value.context_timestamp != anchor_timestamp)
        ):
            raise ValueError("Phase 5 context timeframe/context timestamp does not match enrichment")
    if sector_membership is not None and anchor_timestamp is not None and not (
        sector_membership.effective_from <= anchor_timestamp
        and (sector_membership.effective_to is None or anchor_timestamp < sector_membership.effective_to)
    ):
        raise ValueError("sector membership effective interval does not cover enrichment")
    if sector_context is not None and sector_membership is not None:
        if sector_context.sector != sector_membership.sector:
            raise ValueError("sector context sector does not match membership")
        if sector_context.mapping_version != sector_membership.mapping_version:
            raise ValueError("sector context mapping version does not match membership")
    universe_ids = {
        int(value.universe_run_id)
        for value in (regime_context, relative_strength, sector_context)
        if value is not None and getattr(value, "universe_run_id", None) is not None
    }
    if len(universe_ids) > 1:
        raise ValueError("universe_run_id does not match enrichment contexts")
    sources = [leader_context, regime_context, relative_strength, sector_context]
    statuses = [_status(source) for source in sources if source is not None]
    if leader_context is None:
        statuses.append(ContextStatus.NOT_AVAILABLE)
    if sector_context is None:
        statuses.append(ContextStatus.NOT_AVAILABLE)
    if sector_membership is None or sector_membership.status.value != "AVAILABLE":
        statuses.append(ContextStatus.NOT_AVAILABLE)
    if ContextStatus.STALE in statuses:
        context_status = ContextStatus.STALE
        reason = ReasonCode.STALE_INPUT
    elif ContextStatus.ERROR in statuses:
        context_status = ContextStatus.NOT_AVAILABLE
        reason = ReasonCode.SOURCE_UNAVAILABLE
    elif not statuses or all(item is ContextStatus.NOT_AVAILABLE for item in statuses):
        context_status = ContextStatus.NOT_AVAILABLE
        reason = ReasonCode.MISSING_INPUT
    elif all(item is ContextStatus.AVAILABLE for item in statuses):
        context_status = ContextStatus.AVAILABLE
        reason = None
    else:
        context_status = ContextStatus.PARTIAL
        reason = None
    leader_available = _status(leader_context) is ContextStatus.AVAILABLE and leader_context is not None
    sector_available = _status(sector_context) in {ContextStatus.AVAILABLE, ContextStatus.PARTIAL} and sector_context is not None
    mapping_available = (
        sector_membership is not None
        and sector_membership.status.value == "AVAILABLE"
        and sector_membership.sector != "UNKNOWN"
    )
    candidate_return = leader_context.return_pct if leader_available else None
    sector_return = sector_context.sector_return_pct if sector_available else None
    candidate_vs_sector = None
    relation = SectorRelation.NOT_AVAILABLE
    missing: list[str] = []
    if candidate_return is None:
        missing.append("candidate_return")
    if sector_return is None:
        missing.append("sector_return")
    if not mapping_available:
        missing.append("sector_mapping")
    if mapping_available and candidate_return is not None and sector_return is not None and context_status not in {ContextStatus.STALE, ContextStatus.NOT_AVAILABLE}:
        candidate_vs_sector = candidate_return - sector_return
        negative, positive = (Decimal(str(value)) for value in settings.phase5_sector_thresholds_pct[timeframe])
        if candidate_vs_sector >= positive:
            relation = SectorRelation.OUTPERFORMING
        elif candidate_vs_sector <= negative:
            relation = SectorRelation.UNDERPERFORMING
        else:
            relation = SectorRelation.IN_LINE
    else:
        missing.append("candidate_vs_sector")
    if context_status in {ContextStatus.STALE, ContextStatus.NOT_AVAILABLE}:
        candidate_return = None
        sector_return = None
        candidate_vs_sector = None
        relation = SectorRelation.NOT_AVAILABLE
    sector = sector_membership.sector if sector_membership is not None else "UNKNOWN"
    mapping_version = sector_membership.mapping_version if sector_membership is not None else None
    universe_run_id = sector_context.universe_run_id if sector_context is not None else None
    return Stage1Phase5Enrichment(
        screening_run_id=screening_run_id,
        symbol=stage1_result.symbol,
        universe_run_id=universe_run_id,
        sector=sector,
        mapping_version=mapping_version,
        candidate_return_pct=candidate_return,
        sector_return_pct=sector_return,
        candidate_vs_sector_pct=candidate_vs_sector,
        sector_relation=relation,
        context_status=context_status,
        leader_context_ref=_ref(leader_context, "leader_context"),
        regime_context_ref=_ref(regime_context, "regime_context"),
        relative_strength_ref=_ref(relative_strength, "relative_strength"),
        sector_context_ref=_ref(sector_context, "sector_context"),
        processed_at=processed_at,
        missing_evidence=tuple(dict.fromkeys(missing)),
        reason_code=reason,
    )
