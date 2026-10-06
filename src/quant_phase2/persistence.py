"""PostgreSQL persistence for canonical Phase 2 derivatives."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
import hashlib
import json
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from .contracts import ContractType, CrossExchangeSnapshot, DataStatus, FundingObservation, InstrumentMetadata, OIObservation


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _observation_key(row: OIObservation | FundingObservation) -> str:
    """Build a stable provider-event key for idempotent persistence."""
    if row.exchange_timestamp is not None:
        return f"timestamp:{row.exchange_timestamp.isoformat()}"
    payload = json.dumps(_jsonable(row.raw_payload), sort_keys=True, separators=(",", ":"))
    return f"payload:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


class Phase2Repository:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def upsert_instruments(self, instruments: Iterable[InstrumentMetadata]) -> None:
        rows = [
            (
                row.exchange, row.exchange_symbol, row.canonical_symbol, row.contract_type.value,
                row.base_asset, row.quote_asset, row.settle_asset, row.margin_asset, row.contract_multiplier,
                row.contract_size, row.funding_interval_seconds, row.mark_price, row.source_endpoint,
                row.exchange_timestamp, row.fetched_at, row.status.value, Jsonb(_jsonable(row.raw_payload)),
            ) for row in instruments
        ]
        if rows:
            self.connection.cursor().executemany(
                """
                INSERT INTO exchange_instruments (
                    exchange, exchange_symbol, canonical_symbol, contract_type, base_asset, quote_asset,
                    settle_asset, margin_asset, contract_multiplier, contract_size, funding_interval_seconds,
                    mark_price, source_endpoint, exchange_timestamp, fetched_at, status, raw_payload
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (exchange, exchange_symbol) DO UPDATE SET
                    canonical_symbol=EXCLUDED.canonical_symbol, status=EXCLUDED.status,
                    funding_interval_seconds=EXCLUDED.funding_interval_seconds,
                    mark_price=EXCLUDED.mark_price, fetched_at=EXCLUDED.fetched_at,
                    raw_payload=EXCLUDED.raw_payload
                """, rows
            )

    def insert_open_interest(self, observations: Iterable[OIObservation]) -> None:
        rows = [
            (
                row.symbol, row.canonical_symbol, row.exchange, row.contract_type.value, row.margin_asset,
                row.settle_asset, row.raw_open_interest, row.raw_unit, row.open_interest_base,
                row.open_interest_quote, row.open_interest_usd, row.mark_price, row.normalization_method,
                row.exchange_timestamp, row.fetched_at, row.processed_at, row.status.value,
                row.source_endpoint, _observation_key(row), Jsonb(_jsonable(row.raw_payload)), row.raw_reference,
            ) for row in observations
        ]
        if rows:
            self.connection.cursor().executemany(
                """
                INSERT INTO open_interest (
                    symbol, canonical_symbol, exchange, contract_type, margin_asset, settle_asset,
                    raw_open_interest, raw_unit, open_interest_base, open_interest_quote, open_interest_usd,
                    mark_price, normalization_method, exchange_timestamp, fetched_at, processed_at,
                    status, source_endpoint, observation_key, raw_payload, raw_reference
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (exchange, symbol, observation_key, source_endpoint) DO UPDATE SET
                    raw_open_interest=EXCLUDED.raw_open_interest, raw_unit=EXCLUDED.raw_unit,
                    open_interest_base=EXCLUDED.open_interest_base, open_interest_quote=EXCLUDED.open_interest_quote,
                    open_interest_usd=EXCLUDED.open_interest_usd, normalization_method=EXCLUDED.normalization_method,
                    fetched_at=EXCLUDED.fetched_at, processed_at=EXCLUDED.processed_at,
                    status=EXCLUDED.status, raw_payload=EXCLUDED.raw_payload
                """, rows
            )

    def insert_funding(self, observations: Iterable[FundingObservation]) -> None:
        rows = [
            (
                row.symbol, row.canonical_symbol, row.exchange, row.contract_type.value, row.funding_rate,
                row.funding_interval_seconds, row.normalized_8h_rate, row.predicted_funding_rate,
                row.realized_funding_rate, row.next_funding_time, row.exchange_timestamp, row.fetched_at,
                row.processed_at, row.status.value, row.source_endpoint, Jsonb(_jsonable(row.raw_payload)),
                _observation_key(row), row.raw_reference, row.classification.value if row.classification else None,
            ) for row in observations
        ]
        if rows:
            self.connection.cursor().executemany(
                """
                INSERT INTO funding_rates (
                    symbol, canonical_symbol, exchange, contract_type, funding_rate, funding_interval_seconds,
                    normalized_8h_rate, predicted_funding_rate, realized_funding_rate, next_funding_time,
                    exchange_timestamp, fetched_at, processed_at, status, source_endpoint, raw_payload,
                    observation_key, raw_reference, classification
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (exchange, symbol, observation_key, source_endpoint) DO UPDATE SET
                    funding_rate=EXCLUDED.funding_rate, funding_interval_seconds=EXCLUDED.funding_interval_seconds,
                    normalized_8h_rate=EXCLUDED.normalized_8h_rate, predicted_funding_rate=EXCLUDED.predicted_funding_rate,
                    realized_funding_rate=EXCLUDED.realized_funding_rate, next_funding_time=EXCLUDED.next_funding_time,
                    fetched_at=EXCLUDED.fetched_at, processed_at=EXCLUDED.processed_at,
                    status=EXCLUDED.status, raw_payload=EXCLUDED.raw_payload, classification=EXCLUDED.classification
                """, rows
            )

    def insert_cross_exchange_snapshot(self, snapshot: CrossExchangeSnapshot) -> None:
        payload = _jsonable(snapshot)
        self.connection.execute(
            """
            INSERT INTO cross_exchange_derivative_snapshots (
                canonical_symbol, snapshot_timestamp, status, oi_exchange_count, funding_exchange_count,
                oi_total_usd, oi_weighted_funding, median_funding, max_funding, min_funding,
                funding_dispersion, reason, snapshot
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (canonical_symbol, snapshot_timestamp) DO UPDATE SET
                status=EXCLUDED.status, oi_exchange_count=EXCLUDED.oi_exchange_count,
                funding_exchange_count=EXCLUDED.funding_exchange_count, oi_total_usd=EXCLUDED.oi_total_usd,
                oi_weighted_funding=EXCLUDED.oi_weighted_funding, median_funding=EXCLUDED.median_funding,
                max_funding=EXCLUDED.max_funding, min_funding=EXCLUDED.min_funding,
                funding_dispersion=EXCLUDED.funding_dispersion, reason=EXCLUDED.reason,
                snapshot=EXCLUDED.snapshot
            """,
            (
                snapshot.canonical_symbol, snapshot.timestamp, snapshot.status.value, snapshot.oi_exchange_count,
                snapshot.funding_exchange_count, snapshot.oi_total_usd, snapshot.oi_weighted_funding,
                snapshot.median_funding, snapshot.max_funding, snapshot.min_funding,
                snapshot.funding_dispersion, snapshot.reason, Jsonb(payload),
            ),
        )

    def cleanup_derivatives(
        self, *, oi_retention_days: int, funding_retention_days: int, snapshot_retention_days: int
    ) -> None:
        if min(oi_retention_days, funding_retention_days, snapshot_retention_days) <= 0:
            raise ValueError("Phase 2 retention values must be positive")
        self.connection.execute(
            "DELETE FROM open_interest WHERE fetched_at < now() - (%s * interval '1 day')",
            (oi_retention_days,),
        )
        self.connection.execute(
            "DELETE FROM funding_rates WHERE fetched_at < now() - (%s * interval '1 day')",
            (funding_retention_days,),
        )
        self.connection.execute(
            "DELETE FROM cross_exchange_derivative_snapshots WHERE snapshot_timestamp < now() - (%s * interval '1 day')",
            (snapshot_retention_days,),
        )

    def load_recent_oi_history(self, *, limit: int = 4000) -> list[OIObservation]:
        rows = self.connection.execute(
            """
            SELECT symbol, canonical_symbol, exchange, margin_asset, settle_asset,
                   raw_open_interest, raw_unit, open_interest_base, open_interest_quote,
                   open_interest_usd, mark_price, normalization_method, exchange_timestamp,
                   fetched_at, processed_at, status, source_endpoint, raw_payload, raw_reference
            FROM open_interest
            WHERE status = 'AVAILABLE' AND open_interest_usd IS NOT NULL
            ORDER BY fetched_at DESC
            LIMIT %s
            """,
            (limit,),
        ).fetchall()
        return [
            OIObservation(
                symbol=row[0], canonical_symbol=row[1], exchange=row[2], contract_type=ContractType.PERPETUAL,
                margin_asset=row[3], settle_asset=row[4], raw_open_interest=row[5], raw_unit=row[6],
                open_interest_base=row[7], open_interest_quote=row[8], open_interest_usd=row[9], mark_price=row[10],
                normalization_method=row[11], exchange_timestamp=row[12], fetched_at=row[13], processed_at=row[14],
                status=DataStatus(row[15]), source_endpoint=row[16], raw_payload=row[17] or {}, raw_reference=row[18],
            )
            for row in rows
        ]

    def insert_stage1_enrichments(self, run_id: int, results: Iterable[Any], processed_at: datetime) -> int:
        """Persist derivative context without changing the Phase 1 result."""
        rows = list(results)
        if not rows:
            return 0
        symbols = [str(result.symbol) for result in rows]
        canonical_by_symbol = {
            symbol: f"{symbol[:-4]}-USDT-PERP"
            for symbol in symbols if symbol.upper().endswith("USDT")
        }
        latest = self.connection.execute(
            """
            SELECT DISTINCT ON (canonical_symbol) canonical_symbol, status, snapshot_timestamp, snapshot
            FROM cross_exchange_derivative_snapshots
            WHERE canonical_symbol = ANY(%s)
            ORDER BY canonical_symbol, snapshot_timestamp DESC
            """,
            (list(canonical_by_symbol.values()),),
        ).fetchall()
        latest_by_canonical = {row[0]: row for row in latest}
        payload_rows = []
        for result in rows:
            canonical = canonical_by_symbol.get(str(result.symbol))
            snapshot_row = latest_by_canonical.get(canonical)
            if snapshot_row is None:
                status, timestamp, snapshot = DataStatus.NOT_AVAILABLE.value, None, {}
            else:
                status, timestamp, snapshot = snapshot_row[1], snapshot_row[2], snapshot_row[3]
            payload_rows.append((
                run_id, result.symbol, canonical, status, timestamp, Jsonb(_jsonable(snapshot or {})), processed_at,
            ))
        self.connection.cursor().executemany(
            """
            INSERT INTO stage1_derivative_enrichment (
                screening_run_id, symbol, canonical_symbol, derivative_status,
                derivative_snapshot_timestamp, derivative_snapshot, processed_at
            ) VALUES (%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (screening_run_id, symbol) DO UPDATE SET
                canonical_symbol=EXCLUDED.canonical_symbol,
                derivative_status=EXCLUDED.derivative_status,
                derivative_snapshot_timestamp=EXCLUDED.derivative_snapshot_timestamp,
                derivative_snapshot=EXCLUDED.derivative_snapshot,
                processed_at=EXCLUDED.processed_at
            """,
            payload_rows,
        )
        return len(payload_rows)
