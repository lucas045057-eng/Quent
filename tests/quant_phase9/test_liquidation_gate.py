from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from quant_phase9.canonical import canonical_sha256
from quant_phase9.contracts import (
    EvidenceFreshnessV1,
    EvidenceQualityV1,
    PolicyCoverageStatusV1,
    PolicyDataStatusV1,
    SourcePhaseV1,
)
from quant_phase9.liquidation import project_liquidation


AS_OF = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
EVALUATION_ID = UUID("9ac2113d-9484-53bd-9622-02c536a7bc61")


def _window(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "evaluation_id": EVALUATION_ID,
        "exchange": "bitget",
        "canonical_symbol": "BTC-USDT-PERP",
        "market": "USDT_PERPETUAL",
        "timeframe": "1m",
        "window_open": AS_OF - timedelta(minutes=1),
        "window_close": AS_OF,
        "event_count": 2,
        "liquidated_long_count": 1,
        "liquidated_short_count": 1,
        "convertible_notional_usd": Decimal("1250.50"),
        "largest_source_notional_usd": Decimal("800.25"),
        "source_exchange_count": 1,
        "source_granularity": "AGGREGATED_MAX_PER_SECOND",
        "coverage_semantics": "PARTIAL_AGGREGATED",
        "status": "AVAILABLE",
        "reason": None,
        "processed_at": AS_OF,
    }
    row.update(overrides)
    return row


def _health(*, gap: bool = False, gap_count: int = 0) -> dict[str, object]:
    details: dict[str, object] = {
        "runtime_state": "DEGRADED" if gap else "RUNNING",
        "phase4_status": "PARTIAL" if gap else "AVAILABLE",
        "data_quality": "PARTIAL" if gap else "VALID",
        "gap_detected": gap,
        "gap_count": gap_count,
        "gap_reason": "STREAM_GAP" if gap else None,
        "gap_detected_at": (AS_OF - timedelta(minutes=2)).isoformat() if gap else None,
        "gap_watermark_received_at": (AS_OF - timedelta(minutes=1)).isoformat() if gap else None,
        "gap_watermark_event_timestamp": (AS_OF - timedelta(minutes=3)).isoformat() if gap else None,
    }
    return {
        "component": "phase4-liquidation",
        "status": "NOT_AVAILABLE" if gap else "AVAILABLE",
        "checked_at": AS_OF,
        "details": details,
    }


def test_partial_aggregated_remains_partial_even_when_transport_and_row_are_available():
    projection = project_liquidation(_window(), _health(), as_of=AS_OF)

    assert projection.source_phase is SourcePhaseV1.PHASE4
    assert projection.availability_status is PolicyDataStatusV1.AVAILABLE
    assert projection.coverage_status is PolicyCoverageStatusV1.PARTIAL
    assert projection.quality_status is EvidenceQualityV1.PARTIAL
    assert projection.canonical_payload["transport_status"] == "AVAILABLE"
    assert projection.canonical_payload["gap_status"] == "NO_KNOWN_GAP"
    assert projection.canonical_payload["aggregation_semantics"] == "PARTIAL_AGGREGATED"


def test_missing_authoritative_gap_health_is_unknown_not_complete():
    row = _window(coverage_semantics="EXCHANGE_DECLARED_ALL_LIQUIDATIONS")
    projection = project_liquidation(row, None, as_of=AS_OF)

    assert projection.canonical_payload["gap_status"] == "UNKNOWN"
    assert projection.coverage_status is PolicyCoverageStatusV1.UNKNOWN
    assert projection.freshness_status is EvidenceFreshnessV1.FRESH


def test_absent_event_observation_is_not_normalized_to_zero():
    row = _window(
        event_count=None,
        liquidated_long_count=None,
        liquidated_short_count=None,
        convertible_notional_usd=None,
        status="NOT_AVAILABLE",
        reason="EMPTY_RESPONSE",
    )
    projection = project_liquidation(row, _health(), as_of=AS_OF)

    assert projection.availability_status is PolicyDataStatusV1.NOT_AVAILABLE
    assert projection.canonical_payload["event_count"] is None
    assert projection.canonical_payload["liquidated_long_count"] is None
    assert projection.canonical_payload["liquidated_short_count"] is None
    assert projection.canonical_payload["reason"] == "EMPTY_RESPONSE"


def test_source_error_is_not_misclassified_as_invalid_canonical_data():
    row = _window(status="ERROR", event_count=None, reason="SOURCE_UNAVAILABLE")
    projection = project_liquidation(row, _health(), as_of=AS_OF)

    assert projection.availability_status is PolicyDataStatusV1.ERROR
    assert projection.quality_status is EvidenceQualityV1.UNKNOWN


