"""Serialization and safe, configured retention statements."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from datetime import datetime
from enum import Enum
import json
from typing import Any, Mapping

from .stage1 import Stage1Result


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def stage1_result_json(result: Stage1Result) -> dict[str, Any]:
    payload = json.loads(json.dumps(asdict(result), default=_json_default))
    payload["classification"] = result.classification
    return payload


def retention_delete_statements(retention_days: Mapping[str, int]) -> list[tuple[str, tuple[str, int]]]:
    statements: list[tuple[str, tuple[str, int]]] = []
    for interval in ("5m", "15m", "1H", "4H"):
        days = int(retention_days[interval])
        if days <= 0:
            raise ValueError("retention days must be positive")
        statements.append(
            (
                "DELETE FROM klines WHERE interval = %s AND bar_open_timestamp < now() - (%s * INTERVAL '1 day')",
                (interval, days),
            )
        )
    return statements
