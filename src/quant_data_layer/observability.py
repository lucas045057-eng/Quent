"""Bounded, secret-safe runtime observability contracts for Data Layer V1.

This module only describes measurements. It does not schedule work, persist
high-frequency samples, or infer unavailable values.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import math
from pathlib import Path
import re
import time
from typing import Any, Generic, Mapping, TypeVar


MAX_SOURCE_SNAPSHOTS = 12
_SAFE_TEXT_LIMIT = 256
_SECRET_TEXT = re.compile(
    r"(?:https?|wss?)://|(?:api[_-]?key|password|secret|token|authorization)\s*[:=]|://[^/\s]+@",
    re.IGNORECASE,
)


class MeasurementState(StrEnum):
    AVAILABLE = "AVAILABLE"
    NOT_EXPOSED = "NOT_EXPOSED"


class MeasurementReason(StrEnum):
    NOT_INSTRUMENTED = "NOT_INSTRUMENTED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    EVENT_LOOP_UNAVAILABLE = "EVENT_LOOP_UNAVAILABLE"
    UNSUPPORTED_PLATFORM = "UNSUPPORTED_PLATFORM"
    READ_ERROR = "READ_ERROR"
    NO_SAMPLE_YET = "NO_SAMPLE_YET"


class ProcessRole(StrEnum):
    COLLECTOR = "collector"
    ENGINE = "engine"


class SourcePhase(StrEnum):
    PHASE1 = "phase1"
    PHASE2 = "phase2"
    PHASE3 = "phase3"
    PHASE4 = "phase4"
    PHASE5 = "phase5"
    PHASE6 = "phase6"
    PHASE7 = "phase7"
    PHASE8 = "phase8"
    PHASE9 = "phase9"


class SourceId(StrEnum):
    PHASE1_MARKET_DATA = "phase1.market_data"
    ENGINE_STAGE1 = "engine.stage1"
    ENGINE_PHASE2 = "engine.phase2_derivatives"
    PHASE2_DERIVATIVES = "phase2.derivatives"
    PHASE3_BITGET_TRADES = "phase3.bitget_trades"
    PHASE3_BYBIT_TRADES = "phase3.bybit_trades"
    PHASE3_HYPERLIQUID_TRADES = "phase3.hyperliquid_trades"
    PHASE3_FLOW_PROCESSING = "phase3.flow_processing"
    PHASE4_LIQUIDATION = "phase4.liquidation"
    PHASE5_CONTEXT = "phase5.context"
    PHASE6_EXTERNAL_CONTEXT = "phase6.external_context"
    PHASE6_AI = "phase6.ai"
    PHASE7_ONCHAIN = "phase7.onchain"
    PHASE7_SPOT = "phase7.spot"
    PHASE8_OPTIONS_MARKET = "phase8.options_market"
    PHASE8_OPTIONS_CONTEXT = "phase8.options_context"
    PHASE9_EVALUATIONS = "phase9.evaluations"


class LifecycleState(StrEnum):
    NOT_CONFIGURED = "NOT_CONFIGURED"
    INITIALIZING = "INITIALIZING"
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    RATE_LIMITED = "RATE_LIMITED"
    STALE = "STALE"
    ERROR = "ERROR"
    SHUTTING_DOWN = "SHUTTING_DOWN"


class DataState(StrEnum):
    AVAILABLE = "AVAILABLE"
    STALE = "STALE"
    NOT_AVAILABLE = "NOT_AVAILABLE"
    PARTIAL = "PARTIAL"
    ERROR = "ERROR"


class ErrorCategory(StrEnum):
    PROVIDER_RATE_LIMIT = "PROVIDER_RATE_LIMIT"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_CONTRACT = "PROVIDER_CONTRACT"
    NETWORK = "NETWORK"
    PARSER = "PARSER"
    DATA_QUALITY = "DATA_QUALITY"
    PERSISTENCE = "PERSISTENCE"
    CHECKPOINT = "CHECKPOINT"
    BACKPRESSURE = "BACKPRESSURE"
    ADMISSION_TIMEOUT = "ADMISSION_TIMEOUT"
    RESOURCE = "RESOURCE"
    CONFIGURATION = "CONFIGURATION"
    SHUTDOWN = "SHUTDOWN"
    UNKNOWN = "UNKNOWN"


class SourceMetric(StrEnum):
    LIFECYCLE_STATE = "lifecycle_state"
    DATA_STATUS = "data_status"
    LAST_SOURCE_TIMESTAMP_UTC = "last_source_timestamp_utc"
    LAST_FETCHED_AT_UTC = "last_fetched_at_utc"
    LAST_PROCESSED_AT_UTC = "last_processed_at_utc"
    LAST_PERSISTED_AT_UTC = "last_persisted_at_utc"
    CURSOR_KIND = "cursor_kind"
    CURSOR_VALUE = "cursor_value"
    CURSOR_UPDATED_AT_UTC = "cursor_updated_at_utc"
    FRESHNESS_AGE_SECONDS = "freshness_age_seconds"
    QUEUE_DEPTH = "queue_depth"
    QUEUE_CAPACITY = "queue_capacity"
    QUEUE_BYTES = "queue_bytes"
    OLDEST_PENDING_AGE_SECONDS = "oldest_pending_age_seconds"
    PENDING_WORK = "pending_work"
    ACTIVE_WORK = "active_work"
    BACKFILL_DEPTH = "backfill_depth"
    RETRY_COUNT = "retry_count"
    CONSECUTIVE_FAILURES = "consecutive_failures"
    LAST_ERROR_CATEGORY = "last_error_category"
    LAST_SUCCESS_AT_UTC = "last_success_at_utc"
    ADMISSION_WAIT_SECONDS = "admission_wait_seconds"
    DB_TRANSACTION_CLASS = "db_transaction_class"
    RECONNECT_COUNT = "reconnect_count"
    GAP_COUNT = "gap_count"
    DROP_COUNT = "drop_count"


class WorkClass(StrEnum):
    LIGHT = "LIGHT"
    MEDIUM = "MEDIUM"
    HEAVY = "HEAVY"


class ProcessMetric(StrEnum):
    ASYNC_TASK_COUNT = "async_task_count"
    LIGHT_ACTIVE = "light_active"
    LIGHT_PENDING = "light_pending"
    LIGHT_OLDEST_WAIT_SECONDS = "light_oldest_wait_seconds"
    MEDIUM_ACTIVE = "medium_active"
    MEDIUM_PENDING = "medium_pending"
    MEDIUM_OLDEST_WAIT_SECONDS = "medium_oldest_wait_seconds"
    HEAVY_ACTIVE = "heavy_active"
    HEAVY_PENDING = "heavy_pending"
    HEAVY_OLDEST_WAIT_SECONDS = "heavy_oldest_wait_seconds"
    PROCESS_RSS_BYTES = "process_rss_bytes"
    PROCESS_PSS_BYTES = "process_pss_bytes"
    PROCESS_CPU_SECONDS = "process_cpu_seconds"
    CGROUP_MEMORY_CURRENT_BYTES = "cgroup_memory_current_bytes"
    CGROUP_MEMORY_PEAK_BYTES = "cgroup_memory_peak_bytes"
    CGROUP_MEMORY_ANON_BYTES = "cgroup_memory_anon_bytes"
    CGROUP_MEMORY_FILE_BYTES = "cgroup_memory_file_bytes"
    CGROUP_CPU_USAGE_USEC = "cgroup_cpu_usage_usec"
    CGROUP_OOM_EVENTS = "cgroup_oom_events"
    CGROUP_OOM_KILL_EVENTS = "cgroup_oom_kill_events"
    SHUTDOWN_REQUESTED = "shutdown_requested"
    SHUTDOWN_OUTCOME = "shutdown_outcome"
    RESTART_COUNT = "restart_count"


class TransactionClass(StrEnum):
    HEALTH_STATUS = "HEALTH_STATUS"
    CANONICAL_BATCH = "CANONICAL_BATCH"
    LARGE_ATOMIC_SOURCE = "LARGE_ATOMIC_SOURCE"
    CHECKPOINT_CONTROL = "CHECKPOINT_CONTROL"
    CONTEXT_SNAPSHOT = "CONTEXT_SNAPSHOT"
    OPTIONS_RECONCILIATION = "OPTIONS_RECONCILIATION"


class DatabaseMetric(StrEnum):
    CONNECTIONS = "connections"
    PENDING_ADMISSIONS = "pending_admissions"
    ACTIVE_TRANSACTIONS = "active_transactions"
    APPLICATION_WRITER_ACTIVE_TRANSACTIONS = "application_writer_active_transactions"
    APPLICATION_WRITER_PENDING_BATCHES = "application_writer_pending_batches"
    LOCK_WAIT_SECONDS = "lock_wait_seconds"
    LOCK_HOLD_SECONDS = "lock_hold_seconds"
    LOCK_TIMEOUTS = "lock_timeouts"
    TRANSACTION_DURATION_SECONDS = "transaction_duration_seconds"
    TRANSACTION_ROWS = "transaction_rows"
    TRANSACTION_BYTES = "transaction_bytes"
    DATABASE_BYTES = "database_bytes"
    TABLE_INDEX_BYTES = "table_index_bytes"
    WAL_BYTES = "wal_bytes"
    CHECKPOINT_DURATION_SECONDS = "checkpoint_duration_seconds"
    DEAD_TUPLES = "dead_tuples"
    VACUUM_AGE_SECONDS = "vacuum_age_seconds"
    CGROUP_MEMORY_CURRENT_BYTES = "cgroup_memory_current_bytes"
    CGROUP_MEMORY_PEAK_BYTES = "cgroup_memory_peak_bytes"
    CGROUP_MEMORY_ANON_BYTES = "cgroup_memory_anon_bytes"
    CGROUP_MEMORY_FILE_BYTES = "cgroup_memory_file_bytes"
    CGROUP_OOM_EVENTS = "cgroup_oom_events"
    CGROUP_OOM_KILL_EVENTS = "cgroup_oom_kill_events"


T = TypeVar("T")


def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError("observability timestamps must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _safe_scalar(value: Any) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value < 0:
            raise ValueError("observability numeric values must be finite and non-negative")
        return
    if isinstance(value, datetime):
        require_utc(value)
        return
    if isinstance(value, StrEnum):
        value = value.value
    if isinstance(value, str):
        # Export text only as a low-cardinality enum-like token. Free-form values
        # can contain credentials or identifiers even when their shape is short.
        if (
            len(value) > _SAFE_TEXT_LIMIT
            or "\n" in value
            or "\r" in value
            or _SECRET_TEXT.search(value)
            or re.fullmatch(r"[A-Z][A-Z0-9_.-]{0,63}", value) is None
        ):
            raise ValueError("observability text is unsafe or exceeds its bound")
        return
    raise TypeError("observability values must be bounded scalars; payloads are not supported")


@dataclass(frozen=True, slots=True)
class Measurement(Generic[T]):
    state: MeasurementState
    value: T | None = None
    reason: MeasurementReason | None = None

    def __post_init__(self) -> None:
        if self.state is MeasurementState.AVAILABLE:
            if self.value is None or self.reason is not None:
                raise ValueError("AVAILABLE measurements require a value and no reason")
            _safe_scalar(self.value)
        elif self.state is MeasurementState.NOT_EXPOSED:
            if self.value is not None or self.reason is None:
                raise ValueError("NOT_EXPOSED measurements require a reason and no value")
        else:
            raise ValueError("unknown measurement state")

    @classmethod
    def available(cls, value: T) -> "Measurement[T]":
        return cls(MeasurementState.AVAILABLE, value=value)

    @classmethod
    def not_exposed(cls, reason: MeasurementReason = MeasurementReason.NOT_INSTRUMENTED) -> "Measurement[T]":
        return cls(MeasurementState.NOT_EXPOSED, reason=reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "value": _json_value(self.value),
            "reason": self.reason.value if self.reason is not None else None,
        }


@dataclass(frozen=True, slots=True)
class MetricObservation:
    name: StrEnum
    measurement: Measurement[Any]

    def __post_init__(self) -> None:
        if not isinstance(self.name, StrEnum):
            raise TypeError("metric name must be a registered enum")
        if not isinstance(self.measurement, Measurement):
            raise TypeError("metric measurement is required")

    def to_dict(self) -> dict[str, Any]:
        return self.measurement.to_dict()


def _unknown_metrics(names: type[StrEnum]) -> tuple[MetricObservation, ...]:
    reason = Measurement.not_exposed(MeasurementReason.NOT_INSTRUMENTED)
    return tuple(MetricObservation(name, reason) for name in names)


def _merge_metrics(
    names: type[StrEnum],
    overrides: Mapping[StrEnum, Measurement[Any]] | None,
) -> tuple[MetricObservation, ...]:
    values = {item.name: item.measurement for item in _unknown_metrics(names)}
    if overrides:
        for name, measurement in overrides.items():
            if not isinstance(name, names):
                raise ValueError("metric name is not in the registered metric set")
            if not isinstance(measurement, Measurement):
                raise TypeError("metric values must use Measurement")
            values[name] = measurement
    return tuple(MetricObservation(name, values[name]) for name in names)


def _lookup(items: tuple[MetricObservation, ...], name: StrEnum) -> Measurement[Any]:
    for item in items:
        if item.name is name:
            return item.measurement
    raise KeyError(name)


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    source_id: SourceId
    phase: SourcePhase
    configured: bool
    sampled_at_utc: datetime
    metrics: tuple[MetricObservation, ...]

    def __post_init__(self) -> None:
        require_utc(self.sampled_at_utc)
        if not isinstance(self.source_id, SourceId) or not isinstance(self.phase, SourcePhase):
            raise TypeError("source identity must use registered enums")
        if not isinstance(self.configured, bool):
            raise TypeError("configured must be boolean")
        expected = set(SourceMetric)
        actual = {item.name for item in self.metrics}
        if actual != expected or len(self.metrics) != len(expected):
            raise ValueError("source snapshot must contain every registered metric exactly once")
        if not self.configured:
            state = self.metric(SourceMetric.LIFECYCLE_STATE)
            if state.state is MeasurementState.AVAILABLE and state.value != LifecycleState.NOT_CONFIGURED.value:
                raise ValueError("disabled source lifecycle must be NOT_CONFIGURED")

    @classmethod
    def not_exposed(
        cls,
        *,
        source_id: SourceId,
        phase: SourcePhase,
        configured: bool,
        sampled_at_utc: datetime,
        measurements: Mapping[SourceMetric, Measurement[Any]] | None = None,
    ) -> "SourceSnapshot":
        overrides = dict(measurements or {})
        if not configured:
            overrides[SourceMetric.LIFECYCLE_STATE] = Measurement.available(LifecycleState.NOT_CONFIGURED.value)
            overrides.setdefault(
                SourceMetric.DATA_STATUS,
                Measurement.not_exposed(MeasurementReason.NOT_CONFIGURED),
            )
        return cls(
            source_id=source_id,
            phase=phase,
            configured=configured,
            sampled_at_utc=require_utc(sampled_at_utc),
            metrics=_merge_metrics(SourceMetric, overrides),
        )

    def metric(self, name: SourceMetric) -> Measurement[Any]:
        return _lookup(self.metrics, name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id.value,
            "phase": self.phase.value,
            "configured": self.configured,
            "sampled_at_utc": _iso_utc(self.sampled_at_utc),
            "metrics": {item.name.value: item.to_dict() for item in self.metrics},
        }


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    role: ProcessRole
    sampled_at_utc: datetime
    metrics: tuple[MetricObservation, ...]

    def __post_init__(self) -> None:
        require_utc(self.sampled_at_utc)
        if not isinstance(self.role, ProcessRole):
            raise TypeError("process role must use a registered enum")
        expected = set(ProcessMetric)
        actual = {item.name for item in self.metrics}
        if actual != expected or len(self.metrics) != len(expected):
            raise ValueError("process snapshot must contain every registered metric exactly once")

    @classmethod
    def not_exposed(
        cls,
        role: ProcessRole,
        *,
        sampled_at_utc: datetime,
        measurements: Mapping[ProcessMetric, Measurement[Any]] | None = None,
    ) -> "ProcessSnapshot":
        return cls(
            role=role,
            sampled_at_utc=require_utc(sampled_at_utc),
            metrics=_merge_metrics(ProcessMetric, measurements),
        )

    def metric(self, name: ProcessMetric) -> Measurement[Any]:
        return _lookup(self.metrics, name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "sampled_at_utc": _iso_utc(self.sampled_at_utc),
            "metrics": {item.name.value: item.to_dict() for item in self.metrics},
        }


@dataclass(frozen=True, slots=True)
class TransactionClassSnapshot:
    transaction_class: TransactionClass
    pending: Measurement[int]
    active: Measurement[int]
    wait_seconds: Measurement[float]
    hold_seconds: Measurement[float]
    timeouts: Measurement[int]

    @classmethod
    def not_exposed(cls, transaction_class: TransactionClass) -> "TransactionClassSnapshot":
        unknown = Measurement.not_exposed(MeasurementReason.NOT_INSTRUMENTED)
        return cls(transaction_class, unknown, unknown, unknown, unknown, unknown)


@dataclass(frozen=True, slots=True)
class DatabaseSnapshot:
    sampled_at_utc: datetime
    metrics: tuple[MetricObservation, ...]
    transaction_classes: tuple[TransactionClassSnapshot, ...]

    def __post_init__(self) -> None:
        require_utc(self.sampled_at_utc)
        expected = set(DatabaseMetric)
        actual = {item.name for item in self.metrics}
        if actual != expected or len(self.metrics) != len(expected):
            raise ValueError("database snapshot must contain every registered metric exactly once")
        classes = {item.transaction_class for item in self.transaction_classes}
        if classes != set(TransactionClass) or len(self.transaction_classes) != len(classes):
            raise ValueError("database snapshot must include every registered transaction class exactly once")

    @classmethod
    def not_exposed(
        cls,
        *,
        sampled_at_utc: datetime,
        measurements: Mapping[DatabaseMetric, Measurement[Any]] | None = None,
        transaction_classes: tuple[TransactionClassSnapshot, ...] | None = None,
    ) -> "DatabaseSnapshot":
        return cls(
            sampled_at_utc=require_utc(sampled_at_utc),
            metrics=_merge_metrics(DatabaseMetric, measurements),
            transaction_classes=transaction_classes or tuple(
                TransactionClassSnapshot.not_exposed(value) for value in TransactionClass
            ),
        )

    def metric(self, name: DatabaseMetric) -> Measurement[Any]:
        return _lookup(self.metrics, name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sampled_at_utc": _iso_utc(self.sampled_at_utc),
            "metrics": {item.name.value: item.to_dict() for item in self.metrics},
            "transaction_classes": [
                {
                    "transaction_class": item.transaction_class.value,
                    "pending": item.pending.to_dict(),
                    "active": item.active.to_dict(),
                    "wait_seconds": item.wait_seconds.to_dict(),
                    "hold_seconds": item.hold_seconds.to_dict(),
                    "timeouts": item.timeouts.to_dict(),
                }
                for item in self.transaction_classes
            ],
        }


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    process: ProcessSnapshot
    sources: tuple[SourceSnapshot, ...]
    database: DatabaseSnapshot

    def __post_init__(self) -> None:
        if len(self.sources) > MAX_SOURCE_SNAPSHOTS:
            raise ValueError(f"source snapshot count exceeds bound of {MAX_SOURCE_SNAPSHOTS}")
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source snapshots must have unique registered source ids")
        sample_time = self.process.sampled_at_utc
        if self.database.sampled_at_utc != sample_time:
            raise ValueError("process and database snapshots must share one sample timestamp")
        if any(source.sampled_at_utc != sample_time for source in self.sources):
            raise ValueError("all runtime snapshots must share one sample timestamp")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "DATA_LAYER_OBSERVABILITY_V1",
            "sampled_at_utc": _iso_utc(self.process.sampled_at_utc),
            "process": self.process.to_dict(),
            "sources": [source.to_dict() for source in sorted(self.sources, key=lambda item: item.source_id.value)],
            "database": self.database.to_dict(),
        }


def capture_process_snapshot(
    role: ProcessRole,
    *,
    sampled_at_utc: datetime,
    admission_snapshot: Any | None = None,
    shutdown_requested: bool | None = None,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> ProcessSnapshot:
    """Capture bounded process-local counters; unavailable OS counters stay explicit."""

    now = require_utc(sampled_at_utc)
    measurements: dict[ProcessMetric, Measurement[Any]] = {}

    if admission_snapshot is not None:
        if getattr(admission_snapshot, "role", None) is not role:
            raise ValueError("admission snapshot process role does not match process snapshot")
        active_by_class = getattr(admission_snapshot, "active_by_class", None)
        pending_by_class = getattr(admission_snapshot, "pending_by_class", None)
        oldest_by_class = getattr(admission_snapshot, "oldest_pending_wait_seconds_by_class", None)
        if not all(isinstance(values, Mapping) for values in (active_by_class, pending_by_class, oldest_by_class)):
            raise TypeError("admission snapshot is missing registered class measurements")
        metric_names = {
            WorkClass.LIGHT: (
                ProcessMetric.LIGHT_ACTIVE,
                ProcessMetric.LIGHT_PENDING,
                ProcessMetric.LIGHT_OLDEST_WAIT_SECONDS,
            ),
            WorkClass.MEDIUM: (
                ProcessMetric.MEDIUM_ACTIVE,
                ProcessMetric.MEDIUM_PENDING,
                ProcessMetric.MEDIUM_OLDEST_WAIT_SECONDS,
            ),
            WorkClass.HEAVY: (
                ProcessMetric.HEAVY_ACTIVE,
                ProcessMetric.HEAVY_PENDING,
                ProcessMetric.HEAVY_OLDEST_WAIT_SECONDS,
            ),
        }
        for work_class, (active_name, pending_name, oldest_name) in metric_names.items():
            if any(work_class not in values for values in (active_by_class, pending_by_class, oldest_by_class)):
                raise ValueError("admission snapshot must include every registered work class")
            measurements[active_name] = Measurement.available(active_by_class[work_class])
            measurements[pending_name] = Measurement.available(pending_by_class[work_class])
            measurements[oldest_name] = Measurement.available(oldest_by_class[work_class])

    try:
        measurements[ProcessMetric.ASYNC_TASK_COUNT] = Measurement.available(len(asyncio.all_tasks()))
    except RuntimeError:
        measurements[ProcessMetric.ASYNC_TASK_COUNT] = Measurement.not_exposed(
            MeasurementReason.EVENT_LOOP_UNAVAILABLE
        )

    cpu_seconds = time.process_time()
    measurements[ProcessMetric.PROCESS_CPU_SECONDS] = Measurement.available(max(0.0, cpu_seconds))
    measurements[ProcessMetric.PROCESS_RSS_BYTES] = _proc_status_bytes(proc_root / "self" / "status", "VmRSS:")
    measurements[ProcessMetric.PROCESS_PSS_BYTES] = _proc_status_bytes(
        proc_root / "self" / "smaps_rollup", "Pss:"
    )
    measurements[ProcessMetric.CGROUP_MEMORY_CURRENT_BYTES] = _read_integer(
        cgroup_root / "memory.current", scale=1
    )
    measurements[ProcessMetric.CGROUP_MEMORY_PEAK_BYTES] = _read_integer(
        cgroup_root / "memory.peak", scale=1
    )
    memory_stats = _read_key_values(cgroup_root / "memory.stat")
    measurements[ProcessMetric.CGROUP_MEMORY_ANON_BYTES] = _measurement_from_mapping(memory_stats, "anon")
    measurements[ProcessMetric.CGROUP_MEMORY_FILE_BYTES] = _measurement_from_mapping(memory_stats, "file")
    cpu_stats = _read_key_values(cgroup_root / "cpu.stat")
    measurements[ProcessMetric.CGROUP_CPU_USAGE_USEC] = _measurement_from_mapping(cpu_stats, "usage_usec")
    event_stats = _read_key_values(cgroup_root / "memory.events")
    measurements[ProcessMetric.CGROUP_OOM_EVENTS] = _measurement_from_mapping(event_stats, "oom")
    measurements[ProcessMetric.CGROUP_OOM_KILL_EVENTS] = _measurement_from_mapping(event_stats, "oom_kill")
    if shutdown_requested is not None:
        measurements[ProcessMetric.SHUTDOWN_REQUESTED] = Measurement.available(shutdown_requested)

    return ProcessSnapshot.not_exposed(role, sampled_at_utc=now, measurements=measurements)


def _proc_status_bytes(path: Path, key: str) -> Measurement[int]:
    try:
        for line in path.read_text(encoding="ascii").splitlines():
            if line.startswith(key):
                value = int(line.split()[1]) * 1024
                return Measurement.available(value)
    except (OSError, ValueError, IndexError):
        return Measurement.not_exposed(MeasurementReason.READ_ERROR)
    return Measurement.not_exposed(MeasurementReason.UNSUPPORTED_PLATFORM)


def _read_integer(path: Path, *, scale: int) -> Measurement[int]:
    try:
        return Measurement.available(int(path.read_text(encoding="ascii").strip()) * scale)
    except (OSError, ValueError):
        return Measurement.not_exposed(MeasurementReason.UNSUPPORTED_PLATFORM)


def _read_key_values(path: Path) -> dict[str, int]:
    try:
        rows = path.read_text(encoding="ascii").splitlines()
    except OSError:
        return {}
    result: dict[str, int] = {}
    for row in rows:
        parts = row.split()
        if len(parts) == 2:
            try:
                result[parts[0]] = int(parts[1])
            except ValueError:
                continue
    return result


def _measurement_from_mapping(values: Mapping[str, int], key: str) -> Measurement[int]:
    if key not in values:
        return Measurement.not_exposed(MeasurementReason.UNSUPPORTED_PLATFORM)
    return Measurement.available(values[key])


def _iso_utc(value: datetime) -> str:
    return require_utc(value).isoformat().replace("+00:00", "Z")


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return _iso_utc(value)
    return value
