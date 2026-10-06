"""Bitget UTA v3 public ticker and liquidation adapters."""

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
    QuantityUnit,
    SourceGranularity,
)

from .base import AdapterSchemaError, SharedPublicRESTTransport, canonical_perpetual, decimal_field, epoch_milliseconds, mapping, text, utc_datetime


def _usdt_symbol(value: Any, field: str) -> str:
    symbol = text(value, field).upper()
    if len(symbol) <= 4 or not symbol.endswith("USDT") or not symbol.isalnum():
        raise AdapterSchemaError(f"{field} must be a valid USDT perpetual symbol")
    return symbol


class BitgetUTA3BasisAdapter:
    exchange = "bitget"
    base_url = "https://api.bitget.com"
    path = "/api/v3/market/tickers"

    def __init__(self, *, canonical_symbol: str | None = None, settings: Any | None = None) -> None:
        self.canonical_symbol = canonical_symbol
        self.base_url = getattr(settings, "phase4_bitget_uta_rest_base_url", self.base_url).rstrip("/")
        self._transport: SharedPublicRESTTransport | None = None

    def configure_transport(self, transport: SharedPublicRESTTransport) -> None:
        self._transport = transport

    async def fetch(self, symbol: str | None = None, *, received_at: datetime) -> BasisObservation:
        if self._transport is None:
            raise RuntimeError("Bitget UTA basis adapter requires a runtime REST transport")
        path, params = self.ticker_request()
        rows = self.parse_tickers(await self._transport.get_json(path, params), received_at)
        if symbol is not None:
            rows = tuple(row for row in rows if row.exchange_symbol == symbol.upper())
        if not rows:
            raise AdapterSchemaError("Bitget ticker data must be non-empty")
        return rows[0]

    def ticker_request(self) -> tuple[str, dict[str, str]]:
        return self.path, {"category": "USDT-FUTURES"}

    def parse_tickers(self, payload: Mapping[str, Any], received_at: datetime) -> Sequence[BasisObservation]:
        received_at = utc_datetime(received_at, "received_at")
        response = mapping(payload, "Bitget response")
        if response.get("code") != "00000":
            raise AdapterSchemaError(f"unexpected Bitget response code: {response.get('code')!r}")
        rows = response.get("data")
        if not isinstance(rows, list):
            raise AdapterSchemaError("Bitget ticker data must be a list")
        fallback_timestamp = response.get("requestTime")
        return tuple(self._parse_row(row, received_at, fallback_timestamp) for row in rows)

    def _parse_row(self, row: Any, received_at: datetime, fallback_timestamp: Any) -> BasisObservation:
        item = mapping(row, "Bitget ticker")
        symbol = text(item.get("symbol"), "symbol")
        timestamp = epoch_milliseconds(item.get("ts", fallback_timestamp), "ts")
        basis = compute_basis(
            decimal_field(item.get("markPrice"), "markPrice"), decimal_field(item.get("indexPrice"), "indexPrice"),
            basis_type=BasisType.MARK_INDEX, perpetual_timestamp=timestamp, reference_timestamp=timestamp,
            max_timestamp_skew=timedelta(0),
            source=self.path, received_at=received_at, fetched_at=received_at,
        )
        return replace(basis, exchange=self.exchange, exchange_symbol=symbol, canonical_symbol=self.canonical_symbol or canonical_perpetual(symbol), raw_payload=dict(item))


class BitgetUTA3LiquidationAdapter:
    """Parse the documented UTA v3 one-second maximum liquidation feed."""

    exchange = "bitget"
    public_ws_url = "wss://ws.bitget.com/v3/ws/public"
    inst_type = "usdt-futures"
    topic = "liquidation"

    def __init__(self, *, canonical_symbol: str | None = None) -> None:
        self.canonical_symbol = canonical_symbol

    def subscription(self, symbol: str) -> dict[str, object]:
        return {
            "op": "subscribe",
            "args": [{"instType": self.inst_type, "topic": self.topic, "symbol": _usdt_symbol(symbol, "symbol")}],
        }

    def parse_ws_message(self, payload: Mapping[str, Any], received_at: datetime) -> Sequence[CanonicalLiquidation]:
        received_at = utc_datetime(received_at, "received_at")
        response = mapping(payload, "Bitget liquidation message")
        arg = mapping(response.get("arg"), "arg")
        if arg.get("instType") != self.inst_type or arg.get("topic") != self.topic:
            raise AdapterSchemaError("unexpected Bitget liquidation channel")
        arg_symbol = arg.get("symbol")
        # The UTA liquidation feed is an aggregated topic in the live API:
        # subscription ACKs echo the requested symbol, while update messages
        # may omit arg.symbol and identify the instrument in each data row.
        expected_symbol = _usdt_symbol(arg_symbol, "arg.symbol") if arg_symbol is not None else None
        rows = response.get("data")
        if not isinstance(rows, list) or not rows:
            raise AdapterSchemaError("Bitget liquidation data must be a non-empty list")
        return tuple(self._parse_row(row, received_at, expected_symbol) for row in rows)

    def _parse_row(self, row: Any, received_at: datetime, expected_symbol: str | None) -> CanonicalLiquidation:
        item = mapping(row, "Bitget liquidation row")
        symbol = _usdt_symbol(item.get("symbol"), "symbol")
        if expected_symbol is not None and symbol != expected_symbol:
            raise AdapterSchemaError("Bitget liquidation symbol must match channel")
        raw_side = text(item.get("side"), "side")
        sides = {"buy": LiquidationSide.LIQUIDATED_LONG, "sell": LiquidationSide.LIQUIDATED_SHORT}
        try:
            side = sides[raw_side]
        except KeyError as exc:
            raise AdapterSchemaError("Bitget liquidation side must be buy or sell") from exc
        price = decimal_field(item.get("price"), "price")
        amount = decimal_field(item.get("amount"), "amount")
        timestamp = epoch_milliseconds(item.get("ts"), "ts")
        return CanonicalLiquidation(
            event_id=f"{symbol}:{timestamp.isoformat()}:{raw_side}:{price}:{amount}",
            exchange=self.exchange,
            exchange_symbol=symbol,
            canonical_symbol=self.canonical_symbol or canonical_perpetual(symbol),
            event_timestamp=timestamp,
            received_at=received_at,
            processed_at=received_at,
            side=side,
            raw_side=raw_side,
            raw_side_semantics="EXCHANGE_PROVIDED_LIQUIDATED_POSITION_SIDE",
            price=price,
            raw_quantity=amount,
            quantity_unit=QuantityUnit.QUOTE_COIN,
            quantity_base=None,
            notional_usd=None,
            source_endpoint=self.public_ws_url,
            source_channel=self.topic,
            source_granularity=SourceGranularity.AGGREGATED_MAX_PER_SECOND,
            coverage_semantics=CoverageSemantics.PARTIAL_AGGREGATED,
            status=DataStatus.AVAILABLE,
            raw_reference=None,
            raw_payload=dict(item),
        )
