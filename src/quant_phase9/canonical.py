from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from collections.abc import Mapping
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from quant_phase9.contracts import EvaluationIdentityV1, EventIdentityV1, Sha256Hex


_KNOWN_SCHEMAS = frozenset(
    {
        "PHASE9_STAGE1_CANDIDATE_EVENT_V1",
        "PHASE9_SOURCE_PROJECTION_V1",
        "PHASE9_EVALUATION_SNAPSHOT_V1",
        "PHASE9_EVIDENCE_ITEM_V1",
        "PHASE9_EVIDENCE_CHAIN_V1",
        "PHASE9_PATTERN_MATCH_V1",
        "PHASE9_JEV_SAFE_CONTEXT_V1",
        "PHASE9_POLICY_MANIFEST_REF_V1",
        "PHASE9_DECISION_CANDIDATE_V1",
        "PHASE9_DECISION_STATUS_EVENT_V1",
        "PHASE9_REPLAY_V1",
        "PHASE9_STAGE1_CANDIDATE_FIXTURE_V1",
        "PHASE9_EVALUATION_SNAPSHOT_FIXTURE_V1",
        "PHASE9_RECORDED_JEV_REVIEW_FIXTURE_V1",
        "PHASE9_REPLAY_RESULT_V1",
        "PHASE9_ACCEPTANCE_COMMAND_V1",
        "PHASE9_FINAL_ACCEPTANCE_V1",
        "PHASE9_IMPLEMENTATION_COMPLETION_V1",
        "PHASE9_COMPATIBILITY_CLOSURE_MARKER_V1",
        "PHASE9_COMPATIBILITY_SUITE_EVIDENCE_V1",
        "PHASE9_COMPATIBILITY_GATE_V1",
        "PHASE9_JEV_REVIEW_REQUEST_V1",
    }
)


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("canonical datetimes must be timezone-aware UTC")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError("canonical datetimes must be UTC")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("non-finite Decimal values are not canonical")
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _to_json_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise TypeError("binary floats are not permitted in canonical values")
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, datetime):
        return _utc_text(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return _to_json_value(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _to_json_value(getattr(value, field.name)) for field in fields(value)
                if not (field.metadata.get("omit_if_none") and getattr(value, field.name) is None)}
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical object keys must be strings")
        return {key: _to_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_to_json_value(item) for item in value]
    raise TypeError(f"unsupported canonical value type: {type(value).__name__}")


def canonical_json(value: object) -> str:
    """Serialize a contract value using the Phase 9 canonical JSON rules."""
    return json.dumps(
        _to_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: object) -> bytes:
    return canonical_json(value).encode("utf-8")


def canonical_sha256(value: object) -> Sha256Hex:
    return Sha256Hex(hashlib.sha256(canonical_bytes(value)).hexdigest())


def parse_canonical_json(data: bytes) -> object:
    """Parse canonical UTF-8 JSON, rejecting duplicate keys and float numbers."""
    if not isinstance(data, bytes):
        raise TypeError("canonical JSON input must be bytes")

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_float(token: str) -> object:
        raise ValueError(f"JSON floating-point number is not canonical: {token}")

    def reject_constant(token: str) -> object:
        raise ValueError(f"non-finite JSON number is not canonical: {token}")

    text = data.decode("utf-8", errors="strict")
    parsed = json.loads(
        text,
        object_pairs_hook=reject_duplicate_keys,
        parse_float=reject_float,
        parse_constant=reject_constant,
    )
    _validate_schema_versions(parsed)
    if canonical_json(parsed).encode("utf-8") != data:
        raise ValueError("input is valid JSON but not in canonical form")
    return parsed


def _validate_schema_versions(value: object) -> None:
    if isinstance(value, dict):
        schema = value.get("schema")
        if isinstance(schema, str) and schema.startswith("PHASE9_") and schema not in _KNOWN_SCHEMAS:
            raise ValueError(f"unknown Phase 9 schema version: {schema}")
        for child in value.values():
            _validate_schema_versions(child)
    elif isinstance(value, list):
        for child in value:
            _validate_schema_versions(child)


def identity_preimage_v1(identity: EvaluationIdentityV1) -> dict[str, object]:
    if not isinstance(identity, EvaluationIdentityV1):
        raise TypeError("identity must be EvaluationIdentityV1")
    return {
        "stage1_candidate_id": identity.stage1_candidate_id,
        "timeframe": identity.timeframe,
        "evaluation_window_start": _utc_text(identity.evaluation_window_start),
        "evaluation_window_end": _utc_text(identity.evaluation_window_end),
        "policy_generation": identity.policy_generation,
        "material_change_generation": identity.material_change_generation,
    }


def evaluation_id_for(identity: EvaluationIdentityV1) -> UUID:
    return uuid5(NAMESPACE_URL, canonical_json(identity_preimage_v1(identity)))


def event_id_for(identity: EventIdentityV1) -> str:
    if not isinstance(identity, EventIdentityV1):
        raise TypeError("identity must be EventIdentityV1")
    return hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
