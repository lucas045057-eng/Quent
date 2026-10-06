"""Hyperliquid public mark/oracle adapter with explicit timestamp discipline."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from quant_phase4.contracts import (
    BasisObservation,
    BasisType,
    CanonicalLiquidation,
    CoverageSemantics,
    DataStatus,
    LiquidationSide,
    LongShortMetricType,
    LongShortObservation,
    LongShortPopulationSemantics,
    QuantityUnit,
    ReasonCode,
    SourceGranularity,
)

from .base import AdapterSchemaError, canonical_perpetual, decimal_field, mapping, text, utc_datetime


class HyperliquidPublicBasisAdapter:
    exchange = "hyperliquid"
    base_url = "https://api.hyperliquid.xyz"
    path = "/info"

    def __init__(self, *, canonical_symbol: str | None = None) -> None:
        self.canonical_symbol = canonical_symbol

    def info_request(self) -> tuple[str, dict[str, str]]:
        return self.path, {"type": "metaAndAssetCtxs"}

    def parse_meta_and_asset_contexts(self, payload: Any, received_at: datetime) -> Sequence[BasisObservation]:
        received_at = utc_datetime(received_at, "received_at")
        if not isinstance(payload, list) or len(payload) != 2:
            raise AdapterSchemaError("Hyperliquid metaAndAssetCtxs response must be a two-item list")
        meta = mapping(payload[0], "meta")
        universe = meta.get("universe")
        contexts = payload[1]
        if not isinstance(universe, list) or not isinstance(contexts, list) or len(universe) != len(contexts):
            raise AdapterSchemaError("Hyperliquid universe and asset contexts must be matching lists")
        return tuple(self._unavailable(name, context, received_at) for name, context in zip(universe, contexts))

    def _unavailable(self, asset: Any, context: Any, received_at: datetime) -> BasisObservation:
        name = text(mapping(asset, "universe item").get("name"), "name")
        raw_context = dict(mapping(context, "asset context"))
        decimal_field(raw_context.get("markPx"), "markPx")
        decimal_field(raw_context.get("oraclePx"), "oraclePx")
        current = received_at
        return BasisObservation(
            exchange=self.exchange, exchange_symbol=name, canonical_symbol=self.canonical_symbol or canonical_perpetual(name),
            basis_type=BasisType.MARK_ORACLE, perpetual_price=None, reference_price=None, absolute_basis=None,
            basis_bps=None, basis_pct=None, exchange_timestamp=current, fetched_at=current, received_at=current,
            processed_at=current, max_timestamp_skew=timedelta(0), timestamp_skew=timedelta(0),
            source_endpoint=self.path, status=DataStatus.NOT_AVAILABLE, raw_reference=None, raw_payload=raw_context,
            reason_code=ReasonCode.UNCONFIRMED_SEMANTICS,
        )


class HyperliquidPublicLongShortAdapter:
    """Explicitly unavailable: no confirmed anonymous public long/short source."""

    exchange = "hyperliquid"
    path = "/info"

    def __init__(self, *, canonical_symbol: str | None = None) -> None:
        self.canonical_symbol = canonical_symbol

    def unavailable(self, symbol: str, period: str, *, received_at: datetime) -> LongShortObservation:
        received_at = utc_datetime(received_at, "received_at")
        symbol = text(symbol, "symbol")
        period = text(period, "period")
        return LongShortObservation(
            exchange=self.exchange,
            exchange_symbol=symbol,
            canonical_symbol=self.canonical_symbol or canonical_perpetual(symbol),
            metric_type=LongShortMetricType.ACCOUNT_HOLDER_RATIO,
            population_semantics=LongShortPopulationSemantics.UNCONFIRMED_PUBLIC_SOURCE,
            period=period,
            long_value=None,
            short_value=None,
            ratio=None,
            exchange_timestamp=received_at,
            fetched_at=received_at,
            received_at=received_at,
            processed_at=received_at,
            source_endpoint=self.path,
            status=DataStatus.NOT_AVAILABLE,
            raw_reference=None,
            raw_payload=None,
            reason_code=ReasonCode.UNCONFIRMED_PUBLIC_SOURCE,
        )


class UnavailableLiquidationAdapter:
    """Explicitly unavailable: Hyperliquid has no verified anonymous liquidation feed."""

    exchange = "hyperliquid"
    source_endpoint = "/info"

    def __init__(self, *, canonical_symbol: str | None = None) -> None:
        self.canonical_symbol = canonical_symbol

    def unavailable(self, symbol: str, *, received_at: datetime) -> CanonicalLiquidation:
        received_at = utc_datetime(received_at, "received_at")
        symbol = text(symbol, "symbol")
        return CanonicalLiquidation(
            event_id=f"UNAVAILABLE:{symbol}",
            exchange=self.exchange,
            exchange_symbol=symbol,
            canonical_symbol=self.canonical_symbol or canonical_perpetual(symbol),
            event_timestamp=received_at,
            received_at=received_at,
            processed_at=received_at,
            side=LiquidationSide.UNKNOWN,
            raw_side=None,
            raw_side_semantics="UNCONFIRMED_PUBLIC_SOURCE",
            price=None,
            raw_quantity=None,
            quantity_unit=QuantityUnit.UNKNOWN,
            quantity_base=None,
            notional_usd=None,
            source_endpoint=self.source_endpoint,
            source_channel="unavailable",
            source_granularity=SourceGranularity.NOT_AVAILABLE,
            coverage_semantics=CoverageSemantics.NOT_AVAILABLE,
            status=DataStatus.NOT_AVAILABLE,
            raw_reference=None,
            raw_payload=None,
            reason_code=ReasonCode.UNCONFIRMED_PUBLIC_SOURCE,
        )
