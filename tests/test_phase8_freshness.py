from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_phase8.config import Phase8Settings
from quant_phase8.contracts import (
    DataStatus,
    ObservationKind,
    OptionMarketObservation,
    OptionMetricValue,
    TimestampSemantics,
)
from quant_phase8.freshness import (
    evaluate_catalog_freshness,
    evaluate_lifecycle_freshness,
    evaluate_observation_metric_freshness,
    select_context_inputs,
)


UTC = timezone.utc
AS_OF = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _settings(**overrides) -> Phase8Settings:
    values = {
        "TRADING_MODE": "paper",
        "PHASE8_OPTIONS_MAX_SOURCE_SKEW_SECONDS": "5400",
    }
    values.update(overrides)
    return Phase8Settings.from_env(values)


def _observation(
    metric_name: str,
    *,
    kind: ObservationKind = ObservationKind.WS_INCREMENTAL_TICKER_CHANGE,
    source_timestamp: datetime | None,
    capture_time: datetime = AS_OF,
    semantics: TimestampSemantics = TimestampSemantics.VERIFIED,
    value: Decimal | None = Decimal("1.25"),
    status: DataStatus | None = None,
    symbol: str = "BTC-26DEC26-100000-C",
) -> OptionMarketObservation:
    is_rest = kind is ObservationKind.REST_CHAIN_SUMMARY
    metric = OptionMetricValue(
        metric=metric_name,
        value=value,
        source_field=f"data.{metric_name}",
        source_method_or_channel="public/get_book_summary_by_currency" if is_rest else "incremental_ticker",
        exchange_timestamp=source_timestamp,
        field_last_updated_at=source_timestamp,
        fetched_at=capture_time if is_rest else None,
        received_at=None if is_rest else capture_time,
        processed_at=capture_time,
        timestamp_semantics=TimestampSemantics.NOT_PROVIDED if is_rest else semantics,
        status=status,
    )
    return OptionMarketObservation(
        exchange="DERIBIT",
        source="deribit",
        symbol=symbol,
        underlying="BTC",
        observation_kind=kind,
        metrics={metric_name: metric},
        exchange_timestamp=source_timestamp,
        fetched_at=capture_time if is_rest else None,
        received_at=None if is_rest else capture_time,
        processed_at=capture_time,
        status=DataStatus.AVAILABLE,
    )


def test_rest_chain_age_uses_fetched_at_when_event_time_is_not_provided():
    observation = _observation(
        "open_interest",
        kind=ObservationKind.REST_CHAIN_SUMMARY,
        source_timestamp=None,
        capture_time=AS_OF - timedelta(seconds=5401),
    )
    result = evaluate_observation_metric_freshness(
        observation, "open_interest", as_of=AS_OF, settings=_settings()
    )
    assert (result.status, result.data_age_seconds, result.reason_code) == (
        DataStatus.STALE,
        5401,
        "SOURCE_SNAPSHOT_STALE",
    )


def test_markprice_uses_each_fields_900_second_source_age():
    observation = _observation(
        "mark_price", kind=ObservationKind.WS_MARKPRICE_CHANGE,
        source_timestamp=AS_OF - timedelta(seconds=901),
    )
    result = evaluate_observation_metric_freshness(
        observation, "mark_price", as_of=AS_OF, settings=_settings()
    )
    assert (result.status, result.data_age_seconds) == (DataStatus.STALE, 901)


def test_ticker_uses_each_fields_300_second_source_age():
    observation = _observation(
        "delta", source_timestamp=AS_OF - timedelta(seconds=301)
    )
    result = evaluate_observation_metric_freshness(
        observation, "delta", as_of=AS_OF, settings=_settings()
    )
    assert (result.status, result.data_age_seconds) == (DataStatus.STALE, 301)


def test_catalog_and_lifecycle_freshness_use_their_own_capture_times():
    assert evaluate_catalog_freshness(
        AS_OF - timedelta(seconds=93_601), as_of=AS_OF, settings=_settings()
    ).status is DataStatus.STALE
    assert evaluate_lifecycle_freshness(
        AS_OF - timedelta(seconds=61), as_of=AS_OF, settings=_settings()
    ).status is DataStatus.STALE


def test_verified_source_clock_skew_is_rejected():
    observation = _observation(
        "mark_iv",
        source_timestamp=AS_OF - timedelta(seconds=6000),
        capture_time=AS_OF - timedelta(seconds=10),
    )
    result = evaluate_observation_metric_freshness(
        observation, "mark_iv", as_of=AS_OF, settings=_settings()
    )
    assert (result.status, result.reason_code) == (DataStatus.STALE, "SOURCE_TIME_SKEW")


