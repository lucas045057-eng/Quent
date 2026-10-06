"""Resolve Phase 1 universe symbols to a canonical perpetual identity.

The resolver uses registered instrument metadata. It never guesses a mapping
from an arbitrary candidate string and has no execution-engine dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from psycopg import Connection


@dataclass(frozen=True, slots=True)
class InstrumentIdentity:
    core_symbol: str
    canonical_symbol: str
    base_asset: str
    quote_asset: str
    contract_type: str
    venue_symbols: tuple[tuple[str, str], ...]

    def venue_symbol(self, venue: str) -> str:
        matches = [symbol for name, symbol in self.venue_symbols if name == venue.lower()]
        if len(matches) != 1:
            raise ValueError(f"venue symbol not registered for {venue!r}")
        return matches[0]


def resolve_core_instrument(conn: Connection[Any], core_symbol: str) -> InstrumentIdentity:
    if not isinstance(core_symbol, str) or not core_symbol:
        raise ValueError("core symbol is required")
    rows = conn.execute(
        """SELECT symbol, category, base_coin, quote_coin, symbol_type,
                  contract_type, status, exchange
             FROM symbols WHERE symbol = %s""",
        (core_symbol,),
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(f"core symbol is not uniquely registered: {core_symbol!r}")
    symbol, category, base, quote, symbol_type, contract_type, status, exchange = rows[0]
    if (
        category != "USDT-FUTURES" or quote != "USDT" or
        str(symbol_type).upper() != "PERPETUAL" or
        str(contract_type).lower() != "perpetual" or
        str(status).lower() != "online" or
        not isinstance(base, str) or not base or
        symbol != f"{base}{quote}"
    ):
        raise ValueError(f"core symbol is not an eligible USDT perpetual: {core_symbol!r}")
    canonical = f"{base}-{quote}-PERP"
    duplicates = conn.execute(
        """SELECT count(*) FROM symbols
           WHERE base_coin = %s AND quote_coin = %s
             AND upper(symbol_type) = 'PERPETUAL'
             AND lower(contract_type) = 'perpetual'
             AND lower(status) = 'online'""",
        (base, quote),
    ).fetchone()[0]
    if duplicates != 1:
        raise ValueError(f"ambiguous canonical instrument: {canonical!r}")
    venues: dict[str, str] = {str(exchange).lower(): symbol} if exchange else {}
    for venue, venue_symbol in conn.execute(
        """SELECT exchange, exchange_symbol FROM exchange_instruments
           WHERE canonical_symbol = %s AND status = 'AVAILABLE'""",
        (canonical,),
    ).fetchall():
        key = str(venue).lower()
        value = str(venue_symbol)
        if key in venues and venues[key] != value:
            raise ValueError(f"conflicting venue symbol for {key!r}")
        venues[key] = value
    return InstrumentIdentity(
        core_symbol=symbol, canonical_symbol=canonical, base_asset=base,
        quote_asset=quote, contract_type="PERPETUAL",
        venue_symbols=tuple(sorted(venues.items())),
    )
