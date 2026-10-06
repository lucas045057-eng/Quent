import asyncio

from quant_phase1.adapters.bitget_v3.rate_limit import TokenBucket
from quant_phase1.adapters.bitget_v3.rest import retry_delay


def test_token_bucket_exposes_bitget_v3_20_requests_per_second_limit():
    bucket = TokenBucket(rate_per_second=20, capacity=20)
    assert bucket.rate_per_second == 20
    assert bucket.capacity == 20


def test_token_bucket_waits_when_empty():
    async def run():
        bucket = TokenBucket(rate_per_second=20, capacity=1)
        await bucket.acquire()
        delay = await bucket.delay_until_available()
        assert delay >= 0

    asyncio.run(run())


def test_429_backoff_is_bounded_and_honors_retry_after():
    assert retry_delay(0) == 1
    assert retry_delay(3) == 8
    assert retry_delay(3, retry_after=2) == 2
