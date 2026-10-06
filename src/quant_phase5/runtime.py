"""Bounded, restart-safe Phase 5 context cycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections import Counter, deque
from typing import Any, Callable, Iterable, Mapping

from psycopg.types.json import Jsonb

from quant_phase1.pipeline import MarketDataBatch
from quant_phase1.stage1 import Stage1Result
from quant_phase1.time import ensure_utc, utc_now

from .breadth import compute_market_breadth
from .contracts import ContextStatus
from .enrichment import enrich_stage1_context
from .health import Phase5HealthRegistry
from .market_context import build_market_leader_context
from .persistence import Phase5Repository
from .reliability import BoundedContextCache
from .regime import compute_market_regime
from .relative_strength import compute_relative_strength
from .sector_context import compute_sector_context
from .sector_taxonomy import load_sector_taxonomy, resolve_sector


@dataclass(frozen=True, slots=True)
class Phase5CycleResult:
    loaded: int
    persisted: int
    errors: int


class Phase5Runtime:
    def __init__(
        self,
        settings: Any | None = None,
        *,
        cache_capacity: int = 512,
        health_writer: Callable[[Any], None] | None = None,
        health_event_writer: Callable[[Any, Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings
        self.loaded_context = BoundedContextCache(capacity=cache_capacity)
        self.health = Phase5HealthRegistry()
        self.health_writer = health_writer
        self.health_event_writer = health_event_writer

    def _publish_running(self, snapshot: Any, previous: Any) -> None:
        if self.health_writer is not None:
            self.health_writer(snapshot)
        if (
            self.health_event_writer is not None
            and previous.status in {ContextStatus.ERROR, ContextStatus.STALE}
        ):
            self.health_event_writer(
                snapshot,
                {
                    "event": "recovered",
                    "outage_started_at": previous.checked_at,
                    "reason": previous.details.get("reason"),
                },
            )

    def _load_rows(self, rows: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
        self.loaded_context = BoundedContextCache(capacity=self.loaded_context.capacity)
        loaded: deque[dict[str, Any]] = deque(maxlen=self.loaded_context.capacity)
        for row in rows:
            if not isinstance(row, dict):
                raise TypeError("Phase 5 context row must be a mapping")
            key = str(row.get("key") or row.get("symbol") or "")
            if not key:
                raise ValueError("Phase 5 context row requires a bounded key")
            self.loaded_context.put(key, row)
            loaded.append(row)
        return tuple(loaded)

    def rebuild_loaded_context(self, rows: Iterable[dict[str, Any]]) -> int:
        """Rebuild at most cache_capacity rows, preserving source statuses."""
        return len(self._load_rows(rows))

    def rebuild_from_connection(self, connection: Any, *, limit: int | None = None) -> tuple[dict[str, Any], ...]:
        row_limit = min(limit or self.loaded_context.capacity, self.loaded_context.capacity)
        if row_limit <= 0:
            raise ValueError("row limit must be positive")
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT symbol, timeframe, context_timestamp, status
            FROM phase5_market_leader_context
            ORDER BY processed_at DESC, id DESC
            LIMIT %s
            """,
            (row_limit,),
        )
        rows = []
        for symbol, timeframe, context_timestamp, status in cursor.fetchall():
            rows.append({
                "key": f"{symbol}:{timeframe}:{context_timestamp.isoformat()}",
                "symbol": symbol,
                "timeframe": timeframe,
                "context_timestamp": context_timestamp,
                "status": status,
            })
        self._load_rows(rows)
        return tuple(rows[-self.loaded_context.capacity:])

    def run_cycle(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        persist: Callable[[tuple[dict[str, Any], ...]], int | None] | None = None,
        now: datetime | None = None,
        context_status: ContextStatus | str | None = None,
        health_details: Mapping[str, Any] | None = None,
    ) -> Phase5CycleResult:
        checked_at = ensure_utc(now or utc_now())
        try:
            previous = self.health.snapshot("phase5_context")
            loaded_rows = self._load_rows(rows)
            persisted = 0
            if persist is not None:
                persisted_value = persist(loaded_rows)
                persisted = len(loaded_rows) if persisted_value is None else int(persisted_value)
            status = ContextStatus(context_status) if context_status is not None else ContextStatus.AVAILABLE
            details = {"heartbeat": True, "loaded": len(loaded_rows), "persisted": persisted}
            details.update(dict(health_details or {}))
            previous_evidence = previous.details.get("freshness_evidence", {})
            current_evidence = details.get("freshness_evidence", {})
            if status is ContextStatus.STALE:
                details["stale_since"] = (
                    previous.details.get("stale_since")
                    if previous.status is ContextStatus.STALE and previous.details.get("stale_since")
                    else checked_at.isoformat()
                )
            if isinstance(current_evidence, Mapping):
                normalized_evidence = {}
                for source_key, item in current_evidence.items():
                    if not isinstance(item, Mapping):
                        continue
                    value = dict(item)
                    if str(value.get("status", "")).upper() == ContextStatus.STALE.value:
                        old = previous_evidence.get(source_key, {}) if isinstance(previous_evidence, Mapping) else {}
                        old_since = old.get("stale_since") if isinstance(old, Mapping) else None
                        value["stale_since"] = old_since or checked_at.isoformat()
                    normalized_evidence[str(source_key)] = value
                details["freshness_evidence"] = normalized_evidence
            snapshot = self.health.mark_status(
                "phase5_context",
                status,
                checked_at,
                details,
            )
            self._publish_running(snapshot, previous)
            return Phase5CycleResult(len(loaded_rows), persisted, 0)
        except Exception as exc:  # noqa: BLE001 - cycle boundary isolates context failure
            snapshot = self.health.mark_degraded("phase5_context", checked_at, str(exc))
            if self.health_writer is not None:
                try:
                    self.health_writer(snapshot)
                except Exception:
                    pass
            return Phase5CycleResult(0, 0, 1)

    def run_from_connection(
        self,
        connection: Any,
        *,
        persist: Callable[[tuple[dict[str, Any], ...]], int | None] | None = None,
        now: datetime | None = None,
    ) -> Phase5CycleResult:
        checked_at = ensure_utc(now or utc_now())
        try:
            rows = self.rebuild_from_connection(connection)
        except Exception as exc:  # noqa: BLE001 - database boundary is health-isolated
            snapshot = self.health.mark_degraded("phase5_context", checked_at, str(exc))
            if self.health_writer is not None:
                try:
                    self.health_writer(snapshot)
                except Exception:
                    pass
            return Phase5CycleResult(0, 0, 1)
        return self.run_cycle(rows, persist=persist, now=checked_at)


