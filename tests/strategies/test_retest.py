from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
from types import SimpleNamespace as NS


def candle(opened, *, high=D("101"), low=D("99"), close=D("100"), volume=D("100")):
    return NS(bar_open_timestamp=opened, high=high, low=low, close=close, volume=volume,
              is_closed=True, status=NS(value="AVAILABLE"))


def rising_breakout_bars(as_of, minutes=15):
    start = as_of - timedelta(minutes=minutes * 26)
    rows = [candle(start + timedelta(minutes=minutes * i)) for i in range(26)]
    rows[20] = candle(start + timedelta(minutes=minutes * 20), high=D("101.5"), close=D("101.4"), volume=D("130"))
    rows[21] = candle(start + timedelta(minutes=minutes * 21), high=D("101.3"), low=D("100.8"), close=D("101.2"))
    for i in range(22, 26):
        rows[i] = candle(start + timedelta(minutes=minutes * i), high=D("101.4"), low=D("100.9"), close=D("101.2"))
    return rows


def test_closed_continuous_breakout_and_retest_is_detected_for_side_and_timeframe():
    from strategies.structures.retest import retest_confirmation
    as_of = datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc)
    assert retest_confirmation(rising_breakout_bars(as_of), "LONG", "15m", as_of=as_of) is True
    assert retest_confirmation(rising_breakout_bars(as_of, 60), "LONG", "1H", as_of=as_of) is True


def test_retest_evidence_is_unavailable_for_gapped_or_unclosed_bars():
    from strategies.structures.retest import retest_confirmation
    from types import SimpleNamespace
    as_of = datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc)
    bars = rising_breakout_bars(as_of)
    bars[10] = candle(bars[10].bar_open_timestamp + timedelta(minutes=15))
    assert retest_confirmation(bars, "LONG", "15m", as_of=as_of) is None
    bars = rising_breakout_bars(as_of)
    bars[-2] = SimpleNamespace(**{**vars(bars[-2]), "is_closed": False})
    assert retest_confirmation(bars, "LONG", "15m", as_of=as_of) is None


def test_retest_predicate_requires_matching_directional_source_group():
    from strategies.evidence.evidence_chain import MarketHypothesis, _predicate
    from strategies.contracts import MarketObservation
    at = datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc)
    fact = MarketObservation(symbol="ETHUSDT", kind="RETEST:15m", provider="bitget",
        source_ref="canonical:retest", source_group="RETEST_SHORT_15m", value=D(1), unit="BOOLEAN",
        observed_at=at, fetched_at=at, availability="AVAILABLE", freshness="FRESH",
        quality="VALID", coverage="COMPLETE")
    assert _predicate(fact, MarketHypothesis(direction="SHORT", timeframe="15m", structure="BREAKOUT_FORMING"))
    assert not _predicate(fact, MarketHypothesis(direction="LONG", timeframe="15m", structure="BREAKOUT_FORMING"))


def test_retest_interval_must_match_requested_timeframe():
    from strategies.structures.retest import retest_confirmation
    as_of = datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc)
    assert retest_confirmation(rising_breakout_bars(as_of), "LONG", "1H", as_of=as_of) is None


def test_retest_followed_by_invalidation_does_not_remain_confirmed():
    from strategies.structures.retest import retest_confirmation
    as_of = datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc)
    bars = rising_breakout_bars(as_of)
    bars[22] = candle(bars[22].bar_open_timestamp, high=D("99"), low=D("97"), close=D("98"))
    assert retest_confirmation(bars, "LONG", "15m", as_of=as_of) is False
