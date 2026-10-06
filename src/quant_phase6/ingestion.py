"""Bounded source-adapter orchestration without live network assumptions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from itertools import islice
from typing import Any, Iterable, Protocol

from .contracts import EventStatus, ExternalEvent
from .normalization import (
    DedupIndex,
    FreshnessPolicy,
    NormalizationError,
    apply_freshness,
    normalize_macro,
    normalize_news,
    normalize_unlock,
)
from .sources import SourceRegistry


class FetchError(RuntimeError):
    pass


class SourceFetcher(Protocol):
    def fetch(self, definition: Any) -> Iterable[dict[str, Any]]:
        ...


@dataclass(frozen=True, slots=True)
class IngestionResult:
    kind: str
    source_id: str
    status: EventStatus
    events: tuple[ExternalEvent, ...]
    reason_code: str | None = None
    rejected_count: int = 0
    truncated: bool = False


class ExternalEventIngestor:
    def __init__(
        self,
        registry: SourceRegistry,
        fetcher: SourceFetcher,
        *,
        freshness: FreshnessPolicy,
        max_events: int = 100,
    ) -> None:
        if max_events <= 0 or max_events > 10_000:
            raise ValueError("max_events must be between 1 and 10000")
        self.registry = registry
        self.fetcher = fetcher
        self.freshness = freshness
        self.max_events = max_events
        self.dedup = DedupIndex(max_entries=max_events * 10)

    def ingest(
        self,
        kind: str,
        source_id: str,
        *,
        observed_at: datetime,
        fetched_at: datetime,
        processed_at: datetime | None = None,
    ) -> IngestionResult:
        try:
            definition = self.registry.require(source_id)
        except KeyError:
            return IngestionResult(kind, source_id, EventStatus.NOT_AVAILABLE, (), "SOURCE_NOT_CONFIGURED")
        try:
            # Consume at most one item beyond the persisted bound so an
            # accidental/infinite upstream iterator cannot exhaust memory.
            payloads = tuple(islice(self.fetcher.fetch(definition), self.max_events + 1))
        except Exception as exc:  # source failures are isolated from the engine
            return IngestionResult(kind, source_id, EventStatus.ERROR, (), "SOURCE_FETCH_ERROR")
        truncated = len(payloads) > self.max_events
        accepted: list[ExternalEvent] = []
        rejected = 0
        for payload in payloads[: self.max_events]:
            try:
                event = self._normalize(kind, source_id, payload, observed_at, fetched_at, processed_at)
                event = apply_freshness(event, now=fetched_at, policy=self.freshness, kind=kind)
                if self.dedup.accept(event):
                    accepted.append(event)
            except (NormalizationError, ValueError, TypeError):
                rejected += 1
        status = _result_status(accepted, rejected, truncated)
        reason = None
        if not accepted and rejected:
            reason = "NORMALIZATION_ERROR"
        elif not accepted:
            reason = "NO_EVENTS"
        elif rejected or truncated:
            reason = "BOUNDED_PARTIAL_FEED"
        return IngestionResult(kind, source_id, status, tuple(accepted), reason, rejected, truncated)

    def _normalize(self, kind, source_id, payload, observed_at, fetched_at, processed_at):
        if kind == "news":
            return normalize_news(
                self.registry, payload, source_id=source_id,
                observed_at=observed_at, fetched_at=fetched_at, processed_at=processed_at,
            )
        if kind == "macro":
            return normalize_macro(
                self.registry, payload, source_id=source_id,
                observed_at=observed_at, fetched_at=fetched_at, processed_at=processed_at,
            )
        if kind == "unlock":
            return normalize_unlock(
                self.registry, payload, source_id=source_id,
                observed_at=observed_at, fetched_at=fetched_at, processed_at=processed_at,
            )
        raise ValueError(f"unsupported event kind: {kind}")


def _result_status(events: list[ExternalEvent], rejected: int, truncated: bool) -> EventStatus:
    if not events:
        return EventStatus.ERROR if rejected else EventStatus.NOT_AVAILABLE
    statuses = {event.status for event in events}
    if EventStatus.ERROR in statuses:
        return EventStatus.ERROR
    if EventStatus.STALE in statuses and all(status is EventStatus.STALE for status in statuses):
        return EventStatus.STALE
    if rejected or truncated or EventStatus.PARTIAL in statuses:
        return EventStatus.PARTIAL
    return EventStatus.AVAILABLE
