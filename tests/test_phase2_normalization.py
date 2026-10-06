from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_phase2.contracts import DataStatus, OIObservation
from quant_phase2.normalization import (
    apply_derivative_freshness,
    compute_oi_change,
    normalize_open_interest,
)


def test_contracts_to_base_to_usd() -> None:
    result = normalize_open_interest(
        raw_value=Decimal("100"),
        raw_unit="CONTRACTS",
        mark_price=Decimal("2000"),
        contract_multiplier=Decimal("0.01"),
    )
    assert result.open_interest_base == Decimal("1.00")
    assert result.open_interest_usd == Decimal("2000.00")
    assert result.method == "contracts_times_multiplier_times_mark_price"


def test_missing_multiplier_does_not_guess() -> None:
    result = normalize_open_interest(
        raw_value=Decimal("100"), raw_unit="CONTRACTS", mark_price=Decimal("2000"), contract_multiplier=None
    )
    assert result.status is DataStatus.NOT_AVAILABLE
    assert result.open_interest_usd is None


def test_unconfirmed_unit_is_not_normalized() -> None:
    result = normalize_open_interest(
        raw_value=Decimal("100"), raw_unit="UNCONFIRMED", mark_price=Decimal("2000")
    )
    assert result.status is DataStatus.NOT_AVAILABLE
    assert result.method == "UNCONFIRMED_UNIT"
    assert result.open_interest_usd is None


def test_derivative_freshness_uses_receipt_when_exchange_time_is_missing() -> None:
    now = datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc)
    old = OIObservation(
        symbol="BTCUSDT", canonical_symbol="BTC-USDT-PERP", exchange="hyperliquid", contract_type="PERPETUAL",
        margin_asset=None, settle_asset="USDC", raw_open_interest=Decimal("10"), raw_unit="BASE_ASSET",
        open_interest_base=Decimal("10"), open_interest_quote=Decimal("100"), open_interest_usd=Decimal("100"),
        mark_price=Decimal("10"), normalization_method="test", exchange_timestamp=None,
        fetched_at=now - timedelta(minutes=5), processed_at=now - timedelta(minutes=5),
        status=DataStatus.AVAILABLE, source_endpoint="test", raw_payload={}
    )
    fresh = apply_derivative_freshness([old], now, max_age_seconds=180)
    assert fresh[0].status is DataStatus.STALE


def test_usd_notional_preserves_quote_value() -> None:
    result = normalize_open_interest(
        raw_value=Decimal("100000"), raw_unit="USD_NOTIONAL", mark_price=Decimal("2000")
    )
    assert result.open_interest_quote == Decimal("100000")
    assert result.open_interest_base == Decimal("50")


def test_oi_change_records_actual_window() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    old = OIObservation(
        symbol="BTCUSDT", canonical_symbol="BTC-USDT-PERP", exchange="bybit", contract_type="PERPETUAL",
        margin_asset="USDC", settle_asset="USDT", raw_open_interest=Decimal("10"), raw_unit="BASE_ASSET",
        open_interest_base=Decimal("10"), open_interest_quote=None, open_interest_usd=Decimal("100"),
        mark_price=Decimal("10"), normalization_method="test", exchange_timestamp=now - timedelta(minutes=6),
        fetched_at=now, processed_at=now, status=DataStatus.AVAILABLE, source_endpoint="test", raw_payload={}
    )
    new = OIObservation(
        symbol="BTCUSDT", canonical_symbol="BTC-USDT-PERP", exchange="bybit", contract_type="PERPETUAL",
        margin_asset="USDC", settle_asset="USDT", raw_open_interest=Decimal("12"), raw_unit="BASE_ASSET",
        open_interest_base=Decimal("12"), open_interest_quote=None, open_interest_usd=Decimal("120"),
        mark_price=Decimal("10"), normalization_method="test", exchange_timestamp=now,
        fetched_at=now, processed_at=now, status=DataStatus.AVAILABLE, source_endpoint="test", raw_payload={}
    )
    result = compute_oi_change([old, new], requested_window_seconds=300, canonical_symbol="BTC-USDT-PERP")
    assert result.old_value_usd == Decimal("100")
    assert result.new_value_usd == Decimal("120")
    assert result.change_absolute_usd == Decimal("20")
    assert result.actual_old_timestamp == old.exchange_timestamp


def test_future_exchange_timestamp_is_invalid_not_fresh():
    now = datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc)
    future = OIObservation(
        symbol="BTCUSDT", canonical_symbol="BTC-USDT-PERP", exchange="bitget", contract_type="PERPETUAL",
        margin_asset=None, settle_asset="USDT", raw_open_interest=Decimal("10"), raw_unit="BASE_ASSET",
        open_interest_base=Decimal("10"), open_interest_quote=Decimal("100"), open_interest_usd=Decimal("100"),
        mark_price=Decimal("10"), normalization_method="test", exchange_timestamp=now+timedelta(seconds=1),
        fetched_at=now, processed_at=now, status=DataStatus.AVAILABLE, source_endpoint="test", raw_payload={},
    )
    result = apply_derivative_freshness([future], now, max_age_seconds=600)
    assert result[0].status is DataStatus.STALE
