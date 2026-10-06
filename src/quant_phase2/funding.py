"""Funding interval normalization and configurable classifications."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .contracts import DataStatus, FundingClassification


@dataclass(frozen=True, slots=True)
class FundingNormalization:
    value: Decimal | None
    method: str | None
    status: DataStatus
    classification: FundingClassification


def normalize_funding_to_8h(rate: Decimal | None, interval_seconds: int | None) -> FundingNormalization:
    if rate is None or interval_seconds is None or interval_seconds <= 0:
        return FundingNormalization(None, "interval_or_rate_missing", DataStatus.NOT_AVAILABLE, FundingClassification.NOT_COMPARABLE)
    value = rate * Decimal(28800) / Decimal(interval_seconds)
    return FundingNormalization(value, "linear_period_to_8h_equivalent", DataStatus.AVAILABLE, FundingClassification.NORMAL)


def classify_funding(
    value: Decimal | None,
    *,
    extreme_positive: Decimal = Decimal("0.001"),
    long_crowded: Decimal = Decimal("0.0003"),
    short_crowded: Decimal = Decimal("-0.0003"),
    extreme_negative: Decimal = Decimal("-0.001"),
) -> FundingClassification:
    if value is None:
        return FundingClassification.NOT_COMPARABLE
    if value >= extreme_positive:
        return FundingClassification.EXTREME_POSITIVE
    if value <= extreme_negative:
        return FundingClassification.EXTREME_NEGATIVE
    if value >= long_crowded:
        return FundingClassification.LONG_CROWDED
    if value <= short_crowded:
        return FundingClassification.SHORT_CROWDED
    return FundingClassification.NORMAL

