"""Canonical, exchange-neutral Phase 5 context contracts.

Exchange payloads never cross this boundary.  Phase 5 derived status is kept
separate from Phase 1-4 source status, and missing values are represented by
``None`` rather than a fabricated zero.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
import json
from typing import Any, Mapping

from quant_phase1.time import ensure_utc


class ContextStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    ERROR = "ERROR"


class MappingStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    ERROR = "ERROR"


class DirectionState(StrEnum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    MIXED = "MIXED"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class VolatilityState(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    EXTREME = "EXTREME"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class VolumeState(StrEnum):
    BELOW_BASELINE = "BELOW_BASELINE"
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class StructureState(StrEnum):
    HIGHER_HIGH_HIGHER_LOW = "HIGHER_HIGH_HIGHER_LOW"
    LOWER_HIGH_LOWER_LOW = "LOWER_HIGH_LOWER_LOW"
    RANGE = "RANGE"
    MIXED = "MIXED"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class BreadthState(StrEnum):
    BROAD_STRENGTH = "BROAD_STRENGTH"
    NARROW_STRENGTH = "NARROW_STRENGTH"
    BROAD_WEAKNESS = "BROAD_WEAKNESS"
    NARROW_WEAKNESS = "NARROW_WEAKNESS"
    MIXED = "MIXED"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class RelativeStrengthClass(StrEnum):
    STRONG = "STRONG"
    WEAK = "WEAK"
    NEUTRAL = "NEUTRAL"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class SectorRelation(StrEnum):
    OUTPERFORMING = "OUTPERFORMING"
    UNDERPERFORMING = "UNDERPERFORMING"
    IN_LINE = "IN_LINE"
    NOT_AVAILABLE = "NOT_AVAILABLE"


class ReasonCode(StrEnum):
    MISSING_INPUT = "MISSING_INPUT"
    STALE_INPUT = "STALE_INPUT"
    INSUFFICIENT_COVERAGE = "INSUFFICIENT_COVERAGE"
    TIMESTAMP_SKEW = "TIMESTAMP_SKEW"
    NO_ELIGIBLE_MEMBERS = "NO_ELIGIBLE_MEMBERS"
    UNKNOWN_SECTOR = "UNKNOWN_SECTOR"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    CALCULATION_ERROR = "CALCULATION_ERROR"
    PERSISTENCE_ERROR = "PERSISTENCE_ERROR"


MAX_EVIDENCE_BYTES = 65_536


def _utc(value: datetime | None, field_name: str) -> datetime | None:
    if value is None:
        return None
    try:
        return ensure_utc(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be UTC") from exc


def _decimal(value: Decimal | int | float | str | None, field_name: str) -> Decimal | None:
    if value is None:
        return None
    result = value if isinstance(value, Decimal) else Decimal(str(value))
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return result


def _nonnegative(value: Decimal | int | float | str | None, field_name: str) -> Decimal | None:
    result = _decimal(value, field_name)
    if result is not None and result < 0:
        raise ValueError(f"{field_name} cannot be negative")
    return result


def _enum(value: Any, enum_type: type[StrEnum], field_name: str) -> StrEnum:
    try:
        return value if isinstance(value, enum_type) else enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field_name}: {value}") from exc


def _evidence(
    values: tuple[str, ...] | list[str] | None, *, max_bytes: int = MAX_EVIDENCE_BYTES
) -> tuple[str, ...]:
    result = tuple(values or ())
    if any(not isinstance(item, str) or not item.strip() for item in result):
        raise ValueError("evidence entries must be non-empty strings")
    if len(result) > 64:
        raise ValueError("evidence is unbounded")
    encoded = json.dumps(result, separators=(",", ":"))
    if max_bytes <= 0 or len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError("evidence exceeds bounded evidence size")
    return result


def _bounded_mapping(
    value: Mapping[str, Any], field_name: str, *, max_bytes: int = MAX_EVIDENCE_BYTES
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} must be a mapping")

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                key_text = str(key).lower()
                if key_text in {"raw_payload", "payload"}:
                    raise ValueError(f"{field_name} cannot contain raw payload")
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    try:
        encoded = json.dumps(value, default=str, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON-compatible") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field_name} exceeds bounded evidence size")
    return value


def validate_bounded_mapping(value: Mapping[str, Any], field_name: str, max_bytes: int) -> Mapping[str, Any]:
    """Validate evidence using the runtime-configured byte ceiling."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    return _bounded_mapping(value, field_name, max_bytes=max_bytes)


