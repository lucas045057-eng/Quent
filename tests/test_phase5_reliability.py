import pytest

from quant_phase5.reliability import BoundedContextCache, RetryPolicy, bounded_chunks, run_with_retry


def test_bounded_chunks_and_cache_do_not_load_unbounded_history():
    assert list(bounded_chunks(range(5), 2)) == [(0, 1), (2, 3), (4,)]
    cache = BoundedContextCache(capacity=2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("c", 3)
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3
    assert len(cache) == 2


def test_retry_is_finite_and_backoff_is_bounded():
    attempts = []
    delays = []

    def operation():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("temporary")
        return "ok"

    result = run_with_retry(
        operation,
        RetryPolicy(max_attempts=3, base_delay_seconds=0.1, max_delay_seconds=0.15),
        sleep=delays.append,
    )
    assert result == "ok"
    assert len(attempts) == 3
    assert delays == [0.1, 0.15]

    with pytest.raises(RuntimeError, match="temporary"):
        run_with_retry(lambda: (_ for _ in ()).throw(RuntimeError("temporary")), RetryPolicy(max_attempts=2), sleep=lambda _: None)
