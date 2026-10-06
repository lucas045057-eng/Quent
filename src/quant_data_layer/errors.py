"""Low-cardinality error normalization with no exception-message retention."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import re
from typing import Any

from .observability import ErrorCategory


_TOKEN = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_NUMERIC_CODE = re.compile(r"[0-9]{1,12}")
_RETRYABLE = frozenset(
    {
        ErrorCategory.PROVIDER_RATE_LIMIT,
        ErrorCategory.PROVIDER_TIMEOUT,
        ErrorCategory.NETWORK,
        ErrorCategory.PERSISTENCE,
        ErrorCategory.BACKPRESSURE,
        ErrorCategory.ADMISSION_TIMEOUT,
    }
)


@dataclass(frozen=True, slots=True)
class NormalizedError:
    """Safe error evidence; deliberately excludes exception text and payloads."""

    category: ErrorCategory
    exception_type: str
    code: str | None
    retryable: bool

    def __post_init__(self) -> None:
        if not isinstance(self.category, ErrorCategory):
            raise TypeError("category must be a registered ErrorCategory")
        if not isinstance(self.exception_type, str) or _TOKEN.fullmatch(self.exception_type) is None:
            raise ValueError("exception_type must be a bounded type token")
        if self.code is not None and _NUMERIC_CODE.fullmatch(self.code) is None:
            raise ValueError("error code must contain only bounded numeric digits")
        if not isinstance(self.retryable, bool):
            raise TypeError("retryable must be boolean")
        if self.retryable != (self.category in _RETRYABLE):
            raise ValueError("retryable must match the normalized error category")

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "exception_type": self.exception_type,
            "code": self.code,
            "retryable": self.retryable,
        }

    @property
    def safe_summary(self) -> str:
        suffix = f".{self.code}" if self.code is not None else ""
        return f"{self.category.value}.{self.exception_type}{suffix}"


def normalize_error(
    error: BaseException,
    *,
    http_status: int | None = None,
    error_code: str | int | None = None,
) -> NormalizedError:
    """Classify an exception without serializing its message, args, or response body."""

    if not isinstance(error, BaseException):
        raise TypeError("error must be an exception")
    name = type(error).__name__
    token = _exception_type_token(name)
    status = _http_status(error, http_status)
    category = _classify(error, name=name, status=status)
    code = _safe_numeric_code(error_code)
    if code is None:
        code = _safe_numeric_code(status)
    if code is None:
        code = _safe_numeric_code(getattr(error, "errno", None))
    if code is None:
        code = _safe_numeric_code(getattr(error, "code", None))
    return NormalizedError(
        category=category,
        exception_type=token,
        code=code,
        retryable=category in _RETRYABLE,
    )


def _classify(error: BaseException, *, name: str, status: int | None) -> ErrorCategory:
    lowered_name = name.lower()
    module = type(error).__module__.lower()

    if isinstance(error, asyncio.CancelledError):
        return ErrorCategory.SHUTDOWN
    if "admission" in lowered_name and "timeout" in lowered_name:
        return ErrorCategory.ADMISSION_TIMEOUT
    if "backpressure" in lowered_name:
        return ErrorCategory.BACKPRESSURE
    if isinstance(error, MemoryError) or "resource" in lowered_name:
        return ErrorCategory.RESOURCE
    if "checkpoint" in lowered_name:
        return ErrorCategory.CHECKPOINT
    if module.startswith(("psycopg", "sqlalchemy")) or any(
        marker in lowered_name for marker in ("persistence", "databaseerror", "integrityerror")
    ):
        return ErrorCategory.PERSISTENCE
    if status == 429 or "ratelimit" in lowered_name or "rate_limit" in lowered_name:
        return ErrorCategory.PROVIDER_RATE_LIMIT
    if status in {408, 504} or isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return ErrorCategory.PROVIDER_TIMEOUT
    if "contract" in lowered_name or "schema" in lowered_name:
        return ErrorCategory.PROVIDER_CONTRACT
    if "parser" in lowered_name or isinstance(error, (json.JSONDecodeError, UnicodeDecodeError)):
        return ErrorCategory.PARSER
    if "dataquality" in lowered_name or "integrity" in lowered_name:
        return ErrorCategory.DATA_QUALITY
    if "configuration" in lowered_name or module.startswith(("pydantic", "pydantic_core")):
        return ErrorCategory.CONFIGURATION
    if status is not None and 400 <= status < 500:
        return ErrorCategory.PROVIDER_CONTRACT
    if isinstance(error, (ConnectionError, OSError)) or (status is not None and status >= 500):
        return ErrorCategory.NETWORK
    return ErrorCategory.UNKNOWN


def _http_status(error: BaseException, explicit: int | None) -> int | None:
    candidates = (explicit, getattr(error, "status", None), getattr(error, "status_code", None))
    for candidate in candidates:
        if isinstance(candidate, int) and not isinstance(candidate, bool) and 100 <= candidate <= 599:
            return candidate
    return None


def _safe_numeric_code(candidate: Any) -> str | None:
    if isinstance(candidate, int) and not isinstance(candidate, bool):
        candidate = str(candidate)
    if isinstance(candidate, str) and _NUMERIC_CODE.fullmatch(candidate):
        return candidate
    return None


def _exception_type_token(name: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_]", "_", name).upper()
    token = re.sub(r"_+", "_", token).strip("_") or "UNKNOWN_EXCEPTION"
    if not token[0].isalpha():
        token = f"ERROR_{token}"
    return token[:64]
