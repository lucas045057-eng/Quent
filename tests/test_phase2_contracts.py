from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_phase2.contracts import (
    CanonicalSymbol,
    ContractType,
    DataStatus,
    FundingObservation,
    InstrumentMetadata,
    OIObservation,
)
from quant_phase2.symbols import SymbolRegistry


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_oi_contract_preserves_raw_and_utc_timestamps() -> None:
    now = _now()
    observation = OIObservation(
        symbol="BTCUSDT",
        canonical_symbol="BTC-USDT-PERP",
        exchange="bybit",
        contract_type=ContractType.PERPETUAL,
        margin_asset="USDC",
        settle_asset="USDT",
        raw_open_interest=Decimal("10"),
        raw_unit="BASE_ASSET",
        open_interest_base=Decimal("10"),
        open_interest_quote=Decimal("650000"),
        open_interest_usd=Decimal("650000"),
        mark_price=Decimal("65000"),
        normalization_method="base_quantity_times_mark_price",
        exchange_timestamp=now,
        fetched_at=now,
        processed_at=now,
        status=DataStatus.AVAILABLE,
        source_endpoint="/v5/market/open-interest",
        raw_payload={"openInterest": "10"},
    )
    assert observation.raw_open_interest == Decimal("10")
    assert observation.open_interest_usd == Decimal("650000")


def test_not_available_cannot_have_normalized_value() -> None:
    now = _now()
    with pytest.raises(ValueError, match="NOT_AVAILABLE"):
        OIObservation(
            symbol="BTCUSDT",
            canonical_symbol="BTC-USDT-PERP",
            exchange="mexc",
            contract_type=ContractType.PERPETUAL,
            margin_asset=None,
            settle_asset="USDT",
            raw_open_interest=Decimal("1"),
            raw_unit="UNKNOWN",
            open_interest_base=None,
            open_interest_quote=None,
            open_interest_usd=Decimal("1"),
            mark_price=None,
            normalization_method=None,
            exchange_timestamp=None,
            fetched_at=now,
            processed_at=now,
            status=DataStatus.NOT_AVAILABLE,
            source_endpoint="/ticker",
            raw_payload={"holdVol": "1"},
        )


def test_registry_requires_explicit_mapping() -> None:
    registry = SymbolRegistry(
        [
            CanonicalSymbol(
                canonical_symbol="BTC-USDT-PERP",
                base_asset="BTC",
                quote_asset="USDT",
                settle_asset="USDT",
                contract_type=ContractType.PERPETUAL,
                exchange_symbols={"bitget": "BTCUSDT", "bybit": "BTCUSDT", "hyperliquid": "BTC"},
            )
        ]
    )
    assert registry.resolve("bybit", "BTCUSDT").canonical_symbol == "BTC-USDT-PERP"
    with pytest.raises(KeyError):
        registry.resolve("okx", "BTC-USDT-SWAP")


def test_funding_keeps_predicted_and_realized_separate() -> None:
    now = _now()
    funding = FundingObservation(
        symbol="BTCUSDT",
        canonical_symbol="BTC-USDT-PERP",
        exchange="bitget",
        contract_type=ContractType.PERPETUAL,
        funding_rate=Decimal("0.0001"),
        funding_interval_seconds=28800,
        normalized_8h_rate=Decimal("0.0001"),
        predicted_funding_rate=Decimal("0.0002"),
        realized_funding_rate=None,
        next_funding_time=now,
        exchange_timestamp=now,
        fetched_at=now,
        processed_at=now,
        status=DataStatus.AVAILABLE,
        source_endpoint="/api/v3/market/tickers",
        raw_payload={"fundingRate": "0.0001"},
    )
    assert funding.predicted_funding_rate != funding.realized_funding_rate
