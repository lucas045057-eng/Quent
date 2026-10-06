from __future__ import annotations

import pytest

from quant_data_layer.admission import ReplayClass, make_work_request
from quant_data_layer.backpressure import (
    BackpressureAction,
    StreamContract,
    STREAM_CONTRACTS,
    build_stream_registry,
    decide_overload,
    get_stream_contract,
)
from quant_data_layer.observability import DataState, ProcessRole, SourceId, SourcePhase, WorkClass


EXPECTED_STREAMS = {
    "system.health_events",
    "system.health_latest",
    "phase1.instruments",
    "phase1.closed_klines",
    "phase1.ticker_latest",
    "phase1.local_indicators",
    "phase1.market_structure",
    "phase1.universe",
    "phase1.stage1_results",
    "phase2.open_interest",
    "phase2.funding_rates",
    "phase2.long_short",
    "phase2.cross_exchange_derivatives",
    "phase3.bitget_trades",
    "phase3.bybit_trades",
    "phase3.hyperliquid_trades",
    "phase3.flow_windows",
    "phase3.trade_gap_events",
    "phase3.cvd_snapshots",
    "phase3.cross_exchange_flow",
    "phase3.stage1_enrichment",
    "phase4.liquidation_events",
    "phase4.liquidation_windows",
    "phase4.long_short_basis",
    "phase4.cross_exchange_snapshots",
    "phase4.stage1_enrichment",
    "phase5.market_regime",
    "phase5.relative_strength",
    "phase5.sector_context",
    "phase5.market_leader_context",
    "phase5.stage1_enrichment",
    "phase6.news_feed",
    "phase6.macro_feed",
    "phase6.unlock_feed",
    "phase6.ai_analyses",
    "phase6.ai_extractions",
    "phase6.ai_usage",
    "phase7.bitcoin_blocks",
    "phase7.ethereum_blocks",
    "phase7.spot_trades",
    "phase7.onchain_events",
    "phase7.onchain_context",
    "phase7.spot_context",
    "phase7.stage1_context",
    "phase8.option_instruments",
    "phase8.option_lifecycle",
    "phase8.markprice_latest",
    "phase8.bounded_ticker_state",
    "phase8.chain_summary",
    "phase8.options_context",
}


def test_phase_one_through_eight_stream_inventory_is_complete_unique_and_classified():
    registry = build_stream_registry(STREAM_CONTRACTS)

    assert set(registry) == EXPECTED_STREAMS
    assert len(registry) == len(STREAM_CONTRACTS)
    legacy_phases = {phase for phase in SourcePhase if phase is not SourcePhase.PHASE9}
    assert {contract.phase for contract in registry.values() if contract.phase is not None} == legacy_phases
    assert all(contract.replay_class in ReplayClass for contract in registry.values())
    assert all(contract.overload_action in BackpressureAction for contract in registry.values())


def test_duplicate_stream_declaration_is_rejected_instead_of_last_write_wins():
    duplicate = STREAM_CONTRACTS[0]

    with pytest.raises(ValueError, match="duplicate stream_id"):
        build_stream_registry((*STREAM_CONTRACTS, duplicate))


@pytest.mark.parametrize(
    ("stream_id", "replay_class", "action"),
    [
        ("phase3.bitget_trades", ReplayClass.CANONICAL_UNRECOVERABLE, BackpressureAction.RECORD_GAP_AND_MARK_PARTIAL),
        ("phase1.closed_klines", ReplayClass.RECOVERABLE_REPLAYABLE, BackpressureAction.DEFER_TO_DURABLE_CURSOR),
        ("phase8.options_context", ReplayClass.DERIVED_REPLACEABLE, BackpressureAction.COALESCE_OR_SKIP_WITH_FRESHNESS),
    ],
)
def test_overload_decision_obeys_a_b_c_contract(stream_id, replay_class, action):
    contract = get_stream_contract(stream_id)
    decision = decide_overload(stream_id)

    assert contract.replay_class is replay_class
    assert contract.overload_action is action
    assert decision.replay_class is replay_class
    assert decision.action is action
    assert decision.cursor_advance_allowed is False
    assert decision.freshness_reset_allowed is False


