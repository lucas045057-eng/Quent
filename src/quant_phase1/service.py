"""Shared persistence and runtime health helpers for Phase 1 services."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from quant_data_layer.errors import NormalizedError, normalize_error

from .contracts import DataStatus, Observation
from .db import assert_schema_ready
from .repositories import Phase1Repository
from .runtime import RuntimeHealthTracker, RuntimeHealthSnapshot
from .time import utc_now


LOGGER = logging.getLogger("quant_phase1")


def health_file(component: str) -> Path:
    return Path(f"/tmp/{component}.health")


def write_health_file(component: str, state: str, *, reason: str | None = None) -> None:
    path = health_file(component)
    payload = f"{state}\n{utc_now().isoformat()}\n{reason or ''}\n"
    try:
        path.write_text(payload, encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("health_file_write_failed error=%s", normalize_error(exc).safe_summary)


def persist_market_batch(
    repository: Phase1Repository,
    batch: Any,
    *,
    retention_days: dict[str, int] | None = None,
    market_snapshot_retention_days: int | None = None,
) -> int:
    repository.upsert_instruments(batch.instruments)
    repository.insert_market_snapshots(batch.tickers, batch.collected_at)
    for metric, values in {
        "last_price": [ticker.last_price for ticker in batch.tickers],
        "volume24h": [ticker.volume24h for ticker in batch.tickers],
        "turnover24h": [ticker.turnover24h for ticker in batch.tickers],
    }.items():
        repository.insert_market_observations(
            metric,
            [
                Observation(
                    symbol=ticker.symbol,
                    value=value,
                    source=ticker.source,
                    exchange=ticker.exchange,
                    exchange_timestamp=ticker.exchange_timestamp,
                    fetched_at=ticker.fetched_at,
                    processed_at=ticker.processed_at,
                    status=ticker.status,
                    raw_payload=ticker.raw_payload,
                )
                for ticker, value in zip(batch.tickers, values, strict=True)
            ],
        )
    candles = [
        candle
        for symbol_candles in batch.candles_by_symbol.values()
        for candles in symbol_candles.values()
        for candle in candles
        if candle.is_closed
    ]
    repository.upsert_candles(candles)
    # Retention runs on bootstrap/universe refresh batches only.  The high
    # frequency WS ticker persistence path must not issue a DELETE every few
    # seconds.
    if batch.instruments:
        if retention_days:
            repository.cleanup_klines(retention_days)
        if market_snapshot_retention_days:
            repository.cleanup_market_snapshots(market_snapshot_retention_days)
    return len(candles)


def persist_stage1(
    repository: Phase1Repository, batch: Any, results: Iterable[Any], *,
    stage1_candidate_ttl_seconds: int | None = None,
) -> int:
    from quant_phase9.intake import (
        Stage1IntakeTTLPolicyV1, persist_stage1_candidate_with_event,
        resolve_stage1_candidate_expiry,
    )

    ttl_policy = Stage1IntakeTTLPolicyV1(stage1_candidate_ttl_seconds)
    results = tuple(results)
    versions = {getattr(result,"strategy_version",None) for result in results}
    if len(versions)>1:raise ValueError("MIXED_STRATEGY_PRODUCERS")
    rule_version = "QUANT_PAPER_V2" if versions=={"QUANT_PAPER_V2"} else "phase1-basic-v1"
    run_id = repository.create_screening_run(batch.collected_at, rule_version=rule_version)
    count = 0
    for result in results:
        persist_stage1_candidate_with_event(
            repository.connection,
            run_id=run_id,
            screening_result=result,
            candidate_created_at=batch.collected_at,
            candidate_valid_until=resolve_stage1_candidate_expiry(
                batch.collected_at, policy=ttl_policy
            ),
            stage1_policy_version=rule_version,
            source_as_of=batch.collected_at,
        )
        count += 1
    return count


def persist_health(
    repository: Phase1Repository,
    component: str,
    tracker: RuntimeHealthTracker,
    *,
    details: dict[str, Any],
    recovery: RuntimeHealthSnapshot | None = None,
    degraded_grace_seconds: float = 0.0,
) -> None:
    checked_at = utc_now()
    # A self-healed blip (e.g. a WebSocket 1006 that recovers in ~1s) must not
    # poison the heartbeat consumed by zero-tolerance readiness gates.  Only a
    # DEGRADED state sustained beyond the grace window is reported as ERROR;
    # the raw runtime_state and outage age stay visible in details either way,
    # and a component that stops persisting entirely is still caught by the
    # consumer's heartbeat-age ceiling.  The grace never widens any data
    # freshness bound.
    outage_age = (
        (checked_at - tracker.outage_started_at).total_seconds()
        if tracker.state != "RUNNING" and tracker.outage_started_at is not None
        else None
    )
    within_grace = (
        outage_age is not None
        and degraded_grace_seconds > 0
        and 0 <= outage_age < degraded_grace_seconds
    )
    merged = {**details, "runtime_state": tracker.state}
    if within_grace:
        merged["degraded_within_grace"] = True
        merged["outage_age_seconds"] = outage_age
    repository.upsert_system_health(
        component,
        DataStatus.AVAILABLE if tracker.state == "RUNNING" or within_grace else DataStatus.ERROR,
        checked_at,
        merged,
    )
    if recovery is not None:
        repository.insert_runtime_health_event(
            component,
            "RUNNING",
            outage_started_at=recovery.outage_started_at,
            recovered_at=recovery.checked_at,
            reason=recovery.last_error.safe_summary if recovery.last_error is not None else None,
            details={"event": "recovered"},
        )


def mark_degraded(
    tracker: RuntimeHealthTracker,
    component: str,
    reason: str | BaseException | NormalizedError,
) -> NormalizedError:
    snapshot = tracker.mark_degraded(reason)
    current = snapshot or tracker.snapshot()
    normalized = current.last_error
    safe_reason = normalized.safe_summary if normalized is not None else "UNKNOWN.UNKNOWNERROR"
    write_health_file(component, "DEGRADED", reason=safe_reason)
    if snapshot is not None:
        LOGGER.warning(
            "runtime_health_degraded category=%s exception_type=%s code=%s",
            normalized.category.value if normalized else "UNKNOWN",
            normalized.exception_type if normalized else "UNKNOWNERROR",
            normalized.code if normalized and normalized.code else "NONE",
            extra={"event_id": "runtime_health", "status": "DEGRADED", "component": component},
        )
    return normalized or normalize_error(RuntimeError("unknown runtime health error"))


def mark_running(tracker: RuntimeHealthTracker, component: str) -> RuntimeHealthSnapshot | None:
    recovery = tracker.mark_running()
    write_health_file(component, "RUNNING")
    if recovery is not None:
        LOGGER.info(
            "runtime_health_recovered",
            extra={"event_id": "runtime_health", "status": "RUNNING", "component": component},
        )
    return recovery


def open_repository(connection_factory: Any, dsn: str) -> tuple[Any, Phase1Repository]:
    connection = connection_factory(dsn)
    assert_schema_ready(connection)
    return connection, Phase1Repository(connection)
