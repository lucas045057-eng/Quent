from datetime import datetime, timedelta, timezone

from quant_phase1.contracts import DataStatus
from quant_phase1.freshness import evaluate_kline_freshness, expected_latest_closed_open


UTC = timezone.utc


def test_expected_latest_closed_1h_bar_at_1030_is_0900():
    now = datetime(2026, 9, 20, 10, 30, tzinfo=UTC)
    assert expected_latest_closed_open(now, "1H") == datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


def test_old_but_latest_1h_bar_is_available():
    now = datetime(2026, 9, 20, 10, 30, tzinfo=UTC)
    latest = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
    assert evaluate_kline_freshness(now, "1H", latest, grace_seconds=120) is DataStatus.AVAILABLE


def test_missing_theoretical_latest_bar_is_stale_after_grace():
    now = datetime(2026, 9, 20, 10, 30, tzinfo=UTC)
    latest = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)
    assert evaluate_kline_freshness(now, "1H", latest, grace_seconds=120) is DataStatus.STALE


def test_missing_bar_is_not_available_during_ingestion_grace():
    now = datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC) + timedelta(seconds=30)
    assert evaluate_kline_freshness(now, "1H", None, grace_seconds=120) is DataStatus.NOT_AVAILABLE


def test_future_local_bar_is_error():
    now = datetime(2026, 9, 20, 10, 30, tzinfo=UTC)
    latest = datetime(2026, 9, 20, 11, 0, tzinfo=UTC)
    assert evaluate_kline_freshness(now, "1H", latest, grace_seconds=120) is DataStatus.ERROR


def test_ticker_age_based_freshness_is_not_used_for_kline():
    now = datetime(2026, 9, 20, 10, 30, tzinfo=UTC)
    latest = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
    assert evaluate_kline_freshness(now, "1H", latest, grace_seconds=120) is DataStatus.AVAILABLE
