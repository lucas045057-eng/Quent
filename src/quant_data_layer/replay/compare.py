from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class ReplayComparison:
    equal: bool
    reference_sha256: str
    differences: tuple[str, ...]


def _normalize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        normalized = value.astimezone(timezone.utc)
        return normalized.isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): _normalize(child) for key, child in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (tuple, list)):
        return [_normalize(child) for child in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError("replay outcomes must contain only deterministic JSON-compatible values")


def _difference_paths(left: Any, right: Any, path: str = "") -> list[str]:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        result: list[str] = []
        for key in sorted(set(left) | set(right), key=str):
            child = f"{path}.{key}" if path else str(key)
            if key not in left or key not in right:
                result.append(child)
            else:
                result.extend(_difference_paths(left[key], right[key], child))
        return result
    if isinstance(left, list) and isinstance(right, list):
        result = []
        for index in range(max(len(left), len(right))):
            child = f"{path}[{index}]"
            if index >= len(left) or index >= len(right):
                result.append(child)
            else:
                result.extend(_difference_paths(left[index], right[index], child))
        return result
    return [] if left == right else [path or "<root>"]


def compare_replay_outcomes(outcomes: Sequence[Mapping[str, Any]]) -> ReplayComparison:
    if len(outcomes) < 2:
        raise ValueError("at least two replay outcomes are required for deterministic comparison")
    normalized = tuple(_normalize(outcome) for outcome in outcomes)
    reference = normalized[0]
    encoded = json.dumps(reference, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    differences = sorted({
        path
        for candidate in normalized[1:]
        for path in _difference_paths(reference, candidate)
    })
    return ReplayComparison(not differences, hashlib.sha256(encoded).hexdigest(), tuple(differences))
