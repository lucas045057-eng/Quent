"""Bounded, idempotent persistence for Phase 5 derived context."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from typing import Any, Iterable, Mapping

from psycopg.types.json import Jsonb

from .contracts import (
    MarketLeaderContext,
    MarketRegimeSnapshot,
    RelativeStrengthSnapshot,
    SectorContextSnapshot,
    SectorMembership,
    Stage1Phase5Enrichment,
    MAX_EVIDENCE_BYTES,
    validate_bounded_mapping,
)


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _value(value: Any) -> Any:
    return getattr(value, "value", value)


def _json_compatible(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        return {key: _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_compatible(item) for item in value]
    return value


def _json(value: Mapping[str, Any] | Iterable[str], field_name: str = "evidence", max_bytes: int = MAX_EVIDENCE_BYTES) -> Jsonb:
    if isinstance(value, Mapping):
        bounded = validate_bounded_mapping(value, field_name, max_bytes)
        return Jsonb(_json_compatible(bounded))
    entries = tuple(value)
    if len(entries) > 64 or any(not isinstance(item, str) or not item.strip() for item in entries):
        raise ValueError(f"{field_name} must be bounded non-empty strings")
    encoded = json.dumps(entries, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError(f"{field_name} exceeds bounded evidence size")
    return Jsonb(list(entries))

def _enrichment_value(row: Any, name: str) -> Any:
    if isinstance(row, Mapping):
        return row[name]
    return getattr(row, name)


class Phase5Repository:
    """Persist only validated Phase 5 contracts and bounded references."""

    def __init__(self, connection: Any, *, max_evidence_bytes: int = MAX_EVIDENCE_BYTES) -> None:
        if max_evidence_bytes <= 0:
            raise ValueError("max_evidence_bytes must be positive")
        self.max_evidence_bytes = max_evidence_bytes
        self.connection = connection

    def _json(self, value: Mapping[str, Any] | Iterable[str], field_name: str = "evidence") -> Jsonb:
        return _json(value, field_name, self.max_evidence_bytes)

    def insert_leader_context(self, rows: Iterable[MarketLeaderContext]) -> int:
        values = [
            (
                row.symbol,
                row.timeframe,
                _utc(row.context_timestamp, "context_timestamp"),
                _utc(row.input_window_start, "input_window_start") if row.input_window_start else None,
                _utc(row.input_window_end, "input_window_end") if row.input_window_end else None,
                row.return_pct,
                _value(row.trend_state),
                _value(row.structure_state),
                _value(row.volatility_state),
                _value(row.volume_state),
                row.volatility_value,
                row.volume_ratio,
                _value(row.freshness_status),
                self._json(row.data_quality),
                row.source_count,
                row.missing_count,
                self._json(row.support_evidence),
                self._json(row.conflict_evidence),
                self._json(row.missing_evidence),
                _value(row.status),
                _value(row.reason_code) if row.reason_code else None,
                row.calculation_version,
                self._json(row.input_reference),
                _utc(row.processed_at, "processed_at"),
            )
            for row in rows
        ]
        if not values:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO phase5_market_leader_context (
                symbol, timeframe, context_timestamp, input_window_start, input_window_end,
                return_pct, trend_state, structure_state, volatility_state, volume_state,
                volatility_value, volume_ratio, freshness_status, data_quality, source_count,
                missing_count, support_evidence, conflict_evidence, missing_evidence, status,
                reason_code, calculation_version, input_reference, processed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (symbol, timeframe, context_timestamp, calculation_version) DO UPDATE SET
                input_window_start=EXCLUDED.input_window_start,
                input_window_end=EXCLUDED.input_window_end,
                return_pct=EXCLUDED.return_pct, trend_state=EXCLUDED.trend_state,
                structure_state=EXCLUDED.structure_state, volatility_state=EXCLUDED.volatility_state,
                volume_state=EXCLUDED.volume_state, volatility_value=EXCLUDED.volatility_value,
                volume_ratio=EXCLUDED.volume_ratio, freshness_status=EXCLUDED.freshness_status,
                data_quality=EXCLUDED.data_quality, source_count=EXCLUDED.source_count,
                missing_count=EXCLUDED.missing_count, support_evidence=EXCLUDED.support_evidence,
                conflict_evidence=EXCLUDED.conflict_evidence, missing_evidence=EXCLUDED.missing_evidence,
                status=EXCLUDED.status, reason_code=EXCLUDED.reason_code,
                input_reference=EXCLUDED.input_reference, processed_at=EXCLUDED.processed_at
            """,
            values,
        )
        return len(values)

    def insert_regime_snapshots(self, rows: Iterable[MarketRegimeSnapshot]) -> int:
        values = [
            (
                row.timeframe,
                _utc(row.context_timestamp, "context_timestamp"),
                _value(row.direction_regime),
                _value(row.volatility_regime),
                _value(row.breadth_regime),
                _value(row.direction_status),
                _value(row.volatility_status),
                _value(row.breadth_status),
                row.universe_run_id,
                row.sample_size,
                row.available_count,
                row.missing_count,
                row.coverage_ratio,
                self._json(row.support_evidence),
                self._json(row.conflict_evidence),
                self._json(row.missing_evidence),
                _value(row.status),
                _value(row.reason_code) if row.reason_code else None,
                row.calculation_version,
                self._json(row.input_reference),
                _utc(row.processed_at, "processed_at"),
            )
            for row in rows
        ]
        if not values:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO phase5_market_regime_snapshots (
                timeframe, context_timestamp, direction_regime, volatility_regime, breadth_regime,
                direction_status, volatility_status, breadth_status, universe_run_id, sample_size,
                available_count, missing_count, coverage_ratio, support_evidence, conflict_evidence,
                missing_evidence, status, reason_code, calculation_version, input_reference, processed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (
                timeframe, context_timestamp, (COALESCE(universe_run_id, 0)), calculation_version
            ) DO UPDATE SET
                direction_regime=EXCLUDED.direction_regime,
                volatility_regime=EXCLUDED.volatility_regime,
                breadth_regime=EXCLUDED.breadth_regime,
                direction_status=EXCLUDED.direction_status,
                volatility_status=EXCLUDED.volatility_status,
                breadth_status=EXCLUDED.breadth_status,
                sample_size=EXCLUDED.sample_size,
                available_count=EXCLUDED.available_count,
                missing_count=EXCLUDED.missing_count,
                coverage_ratio=EXCLUDED.coverage_ratio,
                support_evidence=EXCLUDED.support_evidence,
                conflict_evidence=EXCLUDED.conflict_evidence,
                missing_evidence=EXCLUDED.missing_evidence,
                status=EXCLUDED.status,
                reason_code=EXCLUDED.reason_code,
                input_reference=EXCLUDED.input_reference,
                processed_at=EXCLUDED.processed_at
            """,
            values,
        )
        return len(values)

    def insert_relative_strength(self, rows: Iterable[RelativeStrengthSnapshot]) -> int:
        values = [
            (
                row.symbol,
                row.benchmark,
                row.timeframe,
                _utc(row.context_timestamp, "context_timestamp"),
                row.candidate_return_pct,
                row.benchmark_return_pct,
                row.relative_return_pct,
                _value(row.relative_class),
                row.universe_run_id,
                row.sample_size,
                row.available_count,
                row.missing_count,
                row.coverage_ratio,
                _value(row.status),
                _value(row.reason_code) if row.reason_code else None,
                row.calculation_version,
                self._json(row.input_reference),
                self._json(row.missing_evidence),
                _utc(row.processed_at, "processed_at"),
            )
            for row in rows
        ]
        if not values:
            return 0
        cursor = self.connection.cursor()
        leader_values = [row for row in values if row[1] in {"BTCUSDT", "ETHUSDT"}]
        market_values = [row for row in values if row[1] == "MARKET_UNIVERSE_EQUAL_WEIGHT"]
        if leader_values:
            cursor.executemany(
                """
                INSERT INTO phase5_relative_strength_snapshots (
                    symbol, benchmark, timeframe, context_timestamp, candidate_return_pct,
                    benchmark_return_pct, relative_return_pct, relative_class, universe_run_id,
                    sample_size, available_count, missing_count, coverage_ratio, status, reason_code,
                    calculation_version, input_reference, missing_evidence, processed_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (
                    symbol, benchmark, timeframe, context_timestamp, calculation_version
                ) WHERE benchmark IN ('BTCUSDT','ETHUSDT') DO UPDATE SET
                    candidate_return_pct=EXCLUDED.candidate_return_pct,
                    benchmark_return_pct=EXCLUDED.benchmark_return_pct,
                    relative_return_pct=EXCLUDED.relative_return_pct,
                    relative_class=EXCLUDED.relative_class,
                    sample_size=EXCLUDED.sample_size,
                    available_count=EXCLUDED.available_count,
                    missing_count=EXCLUDED.missing_count,
                    coverage_ratio=EXCLUDED.coverage_ratio,
                    status=EXCLUDED.status,
                    reason_code=EXCLUDED.reason_code,
                    input_reference=EXCLUDED.input_reference,
                    missing_evidence=EXCLUDED.missing_evidence,
                    processed_at=EXCLUDED.processed_at
                """,
                leader_values,
            )
        if market_values:
            cursor.executemany(
                """
                INSERT INTO phase5_relative_strength_snapshots (
                    symbol, benchmark, timeframe, context_timestamp, candidate_return_pct,
                    benchmark_return_pct, relative_return_pct, relative_class, universe_run_id,
                    sample_size, available_count, missing_count, coverage_ratio, status, reason_code,
                    calculation_version, input_reference, missing_evidence, processed_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (
                    symbol, benchmark, timeframe, context_timestamp, universe_run_id, calculation_version
                ) WHERE benchmark = 'MARKET_UNIVERSE_EQUAL_WEIGHT' DO UPDATE SET
                    candidate_return_pct=EXCLUDED.candidate_return_pct,
                    benchmark_return_pct=EXCLUDED.benchmark_return_pct,
                    relative_return_pct=EXCLUDED.relative_return_pct,
                    relative_class=EXCLUDED.relative_class,
                    sample_size=EXCLUDED.sample_size,
                    available_count=EXCLUDED.available_count,
                    missing_count=EXCLUDED.missing_count,
                    coverage_ratio=EXCLUDED.coverage_ratio,
                    status=EXCLUDED.status,
                    reason_code=EXCLUDED.reason_code,
                    input_reference=EXCLUDED.input_reference,
                    missing_evidence=EXCLUDED.missing_evidence,
                    processed_at=EXCLUDED.processed_at
                """,
                market_values,
            )
        return len(values)

    def insert_sector_membership(self, rows: Iterable[SectorMembership]) -> int:
        values = [
            (
                row.mapping_version,
                row.symbol,
                row.sector,
                row.source_reference,
                _utc(row.effective_from, "effective_from"),
                _utc(row.effective_to, "effective_to") if row.effective_to else None,
                _value(row.status),
                _utc(row.processed_at, "processed_at"),
            )
            for row in rows
        ]
        if not values:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO phase5_sector_membership (
                mapping_version, symbol, sector, source_reference, effective_from,
                effective_to, status, processed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (mapping_version, symbol, effective_from) DO UPDATE SET
                sector=EXCLUDED.sector, source_reference=EXCLUDED.source_reference,
                effective_to=EXCLUDED.effective_to, status=EXCLUDED.status,
                processed_at=EXCLUDED.processed_at
            """,
            values,
        )
        return len(values)

    def insert_sector_context(self, rows: Iterable[SectorContextSnapshot]) -> int:
        values = [
            (
                row.sector,
                row.timeframe,
                _utc(row.context_timestamp, "context_timestamp"),
                row.universe_run_id,
                row.mapping_version,
                row.sector_return_pct,
                row.sector_positive_ratio,
                row.member_count,
                row.sample_size,
                row.available_count,
                row.missing_count,
                row.coverage_ratio,
                _value(row.status),
                _value(row.reason_code) if row.reason_code else None,
                self._json(row.input_reference),
                self._json(row.missing_evidence),
                row.calculation_version,
                _utc(row.processed_at, "processed_at"),
            )
            for row in rows
        ]
        if not values:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO phase5_sector_context_snapshots (
                sector, timeframe, context_timestamp, universe_run_id, mapping_version,
                sector_return_pct, sector_positive_ratio, member_count, sample_size,
                available_count, missing_count, coverage_ratio, status, reason_code,
                input_reference, missing_evidence, calculation_version, processed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (
                sector, timeframe, context_timestamp, (COALESCE(universe_run_id, 0)),
                mapping_version, calculation_version
            ) DO UPDATE SET
                sector_return_pct=EXCLUDED.sector_return_pct,
                sector_positive_ratio=EXCLUDED.sector_positive_ratio,
                member_count=EXCLUDED.member_count,
                sample_size=EXCLUDED.sample_size,
                available_count=EXCLUDED.available_count,
                missing_count=EXCLUDED.missing_count,
                coverage_ratio=EXCLUDED.coverage_ratio,
                status=EXCLUDED.status,
                reason_code=EXCLUDED.reason_code,
                input_reference=EXCLUDED.input_reference,
                missing_evidence=EXCLUDED.missing_evidence,
                processed_at=EXCLUDED.processed_at
            """,
            values,
        )
        return len(values)

    def insert_stage1_enrichment(self, rows: Iterable[Stage1Phase5Enrichment]) -> int:
        rows = tuple(rows)
        if any(not isinstance(row, Stage1Phase5Enrichment) for row in rows):
            raise TypeError("Stage1 Phase 5 enrichment must use canonical contract")
        values = [
            (
                _enrichment_value(row, "screening_run_id"),
                _enrichment_value(row, "symbol"),
                _enrichment_value(row, "universe_run_id"),
                _enrichment_value(row, "sector"),
                _enrichment_value(row, "mapping_version"),
                _enrichment_value(row, "candidate_return_pct"),
                _enrichment_value(row, "sector_return_pct"),
                _enrichment_value(row, "candidate_vs_sector_pct"),
                _value(_enrichment_value(row, "sector_relation")),
                _value(_enrichment_value(row, "context_status")),
                self._json(_enrichment_value(row, "leader_context_ref")),
                self._json(_enrichment_value(row, "regime_context_ref")),
                self._json(_enrichment_value(row, "relative_strength_ref")),
                self._json(_enrichment_value(row, "sector_context_ref")),
                self._json(_enrichment_value(row, "missing_evidence")),
                _value(_enrichment_value(row, "reason_code"))
                if (not isinstance(row, Mapping) and getattr(row, "reason_code", None) is not None)
                or (isinstance(row, Mapping) and row.get("reason_code") is not None)
                else None,
                _utc(_enrichment_value(row, "processed_at"), "processed_at"),
                bool(row.get("context_only", True))
                if isinstance(row, Mapping)
                else bool(getattr(row, "context_only", True)),
            )
            for row in rows
        ]
        if not values:
            return 0
        self.connection.cursor().executemany(
            """
            INSERT INTO stage1_phase5_context_enrichment (
                screening_run_id, symbol, universe_run_id, sector, mapping_version,
                candidate_return_pct, sector_return_pct, candidate_vs_sector_pct,
                sector_relation, context_status, leader_context_ref, regime_context_ref,
                relative_strength_ref, sector_context_ref, missing_evidence, reason_code, processed_at, context_only
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (screening_run_id, symbol) DO UPDATE SET
                universe_run_id=EXCLUDED.universe_run_id, sector=EXCLUDED.sector,
                mapping_version=EXCLUDED.mapping_version,
                candidate_return_pct=EXCLUDED.candidate_return_pct,
                sector_return_pct=EXCLUDED.sector_return_pct,
                candidate_vs_sector_pct=EXCLUDED.candidate_vs_sector_pct,
                sector_relation=EXCLUDED.sector_relation, context_status=EXCLUDED.context_status,
                leader_context_ref=EXCLUDED.leader_context_ref, regime_context_ref=EXCLUDED.regime_context_ref,
                relative_strength_ref=EXCLUDED.relative_strength_ref, sector_context_ref=EXCLUDED.sector_context_ref,
                missing_evidence=EXCLUDED.missing_evidence,
                reason_code=EXCLUDED.reason_code, processed_at=EXCLUDED.processed_at
            """,
            values,
        )
        return len(values)
