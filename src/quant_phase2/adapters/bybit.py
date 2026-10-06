"""Bybit V5 public linear derivative adapter."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from ..contracts import ContractType, DataStatus, FundingObservation, InstrumentMetadata, OIObservation
from ..funding import normalize_funding_to_8h
from ..normalization import normalize_open_interest
from ..symbols import SymbolRegistry
from .base import AdapterSchemaError, PublicHTTPAdapter, decimal, timestamp_ms


class BybitV5Adapter(PublicHTTPAdapter):
    BASE = "https://api.bybit.com"
    INSTRUMENTS = "/v5/market/instruments-info"
    TICKERS = "/v5/market/tickers"
    OPEN_INTEREST = "/v5/market/open-interest"
    FUNDING_HISTORY = "/v5/market/funding/history"
    RATE_LIMIT_PER_SECOND = 120.0
    JITTER_SECONDS = 0.005

    def __init__(self, *, session: Any = None, registry: SymbolRegistry | None = None, base_url: str = BASE) -> None:
        super().__init__(base_url, session=session, rate_limit_per_second=self.RATE_LIMIT_PER_SECOND, jitter_seconds=self.JITTER_SECONDS)
        self.registry = registry

    @staticmethod
    def _result(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if payload.get("retCode") != 0:
            raise AdapterSchemaError(f"Bybit retCode={payload.get('retCode')!r}")
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise AdapterSchemaError("Bybit result must be an object")
        return result

    def _canonical(self, symbol: str) -> str | None:
        return self.registry.canonical_for("bybit", symbol) if self.registry else None

    def parse_instruments(self, payload: Mapping[str, Any], fetched_at: datetime) -> list[InstrumentMetadata]:
        result = self._result(payload)
        items = result.get("list")
        if not isinstance(items, list):
            raise AdapterSchemaError("Bybit instruments list must be a list")
        rows: list[InstrumentMetadata] = []
        for item in items:
            if not isinstance(item, Mapping) or item.get("contractType") != "LinearPerpetual":
                continue
            rows.append(
                InstrumentMetadata(
                    exchange="bybit", exchange_symbol=str(item["symbol"]), canonical_symbol=self._canonical(str(item["symbol"])),
                    base_asset=str(item.get("baseCoin", "")) or None, quote_asset=str(item.get("quoteCoin", "")) or None,
                    settle_asset=str(item.get("settleCoin", "")) or None, contract_type=ContractType.PERPETUAL,
                    margin_asset=str(item.get("settleCoin", "")) or None, contract_multiplier=None, contract_size=None,
                    funding_interval_seconds=int(item["fundingInterval"]) * 60 if item.get("fundingInterval") else None,
                    mark_price=None, source_endpoint=self.INSTRUMENTS, fetched_at=fetched_at, exchange_timestamp=None,
                    raw_payload=dict(item),
                )
            )
        return rows

    def parse_ticker(self, payload: Mapping[str, Any], fetched_at: datetime, *, symbol: str, interval_seconds: int | None) -> tuple[OIObservation, FundingObservation]:
        result = self._result(payload)
        items = result.get("list")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], Mapping):
            raise AdapterSchemaError("Bybit ticker list must contain one object")
        item = items[0]
        mark_price = decimal(item["markPrice"], "markPrice")
        exchange_ts = timestamp_ms(payload.get("time"), "Bybit time") if payload.get("time") is not None else None
        raw_oi = decimal(item["openInterest"], "openInterest")
        normalized = normalize_open_interest(raw_value=raw_oi, raw_unit="BASE_ASSET", mark_price=mark_price)
        oi = OIObservation(
            symbol=symbol, canonical_symbol=self._canonical(symbol), exchange="bybit", contract_type=ContractType.PERPETUAL,
            margin_asset="USDT", settle_asset="USDT", raw_open_interest=raw_oi, raw_unit="BASE_ASSET",
            open_interest_base=normalized.open_interest_base, open_interest_quote=normalized.open_interest_quote,
            open_interest_usd=normalized.open_interest_usd, mark_price=mark_price,
            normalization_method=normalized.method, exchange_timestamp=exchange_ts, fetched_at=fetched_at, processed_at=fetched_at,
            status=DataStatus.AVAILABLE, source_endpoint=self.TICKERS, raw_payload=dict(item),
        )
        rate = decimal(item["fundingRate"], "fundingRate") if item.get("fundingRate") not in (None, "") else None
        funding_norm = normalize_funding_to_8h(rate, interval_seconds)
        next_time = timestamp_ms(item["nextFundingTime"], "nextFundingTime") if item.get("nextFundingTime") not in (None, "") else None
        funding = FundingObservation(
            symbol=symbol, canonical_symbol=self._canonical(symbol), exchange="bybit", contract_type=ContractType.PERPETUAL,
            funding_rate=rate, funding_interval_seconds=interval_seconds, normalized_8h_rate=funding_norm.value,
            predicted_funding_rate=None, realized_funding_rate=None, next_funding_time=next_time,
            exchange_timestamp=exchange_ts, fetched_at=fetched_at, processed_at=fetched_at,
            status=DataStatus.AVAILABLE if rate is not None else DataStatus.NOT_AVAILABLE,
            source_endpoint=self.TICKERS, raw_payload=dict(item),
        )
        return oi, funding

    async def fetch_instruments(self, fetched_at: datetime) -> list[InstrumentMetadata]:
        payload = await self.get_json(self.INSTRUMENTS, {"category": "linear", "limit": "1000"})
        return self.parse_instruments(payload, fetched_at)

    async def fetch_ticker(self, symbol: str, fetched_at: datetime, interval_seconds: int | None) -> tuple[OIObservation, FundingObservation]:
        payload = await self.get_json(self.TICKERS, {"category": "linear", "symbol": symbol})
        return self.parse_ticker(payload, fetched_at, symbol=symbol, interval_seconds=interval_seconds)

    async def fetch_oi_history(self, symbol: str, *, interval: str = "5min", limit: int = 200) -> Mapping[str, Any]:
        return await self.get_json(self.OPEN_INTEREST, {"category": "linear", "symbol": symbol, "intervalTime": interval, "limit": str(limit)})

    async def fetch_funding_history(self, symbol: str, *, limit: int = 200) -> Mapping[str, Any]:
        return await self.get_json(self.FUNDING_HISTORY, {"category": "linear", "symbol": symbol, "limit": str(limit)})
