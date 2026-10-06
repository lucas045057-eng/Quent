from __future__ import annotations

import asyncio
import json

import pytest

from quant_data_layer.observability import ErrorCategory
from quant_data_layer.errors import NormalizedError, normalize_error


class ProviderContractError(Exception):
    pass


class ParserError(Exception):
    pass


class PersistenceError(Exception):
    pass


class CheckpointError(Exception):
    pass


class AdmissionTimeoutError(Exception):
    pass


class BackpressureError(Exception):
    pass


class ResourceLimitError(Exception):
    pass


class ConfigurationError(Exception):
    pass


class DataQualityError(Exception):
    pass


@pytest.mark.parametrize(
    ("error", "kwargs", "category"),
    [
        (RuntimeError("throttled"), {"http_status": 429}, ErrorCategory.PROVIDER_RATE_LIMIT),
        (TimeoutError("provider timed out"), {}, ErrorCategory.PROVIDER_TIMEOUT),
        (ConnectionResetError("connection reset"), {}, ErrorCategory.NETWORK),
        (ProviderContractError("bad schema"), {}, ErrorCategory.PROVIDER_CONTRACT),
        (ParserError("bad payload"), {}, ErrorCategory.PARSER),
        (json.JSONDecodeError("bad json", "{", 0), {}, ErrorCategory.PARSER),
        (PersistenceError("write failed"), {}, ErrorCategory.PERSISTENCE),
        (CheckpointError("cursor commit failed"), {}, ErrorCategory.CHECKPOINT),
        (AdmissionTimeoutError("admission timed out"), {}, ErrorCategory.ADMISSION_TIMEOUT),
        (BackpressureError("queue saturated"), {}, ErrorCategory.BACKPRESSURE),
        (ResourceLimitError("resource exhausted"), {}, ErrorCategory.RESOURCE),
        (ConfigurationError("invalid settings"), {}, ErrorCategory.CONFIGURATION),
        (DataQualityError("invalid observation"), {}, ErrorCategory.DATA_QUALITY),
        (asyncio.CancelledError(), {}, ErrorCategory.SHUTDOWN),
        (RuntimeError("unclassified"), {}, ErrorCategory.UNKNOWN),
    ],
)
def test_normalizes_operational_failures_without_using_exception_message(error, kwargs, category):
    normalized = normalize_error(error, **kwargs)
    assert normalized.category is category
    assert normalized.exception_type
    serialized = json.dumps(normalized.to_dict())
    assert not str(error) or str(error) not in serialized


def test_numeric_provider_code_is_preserved_but_arbitrary_code_is_not():
    assert normalize_error(RuntimeError(), error_code="40010").code == "40010"
    assert normalize_error(RuntimeError(), error_code="test-secret-value").code is None
    assert normalize_error(RuntimeError(), error_code=123).code == "123"
    assert normalize_error(RuntimeError(), http_status=429).code == "429"


def test_normalized_error_repr_and_output_never_contain_secret_or_payload():
    secret = "api-key: test-secret-value https://rpc.example/private/token"
    error = RuntimeError(f"failed {secret} raw_payload={{'block': 'large'}}")

    normalized = normalize_error(error, error_code=secret)
    serialized = json.dumps(normalized.to_dict())

    assert secret not in repr(normalized)
    assert "test-secret-value" not in serialized
    assert "rpc.example" not in serialized
    assert "raw_payload" not in serialized
    assert set(normalized.to_dict()) == {"category", "exception_type", "code", "retryable"}


def test_provider_http_status_can_be_read_without_exposing_response_body():
    class ResponseFailure(Exception):
        status = 504

    normalized = normalize_error(ResponseFailure("Authorization: Bearer hidden"))
    assert normalized.category is ErrorCategory.PROVIDER_TIMEOUT
    assert "hidden" not in repr(normalized)


def test_normalized_error_rejects_retryability_that_conflicts_with_category():
    with pytest.raises(ValueError, match="retryable"):
        NormalizedError(
            category=ErrorCategory.NETWORK,
            exception_type="CONNECTIONERROR",
            code=None,
            retryable=False,
        )
