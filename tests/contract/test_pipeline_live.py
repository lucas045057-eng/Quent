import asyncio

from quant_phase1.adapters.bitget_v3.rest import BitgetV3UtaRestClient
from quant_phase1.pipeline import MarketDataCollector, run_stage1


def test_live_public_data_can_complete_one_symbol_collection_cycle():
    async def run():
        async with BitgetV3UtaRestClient() as client:
            batch = await MarketDataCollector(client, max_symbols=1).collect_once()
            assert batch.instruments
            assert batch.tickers
            assert batch.candles_by_symbol
            results = run_stage1(batch, now=batch.collected_at)
            assert results

    asyncio.run(run())
