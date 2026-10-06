import asyncio
from datetime import datetime, timezone
from decimal import Decimal

from quant_phase1.contracts import Candle, DataStatus, Instrument, Ticker
from quant_phase1.pipeline import MarketDataCollector, run_stage1


NOW = datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc)


def _instrument() -> Instrument:
    return Instrument("BTCUSDT", "USDT-FUTURES", "BTC", "USDT", "PERPETUAL", "perpetual", "online", 2, 3, Decimal("0.001"), None, NOW, {"symbolType": "crypto"})


def _ticker() -> Ticker:
    return Ticker("BTCUSDT", Decimal("100"), Decimal("99.9"), Decimal("100.1"), Decimal("1"), Decimal("1"), Decimal("1000"), Decimal("100000"), Decimal("100"), Decimal("100"), NOW, NOW, NOW, DataStatus.AVAILABLE, {})


def _candle(interval: str) -> Candle:
    return Candle("BTCUSDT", interval, NOW, Decimal("99"), Decimal("102"), Decimal("98"), Decimal("101"), Decimal("10"), Decimal("1000"), NOW, NOW, NOW, DataStatus.AVAILABLE, True, [])


class FakeRest:
    def __init__(self) -> None:
        self.ticker_calls = 0

    async def get_instruments(self):
        return [_instrument()]

    async def get_tickers(self):
        self.ticker_calls += 1
        return [_ticker()]

    async def get_candles(self, *, symbol, interval, limit=200):
        return [_candle(interval)]


def test_collector_collects_only_configured_closed_kline_intervals():
    async def run():
        batch = await MarketDataCollector(FakeRest(), max_symbols=1).collect_once()
        assert list(batch.candles_by_symbol["BTCUSDT"]) == ["5m", "15m", "1H", "4H"]
        assert all(candle.is_closed for candles in batch.candles_by_symbol["BTCUSDT"].values() for candle in candles)

    asyncio.run(run())


def test_collector_refreshes_tickers_after_kline_collection():
    async def run():
        rest = FakeRest()
        await MarketDataCollector(rest, max_symbols=1).collect_once()
        assert rest.ticker_calls == 2

    asyncio.run(run())


def test_pipeline_stage1_uses_collected_canonical_data():
    async def run():
        batch = await MarketDataCollector(FakeRest(), max_symbols=1).collect_once()
        results = run_stage1(batch, now=NOW)
        assert len(results) == 1
        assert results[0].symbol == "BTCUSDT"

    asyncio.run(run())
