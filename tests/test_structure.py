from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_phase1.contracts import Candle, DataStatus
from quant_phase1.market.structure import classify_structure


def _candle(index: int, high: int, low: int, close: int) -> Candle:
    timestamp = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc) + timedelta(hours=index)
    return Candle(
        symbol="BTCUSDT",
        interval="1H",
        bar_open_timestamp=timestamp,
        open=Decimal(close - 1),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("10"),
        turnover=Decimal("1000"),
        exchange_timestamp=timestamp,
        fetched_at=timestamp,
        processed_at=timestamp,
        status=DataStatus.AVAILABLE,
        is_closed=True,
        raw_payload=[],
    )


def test_structure_identifies_higher_highs_and_higher_lows():
    snapshot = classify_structure([_candle(0, 101, 98, 100), _candle(1, 103, 99, 102), _candle(2, 105, 101, 104)])
    assert snapshot.trend == "BULLISH"
    assert snapshot.higher_highs == 2
    assert snapshot.higher_lows == 2


def test_structure_identifies_range_when_no_directional_progress():
    snapshot = classify_structure([_candle(0, 101, 99, 100), _candle(1, 101, 99, 100), _candle(2, 101, 99, 100)])
    assert snapshot.trend == "RANGE"
