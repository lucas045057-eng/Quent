"""One-cycle Phase 1 collector and Stage1 orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Mapping, Sequence

from .contracts import Candle, Instrument, Ticker
from .stage1 import Stage1Result, evaluate_stage1
from .time import utc_now
from quant_data_layer.freshness import FRESHNESS_POLICY
from .universe import select_universe


INTERVALS = ("5m", "15m", "1H", "4H")


@dataclass(frozen=True, slots=True)
class MarketDataBatch:
    collected_at: datetime
    instruments: list[Instrument]
    tickers: list[Ticker]
    selected_symbols: tuple[str, ...]
    candles_by_symbol: dict[str, dict[str, list[Candle]]]


class MarketDataCollector:
    def __init__(self, rest_client: Any, *, max_symbols: int = 200, concurrency: int = 8, kline_limit: int = 100) -> None:
        self.rest_client = rest_client
        self.max_symbols = max_symbols
        self.kline_limit = kline_limit
        self._semaphore = asyncio.Semaphore(concurrency)

    async def _fetch_symbol(self, symbol: str) -> tuple[str, dict[str, list[Candle]]]:
        async def fetch(interval: str) -> tuple[str, list[Candle]]:
            async with self._semaphore:
                try:
                    return interval, await self.rest_client.get_candles(symbol=symbol, interval=interval, limit=self.kline_limit)
                except Exception:
                    return interval, []

        values = await asyncio.gather(*(fetch(interval) for interval in INTERVALS))
        return symbol, dict(values)

    async def collect_once(self) -> MarketDataBatch:
        instruments = await self.rest_client.get_instruments()
        tickers = await self.rest_client.get_tickers()
        universe = select_universe(instruments, tickers, limit=self.max_symbols)
        candles = dict(await asyncio.gather(*(self._fetch_symbol(item.symbol) for item in universe)))
        tickers = await self.rest_client.get_tickers()
        return MarketDataBatch(utc_now(), instruments, tickers, tuple(item.symbol for item in universe), candles)


def split_market_batch(batch: MarketDataBatch, *, candle_budget: int = 8000):
    """Keep every original record/clock, with bounded canonical write batches."""
    if candle_budget<1:raise ValueError("CANDLE_BUDGET_INVALID")
    yield replace(batch,candles_by_symbol={})
    pending={};count=0
    for symbol,by_interval in batch.candles_by_symbol.items():
        for interval,candles in by_interval.items():
            for candle in candles:
                pending.setdefault(symbol,{}).setdefault(interval,[]).append(candle)
                count+=1
                if count==candle_budget:
                    yield replace(batch,instruments=[],tickers=[],candles_by_symbol=pending)
                    pending={};count=0
    if pending:yield replace(batch,instruments=[],tickers=[],candles_by_symbol=pending)


def run_stage1(
    batch: MarketDataBatch, *, now: datetime | None = None,
    ticker_max_age_seconds: float = FRESHNESS_POLICY["PRICE_STAGE1"].hard_seconds,
) -> list[Stage1Result]:
    at = now or batch.collected_at
    ticker_by_symbol = {ticker.symbol: ticker for ticker in batch.tickers}
    results: list[Stage1Result] = []
    for symbol in batch.selected_symbols:
        ticker = ticker_by_symbol[symbol]
        results.append(evaluate_stage1(
            symbol, ticker, batch.candles_by_symbol.get(symbol, {}), now=at,
            ticker_max_age_seconds=ticker_max_age_seconds,
        ))
    # A is a scarce, explainable handoff to the later analysis stage.  The
    # hard eligibility rules above decide whether a symbol may be A; this
    # deterministic capacity guard prevents a broad market condition from
    # turning the entire universe into high-confidence candidates.
    eligible = [result for result in results if result.category == "A"]
    if len(eligible) <= 5:
        return results
    ranked = sorted(
        eligible,
        key=lambda result: (
            result.key_metrics.get("range_to_atr", 0),
            result.key_metrics.get("turnover24h", 0),
            result.symbol,
        ),
        reverse=True,
    )
    keep = {result.symbol for result in ranked[:5]}
    return [
        result if result.symbol in keep or result.category != "A" else replace(
            result,
            category="B",
            reason="A_CAPACITY_DEFERRED",
            reason_codes=(*result.reason_codes, "A_CAPACITY_DEFERRED"),
        )
        for result in results
    ]