def test_explicit_complete_zero_interval_is_preserved_without_inventing_event_count():
    row = _window(
        event_count=0,
        liquidated_long_count=0,
        liquidated_short_count=0,
        convertible_notional_usd=Decimal("0"),
        largest_source_notional_usd=None,
        coverage_semantics="EXCHANGE_DECLARED_ALL_LIQUIDATIONS",
    )
    projection = project_liquidation(row, _health(), as_of=AS_OF)

    assert projection.coverage_status is PolicyCoverageStatusV1.SOURCE_DECLARED_COMPLETE
    assert projection.canonical_payload["event_count"] == 0
    assert projection.canonical_payload["observed_notional_usd"] == Decimal("0")
    assert projection.canonical_payload["gap_status"] == "NO_KNOWN_GAP"


def test_complete_coverage_is_not_asserted_for_interval_not_fresh_at_as_of():
    row = _window(
        window_open=AS_OF - timedelta(minutes=2),
        window_close=AS_OF - timedelta(minutes=1),
        event_count=0,
        liquidated_long_count=0,
        liquidated_short_count=0,
        coverage_semantics="EXCHANGE_DECLARED_ALL_LIQUIDATIONS",
    )
    projection = project_liquidation(row, _health(), as_of=AS_OF)

    assert projection.freshness_status is EvidenceFreshnessV1.UNKNOWN
    assert projection.coverage_status is PolicyCoverageStatusV1.UNKNOWN


def test_complete_coverage_requires_the_declared_timeframe_window():
    row = _window(
        window_open=AS_OF - timedelta(seconds=30),
        event_count=0,
        liquidated_long_count=0,
        liquidated_short_count=0,
        coverage_semantics="EXCHANGE_DECLARED_ALL_LIQUIDATIONS",
    )

    projection = project_liquidation(row, _health(), as_of=AS_OF)

    assert projection.freshness_status is EvidenceFreshnessV1.FRESH
    assert projection.coverage_status is PolicyCoverageStatusV1.UNKNOWN


def test_projection_rejects_future_window_data():
    row = _window(window_close=AS_OF + timedelta(seconds=1))

    with pytest.raises(ValueError, match="later than as_of"):
        project_liquidation(row, _health(), as_of=AS_OF)


def test_projection_rejects_future_gap_health_evidence():
    health = _health()
    health["checked_at"] = AS_OF + timedelta(seconds=1)

    with pytest.raises(ValueError, match="health evidence cannot be later than as_of"):
        project_liquidation(_window(), health, as_of=AS_OF)


def test_phase4_partial_health_cannot_be_promoted_by_complete_source_label():
    row = _window(coverage_semantics="EXCHANGE_DECLARED_ALL_LIQUIDATIONS")
    health = _health()
    health["details"] = {
        **health["details"],
        "phase4_status": "PARTIAL",
        "data_quality": "PARTIAL",
    }

    projection = project_liquidation(row, health, as_of=AS_OF)

    assert projection.coverage_status is PolicyCoverageStatusV1.PARTIAL
    assert projection.quality_status is EvidenceQualityV1.PARTIAL


def test_known_gap_is_not_erased_when_health_check_time_is_missing():
    health = _health(gap=True, gap_count=1)
    health["checked_at"] = None

    projection = project_liquidation(_window(), health, as_of=AS_OF)

    assert projection.canonical_payload["gap_status"] == "KNOWN_GAP"
    assert projection.coverage_status is PolicyCoverageStatusV1.PARTIAL


def test_known_gap_is_retained_as_partial_with_historical_watermarks():
    projection = project_liquidation(_window(), _health(gap=True, gap_count=2), as_of=AS_OF)
    payload = projection.canonical_payload

    assert projection.coverage_status is PolicyCoverageStatusV1.PARTIAL
    assert projection.quality_status is EvidenceQualityV1.PARTIAL
    assert payload["gap_status"] == "KNOWN_GAP"
    assert payload["gap_count"] == 2
    assert payload["gap_reason"] == "STREAM_GAP"
    assert payload["health_runtime_state"] == "DEGRADED"
    assert payload["health_phase4_status"] == "PARTIAL"
    assert payload["health_data_quality"] == "PARTIAL"
    assert payload["gap_detected_at"] == AS_OF - timedelta(minutes=2)
    assert payload["gap_watermark_received_at"] == AS_OF - timedelta(minutes=1)
    assert payload["gap_watermark_event_timestamp"] == AS_OF - timedelta(minutes=3)


def test_projection_digest_is_canonical_and_does_not_embed_raw_provider_payload():
    row = _window(raw_payload={"sensitive": "must not copy"})
    projection = project_liquidation(row, _health(), as_of=AS_OF)

    assert projection.canonical_digest == canonical_sha256(projection.canonical_payload)
    assert "raw_payload" not in projection.canonical_payload
    assert "sensitive" not in repr(projection.canonical_payload)
