from quant_phase1.entrypoints.engine import _phase2_next_delay_seconds


def test_phase2_cycle_runtime_counts_toward_poll_interval():
    assert _phase2_next_delay_seconds(interval_seconds=300, elapsed_seconds=170) == 130


def test_phase2_cycle_that_exceeds_interval_restarts_without_extra_wait():
    assert _phase2_next_delay_seconds(interval_seconds=300, elapsed_seconds=325) == 0
