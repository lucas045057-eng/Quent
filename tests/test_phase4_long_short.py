import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_phase4.adapters.base import (
    AdapterAuthError,
    AdapterHTTPError,
    AdapterNetworkError,
    AdapterParseError,
    AdapterProviderError,
    AdapterRateLimitError,
    AdapterSchemaError,
    AdapterTimeoutError,
    SharedPublicRESTTransport,
    epoch_milliseconds,
)
from quant_phase4.adapters.bitget_classic_v2 import BitgetClassicV2LongShortAdapter
from quant_phase4.adapters.bybit_v5 import BybitV5LongShortAdapter
from quant_phase4.adapters.hyperliquid_public import HyperliquidPublicLongShortAdapter
from quant_phase4.contracts import DataStatus, LongShortMetricType, ReasonCode
from quant_phase4.long_short import validate_long_short_comparability


NOW = datetime(2026, 9, 21, 12, tzinfo=timezone.utc)


def test_bitget_classic_v2_parses_account_holder_ratios_without_using_uta_v3() -> None:
    adapter = BitgetClassicV2LongShortAdapter(canonical_symbol="BTC-USDT-PERP")

    observation = adapter.parse_response(
        {
            "code": "00000",
            "data": [{"longRatio": "0.55", "shortRatio": "0.45", "longShortRatio": "1.2222", "ts": "1789992000000"}],
        },
        received_at=NOW,
    )

    assert adapter.path == "/api/v2/mix/market/long-short"
    assert observation.exchange == "bitget"
    assert observation.metric_type is LongShortMetricType.ACCOUNT_HOLDER_RATIO
    assert observation.population_semantics.value == "HOLDER_COUNT_RATIO"
    assert observation.long_value == Decimal("0.55")
    assert observation.short_value == Decimal("0.45")
    assert observation.ratio == Decimal("1.2222")
    assert observation.period == "5m"
    assert observation.exchange_timestamp == datetime.fromtimestamp(1789992000, tz=timezone.utc)


