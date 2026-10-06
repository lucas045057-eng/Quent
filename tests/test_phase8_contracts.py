from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_phase8 import contracts


OptionContextMetric = getattr(contracts, "OptionContextMetric", None)
OptionContextSnapshot = getattr(contracts, "OptionContextSnapshot", None)
OptionInstrument = getattr(contracts, "OptionInstrument", None)
OptionInstrumentEvent = getattr(contracts, "OptionInstrumentEvent", None)
OptionMarketObservation = getattr(contracts, "OptionMarketObservation", None)
OptionMetricValue = getattr(contracts, "OptionMetricValue", None)
OptionType = getattr(contracts, "OptionType", None)
DataStatus = getattr(contracts, "DataStatus", None)
ObservationKind = getattr(contracts, "ObservationKind", None)
Provenance = getattr(contracts, "Provenance", None)
UnitStatus = getattr(contracts, "UnitStatus", None)
loads_decimal_json = getattr(contracts, "loads_decimal_json", None)

T0 = datetime(2026, 9, 25, 16, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(seconds=1)


def test_metric_keeps_exact_decimal_source_and_time_provenance():
    assert OptionMetricValue is not None, "canonical options metric contract must exist"
    metric = OptionMetricValue(
        metric="mark_price",
        value=Decimal("0.123456789012345678901"),
        exchange_timestamp=T0,
        received_at=T1,
        field_last_updated_at=T0,
        unit_status=UnitStatus.SOURCE_NATIVE_UNVERIFIED,
    )

    assert metric.value == Decimal("0.123456789012345678901")
    assert metric.source == "deribit"
    assert metric.exchange == "DERIBIT"
    assert metric.source_field == "mark_price"
    assert metric.exchange_timestamp == T0
    assert metric.received_at == T1
    assert metric.field_last_updated_at == T0
    assert metric.status is DataStatus.AVAILABLE
    assert metric.provenance is Provenance.SOURCE_PROVIDED


def test_missing_and_explicit_null_have_distinct_reasons_without_zero_fill():
    missing = OptionMetricValue(
        metric="mark_iv", value=None, status=DataStatus.NOT_AVAILABLE, quality_reason="MISSING_FIELD"
    )
    explicit_null = OptionMetricValue(
        metric="mark_iv", value=None, status=DataStatus.NOT_AVAILABLE, quality_reason="EXPLICIT_NULL"
    )

    assert missing.value is None
    assert explicit_null.value is None
    assert missing.quality_reason != explicit_null.quality_reason
    assert Decimal("0") not in (missing.value, explicit_null.value)


def test_json_decoder_preserves_decimal_tokens_without_float_rounding():
    assert loads_decimal_json is not None
    payload = loads_decimal_json('{"iv":0.123456789012345678901,"timestamp":1790352000000}')

    assert payload["iv"] == Decimal("0.123456789012345678901")
    assert isinstance(payload["timestamp"], int)


def test_instrument_uses_canonical_fields_and_utc_times():
    instrument = OptionInstrument(
        exchange="DERIBIT",
        source="deribit",
        symbol="BTC-26SEP26-100000-C",
        underlying="BTC",
        option_type=OptionType.CALL,
        strike=Decimal("100000"),
        expires_at=T0,
        instrument_created_at=T0 - timedelta(days=1),
        instrument_state="open",
        is_active=True,
        price_index="btc_usd",
        base_currency="BTC",
        quote_currency="BTC",
        settlement_currency="BTC",
        exchange_timestamp=None,
        fetched_at=T1,
        processed_at=T1,
        status=DataStatus.AVAILABLE,
    )

    assert instrument.symbol == "BTC-26SEP26-100000-C"
    assert instrument.strike == Decimal("100000")
    assert instrument.expires_at.tzinfo is timezone.utc
    assert instrument.exchange_timestamp is None
    assert instrument.fetched_at == T1


def test_rest_and_websocket_observations_keep_distinct_capture_times():
    metric = OptionMetricValue(metric="open_interest", value=Decimal("1.25"))
    rest = OptionMarketObservation(
        exchange="DERIBIT",
        source="deribit",
        symbol="BTC-26SEP26-100000-C",
        underlying="BTC",
        observation_kind=ObservationKind.REST_CHAIN_SUMMARY,
        metrics={"open_interest": metric},
        exchange_timestamp=None,
        fetched_at=T0,
        received_at=None,
        processed_at=T1,
        status=DataStatus.AVAILABLE,
    )
    stream = OptionMarketObservation(
        exchange="DERIBIT",
        source="deribit",
        symbol="BTC-26SEP26-100000-C",
        underlying="BTC",
        observation_kind=ObservationKind.WS_MARKPRICE_CHANGE,
        metrics={"mark_price": OptionMetricValue(metric="mark_price", value=Decimal("0.12"))},
        exchange_timestamp=T0,
        fetched_at=None,
        received_at=T1,
        processed_at=T1,
        status=DataStatus.AVAILABLE,
    )

    assert rest.fetched_at == T0 and rest.received_at is None
    assert rest.exchange_timestamp is None
    assert stream.fetched_at is None and stream.received_at == T1
    assert stream.exchange_timestamp == T0


def test_market_observation_keeps_instrument_index_distinct_from_summary_underlying_index():
    observation = OptionMarketObservation(
        exchange="DERIBIT",
        source="deribit",
        symbol="BTC-26SEP26-100000-C",
        underlying="BTC",
        observation_kind=ObservationKind.REST_CHAIN_SUMMARY,
        metrics={"open_interest": OptionMetricValue(metric="open_interest", value=Decimal("1"))},
        exchange_timestamp=None,
        fetched_at=T0,
        received_at=None,
        processed_at=T1,
        status=DataStatus.AVAILABLE,
        price_index="btc_usd",
        underlying_index="index_price",
        quote_currency="BTC",
    )

    assert observation.price_index == "btc_usd"
    assert observation.underlying_index == "index_price"
    assert observation.quote_currency == "BTC"


def test_lifecycle_event_retains_event_and_local_receive_times():
    event = OptionInstrumentEvent(
        exchange="DERIBIT",
        source="deribit",
        symbol="BTC-26SEP26-100000-C",
        event_type="STATE",
        instrument_state="open",
        exchange_timestamp=T0,
        received_at=T1,
        processed_at=T1,
        status=DataStatus.AVAILABLE,
    )

    assert event.exchange_timestamp == T0
    assert event.received_at == T1
    assert event.processed_at == T1


def test_context_metric_exposes_independent_source_age_and_quality():
    metric = OptionContextMetric(
        metric="put_call_oi_ratio",
        value=Decimal("1.25"),
        source_timestamps=(T0,),
        source_fetched_at=T0,
        source_received_at=None,
        data_age_seconds=3600,
        coverage_expected=100,
        coverage_available=98,
        status=DataStatus.PARTIAL,
        quality_reason="SOURCE_TIME_NOT_PROVIDED",
        provenance=Provenance.COMPUTED,
    )
    snapshot = OptionContextSnapshot(
        underlying="BTC",
        context_timestamp=T0 + timedelta(minutes=15),
        processed_at=T1,
        calculation_version="v1",
        metrics={"put_call_oi_ratio": metric},
    )

    assert snapshot.context_timestamp == T0 + timedelta(minutes=15)
    assert snapshot.metrics["put_call_oi_ratio"].source_fetched_at == T0
    assert snapshot.metrics["put_call_oi_ratio"].data_age_seconds == 3600
    assert snapshot.metrics["put_call_oi_ratio"].coverage_ratio == Decimal("0.98")
    assert snapshot.metrics["put_call_oi_ratio"].status is DataStatus.PARTIAL


@pytest.mark.parametrize(
    "value",
    [0.125, Decimal("NaN"), Decimal("Infinity")],
)
def test_metric_rejects_float_coercion_and_non_finite_decimals(value):
    with pytest.raises((TypeError, ValueError)):
        OptionMetricValue(metric="mark_iv", value=value)


def test_contracts_reject_naive_timestamps_invalid_option_side_and_unknown_status():
    with pytest.raises(ValueError, match="timezone-aware"):
        OptionMetricValue(metric="mark_price", value=Decimal("1"), received_at=datetime(2026, 9, 25))

    with pytest.raises(ValueError, match="option_type"):
        OptionInstrument(
            exchange="DERIBIT", source="deribit", symbol="x", underlying="BTC", option_type="straddle",
            strike=Decimal("1"), expires_at=T0, instrument_created_at=None, instrument_state="open",
            is_active=True, price_index="btc_usd", base_currency="BTC", quote_currency="BTC",
            settlement_currency="BTC", exchange_timestamp=None, fetched_at=T1, processed_at=T1,
        )

    with pytest.raises(ValueError, match="status"):
        OptionMetricValue(metric="mark_price", value=Decimal("1"), status="UNKNOWN_STATUS")