def _require_reason(status: ContextStatus, reason_code: ReasonCode | str | None) -> None:
    if status in {ContextStatus.NOT_AVAILABLE, ContextStatus.ERROR} and reason_code is None:
        raise ValueError(f"{status.value} context requires reason_code")


def _require_partial_evidence(
    status: ContextStatus, values: tuple[Any, ...], missing_evidence: tuple[str, ...], *, missing_count: int = 0
) -> None:
    if status is ContextStatus.PARTIAL and not missing_evidence:
        raise ValueError("PARTIAL context with missing values requires missing_evidence")


def _require_unavailable_label(status: ContextStatus, label: StrEnum, unavailable: StrEnum) -> None:
    if status in {ContextStatus.STALE, ContextStatus.NOT_AVAILABLE, ContextStatus.ERROR} and label is not unavailable:
        raise ValueError(f"{status.value} context must use {unavailable.value} label")


def _validate_window(start: datetime | None, end: datetime | None, context: datetime) -> None:
    if (start is None) != (end is None):
        raise ValueError("input window must provide both start and end")
    if start is not None and end is not None and not start <= end <= context:
        raise ValueError("input window must end at or before context_timestamp")


def _reject_unavailable_values(status: ContextStatus, values: tuple[Any, ...]) -> None:
    if status in {ContextStatus.NOT_AVAILABLE, ContextStatus.ERROR} and any(value is not None for value in values):
        raise ValueError(f"{status.value} context cannot carry calculated values")


