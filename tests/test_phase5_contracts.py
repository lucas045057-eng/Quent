from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_phase5.contracts import (
    BreadthState,
    ContextStatus,
    MarketBreadthSnapshot,
    MarketLeaderContext,
    MarketRegimeSnapshot,
    MappingStatus,
    ReasonCode,
    RelativeStrengthSnapshot,
    SectorContextSnapshot,
    SectorMembership,
    SectorRelation,
)


UTC_NOW = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)


def test_phase5_context_status_supports_partial_without_trading_vocab():
    assert {item.value for item in ContextStatus} == {
        "AVAILABLE",
        "PARTIAL",
        "STALE",
        "NOT_AVAILABLE",
        "ERROR",
    }
    assert not {item.value for item in SectorRelation} & {"BUY", "SELL", "LONG", "SHORT"}


def test_leader_context_requires_utc_and_rejects_naive_timestamp():
    with pytest.raises(ValueError, match="context_timestamp must be UTC"):
        MarketLeaderContext(
            symbol="BTCUSDT",
            timeframe="5m",
            context_timestamp=datetime(2026, 9, 22),
            input_window_start=UTC_NOW,
            input_window_end=UTC_NOW,
            return_pct=Decimal("0.1"),
            trend_state="TREND_UP",
            structure_state="RANGE",
            volatility_state="NORMAL",
            volume_state="NORMAL",
            volatility_value=Decimal("0.001"),
            volume_ratio=Decimal("1"),
            freshness_status=ContextStatus.AVAILABLE,
            data_quality={"coverage_ratio": "1"},
            source_count=1,
            missing_count=0,
            status=ContextStatus.AVAILABLE,
            processed_at=UTC_NOW,
        )


def test_breadth_counts_missing_members_and_never_converts_missing_to_zero():
    row = MarketBreadthSnapshot(
        timeframe="15m",
        context_timestamp=UTC_NOW,
        universe_run_id=7,
        sample_size=10,
        available_count=8,
        missing_count=2,
        coverage_ratio=Decimal("0.8"),
        positive_ratio=Decimal("0.625"),
        up_structure_ratio=Decimal("0.5"),
        down_structure_ratio=Decimal("0.25"),
        breadth_state=BreadthState.BROAD_STRENGTH,
        status=ContextStatus.PARTIAL,
        processed_at=UTC_NOW,
        missing_evidence=("MISSING_SYMBOL",),
    )
    assert row.missing_count == 2
    assert row.status is ContextStatus.PARTIAL

    with pytest.raises(ValueError, match=r"available_count \+ missing_count"):
        MarketBreadthSnapshot(
            timeframe="15m",
            context_timestamp=UTC_NOW,
            universe_run_id=7,
            sample_size=10,
            available_count=8,
            missing_count=0,
            coverage_ratio=Decimal("0.8"),
            positive_ratio=Decimal("0.625"),
            up_structure_ratio=Decimal("0.5"),
            down_structure_ratio=Decimal("0.25"),
            breadth_state=BreadthState.BROAD_STRENGTH,
            status=ContextStatus.AVAILABLE,
            processed_at=UTC_NOW,
        )


def test_regime_keeps_dimension_statuses_explicit():
    row = MarketRegimeSnapshot(
        timeframe="1H",
        context_timestamp=UTC_NOW,
        universe_run_id=7,
        direction_regime="MIXED",
        volatility_regime="HIGH",
        breadth_regime=BreadthState.NARROW_STRENGTH,
        direction_status=ContextStatus.PARTIAL,
        volatility_status=ContextStatus.AVAILABLE,
        breadth_status=ContextStatus.PARTIAL,
        status=ContextStatus.PARTIAL,
        support_evidence=("btc_trend_up",),
        conflict_evidence=(),
        missing_evidence=("eth_context",),
        processed_at=UTC_NOW,
        sample_size=10,
        available_count=8,
        missing_count=2,
        coverage_ratio=Decimal("0.8"),
    )
    assert row.direction_status is ContextStatus.PARTIAL
    assert row.breadth_status is ContextStatus.PARTIAL
    assert row.status is ContextStatus.PARTIAL


def test_regime_status_must_follow_dimension_precedence():
    with pytest.raises(ValueError, match="precedence"):
        MarketRegimeSnapshot(
            timeframe="1H",
            context_timestamp=UTC_NOW,
            universe_run_id=7,
            direction_regime="MIXED",
            volatility_regime="HIGH",
            breadth_regime=BreadthState.MIXED,
            direction_status=ContextStatus.PARTIAL,
            volatility_status=ContextStatus.AVAILABLE,
            breadth_status=ContextStatus.AVAILABLE,
            status=ContextStatus.AVAILABLE,
            support_evidence=(),
            conflict_evidence=(),
            missing_evidence=(),
            processed_at=UTC_NOW,
            sample_size=10,
            available_count=10,
            missing_count=0,
            coverage_ratio=Decimal("1"),
        )


