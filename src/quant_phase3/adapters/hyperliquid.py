"""Hyperliquid public ``trades`` WebSocket adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .base import AdapterSchemaError, PublicTradeAdapter, decimal_field, epoch_milliseconds, require_mapping, required_text
from quant_phase3.capabilities import HYPERLIQUID_CAPABILITIES, TradeSourceCapabilities
from quant_phase3.contracts import CanonicalTrade, FlowStatus, SideSource, TradeSide


class HyperliquidPublicTradeAdapter:
    exchange = "hyperliquid"
    ws_url = "wss://api.hyperliquid.xyz/ws"
    capabilities: TradeSourceCapabilities = HYPERLIQUID_CAPABILITIES

    def __init__(self, *, canonical_symbol: str | None = None) -> None:
        self.canonical_symbol = canonical_symbol

    def subscription(self, symbol: str) -> Mapping[str, Any]:
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError("coin is required")
        return {
            "method": "subscribe",
            "subscription": {"type": "trades", "coin": symbol},
        }

    def parse_ws_message(
        self, payload: Mapping[str, Any], received_at: datetime
    ) -> Sequence[CanonicalTrade]:
        message = require_mapping(payload, "websocket message")
        if message.get("channel") != "trades":
            raise AdapterSchemaError("Hyperliquid message must use the public trades channel")
        rows = message.get("data")
        if not isinstance(rows, list) or not rows:
            raise AdapterSchemaError("Hyperliquid trades data must be a non-empty list")
        return tuple(self._parse_row(row, received_at, source_channel="ws:trades") for row in rows)

    def parse_recent_trades(
        self,
        payload: Mapping[str, Any],
        received_at: datetime,
        *,
        exchange_symbol: str | None = None,
    ) -> Sequence[CanonicalTrade]:
        response = require_mapping(payload, "recent-trades response")
        if response.get("type") not in {None, "recentTrades"}:
            raise AdapterSchemaError("unexpected Hyperliquid recent-trades response type")
        rows = response.get("data")
        if not isinstance(rows, list):
            raise AdapterSchemaError("Hyperliquid recent-trades data must be a list")
        trades = [
            self._parse_row(row, received_at, source_channel="rest:info/recentTrades") for row in rows
        ]
        return tuple(sorted(trades, key=lambda trade: (trade.exchange_timestamp, trade.trade_id)))

    def identity_key(self, trade: CanonicalTrade) -> tuple[str, ...]:
        return (trade.exchange, trade.canonical_symbol or trade.exchange_symbol, trade.trade_id)

    def _parse_row(
        self, row: Any, received_at: datetime, *, source_channel: str
    ) -> CanonicalTrade:
        item = require_mapping(row, "trade")
        coin = required_text(item, "coin")
        raw_side = required_text(item, "side")
        if item.get("tid") is None:
            raise AdapterSchemaError("Hyperliquid trade must include tid")
        if not isinstance(item.get("users"), list) or len(item["users"]) != 2:
            raise AdapterSchemaError("Hyperliquid public trade users must be a two-item list")
        block_time = item.get("time")
        exchange_timestamp = epoch_milliseconds(block_time, "time")
        trade_id = f"{int(block_time)}:{coin}:{item['tid']}"
        price = decimal_field(item.get("px"), "px")
        quantity = decimal_field(item.get("sz"), "sz")
        return CanonicalTrade(
            exchange=self.exchange,
            exchange_symbol=coin,
            canonical_symbol=self.canonical_symbol,
            trade_id=trade_id,
            price=price,
            quantity_base=quantity,
            notional_usd=price * quantity,
            aggressor_side=TradeSide.UNKNOWN,
            raw_side=raw_side,
            raw_side_semantics="PUBLIC_TRADE_SIDE_UNCONFIRMED_AGGRESSOR",
            side_source=SideSource.UNKNOWN,
            exchange_timestamp=exchange_timestamp,
            received_at=received_at,
            processed_at=datetime.now(timezone.utc),
            source_channel=source_channel,
            status=FlowStatus.AVAILABLE,
            raw_payload=dict(item),
        )


assert isinstance(HyperliquidPublicTradeAdapter(), PublicTradeAdapter)