def _ensure_universe_run(connection: Any, batch: MarketDataBatch, processed_at: datetime) -> int:
    """Create/reuse the immutable universe identity for this input batch."""
    symbols = tuple(dict.fromkeys(batch.selected_symbols))
    if not symbols:
        raise ValueError("Phase 5 requires a non-empty selected universe")
    ticker_by_symbol = {ticker.symbol: ticker for ticker in batch.tickers}
    cursor = connection.cursor()
    cursor.execute(
        """
        SELECT id FROM universe_runs
        WHERE run_timestamp = %s AND source = 'phase5-runtime'
        ORDER BY id DESC LIMIT 1
        """,
        (batch.collected_at,),
    )
    row = cursor.fetchone()
    if row is None:
        cursor.execute(
            """
            INSERT INTO universe_runs (run_timestamp, source, status, parameters)
            VALUES (%s, 'phase5-runtime', 'AVAILABLE', %s)
            RETURNING id
            """,
            (batch.collected_at, Jsonb({"limit": len(symbols), "processed_at": processed_at.isoformat()})),
        )
        row = cursor.fetchone()
    universe_run_id = int(row[0])
    cursor.executemany(
        """
        INSERT INTO universe_members (run_id, symbol, rank, turnover)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (run_id, symbol) DO UPDATE SET rank = EXCLUDED.rank, turnover = EXCLUDED.turnover
        """,
        [
            (universe_run_id, symbol, rank, ticker_by_symbol[symbol].turnover24h if symbol in ticker_by_symbol else None)
            for rank, symbol in enumerate(symbols, start=1)
        ],
    )
    return universe_run_id


def _aligned(context: Any, timestamp: datetime) -> Any | None:
    if context is None or context.context_timestamp != timestamp:
        return None
    return context


