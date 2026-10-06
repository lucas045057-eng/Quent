from datetime import datetime, timezone
from decimal import Decimal

from quant_phase1.contracts import Candle, DataStatus
from quant_phase1.market.closed_bars import accept_closed_candle


def _candle(open_time: datetime) -> Candle:
    return Candle(
        symbol="BTCUSDT",
        interval="5m",
        bar_open_timestamp=open_time,
        open=Decimal("99"),
        high=Decimal("102"),
        low=Decimal("98"),
        close=Decimal("101"),
        volume=Decimal("10"),
        turnover=Decimal("1000"),
        exchange_timestamp=open_time,
        fetched_at=open_time,
        processed_at=open_time,
        status=DataStatus.AVAILABLE,
        is_closed=True,
        raw_payload=[],
    )


def test_only_candles_whose_interval_has_closed_are_accepted():
    now = datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc)
    assert accept_closed_candle(_candle(datetime(2026, 9, 20, 10, 20, tzinfo=timezone.utc)), now) is True
    assert accept_closed_candle(_candle(datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc)), now) is False


def test_declared_open_bar_is_rejected_even_if_payload_claims_closed():
    candle = _candle(datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc))
    now = datetime(2026, 9, 20, 10, 31, tzinfo=timezone.utc)
    assert accept_closed_candle(candle, now) is False
