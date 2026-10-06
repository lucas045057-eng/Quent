import asyncio
import json

from websockets.asyncio.client import connect

from quant_phase1.adapters.bitget_v3.websocket import BitgetV3UtaWebSocket


def test_live_v3_ws_subscription_ack_and_snapshots():
    async def run():
        client = BitgetV3UtaWebSocket()
        await client.connect()
        try:
            await client.subscribe_ticker("BTCUSDT")
            await client.subscribe_kline("BTCUSDT", "5m")
            seen_ack = set()
            seen_snapshot_topics = set()
            for _ in range(8):
                message = await asyncio.wait_for(client.receive(), 10)
                if message.get("event") == "subscribe":
                    arg = message["arg"]
                    seen_ack.add((arg["topic"], arg.get("interval")))
                if message.get("action") == "snapshot":
                    arg = message["arg"]
                    seen_snapshot_topics.add((arg["topic"], arg.get("interval")))
                if {"ticker", "kline"}.issubset({topic for topic, _ in seen_snapshot_topics}):
                    break
            assert ("ticker", None) in seen_ack
            assert ("kline", "5m") in seen_ack
            assert ("kline", "5m") in seen_snapshot_topics
        finally:
            await client.close()

    asyncio.run(run())


def test_reconnect_resubscribes_registered_v3_args():
    async def run():
        client = BitgetV3UtaWebSocket()
        await client.connect()
        await client.subscribe_ticker("BTCUSDT")
        await client.reconnect()
        try:
            message = await asyncio.wait_for(client.receive(), 10)
            assert message.get("event") == "subscribe"
            assert message["arg"]["topic"] == "ticker"
        finally:
            await client.close()

    asyncio.run(run())
