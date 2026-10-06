"""Explicit AI input allowlist and recursive secret redaction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlparse

from .contracts import ExternalEvent, MacroEvent, UnlockEvent


SECRET_KEY_MARKERS = (
    "api_key", "secret", "passphrase", "authorization", "cookie", "token",
    "password", "credential", "private_key", "database_password", "ssh_key",
)
_SAFE_PHASE5_FIELDS = {
    "direction_regime", "volatility_regime", "breadth_regime", "relative_class",
    "sector_relation", "status", "reason_code", "context_timestamp",
    "calculation_version", "coverage_ratio", "return_pct",
}


class SafeContextError(ValueError):
    """The requested AI context is outside the explicit allowlist."""


class SecretLeakError(SafeContextError):
    """A forbidden field was found while constructing or redacting context."""


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(child) for child in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(child) for child in value]
    return value


@dataclass(frozen=True, slots=True)
class AISafeContext:
    task_id: str
    allowed_fields: Mapping[str, Any]
    context_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_fields", _freeze_json(self.allowed_fields))

    def to_dict(self) -> dict[str, Any]:
        # Return a detached JSON value while keeping nested internal objects
        # immutable so evidence and context hashes cannot drift.
        return _thaw_json(self.allowed_fields)


def redact_sensitive(value: Any, *, strict: bool = False) -> tuple[Any, tuple[str, ...]]:
    """Remove forbidden-key branches and return their bounded paths.

    Strict mode is used for AI serialization and rejects instead of silently
    carrying a redacted object. Non-strict mode is intended for safe logging.
    """
    paths: list[str] = []

    def visit(item: Any, path: str) -> Any:
        if isinstance(item, Mapping):
            result: dict[str, Any] = {}
            for key, child in item.items():
                key_text = str(key)
                child_path = f"{path}.{key_text}" if path else key_text
                if _is_secret_key(key_text):
                    paths.append(child_path)
                    continue
                result[key_text] = visit(child, child_path)
            return result
        if isinstance(item, tuple):
            return tuple(visit(child, f"{path}[{index}]") for index, child in enumerate(item))
        if isinstance(item, list):
            return [visit(child, f"{path}[{index}]") for index, child in enumerate(item)]
        return item

    cleaned = visit(value, "")
    if strict and paths:
        raise SecretLeakError(f"forbidden fields in AI context: {', '.join(paths[:8])}")
    return cleaned, tuple(paths)


def build_safe_context(
    event: ExternalEvent,
    *,
    task_id: str,
    phase5_context: Mapping[str, Any] | None = None,
    max_bytes: int = 65_536,
) -> AISafeContext:
    if not task_id.strip():
        raise SafeContextError("task_id is required")
    if max_bytes <= 0:
        raise SafeContextError("max_bytes must be positive")
    url = _safe_url(event.url)
    phase5 = _safe_phase5_context(phase5_context or {})
    payload: dict[str, Any] = {
        "task_id": task_id,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "source": event.source,
        "source_type": event.source_type.value,
        "source_ref": event.source_ref,
        "url": url,
        "published_at": _iso(event.published_at),
        "observed_at": _iso(event.observed_at),
        "event_at": _iso(event.event_at),
        "symbols": list(event.symbols),
        "entities": list(event.entities),
        "headline": getattr(event, "headline", None),
        "summary": event.summary,
        "importance": event.importance.value,
        "sentiment": event.sentiment,
        "status": event.status.value,
        "reason_code": event.reason_code,
        "confidence": str(event.confidence) if event.confidence is not None else None,
        "provenance": {
            "source_id": event.provenance.source_id,
            "source_ref": event.provenance.source_ref,
            "content_hash": event.provenance.content_hash,
            "parser_version": event.provenance.parser_version,
            "schema_version": event.provenance.schema_version,
            "normalization_version": event.provenance.normalization_version,
            "observed_at": _iso(event.provenance.observed_at),
            "fetched_at": _iso(event.provenance.fetched_at),
            "processed_at": _iso(event.provenance.processed_at),
        },
        "phase5_context": phase5,
    }
    if isinstance(event, MacroEvent):
        payload["macro"] = {
            "macro_event_type": event.macro_event_type,
            "region": event.region,
            "scheduled_at": _iso(event.scheduled_at),
            "released_at": _iso(event.released_at),
            "actual": str(event.actual) if event.actual is not None else None,
            "forecast": str(event.forecast) if event.forecast is not None else None,
            "previous": str(event.previous) if event.previous is not None else None,
            "unit": event.unit,
            "surprise": str(event.surprise) if event.surprise is not None else None,
        }
    if isinstance(event, UnlockEvent):
        payload["unlock"] = {
            "symbol": event.symbol,
            "asset": event.asset,
            "amount": str(event.amount) if event.amount is not None else None,
            "amount_unit": event.amount_unit,
            "value": str(event.value) if event.value is not None else None,
            "value_currency": event.value_currency,
            "value_at": _iso(event.value_at),
            "circulating_supply": str(event.circulating_supply) if event.circulating_supply is not None else None,
            "circulating_supply_unit": event.circulating_supply_unit,
            "circulating_supply_ref": event.circulating_supply_ref,
            "unlock_pct": str(event.unlock_pct) if event.unlock_pct is not None else None,
            "recipient_category": event.recipient_category.value,
        }
    safe, paths = redact_sensitive(payload)
    if paths:
        raise SecretLeakError("forbidden fields entered safe context")
    encoded = json.dumps(safe, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_bytes:
        raise SafeContextError("AISafeContext exceeds bounded size")
    context_hash = hashlib.sha256(encoded).hexdigest()
    return AISafeContext(task_id=task_id, allowed_fields=MappingProxyType(safe), context_hash=context_hash)


def _is_secret_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return any(marker in normalized for marker in SECRET_KEY_MARKERS)


def _safe_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise SafeContextError("unsafe URL in AI context")
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if _is_secret_key(key) or key.lower() in {"sig", "signature", "expires", "x-amz-signature"}:
            raise SafeContextError("signed or credential-bearing URL in AI context")
    return value


def _safe_phase5_context(value: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(value) - _SAFE_PHASE5_FIELDS
    if unknown:
        raise SafeContextError(f"Phase 5 fields are not allowlisted: {sorted(unknown)}")
    result: dict[str, Any] = {}
    for key, child in value.items():
        if isinstance(child, (Mapping, list, tuple)):
            raise SafeContextError(f"Phase 5 field {key} must be scalar")
        if isinstance(child, datetime):
            result[key] = _iso(child)
        else:
            result[key] = child
    return result


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
