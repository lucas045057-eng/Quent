from datetime import datetime, timedelta, timezone
from dataclasses import fields, is_dataclass
from decimal import Decimal

import pytest

from quant_phase1.config import Settings
from quant_phase1.contracts import Candle, DataStatus
from quant_phase5.contracts import ContextStatus, DirectionState, ReasonCode, StructureState, VolatilityState, VolumeState
from quant_phase5.market_context import build_market_leader_context, classify_volatility


UTC = timezone.utc
START = datetime(2026, 9, 22, 0, 0, tzinfo=UTC)


def make_candle(
    index: int,
    *,
    close: str | None = None,
    is_closed: bool = True,
    offset_seconds: int = 0,
    symbol: str = "BTCUSDT",
    volume: str = "100",
) -> Candle:
    value = Decimal(close) if close is not None else Decimal(100 + index)
    opened = START + timedelta(minutes=5 * index, seconds=offset_seconds)
    return Candle(
        symbol=symbol,
        interval="5m",
        bar_open_timestamp=opened,
        open=value - Decimal("0.2"),
        high=value + Decimal("0.5"),
        low=value - Decimal("0.5"),
        close=value,
        volume=Decimal(volume),
        turnover=value * Decimal(volume),
        exchange_timestamp=opened + timedelta(minutes=5),
        fetched_at=opened + timedelta(minutes=5, seconds=1),
        processed_at=opened + timedelta(minutes=5, seconds=2),
        status=DataStatus.AVAILABLE,
        is_closed=is_closed,
        raw_payload={"id": index},
    )


def settings() -> Settings:
    return Settings.from_env({})


def test_context_uses_closed_bar_close_time_and_deterministic_local_metrics():
    rows = [make_candle(index) for index in range(25)]
    context = build_market_leader_context(
        rows,
        settings(),
        now=START + timedelta(minutes=125, seconds=30),
        processed_at=START + timedelta(minutes=126),
    )

    assert context.symbol == "BTCUSDT"
    assert context.timeframe == "5m"
    assert context.context_timestamp == START + timedelta(minutes=125)
    assert context.input_window_end == context.context_timestamp
    assert context.status is ContextStatus.AVAILABLE
    assert context.freshness_status is ContextStatus.AVAILABLE
    assert context.return_pct == Decimal("2.479338842975206611570247900")
    assert context.trend_state is DirectionState.TREND_UP
    assert context.structure_state is StructureState.HIGHER_HIGH_HIGHER_LOW
    assert context.volatility_state is VolatilityState.LOW
    assert context.volume_state is VolumeState.NORMAL
    assert context.volume_ratio == Decimal("1")
    assert context.source_count == 1
    assert context.missing_count == 0


def _float_paths(value, path="context"):
    if isinstance(value, float):
        return [path]
    if is_dataclass(value) and not isinstance(value, type):
        return [found for item in fields(value)
                for found in _float_paths(getattr(value, item.name), f"{path}.{item.name}")]
    if isinstance(value, dict):
        return [found for key, item in value.items()
                for found in _float_paths(item, f"{path}.{key}")]
    if isinstance(value, (tuple, list)):
        return [found for index, item in enumerate(value)
                for found in _float_paths(item, f"{path}[{index}]")]
    return []


@pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT"])
@pytest.mark.parametrize("interval,interval_seconds", [("1H", 3600), ("4H", 14400)])
@pytest.mark.parametrize("age_microseconds,expected_age", [(123456, "2.123"), (123500, "2.123")])
def test_live_like_bitget_phase5_context_contains_no_binary_float(
    symbol, interval, interval_seconds, age_microseconds, expected_age,
):
    from quant_phase1.adapters.bitget_v3.parsers import parse_candles_response

    start = datetime(2026, 9, 22, 0, 0, tzinfo=UTC)
    rows = []
    for index in range(40):
        opened = start + timedelta(seconds=index * interval_seconds)
        epoch_ms = int(opened.timestamp()) * 1000
        close = 100 + index
        rows.append([str(epoch_ms), str(close - 1), str(close + 1), str(close - 2),
                     str(close), "10", str(close * 10)])
    source_close = start + timedelta(seconds=40 * interval_seconds)
    processed_at = source_close + timedelta(seconds=2, microseconds=age_microseconds)
    candles = parse_candles_response(
        {"code": "00000", "data": rows}, symbol=symbol, interval=interval,
        fetched_at=processed_at, now=processed_at,
    )

    context = build_market_leader_context(
        candles, settings(), now=processed_at, processed_at=processed_at,
    )

    assert context.data_quality["freshness_age_seconds"] == Decimal(expected_age)
    assert context.data_quality["context_age_seconds"] == Decimal(expected_age)
    assert context.data_quality["freshness_age_seconds"].as_tuple().exponent == -3
    assert _float_paths(context) == []


def test_open_and_future_bars_never_advance_context_timestamp():
    rows = [make_candle(index) for index in range(25)]
    rows.extend(
        [
            make_candle(25, is_closed=False),
            make_candle(26, close="1000"),
        ]
    )
    context = build_market_leader_context(
        rows,
        settings(),
        now=START + timedelta(minutes=125, seconds=30),
        processed_at=START + timedelta(minutes=126),
    )

    assert context.context_timestamp == START + timedelta(minutes=125)
    assert context.return_pct == Decimal("2.479338842975206611570247900")


