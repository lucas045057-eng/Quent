from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from quant_data_layer.freshness import (
    FRESHNESS_POLICY,
    assess_freshness,
    bitget_price_source_clock_skew_tolerance,
    measure_lags,
)


def test_policy_is_data_type_and_stage_aware_and_keeps_execution_strict():
    expected = {
        "PRICE_STAGE1", "PRICE_DECISION", "PRICE_EXECUTION", "TRADE_FLOW",
        "OPEN_INTEREST", "FUNDING", "LIQUIDATION",
    }
    assert set(FRESHNESS_POLICY) == expected
    assert FRESHNESS_POLICY["PRICE_STAGE1"].hard_seconds == 60
    assert FRESHNESS_POLICY["PRICE_DECISION"].hard_seconds == 60
    assert FRESHNESS_POLICY["PRICE_EXECUTION"].hard_seconds == 5
    assert FRESHNESS_POLICY["OPEN_INTEREST"].expected_cadence_seconds == 300
    assert FRESHNESS_POLICY["FUNDING"].expected_cadence_seconds == 300


def test_lag_measurement_keeps_event_ingest_and_processing_lags_separate():
    base = datetime(2026, 9, 29, tzinfo=timezone.utc)
    observed = measure_lags(
        now=base + timedelta(seconds=30),
        source_event_time=base,
        fetched_at=base + timedelta(seconds=10),
        processed_at=base + timedelta(seconds=12),
    )
    assert observed == {
        "source_event_age": 30.0,
        "ingest_lag": 10.0,
        "processing_lag": 2.0,
        "observation_age": 20.0,
    }


def test_missing_source_time_does_not_get_relabelled_as_source_event_age():
    base = datetime(2026, 9, 29, tzinfo=timezone.utc)
    observed = measure_lags(
        now=base + timedelta(seconds=30),
        source_event_time=None,
        fetched_at=base + timedelta(seconds=20),
        processed_at=base + timedelta(seconds=22),
    )
    assert observed["source_event_age"] is None
    assert observed["ingest_lag"] is None
    assert observed["processing_lag"] == 2.0
    assert observed["observation_age"] == 10.0


def test_soft_degraded_and_hard_stale_bands_do_not_use_collector_heartbeat():
    policy = FRESHNESS_POLICY["PRICE_STAGE1"]
    assert assess_freshness(20, policy) == "FRESH"
    assert assess_freshness(45, policy) == "DEGRADED"
    assert assess_freshness(60.001, policy) == "STALE"
    assert assess_freshness(-0.1, policy) == "INVALID_FUTURE_TIMESTAMP"
    assert assess_freshness(
        -1.5, policy, future_skew_tolerance_seconds=2.0,
    ) == "FRESH"
    assert assess_freshness(
        -2.001, policy, future_skew_tolerance_seconds=2.0,
    ) == "INVALID_FUTURE_TIMESTAMP"
    assert assess_freshness(
        -2.5, policy, future_skew_tolerance_seconds=2.0,
    ) == "INVALID_FUTURE_TIMESTAMP"
    assert assess_freshness(
        -1.5, FRESHNESS_POLICY["TRADE_FLOW"], future_skew_tolerance_seconds=2.0,
    ) == "INVALID_FUTURE_TIMESTAMP"
    assert bitget_price_source_clock_skew_tolerance(
        "PRICE_STAGE1", "bitget", "bitget_v3_rest",
    ) == 2.0
    assert bitget_price_source_clock_skew_tolerance(
        "PRICE_STAGE1", "BITGET", "bitget_v3_ws",
    ) == 2.0
    assert bitget_price_source_clock_skew_tolerance(
        "PRICE_STAGE1", "binance", "bitget_v3_ws",
    ) == 0.0
    assert bitget_price_source_clock_skew_tolerance(
        "PRICE_DECISION", "bitget", "bitget_v3_ws",
    ) == 0.0
    assert bitget_price_source_clock_skew_tolerance(
        "PRICE_EXECUTION", "bitget", "bitget_v3_ws",
    ) == 2.0