@dataclass(frozen=True, slots=True)
class MarketLeaderContext:
    symbol: str
    timeframe: str
    context_timestamp: datetime
    input_window_start: datetime | None
    input_window_end: datetime | None
    return_pct: Decimal | None
    trend_state: DirectionState | str
    structure_state: StructureState | str
    volatility_state: VolatilityState | str
    volume_state: VolumeState | str
    volatility_value: Decimal | None
    volume_ratio: Decimal | None
    freshness_status: ContextStatus | str
    data_quality: Mapping[str, Any]
    source_count: int
    missing_count: int
    status: ContextStatus | str
    processed_at: datetime
    reason_code: ReasonCode | str | None = None
    support_evidence: tuple[str, ...] = field(default_factory=tuple)
    conflict_evidence: tuple[str, ...] = field(default_factory=tuple)
    missing_evidence: tuple[str, ...] = field(default_factory=tuple)
    calculation_version: str = "phase5-v1"
    input_reference: Mapping[str, Any] = field(default_factory=dict)
    evidence_max_bytes: InitVar[int] = MAX_EVIDENCE_BYTES

    def __post_init__(self, evidence_max_bytes: int) -> None:
        if not self.symbol.strip() or self.timeframe not in {"5m", "15m", "1H", "4H"}:
            raise ValueError("symbol and supported timeframe are required")
        context = _utc(self.context_timestamp, "context_timestamp")
        processed = _utc(self.processed_at, "processed_at")
        start = _utc(self.input_window_start, "input_window_start")
        end = _utc(self.input_window_end, "input_window_end")
        assert context is not None and processed is not None
        _validate_window(start, end, context)
        if self.source_count < 0 or self.missing_count < 0:
            raise ValueError("source and missing counts cannot be negative")
        status = _enum(self.status, ContextStatus, "status")
        freshness = _enum(self.freshness_status, ContextStatus, "freshness_status")
        if status is ContextStatus.AVAILABLE and freshness is not ContextStatus.AVAILABLE:
            raise ValueError("AVAILABLE context requires AVAILABLE freshness")
        if status in {ContextStatus.STALE, ContextStatus.ERROR} and freshness is not status:
            raise ValueError("stale/error context must preserve freshness status")
        if freshness is ContextStatus.STALE and status is not ContextStatus.STALE:
            raise ValueError("STALE freshness cannot produce a normal context")
        if freshness is ContextStatus.ERROR and status is not ContextStatus.ERROR:
            raise ValueError("ERROR freshness cannot produce a normal context")
        reason = _enum(self.reason_code, ReasonCode, "reason_code") if self.reason_code is not None else None
        trend = _enum(self.trend_state, DirectionState, "trend_state")
        structure = _enum(self.structure_state, StructureState, "structure_state")
        volatility = _enum(self.volatility_state, VolatilityState, "volatility_state")
        volume = _enum(self.volume_state, VolumeState, "volume_state")
        _require_reason(status, reason)
        _require_unavailable_label(status, trend, DirectionState.NOT_AVAILABLE)
        _require_unavailable_label(status, structure, StructureState.NOT_AVAILABLE)
        _require_unavailable_label(status, volatility, VolatilityState.NOT_AVAILABLE)
        _require_unavailable_label(status, volume, VolumeState.NOT_AVAILABLE)
        _reject_unavailable_values(status, (self.return_pct, self.volatility_value, self.volume_ratio))
        if status is ContextStatus.AVAILABLE and any(
            value is None for value in (self.return_pct, self.volatility_value, self.volume_ratio)
        ):
            raise ValueError("AVAILABLE leader context requires calculated values")
        object.__setattr__(self, "context_timestamp", context)
        object.__setattr__(self, "processed_at", processed)
        object.__setattr__(self, "input_window_start", start)
        object.__setattr__(self, "input_window_end", end)
        object.__setattr__(self, "return_pct", _decimal(self.return_pct, "return_pct"))
        object.__setattr__(self, "volatility_value", _nonnegative(self.volatility_value, "volatility_value"))
        object.__setattr__(self, "volume_ratio", _nonnegative(self.volume_ratio, "volume_ratio"))
        object.__setattr__(self, "trend_state", trend)
        object.__setattr__(self, "structure_state", structure)
        object.__setattr__(self, "volatility_state", volatility)
        object.__setattr__(self, "volume_state", volume)
        object.__setattr__(self, "freshness_status", freshness)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "reason_code", reason)
        object.__setattr__(
            self,
            "data_quality",
            _bounded_mapping(self.data_quality, "data_quality", max_bytes=evidence_max_bytes),
        )
        object.__setattr__(
            self,
            "input_reference",
            _bounded_mapping(self.input_reference, "input_reference", max_bytes=evidence_max_bytes),
        )
        object.__setattr__(self, "support_evidence", _evidence(self.support_evidence, max_bytes=evidence_max_bytes))
        object.__setattr__(self, "conflict_evidence", _evidence(self.conflict_evidence, max_bytes=evidence_max_bytes))
        object.__setattr__(self, "missing_evidence", _evidence(self.missing_evidence, max_bytes=evidence_max_bytes))
        _require_partial_evidence(
            status,
            (self.return_pct, self.volatility_value, self.volume_ratio),
            self.missing_evidence,
            missing_count=self.missing_count,
        )


