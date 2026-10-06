"""Shared A/B/C replayability and overload contract for Phase 1–8 streams.

This module classifies outputs; it does not own queues, drop data, advance
cursors, synthesize values, or infer that an unmeasured transport buffer is
empty. Callers remain responsible for executing and persisting the returned
overload action.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
import re
from types import MappingProxyType
from typing import Iterable, Mapping

from .admission import ReplayClass
from .observability import DataState, SourceId, SourcePhase


class BackpressureAction(StrEnum):
    """Stable action tokens paired one-to-one with the replayability classes."""

    RECORD_GAP_AND_MARK_PARTIAL = "RECORD_GAP_AND_MARK_PARTIAL"
    DEFER_TO_DURABLE_CURSOR = "DEFER_TO_DURABLE_CURSOR"
    COALESCE_OR_SKIP_WITH_FRESHNESS = "COALESCE_OR_SKIP_WITH_FRESHNESS"


_ACTION_BY_CLASS = {
    ReplayClass.CANONICAL_UNRECOVERABLE: BackpressureAction.RECORD_GAP_AND_MARK_PARTIAL,
    ReplayClass.RECOVERABLE_REPLAYABLE: BackpressureAction.DEFER_TO_DURABLE_CURSOR,
    ReplayClass.DERIVED_REPLACEABLE: BackpressureAction.COALESCE_OR_SKIP_WITH_FRESHNESS,
}
_SAFE_STREAM_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")


@dataclass(frozen=True, slots=True)
class StreamContract:
    stream_id: str
    phase: SourcePhase | None
    source_id: SourceId | None
    replay_class: ReplayClass
    overload_action: BackpressureAction
    description: str
    registered: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.stream_id, str) or _SAFE_STREAM_ID.fullmatch(self.stream_id) is None:
            raise ValueError("stream_id must be a bounded lowercase identifier")
        if (self.phase is None) != (self.source_id is None):
            raise ValueError("phase and source_id must both be set or both be absent")
        if self.phase is not None and not isinstance(self.phase, SourcePhase):
            raise TypeError("phase must be a registered SourcePhase")
        if self.source_id is not None and not isinstance(self.source_id, SourceId):
            raise TypeError("source_id must be a registered SourceId")
        if not isinstance(self.replay_class, ReplayClass):
            raise TypeError("replay_class must be a registered ReplayClass")
        if not isinstance(self.overload_action, BackpressureAction):
            raise TypeError("overload_action must be a registered BackpressureAction")
        if self.overload_action is not _ACTION_BY_CLASS[self.replay_class]:
            raise ValueError("overload_action does not match the declared replay class")
        if not isinstance(self.description, str) or not self.description.strip() or len(self.description) > 240:
            raise ValueError("description must be a non-empty bounded explanation")
        if not isinstance(self.registered, bool):
            raise TypeError("registered must be bool")


@dataclass(frozen=True, slots=True)
class BackpressureDecision:
    stream_id: str
    replay_class: ReplayClass
    action: BackpressureAction
    status: DataState
    reason: str
    gap_required: bool
    replay_required: bool
    cursor_advance_allowed: bool = False
    freshness_reset_allowed: bool = False
    preserve_previous_value: bool = False


def _entry(
    stream_id: str,
    phase: SourcePhase,
    source_id: SourceId,
    replay_class: ReplayClass,
    description: str,
) -> StreamContract:
    return StreamContract(
        stream_id=stream_id,
        phase=phase,
        source_id=source_id,
        replay_class=replay_class,
        overload_action=_ACTION_BY_CLASS[replay_class],
        description=description,
    )


_A = ReplayClass.CANONICAL_UNRECOVERABLE
_B = ReplayClass.RECOVERABLE_REPLAYABLE
_C = ReplayClass.DERIVED_REPLACEABLE

# Stream-level inventory, not phase-level shorthand. Unknown streams are A
# until a bounded authoritative replay contract is demonstrated and registered.
STREAM_CONTRACTS: tuple[StreamContract, ...] = (
    StreamContract("system.health_events", None, None, _A, _ACTION_BY_CLASS[_A], "Append-only runtime health evidence."),
    StreamContract("system.health_latest", None, None, _C, _ACTION_BY_CLASS[_C], "Replaceable latest health snapshot."),
    _entry("phase1.instruments", SourcePhase.PHASE1, SourceId.PHASE1_MARKET_DATA, _B, "Instrument catalog can be reconciled from the source."),
    _entry("phase1.closed_klines", SourcePhase.PHASE1, SourceId.PHASE1_MARKET_DATA, _B, "Closed bars recover from the durable time cursor."),
    _entry("phase1.ticker_latest", SourcePhase.PHASE1, SourceId.PHASE1_MARKET_DATA, _C, "Latest ticker state is replaceable."),
    _entry("phase1.local_indicators", SourcePhase.PHASE1, SourceId.PHASE1_MARKET_DATA, _C, "Indicators recompute from retained canonical market data."),
    _entry("phase1.market_structure", SourcePhase.PHASE1, SourceId.PHASE1_MARKET_DATA, _C, "Market structure is recomputable context."),
    _entry("phase1.universe", SourcePhase.PHASE1, SourceId.PHASE1_MARKET_DATA, _C, "Universe is replaceable screening state."),
    _entry("phase1.stage1_results", SourcePhase.PHASE1, SourceId.ENGINE_STAGE1, _C, "Stage 1 results are replaceable derived output."),
    _entry("phase2.open_interest", SourcePhase.PHASE2, SourceId.PHASE2_DERIVATIVES, _C, "Open-interest snapshots are replaceable observations."),
    _entry("phase2.funding_rates", SourcePhase.PHASE2, SourceId.PHASE2_DERIVATIVES, _C, "Funding snapshots are replaceable observations."),
    _entry("phase2.long_short", SourcePhase.PHASE2, SourceId.PHASE2_DERIVATIVES, _C, "Long/short snapshots are replaceable observations."),
    _entry("phase2.cross_exchange_derivatives", SourcePhase.PHASE2, SourceId.PHASE2_DERIVATIVES, _C, "Cross-exchange derivative context is recomputable."),
    _entry("phase3.bitget_trades", SourcePhase.PHASE3, SourceId.PHASE3_BITGET_TRADES, _A, "No complete bounded replay is assumed for trade prints."),
    _entry("phase3.bybit_trades", SourcePhase.PHASE3, SourceId.PHASE3_BYBIT_TRADES, _A, "No complete bounded replay is assumed for trade prints."),
    _entry("phase3.hyperliquid_trades", SourcePhase.PHASE3, SourceId.PHASE3_HYPERLIQUID_TRADES, _A, "No complete bounded replay is assumed for trade prints."),
    _entry("phase3.flow_windows", SourcePhase.PHASE3, SourceId.PHASE3_FLOW_PROCESSING, _A, "Flow completeness inherits unrecoverable trade-print gaps."),
    _entry("phase3.trade_gap_events", SourcePhase.PHASE3, SourceId.PHASE3_FLOW_PROCESSING, _A, "Gap events are append-only completeness evidence."),
    _entry("phase3.cvd_snapshots", SourcePhase.PHASE3, SourceId.PHASE3_FLOW_PROCESSING, _C, "CVD is recomputable from persisted flow windows and retains their quality state."),
    _entry("phase3.cross_exchange_flow", SourcePhase.PHASE3, SourceId.PHASE3_FLOW_PROCESSING, _C, "Cross-exchange flow is replaceable context over quality-tagged windows."),
    _entry("phase3.stage1_enrichment", SourcePhase.PHASE3, SourceId.PHASE3_FLOW_PROCESSING, _C, "Stage 1 flow enrichment is replaceable and context-only."),
    _entry("phase4.liquidation_events", SourcePhase.PHASE4, SourceId.PHASE4_LIQUIDATION, _A, "Venue-complete liquidation replay is not assumed."),
    _entry("phase4.liquidation_windows", SourcePhase.PHASE4, SourceId.PHASE4_LIQUIDATION, _C, "Windows can be rebuilt from persisted events and keep gap status."),
    _entry("phase4.long_short_basis", SourcePhase.PHASE4, SourceId.PHASE4_LIQUIDATION, _C, "Long/short and basis snapshots are replaceable."),
    _entry("phase4.cross_exchange_snapshots", SourcePhase.PHASE4, SourceId.PHASE4_LIQUIDATION, _C, "Cross-exchange rollups are recomputable snapshots."),
    _entry("phase4.stage1_enrichment", SourcePhase.PHASE4, SourceId.PHASE4_LIQUIDATION, _C, "Stage 1 liquidation enrichment is replaceable context."),
    _entry("phase5.market_regime", SourcePhase.PHASE5, SourceId.PHASE5_CONTEXT, _C, "Market regime is derived context."),
    _entry("phase5.relative_strength", SourcePhase.PHASE5, SourceId.PHASE5_CONTEXT, _C, "Relative strength is recomputable context."),
    _entry("phase5.sector_context", SourcePhase.PHASE5, SourceId.PHASE5_CONTEXT, _C, "Sector context is recomputable."),
    _entry("phase5.market_leader_context", SourcePhase.PHASE5, SourceId.PHASE5_CONTEXT, _C, "Market leader context is replaceable."),
    _entry("phase5.stage1_enrichment", SourcePhase.PHASE5, SourceId.PHASE5_CONTEXT, _C, "Stage 1 Phase 5 enrichment is replaceable context."),
    _entry("phase6.news_feed", SourcePhase.PHASE6, SourceId.PHASE6_EXTERNAL_CONTEXT, _A, "No source-wide bounded news replay contract is assumed."),
    _entry("phase6.macro_feed", SourcePhase.PHASE6, SourceId.PHASE6_EXTERNAL_CONTEXT, _A, "No source-wide bounded macro replay contract is assumed."),
    _entry("phase6.unlock_feed", SourcePhase.PHASE6, SourceId.PHASE6_EXTERNAL_CONTEXT, _A, "No source-wide bounded unlock replay contract is assumed."),
    _entry("phase6.ai_analyses", SourcePhase.PHASE6, SourceId.PHASE6_AI, _C, "AI analyses are derived and replaceable."),
    _entry("phase6.ai_extractions", SourcePhase.PHASE6, SourceId.PHASE6_AI, _C, "AI extractions are derived and replaceable."),
    _entry("phase6.ai_usage", SourcePhase.PHASE6, SourceId.PHASE6_AI, _A, "Usage/accounting rows are append-only evidence."),
    _entry("phase7.bitcoin_blocks", SourcePhase.PHASE7, SourceId.PHASE7_ONCHAIN, _B, "Bitcoin blocks resume from the durable chain cursor."),
    _entry("phase7.ethereum_blocks", SourcePhase.PHASE7, SourceId.PHASE7_ONCHAIN, _B, "Ethereum blocks resume from the durable chain cursor."),
    _entry("phase7.spot_trades", SourcePhase.PHASE7, SourceId.PHASE7_SPOT, _B, "Spot events resume from the durable exchange cursor."),
    _entry("phase7.onchain_events", SourcePhase.PHASE7, SourceId.PHASE7_ONCHAIN, _B, "On-chain events are reconstructed from block/log cursors."),
    _entry("phase7.onchain_context", SourcePhase.PHASE7, SourceId.PHASE7_ONCHAIN, _C, "On-chain flow and enrichment are replaceable derived context."),
    _entry("phase7.spot_context", SourcePhase.PHASE7, SourceId.PHASE7_SPOT, _C, "Spot flow context is replaceable over cursor-backed events."),
    _entry("phase7.stage1_context", SourcePhase.PHASE7, SourceId.PHASE7_ONCHAIN, _C, "Stage 1 chain context is replaceable."),
    _entry("phase8.option_instruments", SourcePhase.PHASE8, SourceId.PHASE8_OPTIONS_MARKET, _B, "Instrument catalog is restored by bounded full reconciliation."),
    _entry("phase8.option_lifecycle", SourcePhase.PHASE8, SourceId.PHASE8_OPTIONS_MARKET, _B, "Lifecycle resumes through bounded reconciliation."),
    _entry("phase8.markprice_latest", SourcePhase.PHASE8, SourceId.PHASE8_OPTIONS_MARKET, _C, "Latest mark and IV source state is replaceable and reseeded after gaps."),
    _entry("phase8.bounded_ticker_state", SourcePhase.PHASE8, SourceId.PHASE8_OPTIONS_MARKET, _C, "Bounded per-instrument ticker state is replaceable and reseeded."),
    _entry("phase8.chain_summary", SourcePhase.PHASE8, SourceId.PHASE8_OPTIONS_MARKET, _C, "Periodic chain summary is replaceable by REST reconciliation."),
    _entry("phase8.options_context", SourcePhase.PHASE8, SourceId.PHASE8_OPTIONS_CONTEXT, _C, "Options contexts are recomputable with source timestamps and coverage."),

)


def build_stream_registry(contracts: Iterable[StreamContract]) -> Mapping[str, StreamContract]:
    registry: dict[str, StreamContract] = {}
    for contract in contracts:
        if not isinstance(contract, StreamContract) or not contract.registered:
            raise TypeError("registry entries must be registered StreamContract values")
        if contract.stream_id in registry:
            raise ValueError(f"duplicate stream_id: {contract.stream_id}")
        registry[contract.stream_id] = contract
    return MappingProxyType(registry)


PHASE9_EVALUATIONS_CONTRACT = _entry(
    "phase9.evaluations", SourcePhase.PHASE9, SourceId.PHASE9_EVALUATIONS,
    _B, "Durable Phase 9 evaluations resume from the outbox and evaluation ledger.",
)
STREAM_REGISTRY = build_stream_registry((*STREAM_CONTRACTS, PHASE9_EVALUATIONS_CONTRACT))


def get_stream_contract(stream_id: str) -> StreamContract:
    if not isinstance(stream_id, str) or _SAFE_STREAM_ID.fullmatch(stream_id) is None:
        raise ValueError("stream_id must be a bounded lowercase identifier")
    registered = STREAM_REGISTRY.get(stream_id)
    if registered is not None:
        return registered
    return StreamContract(
        stream_id=stream_id,
        phase=None,
        source_id=None,
        replay_class=_A,
        overload_action=_ACTION_BY_CLASS[_A],
        description="Unregistered stream defaults conservatively to canonical-unrecoverable.",
        registered=False,
    )


def decide_overload(
    stream_id: str,
    *,
    data_age_seconds: float | None = None,
    max_age_seconds: float | None = None,
) -> BackpressureDecision:
    contract = get_stream_contract(stream_id)
    if (data_age_seconds is None) != (max_age_seconds is None):
        raise ValueError("data_age_seconds and max_age_seconds must be supplied together")
    if data_age_seconds is not None:
        if (
            not isinstance(data_age_seconds, (int, float))
            or isinstance(data_age_seconds, bool)
            or not math.isfinite(data_age_seconds)
            or data_age_seconds < 0
            or not isinstance(max_age_seconds, (int, float))
            or isinstance(max_age_seconds, bool)
            or not math.isfinite(max_age_seconds)
            or max_age_seconds <= 0
        ):
            raise ValueError("replaceable data age must be finite/non-negative and its limit finite/positive")
    if contract.replay_class is _A:
        return BackpressureDecision(
            stream_id, _A, contract.overload_action, DataState.PARTIAL,
            "CANONICAL_INPUT_GAP", True, False,
        )
    if contract.replay_class is _B:
        return BackpressureDecision(
            stream_id, _B, contract.overload_action, DataState.PARTIAL,
            "DURABLE_REPLAY_PENDING", False, True,
        )
    status = (
        DataState.STALE
        if data_age_seconds is not None and data_age_seconds >= max_age_seconds
        else DataState.PARTIAL
    )
    return BackpressureDecision(
        stream_id, _C, contract.overload_action, status,
        "REPLACEABLE_CYCLE_SKIPPED", False, False,
        preserve_previous_value=True,
    )
