from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

import pytest

from quant_phase9.canonical import (
    canonical_bytes,
    canonical_json,
    canonical_sha256,
    event_id_for,
    evaluation_id_for,
    identity_preimage_v1,
    parse_canonical_json,
)
from quant_phase9.contracts import EvaluationIdentityV1, EventIdentityV1


def _identity(timeframe: str = "15m") -> EvaluationIdentityV1:
    return EvaluationIdentityV1(
        stage1_candidate_id=42,
        market="USDT_PERPETUAL",
        symbol="BTCUSDT",
        timeframe=timeframe,
        evaluation_window_start=datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc),
        evaluation_window_end=datetime(2026, 9, 27, 10, 15, tzinfo=timezone.utc),
        policy_generation="policy-1",
        material_change_generation="0",
    )


def test_canonical_json_sorts_object_keys_but_preserves_ordered_arrays():
    value = {"z": ["second", "first"], "a": {"y": 2, "x": 1}}
    assert canonical_json(value) == '{"a":{"x":1,"y":2},"z":["second","first"]}'
    assert canonical_bytes(value) == canonical_json(value).encode("utf-8")
    assert canonical_json({"a": 1, "z": ["second", "first"]}) != canonical_json(
        {"a": 1, "z": ["first", "second"]}
    )


def test_decimal_datetime_enum_and_explicit_null_have_canonical_forms():
    instant = datetime(2026, 9, 27, 2, 15, 1, 23, tzinfo=timezone.utc)
    assert canonical_json({"d": Decimal("1.23000"), "zero": Decimal("-0.000"),
                           "at": instant, "missing": None}) == (
        '{"at":"2026-09-27T02:15:01.000023Z","d":"1.23","missing":null,"zero":"0"}'
    )


def test_hash_helpers_are_sha256_over_exact_canonical_utf8_bytes():
    value = {"symbol": "比特币", "value": Decimal("0.00000001")}
    expected = hashlib.sha256(canonical_bytes(value)).hexdigest()
    assert canonical_sha256(value) == expected
    assert len(expected) == 64 and expected == expected.lower()


def test_evaluation_identity_uses_frozen_uuid5_preimage_and_timeframe():
    identity = _identity()
    preimage = identity_preimage_v1(identity)
    expected_fields = {
        "stage1_candidate_id": 42,
        "timeframe": "15m",
        "evaluation_window_start": "2026-09-27T10:00:00.000000Z",
        "evaluation_window_end": "2026-09-27T10:15:00.000000Z",
        "policy_generation": "policy-1",
        "material_change_generation": "0",
    }
    assert preimage == expected_fields
    assert evaluation_id_for(identity) == uuid5(NAMESPACE_URL, canonical_json(expected_fields))
    assert evaluation_id_for(identity) == evaluation_id_for(_identity())
    assert len({evaluation_id_for(_identity(frame)) for frame in ("15m", "1H", "4H")}) == 3


def test_event_identity_is_full_sha256_and_sensitive_to_each_contract_field():
    identity = EventIdentityV1(
        event_type="phase9.stage1_candidate",
        event_schema_version="1",
        screening_result_id=27,
        canonical_payload_digest="a" * 64,
    )
    event_id = event_id_for(identity)
    assert event_id == hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    assert len(event_id) == 64 and all(char in "0123456789abcdef" for char in event_id)
    assert event_id == event_id_for(identity)
    variants = (
        EventIdentityV1("phase9.stage1_candidate.v2", "1", 27, "a" * 64),
        EventIdentityV1("phase9.stage1_candidate", "2", 27, "a" * 64),
        EventIdentityV1("phase9.stage1_candidate", "1", 28, "a" * 64),
        EventIdentityV1("phase9.stage1_candidate", "1", 27, "b" * 64),
    )
    assert all(event_id_for(item) != event_id for item in variants)


@pytest.mark.parametrize("value", [0.1, float("inf"), float("nan")])
def test_canonical_serializer_rejects_binary_float_and_non_finite_values(value):
    with pytest.raises((TypeError, ValueError)):
        canonical_json({"value": value})


def test_canonical_serializer_rejects_naive_datetime_and_unsupported_values():
    with pytest.raises((TypeError, ValueError)):
        canonical_json({"at": datetime(2026, 9, 27)})
    with pytest.raises((TypeError, ValueError)):
        canonical_json({"value": object()})


def test_canonical_serializer_rejects_non_utc_datetime_offsets():
    local_time = datetime(2026, 9, 27, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    with pytest.raises(ValueError, match="UTC"):
        canonical_json({"at": local_time})


def test_parser_rejects_duplicate_keys_and_unknown_versioned_schema():
    with pytest.raises((TypeError, ValueError)):
        parse_canonical_json(b'{"x":1,"x":2}')
    with pytest.raises((TypeError, ValueError)):
        parse_canonical_json(b'{"schema":"PHASE9_UNKNOWN_SCHEMA_V9"}')


def test_parser_accepts_canonical_payload_and_keeps_null_distinct():
    parsed = parse_canonical_json(b'{"a":null,"b":[1,"x"]}')
    assert parsed == {"a": None, "b": [1, "x"]}
