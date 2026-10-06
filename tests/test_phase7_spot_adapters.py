from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from decimal import Decimal

import pytest

from quant_phase7.contracts import MarketKind
from quant_phase7.spot import (
    BinanceSpotAdapter,
    BitgetUtaV3SpotAdapter,
    SpotAdapterError,
    SpotSide,
    validate_spot_subscription,
)
from quant_phase7.sources import SourceStatus, SourceRegistry, default_source_definitions


NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _verified_bitget_registry() -> SourceRegistry:
    return SourceRegistry(tuple(
        replace(definition, status=SourceStatus.ENABLED)
        if definition.source_id == "bitget_spot_uta_v3" else definition
        for definition in default_source_definitions()
    ))


def test_binance_subscription_is_spot_agg_trade_only():
    adapter = BinanceSpotAdapter()
    assert adapter.subscription("BTCUSDT", request_id=7) == {
        "method": "SUBSCRIBE",
        "params": ["btcusdt@aggTrade"],
        "id": 7,
    }
    assert adapter.market_kind is MarketKind.SPOT
    with pytest.raises(SpotAdapterError):
        adapter.subscription("BTC-USDT", request_id=7)


def test_bitget_uta_v3_subscription_is_exact_spot_public_trade_schema():
    adapter = BitgetUtaV3SpotAdapter()
    assert adapter.subscription("BTCUSDT") == {
        "op": "subscribe",
        "args": [{
            "instType": "spot",
            "topic": "publicTrade",
            "symbol": "BTCUSDT",
        }],
    }
    assert adapter.market_kind is MarketKind.SPOT


def test_classic_v2_subscription_schema_is_rejected():
    with pytest.raises(SpotAdapterError, match="v2|Classic|channel|instId"):
        validate_spot_subscription({
            "op": "subscribe",
            "args": [{
                "instType": "SPOT",
                "channel": "trade",
                "instId": "BTCUSDT",
            }],
        })
    with pytest.raises(SpotAdapterError):
        BitgetUtaV3SpotAdapter().parse_ws_message({
            "arg": {"instType": "SPOT", "channel": "trade", "instId": "BTCUSDT"},
            "data": [],
        }, fetched_at=NOW)


def test_only_approved_spot_symbols_are_accepted():
    for symbol in ("BTCUSDT", "ETHUSDT", "btcusdt", "ethusdt"):
        assert BinanceSpotAdapter().normalize_symbol(symbol) in {"BTCUSDT", "ETHUSDT"}
        assert BitgetUtaV3SpotAdapter().normalize_symbol(symbol) in {"BTCUSDT", "ETHUSDT"}
    for symbol in ("SOLUSDT", "BTC-USD-SWAP", "BTCUSDT_UMCBL", "BTCUSD"):
        with pytest.raises(SpotAdapterError):
            BinanceSpotAdapter().normalize_symbol(symbol)


def test_binance_ws_maker_flag_maps_aggressor_and_preserves_exact_decimals():
    adapter = BinanceSpotAdapter()
    payload = {
        "e": "aggTrade",
        "E": 1727092800123,
        "s": "BTCUSDT",
        "a": 123456,
        "p": "63000.12000000",
        "q": "0.01050000",
        "f": 100,
        "l": 101,
        "T": 1727092800000,
        "m": False,
        "M": True,
    }
    event = adapter.parse_ws_message(payload, fetched_at=NOW)
    assert event.market_kind is MarketKind.SPOT
    assert event.trade_id == "123456"
    assert event.side is SpotSide.BUY
    assert event.maker_flag is False
    assert event.price == Decimal("63000.12000000")
    assert event.quantity == Decimal("0.01050000")
    assert event.first_trade_id == "100"
    assert event.last_trade_id == "101"
    assert event.event_timestamp == datetime(2024, 9, 23, 12, 0, tzinfo=timezone.utc)
    assert event.exchange_timestamp == event.event_timestamp
    assert event.identity == ("binance_spot", "BTCUSDT", "123456")

    payload["m"] = True
    sell = adapter.parse_ws_message(payload, fetched_at=NOW)
    assert sell.side is SpotSide.SELL
    assert sell.maker_flag is True

    payload["m"] = "false"
    unknown = adapter.parse_ws_message(payload, fetched_at=NOW)
    assert unknown.side is SpotSide.UNKNOWN
    assert unknown.maker_flag is None


