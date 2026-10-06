from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_phase3.adapters.base import AdapterSchemaError
from quant_phase3.adapters.bybit import BybitPublicTradeAdapter
from quant_phase3.contracts import SideSource, TradeSide


def _message(side="Buy", trade_id="bybit-1", timestamp=1789866123004):
    return {
        "topic": "publicTrade.BTCUSDT",
        "type": "snapshot",
        "ts": timestamp,
        "data": [
            {
                "T": timestamp,
                "s": "BTCUSDT",
                "S": side,
                "v": "0.125",
                "p": "100.50",
                "i": trade_id,
                "seq": 101,
            }
        ],
    }


def _rest_row(side="Buy", trade_id="bybit-1", timestamp="1789866123004"):
    return {
        "execId": trade_id,
        "symbol": "BTCUSDT",
        "price": "100.50",
        "size": "0.125",
        "side": side,
        "time": timestamp,
    }


def test_bybit_uses_v5_public_trade_subscription_and_capabilities():
    adapter = BybitPublicTradeAdapter(canonical_symbol="BTC-USDT-PERP")

    assert adapter.exchange == "bybit"
    assert adapter.ws_url == "wss://stream.bybit.com/v5/public/linear"
    assert adapter.subscription("BTCUSDT") == {
        "op": "subscribe",
        "args": ["publicTrade.BTCUSDT"],
    }
    assert adapter.capabilities.supports_aggressor_side is True


def test_bybit_ws_trade_maps_documented_taker_side_to_canonical_trade():
    received_at = datetime(2026, 9, 20, 1, 2, 4, tzinfo=timezone.utc)
    trade = BybitPublicTradeAdapter(canonical_symbol="BTC-USDT-PERP").parse_ws_message(
        _message(), received_at
    )[0]

    assert trade.trade_id == "bybit-1"
    assert trade.exchange_timestamp == datetime.fromtimestamp(1789866123004 / 1000, timezone.utc)
    assert trade.price == Decimal("100.50")
    assert trade.quantity_base == Decimal("0.125")
    assert trade.notional_usd == Decimal("12.5625")
    assert trade.aggressor_side is TradeSide.BUY
    assert trade.raw_side == "Buy"
    assert trade.side_source is SideSource.EXCHANGE_PROVIDED
    assert trade.raw_payload["seq"] == 101


def test_bybit_sell_is_directional_sell_and_identity_uses_source_trade_id():
    adapter = BybitPublicTradeAdapter(canonical_symbol="BTC-USDT-PERP")
    trade = adapter.parse_ws_message(
        _message(side="Sell", trade_id="bybit-2"), datetime.now(timezone.utc)
    )[0]

    assert trade.aggressor_side is TradeSide.SELL
    assert adapter.identity_key(trade) == ("bybit", "BTC-USDT-PERP", "bybit-2")


def test_bybit_recent_trade_parser_validates_v5_response_and_orders_by_event_time():
    adapter = BybitPublicTradeAdapter(canonical_symbol="BTC-USDT-PERP")
    payload = {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "category": "linear",
            "list": [
                _rest_row(timestamp="1789866123005", trade_id="new"),
                _rest_row(timestamp="1789866123004", trade_id="old"),
            ],
        },
        "time": 1789866123006,
    }

    trades = adapter.parse_recent_trades(payload, datetime.now(timezone.utc))

    assert [trade.trade_id for trade in trades] == ["old", "new"]
    assert all(trade.source_channel == "rest:/v5/market/recent-trade" for trade in trades)


@pytest.mark.parametrize(
    "payload",
    [
        {"topic": "publicTrade.BTCUSDT", "data": []},
        {"topic": "publicTrade.BTCUSDT", "data": [{"T": 1, "s": "BTCUSDT"}]},
        {"topic": "wrong.BTCUSDT", "data": []},
    ],
)
def test_bybit_parser_fails_closed_on_missing_or_wrong_schema(payload):
    with pytest.raises(AdapterSchemaError):
        BybitPublicTradeAdapter().parse_ws_message(payload, datetime.now(timezone.utc))


def test_bybit_does_not_accept_bitget_or_classic_subscription_fields():
    subscription = BybitPublicTradeAdapter().subscription("BTCUSDT")

    assert "instType" not in subscription
    assert "topic" not in subscription
    assert "channel" not in subscription