def run_phase5_data_cycle(
    batch: MarketDataBatch,
    results: Iterable[Stage1Result],
    settings: Any,
    *,
    connection: Any,
    screening_run_id: int,
    processed_at: datetime,
) -> dict[str, Any]:
    """Execute the complete Phase 5 calculation and persistence chain.

    Every layer is derived from the same canonical closed-bar batch and the
    same immutable universe run.  Missing/stale inputs are persisted as
    explicit contracts; they are never replaced by zeros or synthetic data.
    """
    processed_at = ensure_utc(processed_at)
    results = tuple(results)
    universe_symbols = tuple(dict.fromkeys(batch.selected_symbols))
    universe_run_id = _ensure_universe_run(connection, batch, processed_at)
    repository = Phase5Repository(connection, max_evidence_bytes=settings.phase5_max_evidence_bytes)

    memberships = load_sector_taxonomy(settings.phase5_taxonomy_path)
    mapping_versions = {row.mapping_version for row in memberships}
    if len(mapping_versions) != 1:
        raise ValueError("Phase 5 runtime requires one explicit taxonomy mapping_version")
    mapping_version = next(iter(mapping_versions))
    resolved_memberships = tuple(
        resolve_sector(symbol, memberships, processed_at, mapping_version=mapping_version)
        for symbol in universe_symbols
    )
    fallback_memberships = tuple(
        row for row in resolved_memberships if row.status.value != "AVAILABLE"
    )
    runtime_memberships = (*memberships, *fallback_memberships)
    repository.insert_sector_membership(runtime_memberships)

    contexts_by_timeframe: dict[str, dict[str, Any]] = {}
    leader_rows = []
    for timeframe in ("5m", "15m", "1H", "4H"):
        contexts: dict[str, Any] = {}
        for symbol in universe_symbols:
            contexts[symbol] = build_market_leader_context(
                batch.candles_by_symbol.get(symbol, {}).get(timeframe, ()),
                settings,
                now=processed_at,
                processed_at=processed_at,
                symbol=symbol,
                timeframe=timeframe,
                allow_non_leader=True,
            )
        contexts_by_timeframe[timeframe] = contexts
        for leader in ("BTCUSDT", "ETHUSDT"):
            context = contexts.get(leader)
            if context is None:
                context = build_market_leader_context(
                    (), settings, now=processed_at, processed_at=processed_at,
                    symbol=leader, timeframe=timeframe,
                )
            leader_rows.append(context)
    freshness_evidence: dict[str, dict[str, Any]] = {}
    for context in leader_rows:
        quality = dict(context.data_quality)
        source_key = f"{context.symbol}:{context.timeframe}"
        freshness_evidence[source_key] = {
            "symbol": context.symbol,
            "timeframe": context.timeframe,
            "status": context.status.value,
            "source_timestamp": quality.get("source_timestamp"),
            "exchange_timestamp": quality.get("exchange_timestamp"),
            "fetched_at": quality.get("fetched_at"),
            "checked_at": quality.get("checked_at", processed_at.isoformat()),
            "freshness_age_seconds": quality.get("freshness_age_seconds"),
            "freshness_threshold_seconds": quality.get("freshness_threshold_seconds"),
            "expected_latest_closed_at": quality.get("expected_latest_closed_at"),
            "context_timestamp": quality.get("context_timestamp"),
            "context_age_seconds": quality.get("context_age_seconds"),
            "reason": quality.get("reason") or (
                context.reason_code.value if context.reason_code is not None else None
            ),
            "last_success_at": quality.get("fetched_at") if context.source_count else None,
        }
    for timeframe, contexts in contexts_by_timeframe.items():
        values = tuple(contexts.values())
        stale_values = [
            context for context in values if context.status is ContextStatus.STALE
        ]
        candidates = stale_values or list(values)
        selected = max(
            candidates,
            key=lambda item: float(item.data_quality.get("freshness_age_seconds") or 0),
            default=None,
        )
        quality = dict(selected.data_quality) if selected is not None else {}
        freshness_evidence[f"timeframe:{timeframe}"] = {
            "timeframe": timeframe,
            "status": ContextStatus.STALE.value if stale_values else _aggregate_phase5_status(values).value,
            "source_timestamp": quality.get("source_timestamp"),
            "exchange_timestamp": quality.get("exchange_timestamp"),
            "fetched_at": quality.get("fetched_at"),
            "checked_at": quality.get("checked_at", processed_at.isoformat()),
            "freshness_age_seconds": quality.get("freshness_age_seconds"),
            "freshness_threshold_seconds": quality.get("freshness_threshold_seconds"),
            "expected_latest_closed_at": quality.get("expected_latest_closed_at"),
            "context_timestamp": quality.get("context_timestamp"),
            "context_age_seconds": quality.get("context_age_seconds"),
            "reason": quality.get("reason") or (
                "SOURCE_NOT_ADVANCING" if stale_values else None
            ),
            "last_success_at": max(
                (
                    context.data_quality.get("fetched_at")
                    for context in values
                    if context.data_quality.get("fetched_at") and context.source_count
                ),
                default=None,
            ),
            "source_count": len(values),
            "available_source_count": sum(context.status is ContextStatus.AVAILABLE for context in values),
            "stale_source_count": len(stale_values),
        }
    leader_count = repository.insert_leader_context(leader_rows)

    breadth_rows = []
    regime_rows = []
    relative_rows = []
    sector_rows = []
    enrichments = []
    sector_contexts: dict[tuple[str, str], Any] = {}
    relative_contexts: dict[tuple[str, str, str], Any] = {}

    for timeframe in ("5m", "15m", "1H", "4H"):
        contexts = contexts_by_timeframe[timeframe]
        leaders = {symbol: contexts.get(symbol) for symbol in ("BTCUSDT", "ETHUSDT")}
        leader_timestamps = [
            value.context_timestamp for value in leaders.values()
            if value is not None and value.status in {ContextStatus.AVAILABLE, ContextStatus.STALE}
        ]
        context_timestamp = min(leader_timestamps) if leader_timestamps else processed_at
        aligned_contexts = {
            symbol: context for symbol, context in contexts.items()
            if context.context_timestamp == context_timestamp
        }
        breadth = compute_market_breadth(
            universe_symbols, aligned_contexts, timeframe=timeframe,
            context_timestamp=context_timestamp, universe_run_id=universe_run_id,
            settings=settings, processed_at=processed_at,
        )
        breadth_rows.append(breadth)
        # A skewed leader is omitted from the regime input.  Never rewrite an
        # observed context timestamp merely to make identities line up.
        regime_leaders = {
            symbol: context
            for symbol, context in leaders.items()
            if context is not None and context.context_timestamp == context_timestamp
        }
        regime = compute_market_regime(
            {symbol: context for symbol, context in regime_leaders.items() if context is not None},
            breadth, timeframe=timeframe, context_timestamp=context_timestamp,
            settings=settings, processed_at=processed_at,
        )
        regime_rows.append(regime)
        for symbol in universe_symbols:
            candidate = _aligned(contexts.get(symbol), context_timestamp)
            benchmark_contexts = {
                benchmark: _aligned(contexts.get(benchmark), context_timestamp)
                for benchmark in ("BTCUSDT", "ETHUSDT")
            }
            if timeframe not in {"15m", "1H", "4H"}:
                continue
            for benchmark in ("BTCUSDT", "ETHUSDT", "MARKET_UNIVERSE_EQUAL_WEIGHT"):
                relative = compute_relative_strength(
                    symbol, candidate, benchmark, benchmark_contexts.get(benchmark),
                    timeframe=timeframe, context_timestamp=context_timestamp,
                    processed_at=processed_at, settings=settings,
                    universe_run_id=universe_run_id if benchmark == "MARKET_UNIVERSE_EQUAL_WEIGHT" else None,
                    universe_contexts=aligned_contexts if benchmark == "MARKET_UNIVERSE_EQUAL_WEIGHT" else None,
                )
                relative_rows.append(relative)
                relative_contexts[(symbol, timeframe, benchmark)] = relative

        if timeframe in {"15m", "1H", "4H"}:
            sectors = set(row.sector for row in memberships) | {"UNKNOWN"}
            for sector in sorted(sectors):
                sector_context = compute_sector_context(
                    sector, mapping_version, universe_symbols, runtime_memberships,
                    aligned_contexts, timeframe=timeframe, context_timestamp=context_timestamp,
                    universe_run_id=universe_run_id, settings=settings, processed_at=processed_at,
                )
                sector_rows.append(sector_context)
                sector_contexts[(sector, timeframe)] = sector_context

    repository.insert_regime_snapshots(regime_rows)
    repository.insert_relative_strength(relative_rows)
    repository.insert_sector_context(sector_rows)

    enrichment_timeframe = "15m"
    enrichment_regime = regime_rows[1]
    for result in results:
        symbol = str(result.symbol).upper()
        candidate_context = contexts_by_timeframe[enrichment_timeframe].get(symbol)
        anchor = enrichment_regime.context_timestamp
        candidate_context = _aligned(candidate_context, anchor)
        membership = resolve_sector(symbol, runtime_memberships, anchor, mapping_version=mapping_version)
        sector_context = sector_contexts.get((membership.sector, enrichment_timeframe))
        enrichment = enrich_stage1_context(
            result,
            screening_run_id=screening_run_id,
            leader_context=candidate_context,
            regime_context=enrichment_regime,
            relative_strength=relative_contexts.get((symbol, enrichment_timeframe, "MARKET_UNIVERSE_EQUAL_WEIGHT")),
            sector_context=sector_context,
            sector_membership=membership,
            timeframe=enrichment_timeframe,
            processed_at=processed_at,
            settings=settings,
        )
        enrichments.append(enrichment)
    enrichment_count = repository.insert_stage1_enrichment(enrichments)
    phase5_outputs = (*leader_rows, *breadth_rows, *regime_rows, *relative_rows, *sector_rows, *enrichments)
    health_diagnostics = summarize_phase5_health(phase5_outputs)
    return {
        "leader_context": leader_count,
        "breadth": len(breadth_rows),
        "regime": len(regime_rows),
        "relative_strength": len(relative_rows),
        "sector_membership": len(runtime_memberships),
        "sector_context": len(sector_rows),
        "stage1_enrichment": enrichment_count,
        "universe_run_id": universe_run_id,
        "phase5_status": ContextStatus(health_diagnostics["phase5_status"]),
        "health_diagnostics": health_diagnostics,
        "freshness_evidence": freshness_evidence,
    }