def test_unavailable_relative_strength_cannot_carry_a_numeric_value():
    with pytest.raises(ValueError, match="NOT_AVAILABLE"):
        RelativeStrengthSnapshot(
            symbol="ETHUSDT",
            benchmark="ETHUSDT",
            timeframe="15m",
            context_timestamp=UTC_NOW,
            candidate_return_pct=Decimal("0"),
            benchmark_return_pct=None,
            relative_return_pct=Decimal("0"),
            relative_class="NOT_AVAILABLE",
            universe_run_id=None,
            sample_size=0,
            available_count=0,
            missing_count=0,
            coverage_ratio=None,
            status=ContextStatus.NOT_AVAILABLE,
            processed_at=UTC_NOW,
            reason_code=ReasonCode.MISSING_INPUT,
        )


def test_relative_strength_requires_exact_coverage_ratio():
    with pytest.raises(ValueError, match="coverage_ratio"):
        RelativeStrengthSnapshot(
            symbol="BTCUSDT",
            benchmark="ETHUSDT",
            timeframe="15m",
            context_timestamp=UTC_NOW,
            candidate_return_pct=Decimal("1"),
            benchmark_return_pct=Decimal("0.5"),
            relative_return_pct=Decimal("0.5"),
            relative_class="STRONG",
            universe_run_id=None,
            sample_size=10,
            available_count=8,
            missing_count=2,
            coverage_ratio=Decimal("1"),
            status=ContextStatus.PARTIAL,
            processed_at=UTC_NOW,
        )


def test_unavailable_breadth_and_sector_cannot_carry_metrics():
    with pytest.raises(ValueError, match="cannot carry calculated values"):
        MarketBreadthSnapshot(
            timeframe="15m",
            context_timestamp=UTC_NOW,
            universe_run_id=None,
            sample_size=0,
            available_count=0,
            missing_count=0,
            coverage_ratio=None,
            positive_ratio=Decimal("0"),
            up_structure_ratio=None,
            down_structure_ratio=None,
            breadth_state=BreadthState.NOT_AVAILABLE,
            status=ContextStatus.NOT_AVAILABLE,
            processed_at=UTC_NOW,
            reason_code=ReasonCode.MISSING_INPUT,
        )
    with pytest.raises(ValueError, match="cannot carry calculated values"):
        SectorContextSnapshot(
            sector="UNKNOWN",
            timeframe="15m",
            context_timestamp=UTC_NOW,
            universe_run_id=None,
            mapping_version="v1",
            sector_return_pct=Decimal("0"),
            sector_positive_ratio=None,
            member_count=0,
            sample_size=0,
            available_count=0,
            missing_count=0,
            coverage_ratio=None,
            status=ContextStatus.NOT_AVAILABLE,
            processed_at=UTC_NOW,
            reason_code=ReasonCode.UNKNOWN_SECTOR,
        )


def test_unknown_sector_is_explicit_and_versioned():
    mapping = SectorMembership(
        mapping_version="v1",
        symbol="UNKNOWNUSDT",
        sector="UNKNOWN",
        source_reference="config/phase5_sector_map.csv",
        effective_from=UTC_NOW,
        effective_to=None,
        status=MappingStatus.AVAILABLE,
        processed_at=UTC_NOW,
    )
    assert mapping.sector == "UNKNOWN"
    assert mapping.mapping_version == "v1"


def test_sector_mapping_does_not_accept_phase5_partial_status():
    with pytest.raises(ValueError, match="invalid status"):
        SectorMembership(
            mapping_version="v1",
            symbol="BTCUSDT",
            sector="UNKNOWN",
            source_reference="config/phase5_sector_map.csv",
            effective_from=UTC_NOW,
            effective_to=None,
            status=ContextStatus.PARTIAL,
            processed_at=UTC_NOW,
        )


def test_evidence_contract_rejects_raw_payload():
    with pytest.raises(ValueError, match="raw payload"):
        MarketLeaderContext(
            symbol="BTCUSDT",
            timeframe="5m",
            context_timestamp=UTC_NOW,
            input_window_start=UTC_NOW,
            input_window_end=UTC_NOW,
            return_pct=Decimal("0.1"),
            trend_state="TREND_UP",
            structure_state="RANGE",
            volatility_state="NORMAL",
            volume_state="NORMAL",
            volatility_value=Decimal("0.001"),
            volume_ratio=Decimal("1"),
            freshness_status=ContextStatus.AVAILABLE,
            data_quality={"raw_payload": {"x": 1}},
            source_count=1,
            missing_count=0,
            status=ContextStatus.AVAILABLE,
            processed_at=UTC_NOW,
        )
