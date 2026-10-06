from dataclasses import fields, replace
from datetime import datetime, timezone
from decimal import Decimal

from quant_phase1.config import Settings
from quant_phase1.contracts import DataStatus
from quant_phase1.stage1 import Stage1Result
from quant_phase5.contracts import ContextStatus, DirectionState, MarketLeaderContext, SectorContextSnapshot, SectorMembership
from quant_phase5.enrichment import enrich_stage1_context


NOW = datetime(2026, 9, 22, 1, 0, tzinfo=timezone.utc)


def stage1() -> Stage1Result:
    return Stage1Result(
        symbol="BTCUSDT", category="B", reason="WAIT_FOR_TRIGGER", status=DataStatus.AVAILABLE,
        inputs_used=("price", "closed_5m"), indicators={"ema_fast": Decimal("1")},
        structure="BULLISH", reason_codes=("DIRECTIONAL_BIAS",), key_metrics={"spread_ratio": Decimal("0.001")},
        timestamp=NOW,
    )


def leader(status: ContextStatus = ContextStatus.AVAILABLE):
    unavailable = status is not ContextStatus.AVAILABLE
    return MarketLeaderContext(
        symbol="BTCUSDT", timeframe="15m", context_timestamp=NOW,
        input_window_start=NOW, input_window_end=NOW,
        return_pct=None if unavailable else Decimal("1.0"),
        trend_state="NOT_AVAILABLE" if unavailable else DirectionState.TREND_UP,
        structure_state="NOT_AVAILABLE" if unavailable else "HIGHER_HIGH_HIGHER_LOW",
        volatility_state="NOT_AVAILABLE" if unavailable else "NORMAL",
        volume_state="NOT_AVAILABLE" if unavailable else "NORMAL",
        volatility_value=None if unavailable else Decimal("0.001"),
        volume_ratio=None if unavailable else Decimal("1"),
        freshness_status=status, data_quality={"test": True},
        source_count=1 if not unavailable else 0, missing_count=0 if not unavailable else 1,
        status=status, processed_at=NOW,
        reason_code="MISSING_INPUT" if unavailable else None,
        missing_evidence=("missing",) if unavailable else (),
    )


def sector_context(status: ContextStatus = ContextStatus.AVAILABLE):
    unavailable = status is not ContextStatus.AVAILABLE
    return SectorContextSnapshot(
        sector="MAJOR_CRYPTO", timeframe="15m", context_timestamp=NOW, universe_run_id=7,
        mapping_version="phase5-v1", sector_return_pct=None if unavailable else Decimal("0.5"),
        sector_positive_ratio=None if unavailable else Decimal("0.6"), member_count=5, sample_size=5,
        available_count=0 if unavailable else 5, missing_count=5 if unavailable else 0,
        coverage_ratio=Decimal("0") if unavailable else Decimal("1"),
        status=status, processed_at=NOW, missing_evidence=("missing",) if unavailable else (),
        reason_code="MISSING_INPUT" if unavailable else None,
    )


def membership(sector: str = "MAJOR_CRYPTO", status: str = "AVAILABLE"):
    return SectorMembership(
        mapping_version="phase5-v1", symbol="BTCUSDT", sector=sector,
        source_reference="static", effective_from=NOW, effective_to=None,
        status=status, processed_at=NOW,
    )


def test_enrichment_is_available_and_candidate_vs_sector_is_context_only():
    original = stage1()
    row = enrich_stage1_context(
        original, screening_run_id=10, leader_context=leader(), regime_context=None,
        relative_strength=None, sector_context=sector_context(), sector_membership=membership(),
        timeframe="15m", processed_at=NOW, settings=Settings.from_env({}),
    )
    assert original == stage1()
    assert row.screening_run_id == 10
    assert row.candidate_return_pct == Decimal("1.0")
    assert row.sector_return_pct == Decimal("0.5")
    assert row.candidate_vs_sector_pct == Decimal("0.5")
    assert row.sector_relation.value == "OUTPERFORMING"
    assert row.context_only is True
    assert row.universe_run_id == 7


def test_phase5_enabled_and_disabled_paths_preserve_every_stage1_field():
    original = stage1()
    before = tuple(getattr(original, field.name) for field in fields(Stage1Result))
    row = enrich_stage1_context(
        original, screening_run_id=10, leader_context=leader(), regime_context=None,
        relative_strength=None, sector_context=sector_context(), sector_membership=membership(),
        timeframe="15m", processed_at=NOW, settings=Settings.from_env({"PHASE5_ENABLED": "1"}),
    )
    after = tuple(getattr(original, field.name) for field in fields(Stage1Result))
    assert before == after
    assert original == stage1()
    assert row.context_only is True


def test_missing_leader_or_unknown_sector_never_changes_stage1_result():
    original = stage1()
    missing = enrich_stage1_context(
        original, screening_run_id=10, leader_context=leader(ContextStatus.NOT_AVAILABLE),
        regime_context=None, relative_strength=None, sector_context=sector_context(),
        sector_membership=membership(), timeframe="15m", processed_at=NOW, settings=Settings.from_env({}),
    )
    unknown = enrich_stage1_context(
        original, screening_run_id=10, leader_context=leader(), regime_context=None,
        relative_strength=None, sector_context=None, sector_membership=membership("UNKNOWN", "NOT_AVAILABLE"),
        timeframe="15m", processed_at=NOW, settings=Settings.from_env({}),
    )
    assert original == stage1()
    assert missing.sector_relation.value == "NOT_AVAILABLE"
    assert unknown.sector == "UNKNOWN"
    assert unknown.sector_relation.value == "NOT_AVAILABLE"
    assert missing.context_status is ContextStatus.PARTIAL
    assert unknown.context_status is ContextStatus.PARTIAL


def test_stale_context_propagates_without_demoting_stage1():
    original = stage1()
    row = enrich_stage1_context(
        original, screening_run_id=10, leader_context=leader(ContextStatus.STALE),
        regime_context=None, relative_strength=None, sector_context=sector_context(),
        sector_membership=membership(), timeframe="15m", processed_at=NOW, settings=Settings.from_env({}),
    )
    assert row.context_status is ContextStatus.STALE
    assert row.sector_relation.value == "NOT_AVAILABLE"
    assert original.category == "B"


def test_missing_contexts_are_partial_and_mismatched_context_identity_is_rejected():
    original = stage1()
    missing = enrich_stage1_context(
        original, screening_run_id=10, leader_context=None, regime_context=None,
        relative_strength=None, sector_context=sector_context(), sector_membership=membership(),
        timeframe="15m", processed_at=NOW, settings=Settings.from_env({}),
    )
    assert missing.context_status is ContextStatus.PARTIAL
    assert missing.sector_relation.value == "NOT_AVAILABLE"

    mismatched = sector_context()
    mismatched = replace(mismatched, context_timestamp=NOW.replace(minute=2))
    try:
        enrich_stage1_context(
            original, screening_run_id=10, leader_context=leader(), regime_context=None,
            relative_strength=None, sector_context=mismatched, sector_membership=membership(),
            timeframe="15m", processed_at=NOW, settings=Settings.from_env({}),
        )
    except ValueError as exc:
        assert "timeframe/context timestamp" in str(exc)
    else:
        raise AssertionError("mismatched context must be rejected")