def test_bitget_fetch_retains_requested_symbol_when_public_rows_omit_it(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def json(self) -> object:
            return {
                "code": "00000",
                "data": [{"longRatio": "0.55", "shortRatio": "0.45", "longShortRatio": "1.2222", "ts": "1789992000000"}],
            }

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        def get(self, *args: object, **kwargs: object) -> Response:
            return Response()

    monkeypatch.setattr("quant_phase4.adapters.bitget_classic_v2.aiohttp.ClientSession", lambda **kwargs: Session())

    observation = asyncio.run(BitgetClassicV2LongShortAdapter().fetch("BTCUSDT", "5m", received_at=NOW))

    assert observation.exchange_symbol == "BTCUSDT"
    assert observation.canonical_symbol == "BTC-USDT-PERP"
    assert observation.period == "5m"


def test_bybit_v5_parses_all_position_holder_account_ratio_and_preserves_period() -> None:
    adapter = BybitV5LongShortAdapter(canonical_symbol="BTC-USDT-PERP")

    observation = adapter.parse_response(
        {
            "retCode": 0,
            "result": {"list": [{"symbol": "BTCUSDT", "period": "15min", "buyRatio": "0.52", "sellRatio": "0.48", "timestamp": "1789992000000"}]},
        },
        received_at=NOW,
    )

    assert adapter.path == "/v5/market/account-ratio"
    assert observation.exchange == "bybit"
    assert observation.metric_type is LongShortMetricType.ACCOUNT_HOLDER_RATIO
    assert observation.population_semantics.value == "ALL_POSITION_HOLDER_ACCOUNT_RATIO"
    assert observation.long_value == Decimal("0.52")
    assert observation.short_value == Decimal("0.48")
    assert observation.ratio == Decimal("1.083333333333333333333333333")
    assert observation.period == "15min"


@pytest.mark.parametrize("adapter", [BitgetClassicV2LongShortAdapter(), BybitV5LongShortAdapter()])
def test_long_short_request_keeps_the_requested_official_period(adapter: object) -> None:
    assert adapter.long_short_request("BTCUSDT", "4h")[1]["period"] == "4h"


@pytest.mark.parametrize("period", ["1m", "2h", "1H", "random"])
def test_bybit_rejects_periods_not_documented_by_the_official_api(period: str) -> None:
    with pytest.raises(AdapterSchemaError, match="period"):
        BybitV5LongShortAdapter().long_short_request("BTCUSDT", period)


@pytest.mark.parametrize(
    ("adapter", "payload", "kwargs"),
    [
        (BitgetClassicV2LongShortAdapter(), {"code": "00000", "data": [{"longRatio": "0.55", "shortRatio": "0.45", "ts": "1789992000000"}]}, {}),
        (BybitV5LongShortAdapter(), {"retCode": 0, "result": {"list": [{"buyRatio": "0.52", "timestamp": "1789992000000"}]}}, {}),
    ],
)
def test_long_short_parsers_reject_missing_ratio_fields_and_malformed_success_envelopes(adapter: object, payload: object, kwargs: dict[str, str]) -> None:
    with pytest.raises(AdapterSchemaError):
        adapter.parse_response(payload, received_at=NOW, **kwargs)


@pytest.mark.parametrize(
    ("adapter", "payload"),
    [
        (BitgetClassicV2LongShortAdapter(), {"code": "12345", "data": []}),
        (BybitV5LongShortAdapter(), {"retCode": 10001, "result": {"list": []}}),
    ],
)
def test_long_short_provider_business_errors_are_not_schema_errors(adapter, payload):
    with pytest.raises(AdapterProviderError) as raised:
        adapter.parse_response(payload, received_at=NOW)
    assert raised.value.category == "PROVIDER_ERROR"
    assert raised.value.provider_code is not None


@pytest.mark.parametrize(
    ("adapter", "payload", "kwargs"),
    [
        (BitgetClassicV2LongShortAdapter(), {"code": "00000", "data": [{"longRatio": "0.55", "shortRatio": "0.45", "longShortRatio": "1.2", "ts": "not-a-timestamp"}]}, {}),
        (BybitV5LongShortAdapter(), {"retCode": 0, "result": {"list": [{"buyRatio": "0.52", "sellRatio": "0.48", "timestamp": "not-a-timestamp"}]}}, {}),
    ],
)
def test_long_short_parsers_reject_invalid_source_timestamps(adapter: object, payload: object, kwargs: dict[str, str]) -> None:
    with pytest.raises(AdapterSchemaError, match="timestamp|ts"):
        adapter.parse_response(payload, received_at=NOW, **kwargs)


@pytest.mark.parametrize("timestamp", [1789992000000.5, Decimal("1789992000000.5")])
def test_epoch_milliseconds_rejects_fractional_numeric_values(timestamp: object) -> None:
    with pytest.raises(AdapterSchemaError, match="epoch milliseconds"):
        epoch_milliseconds(timestamp, "timestamp")


@pytest.mark.parametrize("timestamp", [1789992000000, Decimal("1789992000000"), "1789992000000"])
def test_epoch_milliseconds_accepts_integral_values(timestamp: object) -> None:
    assert epoch_milliseconds(timestamp, "timestamp") == datetime.fromtimestamp(1789992000, tz=timezone.utc)


def test_hyperliquid_long_short_is_explicitly_unavailable_without_inference() -> None:
    observation = HyperliquidPublicLongShortAdapter().unavailable("BTC", "5min", received_at=NOW)

    assert observation.status is DataStatus.NOT_AVAILABLE
    assert observation.reason_code is ReasonCode.UNCONFIRMED_PUBLIC_SOURCE
    assert observation.long_value is None
    assert observation.short_value is None
    assert observation.ratio is None


def test_bitget_empty_success_envelope_is_not_misclassified_as_schema_error():
    observation = BitgetClassicV2LongShortAdapter().parse_response(
        {"code": "00000", "requestTime": "1790000000000", "data": []},
        received_at=NOW,
    )

    assert observation.status is DataStatus.NOT_AVAILABLE
    assert observation.long_value is None
    assert observation.short_value is None
    assert observation.ratio is None
    assert observation.reason_code is ReasonCode.EMPTY_RESPONSE


class _NoopLimiter:
    async def acquire(self):
        return None


class _Response:
    def __init__(self, status, payload=None):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _Session:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error

    def get(self, *_args, **_kwargs):
        if self.error is not None:
            raise self.error
        return self.response


@pytest.mark.parametrize(
    ("status", "error_type", "category"),
    [
        (429, AdapterRateLimitError, "RATE_LIMIT"),
        (403, AdapterAuthError, "AUTH_ERROR"),
        (503, AdapterHTTPError, "HTTP_ERROR"),
    ],
)
def test_shared_rest_transport_classifies_http_failures_without_schema_label(status, error_type, category):
    transport = SharedPublicRESTTransport(
        "https://api.example", session=_Session(_Response(status)), rate_limiter=_NoopLimiter()
    )

    with pytest.raises(error_type) as raised:
        asyncio.run(transport.get_json("/public", {}))

    assert raised.value.category == category
    assert raised.value.http_status == status


@pytest.mark.parametrize(
    ("error", "error_type", "category"),
    [
        (TimeoutError("bounded timeout"), AdapterTimeoutError, "TIMEOUT"),
        (OSError("connection reset"), AdapterNetworkError, "NETWORK_ERROR"),
    ],
)
def test_shared_rest_transport_classifies_transport_failures(error, error_type, category):
    transport = SharedPublicRESTTransport(
        "https://api.example", session=_Session(error=error), rate_limiter=_NoopLimiter()
    )

    with pytest.raises(error_type) as raised:
        asyncio.run(transport.get_json("/public", {}))

    assert raised.value.category == category


def test_bitget_business_error_and_malformed_payload_have_distinct_categories():
    adapter = BitgetClassicV2LongShortAdapter()

    with pytest.raises(AdapterProviderError) as provider_error:
        adapter.parse_response({"code": "40034", "data": []}, received_at=NOW)
    assert provider_error.value.category == "PROVIDER_ERROR"
    assert provider_error.value.provider_code == "40034"

    with pytest.raises(AdapterSchemaError) as schema_error:
        adapter.parse_response({"code": "00000", "data": {"unexpected": True}}, received_at=NOW)
    assert schema_error.value.category == "SCHEMA_ERROR"


def test_shared_rest_transport_classifies_invalid_json_as_parse_error():
    transport = SharedPublicRESTTransport(
        "https://api.example",
        session=_Session(_Response(200, ValueError("malformed json"))),
        rate_limiter=_NoopLimiter(),
    )

    with pytest.raises(AdapterParseError) as raised:
        asyncio.run(transport.get_json("/public", {}))

    assert raised.value.category == "PARSE_ERROR"


def test_comparability_rejects_distinct_account_holder_populations() -> None:
    bitget = BitgetClassicV2LongShortAdapter(canonical_symbol="BTC-USDT-PERP").parse_response(
        {"code": "00000", "data": [{"longRatio": "0.55", "shortRatio": "0.45", "longShortRatio": "1.2222", "ts": "1789992000000"}]},
        received_at=NOW,
    )
    bybit = BybitV5LongShortAdapter(canonical_symbol="BTC-USDT-PERP").parse_response(
        {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "period": "5min", "buyRatio": "0.52", "sellRatio": "0.48", "timestamp": "1789992000000"}]}},
        received_at=NOW,
    )

    group = validate_long_short_comparability([bitget, bybit])

    assert group.metric_type is LongShortMetricType.ACCOUNT_HOLDER_RATIO
    assert group.population_semantics.value == "HOLDER_COUNT_RATIO"
    assert group.period == "5min"
    assert group.canonical_symbol == "BTC-USDT-PERP"
    assert group.comparable_source_count == 1
    assert group.status is DataStatus.NOT_AVAILABLE
    assert group.reason == "INSUFFICIENT_COMPARABLE_SOURCES"


def test_comparability_never_averages_or_overwrites_missing_and_stale_sources() -> None:
    available = BybitV5LongShortAdapter(canonical_symbol="BTC-USDT-PERP").parse_response(
        {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "period": "5min", "buyRatio": "0.52", "sellRatio": "0.48", "timestamp": "1789992000000"}]}},
        received_at=NOW,
    )
    unavailable = HyperliquidPublicLongShortAdapter(canonical_symbol="BTC-USDT-PERP").unavailable("BTC", "5min", received_at=NOW)
    stale = available.__class__(**{**available.__dict__, "exchange": "bitget", "status": DataStatus.STALE})

    group = validate_long_short_comparability([available, unavailable, stale])

    assert group.comparable_source_count == 1
    assert group.missing_sources == ("hyperliquid",)
    assert group.stale_sources == ("bitget",)
    assert group.status is DataStatus.NOT_AVAILABLE
    assert group.reason == "INSUFFICIENT_COMPARABLE_SOURCES"
