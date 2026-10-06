"""Semantic gates for cross-exchange long/short context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .contracts import DataStatus, LongShortMetricType, LongShortObservation, LongShortPopulationSemantics


_PERIOD_EQUIVALENTS = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1d"}


def _comparison_period(period: str) -> str:
    return _PERIOD_EQUIVALENTS.get(period, period)


@dataclass(frozen=True)
class ComparableLongShortGroup:
    metric_type: LongShortMetricType | None
    population_semantics: LongShortPopulationSemantics | None
    period: str | None
    canonical_symbol: str | None
    comparable_source_count: int
    missing_sources: tuple[str, ...]
    stale_sources: tuple[str, ...]
    status: DataStatus
    reason: str | None
    observations: tuple[LongShortObservation, ...]


def validate_long_short_comparability(rows: Sequence[LongShortObservation]) -> ComparableLongShortGroup:
    """Return a group only for matching available account-holder observations.

    This function intentionally does not average ratios or select a winning
    source. It only identifies observations that may be compared downstream.
    """
    observations = tuple(rows)
    missing_sources = tuple(sorted({row.exchange for row in observations if row.status is DataStatus.NOT_AVAILABLE}))
    stale_sources = tuple(sorted({row.exchange for row in observations if row.status is DataStatus.STALE}))
    available = tuple(row for row in observations if row.status is DataStatus.AVAILABLE)
    if not available:
        return ComparableLongShortGroup(
            metric_type=None, population_semantics=None, period=None, canonical_symbol=None,
            comparable_source_count=0, missing_sources=missing_sources, stale_sources=stale_sources,
            status=DataStatus.NOT_AVAILABLE, reason="INSUFFICIENT_COMPARABLE_SOURCES", observations=(),
        )

    reference = available[0]
    compatible = tuple(
        row for row in available
        if row.metric_type is reference.metric_type
        and row.population_semantics is reference.population_semantics
        and _comparison_period(row.period) == _comparison_period(reference.period)
        and row.canonical_symbol == reference.canonical_symbol
        and row.exchange_timestamp == reference.exchange_timestamp
    )
    unique_sources: dict[str, LongShortObservation] = {}
    for row in compatible:
        unique_sources.setdefault(row.exchange, row)
    comparable = tuple(unique_sources.values())
    status = DataStatus.AVAILABLE if len(comparable) >= 2 else DataStatus.NOT_AVAILABLE
    return ComparableLongShortGroup(
        metric_type=reference.metric_type,
        population_semantics=reference.population_semantics,
        period=_comparison_period(reference.period),
        canonical_symbol=reference.canonical_symbol,
        comparable_source_count=len(comparable),
        missing_sources=missing_sources,
        stale_sources=stale_sources,
        status=status,
        reason=None if status is DataStatus.AVAILABLE else "INSUFFICIENT_COMPARABLE_SOURCES",
        observations=comparable,
    )
