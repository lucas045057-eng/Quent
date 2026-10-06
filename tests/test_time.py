from datetime import datetime, timezone

import pytest

from quant_phase1.time import UtcClock, ensure_utc, utc_now


def test_utc_now_is_timezone_aware_utc():
    now = utc_now()
    assert now.tzinfo == timezone.utc


def test_ensure_utc_converts_offset_datetime():
    value = ensure_utc(datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc))
    assert value.tzinfo == timezone.utc
    assert value.hour == 12


def test_ensure_utc_rejects_naive_datetime():
    with pytest.raises(ValueError, match="timezone-aware"):
        ensure_utc(datetime(2026, 9, 20, 12, 0))


def test_clock_can_be_injected_for_deterministic_tests():
    expected = datetime(2026, 9, 20, 10, 30, tzinfo=timezone.utc)
    assert UtcClock(lambda: expected).now() == expected