def test_missing_or_unverified_source_time_uses_knowledge_time_and_degrades_quality():
    missing = _observation("open_interest", kind=ObservationKind.REST_CHAIN_SUMMARY, source_timestamp=None)
    missing_result = evaluate_observation_metric_freshness(
        missing, "open_interest", as_of=AS_OF, settings=_settings()
    )
    assert (missing_result.status, missing_result.reason_code, missing_result.data_age_seconds) == (
        DataStatus.PARTIAL, "SOURCE_TIME_NOT_PROVIDED", 0
    )

    unverified = _observation(
        "mark_iv", source_timestamp=AS_OF - timedelta(hours=1),
        capture_time=AS_OF - timedelta(seconds=30), semantics=TimestampSemantics.UNVERIFIED,
    )
    unverified_result = evaluate_observation_metric_freshness(
        unverified, "mark_iv", as_of=AS_OF, settings=_settings()
    )
    assert (unverified_result.status, unverified_result.reason_code, unverified_result.data_age_seconds) == (
        DataStatus.PARTIAL, "SOURCE_TIME_UNVERIFIED", 30
    )


def test_source_partial_status_is_never_upgraded_by_fresh_verified_timestamp():
    observation = _observation(
        "delta", source_timestamp=AS_OF - timedelta(seconds=5), status=DataStatus.PARTIAL
    )
    result = evaluate_observation_metric_freshness(
        observation, "delta", as_of=AS_OF, settings=_settings()
    )
    assert result.status is DataStatus.PARTIAL
    assert result.reason_code == "SOURCE_FIELD_PARTIAL"


def test_future_provider_timestamp_is_rejected():
    observation = _observation("delta", source_timestamp=AS_OF + timedelta(seconds=1))
    result = evaluate_observation_metric_freshness(
        observation, "delta", as_of=AS_OF, settings=_settings()
    )
    assert (result.status, result.reason_code) == (DataStatus.ERROR, "FUTURE_SOURCE_TIMESTAMP")


def test_local_capture_after_as_of_is_excluded_to_prevent_lookahead():
    late = _observation(
        "delta", source_timestamp=AS_OF - timedelta(seconds=10),
        capture_time=AS_OF + timedelta(seconds=1),
    )
    assert select_context_inputs((late,), as_of=AS_OF, settings=_settings()) == ()


def test_out_of_order_updates_do_not_roll_back_and_fields_age_independently():
    older_delivery = _observation(
        "mark_iv", kind=ObservationKind.WS_MARKPRICE_CHANGE,
        source_timestamp=AS_OF - timedelta(seconds=400), capture_time=AS_OF - timedelta(seconds=20),
    )
    newer_delivery = _observation(
        "mark_iv", kind=ObservationKind.WS_MARKPRICE_CHANGE,
        source_timestamp=AS_OF - timedelta(seconds=100), capture_time=AS_OF - timedelta(seconds=30),
        value=Decimal("1.5"),
    )
    old_volume = _observation(
        "open_interest", kind=ObservationKind.REST_CHAIN_SUMMARY,
        source_timestamp=None, capture_time=AS_OF - timedelta(seconds=3600),
    )
    selected = select_context_inputs(
        (newer_delivery, old_volume, older_delivery), as_of=AS_OF, settings=_settings()
    )
    by_metric = {item.metric_name: item for item in selected}
    assert by_metric["mark_iv"].metric.value == Decimal("1.5")
    assert by_metric["mark_iv"].freshness.data_age_seconds == 100
    assert by_metric["open_interest"].freshness.data_age_seconds == 3600
    assert by_metric["open_interest"].freshness.status is DataStatus.PARTIAL
    assert all(item.capture_timestamp <= AS_OF for item in selected)
    assert all(item.exchange_timestamp is None or item.exchange_timestamp <= AS_OF for item in selected)


def test_late_arrival_cannot_rewrite_an_already_fixed_context_as_of():
    original = _observation(
        "delta", source_timestamp=AS_OF - timedelta(seconds=200),
        capture_time=AS_OF - timedelta(seconds=190), value=Decimal("0.2"),
    )
    late_arrival = _observation(
        "delta", source_timestamp=AS_OF - timedelta(seconds=100),
        capture_time=AS_OF + timedelta(seconds=5), value=Decimal("0.8"),
    )
    selected = select_context_inputs((original, late_arrival), as_of=AS_OF, settings=_settings())
    assert len(selected) == 1
    assert selected[0].metric.value == Decimal("0.2")


def test_fifteen_minute_recalculation_does_not_refresh_hourly_summary_age():
    fetched_at = AS_OF - timedelta(hours=1)
    observation = _observation(
        "open_interest", kind=ObservationKind.REST_CHAIN_SUMMARY,
        source_timestamp=None, capture_time=fetched_at,
    )
    context_time = AS_OF + timedelta(minutes=15)
    result = evaluate_observation_metric_freshness(
        observation, "open_interest", as_of=context_time, settings=_settings()
    )
    assert result.data_age_seconds == 4500
    assert result.status is DataStatus.PARTIAL
