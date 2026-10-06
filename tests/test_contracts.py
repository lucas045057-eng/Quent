from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_phase1.contracts import (
    Candle,
    DataStatus,
    Observation,
    RawReference,
    Ticker,
    not_available_observation,
)


NOW = datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc)


def test_observation_keeps_three_distinct_utc_timestamps_and_raw_reference():
    reference = RawReference(provider="bitget", endpoint="/api/v3/market/tickers", payload_hash="abc")
    observation = Observation(
        symbol="BTCUSDT",
        value=Decimal("100.5"),
        source="bitget_v3_rest",
        exchange="bitget",
        exchange_timestamp=NOW,
        fetched_at=NOW,
        processed_at=NOW,
        status=DataStatus.AVAILABLE,
        raw_payload={"lastPrice": "100.5"},
        raw_reference=reference,
    )
    assert observation.value == Decimal("100.5")
    assert observation.exchange_timestamp == NOW
    assert observation.fetched_at == NOW
    assert observation.processed_at == NOW
    assert observation.raw_reference.endpoint.endswith("tickers")


def test_observation_rejects_naive_timestamps():
    with pytest.raises(ValueError, match="UTC"):
        Observation(
            symbol="BTCUSDT",
            value=Decimal("1"),
            source="test",
            exchange="bitget",
            exchange_timestamp=datetime(2026, 9, 20, 10, 30),
            fetched_at=NOW,
            processed_at=NOW,
            status=DataStatus.AVAILABLE,
        )


def test_not_available_observation_has_no_value_and_preserves_reason():
    observation = not_available_observation(
        symbol="BTCUSDT",
        source="phase1_contract",
        reason="OI semantic pipeline is out of scope",
        fetched_at=NOW,
    )
    assert observation.status is DataStatus.NOT_AVAILABLE
    assert observation.value is None
    assert observation.raw_reference is not None
    assert observation.raw_reference.reason == "OI semantic pipeline is out of scope"


def test_ticker_has_price_fields_but_no_semantic_oi_or_funding_fields():
    ticker = Ticker(
        symbol="BTCUSDT",
        last_price=Decimal("100"),
        bid_price=Decimal("99.9"),
        ask_price=Decimal("100.1"),
        bid_size=Decimal("2"),
        ask_size=Decimal("3"),
        volume24h=Decimal("1000"),
        turnover24h=Decimal("100000"),
        index_price=Decimal("100"),
        mark_price=Decimal("100"),
        exchange_timestamp=NOW,
        fetched_at=NOW,
        processed_at=NOW,
        status=DataStatus.AVAILABLE,
        raw_payload={"openInterest": "123", "fundingRate": "0.001"},
    )
    assert ticker.last_price == Decimal("100")
    assert not hasattr(ticker, "open_interest")
    assert not hasattr(ticker, "funding_rate")


def test_candle_contract_contains_interval_and_closed_bar_metadata():
    candle = Candle(
        symbol="BTCUSDT",
        interval="1H",
        bar_open_timestamp=datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc),
        open=Decimal("99"),
        high=Decimal("102"),
        low=Decimal("98"),
        close=Decimal("101"),
        volume=Decimal("10"),
        turnover=Decimal("1000"),
        exchange_timestamp=NOW,
        fetched_at=NOW,
        processed_at=NOW,
        status=DataStatus.AVAILABLE,
        is_closed=True,
        raw_payload=["1758358800000", "99", "102", "98", "101", "10", "1000"],
    )
    assert candle.interval == "1H"
    assert candle.is_closed is True
