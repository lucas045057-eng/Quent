"""Sparse cross-exchange aggregation with explicit source exclusions."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Iterable

from .contracts import CrossExchangeSnapshot, DataStatus, FundingObservation, OIObservation


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def build_cross_exchange_snapshot(
    canonical_symbol: str,
    oi_observations: Iterable[OIObservation],
    funding_observations: Iterable[FundingObservation],
    timestamp: datetime,
    *,
    min_oi_sources: int = 2,
    min_funding_sources: int = 2,
    funding_divergence_threshold: Decimal = Decimal("0.002"),
) -> CrossExchangeSnapshot:
    oi_rows = tuple(oi_observations)
    funding_rows = tuple(funding_observations)
    available_oi = {
        row.exchange: row for row in oi_rows
        if row.canonical_symbol == canonical_symbol and row.status is DataStatus.AVAILABLE and row.open_interest_usd is not None
    }
    available_funding = {
        row.exchange: row for row in funding_rows
        if row.canonical_symbol == canonical_symbol and row.status is DataStatus.AVAILABLE and row.normalized_8h_rate is not None
    }
    oi_total = sum((row.open_interest_usd for row in available_oi.values()), Decimal("0")) if available_oi else None
    rates = [row.normalized_8h_rate for row in available_funding.values()]
    funding_min = min(rates) if rates else None
    funding_max = max(rates) if rates else None
    dispersion = funding_max - funding_min if funding_min is not None and funding_max is not None else None
    weighted_pairs = [
        (available_oi[exchange].open_interest_usd, available_funding[exchange].normalized_8h_rate)
        for exchange in available_oi.keys() & available_funding.keys()
    ]
    total_weight = sum((weight for weight, _ in weighted_pairs), Decimal("0"))
    sufficient_sources = len(available_oi) >= min_oi_sources and len(available_funding) >= min_funding_sources
    weighted = (
        sum((weight * rate for weight, rate in weighted_pairs), Decimal("0")) / total_weight
        if sufficient_sources and total_weight > 0 else None
    )
    reason: str | None = None
    status = DataStatus.AVAILABLE
    if len(available_oi) < min_oi_sources or len(available_funding) < min_funding_sources:
        status = DataStatus.NOT_AVAILABLE
        reason = "INSUFFICIENT_CROSS_EXCHANGE_DATA"
    elif dispersion is not None and dispersion >= funding_divergence_threshold:
        reason = "SOURCE_DIVERGENCE"
    return CrossExchangeSnapshot(
        canonical_symbol=canonical_symbol, timestamp=timestamp,
        exchanges=tuple(oi_rows) + tuple(funding_rows), oi_exchange_count=len(available_oi),
        funding_exchange_count=len(available_funding), oi_total_usd=oi_total,
        oi_weighted_funding=weighted, median_funding=_median(rates), max_funding=funding_max,
        min_funding=funding_min, funding_dispersion=dispersion, status=status, reason=reason,
    )
