from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_phase3.adapters.base import AdapterSchemaError
from quant_phase3.adapters.hyperliquid import HyperliquidPublicTradeAdapter
from quant_phase3.contracts import SideSource, TradeSide


def _row(*, trade_id=123456, block_time=1789866123004, side="B"):
    return {
        "coin": "BTC",
        "side": side,
        "px": "100.50",
        "sz": "0.125",
        "hash": "0xabc",
        "time": block_time,
        "tid": trade_id,
        "users": ["0xbuyer", "0xseller"],
    }


def _message(row=None):
    return {"channel": "trades", "data": [row or _row()]}


def test_hyperliquid_uses_public_trades_subscription_and_no_private_api():
    adapter = HyperliquidPublicTradeAdapter(canonical_symbol="BTC-USDT-PERP")

    assert adapter.exchange == "hyperliquid"
    assert adapter.ws_url == "wss://api.hyperliquid.xyz/ws"
    assert adapter.subscription("BTC") == {
        "method": "subscribe",
        "subscription": {"type": "trades", "coin": "BTC"},
    }
    assert adapter.capabilities.supports_aggressor_side is False


def test_hyperliquid_public_trade_preserves_side_users_and_composite_identity():
    adapter = HyperliquidPublicTradeAdapter(canonical_symbol="BTC-USDT-PERP")
    trade = adapter.parse_ws_message(
        _message(), datetime(2026, 9, 20, 1, 2, 4, tzinfo=timezone.utc)
    )[0]

    assert trade.trade_id == "1789866123004:BTC:123456"
    assert trade.exchange_timestamp == datetime.fromtimestamp(1789866123004 / 1000, timezone.utc)
    assert trade.price == Decimal("100.50")
    assert trade.quantity_base == Decimal("0.125")
    assert trade.raw_side == "B"
    assert trade.raw_side_semantics == "PUBLIC_TRADE_SIDE_UNCONFIRMED_AGGRESSOR"
    assert trade.aggressor_side is TradeSide.UNKNOWN
    assert trade.side_source is SideSource.UNKNOWN
    assert trade.raw_payload["users"] == ["0xbuyer", "0xseller"]
    assert adapter.identity_key(trade) == (
        "hyperliquid",
        "BTC-USDT-PERP",
        "1789866123004:BTC:123456",
    )


def test_hyperliquid_public_trade_parser_ignores_any_crossed_like_field():
    row = _row(side="A")
    row["crossed"] = True
    trade = HyperliquidPublicTradeAdapter().parse_ws_message(
        _message(row), datetime.now(timezone.utc)
    )[0]

    assert trade.raw_payload["crossed"] is True
    assert trade.aggressor_side is TradeSide.UNKNOWN
    assert trade.side_source is SideSource.UNKNOWN


def test_hyperliquid_bounded_recent_trade_parser_uses_same_composite_identity():
    adapter = HyperliquidPublicTradeAdapter(canonical_symbol="BTC-USDT-PERP")
    payload = {
        "type": "recentTrades",
        "data": [_row(trade_id=2, block_time=1789866123005), _row(trade_id=1)],
    }

    trades = adapter.parse_recent_trades(payload, datetime.now(timezone.utc))

    assert [trade.trade_id for trade in trades] == [
        "1789866123004:BTC:1",
        "1789866123005:BTC:2",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"channel": "userFills", "data": []},
        {"channel": "trades", "data": []},
        {"channel": "trades", "data": [{"coin": "BTC", "px": "1", "sz": "1"}]},
    ],
)
def test_hyperliquid_parser_rejects_private_or_incomplete_schema(payload):
    with pytest.raises(AdapterSchemaError):
        HyperliquidPublicTradeAdapter().parse_ws_message(payload, datetime.now(timezone.utc))
