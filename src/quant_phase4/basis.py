"""Central basis calculation with strict Phase 4 time and price contracts."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from .contracts import BasisObservation, BasisType, DataStatus, ReasonCode


def compute_basis(
    perpetual_price: Decimal,
    reference_price: Decimal,
    *,
    basis_type: BasisType,
    perpetual_timestamp: datetime,
    reference_timestamp: datetime,
    max_timestamp_skew: timedelta,
    source: str,
    received_at: datetime,
    fetched_at: datetime,
) -> BasisObservation:
    """Compute one basis observation, or a typed stale result for excessive skew."""
    perpetual = _positive_finite(perpetual_price, "perpetual_price")
    reference = _positive_finite(reference_price, "reference_price")
    _require_utc(perpetual_timestamp, "perpetual_timestamp")
    _require_utc(reference_timestamp, "reference_timestamp")
    _require_utc(received_at, "received_at")
    _require_utc(fetched_at, "fetched_at")
    if max_timestamp_skew < timedelta(0):
        raise ValueError("max_timestamp_skew cannot be negative")
    if not isinstance(basis_type, BasisType):
        raise ValueError("basis_type must be explicit")
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be explicit")

    timestamp_skew = abs(perpetual_timestamp - reference_timestamp)
    common = dict(
        exchange="unknown",
        exchange_symbol="unknown",
        canonical_symbol="unknown",
        basis_type=basis_type,
        exchange_timestamp=perpetual_timestamp,
        fetched_at=fetched_at,
        received_at=received_at,
        processed_at=received_at,
        max_timestamp_skew=max_timestamp_skew,
        timestamp_skew=timestamp_skew,
        source_endpoint=source,
        raw_reference=None,
        raw_payload=None,
    )
    if timestamp_skew > max_timestamp_skew:
        return BasisObservation(
            perpetual_price=None, reference_price=None, absolute_basis=None,
            basis_bps=None, basis_pct=None, status=DataStatus.STALE,
            reason_code=ReasonCode.UNCONFIRMED_SEMANTICS, **common,
        )

    absolute_basis = perpetual - reference
    return BasisObservation(
        perpetual_price=perpetual,
        reference_price=reference,
        absolute_basis=absolute_basis,
        basis_bps=absolute_basis / reference * Decimal("10000"),
        basis_pct=absolute_basis / reference * Decimal("100"),
        status=DataStatus.AVAILABLE,
        **common,
    )


def _positive_finite(value: Decimal, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be Decimal-compatible") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{field} must be positive and finite")
    return parsed


def _require_utc(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC-aware")
