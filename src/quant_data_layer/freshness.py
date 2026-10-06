"""Shared freshness policy and timestamp-lag accounting across pipeline stages.

This module contains policy only. Canonical observations stay in the existing
Phase 1-8 tables; callers pass their event/capture timestamps for assessment.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Mapping


BITGET_PRICE_STAGE1_FUTURE_SKEW_SECONDS = 2.0
BITGET_PRICE_STAGE1_SOURCES = frozenset({"bitget_v3_rest", "bitget_v3_ws"})


@dataclass(frozen=True, slots=True)
class FreshnessPolicy:
    data_type: str
    expected_cadence: str
    expected_cadence_seconds: int | None
    soft_seconds: float
    hard_seconds: float
    reason: str

    def __post_init__(self) -> None:
        if not self.data_type or not self.expected_cadence:
            raise ValueError("freshness policy requires a data type and cadence")
        if self.soft_seconds < 0 or self.hard_seconds < self.soft_seconds:
            raise ValueError("freshness hard threshold must be >= soft threshold >= 0")
        if self.expected_cadence_seconds is not None and self.expected_cadence_seconds <= 0:
            raise ValueError("expected cadence must be positive")


FRESHNESS_POLICY: Mapping[str, FreshnessPolicy] = {
    "PRICE_STAGE1": FreshnessPolicy(
        "PRICE_STAGE1", "Bitget public ticker stream (~5s observed fetch cadence)", 5, 30, 60,
        "15m-4h Stage1 uses closed bars; ticker is a context/price anchor. Warn after 30s and fail stale after 60s.",
    ),
    "PRICE_DECISION": FreshnessPolicy(
        "PRICE_DECISION", "5m/15m/1h/4h closed-candle cadence plus current-price evidence", None, 30, 60,
        "Phase9 price evidence may use up to 60s current-price age; closed candles retain interval-aware Stage1 grace.",
    ),
    "PRICE_EXECUTION": FreshnessPolicy(
        "PRICE_EXECUTION", "quote snapshot immediately before each execution intent", 5, 2, 5,
        "Keep the existing strict 5s execution-price ceiling; an approved RiskPolicy may be stricter, never looser.",
    ),
    "TRADE_FLOW": FreshnessPolicy(
        "TRADE_FLOW", "5m aggregation windows; Phase3 minimum subscription is 300s", 300, 60, 305,
        "Window close is the freshness clock; hard limit is one 5m window plus Phase3's 5s allowed lateness.",
    ),
    "OPEN_INTEREST": FreshnessPolicy(
        "OPEN_INTEREST", "Phase2 provider poll every 300s", 300, 360, 600,
        "Soft allows one poll plus 60s; hard allows at most two polls and still exposes the observed ~1,103s P99 gap as backlog.",
    ),
    "FUNDING": FreshnessPolicy(
        "FUNDING", "Phase2 provider poll every 300s (settlement interval is not observation cadence)", 300, 360, 600,
        "Soft allows one poll plus 60s; hard is two polls. Missing exchange event time stays unknown and is reported separately from observation age.",
    ),
    "LIQUIDATION": FreshnessPolicy(
        "LIQUIDATION", "event-driven; no-event periods are not stale by themselves", None, 20, 60,
        "Threshold applies to received-event lag only; feed silence needs a subscription heartbeat, not an invented event cadence.",
    ),
}


def _as_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value


def _seconds(later: datetime | str | None, earlier: datetime | str | None) -> float | None:
    later_dt, earlier_dt = _as_datetime(later), _as_datetime(earlier)
    if later_dt is None or earlier_dt is None:
        return None
    return (later_dt - earlier_dt).total_seconds()


def measure_lags(
    *, now: datetime | str,
    source_event_time: datetime | str | None,
    fetched_at: datetime | str | None,
    processed_at: datetime | str | None,
) -> dict[str, float | None]:
    """Return distinct source age, fetch-ingest lag, processing lag and receipt age.

    A missing provider event timestamp is never substituted with fetch time in
    ``source_event_age``. ``observation_age`` remains available for APIs whose
    source event time is not part of their contract.
    """
    return {
        "source_event_age": _seconds(now, source_event_time),
        "ingest_lag": _seconds(fetched_at, source_event_time),
        "processing_lag": _seconds(processed_at, fetched_at),
        "observation_age": _seconds(now, fetched_at),
    }


def bitget_price_source_clock_skew_tolerance(
    data_type: str | None,
    exchange: str | None,
    source: str | None,
) -> float:
    """Allow approved bounded Bitget clock skew while receipt age stays strict."""
    if (
        str(data_type or "").upper() in {"PRICE_STAGE1", "PRICE_EXECUTION"}
        and str(exchange or "").lower() == "bitget"
        and str(source or "").lower() in BITGET_PRICE_STAGE1_SOURCES
    ):
        return BITGET_PRICE_STAGE1_FUTURE_SKEW_SECONDS
    return 0.0


def assess_freshness(
    age_seconds: float | None,
    policy: FreshnessPolicy,
    *,
    future_skew_tolerance_seconds: float = 0.0,
) -> str:
    if age_seconds is None:
        return "UNKNOWN"
    try:
        age = float(age_seconds)
    except (TypeError, ValueError, OverflowError):
        return "INVALID"
    if not math.isfinite(age):
        return "INVALID"
    try:
        future_skew_tolerance = float(future_skew_tolerance_seconds)
    except (TypeError, ValueError, OverflowError):
        return "INVALID"
    if not math.isfinite(future_skew_tolerance) or future_skew_tolerance < 0:
        return "INVALID"
    if policy.data_type not in {"PRICE_STAGE1", "PRICE_EXECUTION"}:
        future_skew_tolerance = 0.0
    if age < -future_skew_tolerance:
        return "INVALID_FUTURE_TIMESTAMP"
    if age < 0:
        age = 0.0
    if age <= policy.soft_seconds:
        return "FRESH"
    if age <= policy.hard_seconds:
        return "DEGRADED"
    return "STALE"


def kline_freshness_policy(interval: str, interval_seconds: int, grace_seconds: int) -> FreshnessPolicy:
    if interval_seconds <= 0 or grace_seconds < 0:
        raise ValueError("kline interval must be positive and grace non-negative")
    return FreshnessPolicy(
        "PRICE_DECISION", f"{interval} closed-bar cadence", interval_seconds,
        float(interval_seconds), float(interval_seconds + grace_seconds),
        f"Use the latest closed {interval} candle; existing Phase1 grace is {grace_seconds}s.",
    )
