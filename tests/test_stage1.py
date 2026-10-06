from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_phase1.contracts import Candle, DataStatus, Ticker
from quant_phase1.stage1 import evaluate_stage1


NOW = datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc)


def _candles(interval: str, last_open: datetime, count: int = 20) -> list[Candle]:
    seconds = {"5m": 300, "15m": 900, "1H": 3600, "4H": 14400}[interval]
    result = []
    first = last_open - timedelta(seconds=seconds * (count - 1))
    for index in range(count):
        timestamp = first + timedelta(seconds=seconds * index)
        close = Decimal(100 + index)
        result.append(Candle("BTCUSDT", interval, timestamp, close - 1, close + 1, close - 2, close, Decimal("10"), close * 10, timestamp, NOW, NOW, DataStatus.AVAILABLE, True, []))
    return result


def _ticker() -> Ticker:
    return Ticker("BTCUSDT", Decimal("119"), Decimal("118.9"), Decimal("119.1"), Decimal("2"), Decimal("2"), Decimal("1000"), Decimal("100000"), Decimal("119"), Decimal("119"), NOW, NOW, NOW, DataStatus.AVAILABLE, {})


def test_stage1_is_deterministic_and_does_not_need_advanced_data():
    candles = {
        "5m": _candles("5m", datetime(2026, 9, 20, 10, 25, tzinfo=timezone.utc)),
        "15m": _candles("15m", datetime(2026, 9, 20, 10, 15, tzinfo=timezone.utc)),
        "1H": _candles("1H", datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)),
        "4H": _candles("4H", datetime(2026, 9, 20, 4, 0, tzinfo=timezone.utc)),
    }
    first = evaluate_stage1("BTCUSDT", _ticker(), candles, now=NOW)
    second = evaluate_stage1("BTCUSDT", _ticker(), candles, now=NOW)
    assert first == second
    assert first.category in {"A", "B", "C", "D"}
    assert "open_interest" not in first.inputs_used
    assert "funding_rate" not in first.inputs_used


def test_stale_required_kline_prevents_stage1_candidate():
    candles = {interval: _candles(interval, last) for interval, last in {
        "5m": datetime(2026, 9, 20, 10, 20, tzinfo=timezone.utc),
        "15m": datetime(2026, 9, 20, 10, 15, tzinfo=timezone.utc),
        "1H": datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc),
        "4H": datetime(2026, 9, 20, 4, 0, tzinfo=timezone.utc),
    }.items()}
    result = evaluate_stage1("BTCUSDT", _ticker(), candles, now=NOW, grace_seconds={"5m": 0, "15m": 60, "1H": 120, "4H": 180})
    assert result.category == "D"
    assert result.reason == "REQUIRED_DATA_NOT_FRESH"


def test_stage1_result_has_explainable_classification_and_metrics():
    candles = {
        "5m": _candles("5m", datetime(2026, 9, 20, 10, 25, tzinfo=timezone.utc)),
        "15m": _candles("15m", datetime(2026, 9, 20, 10, 15, tzinfo=timezone.utc)),
        "1H": _candles("1H", datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)),
        "4H": _candles("4H", datetime(2026, 9, 20, 4, 0, tzinfo=timezone.utc)),
    }
    result = evaluate_stage1("BTCUSDT", _ticker(), candles, now=NOW)
    assert result.classification in {"DEEP_ANALYSIS", "WAIT_TRIGGER", "NO_EDGE", "REJECT"}
    assert result.reason_codes
    assert "spread_ratio" in result.key_metrics
    assert result.timestamp == NOW
    assert "open_interest" not in result.inputs_used


def test_stage1_missing_data_is_reject_and_stale_is_not_available():
    result = evaluate_stage1("BTCUSDT", _ticker(), {}, now=NOW)
    assert result.category == "D"
    assert result.classification == "REJECT"
    assert result.reason_codes == ("REQUIRED_DATA_NOT_FRESH",)
