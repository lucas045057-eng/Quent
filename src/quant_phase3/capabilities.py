"""Immutable public-trade source capability declarations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TradeSourceCapabilities:
    supports_public_trade_stream: bool
    supports_trade_id: bool
    supports_aggressor_side: bool
    supports_recent_trade_backfill: bool
    supports_directional_flow: bool
    supports_cvd: bool


BYBIT_CAPABILITIES = TradeSourceCapabilities(
    supports_public_trade_stream=True,
    supports_trade_id=True,
    supports_aggressor_side=True,
    supports_recent_trade_backfill=True,
    supports_directional_flow=True,
    supports_cvd=True,
)

BITGET_CAPABILITIES = TradeSourceCapabilities(
    supports_public_trade_stream=True,
    supports_trade_id=True,
    supports_aggressor_side=False,
    supports_recent_trade_backfill=True,
    supports_directional_flow=False,
    supports_cvd=False,
)

HYPERLIQUID_CAPABILITIES = TradeSourceCapabilities(
    supports_public_trade_stream=True,
    supports_trade_id=True,
    supports_aggressor_side=False,
    supports_recent_trade_backfill=True,
    supports_directional_flow=False,
    supports_cvd=False,
)


_CAPABILITIES = {
    "bybit": BYBIT_CAPABILITIES,
    "bitget": BITGET_CAPABILITIES,
    "hyperliquid": HYPERLIQUID_CAPABILITIES,
}


def get_trade_source_capabilities(exchange: str) -> TradeSourceCapabilities:
    key = exchange.strip().lower()
    try:
        return _CAPABILITIES[key]
    except KeyError as exc:
        raise ValueError(f"unknown trade source: {exchange}") from exc
