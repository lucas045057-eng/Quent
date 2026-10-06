"""Coverage-aware, non-directional Phase 4 context construction."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from .contracts import (
    BasisType,
    CoverageSemantics,
    DataStatus,
    LongShortObservation,
    BasisObservation,
    CanonicalLiquidation,
    SourceGranularity,
)


class _Missing(StrEnum):
    INSUFFICIENT_COMPARABLE_SOURCES = "INSUFFICIENT_COMPARABLE_SOURCES"
    INCOMPATIBLE_COVERAGE = "INCOMPATIBLE_COVERAGE"
    INCOMPATIBLE_BASIS_TYPES = "INCOMPATIBLE_BASIS_TYPES"
    NO_DATA = "NO_DATA"


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be UTC-aware")
    return value.astimezone(timezone.utc)


def _period(period: str) -> str:
    return {"5m": "5min", "15m": "15min", "30m": "30min"}.get(period, period)


@dataclass(frozen=True, slots=True)
class Phase4MetricContext:
    """Metadata and source rows; numeric cross-exchange voting is intentionally absent."""

    status: DataStatus
    observations: tuple[Any, ...]
    source_count: int
    comparable_count: int
    missing_count: int
    stale_count: int
    error_count: int
    source_granularity: tuple[SourceGranularity, ...] = ()
    coverage_semantics: tuple[CoverageSemantics, ...] = ()
    reason_codes: tuple[str, ...] = ()
    reason: str | None = None
    observed_total: None = None
    groups: Mapping[Any, tuple[Any, ...]] | None = None


@dataclass(frozen=True, slots=True)
class CrossExchangePhase4Context:
    canonical_symbol: str | None
    liquidation: Phase4MetricContext
    long_short: Phase4MetricContext
    basis: Phase4MetricContext
    processed_at: datetime

    @property
    def statuses(self) -> tuple[DataStatus, ...]:
        return self.liquidation.status, self.long_short.status, self.basis.status

    def with_metric_status(self, metric: str, status: DataStatus) -> "CrossExchangePhase4Context":
        """Small immutable test/integration hook for a bounded read status."""
        if metric not in {"liquidation", "long_short", "basis"}:
            raise ValueError("unknown Phase 4 metric")
        current = getattr(self, metric)
        updated = replace(current, status=status)
        return replace(self, **{metric: updated})


def _counts(rows: Sequence[Any]) -> tuple[int, int, int, int]:
    return (
        sum(row.status is DataStatus.NOT_AVAILABLE for row in rows),
        sum(row.status is DataStatus.STALE for row in rows),
        sum(row.status is DataStatus.ERROR for row in rows),
        len({row.exchange for row in rows}),
    )


def _base_context(rows: Sequence[Any], *, reason: str | None = None, reasons: Iterable[str] = ()) -> Phase4MetricContext:
    missing, stale, errors, sources = _counts(rows)
    available = [row for row in rows if row.status is DataStatus.AVAILABLE]
    if not rows or not available:
        if stale:
            status = DataStatus.STALE
        elif errors:
            status = DataStatus.ERROR
        else:
            status = DataStatus.NOT_AVAILABLE
    elif missing or stale or errors:
        status = DataStatus.STALE if stale or missing else DataStatus.ERROR
    else:
        status = DataStatus.AVAILABLE
    status_reasons = []
    if missing:
        status_reasons.append("MISSING_SOURCES")
    if stale:
        status_reasons.append("STALE_SOURCES")
    if errors:
        status_reasons.append("ERROR_SOURCES")
    source_reasons = [
        f"{row.exchange}:{row.reason_code.value}"
        for row in rows
        if row.reason_code is not None
    ]
    gap_reasons = [
        str(row.reason) for row in rows
        if row.status is not DataStatus.AVAILABLE and getattr(row, "reason", None)
    ]
    return Phase4MetricContext(
        status=status, observations=tuple(rows), source_count=sources, comparable_count=0,
        missing_count=missing, stale_count=stale, error_count=errors,
        reason_codes=tuple(dict.fromkeys((*status_reasons, *source_reasons, *gap_reasons, *reasons))), reason=reason,
    )


def _liquidation_context(rows: Sequence[CanonicalLiquidation]) -> Phase4MetricContext:
    result = _base_context(rows)
    available = [row for row in rows if row.status is DataStatus.AVAILABLE]
    groups: dict[tuple[Any, ...], list[CanonicalLiquidation]] = {}
    for row in available:
        key = (
            row.canonical_symbol,
            row.source_granularity,
            row.coverage_semantics,
            row.quantity_unit,
            row.event_timestamp,
        )
        groups.setdefault(key, []).append(row)
    source_groups: dict[tuple[Any, ...], dict[str, CanonicalLiquidation]] = {}
    for key, grouped in groups.items():
        source_groups.setdefault(key, {})
        for row in grouped:
            source_groups[key].setdefault(row.exchange, row)
    comparable = max((len(sources) for sources in source_groups.values() if len(sources) >= 2), default=0)
    reasons: list[str] = [str(row.reason) for row in rows if row.status is not DataStatus.AVAILABLE and getattr(row, "reason", None)]
    if len({row.coverage_semantics for row in available}) > 1:
        reasons.append(_Missing.INCOMPATIBLE_COVERAGE)
    if available and comparable < 2:
        reasons.append(_Missing.INSUFFICIENT_COMPARABLE_SOURCES)
    return replace(
        result,
        comparable_count=comparable if not reasons or _Missing.INCOMPATIBLE_COVERAGE not in reasons else 0,
        source_granularity=tuple(sorted({row.source_granularity for row in rows}, key=lambda value: value.value)),
        coverage_semantics=tuple(sorted({row.coverage_semantics for row in rows}, key=lambda value: value.value)),
        reason_codes=tuple(dict.fromkeys((*result.reason_codes, *reasons))),
        reason=(reasons[0] if reasons else None),
    )


def _long_short_context(rows: Sequence[LongShortObservation]) -> Phase4MetricContext:
    result = _base_context(rows)
    available = [row for row in rows if row.status is DataStatus.AVAILABLE]
    groups: dict[tuple[Any, ...], dict[str, LongShortObservation]] = {}
    for row in available:
        key = (row.canonical_symbol, row.metric_type, row.population_semantics, _period(row.period), row.exchange_timestamp)
        groups.setdefault(key, {}).setdefault(row.exchange, row)
    comparable = max((len(group) for group in groups.values()), default=0) if len(groups) == 1 else 0
    reasons = [] if comparable >= 2 else [_Missing.INSUFFICIENT_COMPARABLE_SOURCES]
    return replace(result, comparable_count=comparable, reason_codes=tuple(dict.fromkeys((*result.reason_codes, *reasons))),
                   reason=(reasons[0] if reasons else None))


def _basis_context(rows: Sequence[BasisObservation]) -> Phase4MetricContext:
    result = _base_context(rows)
    available = [row for row in rows if row.status is DataStatus.AVAILABLE]
    groups: dict[BasisType, list[BasisObservation]] = {}
    for row in available:
        groups.setdefault(row.basis_type, []).append(row)
    comparable_groups: dict[BasisType, tuple[BasisObservation, ...]] = {}
    for basis_type, candidates in groups.items():
        by_key: dict[tuple[Any, ...], dict[str, BasisObservation]] = {}
        for row in candidates:
            key = (row.canonical_symbol, row.exchange_timestamp)
            by_key.setdefault(key, {}).setdefault(row.exchange, row)
        best = max(by_key.values(), key=len, default={})
        if len(best) >= 2:
            comparable_groups[basis_type] = tuple(best.values())
    reasons: list[str] = []
    if len(groups) > 1 and not comparable_groups:
        reasons.append(_Missing.INCOMPATIBLE_BASIS_TYPES)
    if not comparable_groups and available and not reasons:
        reasons.append(_Missing.INSUFFICIENT_COMPARABLE_SOURCES)
    return replace(result, comparable_count=max((len(group) for group in comparable_groups.values()), default=0),
                   groups={kind: tuple(rows) for kind, rows in groups.items()},
                   reason_codes=tuple(dict.fromkeys((*result.reason_codes, *reasons))), reason=(reasons[0] if reasons else None))


def build_phase4_context(
    liquidations: Iterable[CanonicalLiquidation],
    long_short_rows: Iterable[LongShortObservation],
    basis_rows: Iterable[BasisObservation],
    processed_at: datetime,
) -> CrossExchangePhase4Context:
    processed_at = _utc(processed_at, "processed_at")
    liquidation_rows = tuple(liquidations)
    long_short = tuple(long_short_rows)
    basis = tuple(basis_rows)
    all_rows = (*liquidation_rows, *long_short, *basis)
    symbols = {row.canonical_symbol for row in all_rows if getattr(row, "canonical_symbol", None)}
    return CrossExchangePhase4Context(
        canonical_symbol=next(iter(symbols)) if len(symbols) == 1 else None,
        liquidation=_liquidation_context(liquidation_rows),
        long_short=_long_short_context(long_short),
        basis=_basis_context(basis),
        processed_at=processed_at,
    )
