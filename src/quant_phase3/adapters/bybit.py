"""Bybit V5 public linear trade adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .base import AdapterSchemaError, PublicTradeAdapter, decimal_field, epoch_milliseconds, require_mapping, required_text
from quant_phase3.capabilities import BYBIT_CAPABILITIES, TradeSourceCapabilities
from quant_phase3.contracts import CanonicalTrade, FlowStatus, SideSource, TradeSide


class BybitPublicTradeAdapter:
    exchange = "bybit"
    ws_url = "wss://stream.bybit.com/v5/public/linear"
    rest_path = "/v5/market/recent-trade"
    capabilities: TradeSourceCapabilities = BYBIT_CAPABILITIES

    def __init__(self, *, canonical_symbol: str | None = None) -> None:
        self.canonical_symbol = canonical_symbol

    def subscription(self, symbol: str) -> Mapping[str, Any]:
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        return {"op": "subscribe", "args": [f"publicTrade.{symbol}"]}

    def recent_trade_request(self, symbol: str, *, limit: int = 1000) -> tuple[str, dict[str, str]]:
        if not 1 <= limit <= 1000:
            raise ValueError("Bybit recent-trade limit must be between 1 and 1000")
        return self.rest_path, {"category": "linear", "symbol": symbol.strip().upper(), "limit": str(limit)}

    def parse_ws_message(
        self, payload: Mapping[str, Any], received_at: datetime
    ) -> Sequence[CanonicalTrade]:
        message = require_mapping(payload, "websocket message")
        topic = required_text(message, "topic")
        if not topic.startswith("publicTrade."):
            raise AdapterSchemaError("Bybit message topic must be publicTrade.<symbol>")
        rows = message.get("data")
        if not isinstance(rows, list) or not rows:
            raise AdapterSchemaError("Bybit public trade data must be a non-empty list")
        return tuple(self._parse_row(row, received_at, source_channel=topic) for row in rows)

    def parse_recent_trades(
        self,
        payload: Mapping[str, Any],
        received_at: datetime,
        *,
        exchange_symbol: str | None = None,
    ) -> Sequence[CanonicalTrade]:
        response = require_mapping(payload, "REST response")
        if response.get("retCode") != 0:
            raise AdapterSchemaError(f"unexpected Bybit retCode: {response.get('retCode')!r}")
        result = require_mapping(response.get("result"), "result")
        if result.get("category") != "linear":
            raise AdapterSchemaError("Bybit recent-trade category must be linear")
        rows = result.get("list")
        if not isinstance(rows, list):
            raise AdapterSchemaError("Bybit recent-trade list must be a list")
        trades = [
            self._parse_row(row, received_at, source_channel="rest:/v5/market/recent-trade", rest=True)
            for row in rows
        ]
        return tuple(sorted(trades, key=lambda trade: (trade.exchange_timestamp, trade.trade_id)))

    def identity_key(self, trade: CanonicalTrade) -> tuple[str, ...]:
        return (trade.exchange, trade.canonical_symbol or trade.exchange_symbol, trade.trade_id)

    def _parse_row(
        self, row: Any, received_at: datetime, *, source_channel: str, rest: bool = False
    ) -> CanonicalTrade:
        item = require_mapping(row, "trade")
        side = required_text(item, "side" if rest else "S")
        try:
            aggressor_side = {"Buy": TradeSide.BUY, "Sell": TradeSide.SELL}[side]
        except KeyError as exc:
            raise AdapterSchemaError(f"unsupported Bybit side: {side!r}") from exc
        trade_id = item.get("execId" if rest else "i") or item.get("i" if rest else "execId")
        if not isinstance(trade_id, str) or not trade_id.strip():
            raise AdapterSchemaError("Bybit trade must include i")
        price_field = "price" if rest else "p"
        quantity_field = "size" if rest else "v"
        timestamp_field = "time" if rest else "T"
        symbol_field = "symbol" if rest else "s"
        price = decimal_field(item.get(price_field), price_field)
        quantity = decimal_field(item.get(quantity_field), quantity_field)
        return CanonicalTrade(
            exchange=self.exchange,
            exchange_symbol=required_text(item, symbol_field),
            canonical_symbol=self.canonical_symbol,
            trade_id=trade_id,
            price=price,
            quantity_base=quantity,
            notional_usd=price * quantity,
            aggressor_side=aggressor_side,
            raw_side=side,
            raw_side_semantics="EXCHANGE_PROVIDED_TAKER_SIDE",
            side_source=SideSource.EXCHANGE_PROVIDED,
            exchange_timestamp=epoch_milliseconds(item.get(timestamp_field), timestamp_field),
            received_at=received_at.astimezone(timezone.utc),
            processed_at=datetime.now(timezone.utc),
            source_channel=source_channel,
            status=FlowStatus.AVAILABLE,
            raw_payload=dict(item),
        )


assert isinstance(BybitPublicTradeAdapter(), PublicTradeAdapter)
