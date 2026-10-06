from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_phase1.contracts import Candle, DataStatus
from quant_phase1.market.indicators import compute_indicators


def _candles(count: int = 20) -> list[Candle]:
    base = datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc)
    result = []
    for index in range(count):
        close = Decimal(100 + index)
        result.append(
            Candle(
                symbol="BTCUSDT",
                interval="5m",
                bar_open_timestamp=base + timedelta(minutes=5 * index),
                open=close - Decimal("0.5"),
                high=close + Decimal("1"),
                low=close - Decimal("1"),
                close=close,
                volume=Decimal("10"),
                turnover=close * Decimal("10"),
                exchange_timestamp=base + timedelta(minutes=5 * index),
                fetched_at=base,
                processed_at=base,
                status=DataStatus.AVAILABLE,
                is_closed=True,
                raw_payload=[],
            )
        )
    return result


def test_indicators_are_deterministic_and_local():
    first = compute_indicators(_candles())
    second = compute_indicators(_candles())
    assert first == second
    assert first["ema_fast"] > first["ema_slow"]
    assert first["atr"] > 0
    assert first["last_close"] == Decimal("119")
