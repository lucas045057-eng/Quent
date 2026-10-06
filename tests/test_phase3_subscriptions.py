from datetime import datetime, timedelta, timezone

from quant_phase3.capabilities import TradeSourceCapabilities
from quant_phase3.subscriptions import (
    SubscriptionCandidate,
    TradeSubscriptionManager,
    select_candidates,
)


NOW = datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc)


def candidate(symbol, rank, *, bybit=None, bitget=None, stage1=False):
    return SubscriptionCandidate(
        canonical_symbol=symbol,
        rank=rank,
        stage1_ab=stage1,
        exchange_symbols={
            key: value
            for key, value in (("bybit", bybit), ("bitget", bitget))
            if value is not None
        },
    )


def test_candidate_union_is_deterministic_and_stage1_ab_does_not_create_fallback_symbols():
    universe = [candidate("ETH-USDT-PERP", 2, bybit="ETHUSDT"), candidate("BTC-USDT-PERP", 1, bybit="BTCUSDT")]
    stage1 = [candidate("SOL-USDT-PERP", 3, bybit="SOLUSDT", stage1=True)]

    selected = select_candidates(universe, stage1, max_symbols=2)

    assert [row.canonical_symbol for row in selected] == ["BTC-USDT-PERP", "ETH-USDT-PERP"]
    assert all(row.canonical_symbol != "SOL-USDT-PERP" for row in selected)


def test_stage1_ab_candidate_can_enter_when_it_has_better_priority():
    universe = [candidate("ETH-USDT-PERP", 10, bybit="ETHUSDT")]
    stage1 = [candidate("SOL-USDT-PERP", 1, bybit="SOLUSDT", stage1=True)]

    selected = select_candidates(universe, stage1, max_symbols=1)

    assert [row.canonical_symbol for row in selected] == ["SOL-USDT-PERP"]


def test_subscription_manager_subscribes_deterministically_and_applies_dwell_cooldown():
    manager = TradeSubscriptionManager(
        max_symbols=2,
        min_subscription_seconds=60,
        cooldown_seconds=30,
        capabilities=TradeSourceCapabilities(True, True, False, True, False, False),
    )
    desired = [candidate("BTC-USDT-PERP", 1, bybit="BTCUSDT"), candidate("ETH-USDT-PERP", 2, bybit="ETHUSDT")]

    first = manager.refresh("bybit", desired, now=NOW)
    pending = manager.refresh("bybit", [desired[1]], now=NOW + timedelta(seconds=61))
    still_pending = manager.refresh("bybit", [desired[1]], now=NOW + timedelta(seconds=90))
    removed = manager.refresh("bybit", [desired[1]], now=NOW + timedelta(seconds=91))

    assert [(row.action, row.symbol) for row in first] == [("SUBSCRIBE", "BTCUSDT"), ("SUBSCRIBE", "ETHUSDT")]
    assert pending == ()
    assert still_pending == ()
    assert [(row.action, row.symbol) for row in removed] == [("UNSUBSCRIBE", "BTCUSDT")]


def test_reappearing_candidate_cancels_pending_unsubscribe_and_exchange_state_isolated():
    manager = TradeSubscriptionManager(
        max_symbols=1,
        min_subscription_seconds=1,
        cooldown_seconds=10,
        capabilities=TradeSourceCapabilities(True, True, False, True, False, False),
    )
    bybit = candidate("BTC-USDT-PERP", 1, bybit="BTCUSDT")
    bitget = candidate("BTC-USDT-PERP", 1, bitget="BTCUSDT")
    manager.refresh("bybit", [bybit], now=NOW)
    manager.refresh("bitget", [bitget], now=NOW)

    manager.refresh("bybit", [], now=NOW + timedelta(seconds=2))
    actions = manager.refresh("bybit", [bybit], now=NOW + timedelta(seconds=5))

    assert actions == ()
    assert manager.active_symbols("bybit") == ("BTCUSDT",)
    assert manager.active_symbols("bitget") == ("BTCUSDT",)


def test_subscription_manager_refuses_sources_without_public_trade_capability():
    manager = TradeSubscriptionManager(
        max_symbols=1,
        min_subscription_seconds=1,
        cooldown_seconds=1,
        capabilities=TradeSourceCapabilities(False, False, False, False, False, False),
    )

    assert manager.refresh("disabled", [candidate("BTC-USDT-PERP", 1, bybit="BTCUSDT")], now=NOW) == ()
