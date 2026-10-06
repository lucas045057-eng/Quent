from datetime import timezone
from decimal import Decimal

import pytest

from quant_phase1.adapters.bitget_v3.parsers import (
    parse_candles_response,
    parse_instruments_response,
    parse_tickers_response,
)
from quant_phase1.contracts import DataStatus


@pytest.mark.parametrize(("symbol", "base_coin"), [("BTCUSDT", "BTC"), ("ETHUSDT", "ETH")])
def test_parse_instruments_response_maps_v3_fields(symbol, base_coin):
    payload = {
        "code": "00000",
        "msg": "success",
        "data": [
            {
                "symbol": symbol,
                "category": "USDT-FUTURES",
                "baseCoin": base_coin,
                "quoteCoin": "USDT",
                "symbolType": "crypto",
                "type": "perpetual",
                "status": "online",
                "pricePrecision": "1",
                "quantityPrecision": "3",
                "minOrderQty": "0.001",
                "maxOrderQty": "100",
            }
        ],
    }
    instruments = parse_instruments_response(payload, fetched_at=_utc_now())
    assert instruments[0].symbol == symbol
    assert instruments[0].symbol_type == "PERPETUAL"
    assert instruments[0].contract_type == "perpetual"
    assert instruments[0].min_order_qty == Decimal("0.001")
    assert instruments[0].raw_payload["symbolType"] == "crypto"
    assert instruments[0].raw_payload["type"] == "perpetual"


def test_parse_bitget_non_crypto_perpetual_asset_class_is_preserved_as_raw_metadata():
    item = {
        "symbol": "AAPLUSDT",
        "category": "USDT-FUTURES",
        "baseCoin": "AAPL",
        "quoteCoin": "USDT",
        "symbolType": "stock",
        "type": "perpetual",
        "status": "online",
        "pricePrecision": "2",
        "quantityPrecision": "3",
        "minOrderQty": "0.01",
    }

    instrument = parse_instruments_response(
        {"code": "00000", "data": [item]}, fetched_at=_utc_now(),
    )[0]

    assert instrument.symbol_type == "PERPETUAL"
    assert instrument.contract_type == "perpetual"
    assert instrument.raw_payload["symbolType"] == "stock"


def test_parse_bitget_spot_metadata_stays_non_perpetual():
    payload = {
        "code": "00000",
        "data": [{
            "symbol": "BTCUSDT",
            "category": "SPOT",
            "baseCoin": "BTC",
            "quoteCoin": "USDT",
            "symbolType": "crypto",
            "type": None,
            "status": "online",
            "pricePrecision": "1",
            "quantityPrecision": "3",
            "minOrderQty": "0.001",
        }],
    }

    instrument = parse_instruments_response(payload, fetched_at=_utc_now())[0]

    assert instrument.symbol_type == "SPOT"
    assert instrument.contract_type == "spot"
    assert instrument.raw_payload["symbolType"] == "crypto"
    assert instrument.raw_payload["type"] is None


def test_parse_spot_metadata_with_missing_type_field_fails_closed():
    item = {
        "symbol": "BTCUSDT",
        "category": "SPOT",
        "baseCoin": "BTC",
        "quoteCoin": "USDT",
        "symbolType": "crypto",
        "status": "online",
        "pricePrecision": "1",
        "quantityPrecision": "3",
        "minOrderQty": "0.001",
    }

    with pytest.raises(ValueError, match="missing market identity"):
        parse_instruments_response({"code": "00000", "data": [item]}, fetched_at=_utc_now())


@pytest.mark.parametrize(
    "metadata",
    [
        {"category": "USDT-FUTURES", "type": "future"},
        {"category": "USDT-FUTURES", "type": None},
        {"category": "UNKNOWN", "type": "perpetual"},
        {"category": "USDT-FUTURES", "quoteCoin": "USDC"},
        {"category": "USDT-FUTURES", "symbol": "BTCUSDC"},
        {"category": "USDT-FUTURES", "symbolType": "forex"},
        {"category": "SPOT", "type": "perpetual"},
    ],
)
def test_parse_instruments_rejects_unknown_missing_or_conflicting_market_identity(metadata):
    item = {
        "symbol": "BTCUSDT",
        "category": "USDT-FUTURES",
        "baseCoin": "BTC",
        "quoteCoin": "USDT",
        "symbolType": "crypto",
        "type": "perpetual",
        "status": "online",
        "pricePrecision": "1",
        "quantityPrecision": "3",
        "minOrderQty": "0.001",
    }
    item.update(metadata)

    with pytest.raises(ValueError, match="market|identity|instrument"):
        parse_instruments_response({"code": "00000", "data": [item]}, fetched_at=_utc_now())


@pytest.mark.parametrize("field", ["category", "symbolType", "type"])
def test_parse_instruments_rejects_missing_market_identity_fields(field):
    item = {
        "symbol": "BTCUSDT",
        "category": "USDT-FUTURES",
        "baseCoin": "BTC",
        "quoteCoin": "USDT",
        "symbolType": "crypto",
        "type": "perpetual",
        "status": "online",
        "pricePrecision": "1",
        "quantityPrecision": "3",
        "minOrderQty": "0.001",
    }
    item.pop(field)

    with pytest.raises(ValueError):
        parse_instruments_response({"code": "00000", "data": [item]}, fetched_at=_utc_now())


def test_parse_ticker_keeps_raw_oi_funding_but_does_not_promote_them():
    payload = {
        "code": "00000",
        "data": [
            {
                "symbol": "BTCUSDT",
                "ts": "1758364200000",
                "lastPrice": "100",
                "bid1Price": "99.9",
                "ask1Price": "100.1",
                "bid1Size": "2",
                "ask1Size": "3",
                "volume24h": "1000",
                "turnover24h": "100000",
                "indexPrice": "100",
                "markPrice": "100",
                "openInterest": "123",
                "fundingRate": "0.001",
            }
        ],
    }
    ticker = parse_tickers_response(payload, fetched_at=_utc_now())[0]
    assert ticker.status is DataStatus.AVAILABLE
    assert ticker.raw_payload["openInterest"] == "123"
    assert not hasattr(ticker, "open_interest")


def test_parse_candles_requires_seven_columns_and_only_accepts_closed_rows():
    payload = {
        "code": "00000",
        "data": [["1758362400000", "99", "102", "98", "101", "10", "1000"]],
    }
    candles = parse_candles_response(
        payload,
        symbol="BTCUSDT",
        interval="5m",
        fetched_at=_utc_now(),
        now=_utc_now(),
    )
    assert len(candles) == 1
    assert candles[0].is_closed is True

    bad_payload = {"code": "00000", "data": [["1", "2", "3"]]}
    with pytest.raises(ValueError, match="seven columns"):
        parse_candles_response(
            bad_payload,
            symbol="BTCUSDT",
            interval="5m",
            fetched_at=_utc_now(),
            now=_utc_now(),
        )


def _utc_now():
    from datetime import datetime

    return datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc)