def test_unknown_stream_defaults_conservatively_to_a_and_is_not_registered():
    contract = get_stream_contract("unreviewed.future.stream")
    decision = decide_overload("unreviewed.future.stream")

    assert contract.registered is False
    assert contract.replay_class is ReplayClass.CANONICAL_UNRECOVERABLE
    assert contract.overload_action is BackpressureAction.RECORD_GAP_AND_MARK_PARTIAL
    assert decision.gap_required is True
    assert decision.status is DataState.PARTIAL


def test_a_overload_records_gap_and_never_advances_cursor():
    decision = decide_overload("phase3.bitget_trades")

    assert decision.gap_required is True
    assert decision.replay_required is False
    assert decision.status is DataState.PARTIAL
    assert decision.reason == "CANONICAL_INPUT_GAP"
    assert decision.preserve_previous_value is False


def test_b_overload_defers_to_durable_cursor_without_cursor_advance():
    decision = decide_overload("phase7.bitcoin_blocks")

    assert decision.gap_required is False
    assert decision.replay_required is True
    assert decision.cursor_advance_allowed is False
    assert decision.status is DataState.PARTIAL
    assert decision.reason == "DURABLE_REPLAY_PENDING"


def test_c_overload_skips_or_coalesces_without_refreshing_stale_data():
    fresh = decide_overload("phase8.options_context", data_age_seconds=30, max_age_seconds=60)
    stale = decide_overload("phase8.options_context", data_age_seconds=90, max_age_seconds=60)

    assert fresh.status is DataState.PARTIAL
    assert stale.status is DataState.STALE
    assert fresh.preserve_previous_value is True
    assert stale.preserve_previous_value is True
    assert fresh.freshness_reset_allowed is False
    assert fresh.reason == stale.reason == "REPLACEABLE_CYCLE_SKIPPED"
    assert fresh.gap_required is False
    assert fresh.replay_required is False


@pytest.mark.parametrize(
    ("data_age_seconds", "max_age_seconds"),
    [(-1, 10), (float("nan"), 10), (1, 0), (1, float("inf"))],
)
def test_replaceable_age_inputs_must_be_finite_and_non_negative(data_age_seconds, max_age_seconds):
    with pytest.raises(ValueError):
        decide_overload(
            "phase8.options_context",
            data_age_seconds=data_age_seconds,
            max_age_seconds=max_age_seconds,
        )


def test_contract_requires_a_registered_phase_and_stream_identifier():
    with pytest.raises((TypeError, ValueError)):
        StreamContract(
            stream_id="",
            phase=SourcePhase.PHASE1,
            source_id=None,
            replay_class=ReplayClass.CANONICAL_UNRECOVERABLE,
            overload_action=BackpressureAction.RECORD_GAP_AND_MARK_PARTIAL,
            description="invalid contract",
        )


def test_work_admission_request_must_match_registered_stream_replay_class():
    common = {
        "phase": SourcePhase.PHASE8,
        "source_id": SourceId.PHASE8_OPTIONS_MARKET,
        "work_class": WorkClass.HEAVY,
        "estimated_items": 1,
        "estimated_bytes": 1024,
        "cancellation_owner": ProcessRole.COLLECTOR,
        "stream_id": "phase8.chain_summary",
    }

    with pytest.raises(ValueError, match="replay_class does not match stream registry"):
        make_work_request(**common, replay_class=ReplayClass.RECOVERABLE_REPLAYABLE)

    request = make_work_request(**common, replay_class=ReplayClass.DERIVED_REPLACEABLE)
    assert request.stream_id == "phase8.chain_summary"


def test_unknown_work_admission_stream_is_conservative_a():
    request = make_work_request(
        phase=SourcePhase.PHASE8,
        source_id=SourceId.PHASE8_OPTIONS_CONTEXT,
        work_class=WorkClass.HEAVY,
        estimated_items=1,
        estimated_bytes=1024,
        replay_class=ReplayClass.CANONICAL_UNRECOVERABLE,
        cancellation_owner=ProcessRole.COLLECTOR,
        stream_id="phase8.unreviewed_new_output",
    )
    assert request.replay_class is ReplayClass.CANONICAL_UNRECOVERABLE
