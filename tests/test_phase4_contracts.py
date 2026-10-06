from datetime import datetime, timedelta, timezone

import pytest

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
    SourceGranularity,
)


UTC_NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


def liquidation(**overrides: object) -> CanonicalLiquidation:
    values = {
        "event_id": "bitget-1",
        "exchange": "bitget",
        "exchange_symbol": "BTCUSDT",
        "canonical_symbol": "BTC-USD",
        "event_timestamp": UTC_NOW,
        "received_at": UTC_NOW,
        "processed_at": UTC_NOW,
        "side": LiquidationSide.LIQUIDATED_LONG,
        "raw_side": "buy",
        "raw_side_semantics": "liquidated long",
        "price": 100_000,
        "raw_quantity": 10,
        "quantity_unit": QuantityUnit.QUOTE_COIN,
        "quantity_base": None,
        "notional_usd": None,
        "source_endpoint": "wss://ws.bitget.com/v3/ws/public",
        "source_channel": "liquidation",
        "source_granularity": SourceGranularity.AGGREGATED_MAX_PER_SECOND,
        "coverage_semantics": CoverageSemantics.PARTIAL_AGGREGATED,
        "status": DataStatus.AVAILABLE,
        "raw_reference": "message-1",
        "raw_payload": {"amount": "10"},
        "reason_code": None,
    }
    values.update(overrides)
    return CanonicalLiquidation(**values)


def long_short(**overrides: object) -> LongShortObservation:
    values = {
        "exchange": "bybit",
        "exchange_symbol": "BTCUSDT",
        "canonical_symbol": "BTC-USD",
        "metric_type": LongShortMetricType.ACCOUNT_HOLDER_RATIO,
        "population_semantics": LongShortPopulationSemantics.ALL_POSITION_HOLDER_ACCOUNT_RATIO,
        "period": "5min",
        "long_value": 0.52,
        "short_value": 0.48,
        "ratio": 1.0833,
        "exchange_timestamp": UTC_NOW,
        "fetched_at": UTC_NOW,
        "received_at": UTC_NOW,
        "processed_at": UTC_NOW,
        "source_endpoint": "https://api.bybit.com/v5/market/account-ratio",
        "status": DataStatus.AVAILABLE,
        "raw_reference": "response-1",
        "raw_payload": {"buyRatio": "0.52"},
        "reason_code": None,
    }
    values.update(overrides)
    return LongShortObservation(**values)


def basis(**overrides: object) -> BasisObservation:
    values = {
        "exchange": "hyperliquid",
        "exchange_symbol": "BTC",
        "canonical_symbol": "BTC-USD",
        "basis_type": BasisType.MARK_ORACLE,
        "perpetual_price": 100_100,
        "reference_price": 100_000,
        "absolute_basis": 100,
        "basis_bps": 10,
        "basis_pct": 0.1,
        "exchange_timestamp": UTC_NOW,
        "fetched_at": UTC_NOW,
        "received_at": UTC_NOW,
        "processed_at": UTC_NOW,
        "max_timestamp_skew": timedelta(seconds=5),
        "timestamp_skew": timedelta(seconds=1),
        "source_endpoint": "https://api.hyperliquid.xyz/info",
        "status": DataStatus.AVAILABLE,
        "raw_reference": "response-1",
        "raw_payload": {"markPx": "100100"},
        "reason_code": None,
    }
    values.update(overrides)
    return BasisObservation(**values)


def test_data_status_preserves_all_four_phase4_statuses() -> None:
    assert {status.value for status in DataStatus} == {
        "AVAILABLE",
        "STALE",
        "NOT_AVAILABLE",
        "ERROR",
    }


@pytest.mark.parametrize("factory", [liquidation, long_short, basis])
def test_contracts_reject_naive_and_non_utc_timestamps(factory: object) -> None:
    with pytest.raises(ValueError, match="UTC-aware"):
        factory(processed_at=datetime(2026, 9, 21, 12, 0))

    with pytest.raises(ValueError, match="UTC-aware"):
        factory(processed_at=datetime(2026, 9, 21, 20, 0, tzinfo=timezone(timedelta(hours=8))))


@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (liquidation, "price"),
        (liquidation, "raw_quantity"),
        (long_short, "long_value"),
        (basis, "perpetual_price"),
        (basis, "reference_price"),
    ],
)
def test_contracts_reject_non_positive_numeric_values(factory: object, field: str) -> None:
    with pytest.raises(ValueError, match="positive"):
        factory(**{field: 0})


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (liquidation, "price"),
        (liquidation, "raw_quantity"),
        (basis, "perpetual_price"),
    ],
)
def test_contracts_reject_non_finite_prices_and_quantities(
    factory: object, field: str, value: float
) -> None:
    with pytest.raises(ValueError, match="finite"):
        factory(**{field: value})


@pytest.mark.parametrize(
    ("factory", "field"),
    [
        (liquidation, "source_endpoint"),
        (liquidation, "source_channel"),
        (long_short, "source_endpoint"),
        (basis, "source_endpoint"),
    ],
)
def test_contracts_require_explicit_source_fields(factory: object, field: str) -> None:
    with pytest.raises(ValueError, match="source"):
        factory(**{field: ""})


def test_liquidation_keeps_unknown_side_and_unverified_normalized_values_nullable() -> None:
    event = liquidation(
        side=LiquidationSide.UNKNOWN,
        quantity_unit=QuantityUnit.UNKNOWN,
        quantity_base=None,
        notional_usd=None,
    )

    assert event.side is LiquidationSide.UNKNOWN
    assert event.quantity_base is None
    assert event.notional_usd is None
    assert event.source_granularity is SourceGranularity.AGGREGATED_MAX_PER_SECOND
    assert event.coverage_semantics is CoverageSemantics.PARTIAL_AGGREGATED


def test_long_short_preserves_account_holder_ratio_metric_type() -> None:
    observation = long_short()

    assert observation.metric_type is LongShortMetricType.ACCOUNT_HOLDER_RATIO


def test_long_short_rejects_unknown_population_semantics() -> None:
    with pytest.raises(ValueError, match="population_semantics"):
        long_short(population_semantics="UNRECOGNIZED")


def test_basis_preserves_mark_index_and_mark_oracle_as_distinct_types() -> None:
    index_basis = basis(basis_type=BasisType.MARK_INDEX)
    oracle_basis = basis(basis_type=BasisType.MARK_ORACLE)

    assert index_basis.basis_type is BasisType.MARK_INDEX
    assert oracle_basis.basis_type is BasisType.MARK_ORACLE
    assert index_basis.basis_type is not oracle_basis.basis_type


@pytest.mark.parametrize("factory", [liquidation, long_short, basis])
@pytest.mark.parametrize("status", [DataStatus.NOT_AVAILABLE, DataStatus.ERROR])
def test_not_available_and_error_contracts_reject_normalized_values(factory: object, status: DataStatus) -> None:
    with pytest.raises(ValueError, match="normalized"):
        factory(status=status)