def test_binance_rest_schema_requires_array_and_parses_each_trade():
    adapter = BinanceSpotAdapter()
    path, params = adapter.rest_request("ETHUSDT", limit=100)
    assert path == "/api/v3/aggTrades"
    assert params == {"symbol": "ETHUSDT", "limit": "100"}
    events = adapter.parse_rest_response([{
        "a": 9,
        "p": "3200.50",
        "q": "0.25",
        "f": 20,
        "l": 20,
        "T": 1727092800000,
        "m": False,
        "M": True,
    }], symbol="ETHUSDT", fetched_at=NOW)
    assert len(events) == 1
    assert events[0].side is SpotSide.BUY
    assert events[0].source_channel == "aggTrades"
    with pytest.raises(SpotAdapterError):
        adapter.parse_rest_response([{
            "s": "BTCUSDT", "a": 9, "p": "3200.50", "q": "0.25",
            "f": 20, "l": 20, "T": 1727092800000, "m": False,
        }], symbol="ETHUSDT", fetched_at=NOW)
    with pytest.raises(SpotAdapterError):
        adapter.parse_rest_response({"data": []}, symbol="ETHUSDT", fetched_at=NOW)
    with pytest.raises(SpotAdapterError):
        adapter.rest_request("BTCUSDT", limit=1001)
    assert adapter.rest_request("BTCUSDT", from_id=42)[1]["fromId"] == "42"
    assert adapter.rest_request("BTCUSDT", start_time_ms=10, end_time_ms=20)[1]["endTime"] == "20"
    with pytest.raises(SpotAdapterError):
        adapter.rest_request("BTCUSDT", from_id=42, start_time_ms=10)


def test_bitget_v3_trade_keeps_side_unknown_even_when_raw_side_exists():
    adapter = BitgetUtaV3SpotAdapter()
    payload = {
        "arg": {"instType": "spot", "topic": "publicTrade", "symbol": "BTCUSDT"},
        "data": [{
            "i": "bg-42",
            "p": "63000.1200",
            "v": "0.0105",
            "S": "buy",
            "T": "1727092800000",
        }],
    }
    event = adapter.parse_ws_message(payload, fetched_at=NOW)
    assert event.market_kind is MarketKind.SPOT
    assert event.side is SpotSide.UNKNOWN
    assert event.raw_side == "buy"
    assert event.price == Decimal("63000.1200")
    assert event.quantity == Decimal("0.0105")
    assert event.event_timestamp == datetime(2024, 9, 23, 12, 0, tzinfo=timezone.utc)
    assert event.identity == ("bitget_spot_uta_v3", "BTCUSDT", "bg-42")


def test_bitget_public_trade_batch_is_bounded_and_each_event_keeps_identity():
    adapter = BitgetUtaV3SpotAdapter()
    payload = {
        "arg": {"instType": "spot", "topic": "publicTrade", "symbol": "ETHUSDT"},
        "data": [
            {"i": "bg-1", "p": "3200", "v": "0.1", "S": "buy", "T": "1727092800000"},
            {"i": "bg-2", "p": "3201", "v": "0.2", "S": "sell", "T": "1727092800001"},
        ],
    }
    events = adapter.parse_ws_events(payload, fetched_at=NOW)
    assert [event.identity for event in events] == [
        ("bitget_spot_uta_v3", "ETHUSDT", "bg-1"),
        ("bitget_spot_uta_v3", "ETHUSDT", "bg-2"),
    ]
    with pytest.raises(SpotAdapterError, match="parse_ws_events"):
        adapter.parse_ws_message(payload, fetched_at=NOW)


def test_bitget_rest_is_v3_spot_only_without_silent_fallback():
    with pytest.raises(SpotAdapterError, match="PENDING_CONTRACT|enabled"):
        BitgetUtaV3SpotAdapter().rest_request("BTCUSDT", limit=100)
    adapter = BitgetUtaV3SpotAdapter(registry=_verified_bitget_registry())
    path, params = adapter.rest_request("BTCUSDT", limit=100)
    assert path == "/api/v3/market/fills"
    assert params == {"category": "SPOT", "symbol": "BTCUSDT", "limit": "100"}
    with pytest.raises(SpotAdapterError):
        adapter.rest_request("BTCUSDT", category="USDT-FUTURES")  # type: ignore[call-arg]
    with pytest.raises(SpotAdapterError, match="v2|fallback"):
        adapter.parse_rest_response({
            "code": "00000",
            "data": [{
                "tradeId": "1",
                "price": "1",
                "size": "1",
                "side": "buy",
                "ts": "1727092800000",
                "instId": "BTCUSDT_UMCBL",
            }],
        }, symbol="BTCUSDT", fetched_at=NOW)


