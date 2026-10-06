"""Bounded dynamic public-trade subscription selection and lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping

from .capabilities import TradeSourceCapabilities


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("subscription timestamps must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class SubscriptionCandidate:
    canonical_symbol: str
    rank: int
    stage1_ab: bool
    exchange_symbols: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class SubscriptionAction:
    action: str
    exchange: str
    symbol: str
    canonical_symbol: str
    occurred_at: datetime


@dataclass(slots=True)
class _ActiveSubscription:
    candidate: SubscriptionCandidate
    started_at: datetime
    pending_since: datetime | None = None


def select_candidates(
    universe: Iterable[SubscriptionCandidate],
    stage1_ab: Iterable[SubscriptionCandidate],
    *,
    max_symbols: int,
) -> tuple[SubscriptionCandidate, ...]:
    if max_symbols <= 0:
        raise ValueError("max_symbols must be positive")
    merged: dict[str, SubscriptionCandidate] = {}
    for candidate in (*tuple(universe), *tuple(stage1_ab)):
        existing = merged.get(candidate.canonical_symbol)
        if existing is None:
            merged[candidate.canonical_symbol] = candidate
            continue
        symbols = dict(existing.exchange_symbols)
        symbols.update(candidate.exchange_symbols)
        merged[candidate.canonical_symbol] = SubscriptionCandidate(
            canonical_symbol=candidate.canonical_symbol,
            rank=min(existing.rank, candidate.rank),
            stage1_ab=existing.stage1_ab or candidate.stage1_ab,
            exchange_symbols=symbols,
        )
    return tuple(sorted(merged.values(), key=lambda item: (item.rank, item.canonical_symbol))[:max_symbols])


class TradeSubscriptionManager:
    def __init__(
        self,
        *,
        max_symbols: int,
        min_subscription_seconds: int,
        cooldown_seconds: int,
        capabilities: TradeSourceCapabilities,
    ) -> None:
        if max_symbols <= 0 or min_subscription_seconds <= 0 or cooldown_seconds <= 0:
            raise ValueError("subscription limits must be positive")
        self.max_symbols = max_symbols
        self.min_subscription_seconds = min_subscription_seconds
        self.cooldown_seconds = cooldown_seconds
        self.capabilities = capabilities
        self._active: dict[str, dict[str, _ActiveSubscription]] = {}

    def refresh(
        self,
        exchange: str,
        candidates: Iterable[SubscriptionCandidate],
        *,
        now: datetime,
    ) -> tuple[SubscriptionAction, ...]:
        current = _utc(now)
        if not self.capabilities.supports_public_trade_stream:
            return ()
        desired = sorted(
            (candidate for candidate in candidates if candidate.exchange_symbols.get(exchange)),
            key=lambda item: (item.rank, item.canonical_symbol),
        )[: self.max_symbols]
        desired_by_symbol = {
            candidate.exchange_symbols[exchange]: candidate
            for candidate in desired
        }
        active = self._active.setdefault(exchange, {})
        actions: list[SubscriptionAction] = []
        for symbol, candidate in desired_by_symbol.items():
            existing = active.get(symbol)
            if existing is None:
                active[symbol] = _ActiveSubscription(candidate=candidate, started_at=current)
                actions.append(SubscriptionAction("SUBSCRIBE", exchange, symbol, candidate.canonical_symbol, current))
            else:
                existing.candidate = candidate
                existing.pending_since = None
        for symbol, existing in list(active.items()):
            if symbol in desired_by_symbol:
                continue
            existing.pending_since = existing.pending_since or current
            dwell_elapsed = (current - existing.started_at).total_seconds() >= self.min_subscription_seconds
            cooldown_elapsed = (current - existing.pending_since).total_seconds() >= self.cooldown_seconds
            if dwell_elapsed and cooldown_elapsed:
                del active[symbol]
                actions.append(
                    SubscriptionAction("UNSUBSCRIBE", exchange, symbol, existing.candidate.canonical_symbol, current)
                )
        return tuple(sorted(actions, key=lambda item: (item.action, item.symbol)))

    def active_symbols(self, exchange: str) -> tuple[str, ...]:
        return tuple(sorted(self._active.get(exchange, {})))
