from datetime import datetime, timezone

from quant_phase1.runtime import WebSocketCanonicalStore


NOW = datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc)


def test_ws_message_is_converted_to_canonical_closed_bar_only():
    store = WebSocketCanonicalStore(capacity=4)
    ticker = {
        "arg": {"instType": "usdt-futures", "topic": "ticker", "symbol": "BTCUSDT"},
        "ts": "1789899900000",
        "data": [{
            "symbol": "BTCUSDT", "lastPrice": "100", "bid1Price": "99.9", "ask1Price": "100.1",
            "bid1Size": "1", "ask1Size": "1", "volume24h": "10", "turnover24h": "1000",
            "indexPrice": "100", "markPrice": "100", "ts": "1790284200000",
        }],
    }
    kline = {
        "arg": {"instType": "usdt-futures", "topic": "kline", "symbol": "BTCUSDT", "interval": "5m"},
        "ts": "1789899900000",
        "data": [["1789899900000", "99", "101", "98", "100", "10", "1000"]],
    }
    store.ingest(ticker, now=NOW)
    store.ingest(kline, now=NOW)
    assert store.tickers["BTCUSDT"].status.value == "AVAILABLE"
    assert store.tickers["BTCUSDT"].source == "bitget_v3_ws"
    assert store.closed_klines["BTCUSDT"]["5m"][0].is_closed is True
    assert store.closed_klines["BTCUSDT"]["5m"][0].status.value == "AVAILABLE"
    assert store.closed_klines["BTCUSDT"]["5m"][0].source == "bitget_v3_ws"


def test_ws_store_is_bounded():
    store = WebSocketCanonicalStore(capacity=1)
    assert store.capacity == 1


def test_ws_subscription_ack_is_not_treated_as_market_data():
    store = WebSocketCanonicalStore(capacity=1)
    store.ingest({"event": "subscribe", "arg": {"topic": "ticker"}}, now=NOW)
    assert store.tickers == {}