def test_identity_dedup_and_reconnect_are_finite_and_schema_stable():
    binance = BinanceSpotAdapter()
    bitget = BitgetUtaV3SpotAdapter()
    assert binance.dedup_key(binance.parse_ws_message({
        "e": "aggTrade", "E": 1, "s": "BTCUSDT", "a": 1, "p": "1",
        "q": "1", "f": 1, "l": 1, "T": 1, "m": False, "M": True,
    }, fetched_at=NOW)) == ("binance_spot", "BTCUSDT", "1")
    assert bitget.reconnect_subscriptions("ETHUSDT", max_attempts=3) == (
        bitget.subscription("ETHUSDT"),
        bitget.subscription("ETHUSDT"),
        bitget.subscription("ETHUSDT"),
    )
    with pytest.raises(SpotAdapterError):
        bitget.reconnect_subscriptions("BTCUSDT", max_attempts=4)
    assert "channel" not in str(bitget.subscription("BTCUSDT"))
    assert "instId" not in str(bitget.subscription("BTCUSDT"))


def test_spot_adapter_rejects_derivative_market_kind_and_non_utc_receive_time():
    for adapter in (BinanceSpotAdapter(), BitgetUtaV3SpotAdapter()):
        with pytest.raises(SpotAdapterError):
            adapter.validate_market_kind("USDT-FUTURES")
        with pytest.raises(SpotAdapterError):
            adapter.subscription("SOLUSDT")
        with pytest.raises(SpotAdapterError):
            adapter.parse_ws_message({}, fetched_at=datetime(2026, 9, 23, 12, 0))



def test_bitget_rest_exec_id_matches_ws_identity_without_promoting_direction():
    adapter = BitgetUtaV3SpotAdapter()
    rest = adapter.parse_rest_response({"code": "00000", "data": [{
        "execId": "1490317872477224960", "price": "84823.23", "size": "0.000012",
        "side": "buy", "ts": "1791045390242", "isRPI": "NO",
    }]}, symbol="BTCUSDT", fetched_at=NOW)[0]
    ws = adapter.parse_ws_message({
        "arg": {"instType": "spot", "topic": "publicTrade", "symbol": "BTCUSDT"},
        "data": [{"i": rest.trade_id, "p": "84823.23", "v": "0.000012",
                  "S": "buy", "T": "1791045390242", "isRPI": "no"}],
    }, fetched_at=NOW)
    assert rest.identity == ws.identity
    assert rest.price == ws.price and rest.quantity == ws.quantity
    assert rest.event_timestamp == ws.event_timestamp
    assert rest.side is ws.side is SpotSide.UNKNOWN
    assert rest.raw_side == ws.raw_side == "buy"


def test_bitget_rest_limit_is_one_to_one_hundred_without_classic_fallback():
    adapter = BitgetUtaV3SpotAdapter(registry=_verified_bitget_registry())
    with pytest.raises(SpotAdapterError):
        adapter.rest_request("BTCUSDT", limit=101)
    row = {"tradeId": "123", "price": "1", "size": "1", "side": "buy", "ts": "1"}
    with pytest.raises(SpotAdapterError):
        adapter.parse_rest_response({"code": "00000", "data": [row]}, symbol="BTCUSDT", fetched_at=NOW)
    row = {"execId": "123", "price": "1", "size": "1", "side": "buy", "ts": "1"}
    with pytest.raises(SpotAdapterError):
        adapter.parse_rest_response({"code": "00000", "data": [row]*101}, symbol="BTCUSDT", fetched_at=NOW)
    row["side"] = "maker"
    with pytest.raises(SpotAdapterError):
        adapter.parse_rest_response({"code": "00000", "data": [row]}, symbol="BTCUSDT", fetched_at=NOW)