def test_stale_latest_closed_bar_propagates_without_calculated_values():
    context = build_market_leader_context(
        [make_candle(index) for index in range(25)],
        settings(),
        now=START + timedelta(minutes=140, seconds=31),
        processed_at=START + timedelta(minutes=141),
    )

    assert context.status is ContextStatus.STALE
    assert context.freshness_status is ContextStatus.STALE
    assert context.reason_code is ReasonCode.STALE_INPUT
    assert context.return_pct is None
    assert context.trend_state is DirectionState.NOT_AVAILABLE
    assert context.structure_state is StructureState.NOT_AVAILABLE
    assert context.data_quality["source_timestamp"] == (START + timedelta(minutes=125)).isoformat()
    assert context.data_quality["freshness_age_seconds"] == 16 * 60
    assert context.data_quality["freshness_threshold_seconds"] == 5 * 60 + settings().kline_ingestion_grace_seconds["5m"]
    assert context.data_quality["reason"] == "SOURCE_NOT_ADVANCING"
    assert context.data_quality["fetched_at"] == (START + timedelta(minutes=125, seconds=1)).isoformat()
    assert context.data_quality["checked_at"] == (START + timedelta(minutes=141)).isoformat()
    assert context.data_quality["context_timestamp"] is None


def test_fresh_context_preserves_source_event_and_ingestion_timestamps_separately():
    checked_at = START + timedelta(minutes=125, seconds=30)
    context = build_market_leader_context(
        [make_candle(index) for index in range(25)],
        settings(),
        now=checked_at,
        processed_at=checked_at,
    )

    assert context.data_quality["source_timestamp"] == context.context_timestamp.isoformat()
    assert context.data_quality["fetched_at"] == (START + timedelta(minutes=125, seconds=1)).isoformat()
    assert context.data_quality["checked_at"] == checked_at.isoformat()
    assert context.data_quality["freshness_age_seconds"] == 30
    assert context.data_quality["freshness_threshold_seconds"] == 5 * 60 + settings().kline_ingestion_grace_seconds["5m"]
    assert context.data_quality["reason"] is None


def test_missing_window_is_not_available_and_never_a_zero_return():
    context = build_market_leader_context(
        [make_candle(index) for index in range(10)],
        settings(),
        now=START + timedelta(minutes=50, seconds=30),
        processed_at=START + timedelta(minutes=51),
    )

    assert context.status is ContextStatus.NOT_AVAILABLE
    assert context.reason_code is ReasonCode.MISSING_INPUT
    assert context.return_pct is None
    assert context.volatility_value is None
    assert context.volume_ratio is None
    assert context.missing_evidence


def test_misaligned_window_is_not_available_with_timestamp_skew_reason():
    rows = [make_candle(index, offset_seconds=1) for index in range(25)]
    context = build_market_leader_context(
        rows,
        settings(),
        now=START + timedelta(minutes=125, seconds=30),
        processed_at=START + timedelta(minutes=126),
    )

    assert context.status is ContextStatus.NOT_AVAILABLE
    assert context.reason_code is ReasonCode.TIMESTAMP_SKEW
    assert context.missing_evidence == ("bar_open_timestamp_alignment",)


def test_invalid_latest_volume_is_error_not_a_low_volume_signal():
    rows = [make_candle(index) for index in range(24)]
    rows.append(make_candle(24, volume="-1"))
    context = build_market_leader_context(
        rows,
        settings(),
        now=START + timedelta(minutes=125, seconds=30),
        processed_at=START + timedelta(minutes=126),
    )

    assert context.status is ContextStatus.ERROR
    assert context.reason_code is ReasonCode.CALCULATION_ERROR
    assert context.volume_ratio is None
    assert context.volume_state is VolumeState.NOT_AVAILABLE


def test_only_btc_and_eth_are_market_leaders():
    rows = [make_candle(index) for index in range(25)]
    with pytest.raises(ValueError, match="BTCUSDT or ETHUSDT"):
        build_market_leader_context(
            rows,
            settings(),
            symbol="XRPUSDT",
            now=START + timedelta(minutes=125, seconds=30),
            processed_at=START + timedelta(minutes=126),
        )


def test_eth_is_resolved_as_a_supported_market_leader():
    context = build_market_leader_context(
        [make_candle(index, symbol="ETHUSDT") for index in range(25)],
        settings(),
        now=START + timedelta(minutes=125, seconds=30),
        processed_at=START + timedelta(minutes=126),
    )

    assert context.symbol == "ETHUSDT"
    assert context.status is ContextStatus.AVAILABLE


def test_volatility_threshold_boundaries_are_exact():
    assert classify_volatility("5m", Decimal("0.0010"), settings()) is VolatilityState.NORMAL
    assert classify_volatility("5m", Decimal("0.0040"), settings()) is VolatilityState.HIGH
    assert classify_volatility("5m", Decimal("0.0080"), settings()) is VolatilityState.EXTREME
