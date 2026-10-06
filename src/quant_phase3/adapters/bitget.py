"""Bitget UTA v3 public-trade adapter.

The public ``side`` field is retained as exchange evidence only.  Bitget's
documented field is not promoted to aggressor direction in Phase 3.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .base import AdapterSchemaError, PublicTradeAdapter, decimal_field, epoch_milliseconds, require_mapping, required_text
from quant_phase3.capabilities import BITGET_CAPABILITIES, TradeSourceCapabilities
from quant_phase3.contracts import CanonicalTrade, FlowStatus, SideSource, TradeSide


class BitgetUTA3PublicTradeAdapter:
    exchange = "bitget"
    ws_url = "wss://ws.bitget.com/v3/ws/public"
    rest_path = "/api/v3/market/fills"
    capabilities: TradeSourceCapabilities = BITGET_CAPABILITIES

    def __init__(self, *, canonical_symbol: str | None = None) -> None:
        self.canonical_symbol = canonical_symbol

    def subscription(self, symbol: str) -> Mapping[str, Any]:
        symbol = symbol.strip().upper()
        if not symbol:
            raise ValueError("symbol is required")
        return {
            "op": "subscribe",
            "args": [{"instType": "usdt-futures", "topic": "publicTrade", "symbol": symbol}],
        }

    def recent_trade_request(self, symbol: str, *, limit: int = 100) -> tuple[str, dict[str, str]]:
        if not 1 <= limit <= 100:
            raise ValueError("Bitget market fills limit must be between 1 and 100")
        return self.rest_path, {
            "category": "USDT-FUTURES",
            "symbol": symbol.strip().upper(),
            "limit": str(limit),
        }

    def parse_ws_message(
        self, payload: Mapping[str, Any], received_at: datetime
    ) -> Sequence[CanonicalTrade]:
        message = require_mapping(payload, "websocket message")
        arg = require_mapping(message.get("arg"), "arg")
        if arg.get("instType") != "usdt-futures" or arg.get("topic") != "publicTrade":
            raise AdapterSchemaError("Bitget message must use the UTA v3 publicTrade topic")
        symbol = required_text(arg, "symbol")
        rows = message.get("data")
        if not isinstance(rows, list) or not rows:
            raise AdapterSchemaError("Bitget public trade data must be a non-empty list")
        return tuple(
            self._parse_row(
                row,
                received_at,
                symbol=symbol,
                source_channel="ws:v3/publicTrade",
                websocket=True,
            )
            for row in rows
        )

    def parse_recent_trades(
        self,
        payload: Mapping[str, Any],
        received_at: datetime,
        *,
        exchange_symbol: str | None = None,
    ) -> Sequence[CanonicalTrade]:
        response = require_mapping(payload, "REST response")
        if response.get("code") != "00000":
            raise AdapterSchemaError(f"unexpected Bitget response code: {response.get('code')!r}")
        rows = response.get("data")
        if not isinstance(rows, list):
            raise AdapterSchemaError("Bitget market fills data must be a list")
        trades = [
            self._parse_row(
                row,
                received_at,
                symbol=exchange_symbol,
                source_channel="rest:/api/v3/market/fills",
                websocket=False,
            )
            for row in rows
        ]
        return tuple(sorted(trades, key=lambda trade: (trade.exchange_timestamp, trade.trade_id)))

    def identity_key(self, trade: CanonicalTrade) -> tuple[str, ...]:
        return (trade.exchange, trade.canonical_symbol or trade.exchange_symbol, trade.trade_id)

    def _parse_row(
        self,
        row: Any,
        received_at: datetime,
        *,
        symbol: str | None,
        source_channel: str,
        websocket: bool,
    ) -> CanonicalTrade:
        item = require_mapping(row, "trade")
        side = required_text(item, "S" if websocket else "side").lower()
        if side not in {"buy", "sell"}:
            raise AdapterSchemaError(f"unsupported Bitget trade side: {side!r}")
        trade_id = required_text(item, "i" if websocket else "execId")
        price_field = "p" if websocket else "price"
        quantity_field = "v" if websocket else "size"
        timestamp_field = "T" if websocket else "ts"
        price = decimal_field(item.get(price_field), price_field)
        quantity = decimal_field(item.get(quantity_field), quantity_field)
        exchange_symbol = symbol or str(item.get("symbol", ""))
        if not exchange_symbol.strip():
            raise AdapterSchemaError("Bitget trade symbol is required")
        return CanonicalTrade(
            exchange=self.exchange,
            exchange_symbol=exchange_symbol,
            canonical_symbol=self.canonical_symbol,
            trade_id=trade_id,
            price=price,
            quantity_base=quantity,
            notional_usd=price * quantity,
            aggressor_side=TradeSide.UNKNOWN,
            raw_side=side,
            raw_side_semantics="TRADE_SIDE_UNCONFIRMED_AGGRESSOR",
            side_source=SideSource.UNKNOWN,
            exchange_timestamp=epoch_milliseconds(item.get(timestamp_field), timestamp_field),
            received_at=received_at.astimezone(timezone.utc),
            processed_at=datetime.now(timezone.utc),
            source_channel=source_channel,
            status=FlowStatus.AVAILABLE,
            raw_payload=dict(item),
        )


assert isinstance(BitgetUTA3PublicTradeAdapter(), PublicTradeAdapter)
