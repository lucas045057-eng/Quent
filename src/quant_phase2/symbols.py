"""Explicit exchange-symbol registry for crypto perpetuals."""

from __future__ import annotations

from typing import Iterable, Mapping

from .contracts import CanonicalSymbol, ContractType


class SymbolRegistry:
    def __init__(self, symbols: Iterable[CanonicalSymbol]) -> None:
        self._by_exchange_symbol = {
            (exchange.lower(), exchange_symbol): symbol
            for symbol in symbols
            for exchange, exchange_symbol in symbol.exchange_symbols.items()
        }

    def resolve(self, exchange: str, exchange_symbol: str) -> CanonicalSymbol:
        return self._by_exchange_symbol[(exchange.lower(), exchange_symbol)]

    def canonical_for(self, exchange: str, exchange_symbol: str) -> str | None:
        try:
            return self.resolve(exchange, exchange_symbol).canonical_symbol
        except KeyError:
            return None


def registry_from_phase1_symbols(rows: Iterable[Mapping[str, object]]) -> SymbolRegistry:
    """Build explicit cross-exchange mappings from the Phase 1 Universe.

    Phase 1 is the authority for eligibility and ranking. The Phase 2
    registry only derives exchange spellings after that selection; it never
    invents a second universe or falls back to a hard-coded BTC/ETH list.
    """
    symbols: list[CanonicalSymbol] = []
    seen: set[str] = set()
    for row in rows:
        exchange_symbol = str(row.get("symbol") or "").upper()
        base = str(row.get("base_coin") or "").upper()
        quote = str(row.get("quote_coin") or "").upper()
        status = str(row.get("status") or "").lower()
        contract_type = str(row.get("contract_type") or "").lower()
        if not exchange_symbol or not base or quote != "USDT" or status != "online" or contract_type != "perpetual":
            continue
        canonical = f"{base}-USDT-PERP"
        if canonical in seen:
            continue
        seen.add(canonical)
        symbols.append(
            CanonicalSymbol(
                canonical_symbol=canonical,
                base_asset=base,
                quote_asset="USDT",
                settle_asset="USDT",
                contract_type=ContractType.PERPETUAL,
                exchange_symbols={"bitget": exchange_symbol, "bybit": exchange_symbol, "hyperliquid": base},
            )
        )
    return SymbolRegistry(symbols)
