from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_phase3.adapters.base import AdapterSchemaError
from quant_phase3.adapters.bitget import BitgetUTA3PublicTradeAdapter
from quant_phase3.contracts import SideSource, TradeSide


def _row(trade_id="bg-1", timestamp="1789866123004", side="buy"):
    return {
        "execId": trade_id,
        "execLinkId": "link-1",
        "price": "100.50",
        "size": "0.125",
        "side": side,
        "ts": timestamp,
        "isRPI": "no",
    }


def _ws_row(trade_id="bg-1", timestamp="1789866123004", side="buy"):
    return {
        "i": trade_id,
        "L": "link-1",
        "p": "100.50",
        "v": "0.125",
        "S": side,
        "T": timestamp,
        "isRPI": "no",
    }


def _ws_message(row=None):
    return {
        "arg": {
            "instType": "usdt-futures",
            "topic": "publicTrade",
            "symbol": "BTCUSDT",
        },
        "action": "snapshot",
        "data": [row or _ws_row()],
    }


def test_bitget_uses_uta_v3_public_trade_subscription_only():
    adapter = BitgetUTA3PublicTradeAdapter(canonical_symbol="BTC-USDT-PERP")

    assert adapter.exchange == "bitget"
    assert adapter.ws_url == "wss://ws.bitget.com/v3/ws/public"
    assert adapter.subscription("BTCUSDT") == {
        "op": "subscribe",
        "args": [
            {
                "instType": "usdt-futures",
                "topic": "publicTrade",
                "symbol": "BTCUSDT",
            }
        ],
    }
    assert adapter.capabilities.supports_aggressor_side is False


def test_bitget_v3_ws_trade_preserves_raw_side_but_marks_aggressor_unknown():
    trade = BitgetUTA3PublicTradeAdapter(canonical_symbol="BTC-USDT-PERP").parse_ws_message(
        _ws_message(), datetime(2026, 9, 20, 1, 2, 4, tzinfo=timezone.utc)
    )[0]

    assert trade.trade_id == "bg-1"
    assert trade.price == Decimal("100.50")
    assert trade.quantity_base == Decimal("0.125")
    assert trade.notional_usd == Decimal("12.5625")
    assert trade.raw_side == "buy"
    assert trade.raw_side_semantics == "TRADE_SIDE_UNCONFIRMED_AGGRESSOR"
    assert trade.aggressor_side is TradeSide.UNKNOWN
    assert trade.side_source is SideSource.UNKNOWN
    assert trade.raw_payload["isRPI"] == "no"


def test_bitget_v3_rest_fills_uses_public_endpoint_and_same_identity_path():
    adapter = BitgetUTA3PublicTradeAdapter(canonical_symbol="BTC-USDT-PERP")
    path, params = adapter.recent_trade_request("BTCUSDT", limit=100)
    payload = {"code": "00000", "msg": "success", "data": [_row()]}

    trades = adapter.parse_recent_trades(
        payload, datetime.now(timezone.utc), exchange_symbol="BTCUSDT"
    )

    assert path == "/api/v3/market/fills"
    assert params == {"category": "USDT-FUTURES", "symbol": "BTCUSDT", "limit": "100"}
    assert adapter.identity_key(trades[0]) == ("bitget", "BTC-USDT-PERP", "bg-1")


@pytest.mark.parametrize(
    "payload",
    [
        {"arg": {"instType": "usdt-futures", "topic": "trade", "symbol": "BTCUSDT"}, "data": []},
        {"arg": {"instType": "USDT-FUTURES", "topic": "publicTrade", "symbol": "BTCUSDT"}, "data": []},
        {"arg": {"instType": "usdt-futures", "topic": "publicTrade", "symbol": "BTCUSDT"}, "data": [{"price": "1"}]},
        {"arg": {"instType": "usdt-futures", "channel": "trade", "instId": "BTCUSDT"}, "data": []},
    ],
)
def test_bitget_parser_rejects_non_v3_or_incomplete_public_trade_schema(payload):
    with pytest.raises(AdapterSchemaError):
        BitgetUTA3PublicTradeAdapter().parse_ws_message(payload, datetime.now(timezone.utc))


def test_bitget_adapter_never_promotes_trade_side_to_directional_flow():
    adapter = BitgetUTA3PublicTradeAdapter()
    for side in ("buy", "sell"):
        trade = adapter.parse_ws_message(_ws_message(_ws_row(side=side)), datetime.now(timezone.utc))[0]
        assert trade.aggressor_side is TradeSide.UNKNOWN
        assert trade.side_source is SideSource.UNKNOWN
