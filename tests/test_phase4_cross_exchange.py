from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_phase4.contracts import (
    BasisObservation,
    BasisType,
    CanonicalLiquidation,
    CoverageSemantics,
    DataStatus,
    LiquidationSide,
    LongShortMetricType,
    LongShortObservation,
    LongShortPopulationSemantics,
    QuantityUnit,
    ReasonCode,
    SourceGranularity,
)
from quant_phase4.cross_exchange import build_phase4_context


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def liquidation(exchange, *, coverage, status=DataStatus.AVAILABLE, timestamp=NOW,
                quantity_unit=QuantityUnit.QUOTE_COIN, reason_code=None):
    return CanonicalLiquidation(
        event_id=f"{exchange}-1", exchange=exchange, exchange_symbol="BTCUSDT",
        canonical_symbol="BTC-USD", event_timestamp=timestamp, received_at=NOW,
        processed_at=NOW, side=LiquidationSide.LIQUIDATED_LONG, raw_side="buy",
        raw_side_semantics="liquidated long", price=(100_000 if status is DataStatus.AVAILABLE else None),
        raw_quantity=(1 if status is DataStatus.AVAILABLE else None),
        quantity_unit=(quantity_unit if status is DataStatus.AVAILABLE else QuantityUnit.UNKNOWN),
        quantity_base=None, notional_usd=None,
        source_endpoint=f"{exchange}-endpoint", source_channel="liquidation",
        source_granularity=(SourceGranularity.AGGREGATED_MAX_PER_SECOND
                            if exchange == "bitget" else SourceGranularity.ALL_LIQUIDATIONS_STREAM),
        coverage_semantics=coverage, status=status, raw_reference=None,
        raw_payload=None, reason_code=reason_code,
    )


def long_short(exchange, *, population=LongShortPopulationSemantics.ALL_POSITION_HOLDER_ACCOUNT_RATIO,
               status=DataStatus.AVAILABLE, timestamp=NOW):
    return LongShortObservation(
        exchange=exchange, exchange_symbol="BTCUSDT", canonical_symbol="BTC-USD",
        metric_type=LongShortMetricType.ACCOUNT_HOLDER_RATIO,
        population_semantics=population, period="5min",
        long_value=(Decimal("0.5") if status is DataStatus.AVAILABLE else None),
        short_value=(Decimal("0.5") if status is DataStatus.AVAILABLE else None),
        ratio=(Decimal("1") if status is DataStatus.AVAILABLE else None), exchange_timestamp=timestamp,
        fetched_at=NOW, received_at=NOW, processed_at=NOW,
        source_endpoint=f"{exchange}-endpoint", status=status, raw_reference=None,
        raw_payload=None,
    )


def basis(exchange, basis_type, *, status=DataStatus.AVAILABLE, timestamp=NOW):
    return BasisObservation(
        exchange=exchange, exchange_symbol="BTCUSDT", canonical_symbol="BTC-USD",
        basis_type=basis_type, perpetual_price=Decimal("100.1"),
        reference_price=Decimal("100"), absolute_basis=Decimal("0.1"),
        basis_bps=Decimal("10"), basis_pct=Decimal("0.1"), exchange_timestamp=timestamp,
        fetched_at=NOW, received_at=NOW, processed_at=NOW,
        max_timestamp_skew=timedelta(seconds=5), timestamp_skew=timedelta(seconds=1),
        source_endpoint=f"{exchange}-endpoint", status=status, raw_reference=None,
        raw_payload=None,
    )


def test_partial_bitget_liquidation_is_not_added_to_bybit_total():
    context = build_phase4_context(
        [liquidation("bitget", coverage=CoverageSemantics.PARTIAL_AGGREGATED),
         liquidation("bybit", coverage=CoverageSemantics.EXCHANGE_DECLARED_ALL_LIQUIDATIONS)],
        [], [], NOW,
    )

    assert context.liquidation.source_count == 2
    assert context.liquidation.comparable_count == 0
    assert context.liquidation.observed_total is None
    assert {row.exchange for row in context.liquidation.observations} == {"bitget", "bybit"}
    assert "INCOMPATIBLE_COVERAGE" in context.liquidation.reason_codes


def test_incompatible_long_short_population_has_no_synthetic_consensus():
    context = build_phase4_context(
        [], [long_short("bybit"), long_short("bitget", population=LongShortPopulationSemantics.HOLDER_COUNT_RATIO)], [], NOW
    )

    assert context.long_short.comparable_count == 0
    assert context.long_short.reason == "INSUFFICIENT_COMPARABLE_SOURCES"
    assert context.long_short.observed_total is None