def _aggregate_phase5_status(rows: Iterable[Any]) -> ContextStatus:
    statuses = [
        getattr(row, "status", getattr(row, "context_status", ContextStatus.NOT_AVAILABLE))
        for row in rows
    ]
    statuses = [value if isinstance(value, ContextStatus) else ContextStatus(value) for value in statuses]
    if any(value is ContextStatus.ERROR for value in statuses):
        return ContextStatus.ERROR
    if any(value is ContextStatus.STALE for value in statuses):
        return ContextStatus.STALE
    if any(value is ContextStatus.PARTIAL for value in statuses):
        return ContextStatus.PARTIAL
    if any(value is ContextStatus.NOT_AVAILABLE for value in statuses):
        return ContextStatus.NOT_AVAILABLE
    return ContextStatus.AVAILABLE


def summarize_phase5_health(rows: Iterable[Any]) -> dict[str, Any]:
    """Return bounded scalar evidence explaining the aggregate context status."""
    materialized = tuple(rows)
    status_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    missing_evidence_count = 0
    for row in materialized:
        value = getattr(row, "status", getattr(row, "context_status", ContextStatus.NOT_AVAILABLE))
        status = value.value if isinstance(value, ContextStatus) else ContextStatus(value).value
        status_counts[status] += 1
        reason = getattr(row, "reason_code", None)
        if reason is not None:
            reason_counts[reason.value if hasattr(reason, "value") else str(reason)] += 1
        evidence = getattr(row, "missing_evidence", ())
        missing_evidence_count += len(evidence) if evidence else 0

    aggregate = _aggregate_phase5_status(materialized)
    data_quality = {
        ContextStatus.AVAILABLE: "AVAILABLE",
        ContextStatus.PARTIAL: "PARTIAL",
        ContextStatus.STALE: "STALE",
        ContextStatus.NOT_AVAILABLE: "NOT_AVAILABLE",
        ContextStatus.ERROR: "ERROR",
    }[aggregate]
    reason_code = None
    if reason_counts:
        reason_code = min(reason_counts, key=lambda item: (-reason_counts[item], item))
    if aggregate is ContextStatus.PARTIAL:
        reason = "PARTIAL_CONTEXT_OUTPUTS"
    elif aggregate is ContextStatus.STALE:
        reason = reason_code or "PHASE5_CONTEXT_STALE"
    elif aggregate is ContextStatus.NOT_AVAILABLE:
        reason = reason_code or "PHASE5_CONTEXT_NOT_AVAILABLE"
    elif aggregate is ContextStatus.ERROR:
        reason = reason_code or "PHASE5_CONTEXT_ERROR"
    else:
        reason = None
    return {
        "phase5_status": aggregate.value,
        "data_quality": data_quality,
        "reason": reason,
        "reason_code": reason_code,
        "output_count": len(materialized),
        "available_output_count": status_counts[ContextStatus.AVAILABLE.value],
        "partial_output_count": status_counts[ContextStatus.PARTIAL.value],
        "stale_output_count": status_counts[ContextStatus.STALE.value],
        "not_available_output_count": status_counts[ContextStatus.NOT_AVAILABLE.value],
        "error_output_count": status_counts[ContextStatus.ERROR.value],
        "missing_evidence_count": missing_evidence_count,
    }
