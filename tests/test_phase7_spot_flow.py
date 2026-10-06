from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
from decimal import Decimal

import pytest

from quant_phase7.contracts import DataStatus, MarketKind
from quant_phase7.spot import BinanceSpotAdapter, BitgetUtaV3SpotAdapter
from quant_phase7.spot_flow import (
    PerpetualFlowSnapshot,
    SpotFlowError,
    aggregate_spot_window,
    compare_spot_perp,
)


UTC = timezone.utc
NOW = datetime(2024, 9, 23, 12, 5, tzinfo=UTC)
OPEN = datetime(2024, 9, 23, 12, 0, tzinfo=UTC)


def binance_event(trade_id: int, timestamp: int, *, fetched_at: datetime = NOW, maker: bool = False):
    return BinanceSpotAdapter().parse_ws_message({
        "e": "aggTrade", "E": timestamp + 1, "s": "BTCUSDT", "a": trade_id,
        "p": "100", "q": "2", "f": trade_id, "l": trade_id,
        "T": timestamp, "m": maker, "M": True,
    }, fetched_at=fetched_at)


def bitget_event(trade_id: str, timestamp: int, *, fetched_at: datetime = NOW):
    return BitgetUtaV3SpotAdapter().parse_ws_message({
        "arg": {"instType": "spot", "topic": "publicTrade", "symbol": "BTCUSDT"},
        "data": [{
            "i": trade_id, "p": "100", "v": "2",
            "S": "buy", "T": str(timestamp),
        }],
    }, fetched_at=fetched_at)


def test_event_time_window_deduplicates_and_excludes_out_of_window_events():
    first = binance_event(1, 1727092801000)
    second = binance_event(2, 1727092802000, maker=True)
    duplicate = binance_event(1, 1727092801000)
    outside = binance_event(3, 1727096400000)
    result = aggregate_spot_window(
        [first, second, duplicate, outside],
        window_open=OPEN,
        timeframe="5m",
        now=NOW,
        late_arrival_grace=timedelta(seconds=30),
    )
    assert result.market_kind is MarketKind.SPOT
    assert result.trade_count == 2
    assert result.base_volume == Decimal("4")
    assert result.quote_volume == Decimal("400")
    assert result.buy_volume == Decimal("2")
    assert result.sell_volume == Decimal("2")
    assert result.delta == Decimal("0")
    assert result.cvd == Decimal("0")
    assert result.status is DataStatus.AVAILABLE
    assert result.cursor_first == "1"
    assert result.cursor_last == "2"


def test_late_event_is_bounded_and_marked_partial_not_retroactively_accepted():
    on_time = binance_event(1, 1727092801000, fetched_at=OPEN + timedelta(seconds=10))
    late = binance_event(2, 1727092802000, fetched_at=OPEN + timedelta(minutes=6))
    result = aggregate_spot_window(
        [on_time, late],
        window_open=OPEN,
        timeframe="5m",
        now=NOW,
        late_arrival_grace=timedelta(seconds=30),
    )
    assert result.trade_count == 1
    assert result.missing_count == 1
    assert result.status is DataStatus.PARTIAL
    assert result.reason == "LATE_EVENT_OUTSIDE_GRACE"


def test_old_last_received_event_is_stale_even_when_event_time_window_is_valid():
    event = binance_event(1, 1727092801000, fetched_at=NOW)
    result = aggregate_spot_window(
        [event],
        window_open=OPEN,
        timeframe="5m",
        now=NOW + timedelta(days=2),
        expected_cadence=timedelta(seconds=30),
        freshness_grace=timedelta(seconds=30),
    )
    assert result.status is DataStatus.STALE
    assert result.reason == "SOURCE_STALE"


def test_bitget_unknown_side_preserves_total_volume_but_disables_directional_outputs():
    result = aggregate_spot_window(
        [bitget_event("bg-1", 1727092801000), bitget_event("bg-2", 1727092802000)],
        window_open=OPEN,
        timeframe="5m",
        now=NOW,
    )
    assert result.base_volume == Decimal("4")
    assert result.quote_volume == Decimal("400")
    assert result.unknown_volume == Decimal("4")
    assert result.buy_volume is None
    assert result.sell_volume is None
    assert result.delta is None
    assert result.cvd is None
    assert result.directional_trade_count == 0
    assert result.status is DataStatus.AVAILABLE
    assert result.reason == "SIDE_SEMANTICS_UNKNOWN"


def test_mixed_source_or_exchange_window_is_rejected_instead_of_blending_semantics():
    with pytest.raises(SpotFlowError, match="mixed"):
        aggregate_spot_window(
            [binance_event(1, 1727092801000), bitget_event("bg-1", 1727092802000)],
            window_open=OPEN,
            timeframe="5m",
            now=NOW,
        )


def test_empty_window_is_not_available_and_no_zero_direction_is_invented():
    result = aggregate_spot_window(
        [],
        window_open=OPEN,
        timeframe="5m",
        now=NOW,
    )
    assert result.status is DataStatus.NOT_AVAILABLE
    assert result.base_volume == Decimal("0")
    assert result.buy_volume is None
    assert result.cvd is None
    assert result.reason == "NO_EVENTS"


def test_spot_perp_comparison_is_descriptive_and_checks_units_and_time():
    spot = aggregate_spot_window(
        [binance_event(1, 1727092801000)],
        window_open=OPEN,
        timeframe="5m",
        now=NOW,
    )
    perp = PerpetualFlowSnapshot(
        symbol="BTCUSDT",
        base_volume=Decimal("3"),
        delta=Decimal("-1"),
        event_timestamp=OPEN + timedelta(seconds=2),
        market_kind="PERPETUAL",
        base_unit="BASE",
        status=DataStatus.AVAILABLE,
    )
    context = compare_spot_perp(spot, perp, max_timestamp_skew=timedelta(seconds=5))
    assert context.status is DataStatus.AVAILABLE
    assert context.spot_volume == Decimal("2")
    assert context.perp_volume == Decimal("3")
    assert context.direction_relation == "DIVERGENCE"
    assert not hasattr(context, "decision")
    with pytest.raises(SpotFlowError, match="unit"):
        compare_spot_perp(spot, replace(perp, base_unit="QUOTE"))


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (DataStatus.ERROR, DataStatus.ERROR),
        (DataStatus.STALE, DataStatus.STALE),
        (DataStatus.NOT_AVAILABLE, DataStatus.NOT_AVAILABLE),
        (DataStatus.PARTIAL, DataStatus.PARTIAL),
    ],
)
def test_spot_perp_comparison_preserves_status_precedence(status, expected):
    spot = aggregate_spot_window([binance_event(1, 1727092801000)], window_open=OPEN, timeframe="5m", now=NOW)
    perp = PerpetualFlowSnapshot(
        symbol="BTCUSDT", base_volume=Decimal("3"), delta=Decimal("-1"),
        event_timestamp=OPEN + timedelta(seconds=2), market_kind="PERPETUAL",
        base_unit="BASE", status=status,
    )
    assert compare_spot_perp(spot, perp, max_timestamp_skew=timedelta(seconds=5)).status is expected

