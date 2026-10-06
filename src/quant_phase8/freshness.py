"""Source-specific freshness and no-future-leakage input selection for Phase 8."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Sequence

from .config import Phase8Settings
from .contracts import (
    DataStatus,
    ObservationKind,
    OptionMarketObservation,
    OptionMetricValue,
    TimestampSemantics,
)


@dataclass(frozen=True, slots=True)
class FreshnessResult:
    status: DataStatus
    reason_code: str | None
    data_age_seconds: int | None
    source_timestamp: datetime | None
    capture_timestamp: datetime | None
    source_time_quality: str


@dataclass(frozen=True, slots=True)
class SelectedContextInput:
    observation: OptionMarketObservation
    metric_name: str
    metric: OptionMetricValue
    freshness: FreshnessResult

    @property
    def symbol(self) -> str:
        return self.observation.symbol

    @property
    def exchange(self) -> str:
        return self.observation.exchange

    @property
    def source(self) -> str:
        return self.observation.source

    @property
    def observation_kind(self) -> ObservationKind:
        return self.observation.observation_kind

    @property
    def capture_timestamp(self) -> datetime:
        assert self.freshness.capture_timestamp is not None
        return self.freshness.capture_timestamp

    @property
    def exchange_timestamp(self) -> datetime | None:
        return self.freshness.source_timestamp


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _capture_timestamp(
    observation: OptionMarketObservation, metric: OptionMetricValue
) -> datetime | None:
    if observation.observation_kind is ObservationKind.REST_CHAIN_SUMMARY:
        return metric.fetched_at or observation.fetched_at
    return metric.received_at or observation.received_at


def _source_timestamp(
    observation: OptionMarketObservation, metric: OptionMetricValue
) -> datetime | None:
    return metric.field_last_updated_at or metric.exchange_timestamp or observation.exchange_timestamp


def _kind_max_age(kind: ObservationKind, settings: Phase8Settings) -> int:
    if kind is ObservationKind.REST_CHAIN_SUMMARY:
        return settings.chain_stale_after_seconds
    if kind in {ObservationKind.WS_MARKPRICE_SNAPSHOT, ObservationKind.WS_MARKPRICE_CHANGE}:
        return settings.markprice_stale_after_seconds
    return settings.ticker_stale_after_seconds


def evaluate_observation_metric_freshness(
    observation: OptionMarketObservation,
    metric_name: str,
    *,
    as_of: datetime,
    settings: Phase8Settings,
) -> FreshnessResult:
    """Evaluate one field without allowing a later capture or future source time."""
    if not isinstance(observation, OptionMarketObservation):
        raise TypeError("observation must be a canonical OptionMarketObservation")
    if not isinstance(settings, Phase8Settings):
        raise TypeError("settings must be validated Phase8Settings")
    if not isinstance(metric_name, str) or metric_name not in observation.metrics:
        raise KeyError("metric is not present in the observation")
    as_of = _utc(as_of, "as_of")
    metric = observation.metrics[metric_name]
    capture = _capture_timestamp(observation, metric)
    source_time = _source_timestamp(observation, metric)
    semantics = metric.timestamp_semantics
    if capture is not None:
        capture = _utc(capture, "capture_timestamp")
    if source_time is not None:
        source_time = _utc(source_time, "source_timestamp")

    if capture is None:
        return FreshnessResult(
            DataStatus.NOT_AVAILABLE, "CAPTURE_TIME_NOT_PROVIDED", None,
            source_time, None, "UNKNOWN",
        )
    if capture > as_of:
        return FreshnessResult(
            DataStatus.ERROR, "FUTURE_CAPTURE_TIMESTAMP", None,
            source_time, capture, "INVALID",
        )
    if source_time is not None and source_time > as_of:
        return FreshnessResult(
            DataStatus.ERROR, "FUTURE_SOURCE_TIMESTAMP", None,
            source_time, capture, "INVALID",
        )

    status = metric.status
    if status in {DataStatus.ERROR, DataStatus.NOT_AVAILABLE, DataStatus.STALE} or metric.value is None:
        reason = metric.quality_reason or f"SOURCE_FIELD_{status.value}"
        age = max(0, int((as_of - capture).total_seconds()))
        return FreshnessResult(status, reason, age, source_time, capture, str(semantics))

    verified_source_time = (
        source_time is not None and semantics is TimestampSemantics.VERIFIED
    )
    if verified_source_time and abs((capture - source_time).total_seconds()) > settings.max_source_skew_seconds:
        return FreshnessResult(
            DataStatus.STALE, "SOURCE_TIME_SKEW",
            max(0, int((as_of - source_time).total_seconds())),
            source_time, capture, "SKEWED",
        )

    # REST summary has no reliable provider event time in the current contract:
    # its age is always the actual request capture time, not a context timestamp.
    age_from = source_time if verified_source_time and observation.observation_kind is not ObservationKind.REST_CHAIN_SUMMARY else capture
    age = max(0, int((as_of - age_from).total_seconds()))
    if age > _kind_max_age(observation.observation_kind, settings):
        return FreshnessResult(
            DataStatus.STALE,
            "SOURCE_SNAPSHOT_STALE" if observation.observation_kind is ObservationKind.REST_CHAIN_SUMMARY else "FIELD_STALE",
            age, source_time, capture,
            "VERIFIED" if verified_source_time else "UNVERIFIED_OR_MISSING",
        )

    if status is DataStatus.PARTIAL:
        quality = "VERIFIED" if verified_source_time else "DEGRADED"
        return FreshnessResult(
            DataStatus.PARTIAL, metric.quality_reason or "SOURCE_FIELD_PARTIAL",
            age, source_time, capture, quality,
        )
    if verified_source_time:
        return FreshnessResult(DataStatus.AVAILABLE, None, age, source_time, capture, "VERIFIED")
    reason = "SOURCE_TIME_NOT_PROVIDED" if source_time is None else "SOURCE_TIME_UNVERIFIED"
    return FreshnessResult(DataStatus.PARTIAL, reason, age, source_time, capture, "DEGRADED")


def _evaluate_local_capture_freshness(
    capture_timestamp: datetime | None,
    *,
    as_of: datetime,
    max_age_seconds: int,
    missing_reason: str,
    stale_reason: str,
) -> FreshnessResult:
    as_of = _utc(as_of, "as_of")
    if capture_timestamp is None:
        return FreshnessResult(DataStatus.NOT_AVAILABLE, missing_reason, None, None, None, "NOT_APPLICABLE")
    capture_timestamp = _utc(capture_timestamp, "capture_timestamp")
    if capture_timestamp > as_of:
        return FreshnessResult(DataStatus.ERROR, "FUTURE_CAPTURE_TIMESTAMP", None, None, capture_timestamp, "INVALID")
    age = max(0, int((as_of - capture_timestamp).total_seconds()))
    if age > max_age_seconds:
        return FreshnessResult(DataStatus.STALE, stale_reason, age, None, capture_timestamp, "NOT_APPLICABLE")
    return FreshnessResult(DataStatus.AVAILABLE, None, age, None, capture_timestamp, "NOT_APPLICABLE")


def evaluate_catalog_freshness(
    fetched_at: datetime | None, *, as_of: datetime, settings: Phase8Settings
) -> FreshnessResult:
    return _evaluate_local_capture_freshness(
        fetched_at, as_of=as_of, max_age_seconds=settings.catalog_stale_after_seconds,
        missing_reason="CATALOG_NOT_FETCHED", stale_reason="CATALOG_STALE",
    )


def evaluate_lifecycle_freshness(
    last_received_at: datetime | None, *, as_of: datetime, settings: Phase8Settings
) -> FreshnessResult:
    return _evaluate_local_capture_freshness(
        last_received_at, as_of=as_of, max_age_seconds=settings.lifecycle_stale_after_seconds,
        missing_reason="LIFECYCLE_HEARTBEAT_NOT_RECEIVED", stale_reason="LIFECYCLE_HEARTBEAT_STALE",
    )


def _candidate_order(item: SelectedContextInput) -> tuple[datetime, datetime, str]:
    source_time = item.freshness.source_timestamp
    metric = item.metric
    semantics = metric.timestamp_semantics
    event_order = source_time if source_time is not None and semantics is TimestampSemantics.VERIFIED else item.capture_timestamp
    digest_input = "|".join((str(metric.value), str(metric.status), metric.source_field or "", metric.raw_reference or ""))
    return event_order, item.capture_timestamp, sha256(digest_input.encode("utf-8")).hexdigest()


def select_context_inputs(
    observations: Sequence[OptionMarketObservation],
    *,
    as_of: datetime,
    settings: Phase8Settings,
) -> tuple[SelectedContextInput, ...]:
    """Select the newest known-time/event-time value independently for each field."""
    as_of = _utc(as_of, "as_of")
    selected: dict[tuple[str, str, str, str], SelectedContextInput] = {}
    for observation in observations:
        if not isinstance(observation, OptionMarketObservation):
            raise TypeError("observations must contain canonical OptionMarketObservation values")
        for metric_name, metric in observation.metrics.items():
            freshness = evaluate_observation_metric_freshness(
                observation, metric_name, as_of=as_of, settings=settings
            )
            if freshness.status is DataStatus.ERROR or freshness.capture_timestamp is None:
                continue
            candidate = SelectedContextInput(observation, metric_name, metric, freshness)
            key = (observation.exchange, observation.source, observation.symbol, f"{observation.observation_kind}:{metric_name}")
            current = selected.get(key)
            if current is None or _candidate_order(candidate) > _candidate_order(current):
                selected[key] = candidate
    return tuple(sorted(
        selected.values(),
        key=lambda item: (item.exchange, item.source, item.symbol, str(item.observation_kind), item.metric_name),
    ))
