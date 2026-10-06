#!/usr/bin/env python3
"""Run the bounded, public-only Data Layer V1 local acceptance gate."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import psycopg

from scripts.phase7_acceptance_monitor import (
    APPLICATION_METRIC_KEYS,
    APPLICATION_STATE_KEYS,
    CGROUP_READ,
)


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "docker-compose.data-layer-acceptance.yml"
ENV_FILE = ROOT / ".env.local"
ARTIFACT_ROOT = ROOT / ".superpowers/sdd/2026-09-26-data-layer-v1-hardening/stage8"
OWNER = "data-layer-v1-acceptance"
ACCEPTANCE_DURATION_SECONDS = 900
ACCEPTANCE_SAMPLE_INTERVAL_SECONDS = 10
MAX_SAMPLE_BYTES = 64 * 1024
MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
MAX_LOG_SAMPLE_BYTES = 128 * 1024
MAX_LOG_TAIL_LINES = 100
LOG_COMMAND_TIMEOUT_SECONDS = 5
MIN_DISK_FREE_RATIO = 0.15
MAX_COMMAND_OUTPUT_BYTES = 256 * 1024
STARTUP_TIMEOUT_SECONDS = 180
HEALTH_MAX_AGE_SECONDS = 180
PHASE6_HEALTH_MAX_AGE_SECONDS = 420
SERVICES = ("postgres", "quant-collector", "quant-engine")
SERVICE_MEMORY_LIMITS = {"postgres": "768m", "quant-collector": "256m", "quant-engine": "384m"}
EXPECTED_SERVICES = set(SERVICES)
PUBLIC_SOURCE_ENV_KEYS = frozenset({
    "BITGET_REST_BASE_URL",
    "BITGET_WS_PUBLIC_URL",
    "KLINE_FETCH_LIMIT",
    "KLINE_RETENTION_5M_DAYS",
    "KLINE_RETENTION_15M_DAYS",
    "KLINE_RETENTION_1H_DAYS",
    "KLINE_RETENTION_4H_DAYS",
    "MAX_TRADE_QUEUE_SIZE",
    "MAX_TRADE_STREAM_SYMBOLS",
    "TRADE_DEDUP_MAX_ENTRIES_PER_EXCHANGE",
    "PHASE2_MIN_OI_SOURCES",
    "PHASE2_MIN_FUNDING_SOURCES",
    "PHASE3_ENABLED",
    "PHASE4_BASIS_RETENTION_DAYS",
    "PHASE4_BITGET_CLASSIC_REST_BASE_URL",
    "PHASE4_BITGET_UTA_REST_BASE_URL",
    "PHASE4_BITGET_UTA_WS_PUBLIC_URL",
    "PHASE4_BYBIT_REST_BASE_URL",
    "PHASE4_BYBIT_WS_PUBLIC_LINEAR_URL",
    "PHASE4_HYPERLIQUID_INFO_URL",
    "PHASE4_BUILDER_WINDOW_BUDGET",
    "PHASE4_CROSS_EXCHANGE_RETENTION_DAYS",
    "PHASE4_ENRICHMENT_RETENTION_DAYS",
    "PHASE4_EVENT_BUDGET",
    "PHASE4_EVENT_BYTES_BUDGET",
    "PHASE4_FINALIZED_WINDOW_BUDGET",
    "PHASE4_HYDRATION_WINDOWS_PER_KEY",
    "PHASE4_LIQUIDATION_15M_RETENTION_DAYS",
    "PHASE4_LIQUIDATION_1H_RETENTION_DAYS",
    "PHASE4_LIQUIDATION_1M_RETENTION_DAYS",
    "PHASE4_LIQUIDATION_4H_RETENTION_DAYS",
    "PHASE4_LIQUIDATION_5M_RETENTION_DAYS",
    "PHASE4_LIQUIDATION_EVENT_RETENTION_HOURS",
    "PHASE4_LONG_SHORT_RETENTION_DAYS",
    "PHASE4_MAX_BASIS_TIMESTAMP_SKEW_SECONDS",
    "PHASE4_QUEUE_CAPACITY",
    "PHASE4_REST_CYCLE_SECONDS",
    "PHASE4_ROLLUP_WINDOW_BUDGET",
    "PHASE7_ENABLED",
    "PHASE7_BITCOIN_RPC_ENABLED",
    "PHASE7_BITCOIN_RPC_URL",
    "PHASE7_BITCOIN_RPC_AUTH_MODE",
    "PHASE7_BITCOIN_RPC_API_KEY",
    "PHASE7_BITCOIN_RPC_TIMEOUT_SECONDS",
    "PHASE7_BITCOIN_RPC_REQUESTS_PER_SECOND",
    "PHASE7_BITCOIN_RPC_MAX_CONCURRENCY",
    "PHASE7_BITCOIN_RPC_MAX_RESPONSE_BYTES",
    "PHASE7_ETHEREUM_RPC_ENABLED",
    "PHASE7_ETHEREUM_RPC_URL",
    "PHASE7_ETHEREUM_RPC_AUTH_MODE",
    "PHASE7_ETHEREUM_RPC_TIMEOUT_SECONDS",
    "PHASE7_ETHEREUM_RPC_REQUESTS_PER_SECOND",
    "PHASE7_ETHEREUM_RPC_MAX_CONCURRENCY",
    "PHASE7_ETHEREUM_RPC_MAX_RESPONSE_BYTES",
    "PHASE7_CONTEXT_INTERVAL_SECONDS",
    "PHASE7_CONTEXT_LOOKBACK_HOURS",
    "PHASE7_CONTEXT_WINDOW_BATCH",
})
PUBLIC_ORIGINS = {
    "BITGET_REST_BASE_URL": ("https", "api.bitget.com"),
    "BITGET_WS_PUBLIC_URL": ("wss", "ws.bitget.com"),
    "PHASE4_BITGET_CLASSIC_REST_BASE_URL": ("https", "api.bitget.com"),
    "PHASE4_BITGET_UTA_REST_BASE_URL": ("https", "api.bitget.com"),
    "PHASE4_BITGET_UTA_WS_PUBLIC_URL": ("wss", "ws.bitget.com"),
    "PHASE4_BYBIT_REST_BASE_URL": ("https", "api.bybit.com"),
    "PHASE4_BYBIT_WS_PUBLIC_LINEAR_URL": ("wss", "stream.bybit.com"),
    "PHASE4_HYPERLIQUID_INFO_URL": ("https", "api.hyperliquid.xyz"),
}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_BASE_ENV = (
    "PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
    "XDG_RUNTIME_DIR", "LANG", "LC_ALL", "TERM",
)
_FORBIDDEN_ARTIFACT_KEYS = {
    "payload", "raw_payload", "raw_reference", "headers", "authorization",
    "api_key", "password", "secret", "rpc_url", "endpoint_url", "full_url",
}
SAFE_HEALTH_STATE_KEYS = APPLICATION_STATE_KEYS | frozenset({
    "phase4_status", "phase5_status", "phase6_status", "phase6_ai_status", "phase8_status",
    "reason", "reason_code", "failure_stage", "provider_status", "configured",
    "source_count", "persisted_events", "rejected_events", "last_success_at", "last_fetch_at",
    "last_persisted_at", "context_timestamp", "fetched_at", "processed_at",
    "source_timestamp", "exchange_timestamp", "checked_at", "observed_at",
    "freshness_age_seconds", "freshness_threshold_seconds", "expected_latest_closed_at",
    "context_age_seconds", "stale_since", "last_attempt_at", "last_error_at",
    "last_error_category", "last_error_type", "provider", "endpoint", "error_code",
    "error_type", "http_status", "schema_stage", "schema_error_summary",
    "usable_source_count", "source_failure_count", "available_source_count", "stale_source_count",
    "failure_type", "runtime_stage", "workers_started", "worker_count", "worker_health",
    "output_count", "available_output_count", "partial_output_count", "stale_output_count",
    "not_available_output_count", "error_output_count", "missing_evidence_count",
    "catalog_count", "ticker_subscription_count", "websocket_ready",
    "last_exchange_at", "last_fetched_at", "last_processed_at", "last_finalized_cursor",
    "progress_source", "chain", "data_quality", "gap_detected", "gap_reason", "gap_count",
    "gap_detected_at", "gap_watermark_received_at", "gap_watermark_event_timestamp",
    "gap_watermark", "consecutive_failures", "error_category",
    "catalog_status", "summary_status", "ws_connection_status", "lifecycle_status",
    "markprice_status", "ticker_status", "context_status", "last_rest_success", "last_ws_success",
})
SAFE_HEALTH_ENUM_KEYS = frozenset({
    "phase4_status", "phase5_status", "phase6_status", "phase6_ai_status", "phase7_status",
    "phase8_status", "reason", "reason_code", "failure_stage", "provider_status",
    "source_status", "runtime_state", "runtime_stage", "lifecycle", "owner", "progress_source", "chain",
    "data_quality", "gap_reason", "error_category", "provider", "endpoint", "error_code",
    "error_type", "schema_stage", "schema_error_summary", "last_error_category",
    "last_error_type", "failure_type", "catalog_status", "summary_status",
    "ws_connection_status", "lifecycle_status", "markprice_status", "ticker_status", "context_status",
})
SAFE_HEALTH_TIMESTAMP_KEYS = frozenset({
    "last_success_at", "last_fetch_at", "last_persisted_at", "context_timestamp", "fetched_at",
    "source_timestamp", "exchange_timestamp", "checked_at", "observed_at", "expected_latest_closed_at",
    "stale_since", "last_attempt_at", "last_error_at",
    "processed_at", "last_exchange_at", "last_fetched_at", "last_processed_at", "gap_detected_at",
    "gap_watermark_received_at", "gap_watermark_event_timestamp", "last_rest_success", "last_ws_success",
})
PHASE_TABLE_PREFIXES = {
    1: ("market_snapshots", "klines", "screening_runs", "screening_results", "universe_runs", "universe_members"),
    2: ("open_interest", "funding_rates", "cross_exchange_derivative_snapshots", "stage1_derivative_enrichment"),
    3: ("trade_flow_windows", "cvd_snapshots", "cross_exchange_flow_snapshots", "stage1_flow_enrichment"),
    4: ("liquidation_events", "liquidation_windows", "long_short_observations", "basis_snapshots", "cross_exchange_phase4_snapshots", "stage1_phase4_enrichment"),
    5: ("phase5_", "stage1_phase5_context_enrichment"),
    6: ("phase6_source_registry", "phase6_news_events", "phase6_macro_events", "phase6_unlock_events"),
    7: ("phase7_", "stage1_phase7_context_enrichment"),
    8: ("phase8_",),
}
PHASE7_SOURCE_COMPONENTS = frozenset({
    "bitcoin-rpc", "ethereum-rpc", "binance-spot", "binance-spot-websocket",
})
PHASE7_CONSECUTIVE_FAILURE_ERROR_THRESHOLD = 3
PHASE8_WORKER_NAMES = frozenset({"instruments", "chain", "markprice", "ticker", "context", "health", "retention"})
PHASE8_WORKER_FIELDS = frozenset({
    "status", "interval_seconds", "started_at", "last_attempt_at", "last_success_at",
    "last_error_at", "consecutive_failures", "reason", "error_type",
})


class AcceptanceError(RuntimeError):
    """A bounded, safe-to-report acceptance failure code."""


class RunDeadline:
    def __init__(
        self,
        *,
        duration_seconds: int = ACCEPTANCE_DURATION_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if duration_seconds <= 0:
            raise ValueError("runtime duration must be positive")
        self.duration_seconds = duration_seconds
        self._clock = clock
        self._started = clock()
        self._deadline = self._started + self.duration_seconds

    def remaining_seconds(self) -> float:
        return max(0.0, self._deadline - self._clock())

    @property
    def elapsed_seconds(self) -> float:
        return min(self.duration_seconds, max(0.0, self._clock() - self._started))


def scheduled_sample_offsets(duration_seconds: int = ACCEPTANCE_DURATION_SECONDS) -> tuple[int, ...]:
    # The final boundary is shutdown, not permission to start another sample.
    return tuple(range(0, duration_seconds, ACCEPTANCE_SAMPLE_INTERVAL_SECONDS))


def run_sampling_window(
    sample: Callable[[int, int], None],
    *,
    hard_stop: Callable[[], str | None],
    deadline: RunDeadline | None = None,
    sleep: Callable[[float], None] = time.sleep,
    stop_requested: Callable[[], bool] = lambda: False,
) -> dict[str, Any]:
    """Run sequential samples on a fixed monotonic schedule without catch-up bursts."""
    active_deadline = deadline or RunDeadline()
    next_offset = 0
    sample_count = 0
    missed_intervals = 0
    while True:
        if stop_requested():
            return {
                "status": "INTERRUPTED",
                "sample_count": sample_count,
                "missed_intervals": missed_intervals,
                "elapsed_seconds": round(active_deadline.elapsed_seconds, 3),
                "stop_reason": "INTERRUPT_REQUESTED",
            }
        remaining = active_deadline.remaining_seconds()
        if remaining <= 0:
            return {
                "status": "COMPLETED",
                "sample_count": sample_count,
                "missed_intervals": missed_intervals,
                "elapsed_seconds": round(active_deadline.elapsed_seconds, 3),
                "stop_reason": None,
            }
        target_offset = min(next_offset, active_deadline.duration_seconds)
        until_target = target_offset - active_deadline.elapsed_seconds
        if until_target > 0:
            sleep(min(until_target, remaining))
            continue
        if target_offset >= active_deadline.duration_seconds:
            sleep(remaining)
            continue
        sample_count += 1
        sample(sample_count, target_offset)
        hard_stop_reason = hard_stop()
        if hard_stop_reason:
            return {
                "status": "HARD_STOP",
                "sample_count": sample_count,
                "missed_intervals": missed_intervals,
                "elapsed_seconds": round(active_deadline.elapsed_seconds, 3),
                "stop_reason": hard_stop_reason,
            }
        next_offset = target_offset + ACCEPTANCE_SAMPLE_INTERVAL_SECONDS
        elapsed = active_deadline.elapsed_seconds
        if elapsed > next_offset:
            skipped = math.ceil((elapsed - next_offset) / ACCEPTANCE_SAMPLE_INTERVAL_SECONDS)
            missed_intervals += skipped
            next_offset += skipped * ACCEPTANCE_SAMPLE_INTERVAL_SECONDS


def _numeric_cursor(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text, 10)
    except ValueError:
        return None


def find_numeric_cursor_rollbacks(
    previous: Mapping[str, Any], current: Mapping[str, Any]
) -> list[str]:
    """Return opaque identities for comparable numeric cursors that decreased."""
    rolled_back = []
    for identity, old_value in previous.items():
        if identity not in current:
            continue
        old_number = _numeric_cursor(old_value)
        new_number = _numeric_cursor(current[identity])
        if old_number is not None and new_number is not None and new_number < old_number:
            rolled_back.append(hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12])
    return sorted(rolled_back)


def evaluate_hard_stop(
    *,
    disk_free_ratio: float | None,
    previous_memory_events: Mapping[str, Mapping[str, int]] | None,
    current_memory_events: Mapping[str, Mapping[str, int]],
    cursor_rollback_ids: Iterable[str] = (),
    secret_leak_found: bool = False,
) -> str | None:
    if secret_leak_found:
        return "SECRET_LEAK_DETECTED"
    if disk_free_ratio is not None and disk_free_ratio < MIN_DISK_FREE_RATIO:
        return "DISK_FREE_BELOW_15_PERCENT"
    if tuple(cursor_rollback_ids):
        return "NUMERIC_CURSOR_ROLLBACK"
    if previous_memory_events is not None:
        for service, current in current_memory_events.items():
            previous = previous_memory_events.get(service, {})
            if any(current.get(key, 0) > previous.get(key, 0) for key in ("oom", "oom_kill")):
                return "OOM_EVENT_DELTA"
    return None


def parse_cgroup_resource_sample(output: str) -> dict[str, int | None]:
    values: dict[str, int] = {}
    for line in output.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit():
            values[parts[0].rstrip(":")] = int(parts[1])
    required = {
        "memory_current", "memory_peak", "memory_limit", "event_max", "event_oom",
        "event_oom_kill", "pids_current", "proc_VmRSS", "proc_VmSize", "proc_Threads",
    }
    if not required.issubset(values):
        raise ValueError("CGROUP_METRICS_INCOMPLETE")
    return {
        "memory_current_bytes": values["memory_current"],
        "memory_peak_bytes": values["memory_peak"],
        "memory_limit_bytes": values["memory_limit"],
        "memory_events_max": values["event_max"],
        "memory_events_oom": values["event_oom"],
        "memory_events_oom_kill": values["event_oom_kill"],
        "pids_current": values["pids_current"],
        "process_rss_bytes": values["proc_VmRSS"] * 1024,
        "process_vmsize_bytes": values["proc_VmSize"] * 1024,
        "process_threads": values["proc_Threads"],
        "process_pss_bytes": values.get("smaps_Pss", 0) * 1024 if "smaps_Pss" in values else None,
        "cpu_usage_usec": values.get("cpu_usage_usec"),
        "cpu_throttled_usec": values.get("cpu_throttled_usec"),
        "cpu_nr_throttled": values.get("cpu_nr_throttled"),
    }


def parse_host_cpu_counters(line: str) -> tuple[int, int]:
    parts = line.split()
    if not parts or parts[0] != "cpu" or len(parts) < 5:
        raise ValueError("HOST_CPU_METRICS_INVALID")
    counters = [int(value) for value in parts[1:]]
    total = sum(counters)
    idle = counters[3] + (counters[4] if len(counters) > 4 else 0)
    return total, idle


class HostCpuSampler:
    def __init__(self, reader: Callable[[], str] | None = None) -> None:
        self._reader = reader or (lambda: Path("/proc/stat").read_text(encoding="ascii").splitlines()[0])
        self._previous: tuple[int, int] | None = None

    def sample_percent(self) -> float | None:
        current = parse_host_cpu_counters(self._reader())
        previous = self._previous
        self._previous = current
        if previous is None:
            return None
        total_delta = current[0] - previous[0]
        idle_delta = current[1] - previous[1]
        if total_delta <= 0 or idle_delta < 0 or idle_delta > total_delta:
            return None
        return round(100 * (total_delta - idle_delta) / total_delta, 2)


def phase_table_activity(
    current_stats: Mapping[str, Mapping[str, int]],
    baseline_stats: Mapping[str, Mapping[str, int]],
) -> dict[int, int]:
    activity: dict[int, int] = {}
    for phase, table_prefixes in PHASE_TABLE_PREFIXES.items():
        phase_total = 0
        for table, current in current_stats.items():
            if not any(table == prefix or table.startswith(prefix) for prefix in table_prefixes):
                continue
            previous = baseline_stats.get(table, {})
            phase_total += sum(
                max(0, int(current.get(counter, 0)) - int(previous.get(counter, 0)))
                for counter in ("n_tup_ins", "n_tup_upd", "n_tup_del")
            )
        activity[phase] = phase_total
    return activity


def derive_phase_statuses(
    *,
    health: Mapping[str, Mapping[str, Any]],
    phase_activity: Mapping[int, int],
    table_stats: Mapping[str, Mapping[str, int]],
) -> dict[str, str]:
    statuses: dict[str, str] = {}
    rank = {"ERROR": 5, "STALE": 4, "NOT_AVAILABLE": 3, "PARTIAL": 2, "AVAILABLE": 1}
    for phase, prefixes in PHASE_TABLE_PREFIXES.items():
        table_present = any(
            any(name == prefix or name.startswith(prefix) for prefix in prefixes)
            for name in table_stats
        )
        phase_components: list[str] = []
        phase7_available_transport = False
        phase7_error_evidence: list[bool] = []
        for component, row in health.items():
            name = component.lower().replace("_", "-")
            if phase == 1:
                matches = name in {"quant-collector", "quant-engine"}
            elif phase == 7:
                matches = "phase7" in name or name in PHASE7_SOURCE_COMPONENTS
            else:
                matches = f"phase{phase}" in name
            if phase == 6 and ("ai" in name or "gateway" in name or "persistence" in name):
                matches = False
            if not matches:
                continue
            details = row.get("details", {})
            detail_status = details.get(f"phase{phase}_status") if isinstance(details, Mapping) else None
            observed = str(detail_status or row.get("status", "NOT_EXPOSED")).upper()
            if phase == 8 and isinstance(details, Mapping):
                if details.get("configured") is False:
                    observed = "NOT_CONFIGURED"
                elif details.get("configured") is True and detail_status is None:
                    observed = "UNPROVEN"
            if (
                phase == 6
                and isinstance(details, Mapping)
                and details.get("reason_code") == "SOURCE_NOT_CONFIGURED"
                and details.get("configured") is False
                and details.get("source_count") == 0
            ):
                observed = "NOT_CONFIGURED"
            if phase == 4 and ("long-short" in name or "long_short" in name) and isinstance(details, Mapping):
                category = str(details.get("error_category", "")).upper()
                http_status = details.get("http_status")
                transient = category in {"NETWORK_ERROR", "TIMEOUT", "RATE_LIMIT"} or (
                    category == "HTTP_ERROR" and isinstance(http_status, int) and http_status >= 500
                )
                if category and not transient:
                    observed = "ERROR"
                elif transient and not (
                    isinstance(details.get("usable_source_count"), int)
                    and details["usable_source_count"] > 0
                ):
                    observed = "ERROR"
            if phase == 7 and name in PHASE7_SOURCE_COMPONENTS:
                if observed == "AVAILABLE":
                    phase7_available_transport = True
                elif observed == "ERROR":
                    error_category = str(details.get("error_category", "")).upper() if isinstance(details, Mapping) else ""
                    data_quality = str(details.get("data_quality", "")).upper() if isinstance(details, Mapping) else ""
                    reason = details.get("reason") if isinstance(details, Mapping) else None
                    phase7_error_evidence.append(
                        error_category in {"NETWORK", "RATE_LIMIT"}
                        and data_quality == "PARTIAL"
                        and isinstance(reason, str)
                        and bool(reason.strip())
                        and isinstance(details.get("consecutive_failures"), int)
                        and details["consecutive_failures"] >= PHASE7_CONSECUTIVE_FAILURE_ERROR_THRESHOLD
                    )
            age_seconds = row.get("age_seconds")
            max_age = PHASE6_HEALTH_MAX_AGE_SECONDS if phase == 6 else HEALTH_MAX_AGE_SECONDS
            phase_data_status_is_authoritative = phase in {5, 6} and detail_status is not None
            if (
                isinstance(age_seconds, (int, float))
                and age_seconds > max_age
                and observed not in {"ERROR", "NOT_CONFIGURED"}
                and not phase_data_status_is_authoritative
            ):
                observed = "STALE"
            phase_components.append(observed)
        if "ERROR" in phase_components:
            if (
                phase == 7
                and phase7_available_transport
                and phase7_error_evidence
                and all(phase7_error_evidence)
            ):
                statuses[f"phase{phase}"] = "PARTIAL"
            else:
                statuses[f"phase{phase}"] = "ERROR"
        elif "STALE" in phase_components:
            statuses[f"phase{phase}"] = "STALE"
        elif phase == 6 and phase_components and all(item == "NOT_CONFIGURED" for item in phase_components):
            statuses[f"phase{phase}"] = "NOT_CONFIGURED"
        elif phase == 8 and "UNPROVEN" in phase_components:
            statuses[f"phase{phase}"] = "UNPROVEN"
        elif phase == 8 and phase_components and all(item == "NOT_CONFIGURED" for item in phase_components):
            statuses[f"phase{phase}"] = "NOT_CONFIGURED"
        elif phase == 8 and not phase_components:
            statuses[f"phase{phase}"] = "UNPROVEN"
        elif phase_activity.get(phase, 0) > 0 or "AVAILABLE" in phase_components:
            statuses[f"phase{phase}"] = (
                "PARTIAL" if any(item in {"NOT_AVAILABLE", "PARTIAL"} for item in phase_components) else "AVAILABLE"
            )
        elif "PARTIAL" in phase_components:
            statuses[f"phase{phase}"] = "PARTIAL"
        elif phase_components:
            statuses[f"phase{phase}"] = max(phase_components, key=lambda item: rank.get(item, 0))
        elif phase == 8:
            statuses[f"phase{phase}"] = "UNPROVEN"
        elif table_present:
            statuses[f"phase{phase}"] = "NOT_AVAILABLE"
        else:
            statuses[f"phase{phase}"] = "NOT_EXPOSED"
    ai_components = []
    ai_provider_configuration: list[str] = []
    for name, row in health.items():
        normalized_name = name.lower().replace("_", "-")
        if "phase6" not in normalized_name or not ("ai" in normalized_name or "gateway" in normalized_name):
            continue
        details = row.get("details", {})
        status = str(details.get("phase6_ai_status", row.get("status", "NOT_EXPOSED"))).upper()
        provider_status = str(details.get("provider_status", "")).upper()
        if provider_status in {"CONFIGURED", "NOT_CONFIGURED"}:
            ai_provider_configuration.append(provider_status)
        age_seconds = row.get("age_seconds")
        if isinstance(age_seconds, (int, float)) and age_seconds > PHASE6_HEALTH_MAX_AGE_SECONDS and status not in {"ERROR", "NOT_CONFIGURED"}:
            status = "STALE"
        ai_components.append(status)
    if ai_provider_configuration and all(value == "NOT_CONFIGURED" for value in ai_provider_configuration):
        statuses["phase6_ai"] = "NOT_CONFIGURED"
    elif ai_components:
        statuses["phase6_ai"] = max(ai_components, key=lambda item: rank.get(item, 0))
    else:
        statuses["phase6_ai"] = "NOT_EXPOSED"
    return statuses


def derive_phase_degradation_evidence(
    health: Mapping[str, Mapping[str, Any]],
) -> dict[str, bool]:
    """Require explicit, safe diagnostics before a degraded phase can pass."""
    evidence: dict[str, bool] = {}
    for phase in range(2, 9):
        phase_name = f"phase{phase}"
        degraded_rows = []
        for component, row in health.items():
            name = component.lower().replace("_", "-")
            if phase_name not in name and not (phase == 7 and name in PHASE7_SOURCE_COMPONENTS):
                continue
            details = row.get("details", {})
            if not isinstance(details, Mapping):
                continue
            detail_status = str(details.get(f"{phase_name}_status", "")).upper()
            data_quality = str(details.get("data_quality", "")).upper()
            if detail_status == "PARTIAL" or data_quality == "PARTIAL":
                reason = details.get("reason") or details.get("reason_code")
                verified = data_quality == "PARTIAL" and isinstance(reason, str) and bool(reason.strip())
                if phase == 4:
                    if name.endswith("phase4-liquidation"):
                        verified = (
                            verified
                            and details.get("gap_detected") is True
                            and isinstance(details.get("gap_watermark_received_at"), str)
                            and isinstance(details.get("gap_watermark_event_timestamp"), str)
                            and str(details.get("gap_reason", "")).startswith("LIQUIDATION_GAP")
                        )
                    elif "long-short" in name or "long_short" in name:
                        category = str(details.get("error_category", "")).upper()
                        http_status = details.get("http_status")
                        transient = category in {"NETWORK_ERROR", "TIMEOUT", "RATE_LIMIT"} or (
                            category == "HTTP_ERROR" and isinstance(http_status, int) and http_status >= 500
                        )
                        verified = (
                            verified
                            and transient
                            and isinstance(details.get("usable_source_count"), int)
                            and details["usable_source_count"] > 0
                            and isinstance(details.get("provider"), str)
                            and isinstance(details.get("endpoint"), str)
                        )
                    else:
                        verified = False
                elif phase == 7:
                    failures = details.get("consecutive_failures")
                    if detail_status == "ERROR":
                        verified = (
                            verified
                            and str(details.get("error_category", "")).upper() in {"NETWORK", "RATE_LIMIT"}
                            and isinstance(failures, int)
                            and failures >= PHASE7_CONSECUTIVE_FAILURE_ERROR_THRESHOLD
                        )
                elif phase == 5:
                    verified = (
                        verified
                        and isinstance(details.get("partial_output_count"), int)
                        and details["partial_output_count"] > 0
                        and isinstance(details.get("missing_evidence_count"), int)
                        and details["missing_evidence_count"] > 0
                    )
                degraded_rows.append(verified)
        evidence[phase_name] = bool(degraded_rows) and all(degraded_rows)
    ai_degraded_rows = []
    for component, row in health.items():
        name = component.lower().replace("_", "-")
        if "phase6" not in name or not ("ai" in name or "gateway" in name):
            continue
        details = row.get("details", {})
        if not isinstance(details, Mapping):
            continue
        detail_status = str(details.get("phase6_ai_status", "")).upper()
        data_quality = str(details.get("data_quality", "")).upper()
        if detail_status == "PARTIAL" or data_quality == "PARTIAL":
            reason = details.get("reason") or details.get("reason_code")
            ai_degraded_rows.append(data_quality == "PARTIAL" and isinstance(reason, str) and bool(reason.strip()))
    evidence["phase6_ai"] = bool(ai_degraded_rows) and all(ai_degraded_rows)
    return evidence


def derive_optional_source_configuration(
    health: Mapping[str, Mapping[str, Any]],
) -> dict[str, bool | None]:
    """Return configuration evidence; None means the health contract is absent/ambiguous."""
    phase6_rows: list[Mapping[str, Any]] = []
    phase6_kinds: set[str] = set()
    ai_rows: list[Mapping[str, Any]] = []
    phase8_rows: list[Mapping[str, Any]] = []
    for component, row in health.items():
        name = component.lower().replace("_", "-")
        if "phase8" in name:
            details = row.get("details", {})
            if isinstance(details, Mapping):
                phase8_rows.append(details)
        if "phase6" not in name:
            continue
        details = row.get("details", {})
        if not isinstance(details, Mapping):
            continue
        for kind in ("news", "macro", "unlock"):
            if f"phase6-{kind}-ingestion" in name:
                phase6_rows.append(details)
                phase6_kinds.add(kind)
        if "ai" in name or "gateway" in name:
            ai_rows.append(details)

    phase6_config: bool | None = None
    if phase6_rows:
        explicit_false = all(
            row.get("configured") is False
            and row.get("source_count") == 0
            and row.get("reason_code") == "SOURCE_NOT_CONFIGURED"
            for row in phase6_rows
        ) and phase6_kinds == {"news", "macro", "unlock"}
        if explicit_false:
            phase6_config = False
        elif any(
            row.get("configured") is True
            or (isinstance(row.get("source_count"), int) and row.get("source_count", 0) > 0)
            for row in phase6_rows
        ):
            phase6_config = True

    ai_config: bool | None = None
    if ai_rows:
        provider_states = [
            str(row.get("provider_status", "")).upper()
            for row in ai_rows
            if str(row.get("provider_status", "")).upper() in {"CONFIGURED", "NOT_CONFIGURED"}
        ]
        if provider_states and all(state == "NOT_CONFIGURED" for state in provider_states):
            ai_config = False
        elif any(state == "CONFIGURED" for state in provider_states):
            ai_config = True
        elif any(row.get("configured") is False for row in ai_rows):
            ai_config = False
        elif any(row.get("configured") is True for row in ai_rows):
            ai_config = True
    phase8_config: bool | None = None
    explicit_phase8 = [row.get("configured") for row in phase8_rows if isinstance(row.get("configured"), bool)]
    if explicit_phase8 and all(value is explicit_phase8[0] for value in explicit_phase8):
        phase8_config = explicit_phase8[0]
    return {"phase6": phase6_config, "phase6_ai": ai_config, "phase8": phase8_config}


def safe_health_details(details: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only reviewed, scalar health diagnostics; never copy URLs or payloads."""
    allowed = APPLICATION_METRIC_KEYS | SAFE_HEALTH_STATE_KEYS
    safe: dict[str, Any] = {}
    for key, value in details.items():
        if key not in allowed or value is None:
            continue
        if key == "worker_health" and isinstance(value, Mapping):
            workers: dict[str, dict[str, Any]] = {}
            for worker_name, worker_details in value.items():
                if worker_name not in PHASE8_WORKER_NAMES or not isinstance(worker_details, Mapping):
                    continue
                safe_worker: dict[str, Any] = {}
                for worker_key, worker_value in worker_details.items():
                    if worker_key not in PHASE8_WORKER_FIELDS or worker_value is None:
                        continue
                    if worker_key in {"started_at", "last_attempt_at", "last_success_at", "last_error_at"}:
                        if not isinstance(worker_value, str):
                            continue
                        try:
                            parsed = datetime.fromisoformat(worker_value.replace("Z", "+00:00"))
                        except ValueError:
                            continue
                        if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
                            continue
                        safe_worker[worker_key] = parsed.astimezone(timezone.utc).isoformat()
                    elif worker_key in {"status", "reason", "error_type"}:
                        if isinstance(worker_value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", worker_value):
                            safe_worker[worker_key] = worker_value
                    elif isinstance(worker_value, bool):
                        continue
                    elif isinstance(worker_value, int) and worker_value >= 0:
                        safe_worker[worker_key] = worker_value
                if safe_worker:
                    workers[worker_name] = safe_worker
            if workers:
                safe[key] = workers
        elif key in SAFE_HEALTH_TIMESTAMP_KEYS:
            if not isinstance(value, str):
                continue
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                continue
            if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
                continue
            safe[key] = parsed.astimezone(timezone.utc).isoformat()
        elif key in SAFE_HEALTH_ENUM_KEYS:
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", value):
                safe[key] = value
        elif isinstance(value, bool):
            safe[key] = value
        elif isinstance(value, (int, float)) and math.isfinite(float(value)):
            safe[key] = value
    return safe


def build_acceptance_gate_results(
    *,
    window_status: str,
    missed_intervals: int,
    migrations_match: bool,
    database_identity_utc: bool,
    services_stable: bool,
    core_health_fresh: bool,
    phase1_market_activity: bool,
    phase1_stage1_activity: bool,
    resource_measurements_complete: bool,
    memory_caps_respected: bool,
    cursor_observability_complete: bool,
    phase_statuses: Mapping[str, str],
    minimum_disk_free_ratio: float | None,
    hard_stop_reason: str | None,
    logs_audited: bool,
    secret_leak_found: bool,
    clean_shutdown: bool,
    phase_degradation_evidence: Mapping[str, bool] | None = None,
    optional_source_configuration: Mapping[str, bool | None] | None = None,
) -> list[dict[str, str]]:
    gates = [
        ("fixed_15_minute_window", window_status == "COMPLETED", "WINDOW_NOT_COMPLETED"),
        ("10_second_sampling", missed_intervals == 0, "MISSED_SAMPLE_INTERVALS"),
        ("migrations", migrations_match, "MIGRATION_SET_MISMATCH"),
        ("database_quant_utc", database_identity_utc, "DATABASE_IDENTITY_OR_TIMEZONE_MISMATCH"),
        ("services_stable", services_stable, "SERVICE_RESTART_OR_EXIT"),
        ("core_runtime_health", core_health_fresh, "CORE_HEALTH_STALE_OR_MISSING"),
        ("phase1_market_persistence", phase1_market_activity, "NO_NEW_MARKET_DATA"),
        ("stage1_persistence", phase1_stage1_activity, "NO_NEW_STAGE1_RESULTS"),
        ("resource_measurements", resource_measurements_complete, "RESOURCE_METRICS_NOT_EXPOSED"),
        ("memory_caps", memory_caps_respected, "MEMORY_CAP_EXCEEDED"),
        ("cursor_observability", cursor_observability_complete, "CURSOR_SET_TRUNCATED"),
        ("disk_headroom", minimum_disk_free_ratio is not None and minimum_disk_free_ratio >= MIN_DISK_FREE_RATIO, "DISK_HEADROOM_NOT_PROVEN"),
        ("hard_safety", hard_stop_reason is None, hard_stop_reason or "HARD_STOP"),
        ("log_audit", logs_audited, "LOG_AUDIT_NOT_EXPOSED"),
        ("secret_audit", not secret_leak_found, "SECRET_LEAK_DETECTED"),
        ("clean_shutdown", clean_shutdown, "SHUTDOWN_NOT_CLEAN"),
    ]
    results = [
        {"gate": name, "status": "PASS" if passed else "FAIL", "reason": "NONE" if passed else reason}
        for name, passed, reason in gates
    ]
    for phase_name in (f"phase{phase}" for phase in range(2, 9)):
        observed = phase_statuses.get(phase_name, "NOT_EXPOSED")
        if observed == "AVAILABLE":
            status, reason = "PASS", "NONE"
        elif observed == "PARTIAL":
            verified = bool((phase_degradation_evidence or {}).get(phase_name))
            status, reason = ("PASS_WITH_DEGRADATION", "DEGRADATION_EVIDENCE_VERIFIED") if verified else ("FAIL", "DEGRADATION_EVIDENCE_NOT_EXPOSED")
        elif observed == "NOT_CONFIGURED" and phase_name == "phase6" and (optional_source_configuration or {}).get("phase6") is False:
            status, reason = "PASS", "OPTIONAL_SOURCE_NOT_CONFIGURED"
        elif observed == "NOT_CONFIGURED" and phase_name == "phase8" and (optional_source_configuration or {}).get("phase8") is False:
            status, reason = "PASS", "OPTIONAL_PHASE_NOT_CONFIGURED"
        elif observed == "NOT_EXPOSED":
            status, reason = "NOT_EXPOSED", "PHASE_STATUS_NOT_OBSERVED"
        else:
            status, reason = "FAIL", observed
        results.append({"gate": phase_name, "status": status, "reason": reason})
    ai_status = phase_statuses.get("phase6_ai", "NOT_CONFIGURED")
    ai_configuration = (optional_source_configuration or {}).get("phase6_ai")
    if ai_status == "NOT_CONFIGURED":
        ai_gate_status = "PASS" if ai_configuration is False else "FAIL"
    elif ai_status == "AVAILABLE":
        ai_gate_status = "PASS"
    elif ai_status == "PARTIAL":
        ai_gate_status = "PASS_WITH_DEGRADATION" if (phase_degradation_evidence or {}).get("phase6_ai") else "FAIL"
    else:
        ai_gate_status = "FAIL"
    results.append({
        "gate": "phase6_ai_provider",
        "status": ai_gate_status,
        "reason": (
            "OPTIONAL_PROVIDER_NOT_CONFIGURED" if ai_status == "NOT_CONFIGURED" and ai_gate_status == "PASS"
            else "DEGRADATION_EVIDENCE_VERIFIED" if ai_status == "PARTIAL" and ai_gate_status == "PASS_WITH_DEGRADATION"
            else "NONE" if ai_gate_status == "PASS" else "DEGRADATION_EVIDENCE_NOT_EXPOSED" if ai_status == "PARTIAL" else ai_status
        ),
    })
    return results


def acceptance_gates_pass(gates: Iterable[Mapping[str, str]]) -> bool:
    return all(gate.get("status") in {"PASS", "PASS_WITH_DEGRADATION", "SKIPPED"} for gate in gates)


def _run_bounded_process(
    args: list[str],
    *,
    timeout_seconds: float = 10,
    max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
    env: Mapping[str, str] | None = None,
    merge_stderr: bool = False,
) -> str:
    try:
        result = subprocess.run(
            args,
            cwd=ROOT,
            env=dict(env) if env is not None else _compose_child_environment({}),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise AcceptanceError("COMMAND_TIMEOUT") from None
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    if len(stdout.encode("utf-8")) + len(stderr.encode("utf-8")) > max_output_bytes:
        raise AcceptanceError("COMMAND_OUTPUT_LIMIT_EXCEEDED")
    if result.returncode:
        raise AcceptanceError("COMMAND_FAILED")
    return stdout.strip()


def _docker_executable() -> str:
    docker = shutil.which("docker")
    if not docker:
        raise AcceptanceError("DOCKER_UNAVAILABLE")
    return docker


def _docker_output(
    args: list[str],
    *,
    timeout_seconds: float = 8,
    max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
    merge_stderr: bool = False,
) -> str:
    return _run_bounded_process(
        [_docker_executable(), *args],
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
        merge_stderr=merge_stderr,
    )


def _container_resource_sample(container_id: str) -> dict[str, int | None]:
    output = _docker_output(
        ["exec", container_id, "sh", "-c", CGROUP_READ],
        timeout_seconds=5,
        max_output_bytes=16 * 1024,
    )
    try:
        return parse_cgroup_resource_sample(output)
    except ValueError:
        raise AcceptanceError("CGROUP_METRICS_INCOMPLETE") from None


def _container_state(container_id: str) -> dict[str, Any]:
    raw = _docker_output(["inspect", "--format", "{{json .State}}|{{.RestartCount}}", container_id], max_output_bytes=16 * 1024)
    state_text, separator, restart_count = raw.partition("|")
    if not separator or not restart_count.isdigit():
        raise AcceptanceError("CONTAINER_STATE_UNREADABLE")
    try:
        state = json.loads(state_text)
    except json.JSONDecodeError:
        raise AcceptanceError("CONTAINER_STATE_UNREADABLE") from None
    if not isinstance(state, dict):
        raise AcceptanceError("CONTAINER_STATE_UNREADABLE")
    health = state.get("Health") or {}
    return {
        "status": state.get("Status"),
        "health": health.get("Status") if isinstance(health, Mapping) else None,
        "oom_killed": bool(state.get("OOMKilled", False)),
        "exit_code": state.get("ExitCode"),
        "restart_count": int(restart_count),
    }


def _container_log_sample(container_id: str, secret_values: Iterable[str]) -> dict[str, Any]:
    try:
        output = _docker_output(
            ["logs", "--tail", str(MAX_LOG_TAIL_LINES), container_id],
            timeout_seconds=LOG_COMMAND_TIMEOUT_SECONDS,
            max_output_bytes=MAX_LOG_SAMPLE_BYTES,
            merge_stderr=True,
        )
    except AcceptanceError:
        return {"status": "NOT_EXPOSED", "bytes": None, "sampled_tail_bytes": 0, "secret_leak_found": False}
    tail = output.encode("utf-8")[-MAX_LOG_SAMPLE_BYTES:]
    lowered = tail.lower()
    leak = any(value.encode("utf-8") in tail for value in secret_values if value)
    leak = leak or b"authorization:" in lowered or b"api-key:" in lowered
    return {
        "status": "AVAILABLE",
        "bytes": len(tail),
        "sampled_tail_bytes": len(tail),
        "secret_leak_found": bool(leak),
    }


def _db_connect(host: str):
    return psycopg.connect(
        host=host,
        port=5432,
        user="quant",
        dbname="quant",
        connect_timeout=2,
        autocommit=True,
        options="-c default_transaction_read_only=on -c statement_timeout=500 -c lock_timeout=250",
    )


def _database_snapshot(
    postgres_container_id: str,
    *,
    include_stage1_detail: bool = False,
) -> dict[str, Any]:
    host = _docker_output(
        ["inspect", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", postgres_container_id],
        max_output_bytes=1024,
    )
    if not host or not all(char.isdigit() or char == "." for char in host):
        raise AcceptanceError("ISOLATED_DATABASE_ADDRESS_UNAVAILABLE")
    try:
        connection = _db_connect(host)
    except Exception:
        raise AcceptanceError("DATABASE_CONNECTION_FAILED") from None
    with connection:
        current_database, timezone_name, database_bytes, commits, rollbacks, tuples_in, tuples_out = connection.execute(
            """
            SELECT current_database(), current_setting('TimeZone'), pg_database_size(current_database()),
                   xact_commit, xact_rollback, tup_inserted, tup_returned
            FROM pg_stat_database WHERE datname = current_database()
            """
        ).fetchone()
        migration_rows = connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version LIMIT 128"
        ).fetchall()
        allowed_keys = sorted(APPLICATION_METRIC_KEYS | SAFE_HEALTH_STATE_KEYS)
        health_rows = connection.execute(
            """
            SELECT component, status, checked_at,
                   COALESCE((
                       SELECT jsonb_object_agg(item.key, item.value)
                       FROM jsonb_each(system_health.details) AS item(key, value)
                       WHERE item.key = ANY(%s)
                         AND (
                             jsonb_typeof(item.value) IN ('string', 'number', 'boolean', 'null')
                             OR (item.key = 'worker_health' AND jsonb_typeof(item.value) = 'object')
                         )
                   ), '{}'::jsonb)
            FROM system_health ORDER BY component LIMIT 256
            """,
            (allowed_keys,),
        ).fetchall()
        table_rows = connection.execute(
            """
            SELECT relname, n_live_tup, n_tup_ins, n_tup_upd, n_tup_del, n_dead_tup
            FROM pg_stat_user_tables WHERE schemaname = 'public'
            ORDER BY relname LIMIT 256
            """
        ).fetchall()
        active_transactions, idle_transactions = connection.execute(
            """
            SELECT count(*) FILTER (WHERE state = 'active'),
                   count(*) FILTER (WHERE state LIKE 'idle in transaction%')
            FROM pg_stat_activity
            WHERE datname = current_database() AND pid <> pg_backend_pid()
            """
        ).fetchone()
        checkpoints = connection.execute(
            """
            SELECT source_id, scope_kind, scope_key, cursor_kind, cursor_value, status,
                   count(*) OVER () AS total_count
            FROM phase7_ingestion_checkpoints ORDER BY source_id, scope_kind, scope_key LIMIT 512
            """
        ).fetchall()
        stage1_categories: dict[str, int] | None = None
        if include_stage1_detail:
            latest_run = connection.execute("SELECT max(id) FROM screening_runs").fetchone()[0]
            if latest_run is not None:
                stage1_categories = {
                    str(category): int(count)
                    for category, count in connection.execute(
                        "SELECT category, count(*) FROM screening_results WHERE run_id = %s GROUP BY category",
                        (latest_run,),
                    ).fetchall()
                }
    now = datetime.now(timezone.utc)
    health: dict[str, Any] = {}
    numeric_cursors: dict[str, str] = {}
    for component, status, checked_at, details in health_rows:
        age = max(0.0, (now - checked_at.astimezone(timezone.utc)).total_seconds())
        safe_details = safe_health_details(details or {})
        health[str(component)] = {
            "status": str(status),
            "checked_at_utc": checked_at.astimezone(timezone.utc).isoformat(),
            "age_seconds": round(age, 3),
            "details": safe_details,
        }
    checkpoint_total = max((int(row[6]) for row in checkpoints), default=0)
    for source_id, scope_kind, scope_key, cursor_kind, cursor_value, _status, _total in checkpoints:
        if _numeric_cursor(cursor_value) is not None:
            identity = f"{source_id}:{scope_kind}:{scope_key}:{cursor_kind}"
            numeric_cursors[identity] = str(cursor_value)
    checkpoint_statuses: dict[str, int] = {}
    for row in checkpoints:
        status = row[5]
        checkpoint_statuses[str(status)] = checkpoint_statuses.get(str(status), 0) + 1
    table_stats = {
        str(name): {
            "estimated_rows": int(live_rows or 0),
            "n_tup_ins": int(inserted or 0),
            "n_tup_upd": int(updated or 0),
            "n_tup_del": int(deleted or 0),
            "n_dead_tup": int(dead_rows or 0),
        }
        for name, live_rows, inserted, updated, deleted, dead_rows in table_rows
    }
    return {
        "database": {
            "name": str(current_database),
            "timezone": str(timezone_name),
            "size_bytes": int(database_bytes),
            "commits": int(commits or 0),
            "rollbacks": int(rollbacks or 0),
            "tuples_inserted": int(tuples_in or 0),
            "tuples_returned": int(tuples_out or 0),
            "active_transactions": int(active_transactions or 0),
            "idle_in_transaction": int(idle_transactions or 0),
        },
        "migrations": [str(row[0]) for row in migration_rows],
        "health": health,
        "table_stats": table_stats,
        "checkpoints": {
            "count": checkpoint_total,
            "sampled_count": len(checkpoints),
            "truncated": checkpoint_total > len(checkpoints),
            "status_counts": checkpoint_statuses,
        },
        "_numeric_cursors": numeric_cursors,
        "stage1_categories": stage1_categories,
    }


def _expected_migrations() -> set[str]:
    names = {path.name for path in (ROOT / "migrations").glob("*.sql")}
    if "009_phase4_metrics.sql" in names:
        names.add("009_phase4_metrics.repair.v1")
    return names


class ComposeSession:
    def __init__(self, project: str, env: Mapping[str, str]) -> None:
        self.project = project
        self.env = dict(env)
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="quant-data-layer-accept-")
        self.empty_env_file = Path(self._temporary_directory.name) / ".env"
        self.empty_env_file.touch()
        self.compose = _compose_command()
        self.base_args = self.compose + [
            "--env-file", str(self.empty_env_file),
            "--project-name", self.project,
            "--file", str(COMPOSE_FILE),
        ]
        self.child_env = _compose_child_environment(self.env)

    def run(
        self,
        *args: str,
        timeout_seconds: float = 30,
        max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES,
    ) -> str:
        return _run_bounded_process(
            self.base_args + list(args),
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            env=self.child_env,
        )

    def close(self) -> None:
        self._temporary_directory.cleanup()


def _label_map(raw: str) -> dict[str, str]:
    try:
        labels = json.loads(raw)
    except json.JSONDecodeError:
        raise AcceptanceError("RESOURCE_LABELS_UNREADABLE") from None
    if not isinstance(labels, dict):
        raise AcceptanceError("RESOURCE_LABELS_UNREADABLE")
    return {str(key): str(value) for key, value in labels.items() if value is not None}


def _project_container_records(project: str) -> list[dict[str, str]]:
    output = _docker_output([
        "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"
    ], max_output_bytes=16 * 1024)
    records = []
    for container_id in (line.strip() for line in output.splitlines() if line.strip()):
        labels = _label_map(_docker_output([
            "inspect", "--format", "{{json .Config.Labels}}", container_id
        ], max_output_bytes=16 * 1024))
        records.append({
            "project": labels.get("quant.project", labels.get("com.docker.compose.project", "")),
            "owner": labels.get("quant.owner", ""),
            "run_id": labels.get("quant.run_id", ""),
            "service": labels.get("com.docker.compose.service", ""),
            "container_id": container_id,
        })
    return records


def _owned_named_resource_records(kind: str, project: str) -> list[dict[str, str]]:
    allowed = {"volume": ("volume", "ls"), "network": ("network", "ls")}
    if kind not in allowed:
        raise ValueError("RESOURCE_KIND_INVALID")
    output = _docker_output([
        *allowed[kind], "-q", "--filter", f"label=quant.project={project}"
    ], max_output_bytes=16 * 1024)
    records = []
    for name in (line.strip() for line in output.splitlines() if line.strip()):
        labels = _label_map(_docker_output([
            kind, "inspect", "--format", "{{json .Labels}}", name
        ], max_output_bytes=16 * 1024))
        records.append({
            "project": labels.get("quant.project", ""),
            "owner": labels.get("quant.owner", ""),
            "run_id": labels.get("quant.run_id", ""),
            "name": name,
        })
    return records


def _assert_no_project_collision(project: str) -> None:
    containers = _project_container_records(project)
    volumes = _owned_named_resource_records("volume", project)
    networks = _owned_named_resource_records("network", project)
    if containers or volumes or networks:
        raise AcceptanceError("UNIQUE_PROJECT_RESOURCE_COLLISION")


def _git_secret_file_preflight() -> None:
    try:
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", ".env.local"],
            cwd=ROOT,
            capture_output=True,
            timeout=5,
            check=False,
        )
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".env.local"],
            cwd=ROOT,
            capture_output=True,
            timeout=5,
            check=False,
        )
        ignored_artifact = subprocess.run(
            ["git", "check-ignore", "-q", ".superpowers/sdd/2026-09-26-data-layer-v1-hardening/stage8/probe"],
            cwd=ROOT,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise AcceptanceError("GIT_SECRET_PREFLIGHT_FAILED") from None
    if ignored.returncode != 0 or tracked.returncode == 0:
        raise AcceptanceError("ENV_LOCAL_NOT_IGNORED_OR_IS_TRACKED")
    if ignored_artifact.returncode != 0:
        raise AcceptanceError("ACCEPTANCE_ARTIFACT_PATH_NOT_IGNORED")


def _preflight_public_config(config: Mapping[str, str]) -> None:
    validate_public_source_origins(config)
    for enabled_key, url_key in (
        ("PHASE7_BITCOIN_RPC_ENABLED", "PHASE7_BITCOIN_RPC_URL"),
        ("PHASE7_ETHEREUM_RPC_ENABLED", "PHASE7_ETHEREUM_RPC_URL"),
    ):
        enabled = config.get(enabled_key, "0").strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            continue
        endpoint = config.get(url_key, "").strip()
        if not endpoint:
            raise AcceptanceError("ENABLED_PUBLIC_RPC_ENDPOINT_MISSING")
        try:
            validate_public_rpc_endpoint(endpoint)
        except ValueError as exc:
            raise AcceptanceError(str(exc)) from None
    bitcoin_enabled = config.get("PHASE7_BITCOIN_RPC_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
    if (
        bitcoin_enabled
        and config.get("PHASE7_BITCOIN_RPC_AUTH_MODE", "none").strip().lower() == "api_key_header"
        and not config.get("PHASE7_BITCOIN_RPC_API_KEY", "").strip()
    ):
        raise AcceptanceError("BITCOIN_RPC_API_KEY_MISSING")


def _validate_local_docker_endpoint(endpoint: str) -> None:
    try:
        parsed = urlsplit(endpoint.strip())
    except ValueError:
        raise AcceptanceError("REMOTE_DOCKER_DAEMON_FORBIDDEN") from None
    local_unix = (
        parsed.scheme.lower() == "unix"
        and not parsed.netloc
        and parsed.path.startswith("/")
    )
    local_npipe = parsed.scheme.lower() == "npipe" and bool(parsed.path)
    if not (local_unix or local_npipe):
        raise AcceptanceError("REMOTE_DOCKER_DAEMON_FORBIDDEN")


def _docker_locality_preflight() -> None:
    host_override = os.environ.get("DOCKER_HOST", "").strip()
    if host_override:
        _validate_local_docker_endpoint(host_override)
    context_override = os.environ.get("DOCKER_CONTEXT", "").strip()
    inspect_args = ["context", "inspect"]
    if context_override:
        inspect_args.append(context_override)
    inspect_args.extend(("--format", "{{.Endpoints.docker.Host}}"))
    endpoint = _docker_output(
        inspect_args,
        timeout_seconds=10,
        max_output_bytes=4096,
    ).strip()
    if not endpoint:
        raise AcceptanceError("DOCKER_CONTEXT_UNRESOLVED")
    _validate_local_docker_endpoint(endpoint)
def _docker_proxy_preflight() -> bool:
    proxy_setting_names = {"httpproxy", "httpsproxy", "ftpproxy", "allproxy"}
    proxy_environment_names = ("HTTP_PROXY", "HTTPS_PROXY", "FTP_PROXY", "ALL_PROXY")
    if any(
        os.environ.get(name, "").strip() or os.environ.get(name.lower(), "").strip()
        for name in proxy_environment_names
    ):
        raise AcceptanceError("PROXY_CONFIGURATION_PRESENT")

    docker_config_dir = Path(os.environ.get("DOCKER_CONFIG", Path.home() / ".docker"))
    config_path = docker_config_dir / "config.json"
    if config_path.is_file():
        try:
            docker_config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise AcceptanceError("DOCKER_CLIENT_CONFIG_UNREADABLE") from None

        def has_proxy_setting(value: Any) -> bool:
            if isinstance(value, Mapping):
                for key, child in value.items():
                    if str(key).lower() in proxy_setting_names and isinstance(child, str) and child.strip():
                        return True
                    if has_proxy_setting(child):
                        return True
            elif isinstance(value, list):
                return any(has_proxy_setting(item) for item in value)
            return False

        if isinstance(docker_config, Mapping) and has_proxy_setting(docker_config.get("proxies", {})):
            raise AcceptanceError("PROXY_CONFIGURATION_PRESENT")
    _docker_locality_preflight()
    proxy_status = _docker_output(
        ["info", "--format", "{{.HTTPProxy}}|{{.HTTPSProxy}}"],
        timeout_seconds=15,
        max_output_bytes=4096,
    )
    if any(value.strip() not in {"", "<no value>"} for value in proxy_status.split("|")):
        raise AcceptanceError("PROXY_CONFIGURATION_PRESENT")
    return False


def _write_json_report(path: Path, report: Mapping[str, Any], secret_values: Iterable[str]) -> None:
    encoded = json.dumps(report, sort_keys=True, indent=2, ensure_ascii=True).encode("utf-8") + b"\n"
    if len(encoded) > MAX_ARTIFACT_BYTES:
        raise AcceptanceError("REPORT_SIZE_LIMIT_EXCEEDED")
    if any(secret.encode("utf-8") in encoded for secret in secret_values if secret):
        raise AcceptanceError("REPORT_SECRET_LEAK_BLOCKED")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    with path.open("xb") as stream:
        stream.write(encoded)
    os.chmod(path, 0o600)


def acceptance_project_name(run_uuid: UUID | None = None) -> str:
    return f"quant-dlv1-accept-{(run_uuid or uuid4()).hex[:12]}"


def _parse_env_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] == "'":
        if len(value) < 2 or value[-1] != "'":
            raise ValueError("ENV_FILE_MALFORMED")
        return value[1:-1]
    if value[0] == '"':
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            raise ValueError("ENV_FILE_MALFORMED") from None
        if not isinstance(parsed, str):
            raise ValueError("ENV_FILE_MALFORMED")
        return parsed
    comment_at = value.find(" #")
    return value[:comment_at].rstrip() if comment_at >= 0 else value


def load_public_source_config(path: Path = ENV_FILE) -> dict[str, str]:
    """Read only explicitly allowed public-source settings; never execute the file."""
    if not path.is_file():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        name, separator, raw_value = stripped.partition("=")
        if not separator or not _ENV_NAME.fullmatch(name.strip()):
            raise ValueError("ENV_FILE_MALFORMED")
        name = name.strip()
        if name in PUBLIC_SOURCE_ENV_KEYS:
            if name in result:
                raise ValueError("ENV_FILE_DUPLICATE_KEY")
            result[name] = _parse_env_value(raw_value)
    return result


def validate_public_rpc_endpoint(
    endpoint: str,
    *,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> None:
    """Require HTTPS and globally routed DNS answers without exposing the endpoint."""
    try:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
        ):
            raise ValueError("RPC_ENDPOINT_NOT_PUBLIC")
        try:
            literal = ipaddress.ip_address(parsed.hostname)
            addresses = [literal]
        except ValueError:
            rows = resolver(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
            addresses = [ipaddress.ip_address(row[4][0].split("%", 1)[0]) for row in rows]
        if not addresses or any(not address.is_global for address in addresses):
            raise ValueError("RPC_ENDPOINT_NOT_PUBLIC")
    except ValueError as exc:
        if str(exc) == "RPC_ENDPOINT_NOT_PUBLIC":
            raise
        raise ValueError("RPC_ENDPOINT_NOT_PUBLIC") from None
    except OSError:
        raise ValueError("RPC_DNS_FAILED") from None


def validate_public_source_origins(config: Mapping[str, str]) -> None:
    for name, (scheme, host) in PUBLIC_ORIGINS.items():
        value = config.get(name)
        if not value:
            continue
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError:
            raise ValueError("PUBLIC_SOURCE_ORIGIN_NOT_ALLOWED") from None
        if (
            parsed.scheme != scheme
            or parsed.hostname != host
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("PUBLIC_SOURCE_ORIGIN_NOT_ALLOWED")


def _compose_command() -> list[str]:
    docker = shutil.which("docker")
    if docker:
        probe = subprocess.run([docker, "compose", "version"], capture_output=True, text=True, check=False)
        if probe.returncode == 0:
            return [docker, "compose"]
    standalone = shutil.which("docker-compose")
    if standalone:
        return [standalone]
    raise AcceptanceError("DOCKER_COMPOSE_UNAVAILABLE")


def _compose_child_environment(values: Mapping[str, str]) -> dict[str, str]:
    child = {key: os.environ[key] for key in _SAFE_BASE_ENV if os.environ.get(key)}
    child["PATH"] = child.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
    child.update(values)
    return child


def render_compose_model(project: str, *, env: Mapping[str, str]) -> dict[str, Any]:
    values = dict(env)
    values.setdefault("DATA_LAYER_ACCEPTANCE_RUN_ID", project.rsplit("-", 1)[-1])
    values.setdefault("DATA_LAYER_ACCEPTANCE_VOLUME_NAME", f"{project}_postgres_data")
    values.setdefault("DATA_LAYER_ACCEPTANCE_PROJECT", project)
    compose = _compose_command()
    is_v2 = len(compose) > 1 and compose[-1] == "compose"
    with tempfile.TemporaryDirectory(prefix="quant-compose-env-") as temp_dir:
        empty_env = Path(temp_dir) / ".env"
        empty_env.touch()
        command = compose + [
            "--env-file", str(empty_env),
            "--project-name", project,
            "--file", str(COMPOSE_FILE),
            "config",
        ]
        if is_v2:
            command.extend(("--format", "json"))
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=_compose_child_environment(values),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    if result.returncode:
        raise AcceptanceError("COMPOSE_CONFIG_INVALID")
    try:
        model = json.loads(result.stdout)
    except json.JSONDecodeError:
        # Compose v1.29 emits resolved YAML. Parse that output in memory; never
        # surface resolved config or parser errors because they may contain
        # provider credentials.
        parser = shutil.which("python3")
        if not parser:
            raise AcceptanceError("COMPOSE_CONFIG_PARSER_UNAVAILABLE") from None
        parsed = subprocess.run(
            [parser, "-c", "import json,sys,yaml; print(json.dumps(yaml.safe_load(sys.stdin.read())))"],
            input=result.stdout,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if parsed.returncode:
            raise AcceptanceError("COMPOSE_CONFIG_PARSER_UNAVAILABLE") from None
        try:
            model = json.loads(parsed.stdout)
        except json.JSONDecodeError:
            raise AcceptanceError("COMPOSE_CONFIG_INVALID") from None
    if not isinstance(model, dict):
        raise AcceptanceError("COMPOSE_CONFIG_INVALID")
    return model


def _environment_mapping(service: Mapping[str, Any]) -> dict[str, Any]:
    environment = service.get("environment", {})
    if isinstance(environment, list):
        pairs = (item.split("=", 1) for item in environment if isinstance(item, str))
        return {pair[0]: pair[1] if len(pair) > 1 else None for pair in pairs}
    return dict(environment) if isinstance(environment, Mapping) else {}


def validate_compose_model(model: Mapping[str, Any]) -> None:
    services = model.get("services")
    if not isinstance(services, Mapping) or set(services) != EXPECTED_SERVICES:
        raise ValueError("COMPOSE_SERVICE_SET_INVALID")
    for service_name, expected_limit in SERVICE_MEMORY_LIMITS.items():
        service = services[service_name]
        if service.get("mem_limit") != expected_limit or service.get("memswap_limit") != expected_limit:
            raise ValueError("COMPOSE_MEMORY_CAP_INVALID")
        if service.get("ports") or "container_name" in service or service.get("restart") != "no":
            raise ValueError("COMPOSE_ISOLATION_INVALID")
        logging = service.get("logging", {})
        options = logging.get("options", {}) if isinstance(logging, Mapping) else {}
        if (
            logging.get("driver") != "json-file"
            or options.get("max-size") != "10m"
            or options.get("max-file") != "3"
        ):
            raise ValueError("LOG_ROTATION_REQUIRED")
        labels = service.get("labels", {})
        if labels.get("quant.owner") != OWNER or not labels.get("quant.project") or not labels.get("quant.run_id"):
            raise ValueError("COMPOSE_OWNERSHIP_LABELS_REQUIRED")
    for service_name in ("quant-collector", "quant-engine"):
        environment = _environment_mapping(services[service_name])
        if environment.get("TRADING_MODE") != "paper":
            raise ValueError("PAPER_MODE_REQUIRED")
        if environment.get("POSTGRES_DSN") != "postgresql://quant@postgres:5432/quant":
            raise ValueError("ISOLATED_DATABASE_REQUIRED")
        if "env_file" in services[service_name]:
            raise ValueError("UNFILTERED_ENV_FILE_FORBIDDEN")
        for key in environment:
            normalized_key = key.upper()
            if "PROXY" in normalized_key:
                raise ValueError("PROXY_ENV_FORBIDDEN")
            forbidden_tokens = ("PRIVATE", "SECRET", "PASSWORD", "PASSPHRASE", "TOKEN", "ORDER", "POSITION", "LIVE_EXECUTOR")
            if any(token in normalized_key for token in forbidden_tokens):
                raise ValueError("PRIVATE_ENV_NOT_ALLOWED")
            if normalized_key.endswith("_API_KEY") and normalized_key != "PHASE7_BITCOIN_RPC_API_KEY":
                raise ValueError("PRIVATE_ENV_NOT_ALLOWED")
    collector_env = _environment_mapping(services["quant-collector"])
    engine_env = _environment_mapping(services["quant-engine"])
    if collector_env.get("PHASE7_BITCOIN_RPC_API_KEY") is not None and "PHASE7_BITCOIN_RPC_API_KEY" in engine_env:
        raise ValueError("RPC_SECRET_SCOPE_INVALID")
    volumes = model.get("volumes", {})
    database_volume = volumes.get("postgres_data", {}) if isinstance(volumes, Mapping) else {}
    labels = database_volume.get("labels", {}) if isinstance(database_volume, Mapping) else {}
    if labels.get("quant.owner") != OWNER or not database_volume.get("name"):
        raise ValueError("DISPOSABLE_VOLUME_OWNERSHIP_REQUIRED")
    if not labels.get("quant.project") or not labels.get("quant.run_id"):
        raise ValueError("DISPOSABLE_VOLUME_OWNERSHIP_REQUIRED")
    networks = model.get("networks", {})
    default_network = networks.get("default", {}) if isinstance(networks, Mapping) else {}
    network_labels = default_network.get("labels", {}) if isinstance(default_network, Mapping) else {}
    if (
        network_labels.get("quant.owner") != OWNER
        or not network_labels.get("quant.project")
        or not network_labels.get("quant.run_id")
        or default_network.get("internal") is True
    ):
        raise ValueError("PUBLIC_EGRESS_NETWORK_CONTRACT_INVALID")


def validate_owned_project_resources(project: str, resources: Iterable[Mapping[str, Any]]) -> bool:
    for resource in resources:
        if resource.get("project") != project or resource.get("owner") != OWNER:
            raise ValueError("CLEANUP_OWNERSHIP_MISMATCH")
    return True


def build_failure_matrix(
    *,
    source_statuses: Mapping[str, str],
    unexposed_metrics: Iterable[str] = (),
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for source, status in sorted(source_statuses.items()):
        if status == "AVAILABLE":
            result = "PASS"
        elif status == "NOT_CONFIGURED":
            result = "NOT_CONFIGURED"
        elif status == "NOT_AVAILABLE":
            result = "NOT_AVAILABLE"
        elif status == "PARTIAL":
            result = "PARTIAL"
        elif status in {"STALE", "DEGRADED", "RATE_LIMITED", "ERROR"}:
            result = "FAIL"
        else:
            result = "NOT_EXPOSED"
        rows.append({"source": source, "observed_status": status, "result": result})
    for metric in sorted(set(unexposed_metrics)):
        rows.append({"source": metric, "observed_status": "NOT_EXPOSED", "result": "NOT_EXPOSED"})
    return rows


def _contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in _FORBIDDEN_ARTIFACT_KEYS:
                return True
            if _contains_forbidden_key(child):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_forbidden_key(item) for item in value)
    return False


class BoundedJsonlWriter:
    def __init__(
        self,
        path: Path,
        *,
        max_record_bytes: int = MAX_SAMPLE_BYTES,
        max_artifact_bytes: int = MAX_ARTIFACT_BYTES,
        secret_values: Iterable[str] = (),
    ) -> None:
        self.path = path
        self.max_record_bytes = max_record_bytes
        self.max_artifact_bytes = max_artifact_bytes
        self.secret_values = tuple(value for value in secret_values if value)

    def write(self, record: Mapping[str, Any]) -> int:
        if _contains_forbidden_key(record):
            raise ValueError("RAW_PAYLOAD_BLOCKED")
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8") + b"\n"
        if len(encoded) > self.max_record_bytes:
            raise ValueError("ARTIFACT_RECORD_TOO_LARGE")
        if any(secret.encode("utf-8") in encoded for secret in self.secret_values):
            raise ValueError("SECRET_LEAK_BLOCKED")
        current_size = self.path.stat().st_size if self.path.exists() else 0
        if current_size + len(encoded) > self.max_artifact_bytes:
            raise ValueError("ARTIFACT_TOTAL_SIZE_LIMIT")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as stream:
            stream.write(encoded)
            stream.flush()
        os.chmod(self.path, 0o600)
        return current_size + len(encoded)


class RuntimeTelemetry:
    def __init__(
        self,
        *,
        container_ids: Mapping[str, str],
        writer: BoundedJsonlWriter,
        secret_values: Iterable[str],
    ) -> None:
        self.container_ids = dict(container_ids)
        self.writer = writer
        self.secret_values = tuple(value for value in secret_values if value)
        self.host_cpu = HostCpuSampler()
        self.first_database: dict[str, Any] | None = None
        self.latest_database: dict[str, Any] | None = None
        self.latest_services: dict[str, Any] = {}
        self.latest_memory_events: dict[str, dict[str, int]] = {}
        self.previous_memory_events: dict[str, dict[str, int]] | None = None
        self.previous_cursors: dict[str, str] = {}
        self.current_cursor_rollbacks: list[str] = []
        self.latest_disk_free_ratio: float | None = None
        self.minimum_disk_free_ratio: float | None = None
        self.secret_leak_found = False
        self.logs_audited = True
        self.services_stable = True
        self.database_contract_stable = True
        self.database_samples_complete = True
        self.resource_measurements_complete = True
        self.memory_caps_respected = True
        self.cursor_observability_complete = True
        self.host_cpu_samples: list[float] = []
        self.maximum_resources: dict[str, dict[str, int]] = {}
        self.last_sample_record: dict[str, Any] = {}
        self.artifact_failure: str | None = None
        self.sample_error_counts: dict[str, int] = {}
        self.sample_count = 0

    def _sample_error(self, category: str) -> None:
        self.sample_error_counts[category] = self.sample_error_counts.get(category, 0) + 1

    def sample(self, sequence: int, scheduled_offset: int) -> None:
        self.sample_count = sequence
        timestamp = datetime.now(timezone.utc).isoformat()
        disk = shutil.disk_usage(ROOT)
        disk_ratio = disk.free / disk.total if disk.total else None
        self.latest_disk_free_ratio = disk_ratio
        if disk_ratio is not None:
            self.minimum_disk_free_ratio = (
                disk_ratio if self.minimum_disk_free_ratio is None
                else min(self.minimum_disk_free_ratio, disk_ratio)
            )
        try:
            host_cpu = self.host_cpu.sample_percent()
        except (OSError, ValueError):
            host_cpu = None
            self._sample_error("HOST_CPU_NOT_EXPOSED")
        if host_cpu is not None:
            self.host_cpu_samples.append(host_cpu)

        services: dict[str, Any] = {}
        self.latest_memory_events = {}
        for service in SERVICES:
            container_id = self.container_ids[service]
            try:
                state = _container_state(container_id)
            except AcceptanceError:
                state = {
                    "status": "ERROR", "health": None, "oom_killed": False,
                    "exit_code": None, "restart_count": None,
                }
                self._sample_error(f"{service.upper().replace('-', '_')}_STATE_ERROR")
            resource = None
            if state["status"] == "running":
                try:
                    resource = _container_resource_sample(container_id)
                except AcceptanceError:
                    self._sample_error(f"{service.upper().replace('-', '_')}_CGROUP_NOT_EXPOSED")
            else:
                self._sample_error(f"{service.upper().replace('-', '_')}_NOT_RUNNING")
            if resource is None:
                self.resource_measurements_complete = False
            else:
                self.latest_memory_events[service] = {
                    "oom": int(resource["memory_events_oom"] or 0),
                    "oom_kill": int(resource["memory_events_oom_kill"] or 0),
                }
                limit_expected = {
                    "postgres": 768 * 1024 * 1024,
                    "quant-collector": 256 * 1024 * 1024,
                    "quant-engine": 384 * 1024 * 1024,
                }[service]
                self.memory_caps_respected &= (
                    resource["memory_limit_bytes"] == limit_expected
                    and resource["memory_peak_bytes"] <= resource["memory_limit_bytes"]
                )
                prior = self.maximum_resources.setdefault(service, {})
                for key in ("memory_current_bytes", "memory_peak_bytes", "process_rss_bytes", "process_pss_bytes"):
                    value = resource.get(key)
                    if isinstance(value, int):
                        prior[key] = max(prior.get(key, 0), value)
            try:
                log_sample = _container_log_sample(container_id, self.secret_values)
            except AcceptanceError:
                log_sample = {
                    "status": "ERROR", "bytes": None,
                    "sampled_tail_bytes": 0, "secret_leak_found": False,
                }
            if log_sample["status"] != "AVAILABLE":
                self.logs_audited = False
                self._sample_error(f"{service.upper().replace('-', '_')}_LOG_AUDIT_NOT_EXPOSED")
            self.secret_leak_found |= bool(log_sample["secret_leak_found"])
            self.services_stable &= (
                state["status"] == "running"
                and not state["oom_killed"]
                and state["restart_count"] == 0
                and (service != "postgres" or state["health"] == "healthy")
            )
            services[service] = {**state, "resources": resource, "logs": log_sample}
        self.latest_services = services

        database_sample: dict[str, Any] | None = None
        phase_statuses: dict[str, str] = {f"phase{phase}": "NOT_EXPOSED" for phase in range(1, 9)}
        phase_activity: dict[str, int] = {}
        self.current_cursor_rollbacks = []
        try:
            raw_database = _database_snapshot(
                self.container_ids["postgres"],
                include_stage1_detail=(sequence == len(scheduled_sample_offsets())),
            )
            current_cursors = raw_database.pop("_numeric_cursors", {})
            self.current_cursor_rollbacks = find_numeric_cursor_rollbacks(self.previous_cursors, current_cursors)
            self.previous_cursors = current_cursors
            if self.first_database is None:
                self.first_database = raw_database
            self.latest_database = raw_database
            self.cursor_observability_complete &= not raw_database["checkpoints"]["truncated"]
            activity = phase_table_activity(
                raw_database["table_stats"], self.first_database["table_stats"]
            )
            phase_activity = {str(phase): int(delta) for phase, delta in activity.items()}
            phase_statuses = derive_phase_statuses(
                health=raw_database["health"],
                phase_activity=activity,
                table_stats=raw_database["table_stats"],
            )
            database = raw_database["database"]
            self.database_contract_stable &= database["name"] == "quant" and database["timezone"].upper() == "UTC"
            database_sample = {
                "status": "AVAILABLE",
                "database": database,
                "migrations": raw_database["migrations"],
                "health": raw_database["health"],
                "table_stats": raw_database["table_stats"],
                "checkpoints": raw_database["checkpoints"],
                "stage1_categories": raw_database["stage1_categories"],
                "phase_table_activity": phase_activity,
                "phase_statuses": phase_statuses,
                "cursor_rollback_ids": self.current_cursor_rollbacks,
            }
        except Exception:
            self.database_samples_complete = False
            self._sample_error("DATABASE_SAMPLE_FAILED")
            database_sample = {"status": "ERROR", "error_category": "DATABASE_SAMPLE_FAILED"}

        record = {
            "sampled_at_utc": timestamp,
            "sample_sequence": sequence,
            "scheduled_offset_seconds": scheduled_offset,
            "host": {
                "cpu_percent": host_cpu,
                "disk_total_bytes": disk.total,
                "disk_free_bytes": disk.free,
                "disk_free_ratio": round(disk_ratio, 6) if disk_ratio is not None else None,
            },
            "services": services,
            "database": database_sample,
            "phase_statuses": phase_statuses,
            "phase_table_activity": phase_activity,
            "error_categories": dict(sorted(self.sample_error_counts.items())),
            "artifact_bytes_before_sample": self.writer.path.stat().st_size if self.writer.path.exists() else 0,
        }
        self.last_sample_record = record
        try:
            self.writer.write(record)
        except ValueError as exc:
            category = str(exc)
            if category == "SECRET_LEAK_BLOCKED":
                self.secret_leak_found = True
                self.artifact_failure = "SECRET_LEAK_DETECTED"
            elif category == "RAW_PAYLOAD_BLOCKED":
                self.artifact_failure = "RAW_PAYLOAD_BLOCKED"
            elif category == "ARTIFACT_RECORD_TOO_LARGE":
                self.artifact_failure = "SAMPLE_RECORD_SIZE_LIMIT"
            else:
                self.artifact_failure = "ARTIFACT_SIZE_LIMIT"

    def hard_stop(self) -> str | None:
        if self.artifact_failure:
            return self.artifact_failure
        if any(row.get("oom_killed") for row in self.latest_services.values() if isinstance(row, Mapping)):
            return "CONTAINER_OOM_KILLED"
        if self.latest_disk_free_ratio is not None and self.latest_disk_free_ratio < MIN_DISK_FREE_RATIO:
            return "DISK_FREE_BELOW_15_PERCENT"
        if self.secret_leak_found:
            return "SECRET_LEAK_DETECTED"
        if self.current_cursor_rollbacks:
            return "NUMERIC_CURSOR_ROLLBACK"
        if self.latest_database is not None and self.latest_database["database"]["name"] != "quant":
            return "DATABASE_IDENTITY_MISMATCH"
        previous = self.previous_memory_events
        if previous is None:
            previous = {service: {"oom": 0, "oom_kill": 0} for service in SERVICES}
        if self.latest_memory_events and any(
            self.latest_memory_events.get(service, {}).get(key, 0) > previous.get(service, {}).get(key, 0)
            for service in SERVICES for key in ("oom", "oom_kill")
        ):
            return "OOM_EVENT_DELTA"
        self.previous_memory_events = dict(self.latest_memory_events or previous)
        return None


def _wait_for_runtime_ready(
    session: ComposeSession,
    *,
    run_id: str,
    timeout_seconds: int = STARTUP_TIMEOUT_SECONDS,
) -> dict[str, str]:
    deadline = time.monotonic() + timeout_seconds
    expected_migrations = _expected_migrations()
    last_safe_error = "STARTUP_NOT_READY"
    while time.monotonic() < deadline:
        records = _project_container_records(session.project)
        if records:
            try:
                validate_owned_project_resources(session.project, records)
                if any(row.get("run_id") != run_id for row in records):
                    raise AcceptanceError("CONTAINER_RUN_ID_MISMATCH")
                by_service: dict[str, list[str]] = {service: [] for service in SERVICES}
                for row in records:
                    if row.get("service") in by_service:
                        by_service[row["service"]].append(row["container_id"])
                if any(len(ids) != 1 for ids in by_service.values()) or len(records) != len(SERVICES):
                    last_safe_error = "SERVICE_SET_NOT_READY"
                else:
                    container_ids = {service: ids[0] for service, ids in by_service.items()}
                    states = {service: _container_state(cid) for service, cid in container_ids.items()}
                    if any(state["oom_killed"] for state in states.values()):
                        raise AcceptanceError("STARTUP_OOM_KILLED")
                    if any(state["status"] != "running" for state in states.values()):
                        last_safe_error = "SERVICE_NOT_RUNNING"
                    elif states["postgres"]["health"] != "healthy":
                        last_safe_error = "POSTGRES_NOT_HEALTHY"
                    else:
                        try:
                            database = _database_snapshot(container_ids["postgres"])
                        except Exception:
                            last_safe_error = "DATABASE_SCHEMA_NOT_READY"
                        else:
                            if set(database["migrations"]) == expected_migrations:
                                if database["database"]["name"] != "quant" or database["database"]["timezone"].upper() != "UTC":
                                    raise AcceptanceError("DATABASE_IDENTITY_OR_TIMEZONE_MISMATCH")
                                return container_ids
                            last_safe_error = "MIGRATIONS_NOT_READY"
            except AcceptanceError:
                raise
        time.sleep(2)
    raise AcceptanceError(last_safe_error)


def _shutdown_runtime(
    session: ComposeSession,
    *,
    project: str,
    run_id: str,
) -> dict[str, Any]:
    containers = _project_container_records(project)
    networks = _owned_named_resource_records("network", project)
    volumes = _owned_named_resource_records("volume", project)
    all_records = containers + networks + volumes
    if not all_records:
        return {
            "clean_shutdown": False,
            "cleanup_status": "NO_PROJECT_RESOURCES",
            "final_database_probe": {"status": "NOT_EXPOSED"},
            "service_exit_states": {},
            "preserved_volume_names": [],
        }
    try:
        validate_owned_project_resources(project, all_records)
        if any(row.get("run_id") != run_id for row in all_records):
            raise ValueError("CLEANUP_OWNERSHIP_MISMATCH")
        if any(not row.get("service") for row in containers):
            raise ValueError("CLEANUP_SERVICE_IDENTITY_MISSING")
    except ValueError:
        return {
            "clean_shutdown": False,
            "cleanup_status": "OWNERSHIP_CHECK_FAILED",
            "final_database_probe": {"status": "NOT_EXPOSED"},
            "service_exit_states": {},
            "preserved_volume_names": [row.get("name", "") for row in volumes],
        }

    by_service = {row["service"]: row["container_id"] for row in containers}
    service_exit_states: dict[str, Any] = {}
    final_database_probe: dict[str, Any] = {"status": "NOT_EXPOSED"}
    for service in ("quant-engine", "quant-collector"):
        container_id = by_service.get(service)
        if not container_id:
            continue
        try:
            state = _container_state(container_id)
            if state["status"] == "running":
                session.run("stop", "--timeout", "30", service, timeout_seconds=40)
                state = _container_state(container_id)
            service_exit_states[service] = state
        except AcceptanceError:
            service_exit_states[service] = {"status": "ERROR", "exit_code": None, "oom_killed": False}

    postgres_id = by_service.get("postgres")
    if postgres_id:
        try:
            final_database = _database_snapshot(postgres_id)
            final_database_probe = {
                "status": "AVAILABLE",
                "database": final_database["database"],
                "migration_count": len(final_database["migrations"]),
                "health": {name: row["status"] for name, row in final_database["health"].items()},
            }
        except Exception:
            final_database_probe = {"status": "ERROR", "error_category": "FINAL_DATABASE_PROBE_FAILED"}
        try:
            state = _container_state(postgres_id)
            if state["status"] == "running":
                session.run("stop", "--timeout", "30", "postgres", timeout_seconds=40)
                state = _container_state(postgres_id)
            service_exit_states["postgres"] = state
        except AcceptanceError:
            service_exit_states["postgres"] = {"status": "ERROR", "exit_code": None, "oom_killed": False}

    containers = _project_container_records(project)
    networks = _owned_named_resource_records("network", project)
    volumes = _owned_named_resource_records("volume", project)
    try:
        validate_owned_project_resources(project, containers + networks + volumes)
        if any(row.get("run_id") != run_id for row in containers + networks + volumes):
            raise ValueError("CLEANUP_OWNERSHIP_MISMATCH")
        if any(not row.get("service") for row in containers):
            raise ValueError("CLEANUP_SERVICE_IDENTITY_MISSING")
        session.run("down", "--remove-orphans", timeout_seconds=60)
        remaining_containers = _project_container_records(project)
        remaining_networks = _owned_named_resource_records("network", project)
        remaining_volumes = _owned_named_resource_records("volume", project)
        if remaining_containers or remaining_networks:
            cleanup_status = "PROJECT_RESOURCES_REMAIN"
        elif len(remaining_volumes) != 1 or any(row.get("run_id") != run_id for row in remaining_volumes):
            cleanup_status = "PRESERVED_VOLUME_OWNERSHIP_UNCONFIRMED"
        else:
            cleanup_status = "CONTAINERS_AND_NETWORK_REMOVED_VOLUME_PRESERVED"
    except (ValueError, AcceptanceError):
        cleanup_status = "OWNERSHIP_CHECK_FAILED"
        remaining_volumes = volumes

    clean_shutdown = (
        len(service_exit_states) == len(SERVICES)
        and all(
            row.get("status") == "exited" and row.get("exit_code") == 0 and not row.get("oom_killed")
            for row in service_exit_states.values()
        )
        and final_database_probe.get("status") == "AVAILABLE"
        and cleanup_status == "CONTAINERS_AND_NETWORK_REMOVED_VOLUME_PRESERVED"
    )
    return {
        "clean_shutdown": clean_shutdown,
        "cleanup_status": cleanup_status,
        "final_database_probe": final_database_probe,
        "service_exit_states": service_exit_states,
        "preserved_volume_names": [row.get("name", "") for row in remaining_volumes],
    }


def _health_gate_is_fresh(health: Mapping[str, Mapping[str, Any]] | None) -> bool:
    if not health:
        return False
    for component in ("quant-collector", "quant-engine"):
        row = health.get(component)
        if not isinstance(row, Mapping):
            return False
        if row.get("status") != "AVAILABLE":
            return False
        if not isinstance(row.get("age_seconds"), (int, float)) or row["age_seconds"] > HEALTH_MAX_AGE_SECONDS:
            return False
    return True


def _table_counter_delta(
    baseline: Mapping[str, Mapping[str, int]] | None,
    final: Mapping[str, Mapping[str, int]] | None,
    table: str,
) -> dict[str, int]:
    before = (baseline or {}).get(table, {})
    after = (final or {}).get(table, {})
    return {
        counter: max(0, int(after.get(counter, 0)) - int(before.get(counter, 0)))
        for counter in ("n_tup_ins", "n_tup_upd", "n_tup_del")
    }


def _configured_secret_values(config: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(
        value for key, value in config.items()
        if value and (key.endswith("_API_KEY") or key.endswith("_URL"))
    )


def _build_final_report(
    *,
    run_id: str,
    project: str,
    image: str,
    configured_keys: Iterable[str],
    telemetry: RuntimeTelemetry | None,
    sampling_result: Mapping[str, Any],
    shutdown: Mapping[str, Any],
    startup_error: str | None,
    build_seconds: float | None,
    startup_seconds: float | None,
    proxy_preflight_status: str,
    run_mode: str = "STAGE8_ACCEPTANCE",
) -> dict[str, Any]:
    first = telemetry.first_database if telemetry else None
    final = telemetry.latest_database if telemetry else None
    baseline_stats = first.get("table_stats", {}) if first else {}
    final_stats = final.get("table_stats", {}) if final else {}
    table_deltas = {
        table: {
            counter: max(0, int(final_stats.get(table, {}).get(counter, 0)) - int(baseline_stats.get(table, {}).get(counter, 0)))
            for counter in ("n_tup_ins", "n_tup_upd", "n_tup_del")
        }
        for table in sorted(set(baseline_stats) | set(final_stats))
    }
    phase_activity = phase_table_activity(final_stats, baseline_stats) if final else {}
    phase_statuses = derive_phase_statuses(
        health=final.get("health", {}) if final else {},
        phase_activity=phase_activity,
        table_stats=final_stats,
    )
    degradation_evidence = derive_phase_degradation_evidence(final.get("health", {}) if final else {})
    optional_source_configuration = derive_optional_source_configuration(final.get("health", {}) if final else {})
    migration_match = bool(
        final and set(final.get("migrations", ())) == _expected_migrations()
    )
    database_contract_ok = bool(
        telemetry and telemetry.database_contract_stable and telemetry.database_samples_complete and final
        and final["database"]["name"] == "quant"
        and final["database"]["timezone"].upper() == "UTC"
    )
    market_delta = _table_counter_delta(baseline_stats, final_stats, "market_snapshots")
    kline_delta = _table_counter_delta(baseline_stats, final_stats, "klines")
    stage1_delta = _table_counter_delta(baseline_stats, final_stats, "screening_results")
    phase1_market_activity = sum(market_delta.values()) > 0 and sum(kline_delta.values()) > 0
    phase1_stage1_activity = sum(stage1_delta.values()) > 0
    hard_stop_reason = (
        sampling_result.get("stop_reason")
        if sampling_result.get("status") == "HARD_STOP"
        else telemetry.artifact_failure if telemetry and telemetry.artifact_failure else None
    )
    gates = build_acceptance_gate_results(
        window_status=str(sampling_result.get("status", "NOT_STARTED")),
        missed_intervals=int(sampling_result.get("missed_intervals", 0)),
        migrations_match=migration_match,
        database_identity_utc=database_contract_ok,
        services_stable=bool(telemetry and telemetry.services_stable),
        core_health_fresh=_health_gate_is_fresh(final.get("health", {}) if final else None),
        phase1_market_activity=phase1_market_activity,
        phase1_stage1_activity=phase1_stage1_activity,
        resource_measurements_complete=bool(telemetry and telemetry.resource_measurements_complete),
        memory_caps_respected=bool(telemetry and telemetry.memory_caps_respected),
        cursor_observability_complete=bool(telemetry and telemetry.cursor_observability_complete),
        phase_statuses=phase_statuses,
        phase_degradation_evidence=degradation_evidence,
        optional_source_configuration=optional_source_configuration,
        minimum_disk_free_ratio=telemetry.minimum_disk_free_ratio if telemetry else None,
        hard_stop_reason=hard_stop_reason or startup_error,
        logs_audited=bool(telemetry and telemetry.logs_audited),
        secret_leak_found=bool(telemetry and telemetry.secret_leak_found),
        clean_shutdown=bool(shutdown.get("clean_shutdown")),
    )
    diagnostic_only = run_mode == "TARGETED_DIAGNOSTIC"
    if diagnostic_only:
        for gate in gates:
            if gate["gate"] == "fixed_15_minute_window":
                gate["status"] = "NOT_EVALUATED"
                gate["reason"] = "TARGETED_WINDOW_IS_NOT_STAGE8_ACCEPTANCE"
    host_cpu = telemetry.host_cpu_samples if telemetry else []
    last_services = telemetry.last_sample_record.get("services", {}) if telemetry else {}
    log_bytes = {
        service: (last_services.get(service, {}).get("logs", {}).get("bytes"))
        for service in SERVICES
    }
    memory_peaks = {
        service: metrics.get("memory_peak_bytes")
        for service, metrics in (telemetry.maximum_resources.items() if telemetry else ())
    }
    report = {
        "report_type": (
            "DATA_LAYER_V1_TARGETED_RUNTIME_DIAGNOSTIC"
            if diagnostic_only else "DATA_LAYER_V1_STAGE8_ACCEPTANCE"
        ),
        "run_mode": run_mode,
        "acceptance_evaluated": not diagnostic_only,
        "run_id": run_id,
        "project": project,
        "image": image,
        "acceptance_status": (
            "NOT_EVALUATED_TARGETED"
            if diagnostic_only
            else "PASS" if acceptance_gates_pass(gates) else "FAIL"
        ),
        "configured_environment_keys": sorted(set(configured_keys)),
        "network": {
            "egress_scope": "PUBLIC_ONLY",
            "proxy_used": False,
            "proxy_preflight": proxy_preflight_status,
        },
        "build_seconds": round(build_seconds, 3) if build_seconds is not None else None,
        "startup_seconds": round(startup_seconds, 3) if startup_seconds is not None else None,
        "runtime_window": dict(sampling_result),
        "sample_count": telemetry.sample_count if telemetry else 0,
        "sample_interval_seconds": ACCEPTANCE_SAMPLE_INTERVAL_SECONDS,
        "missed_intervals": int(sampling_result.get("missed_intervals", 0)),
        "database": {
            "baseline_size_bytes": first["database"]["size_bytes"] if first else None,
            "final_size_bytes": final["database"]["size_bytes"] if final else None,
            "measured_size_growth_bytes": (
                final["database"]["size_bytes"] - first["database"]["size_bytes"] if first and final else None
            ),
            "migrations_match": migration_match,
            "migration_count": len(final.get("migrations", ())) if final else 0,
            "current_database": final["database"]["name"] if final else None,
            "timezone": final["database"]["timezone"] if final else None,
            "active_transactions": final["database"]["active_transactions"] if final else None,
            "idle_in_transaction": final["database"]["idle_in_transaction"] if final else None,
            "screening_result_category_counts": (
                final.get("stage1_categories") if final else None
            ),
        },
        "table_activity_deltas": table_deltas,
        "phase_activity_deltas": {str(key): value for key, value in phase_activity.items()},
        "phase_statuses": phase_statuses,
        "phase_degradation_evidence": degradation_evidence,
        "optional_source_configuration": optional_source_configuration,
        "source_failure_matrix": build_failure_matrix(
            source_statuses=phase_statuses,
            unexposed_metrics=(
                ("host_cpu",) if telemetry and not telemetry.host_cpu_samples else ()
            ),
        ),
        "resources": {
            "memory_caps_bytes": {
                "postgres": 768 * 1024 * 1024,
                "quant-collector": 256 * 1024 * 1024,
                "quant-engine": 384 * 1024 * 1024,
            },
            "observed_cgroup_memory_peaks_bytes": memory_peaks,
            "host_cpu_average_percent": round(sum(host_cpu) / len(host_cpu), 2) if host_cpu else None,
            "host_cpu_max_percent": max(host_cpu) if host_cpu else None,
            "minimum_disk_free_ratio": telemetry.minimum_disk_free_ratio if telemetry else None,
            "log_file_bytes_at_last_sample": log_bytes,
            "sample_artifact_bytes": telemetry.writer.path.stat().st_size if telemetry and telemetry.writer.path.exists() else 0,
            "sample_error_categories": telemetry.sample_error_counts if telemetry else {},
        },
        "secret_leak_found": bool(telemetry and telemetry.secret_leak_found),
        "log_audit_complete": bool(telemetry and telemetry.logs_audited),
        "shutdown": dict(shutdown),
        "startup_error_category": startup_error,
        "gates": gates,
    }
    return report


def _run_acceptance(
    *,
    duration_seconds: int = ACCEPTANCE_DURATION_SECONDS,
    diagnostic_only: bool = False,
) -> int:
    run_uuid = uuid4()
    project = acceptance_project_name(run_uuid)
    run_id = project.rsplit("-", 1)[-1]
    image = f"quant-data-layer-acceptance:{run_id}"
    artifact_directory = ARTIFACT_ROOT / run_id
    sample_path = artifact_directory / "samples.jsonl"
    report_path = artifact_directory / "report.json"
    config: dict[str, str] = {}
    secret_values: tuple[str, ...] = ()
    configured_keys: tuple[str, ...] = ()
    session: ComposeSession | None = None
    telemetry: RuntimeTelemetry | None = None
    container_ids: dict[str, str] = {}
    sampling_result: dict[str, Any] = {
        "status": "NOT_STARTED",
        "sample_count": 0,
        "missed_intervals": 0,
        "elapsed_seconds": 0.0,
        "stop_reason": None,
    }
    shutdown: dict[str, Any] = {
        "clean_shutdown": False,
        "cleanup_status": "NOT_STARTED",
        "final_database_probe": {"status": "NOT_EXPOSED"},
        "service_exit_states": {},
        "preserved_volume_names": [],
    }
    startup_error: str | None = None
    build_seconds: float | None = None
    startup_seconds: float | None = None
    proxy_preflight_status = "NOT_CHECKED"
    compose_up_attempted = False
    report_ready = False
    old_handlers: dict[int, Any] = {}

    try:
        _git_secret_file_preflight()
        report_ready = True
        config = load_public_source_config()
        configured_keys = tuple(config)
        secret_values = _configured_secret_values(config)
        _preflight_public_config(config)
        compose_env = {
            **config,
            "DATA_LAYER_ACCEPTANCE_IMAGE": image,
            "DATA_LAYER_ACCEPTANCE_RUN_ID": run_id,
            "DATA_LAYER_ACCEPTANCE_PROJECT": project,
            "DATA_LAYER_ACCEPTANCE_VOLUME_NAME": f"{project}_postgres_data",
        }
        model = render_compose_model(project, env=compose_env)
        validate_compose_model(model)
        try:
            _docker_proxy_preflight()
        except AcceptanceError:
            proxy_preflight_status = "FAIL"
            raise
        proxy_preflight_status = "PASS"
        _assert_no_project_collision(project)
        docker_info = _docker_output(
            ["info", "--format", "{{.ServerVersion}}|{{.DockerRootDir}}"],
            timeout_seconds=15,
            max_output_bytes=4096,
        )
        if not docker_info or "|" not in docker_info:
            raise AcceptanceError("DOCKER_DAEMON_UNAVAILABLE")
        disk = shutil.disk_usage(ROOT)
        if not disk.total or disk.free / disk.total < MIN_DISK_FREE_RATIO:
            raise AcceptanceError("DISK_FREE_BELOW_15_PERCENT")

        artifact_directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        os.chmod(artifact_directory, 0o700)
        writer = BoundedJsonlWriter(
            sample_path,
            max_record_bytes=MAX_SAMPLE_BYTES,
            max_artifact_bytes=MAX_ARTIFACT_BYTES,
            secret_values=secret_values,
        )
        docker = _docker_executable()
        build_started = time.monotonic()
        _run_bounded_process(
            [
                docker, "build", "--quiet", "--tag", image,
                "--label", f"quant.owner={OWNER}",
                "--label", f"quant.project={project}",
                "--label", f"quant.run_id={run_id}",
                str(ROOT),
            ],
            timeout_seconds=600,
            max_output_bytes=32 * 1024,
        )
        build_seconds = time.monotonic() - build_started
        disk = shutil.disk_usage(ROOT)
        if not disk.total or disk.free / disk.total < MIN_DISK_FREE_RATIO:
            raise AcceptanceError("DISK_FREE_BELOW_15_PERCENT_AFTER_BUILD")

        session = ComposeSession(project, compose_env)
        startup_started = time.monotonic()
        compose_up_attempted = True
        session.run("up", "--detach", timeout_seconds=120, max_output_bytes=32 * 1024)
        container_ids = _wait_for_runtime_ready(session, run_id=run_id)
        startup_seconds = time.monotonic() - startup_started
        telemetry = RuntimeTelemetry(
            container_ids=container_ids,
            writer=writer,
            secret_values=secret_values,
        )
        interrupt_requested = {"value": False}

        def request_safe_interrupt(_signum, _frame) -> None:
            interrupt_requested["value"] = True

        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, request_safe_interrupt)

        if diagnostic_only:
            print(
                f"Targeted runtime diagnostic starting: {duration_seconds}s; "
                "not Stage 8 acceptance; public/read-only; paper mode.",
                flush=True,
            )
        else:
            print("Stage 8 runtime window starting: fixed 900 seconds; public/read-only; paper mode.", flush=True)

        def sample_and_report(sequence: int, scheduled_offset: int) -> None:
            telemetry.sample(sequence, scheduled_offset)
            if sequence % 6 == 0:
                print(
                    f"Runtime sample {sequence}/{len(scheduled_sample_offsets(duration_seconds))}; "
                    f"scheduled_offset={scheduled_offset}s; hard safety remains active.",
                    flush=True,
                )

        sampling_result = run_sampling_window(
            sample_and_report,
            hard_stop=telemetry.hard_stop,
            deadline=RunDeadline(duration_seconds=duration_seconds),
            stop_requested=lambda: interrupt_requested["value"],
        )
    except KeyboardInterrupt:
        sampling_result = {
            "status": "INTERRUPTED",
            "sample_count": telemetry.sample_count if telemetry else 0,
            "missed_intervals": 0,
            "elapsed_seconds": 0.0,
            "stop_reason": "INTERRUPT_REQUESTED",
        }
    except AcceptanceError as exc:
        startup_error = str(exc)
    except Exception:
        startup_error = "UNEXPECTED_PRE_RUNTIME_FAILURE"
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        if session is not None and compose_up_attempted:
            try:
                shutdown = _shutdown_runtime(session, project=project, run_id=run_id)
            except Exception:
                shutdown = {
                    "clean_shutdown": False,
                    "cleanup_status": "CLEANUP_FAILED",
                    "final_database_probe": {"status": "ERROR", "error_category": "CLEANUP_FAILED"},
                    "service_exit_states": {},
                    "preserved_volume_names": [],
                }
        if session is not None:
            session.close()

    if report_ready:
        try:
            report = _build_final_report(
                run_id=run_id,
                project=project,
                image=image,
                configured_keys=configured_keys,
                telemetry=telemetry,
                sampling_result=sampling_result,
                shutdown=shutdown,
                startup_error=startup_error,
                build_seconds=build_seconds,
                startup_seconds=startup_seconds,
                proxy_preflight_status=proxy_preflight_status,
                run_mode="TARGETED_DIAGNOSTIC" if diagnostic_only else "STAGE8_ACCEPTANCE",
            )
            _write_json_report(report_path, report, secret_values)
        except Exception:
            report = None
    else:
        report = None

    if report is None:
        print(
            "DATA_LAYER_V1_TARGETED_RUNTIME_REPORT"
            if diagnostic_only else "DATA_LAYER_V1_STAGE8_ACCEPTANCE_REPORT"
        )
        print("status=FAIL")
        print(f"blocker={startup_error or 'SAFE_REPORT_NOT_WRITTEN'}")
        print("secrets=REDACTED")
        return 1

    print(
        "DATA_LAYER_V1_TARGETED_RUNTIME_REPORT"
        if diagnostic_only else "DATA_LAYER_V1_STAGE8_ACCEPTANCE_REPORT"
    )
    print(f"run_id={run_id}")
    print(f"acceptance_status={report['acceptance_status']}")
    print(f"sample_count={report['sample_count']}")
    print(f"runtime_window_status={sampling_result['status']}")
    print(f"missed_intervals={report['missed_intervals']}")
    print(f"postgres_size_growth_bytes={report['database']['measured_size_growth_bytes']}")
    print(f"host_cpu_average_percent={report['resources']['host_cpu_average_percent']}")
    print(f"minimum_disk_free_ratio={report['resources']['minimum_disk_free_ratio']}")
    print(f"secret_leak_found={report['secret_leak_found']}")
    print(f"clean_shutdown={report['shutdown']['clean_shutdown']}")
    print(f"evidence_directory={artifact_directory}")
    print(f"report_file={report_path}")
    print("TRADING_MODE=paper")
    print("private_api=NOT_USED")
    print("live_trading=NOT_USED")
    print(f"proxy_used={'YES' if report['network']['proxy_used'] else 'NO'}")
    print(f"proxy_preflight={report['network']['proxy_preflight']}")
    if startup_error:
        print(f"blocker={startup_error}")
    if diagnostic_only:
        print("targeted_runtime_diagnostic_only=true")
        print("stage8_acceptance=NOT_EVALUATED")
        print(
            "phase_statuses="
            + ",".join(f"{name}:{status}" for name, status in sorted(report["phase_statuses"].items()))
        )
        print(
            "phase_gates="
            + ",".join(
                f"{gate['gate']}:{gate['status']}"
                for gate in report["gates"]
                if gate["gate"].startswith("phase")
            )
        )
        return 0 if sampling_result["status"] == "COMPLETED" and not startup_error else 1
    if report["acceptance_status"] == "PASS":
        print("DATA_LAYER_V1_STAGE8_ACCEPTED=true")
        return 0
    print("DATA_LAYER_V1_STAGE8_ACCEPTED=false")
    print("failed_gates=" + ",".join(gate["gate"] for gate in report["gates"] if gate["status"] not in {"PASS", "SKIPPED"}))
    return 1


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(description="Run Data Layer V1 Stage 8 acceptance or a short diagnostic-only runtime.")
    parser.add_argument(
        "--targeted-seconds",
        type=int,
        metavar="SECONDS",
        help="run a 120–300 second diagnostic window; this never evaluates or claims Stage 8 acceptance",
    )
    args = parser.parse_args(list(argv))
    if args.targeted_seconds is None:
        return _run_acceptance()
    if not 120 <= args.targeted_seconds <= 300:
        parser.error("--targeted-seconds must be between 120 and 300")
    return _run_acceptance(duration_seconds=args.targeted_seconds, diagnostic_only=True)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
