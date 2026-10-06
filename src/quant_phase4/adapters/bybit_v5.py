"""Bybit V5 public mark/index ticker adapter."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from quant_phase4.basis import compute_basis
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

from .base import (
    AdapterProviderError,
    AdapterSchemaError,
    SharedPublicRESTTransport,
    _request_json,
    canonical_perpetual,
    decimal_field,
    epoch_milliseconds,
    mapping,
    text,
    utc_datetime,
)


def _usdt_symbol(value: Any, field: str) -> str:
    symbol = text(value, field).upper()
    if len(symbol) <= 4 or not symbol.endswith("USDT") or not symbol.isalnum():
        raise AdapterSchemaError(f"{field} must be a valid USDT perpetual symbol")
    return symbol


class BybitV5BasisAdapter:
    exchange = "bybit"
    base_url = "https://api.bybit.com"
    path = "/v5/market/tickers"

    def __init__(self, *, canonical_symbol: str | None = None, settings: Any | None = None) -> None:
        self.canonical_symbol = canonical_symbol
        self.base_url = getattr(settings, "phase4_bybit_rest_base_url", self.base_url).rstrip("/")
        self._transport: SharedPublicRESTTransport | None = None

    def configure_transport(self, transport: SharedPublicRESTTransport) -> None:
        self._transport = transport

    async def fetch(self, symbol: str, *, received_at: datetime) -> BasisObservation:
        if self._transport is None:
            raise RuntimeError("Bybit basis adapter requires a runtime REST transport")
        path, params = self.ticker_request(symbol)
        rows = self.parse_tickers(await self._transport.get_json(path, params), received_at)
        if not rows:
            raise AdapterSchemaError("Bybit ticker data must be non-empty")
        return rows[0]

    def ticker_request(self, symbol: str) -> tuple[str, dict[str, str]]:
        return self.path, {"category": "linear", "symbol": text(symbol, "symbol").upper()}

    def parse_tickers(self, payload: Mapping[str, Any], received_at: datetime) -> Sequence[BasisObservation]:
        received_at = utc_datetime(received_at, "received_at")
        response = mapping(payload, "Bybit response")
        if response.get("retCode") != 0:
            raise AdapterSchemaError(f"unexpected Bybit response code: {response.get('retCode')!r}")
        result = mapping(response.get("result"), "result")
        rows = result.get("list")
        if not isinstance(rows, list):
            raise AdapterSchemaError("Bybit ticker result.list must be a list")
        timestamp = epoch_milliseconds(response.get("time"), "time")
        return tuple(self._parse_row(row, received_at, timestamp) for row in rows)

    def _parse_row(self, row: Any, received_at: datetime, timestamp: datetime) -> BasisObservation:
        item = mapping(row, "Bybit ticker")
        symbol = text(item.get("symbol"), "symbol")
        basis = compute_basis(
            decimal_field(item.get("markPrice"), "markPrice"), decimal_field(item.get("indexPrice"), "indexPrice"),
            basis_type=BasisType.MARK_INDEX, perpetual_timestamp=timestamp, reference_timestamp=timestamp,
            max_timestamp_skew=timedelta(0), source=self.path,
            received_at=received_at, fetched_at=received_at,
        )
        return replace(basis, exchange=self.exchange, exchange_symbol=symbol, canonical_symbol=self.canonical_symbol or canonical_perpetual(symbol), raw_payload=dict(item))


class BybitV5LongShortAdapter:
    """Bybit V5 all-position-holder account-ratio adapter."""

    exchange = "bybit"
    base_url = "https://api.bybit.com"
    path = "/v5/market/account-ratio"
    endpoint_id = "bybit_v5_account_ratio"
    official_periods = frozenset({"5min", "15min", "30min", "1h", "4h", "1d"})

    def __init__(self, *, canonical_symbol: str | None = None, settings: Any | None = None) -> None:
        self.canonical_symbol = canonical_symbol
        self.base_url = getattr(settings, "phase4_bybit_rest_base_url", self.base_url).rstrip("/")
        self._transport: SharedPublicRESTTransport | None = None

    def configure_transport(self, transport: SharedPublicRESTTransport) -> None:
        self._transport = transport

    def long_short_request(self, symbol: str, period: str) -> tuple[str, dict[str, str]]:
        if not isinstance(symbol, str) or not symbol.strip():
            raise AdapterSchemaError("symbol must be a non-empty string")
        if period not in self.official_periods:
            raise AdapterSchemaError("period must be an official Bybit account-ratio period")
        return self.path, {"category": "linear", "symbol": symbol.upper(), "period": period}

    async def fetch(self, symbol: str, period: str, *, received_at: datetime) -> LongShortObservation:
        import aiohttp

        received_at = utc_datetime(received_at, "received_at")
        path, params = self.long_short_request(symbol, period)
        if self._transport is not None:
            payload = await self._transport.get_json(path, params)
        else:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                payload = await _request_json(session, self.base_url, path, params)
        return replace(self._parse_response(payload, received_at=received_at, symbol=symbol, period=period), period=period)

    def parse_response(self, payload: Mapping[str, Any], *, received_at: datetime) -> LongShortObservation:
        return self._parse_response(payload, received_at=received_at, symbol=None, period=None)

    def _parse_response(
        self, payload: Mapping[str, Any], *, received_at: datetime, symbol: str | None, period: str | None
    ) -> LongShortObservation:
        received_at = utc_datetime(received_at, "received_at")
        response = mapping(payload, "Bybit response")
        if response.get("retCode") != 0:
            raise AdapterProviderError(
                "Bybit returned a non-success business code",
                endpoint=self.endpoint_id,
                provider_code=response.get("retCode"),
            )
        result = mapping(response.get("result"), "result")
        rows = result.get("list")
        if not isinstance(rows, list):
            raise AdapterSchemaError("Bybit account-ratio result.list must be a list", field="result.list")
        if not rows:
            exchange_symbol = symbol or result.get("symbol") or "UNKNOWN"
            source_period = period or result.get("period") or "5min"
            if source_period not in self.official_periods:
                raise AdapterSchemaError("period must be an official Bybit account-ratio period", field="period")
            return LongShortObservation(
                exchange=self.exchange,
                exchange_symbol=exchange_symbol,
                canonical_symbol=self.canonical_symbol or canonical_perpetual(exchange_symbol),
                metric_type=LongShortMetricType.ACCOUNT_HOLDER_RATIO,
                population_semantics=LongShortPopulationSemantics.ALL_POSITION_HOLDER_ACCOUNT_RATIO,
                period=source_period,
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
                reason_code=ReasonCode.EMPTY_RESPONSE,
            )
        parsed_rows = tuple(mapping(row, "Bybit account-ratio row") for row in rows)
        row = max(parsed_rows, key=lambda item: epoch_milliseconds(item.get("timestamp"), "timestamp"))
        exchange_symbol = symbol or row.get("symbol") or result.get("symbol") or "UNKNOWN"
        if not isinstance(exchange_symbol, str) or not exchange_symbol.strip():
            raise AdapterSchemaError("symbol must be a non-empty string")
        source_period = period or row.get("period") or result.get("period") or "5min"
        if source_period not in self.official_periods:
            raise AdapterSchemaError("period must be an official Bybit account-ratio period")
        long_value = decimal_field(row.get("buyRatio"), "buyRatio")
        short_value = decimal_field(row.get("sellRatio"), "sellRatio")
        return LongShortObservation(
            exchange=self.exchange,
            exchange_symbol=exchange_symbol,
            canonical_symbol=self.canonical_symbol or canonical_perpetual(exchange_symbol),
            metric_type=LongShortMetricType.ACCOUNT_HOLDER_RATIO,
            population_semantics=LongShortPopulationSemantics.ALL_POSITION_HOLDER_ACCOUNT_RATIO,
            period=source_period,
            long_value=long_value,
            short_value=short_value,
            ratio=long_value / short_value,
            exchange_timestamp=epoch_milliseconds(row.get("timestamp"), "timestamp"),
            fetched_at=received_at,
            received_at=received_at,
            processed_at=received_at,
            source_endpoint=self.path,
            status=DataStatus.AVAILABLE,
            raw_reference=None,
            raw_payload=dict(row),
        )


class BybitV5LiquidationAdapter:
    """Parse documented V5 all-liquidation messages without unit inference."""

    exchange = "bybit"
    public_ws_url = "wss://stream.bybit.com/v5/public/linear"
    topic_prefix = "allLiquidation."

    def __init__(self, *, canonical_symbol: str | None = None) -> None:
        self.canonical_symbol = canonical_symbol

    def subscription(self, symbol: str) -> dict[str, object]:
        return {"op": "subscribe", "args": [f"{self.topic_prefix}{_usdt_symbol(symbol, 'symbol')}"]}

    def parse_ws_message(self, payload: Mapping[str, Any], received_at: datetime) -> Sequence[CanonicalLiquidation]:
        received_at = utc_datetime(received_at, "received_at")
        response = mapping(payload, "Bybit liquidation message")
        topic = text(response.get("topic"), "topic")
        if not topic.startswith(self.topic_prefix) or not topic[len(self.topic_prefix) :]:
            raise AdapterSchemaError("unexpected Bybit liquidation channel")
        if response.get("type") != "snapshot":
            raise AdapterSchemaError("Bybit liquidation type must be snapshot")
        epoch_milliseconds(response.get("ts"), "ts")
        channel_symbol = _usdt_symbol(topic[len(self.topic_prefix) :], "topic symbol")
        rows = response.get("data")
        if not isinstance(rows, list) or not rows:
            raise AdapterSchemaError("Bybit liquidation data must be a non-empty list")
        return tuple(self._parse_row(row, received_at, topic, channel_symbol) for row in rows)

    def _parse_row(
        self, row: Any, received_at: datetime, topic: str, channel_symbol: str
    ) -> CanonicalLiquidation:
        item = mapping(row, "Bybit liquidation row")
        symbol = _usdt_symbol(item.get("s"), "s")
        if symbol != channel_symbol:
            raise AdapterSchemaError("Bybit liquidation symbol must match channel")
        raw_side = text(item.get("S"), "S")
        sides = {"Buy": LiquidationSide.LIQUIDATED_LONG, "Sell": LiquidationSide.LIQUIDATED_SHORT}
        try:
            side = sides[raw_side]
        except KeyError as exc:
            raise AdapterSchemaError("Bybit liquidation S must be Buy or Sell") from exc
        size = decimal_field(item.get("v"), "v")
        bankruptcy_price = decimal_field(item.get("p"), "p")
        timestamp = epoch_milliseconds(item.get("T"), "T")
        return CanonicalLiquidation(
            event_id=f"{symbol}:{timestamp.isoformat()}:{raw_side}:{bankruptcy_price}:{size}",
            exchange=self.exchange,
            exchange_symbol=symbol,
            canonical_symbol=self.canonical_symbol or canonical_perpetual(symbol),
            event_timestamp=timestamp,
            received_at=received_at,
            processed_at=received_at,
            side=side,
            raw_side=raw_side,
            raw_side_semantics="EXCHANGE_PROVIDED_LIQUIDATED_POSITION_SIDE",
            price=bankruptcy_price,
            raw_quantity=size,
            quantity_unit=QuantityUnit.CONTRACTS,
            quantity_base=None,
            notional_usd=None,
            source_endpoint=self.public_ws_url,
            source_channel=topic,
            source_granularity=SourceGranularity.ALL_LIQUIDATIONS_STREAM,
            coverage_semantics=CoverageSemantics.EXCHANGE_DECLARED_ALL_LIQUIDATIONS,
            status=DataStatus.AVAILABLE,
            raw_reference=None,
            raw_payload=dict(item),
        )
