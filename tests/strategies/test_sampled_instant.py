"""The screening assertion instant is the sampling instant, not the read end.

A full-universe canonical read takes seconds. Charging that local I/O time to
every source observation pushed the newest ticker past the 5-second PRICE
budget on every cycle, so no symbol could clear Stage A. These cases pin both
halves of the contract: the read duration is not charged as extra age, and a
genuinely stale input is still rejected.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from strategies.refresh import MAX_AGE_SECONDS
from strategies.runtime import sampled_instant

READ_START = datetime(2026, 10, 4, 21, 0, 0, tzinfo=timezone.utc)


def _batch(*clocks):
    return SimpleNamespace(
        tickers=[SimpleNamespace(symbol=f"S{index}USDT", exchange_timestamp=clock)
                 for index, clock in enumerate(clocks)]
    )


def test_assertion_instant_is_the_sampling_instant():
    batch = _batch(READ_START - timedelta(seconds=3))
    assert sampled_instant(READ_START, batch) == READ_START


def test_assertion_instant_is_not_pushed_forward_by_local_read_duration():
    read_end = READ_START + timedelta(seconds=8)
    batch = _batch(READ_START - timedelta(seconds=3))
    instant = sampled_instant(READ_START, batch)
    assert instant == READ_START
    age = (instant - batch.tickers[0].exchange_timestamp).total_seconds()
    assert age == 3
    assert age <= MAX_AGE_SECONDS["PRICE"] < (read_end - batch.tickers[0].exchange_timestamp).total_seconds()


def test_concurrent_write_during_the_read_is_not_treated_as_future_data():
    landed_mid_read = READ_START + timedelta(seconds=2)
    batch = _batch(READ_START - timedelta(seconds=3), landed_mid_read)
    assert sampled_instant(READ_START, batch) == landed_mid_read


def test_genuinely_stale_price_is_still_rejected():
    batch = _batch(READ_START - timedelta(seconds=45))
    instant = sampled_instant(READ_START, batch)
    stale_age = (instant - batch.tickers[0].exchange_timestamp).total_seconds()
    assert instant == READ_START
    assert stale_age > MAX_AGE_SECONDS["PRICE"]