def test_basis_types_are_isolated():
    context = build_phase4_context(
        [], [], [basis("bybit", BasisType.MARK_INDEX), basis("hyperliquid", BasisType.MARK_ORACLE)], NOW
    )

    assert context.basis.comparable_count == 0
    assert context.basis.reason == "INCOMPATIBLE_BASIS_TYPES"
    assert set(context.basis.groups) == {BasisType.MARK_INDEX, BasisType.MARK_ORACLE}


def test_stale_error_and_missing_sources_are_counted_without_false_zeroes():
    context = build_phase4_context(
        [liquidation("bybit", coverage=CoverageSemantics.EXCHANGE_DECLARED_ALL_LIQUIDATIONS,
                      status=DataStatus.STALE),
         liquidation("hyperliquid", coverage=CoverageSemantics.NOT_AVAILABLE,
                      status=DataStatus.ERROR)],
        [long_short("bybit", status=DataStatus.NOT_AVAILABLE)], [], NOW,
    )

    assert context.liquidation.stale_count == 1
    assert context.liquidation.error_count == 1
    assert context.liquidation.missing_count == 0
    assert context.liquidation.observed_total is None
    assert context.long_short.missing_count == 1


def test_compatible_sources_preserve_coverage_metadata_and_no_directional_vote():
    context = build_phase4_context(
        [liquidation("bybit", coverage=CoverageSemantics.EXCHANGE_DECLARED_ALL_LIQUIDATIONS),
         liquidation("okx", coverage=CoverageSemantics.EXCHANGE_DECLARED_ALL_LIQUIDATIONS)],
        [long_short("bybit"), long_short("okx")],
        [basis("bybit", BasisType.MARK_INDEX), basis("okx", BasisType.MARK_INDEX)], NOW,
    )

    assert context.long_short.comparable_count == 2
    assert context.basis.comparable_count == 2
    assert context.liquidation.coverage_semantics == (CoverageSemantics.EXCHANGE_DECLARED_ALL_LIQUIDATIONS,)
    assert not hasattr(context, "vote")
    assert not hasattr(context, "decision")


def test_liquidations_from_different_windows_are_not_comparable():
    context = build_phase4_context(
        [liquidation("bybit", coverage=CoverageSemantics.EXCHANGE_DECLARED_ALL_LIQUIDATIONS),
         liquidation("okx", coverage=CoverageSemantics.EXCHANGE_DECLARED_ALL_LIQUIDATIONS,
                      timestamp=NOW + timedelta(minutes=1))],
        [], [], NOW,
    )

    assert context.liquidation.comparable_count == 0
    assert context.liquidation.reason == "INSUFFICIENT_COMPARABLE_SOURCES"


def test_liquidations_with_incompatible_quantity_units_are_not_comparable():
    context = build_phase4_context(
        [liquidation("bybit", coverage=CoverageSemantics.EXCHANGE_DECLARED_ALL_LIQUIDATIONS),
         liquidation("okx", coverage=CoverageSemantics.EXCHANGE_DECLARED_ALL_LIQUIDATIONS,
                      quantity_unit=QuantityUnit.CONTRACTS)],
        [], [], NOW,
    )

    assert context.liquidation.comparable_count == 0
    assert context.liquidation.reason == "INSUFFICIENT_COMPARABLE_SOURCES"


def test_stale_only_context_preserves_status_and_source_reason():
    context = build_phase4_context(
        [liquidation("bybit", coverage=CoverageSemantics.EXCHANGE_DECLARED_ALL_LIQUIDATIONS,
                      status=DataStatus.STALE, reason_code=ReasonCode.UNCONFIRMED_SEMANTICS)],
        [], [], NOW,
    )

    assert context.liquidation.status is DataStatus.STALE
    assert context.liquidation.stale_count == 1
    assert "bybit:UNCONFIRMED_SEMANTICS" in context.liquidation.reason_codes
    assert context.liquidation.observations[0].reason_code is ReasonCode.UNCONFIRMED_SEMANTICS


def test_source_provenance_arrays_only_include_matching_statuses():
    context = build_phase4_context(
        [
            liquidation("available", coverage=CoverageSemantics.EXCHANGE_DECLARED_ALL_LIQUIDATIONS),
            liquidation("stale", coverage=CoverageSemantics.EXCHANGE_DECLARED_ALL_LIQUIDATIONS,
                        status=DataStatus.STALE),
            liquidation("missing", coverage=CoverageSemantics.NOT_AVAILABLE,
                        status=DataStatus.NOT_AVAILABLE),
        ],
        [], [], NOW,
    )
    assert context.liquidation.missing_count == 1
    assert context.liquidation.stale_count == 1


def test_processed_at_must_be_utc():
    try:
        build_phase4_context([], [], [], datetime(2026, 9, 21, 12, 0))
    except ValueError as exc:
        assert "UTC" in str(exc)
    else:
        raise AssertionError("naive processed_at should fail")
