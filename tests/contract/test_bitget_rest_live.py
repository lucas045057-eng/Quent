import asyncio

from quant_phase1.adapters.bitget_v3.rest import BitgetV3UtaRestClient


def test_live_instruments_contract_uses_current_v3_market_endpoint():
    async def run():
        async with BitgetV3UtaRestClient() as client:
            instruments = await client.get_instruments()
            assert client.INSTRUMENTS_PATH == "/api/v3/market/instruments"
            assert instruments
            assert any(item.symbol == "BTCUSDT" for item in instruments)
            assert all(item.category == "USDT-FUTURES" for item in instruments)

    asyncio.run(run())


def test_live_ticker_contract_contains_required_v3_fields():
    async def run():
        async with BitgetV3UtaRestClient() as client:
            tickers = await client.get_tickers(symbol="BTCUSDT")
            assert tickers
            ticker = tickers[0]
            assert ticker.symbol == "BTCUSDT"
            assert ticker.last_price > 0
            assert ticker.raw_payload.get("symbol") == "BTCUSDT"

    asyncio.run(run())


def test_live_candle_contract_contains_seven_column_v3_bar():
    async def run():
        async with BitgetV3UtaRestClient() as client:
            candles = await client.get_candles(symbol="BTCUSDT", interval="5m", limit=2)
            assert candles
            assert candles[-1].interval == "5m"
            assert candles[-1].is_closed is True

    asyncio.run(run())