def test_stage1_15m_to_4h_strategy_can_accept_a_30s_ticker_with_explicit_policy(monkeypatch):
    from quant_phase1.contracts import Candle, DataStatus, Ticker
    from quant_phase1.stage1 import evaluate_stage1
    from quant_phase1 import stage1

    now = datetime(2026, 1, 1, 4, 0, 30, tzinfo=timezone.utc)
    ticker = Ticker(
        "BTCUSDT", Decimal("100"), Decimal("99.9"), Decimal("100.1"),
        Decimal("1"), Decimal("1"), Decimal("1000"), Decimal("100000"),
        Decimal("100"), Decimal("100"), now - timedelta(seconds=30), now, now,
        DataStatus.AVAILABLE, {}, source="bitget_v3_ws", exchange="bitget",
    )
    boundary = now.replace(second=0)
    opens = {
        "5m": (boundary - timedelta(minutes=5), boundary - timedelta(minutes=10)),
        "15m": (boundary - timedelta(minutes=15),),
        "1H": (boundary - timedelta(hours=1), boundary - timedelta(hours=2)),
        "4H": (boundary - timedelta(hours=4),),
    }
    seconds = {"5m": 300, "15m": 900, "1H": 3600, "4H": 14400}
    candles = {}
    for interval, timestamps in opens.items():
        candles[interval] = [
            Candle("BTCUSDT", interval, opened, Decimal("99"), Decimal("101"), Decimal("98"),
                   Decimal("100"), Decimal("10"), Decimal("1000"), opened, now, now,
                   DataStatus.AVAILABLE, True, [], source="bitget_v3_ws", exchange="bitget")
            for opened in sorted(timestamps)
        ]
    monkeypatch.setattr(stage1, "compute_indicators", lambda _bars: {
        "atr": Decimal("1"), "range_high": Decimal("105"), "range_low": Decimal("95"),
        "ema_fast": Decimal("101"), "ema_slow": Decimal("100"),
    })
    monkeypatch.setattr(stage1, "classify_structure", lambda _bars: SimpleNamespace(trend="BULLISH"))

    result = evaluate_stage1("BTCUSDT", ticker, candles, now=now)
    assert result.status is DataStatus.AVAILABLE
    assert result.category in {"A", "B", "C"}

    slightly_future = replace(ticker, exchange_timestamp=now + timedelta(seconds=1.5))
    future_result = evaluate_stage1("BTCUSDT", slightly_future, candles, now=now)
    assert future_result.status is DataStatus.AVAILABLE

    too_far_future = replace(ticker, exchange_timestamp=now + timedelta(seconds=2.001))
    rejected_result = evaluate_stage1("BTCUSDT", too_far_future, candles, now=now)
    assert rejected_result.status is DataStatus.STALE
    assert seconds == {"5m": 300, "15m": 900, "1H": 3600, "4H": 14400}


def test_phase9_price_rule_cannot_widen_the_price_decision_hard_limit():
    from types import SimpleNamespace
    from quant_phase9.decision import _freshness_rule_age_limit

    rule = SimpleNamespace(source_type="PRICE_TICKER", maximum_age_seconds=900)
    assert _freshness_rule_age_limit(rule) == FRESHNESS_POLICY["PRICE_DECISION"].hard_seconds

def test_phase9_manifest_cannot_widen_each_canonical_data_type_hard_limit():
    from quant_phase9.decision import _freshness_rule_age_limit

    cases = {
        "OPEN_INTEREST": ("15m", 600),
        "FUNDING_RATE": ("15m", 600),
        "TRADE_FLOW_WINDOW": ("15m", 905),
        "CLOSED_KLINE": ("15m", 960),
        "LIQUIDATION_EVENT": ("15m", 60),
    }
    for source_type, (timeframe, expected) in cases.items():
        rule = SimpleNamespace(source_type=source_type, maximum_age_seconds=86400)
        assert _freshness_rule_age_limit(rule, timeframe=timeframe) == expected
def test_stage1_hard_ticker_limit_cannot_be_widened_by_a_caller():
    from decimal import Decimal
    from quant_phase1.contracts import DataStatus, Ticker
    from quant_phase1.stage1 import evaluate_stage1

    now = datetime(2026, 9, 29, 10, 0, 0, tzinfo=timezone.utc)
    ticker = Ticker(
        "BTCUSDT", Decimal("100"), Decimal("99.9"), Decimal("100.1"),
        Decimal("1"), Decimal("1"), Decimal("1000"), Decimal("100000"),
        Decimal("100"), Decimal("100"), now-timedelta(seconds=61), now-timedelta(seconds=1), now,
        DataStatus.AVAILABLE, {}, source="bitget_v3_ws", exchange="bitget",
    )
    result = evaluate_stage1("BTCUSDT", ticker, {}, now=now, ticker_max_age_seconds=86400)
    assert result.status is DataStatus.STALE


def test_runtime_price_stage1_feed_uses_source_scoped_future_skew():
    from quant_realtime_paper.runtime import _feed

    now = datetime(2026, 9, 29, tzinfo=timezone.utc)
    source = "bitget_v3_ws"
    tolerance = bitget_price_source_clock_skew_tolerance(
        "PRICE_STAGE1", "bitget", source,
    )
    accepted = _feed(
        "TICKER_MARK", "BTCUSDT", source, now + timedelta(seconds=1.5), now,
        now, True, data_type="PRICE_STAGE1",
        policy=FRESHNESS_POLICY["PRICE_STAGE1"],
        future_skew_tolerance_seconds=tolerance,
    )
    rejected = _feed(
        "TICKER_MARK", "BTCUSDT", source, now + timedelta(seconds=2.001), now,
        now, True, data_type="PRICE_STAGE1",
        policy=FRESHNESS_POLICY["PRICE_STAGE1"],
        future_skew_tolerance_seconds=tolerance,
    )
    assert accepted["status"] == "HEALTHY"
    assert rejected["status"] == "INVALID"
