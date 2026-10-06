from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from quant_phase3.contracts import CanonicalTrade, FlowStatus, SideSource, TradeSide
from quant_phase3.dedup import BoundedTradeDeduplicator
from quant_phase3.ordering import EventTimeWindowRouter


BASE = datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc)


def trade(exchange="bybit", trade_id="1", timestamp=BASE):
    return CanonicalTrade(
        exchange=exchange,
        exchange_symbol="BTCUSDT",
        canonical_symbol="BTC-USDT-PERP",
        trade_id=trade_id,
        price=Decimal("100"),
        quantity_base=Decimal("1"),
        notional_usd=Decimal("100"),
        aggressor_side=TradeSide.BUY,
        raw_side="Buy",
        raw_side_semantics="EXCHANGE_PROVIDED_TAKER_SIDE",
        side_source=SideSource.EXCHANGE_PROVIDED,
        exchange_timestamp=timestamp,
        received_at=timestamp,
        processed_at=timestamp,
        source_channel="test",
        status=FlowStatus.AVAILABLE,
    )


def test_deduplicator_collapses_same_adapter_identity_and_has_bounded_per_exchange_state():
    dedup = BoundedTradeDeduplicator(
        identity_key=lambda item: (item.exchange, item.trade_id),
        max_entries_per_exchange=2,
        ttl_seconds=60,
    )

    assert dedup.add(trade(trade_id="1"), now=BASE) is True
    assert dedup.add(trade(trade_id="1"), now=BASE + timedelta(seconds=1)) is False
    assert dedup.add(trade(trade_id="2"), now=BASE + timedelta(seconds=1)) is True
    assert dedup.add(trade(trade_id="3"), now=BASE + timedelta(seconds=1)) is True
    assert dedup.size("bybit") == 2
    assert dedup.duplicate_count == 1

    assert dedup.add(trade(trade_id="1"), now=BASE + timedelta(seconds=2)) is True


def test_deduplicator_expires_entries_and_does_not_mix_exchange_buckets():
    dedup = BoundedTradeDeduplicator(
        identity_key=lambda item: (item.exchange, item.trade_id),
        max_entries_per_exchange=2,
        ttl_seconds=10,
    )

    assert dedup.add(trade(exchange="bybit", trade_id="1"), now=BASE) is True
    assert dedup.add(trade(exchange="bitget", trade_id="1"), now=BASE) is True
    assert dedup.add(trade(exchange="bybit", trade_id="1"), now=BASE + timedelta(seconds=11)) is True
    assert dedup.size("bitget") == 1
    assert dedup.total_size == 2


def test_deduplicator_expiration_checks_only_the_expired_prefix():
    class CountingDateTime(datetime):
        timestamp_calls = 0

        def timestamp(self):
            type(self).timestamp_calls += 1
            return super().timestamp()

    dedup = BoundedTradeDeduplicator(
        identity_key=lambda item: (item.exchange, item.trade_id),
        max_entries_per_exchange=512,
        ttl_seconds=60,
    )
    start = CountingDateTime.fromtimestamp(BASE.timestamp(), tz=timezone.utc)
    for index in range(256):
        assert dedup.add(trade(trade_id=f"bulk-{index}"), now=start) is True

    CountingDateTime.timestamp_calls = 0
    later = CountingDateTime.fromtimestamp(
        (BASE + timedelta(seconds=1)).timestamp(), tz=timezone.utc,
    )
    assert dedup.add(trade(trade_id="new"), now=later) is True
    assert CountingDateTime.timestamp_calls <= 2


def test_duplicate_moves_lru_but_does_not_extend_first_seen_ttl():
    dedup = BoundedTradeDeduplicator(
        identity_key=lambda item: (item.exchange, item.trade_id),
        max_entries_per_exchange=2,
        ttl_seconds=10,
    )
    assert dedup.add(trade(trade_id="1"), now=BASE) is True
    assert dedup.add(trade(trade_id="2"), now=BASE + timedelta(seconds=1)) is True
    assert dedup.add(trade(trade_id="1"), now=BASE + timedelta(seconds=5)) is False
    assert dedup.add(trade(trade_id="3"), now=BASE + timedelta(seconds=6)) is True
    assert dedup.size("bybit") == 2

    # The duplicate refreshes LRU ordering, but the original first-seen TTL
    # still determines when this key expires.
    assert dedup.add(trade(trade_id="1"), now=BASE + timedelta(seconds=10)) is True


def test_event_router_accepts_on_time_trade_and_rejects_late_finalized_window():
    router = EventTimeWindowRouter(window_seconds=60, allowed_lateness_seconds=5)
    current = trade(timestamp=BASE + timedelta(seconds=20))

    accepted = router.route(current)
    finalized = router.advance_watermark(BASE + timedelta(seconds=66))
    late = router.route(trade(trade_id="late", timestamp=BASE + timedelta(seconds=10)))

    assert accepted.accepted is True
    assert accepted.window_open == BASE
    assert finalized == (BASE,)
    assert late.accepted is False
    assert late.status is FlowStatus.PARTIAL
    assert late.reason == "LATE_TRADE"


def test_event_router_marks_gap_windows_partial_without_rewriting_finalized_data():
    router = EventTimeWindowRouter(window_seconds=60, allowed_lateness_seconds=5)
    window_open = BASE + timedelta(minutes=1)

    router.mark_gap(window_open, reason="REST_COVERAGE_INSUFFICIENT")
    result = router.route(trade(trade_id="gap", timestamp=window_open + timedelta(seconds=10)))

    assert result.accepted is False
    assert result.status is FlowStatus.PARTIAL
    assert result.reason == "REST_COVERAGE_INSUFFICIENT"


def test_event_router_requires_utc_event_time():
    router = EventTimeWindowRouter(window_seconds=60, allowed_lateness_seconds=5)

    result = router.route(SimpleNamespace(exchange_timestamp=datetime(2026, 9, 20, 1, 0)))

    assert result.accepted is False
    assert result.status is FlowStatus.ERROR
    assert result.reason == "NON_UTC_EVENT_TIME"
