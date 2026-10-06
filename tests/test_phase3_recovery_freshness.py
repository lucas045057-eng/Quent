from datetime import datetime, timedelta, timezone

from quant_phase3.adapters.bybit import BybitPublicTradeAdapter
from quant_phase3.contracts import FlowStatus
from quant_phase3.freshness import TradeFreshnessEvaluator
from quant_phase3.recovery import TradeGapEvent, TradeRecoveryManager


BASE = datetime(2026, 9, 20, 1, 0, tzinfo=timezone.utc)


def rest_payload(*times):
    return {
        "retCode": 0,
        "result": {
            "category": "linear",
            "list": [
                {
                    "execId": f"id-{index}",
                    "symbol": "BTCUSDT",
                    "price": "100",
                    "size": "1",
                    "side": "Buy",
                    "time": str(int(timestamp.timestamp() * 1000)),
                }
                for index, timestamp in enumerate(times)
            ],
        },
    }


def test_recovery_resolves_gap_only_when_bounded_payload_covers_expected_range():
    manager = TradeRecoveryManager(
        BybitPublicTradeAdapter(canonical_symbol="BTC-USDT-PERP"),
        max_backfill_seconds=60,
    )

    result = manager.recover(
        rest_payload(BASE, BASE + timedelta(seconds=30), BASE + timedelta(seconds=60)),
        received_at=BASE + timedelta(seconds=61),
        exchange_symbol="BTCUSDT",
        gap_start=BASE,
        gap_end=BASE + timedelta(seconds=60),
    )

    assert len(result.trades) == 3
    assert result.event.status is FlowStatus.AVAILABLE
    assert result.event.resolved_at == BASE + timedelta(seconds=61)


def test_recovery_emits_trade_gap_when_coverage_is_empty_or_window_too_large():
    manager = TradeRecoveryManager(
        BybitPublicTradeAdapter(canonical_symbol="BTC-USDT-PERP"),
        max_backfill_seconds=60,
    )

    result = manager.recover(
        rest_payload(BASE + timedelta(seconds=20)),
        received_at=BASE + timedelta(seconds=61),
        exchange_symbol="BTCUSDT",
        gap_start=BASE,
        gap_end=BASE + timedelta(seconds=60),
    )

    assert result.trades == ()
    assert result.event.status is FlowStatus.PARTIAL
    assert result.event.reason == "REST_COVERAGE_INSUFFICIENT"
    assert result.event.resolved_at is None
    assert result.event.affected_window_open == BASE


def test_freshness_distinguishes_available_stale_partial_not_available_and_error():
    evaluator = TradeFreshnessEvaluator()

    assert evaluator.evaluate(
        now=BASE + timedelta(seconds=20),
        last_trade_at=BASE,
        last_complete_window_at=BASE,
        trade_stale_seconds=60,
        complete_window_stale_seconds=120,
        source_connected=True,
    ).status is FlowStatus.AVAILABLE
    assert evaluator.evaluate(
        now=BASE + timedelta(seconds=61),
        last_trade_at=BASE,
        last_complete_window_at=BASE,
        trade_stale_seconds=60,
        complete_window_stale_seconds=120,
        source_connected=True,
    ).status is FlowStatus.STALE
    assert evaluator.evaluate(
        now=BASE + timedelta(seconds=20),
        last_trade_at=BASE,
        last_complete_window_at=BASE,
        trade_stale_seconds=60,
        complete_window_stale_seconds=120,
        source_connected=True,
        has_gap=True,
    ).status is FlowStatus.PARTIAL
    assert evaluator.evaluate(
        now=BASE,
        last_trade_at=None,
        last_complete_window_at=None,
        trade_stale_seconds=60,
        complete_window_stale_seconds=120,
        source_connected=True,
    ).status is FlowStatus.NOT_AVAILABLE
    assert evaluator.evaluate(
        now=BASE,
        last_trade_at=BASE,
        last_complete_window_at=BASE,
        trade_stale_seconds=60,
        complete_window_stale_seconds=120,
        source_connected=False,
    ).status is FlowStatus.ERROR


def test_freshness_rejects_naive_timestamps_and_stale_data_is_not_signal_eligible():
    evaluator = TradeFreshnessEvaluator()
    result = evaluator.evaluate(
        now=BASE,
        last_trade_at=datetime(2026, 9, 20, 1, 0),
        last_complete_window_at=BASE,
        trade_stale_seconds=60,
        complete_window_stale_seconds=120,
        source_connected=True,
    )

    assert result.status is FlowStatus.ERROR
    assert result.reason == "NON_UTC_TIMESTAMP"
    assert result.signal_eligible is False