@dataclass(frozen=True, slots=True)
class MarketBreadthSnapshot:
    timeframe: str
    context_timestamp: datetime
    universe_run_id: int | None
    sample_size: int
    available_count: int
    missing_count: int
    coverage_ratio: Decimal | None
    positive_ratio: Decimal | None
    up_structure_ratio: Decimal | None
    down_structure_ratio: Decimal | None
    breadth_state: BreadthState | str
    status: ContextStatus | str
    processed_at: datetime
    missing_evidence: tuple[str, ...] = field(default_factory=tuple)
    input_reference: Mapping[str, Any] = field(default_factory=dict)
    reason_code: ReasonCode | str | None = None
    calculation_version: str = "phase5-v1"
    evidence_max_bytes: InitVar[int] = MAX_EVIDENCE_BYTES

    def __post_init__(self, evidence_max_bytes: int) -> None:
        if self.timeframe not in {"5m", "15m", "1H", "4H"}:
            raise ValueError("unsupported timeframe")
        context = _utc(self.context_timestamp, "context_timestamp")
        processed = _utc(self.processed_at, "processed_at")
        assert context is not None and processed is not None
        if min(self.sample_size, self.available_count, self.missing_count) < 0:
            raise ValueError("breadth counts cannot be negative")
        if self.available_count + self.missing_count != self.sample_size:
            raise ValueError("available_count + missing_count must equal sample_size")
        status = _enum(self.status, ContextStatus, "status")
        reason = _enum(self.reason_code, ReasonCode, "reason_code") if self.reason_code is not None else None
        _require_reason(status, reason)
        breadth_state = _enum(self.breadth_state, BreadthState, "breadth_state")
        _require_unavailable_label(status, breadth_state, BreadthState.NOT_AVAILABLE)
        _reject_unavailable_values(
            status,
            (self.positive_ratio, self.up_structure_ratio, self.down_structure_ratio),
        )
        coverage = _decimal(self.coverage_ratio, "coverage_ratio")
        if self.sample_size == 0:
            if coverage is not None or status is not ContextStatus.NOT_AVAILABLE:
                raise ValueError("empty breadth sample must be NOT_AVAILABLE")
        else:
            expected = Decimal(self.available_count) / Decimal(self.sample_size)
            if coverage is None or not Decimal("0") <= coverage <= Decimal("1") or coverage != expected:
                raise ValueError("coverage_ratio must equal available_count / sample_size")
            if self.missing_count and status is ContextStatus.AVAILABLE:
                raise ValueError("breadth with missing members cannot be AVAILABLE")
            if status is ContextStatus.AVAILABLE and any(
                value is None for value in (self.positive_ratio, self.up_structure_ratio, self.down_structure_ratio)
            ):
                raise ValueError("AVAILABLE breadth requires calculated ratios")
        for name in ("positive_ratio", "up_structure_ratio", "down_structure_ratio"):
            value = _decimal(getattr(self, name), name)
            if value is not None and not Decimal("0") <= value <= Decimal("1"):
                raise ValueError(f"{name} must be between 0 and 1")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "context_timestamp", context)
        object.__setattr__(self, "processed_at", processed)
        object.__setattr__(self, "coverage_ratio", coverage)
        object.__setattr__(self, "breadth_state", breadth_state)
        object.__setattr__(self, "status", status)
        evidence = _evidence(self.missing_evidence, max_bytes=evidence_max_bytes)
        object.__setattr__(self, "missing_evidence", evidence)
        _require_partial_evidence(
            status,
            (self.positive_ratio, self.up_structure_ratio, self.down_structure_ratio),
            evidence,
            missing_count=self.missing_count,
        )
        object.__setattr__(self, "input_reference", _bounded_mapping(self.input_reference, "input_reference", max_bytes=evidence_max_bytes))
        object.__setattr__(self, "reason_code", reason)


