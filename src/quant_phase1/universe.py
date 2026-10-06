"""Phase 1 Top-N eligible instrument universe."""

from __future__ import annotations

from typing import Sequence

from .contracts import Instrument, Ticker, DataStatus


def select_universe(
    instruments: Sequence[Instrument], tickers: Sequence[Ticker], *, limit: int = 200
) -> list[Instrument]:
    ticker_by_symbol = {ticker.symbol: ticker for ticker in tickers}
    eligible: list[tuple[Instrument, Ticker]] = []
    for instrument in instruments:
        ticker = ticker_by_symbol.get(instrument.symbol)
        if ticker is None or ticker.status is not DataStatus.AVAILABLE:
            continue
        if instrument.category != "USDT-FUTURES" or instrument.quote_coin.upper() != "USDT":
            continue
        if instrument.symbol_type.upper() != "PERPETUAL" or instrument.contract_type.lower() != "perpetual":
            continue
        if str(instrument.raw_payload.get("symbolType", "")).lower() != "crypto":
            continue
        if instrument.status.lower() != "online" or instrument.base_coin.upper() in {"RWA"}:
            continue
        eligible.append((instrument, ticker))
    eligible.sort(key=lambda pair: (-pair[1].turnover24h, pair[0].symbol))
    return [instrument for instrument, _ in eligible[:limit]]
