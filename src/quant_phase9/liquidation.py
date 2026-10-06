"""Phase 4 liquidation projection preserving independent quality dimensions."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, UUID, uuid5

from .canonical import canonical_json, canonical_sha256
from .contracts import (
    EvidenceFreshnessV1,
    EvidenceQualityV1,
    PolicyCoverageStatusV1,
    PolicyDataStatusV1,
    SourcePhaseV1,
    SourceProjectionV1,
)


_SOURCE_CONTRACT = "LIQUIDATION_SOURCE_CONTRACT_V1"
_SOURCE_TYPE = "LIQUIDATION_WINDOW"
_EXPECTED_WINDOW_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1H": 3600, "4H": 14400}


def _utc(value: object, name: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be a timezone-aware UTC timestamp")
    return value.astimezone(timezone.utc)


def _nonnegative_int(value: object, name: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer or null")
    return value


def _health_gap(health_row: Mapping[str, object] | None) -> tuple[str, int | None, dict[str, object]]:
    if not isinstance(health_row, Mapping) or health_row.get("component") != "phase4-liquidation":
        return "UNKNOWN", None, {
            "gap_count": None,
            "gap_reason": None,
            "gap_detected_at": None,
            "gap_watermark_received_at": None,
            "gap_watermark_event_timestamp": None,
        }

    details = health_row.get("details")
    if not isinstance(details, Mapping):
        details = {}
    detected = details.get("gap_detected")
    count_value = details.get("gap_count")
    count = _nonnegative_int(count_value, "gap_count", optional=True)
    if detected is True or (count is not None and count > 0):
        gap_status = "KNOWN_GAP"
    elif detected is False and count == 0:
        gap_status = "NO_KNOWN_GAP"
    else:
        gap_status = "UNKNOWN"

    fields: dict[str, object] = {
        "gap_count": count,
        "gap_reason": details.get("gap_reason"),
        "gap_detected_at": _utc(details.get("gap_detected_at"), "gap_detected_at", optional=True),
        "gap_watermark_received_at": _utc(
            details.get("gap_watermark_received_at"), "gap_watermark_received_at", optional=True
        ),
        "gap_watermark_event_timestamp": _utc(
            details.get("gap_watermark_event_timestamp"),
            "gap_watermark_event_timestamp",
            optional=True,
        ),
    }
    checked_at = _utc(health_row.get("checked_at"), "health.checked_at", optional=True)
    if checked_at is None:
        if gap_status != "KNOWN_GAP":
            gap_status = "UNKNOWN"
    else:
        fields["health_checked_at"] = checked_at
    for name in ("runtime_state", "phase4_status", "data_quality"):
        value = details.get(name)
        fields[f"health_{name}"] = value if isinstance(value, str) else None
    health_status = health_row.get("status")
    fields["health_status"] = getattr(health_status, "value", health_status)
    return gap_status, count, fields


def _availability(row_status: object) -> tuple[PolicyDataStatusV1, str]:
    status = getattr(row_status, "value", row_status)
    mapping = {
        "AVAILABLE": (PolicyDataStatusV1.AVAILABLE, "AVAILABLE"),
        "STALE": (PolicyDataStatusV1.STALE, "DEGRADED"),
        "NOT_AVAILABLE": (PolicyDataStatusV1.NOT_AVAILABLE, "NOT_AVAILABLE"),
        "ERROR": (PolicyDataStatusV1.ERROR, "ERROR"),
    }
    if status not in mapping:
        raise ValueError("liquidation row has an unsupported Phase 4 status")
    return mapping[status]


def project_liquidation(
    window_row: Mapping[str, object],
    health_row: Mapping[str, object] | None,
    *,
    as_of: datetime,
) -> SourceProjectionV1:
    """Project one Phase 4 window without inferring missing events or coverage.

    The caller supplies ``evaluation_id`` in the row mapping as projection
    context; source values otherwise retain the Phase 4 persistence field names.
    """
    if not isinstance(window_row, Mapping):
        raise TypeError("window_row must be a mapping")
    as_of = _utc(as_of, "as_of")
    evaluation_id = window_row.get("evaluation_id")
    if not isinstance(evaluation_id, UUID) or evaluation_id.int == 0:
        raise ValueError("window_row must carry a non-nil evaluation_id")

    exchange = window_row.get("exchange")
    symbol = window_row.get("canonical_symbol")
    market = window_row.get("market")
    timeframe = window_row.get("timeframe")
    if not all(isinstance(value, str) and value.strip() for value in (exchange, symbol, market, timeframe)):
        raise ValueError("liquidation row is missing source, symbol, market, or timeframe")

    window_open = _utc(window_row.get("window_open"), "window_open")
    window_close = _utc(window_row.get("window_close"), "window_close")
    processed_at = _utc(window_row.get("processed_at"), "processed_at", optional=True)
    if window_close <= window_open:
        raise ValueError("window_close must follow window_open")
    if window_close > as_of or (processed_at is not None and processed_at > as_of):
        raise ValueError("liquidation projection cannot include data later than as_of")

    event_count = _nonnegative_int(window_row.get("event_count"), "event_count", optional=True)
    long_count = _nonnegative_int(
        window_row.get("liquidated_long_count"), "liquidated_long_count", optional=True
    )
    short_count = _nonnegative_int(
        window_row.get("liquidated_short_count"), "liquidated_short_count", optional=True
    )
    source_exchange_count = _nonnegative_int(
        window_row.get("source_exchange_count"), "source_exchange_count", optional=True
    )
    if event_count is not None and long_count is not None and short_count is not None:
        if long_count + short_count > event_count:
            raise ValueError("liquidation side counts cannot exceed event_count")

    availability_status, transport_status = _availability(window_row.get("status"))
    gap_status, gap_count, gap_fields = _health_gap(health_row)
    if any(
        isinstance(value, datetime) and value > as_of
        for key, value in gap_fields.items()
        if key.endswith("_at") or key.endswith("_timestamp")
    ):
        raise ValueError("Phase 4 health evidence cannot be later than as_of")
    semantics = getattr(window_row.get("coverage_semantics"), "value", window_row.get("coverage_semantics"))
    freshness_status = (
        EvidenceFreshnessV1.FRESH if window_close == as_of else EvidenceFreshnessV1.UNKNOWN
    )
    expected_window_seconds = _EXPECTED_WINDOW_SECONDS.get(timeframe)
    expected_interval = (
        expected_window_seconds is not None
        and (window_close - window_open).total_seconds() == expected_window_seconds
    )
    if semantics == "PARTIAL_AGGREGATED":
        coverage_status = PolicyCoverageStatusV1.PARTIAL
    elif gap_fields.get("health_phase4_status") == "PARTIAL" or gap_fields.get("health_data_quality") == "PARTIAL":
        coverage_status = PolicyCoverageStatusV1.PARTIAL
    elif semantics == "EXCHANGE_DECLARED_ALL_LIQUIDATIONS":
        if gap_status == "KNOWN_GAP":
            coverage_status = PolicyCoverageStatusV1.PARTIAL
        elif gap_status != "NO_KNOWN_GAP":
            coverage_status = PolicyCoverageStatusV1.UNKNOWN
        elif (
            availability_status is not PolicyDataStatusV1.AVAILABLE
            or event_count is None
            or freshness_status is not EvidenceFreshnessV1.FRESH
            or not expected_interval
            or source_exchange_count in {None, 0}
            or processed_at is None
        ):
            coverage_status = PolicyCoverageStatusV1.UNKNOWN
        else:
            coverage_status = PolicyCoverageStatusV1.SOURCE_DECLARED_COMPLETE
    elif semantics == "NOT_AVAILABLE":
        coverage_status = PolicyCoverageStatusV1.NOT_AVAILABLE
    else:
        coverage_status = PolicyCoverageStatusV1.UNKNOWN

    if gap_status == "KNOWN_GAP":
        quality_status = EvidenceQualityV1.PARTIAL
    elif availability_status in {PolicyDataStatusV1.NOT_AVAILABLE, PolicyDataStatusV1.ERROR}:
        quality_status = EvidenceQualityV1.UNKNOWN
    elif coverage_status is PolicyCoverageStatusV1.PARTIAL:
        quality_status = EvidenceQualityV1.PARTIAL
    elif coverage_status is PolicyCoverageStatusV1.SOURCE_DECLARED_COMPLETE:
        quality_status = (
            EvidenceQualityV1.VALID
            if long_count is not None and short_count is not None
            else EvidenceQualityV1.PARTIAL
        )
    else:
        quality_status = EvidenceQualityV1.UNKNOWN

    source_ref = f"phase4:liquidation:{exchange}:{symbol}:{timeframe}:{window_open.isoformat()}"
    payload: dict[str, object] = {
        "schema": _SOURCE_CONTRACT,
        "transport_status": transport_status,
        "availability_status": availability_status.value,
        "coverage_status": coverage_status.value,
        "gap_status": gap_status,
        "gap_count": gap_count,
        "window_start": window_open,
        "window_end": window_close,
        "observed_at": window_close,
        "source": exchange,
        "provider": exchange,
        "aggregation_semantics": semantics if isinstance(semantics, str) else None,
        "event_count": event_count,
        "liquidated_long_count": long_count,
        "liquidated_short_count": short_count,
        "observed_notional_usd": window_row.get("convertible_notional_usd"),
        "largest_source_notional_usd": window_row.get("largest_source_notional_usd"),
        "source_exchange_count": source_exchange_count,
        "source_granularity": getattr(
            window_row.get("source_granularity"), "value", window_row.get("source_granularity")
        ),
        "reason": window_row.get("reason"),
        "gap_reason": gap_fields.pop("gap_reason"),
        **gap_fields,
        "source_ref": source_ref,
    }
    digest = canonical_sha256(payload)
    projection_id = uuid5(
        NAMESPACE_URL,
        canonical_json(
            {
                "evaluation_id": evaluation_id,
                "source_ref": source_ref,
                "canonical_digest": digest,
            }
        ),
    )
    return SourceProjectionV1(
        projection_id=projection_id,
        evaluation_id=evaluation_id,
        source_phase=SourcePhaseV1.PHASE4,
        source_type=_SOURCE_TYPE,
        source_ref=source_ref,
        symbol=window_row.get("core_symbol", symbol),
        market=market,
        event_time=window_close,
        observed_at=window_close,
        captured_at=processed_at,
        processed_at=processed_at,
        available_at=processed_at,
        availability_status=availability_status,
        freshness_status=freshness_status,
        quality_status=quality_status,
        coverage_status=coverage_status,
        canonical_payload=payload,
        source_schema_version=_SOURCE_CONTRACT,
        projection_version=_SOURCE_CONTRACT,
        canonical_digest=digest,
    )