@dataclass(frozen=True, slots=True)
class MarketRegimeSnapshot:
    timeframe: str
    context_timestamp: datetime
    universe_run_id: int | None
    direction_regime: DirectionState | str
    volatility_regime: VolatilityState | str
    breadth_regime: BreadthState | str
    direction_status: ContextStatus | str
    volatility_status: ContextStatus | str
    breadth_status: ContextStatus | str
    status: ContextStatus | str
    support_evidence: tuple[str, ...]
    conflict_evidence: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    processed_at: datetime
    sample_size: int = 0
    available_count: int = 0
    missing_count: int = 0
    coverage_ratio: Decimal | None = None
    reason_code: ReasonCode | str | None = None
    calculation_version: str = "phase5-v1"
    input_reference: Mapping[str, Any] = field(default_factory=dict)
    evidence_max_bytes: InitVar[int] = MAX_EVIDENCE_BYTES

    def __post_init__(self, evidence_max_bytes: int) -> None:
        if self.timeframe not in {"5m", "15m", "1H", "4H"}:
            raise ValueError("unsupported timeframe")
        object.__setattr__(self, "context_timestamp", _utc(self.context_timestamp, "context_timestamp"))
        object.__setattr__(self, "processed_at", _utc(self.processed_at, "processed_at"))
        dimension_statuses: list[ContextStatus] = []
        for name, enum_type in (
            ("direction_regime", DirectionState),
            ("volatility_regime", VolatilityState),
            ("breadth_regime", BreadthState),
            ("direction_status", ContextStatus),
            ("volatility_status", ContextStatus),
            ("breadth_status", ContextStatus),
            ("status", ContextStatus),
        ):
            converted = _enum(getattr(self, name), enum_type, name)
            object.__setattr__(self, name, converted)
            if name.endswith("_status"):
                dimension_statuses.append(converted)
        reason = _enum(self.reason_code, ReasonCode, "reason_code") if self.reason_code is not None else None
        _require_reason(self.status, reason)
        if self.universe_run_id is not None and self.universe_run_id <= 0:
            raise ValueError("universe_run_id must be positive")
        if self.direction_status is ContextStatus.NOT_AVAILABLE and self.direction_regime is not DirectionState.NOT_AVAILABLE:
            raise ValueError("missing direction must have NOT_AVAILABLE label")
        if self.volatility_status is ContextStatus.NOT_AVAILABLE and self.volatility_regime is not VolatilityState.NOT_AVAILABLE:
            raise ValueError("missing volatility must have NOT_AVAILABLE label")
        if self.breadth_status is ContextStatus.NOT_AVAILABLE and self.breadth_regime is not BreadthState.NOT_AVAILABLE:
            raise ValueError("missing breadth must have NOT_AVAILABLE label")
        if self.direction_status in {ContextStatus.STALE, ContextStatus.ERROR} and self.direction_regime is not DirectionState.NOT_AVAILABLE:
            raise ValueError("stale/error direction must have NOT_AVAILABLE label")
        if self.volatility_status in {ContextStatus.STALE, ContextStatus.ERROR} and self.volatility_regime is not VolatilityState.NOT_AVAILABLE:
            raise ValueError("stale/error volatility must have NOT_AVAILABLE label")
        if self.breadth_status in {ContextStatus.STALE, ContextStatus.ERROR} and self.breadth_regime is not BreadthState.NOT_AVAILABLE:
            raise ValueError("stale/error breadth must have NOT_AVAILABLE label")
        if self.direction_status is ContextStatus.AVAILABLE and self.direction_regime is DirectionState.NOT_AVAILABLE:
            raise ValueError("AVAILABLE direction must have a label")
        if self.volatility_status is ContextStatus.AVAILABLE and self.volatility_regime is VolatilityState.NOT_AVAILABLE:
            raise ValueError("AVAILABLE volatility must have a label")
        if self.breadth_status is ContextStatus.AVAILABLE and self.breadth_regime is BreadthState.NOT_AVAILABLE:
            raise ValueError("AVAILABLE breadth must have a label")
        if min(self.sample_size, self.available_count, self.missing_count) < 0:
            raise ValueError("regime counts cannot be negative")
        if self.available_count + self.missing_count != self.sample_size:
            raise ValueError("available_count + missing_count must equal sample_size")
        coverage = _decimal(self.coverage_ratio, "coverage_ratio")
        if self.sample_size == 0:
            if coverage is not None or self.status is ContextStatus.AVAILABLE:
                raise ValueError("empty regime sample must not have coverage or AVAILABLE status")
        else:
            expected = Decimal(self.available_count) / Decimal(self.sample_size)
            if coverage is None or coverage != expected or not Decimal("0") <= coverage <= Decimal("1"):
                raise ValueError("coverage_ratio must equal available_count / sample_size")
            if self.missing_count and self.status is ContextStatus.AVAILABLE:
                raise ValueError("regime with missing members cannot be AVAILABLE")
        if ContextStatus.ERROR in dimension_statuses:
            expected_status = ContextStatus.ERROR
        elif ContextStatus.STALE in dimension_statuses:
            expected_status = ContextStatus.STALE
        elif all(item is ContextStatus.AVAILABLE for item in dimension_statuses):
            expected_status = ContextStatus.AVAILABLE
        elif any(item in {ContextStatus.AVAILABLE, ContextStatus.PARTIAL} for item in dimension_statuses):
            expected_status = ContextStatus.PARTIAL
        else:
            expected_status = ContextStatus.NOT_AVAILABLE
        if self.status is not expected_status:
            raise ValueError("regime status violates dimension status precedence")
        object.__setattr__(self, "support_evidence", _evidence(self.support_evidence, max_bytes=evidence_max_bytes))
        object.__setattr__(self, "conflict_evidence", _evidence(self.conflict_evidence, max_bytes=evidence_max_bytes))
        missing_evidence = _evidence(self.missing_evidence, max_bytes=evidence_max_bytes)
        object.__setattr__(self, "missing_evidence", missing_evidence)
        if self.status is ContextStatus.PARTIAL and any(
            item in {ContextStatus.PARTIAL, ContextStatus.NOT_AVAILABLE} for item in dimension_statuses
        ) and not missing_evidence:
            raise ValueError("PARTIAL regime requires missing_evidence")
        object.__setattr__(self, "coverage_ratio", coverage)
        object.__setattr__(self, "input_reference", _bounded_mapping(self.input_reference, "input_reference", max_bytes=evidence_max_bytes))
        object.__setattr__(self, "reason_code", reason)


