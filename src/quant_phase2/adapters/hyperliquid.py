"""Hyperliquid public info endpoint adapter for the first perp DEX."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from ..contracts import ContractType, DataStatus, FundingObservation, InstrumentMetadata, OIObservation
from ..funding import normalize_funding_to_8h
from ..normalization import normalize_open_interest
from ..symbols import SymbolRegistry
from .base import AdapterSchemaError, PublicHTTPAdapter, decimal, timestamp_ms


class HyperliquidAdapter(PublicHTTPAdapter):
    INFO = "/info"
    RATE_LIMIT_PER_SECOND = 5.0
    JITTER_SECONDS = 0.05

    def __init__(self, *, session: Any = None, registry: SymbolRegistry | None = None, base_url: str = "https://api.hyperliquid.xyz") -> None:
        super().__init__(base_url, session=session, rate_limit_per_second=self.RATE_LIMIT_PER_SECOND, jitter_seconds=self.JITTER_SECONDS)
        self.registry = registry

    def _canonical(self, symbol: str) -> str | None:
        return self.registry.canonical_for("hyperliquid", symbol) if self.registry else None

    @staticmethod
    def _asset_contexts(payload: Any) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
        if not isinstance(payload, list) or len(payload) != 2 or not isinstance(payload[0], Mapping) or not isinstance(payload[1], list):
            raise AdapterSchemaError("Hyperliquid metaAndAssetCtxs must be [meta, contexts]")
        universe = payload[0].get("universe")
        if not isinstance(universe, list) or not all(isinstance(item, Mapping) for item in universe):
            raise AdapterSchemaError("Hyperliquid meta universe must be a list")
        return universe, [item for item in payload[1] if isinstance(item, Mapping)]

    def parse_meta_and_contexts(self, payload: Any, fetched_at: datetime) -> tuple[list[InstrumentMetadata], list[OIObservation], list[FundingObservation]]:
        universe, contexts = self._asset_contexts(payload)
        if len(universe) != len(contexts):
            raise AdapterSchemaError("Hyperliquid universe/context lengths differ")
        instruments: list[InstrumentMetadata] = []
        oi_rows: list[OIObservation] = []
        funding_rows: list[FundingObservation] = []
        for meta, ctx in zip(universe, contexts):
            coin = str(meta["name"])
            if ":" in coin or meta.get("isDelisted"):
                continue
            mark_price = decimal(ctx["markPx"], "markPx") if ctx.get("markPx") is not None else None
            raw_oi = decimal(ctx["openInterest"], "openInterest") if ctx.get("openInterest") is not None else None
            normalized = normalize_open_interest(raw_value=raw_oi, raw_unit="BASE_ASSET", mark_price=mark_price)
            canonical = self._canonical(coin)
            instruments.append(
                InstrumentMetadata(
                    exchange="hyperliquid", exchange_symbol=coin, canonical_symbol=canonical,
                    base_asset=coin, quote_asset="USDT", settle_asset="USDC", contract_type=ContractType.PERPETUAL,
                    margin_asset="USDC", contract_multiplier=Decimal("1"), contract_size=Decimal("1"),
                    funding_interval_seconds=3600, mark_price=mark_price, source_endpoint=self.INFO,
                    fetched_at=fetched_at, exchange_timestamp=None, raw_payload=dict(meta),
                )
            )
            oi_rows.append(
                OIObservation(
                    symbol=coin, canonical_symbol=canonical, exchange="hyperliquid", contract_type=ContractType.PERPETUAL,
                    margin_asset="USDC", settle_asset="USDC", raw_open_interest=raw_oi, raw_unit="BASE_ASSET",
                    open_interest_base=normalized.open_interest_base, open_interest_quote=normalized.open_interest_quote,
                    open_interest_usd=normalized.open_interest_usd, mark_price=mark_price,
                    normalization_method=normalized.method, exchange_timestamp=None, fetched_at=fetched_at, processed_at=fetched_at,
                    status=DataStatus.AVAILABLE if raw_oi is not None else DataStatus.NOT_AVAILABLE,
                    source_endpoint=self.INFO, raw_payload=dict(ctx),
                )
            )
            rate = decimal(ctx["funding"], "funding") if ctx.get("funding") is not None else None
            funding_norm = normalize_funding_to_8h(rate, 3600)
            funding_rows.append(
                FundingObservation(
                    symbol=coin, canonical_symbol=canonical, exchange="hyperliquid", contract_type=ContractType.PERPETUAL,
                    funding_rate=rate, funding_interval_seconds=3600, normalized_8h_rate=funding_norm.value,
                    predicted_funding_rate=None, realized_funding_rate=None, next_funding_time=None,
                    exchange_timestamp=None, fetched_at=fetched_at, processed_at=fetched_at,
                    status=DataStatus.AVAILABLE if rate is not None else DataStatus.NOT_AVAILABLE,
                    source_endpoint=self.INFO, raw_payload=dict(ctx),
                )
            )
        return instruments, oi_rows, funding_rows

    async def fetch_meta_and_contexts(self, fetched_at: datetime) -> tuple[list[InstrumentMetadata], list[OIObservation], list[FundingObservation]]:
        payload = await self.post_json(self.INFO, {"type": "metaAndAssetCtxs"})
        return self.parse_meta_and_contexts(payload, fetched_at)

    async def fetch_funding_history(self, coin: str, *, start_time_ms: int, end_time_ms: int | None = None) -> Any:
        body: dict[str, Any] = {"type": "fundingHistory", "coin": coin, "startTime": start_time_ms}
        if end_time_ms is not None:
            body["endTime"] = end_time_ms
        return await self.post_json(self.INFO, body)
