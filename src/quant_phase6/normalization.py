"""Deterministic normalization, fingerprints, deduplication, and freshness."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, TypeVar
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from quant_phase1.time import ensure_utc, utc_now

from .contracts import (
    EventStatus,
    ExternalEvent,
    Importance,
    MacroEvent,
    NewsEvent,
    Provenance,
    RawReference,
    RecipientCategory,
    UnlockEvent,
    compute_macro_surprise,
)
from .sources import SourceDefinition, SourceRegistry


class NormalizationError(ValueError):
    """The source payload cannot satisfy the canonical contract."""


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    news_max_age: timedelta = timedelta(hours=24)
    macro_max_age: timedelta = timedelta(days=7)
    unlock_max_age: timedelta = timedelta(days=30)

    def __post_init__(self) -> None:
        for name, value in (
            ("news_max_age", self.news_max_age),
            ("macro_max_age", self.macro_max_age),
            ("unlock_max_age", self.unlock_max_age),
        ):
            if value <= timedelta(0):
                raise ValueError(f"{name} must be positive")


TEvent = TypeVar("TEvent", bound=ExternalEvent)


def normalize_news(
    registry: SourceRegistry,
    payload: dict[str, Any],
    *,
    observed_at: datetime,
    fetched_at: datetime,
    processed_at: datetime | None = None,
    source_id: str | None = None,
) -> NewsEvent:
    source = _source(registry, source_id or _required_text(payload, "source_id", default=None))
    source_ref = _required_text(payload, "source_ref", default=payload.get("id"))
    if source_ref is None:
        raise NormalizationError("source_ref is required")
    headline = _required_text(payload, "headline", default=None)
    url = _canonical_url(payload.get("url"))
    if payload.get("url") and (url is None or not registry.validate_url(source.source_id, url)):
        raise NormalizationError("source URL is not approved")
    published_at = _parse_optional_time(payload.get("published_at"), "published_at")
    event_at = _parse_optional_time(payload.get("event_at"), "event_at")
    observed_at, fetched_at, processed_at = _times(observed_at, fetched_at, processed_at)
    content_hash, raw_reference = _content_reference(source, source_ref, payload)
    reason = None
    status = EventStatus.AVAILABLE
    event_type = _required_text(payload, "event_type", default=None)
    if event_type is None:
        event_type = "UNKNOWN"
        status, reason = EventStatus.NOT_AVAILABLE, "MISSING_EVENT_TYPE"
    elif published_at is None:
        status, reason = EventStatus.PARTIAL, "MISSING_PUBLISHED_AT"
    elif not payload.get("summary"):
        status, reason = EventStatus.PARTIAL, "MISSING_SUMMARY"
    return NewsEvent(
        event_id=f"{source.source_id}:{source_ref}",
        source=source.source_id,
        source_type=source.source_type,
        source_ref=source_ref,
        url=url,
        published_at=published_at,
        observed_at=observed_at,
        event_at=event_at,
        fetched_at=fetched_at,
        processed_at=processed_at,
        event_type=event_type,
        entities=_texts(payload.get("entities", ())),
        symbols=_texts(payload.get("symbols", ())),
        summary=str(payload.get("summary") or ""),
        importance=_enum_value(Importance, payload.get("importance"), Importance.NOT_AVAILABLE),
        status=status,
        confidence=_decimal(payload.get("confidence")),
        content_hash=content_hash,
        parser_version=source.parser_version,
        raw_reference=raw_reference,
        provenance=_provenance(source, source_ref, url, content_hash, observed_at, fetched_at, processed_at),
        reason_code=reason,
        sentiment=_optional_text(payload.get("sentiment")),
        headline=headline,
        impact_horizon=_optional_text(payload.get("impact_horizon")),
    )


def normalize_macro(
    registry: SourceRegistry,
    payload: dict[str, Any],
    *,
    observed_at: datetime,
    fetched_at: datetime,
    processed_at: datetime | None = None,
    source_id: str | None = None,
) -> MacroEvent:
    source = _source(registry, source_id or _required_text(payload, "source_id", default=None))
    source_ref = _required_text(payload, "source_ref", default=payload.get("id"))
    if source_ref is None:
        raise NormalizationError("source_ref is required")
    macro_type = _required_text(payload, "macro_event_type", default=payload.get("event_type"))
    region = _required_text(payload, "region", default=None)
    if macro_type is None or region is None:
        raise NormalizationError("macro event type and region are required")
    scheduled_at = _parse_optional_time(payload.get("scheduled_at"), "scheduled_at")
    released_at = _parse_optional_time(payload.get("released_at"), "released_at")
    observed_at, fetched_at, processed_at = _times(observed_at, fetched_at, processed_at)
    actual = _decimal(payload.get("actual"))
    forecast = _decimal(payload.get("forecast"))
    previous = _decimal(payload.get("previous"))
    unit = _optional_text(payload.get("unit"))
    forecast_unit = _optional_text(payload.get("forecast_unit")) or unit
    surprise = compute_macro_surprise(actual, forecast, unit=unit, forecast_unit=forecast_unit)
    content_hash, raw_reference = _content_reference(source, source_ref, payload)
    status = EventStatus.AVAILABLE
    reason = None
    if any(value is not None for value in (actual, forecast, previous)) and not unit:
        actual = forecast = previous = None
        status, reason = EventStatus.PARTIAL, "MISSING_UNIT"
    elif actual is not None and forecast is not None and unit != forecast_unit:
        surprise = None
        status, reason = EventStatus.PARTIAL, "INCOMPATIBLE_UNITS"
    elif scheduled_at is None and released_at is None:
        status, reason = EventStatus.PARTIAL, "MISSING_EVENT_TIME"
    return MacroEvent(
        event_id=f"{source.source_id}:{source_ref}",
        source=source.source_id,
        source_type=source.source_type,
        source_ref=source_ref,
        url=_approved_optional_url(registry, source, payload.get("url")),
        published_at=released_at,
        observed_at=observed_at,
        event_at=released_at or scheduled_at,
        fetched_at=fetched_at,
        processed_at=processed_at,
        event_type=macro_type,
        entities=_texts(payload.get("entities", ())),
        symbols=_texts(payload.get("symbols", ())),
        summary=str(payload.get("summary") or ""),
        importance=_enum_value(Importance, payload.get("importance"), Importance.NOT_AVAILABLE),
        status=status,
        confidence=_decimal(payload.get("confidence")),
        content_hash=content_hash,
        parser_version=source.parser_version,
        raw_reference=raw_reference,
        provenance=_provenance(source, source_ref, _approved_optional_url(registry, source, payload.get("url")), content_hash, observed_at, fetched_at, processed_at),
        reason_code=reason,
        macro_event_type=macro_type,
        region=region,
        scheduled_at=scheduled_at,
        released_at=released_at,
        actual=actual,
        forecast=forecast,
        previous=previous,
        unit=unit,
        surprise=surprise,
    )


def normalize_unlock(
    registry: SourceRegistry,
    payload: dict[str, Any],
    *,
    observed_at: datetime,
    fetched_at: datetime,
    processed_at: datetime | None = None,
    source_id: str | None = None,
) -> UnlockEvent:
    source = _source(registry, source_id or _required_text(payload, "source_id", default=None))
    source_ref = _required_text(payload, "source_ref", default=payload.get("id"))
    if source_ref is None:
        raise NormalizationError("source_ref is required")
    symbol = _required_text(payload, "symbol", default=None)
    asset = _required_text(payload, "asset", default=None)
    if symbol is None or asset is None:
        raise NormalizationError("unlock symbol and asset are required")
    event_at = _parse_optional_time(payload.get("event_at"), "event_at")
    observed_at, fetched_at, processed_at = _times(observed_at, fetched_at, processed_at)
    amount = _decimal(payload.get("amount"))
    amount_unit = _optional_text(payload.get("amount_unit"))
    supply = _decimal(payload.get("circulating_supply"))
    supply_unit = _optional_text(payload.get("circulating_supply_unit"))
    unlock_pct = None
    reason = None
    status = EventStatus.AVAILABLE
    if amount is None:
        status, reason = EventStatus.PARTIAL, "MISSING_UNLOCK_AMOUNT"
    elif supply is None or not payload.get("circulating_supply_ref"):
        status, reason = EventStatus.PARTIAL, "MISSING_CIRCULATING_SUPPLY"
    elif amount_unit != supply_unit or supply == 0:
        status, reason = EventStatus.PARTIAL, "INCOMPATIBLE_SUPPLY_UNITS"
    elif event_at is None:
        status, reason = EventStatus.PARTIAL, "MISSING_EVENT_TIME"
    else:
        unlock_pct = amount / supply
    content_hash, raw_reference = _content_reference(source, source_ref, payload)
    url = _approved_optional_url(registry, source, payload.get("url"))
    return UnlockEvent(
        event_id=f"{source.source_id}:{source_ref}",
        source=source.source_id,
        source_type=source.source_type,
        source_ref=source_ref,
        url=url,
        published_at=_parse_optional_time(payload.get("published_at"), "published_at"),
        observed_at=observed_at,
        event_at=event_at,
        fetched_at=fetched_at,
        processed_at=processed_at,
        event_type=_required_text(payload, "event_type", default="TOKEN_UNLOCK") or "TOKEN_UNLOCK",
        entities=_texts(payload.get("entities", ())),
        symbols=(symbol,),
        summary=str(payload.get("summary") or ""),
        importance=_enum_value(Importance, payload.get("importance"), Importance.NOT_AVAILABLE),
        status=status,
        confidence=_decimal(payload.get("confidence")),
        content_hash=content_hash,
        parser_version=source.parser_version,
        raw_reference=raw_reference,
        provenance=_provenance(source, source_ref, url, content_hash, observed_at, fetched_at, processed_at),
        reason_code=reason,
        symbol=symbol,
        asset=asset,
        amount=amount,
        amount_unit=amount_unit,
        value=_decimal(payload.get("value")),
        value_currency=_optional_text(payload.get("value_currency")),
        value_at=_parse_optional_time(payload.get("value_at"), "value_at"),
        circulating_supply=supply,
        circulating_supply_unit=supply_unit,
        circulating_supply_ref=_optional_text(payload.get("circulating_supply_ref")),
        unlock_pct=unlock_pct,
        recipient_category=_enum_value(RecipientCategory, payload.get("recipient_category"), RecipientCategory.UNKNOWN),
    )


def event_fingerprint(event: ExternalEvent) -> str:
    event_time = event.event_at or event.published_at or event.observed_at
    bucket = ensure_utc(event_time).replace(second=0, microsecond=0).isoformat()
    canonical = {
        "source": event.source,
        "source_type": str(event.source_type),
        "source_ref": event.source_ref if not event.url else None,
        "url": _canonical_url(event.url),
        "content_hash": event.content_hash,
        "event_type": event.event_type,
        "time_bucket": bucket,
        "entities": sorted(event.entities),
        "symbols": sorted(event.symbols),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class DedupIndex:
    """Bounded process-local replay index; PostgreSQL remains durable authority."""

    def __init__(self, max_entries: int = 10_000) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._keys: OrderedDict[str, None] = OrderedDict()
        self._max_entries = max_entries

    def accept(self, event: ExternalEvent) -> bool:
        key = event_fingerprint(event)
        if key in self._keys:
            self._keys.move_to_end(key)
            return False
        self._keys[key] = None
        self._keys.move_to_end(key)
        while len(self._keys) > self._max_entries:
            self._keys.popitem(last=False)
        return True


def freshness_status(
    event: ExternalEvent,
    *,
    now: datetime,
    policy: FreshnessPolicy,
    kind: str,
) -> EventStatus:
    now = ensure_utc(now)
    if event.status in {EventStatus.ERROR, EventStatus.NOT_AVAILABLE}:
        return event.status
    if kind == "news":
        reference = event.published_at or event.observed_at
        limit = policy.news_max_age
    elif kind == "macro":
        if isinstance(event, MacroEvent) and event.released_at is None and event.scheduled_at and event.scheduled_at > now:
            return event.status
        reference = getattr(event, "released_at", None) or getattr(event, "scheduled_at", None)
        limit = policy.macro_max_age
    elif kind == "unlock":
        reference = event.event_at or event.observed_at
        limit = policy.unlock_max_age
    else:
        raise ValueError(f"unsupported freshness kind: {kind}")
    if reference is None:
        return EventStatus.NOT_AVAILABLE
    age = now - ensure_utc(reference)
    if age < timedelta(0):
        return event.status
    return EventStatus.STALE if age > limit else event.status


def apply_freshness(
    event: TEvent,
    *,
    now: datetime,
    policy: FreshnessPolicy,
    kind: str,
) -> TEvent:
    status = freshness_status(event, now=now, policy=policy, kind=kind)
    if status is event.status:
        return event
    reason = f"STALE_{kind.upper()}" if status is EventStatus.STALE else "MISSING_EVENT_TIME"
    return replace(event, status=status, reason_code=reason)


def _source(registry: SourceRegistry, source_id: str | None) -> SourceDefinition:
    if not source_id:
        raise NormalizationError("source_id is required")
    try:
        return registry.require(source_id)
    except KeyError as exc:
        raise NormalizationError(str(exc)) from exc


def _required_text(payload: dict[str, Any], field: str, *, default: Any) -> str | None:
    value = payload.get(field, default)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _optional_text(value: Any) -> str | None:
    return None if value is None or not str(value).strip() else str(value).strip()


def _enum_value(enum_type, value: Any, default):
    if value is None:
        return default
    candidate = value.value if hasattr(value, "value") else str(value).strip().upper()
    try:
        return enum_type(candidate)
    except ValueError:
        return default


def _texts(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    return tuple(text for text in (str(item).strip() for item in value) if text)


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise NormalizationError("invalid decimal value") from exc
    if not result.is_finite():
        raise NormalizationError("decimal value must be finite")
    return result


def _parse_optional_time(value: Any, field: str) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise NormalizationError(f"invalid {field}") from exc
    try:
        return ensure_utc(parsed)
    except ValueError as exc:
        raise NormalizationError(f"{field} must be UTC") from exc


def _times(observed_at: datetime, fetched_at: datetime, processed_at: datetime | None):
    return ensure_utc(observed_at), ensure_utc(fetched_at), ensure_utc(processed_at or utc_now())


def _canonical_url(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    parsed = urlparse(str(value).strip())
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    params = [(key, val) for key, val in parse_qsl(parsed.query, keep_blank_values=True) if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}]
    return urlunparse(("https", parsed.hostname.lower(), parsed.path or "/", "", urlencode(sorted(params)), ""))


def _approved_optional_url(registry: SourceRegistry, source: SourceDefinition, value: Any) -> str | None:
    url = _canonical_url(value)
    if value and (url is None or not registry.validate_url(source.source_id, url)):
        raise NormalizationError("source URL is not approved")
    return url


def _content_reference(source: SourceDefinition, source_ref: str, payload: dict[str, Any]) -> tuple[str, RawReference]:
    try:
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        raise NormalizationError("payload is not JSON-compatible") from exc
    encoded = serialized.encode("utf-8")
    if len(encoded) > source.fetch_policy.max_bytes:
        raise NormalizationError("payload exceeds bounded source size")
    digest = hashlib.sha256(encoded).hexdigest()
    return digest, RawReference(reference=f"{source.source_id}:{source_ref}", content_hash=digest, byte_size=len(encoded))


def _provenance(source, source_ref, url, digest, observed_at, fetched_at, processed_at) -> Provenance:
    return Provenance(
        source_id=source.source_id,
        source_ref=source_ref,
        url=url,
        content_hash=digest,
        observed_at=observed_at,
        fetched_at=fetched_at,
        processed_at=processed_at,
        parser_version=source.parser_version,
        schema_version="phase6-event-v1",
        normalization_version="phase6-normalizer-v1",
    )