@dataclass(frozen=True, slots=True)
class RelativeStrengthSnapshot:
    symbol: str
    benchmark: str
    timeframe: str
    context_timestamp: datetime
    candidate_return_pct: Decimal | None
    benchmark_return_pct: Decimal | None
    relative_return_pct: Decimal | None
    relative_class: RelativeStrengthClass | str
    universe_run_id: int | None
    sample_size: int
    available_count: int
    missing_count: int
    coverage_ratio: Decimal | None
    status: ContextStatus | str
    processed_at: datetime
    missing_evidence: tuple[str, ...] = field(default_factory=tuple)
    input_reference: Mapping[str, Any] = field(default_factory=dict)
    reason_code: ReasonCode | str | None = None
    calculation_version: str = "phase5-v1"
    evidence_max_bytes: InitVar[int] = MAX_EVIDENCE_BYTES

    def __post_init__(self, evidence_max_bytes: int) -> None:
        if not self.symbol.strip() or self.benchmark not in {"BTCUSDT", "ETHUSDT", "MARKET_UNIVERSE_EQUAL_WEIGHT"}:
            raise ValueError("invalid relative-strength identity")
        if self.timeframe not in {"15m", "1H", "4H"}:
            raise ValueError("relative strength supports 15m, 1H, and 4H only")
        status = _enum(self.status, ContextStatus, "status")
        reason = _enum(self.reason_code, ReasonCode, "reason_code") if self.reason_code is not None else None
        _require_reason(status, reason)
        relative_class = _enum(self.relative_class, RelativeStrengthClass, "relative_class")
        _require_unavailable_label(status, relative_class, RelativeStrengthClass.NOT_AVAILABLE)
        _reject_unavailable_values(status, (self.candidate_return_pct, self.benchmark_return_pct, self.relative_return_pct))
        if self.universe_run_id is not None and self.universe_run_id <= 0:
            raise ValueError("universe_run_id must be positive")
        if self.benchmark == "MARKET_UNIVERSE_EQUAL_WEIGHT" and self.universe_run_id is None:
            raise ValueError("market benchmark requires universe_run_id")
        if self.benchmark != "MARKET_UNIVERSE_EQUAL_WEIGHT" and self.universe_run_id is not None:
            raise ValueError("BTC/ETH benchmark must not carry universe_run_id")
        if self.symbol == self.benchmark and status is not ContextStatus.NOT_AVAILABLE:
            raise ValueError("self relative strength must be NOT_AVAILABLE")
        if min(self.sample_size, self.available_count, self.missing_count) < 0:
            raise ValueError("relative-strength counts cannot be negative")
        if self.available_count + self.missing_count != self.sample_size:
            raise ValueError("available_count + missing_count must equal sample_size")
        coverage = _decimal(self.coverage_ratio, "coverage_ratio")
        if self.sample_size == 0:
            if coverage is not None or status is not ContextStatus.NOT_AVAILABLE:
                raise ValueError("empty relative-strength sample must have null coverage")
        else:
            expected = Decimal(self.available_count) / Decimal(self.sample_size)
            if coverage is None or coverage != expected or not Decimal("0") <= coverage <= Decimal("1"):
                raise ValueError("coverage_ratio must equal available_count / sample_size")
            if self.missing_count and status is ContextStatus.AVAILABLE:
                raise ValueError("relative strength with missing members cannot be AVAILABLE")
            if status is ContextStatus.AVAILABLE and any(
                value is None
                for value in (self.candidate_return_pct, self.benchmark_return_pct, self.relative_return_pct)
            ):
                raise ValueError("AVAILABLE relative strength requires calculated values")
        evidence = _evidence(self.missing_evidence, max_bytes=evidence_max_bytes)
        _require_partial_evidence(
            status,
            (self.candidate_return_pct, self.benchmark_return_pct, self.relative_return_pct),
            evidence,
            missing_count=self.missing_count,
        )
        object.__setattr__(self, "context_timestamp", _utc(self.context_timestamp, "context_timestamp"))
        object.__setattr__(self, "processed_at", _utc(self.processed_at, "processed_at"))
        object.__setattr__(self, "candidate_return_pct", _decimal(self.candidate_return_pct, "candidate_return_pct"))
        object.__setattr__(self, "benchmark_return_pct", _decimal(self.benchmark_return_pct, "benchmark_return_pct"))
        object.__setattr__(self, "relative_return_pct", _decimal(self.relative_return_pct, "relative_return_pct"))
        object.__setattr__(self, "relative_class", relative_class)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "coverage_ratio", coverage)
        object.__setattr__(self, "missing_evidence", evidence)
        object.__setattr__(self, "input_reference", _bounded_mapping(self.input_reference, "input_reference", max_bytes=evidence_max_bytes))
        object.__setattr__(self, "reason_code", reason)


@dataclass(frozen=True, slots=True)
class SectorMembership:
    mapping_version: str
    symbol: str
    sector: str
    source_reference: str
    effective_from: datetime
    effective_to: datetime | None
    status: MappingStatus | str
    processed_at: datetime

    def __post_init__(self) -> None:
        if not self.mapping_version.strip() or not self.symbol.strip() or not self.sector.strip():
            raise ValueError("mapping version, symbol, and sector are required")
        start = _utc(self.effective_from, "effective_from")
        end = _utc(self.effective_to, "effective_to")
        processed = _utc(self.processed_at, "processed_at")
        assert start is not None and processed is not None
        if end is not None and end <= start:
            raise ValueError("effective_to must be after effective_from")
        object.__setattr__(self, "effective_from", start)
        object.__setattr__(self, "effective_to", end)
        object.__setattr__(self, "processed_at", processed)
        object.__setattr__(self, "status", _enum(self.status, MappingStatus, "status"))


@dataclass(frozen=True, slots=True)
class SectorContextSnapshot:
    sector: str
    timeframe: str
    context_timestamp: datetime
    universe_run_id: int | None
    mapping_version: str
    sector_return_pct: Decimal | None
    sector_positive_ratio: Decimal | None
    member_count: int
    sample_size: int
    available_count: int
    missing_count: int
    coverage_ratio: Decimal | None
    status: ContextStatus | str
    processed_at: datetime
    missing_evidence: tuple[str, ...] = field(default_factory=tuple)
    reason_code: ReasonCode | str | None = None
    input_reference: Mapping[str, Any] = field(default_factory=dict)
    calculation_version: str = "phase5-v1"
    evidence_max_bytes: InitVar[int] = MAX_EVIDENCE_BYTES

    def __post_init__(self, evidence_max_bytes: int) -> None:
        if not self.sector.strip() or self.timeframe not in {"15m", "1H", "4H"}:
            raise ValueError("sector and supported timeframe are required")
        if min(self.member_count, self.sample_size, self.available_count, self.missing_count) < 0:
            raise ValueError("sector counts cannot be negative")
        if self.available_count + self.missing_count != self.sample_size:
            raise ValueError("available_count + missing_count must equal sample_size")
        status = _enum(self.status, ContextStatus, "status")
        reason = _enum(self.reason_code, ReasonCode, "reason_code") if self.reason_code is not None else None
        _require_reason(status, reason)
        _reject_unavailable_values(status, (self.sector_return_pct, self.sector_positive_ratio))
        if self.universe_run_id is not None and self.universe_run_id <= 0:
            raise ValueError("universe_run_id must be positive")
        if self.universe_run_id is None and status is not ContextStatus.NOT_AVAILABLE:
            raise ValueError("usable sector context requires universe_run_id")
        if self.sample_size == 0:
            if self.coverage_ratio is not None or status is not ContextStatus.NOT_AVAILABLE:
                raise ValueError("empty sector sample must be NOT_AVAILABLE")
        else:
            coverage = _decimal(self.coverage_ratio, "coverage_ratio")
            expected = Decimal(self.available_count) / Decimal(self.sample_size)
            if coverage is None or coverage != expected or not Decimal("0") <= coverage <= Decimal("1"):
                raise ValueError("coverage_ratio must equal available_count / sample_size")
            object.__setattr__(self, "coverage_ratio", coverage)
            if status is ContextStatus.AVAILABLE and any(
                value is None for value in (self.sector_return_pct, self.sector_positive_ratio)
            ):
                raise ValueError("AVAILABLE sector context requires calculated values")
        evidence = _evidence(self.missing_evidence, max_bytes=evidence_max_bytes)
        _require_partial_evidence(
            status,
            (self.sector_return_pct, self.sector_positive_ratio),
            evidence,
            missing_count=self.missing_count,
        )
        ratio = _decimal(self.sector_positive_ratio, "sector_positive_ratio")
        if ratio is not None and not Decimal("0") <= ratio <= Decimal("1"):
            raise ValueError("sector_positive_ratio must be between 0 and 1")
        object.__setattr__(self, "context_timestamp", _utc(self.context_timestamp, "context_timestamp"))
        object.__setattr__(self, "processed_at", _utc(self.processed_at, "processed_at"))
        object.__setattr__(self, "sector_return_pct", _decimal(self.sector_return_pct, "sector_return_pct"))
        object.__setattr__(self, "sector_positive_ratio", ratio)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "missing_evidence", evidence)
        object.__setattr__(self, "input_reference", _bounded_mapping(self.input_reference, "input_reference", max_bytes=evidence_max_bytes))
        object.__setattr__(self, "reason_code", reason)


@dataclass(frozen=True, slots=True)
class Stage1Phase5Enrichment:
    screening_run_id: int
    symbol: str
    universe_run_id: int | None
    sector: str
    mapping_version: str | None
    candidate_return_pct: Decimal | None
    sector_return_pct: Decimal | None
    candidate_vs_sector_pct: Decimal | None
    sector_relation: SectorRelation | str
    context_status: ContextStatus | str
    leader_context_ref: Mapping[str, Any]
    regime_context_ref: Mapping[str, Any]
    relative_strength_ref: Mapping[str, Any]
    sector_context_ref: Mapping[str, Any]
    processed_at: datetime
    missing_evidence: tuple[str, ...] = field(default_factory=tuple)
    reason_code: ReasonCode | str | None = None
    context_only: bool = True
    evidence_max_bytes: InitVar[int] = MAX_EVIDENCE_BYTES

    def __post_init__(self, evidence_max_bytes: int) -> None:
        if self.screening_run_id <= 0 or not self.symbol.strip() or not self.sector.strip():
            raise ValueError("screening_run_id, symbol, and sector are required")
        if not self.context_only:
            raise ValueError("Phase 5 enrichment is context-only")
        status = _enum(self.context_status, ContextStatus, "context_status")
        reason = _enum(self.reason_code, ReasonCode, "reason_code") if self.reason_code is not None else None
        if self.universe_run_id is not None and self.universe_run_id <= 0:
            raise ValueError("universe_run_id must be positive")
        _require_reason(status, reason)
        relation = _enum(self.sector_relation, SectorRelation, "sector_relation")
        _require_unavailable_label(status, relation, SectorRelation.NOT_AVAILABLE)
        _reject_unavailable_values(
            status,
            (self.candidate_return_pct, self.sector_return_pct, self.candidate_vs_sector_pct),
        )
        if status is ContextStatus.AVAILABLE and any(
            value is None for value in (self.candidate_return_pct, self.sector_return_pct, self.candidate_vs_sector_pct)
        ):
            raise ValueError("AVAILABLE enrichment requires calculated values")
        evidence = _evidence(self.missing_evidence, max_bytes=evidence_max_bytes)
        _require_partial_evidence(
            status,
            (self.candidate_return_pct, self.sector_return_pct, self.candidate_vs_sector_pct),
            evidence,
        )
        object.__setattr__(self, "candidate_return_pct", _decimal(self.candidate_return_pct, "candidate_return_pct"))
        object.__setattr__(self, "sector_return_pct", _decimal(self.sector_return_pct, "sector_return_pct"))
        object.__setattr__(self, "candidate_vs_sector_pct", _decimal(self.candidate_vs_sector_pct, "candidate_vs_sector_pct"))
        object.__setattr__(self, "sector_relation", relation)
        object.__setattr__(self, "context_status", status)
        object.__setattr__(self, "processed_at", _utc(self.processed_at, "processed_at"))
        object.__setattr__(self, "missing_evidence", evidence)
        for name in (
            "leader_context_ref",
            "regime_context_ref",
            "relative_strength_ref",
            "sector_context_ref",
        ):
            object.__setattr__(self, name, _bounded_mapping(getattr(self, name), name, max_bytes=evidence_max_bytes))
        object.__setattr__(self, "reason_code", reason)
