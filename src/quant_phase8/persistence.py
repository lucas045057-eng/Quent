"""Bounded PostgreSQL persistence for canonical Phase 8 options context."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence

from psycopg.types.json import Jsonb

from .config import Phase8Settings
from .contracts import (
    DataStatus,
    ObservationKind,
    OptionContextSnapshot,
    OptionInstrument,
    OptionInstrumentEvent,
    OptionMarketObservation,
    TimestampSemantics,
)


MAX_PHASE8_BATCH_ROWS = 4096
MAX_PHASE8_FULL_CHAIN_PER_UNDERLYING = 2048
MAX_PHASE8_TICKER_PER_UNDERLYING = 64
MAX_PHASE8_TICKER_TOTAL = 128
MAX_RETENTION_BATCHES_PER_TABLE = 10
_ALLOWED_CONTEXT_STATUSES = {status.value for status in DataStatus}
_FULL_CHAIN_KINDS = {
    ObservationKind.REST_CHAIN_SUMMARY,
    ObservationKind.WS_MARKPRICE_SNAPSHOT,
}


def _value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _utc(value, "timestamp").isoformat()


def _json_encode(value: Any) -> str:
    """Encode canonical JSON while retaining Decimal values as JSON numbers."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("JSON Decimal values must be finite")
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise TypeError("binary float is forbidden in Phase 8 persistence")
    if isinstance(value, Enum):
        return _json_encode(value.value)
    if isinstance(value, datetime):
        return json.dumps(_iso(value), ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, str):
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        encoded.encode("utf-8")
        return encoded
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        parts = (
            f"{_json_encode(key)}:{_json_encode(value[key])}"
            for key in sorted(value)
        )
        return "{" + ",".join(parts) + "}"
    if isinstance(value, (tuple, list)):
        return "[" + ",".join(_json_encode(item) for item in value) + "]"
    raise TypeError(f"unsupported Phase 8 JSON value: {type(value).__name__}")


def _jsonb(value: Any) -> Jsonb:
    return Jsonb(value, dumps=_json_encode)


def _observation_timestamp_semantics(observation: OptionMarketObservation) -> str:
    semantics = {_value(metric.timestamp_semantics) for metric in observation.metrics.values()}
    if len(semantics) == 1:
        return str(next(iter(semantics)))
    return TimestampSemantics.UNVERIFIED.value


def _metric_values_and_metadata(observation: OptionMarketObservation) -> tuple[dict[str, Any], dict[str, Any]]:
    values: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    for name, metric in observation.metrics.items():
        values[name] = metric.value
        metadata[name] = {
            "source": metric.source,
            "exchange": metric.exchange,
            "source_field": metric.source_field,
            "source_method_or_channel": metric.source_method_or_channel,
            "exchange_timestamp": _iso(metric.exchange_timestamp),
            "field_last_updated_at": _iso(metric.field_last_updated_at),
            "fetched_at": _iso(metric.fetched_at),
            "received_at": _iso(metric.received_at),
            "processed_at": _iso(metric.processed_at),
            "timestamp_semantics": _value(metric.timestamp_semantics),
            "unit_code": metric.unit_code,
            "unit_status": _value(metric.unit_status),
            "status": _value(metric.status),
            "provenance": _value(metric.provenance),
            "quality_reason": metric.quality_reason,
            "raw_reference": metric.raw_reference,
        }
    return values, metadata


def _observation_payload_hash(observation: OptionMarketObservation) -> str:
    metrics: dict[str, Any] = {}
    for name, metric in observation.metrics.items():
        metrics[name] = {
            "value": metric.value,
            "source_field": metric.source_field,
            "source_method_or_channel": metric.source_method_or_channel,
            "exchange_timestamp": metric.exchange_timestamp,
            "field_last_updated_at": metric.field_last_updated_at,
            "timestamp_semantics": _value(metric.timestamp_semantics),
            "unit_code": metric.unit_code,
            "unit_status": _value(metric.unit_status),
            "status": _value(metric.status),
            "provenance": _value(metric.provenance),
            "quality_reason": metric.quality_reason,
        }
    canonical = {
        "exchange": observation.exchange,
        "source": observation.source,
        "symbol": observation.symbol,
        "underlying": observation.underlying,
        "observation_kind": _value(observation.observation_kind),
        "exchange_timestamp": observation.exchange_timestamp,
        "price_index": observation.price_index,
        "underlying_index": observation.underlying_index,
        "quote_currency": observation.quote_currency,
        "schema_version": observation.schema_version,
        "metrics": metrics,
    }
    return sha256(_json_encode(canonical).encode("utf-8")).hexdigest()


def _status(value: Any) -> str:
    result = str(_value(value))
    if result not in _ALLOWED_CONTEXT_STATUSES:
        raise ValueError("unsupported Phase 8 status")
    return result


def _context_status(snapshot: OptionContextSnapshot) -> str:
    statuses = {_status(metric.status) for metric in snapshot.metrics.values()}
    if statuses == {DataStatus.AVAILABLE.value}:
        return DataStatus.AVAILABLE.value
    if DataStatus.AVAILABLE.value in statuses:
        return DataStatus.PARTIAL.value
    if DataStatus.STALE.value in statuses:
        return DataStatus.STALE.value
    if DataStatus.ERROR.value in statuses:
        return DataStatus.ERROR.value
    return DataStatus.NOT_AVAILABLE.value


def _context_payload(
    snapshot: OptionContextSnapshot,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], list[str], str | None]:
    metrics: dict[str, Any] = {}
    source_timestamps: dict[str, Any] = {}
    source_capture_times: dict[str, Any] = {}
    coverage: dict[str, Any] = {}
    provenance: dict[str, Any] = {}
    input_ids: list[str] = []
    reasons: list[str] = []
    for name in sorted(snapshot.metrics):
        metric = snapshot.metrics[name]
        metric_status = _status(metric.status)
        reason = metric.quality_reason
        if reason:
            reasons.append(reason)
        source_times = [_iso(timestamp) for timestamp in metric.source_timestamps]
        capture_times = {
            "fetched_at": _iso(metric.source_fetched_at),
            "received_at": _iso(metric.source_received_at),
        }
        ratio = metric.coverage_ratio
        metrics[name] = {
            "value": metric.value,
            "source_timestamps": source_times,
            "source_fetched_at": capture_times["fetched_at"],
            "source_received_at": capture_times["received_at"],
            "data_age_seconds": metric.data_age_seconds,
            "coverage_expected": metric.coverage_expected,
            "coverage_available": metric.coverage_available,
            "coverage_ratio": ratio,
            "status": metric_status,
            "quality_reason": reason,
            "provenance": _value(metric.provenance),
            "unit_code": metric.unit_code,
            "unit_status": _value(metric.unit_status),
            "input_observation_ids": list(metric.input_observation_ids),
        }
        source_timestamps[name] = source_times
        source_capture_times[name] = capture_times
        coverage[name] = {
            "expected": metric.coverage_expected,
            "available": metric.coverage_available,
            "ratio": ratio,
        }
        provenance[name] = _value(metric.provenance)
        input_ids.extend(metric.input_observation_ids)
    return (
        metrics, source_timestamps, source_capture_times, coverage, provenance,
        sorted(set(input_ids)), (reasons[0] if reasons else None),
    )


def _enforce_per_underlying_cap(
    items: Sequence[Any], *, maximum: int, label: str
) -> None:
    counts: dict[str, int] = {}
    for item in items:
        underlying = item.underlying
        counts[underlying] = counts.get(underlying, 0) + 1
    if any(count > maximum for count in counts.values()):
        raise ValueError(f"per-underlying {label} cap exceeded")


class Phase8Repository:
    """Phase 8-only repository; accepts a psycopg-compatible connection."""

    def __init__(self, connection: Any, *, settings: Phase8Settings | None = None) -> None:
        if not hasattr(connection, "execute") or not hasattr(connection, "transaction"):
            raise TypeError("Phase8Repository requires a transactional PostgreSQL connection")
        if settings is not None and not isinstance(settings, Phase8Settings):
            raise TypeError("Phase8Repository settings must be validated Phase8Settings")
        self.connection = connection
        self.settings = settings or Phase8Settings.from_env()
        self._max_full_chain_records = min(
            self.settings.max_full_chain_records_total, MAX_PHASE8_BATCH_ROWS
        )
        self._max_full_chain_per_underlying = min(
            self.settings.max_full_chain_records_per_underlying,
            MAX_PHASE8_FULL_CHAIN_PER_UNDERLYING,
        )
        self._max_ticker_per_underlying = min(
            self.settings.max_ticker_instruments_per_currency, MAX_PHASE8_TICKER_PER_UNDERLYING
        )
        self._max_ticker_total = min(
            self.settings.max_ticker_subscriptions_total, MAX_PHASE8_TICKER_TOTAL
        )

    @staticmethod
    def _bounded_count(rows: Sequence[Any], *, maximum: int = MAX_PHASE8_BATCH_ROWS) -> int:
        count = len(rows)
        if count > maximum:
            raise ValueError("Phase 8 persistence batch exceeds configured bound")
        return count

    def upsert_instruments(self, instruments: Sequence[OptionInstrument]) -> int:
        self._bounded_count(instruments, maximum=self._max_full_chain_records)
        if any(not isinstance(item, OptionInstrument) for item in instruments):
            raise TypeError("instrument batch contains a non-canonical value")
        _enforce_per_underlying_cap(
            instruments, maximum=self._max_full_chain_per_underlying, label="full-chain"
        )
        if not instruments:
            return 0
        statement = """
            INSERT INTO phase8_option_instruments (
                exchange, source, symbol, provider_instrument_id, underlying, option_type,
                strike, expires_at, instrument_created_at, instrument_state, is_active,
                price_index, base_currency, quote_currency, settlement_currency,
                exchange_timestamp, timestamp_semantics, fetched_at, processed_at,
                status, source_fields, raw_reference
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (exchange, symbol) DO UPDATE SET
                source = EXCLUDED.source,
                provider_instrument_id = EXCLUDED.provider_instrument_id,
                underlying = EXCLUDED.underlying,
                option_type = EXCLUDED.option_type,
                strike = EXCLUDED.strike,
                expires_at = EXCLUDED.expires_at,
                instrument_created_at = EXCLUDED.instrument_created_at,
                instrument_state = EXCLUDED.instrument_state,
                is_active = EXCLUDED.is_active,
                price_index = EXCLUDED.price_index,
                base_currency = EXCLUDED.base_currency,
                quote_currency = EXCLUDED.quote_currency,
                settlement_currency = EXCLUDED.settlement_currency,
                exchange_timestamp = EXCLUDED.exchange_timestamp,
                timestamp_semantics = EXCLUDED.timestamp_semantics,
                fetched_at = EXCLUDED.fetched_at,
                processed_at = EXCLUDED.processed_at,
                status = EXCLUDED.status,
                source_fields = EXCLUDED.source_fields,
                raw_reference = EXCLUDED.raw_reference
        """
        written = 0
        with self.connection.transaction():
            for item in instruments:
                result = self.connection.execute(statement, (
                    item.exchange, item.source, item.symbol, item.provider_instrument_id,
                    item.underlying, _value(item.option_type), item.strike, item.expires_at,
                    item.instrument_created_at, item.instrument_state, item.is_active,
                    item.price_index, item.base_currency, item.quote_currency, item.settlement_currency,
                    item.exchange_timestamp, _value(item.timestamp_semantics), item.fetched_at,
                    item.processed_at, _status(item.status),
                    _jsonb({"source_field": item.source_field, "timestamp_semantics": _value(item.timestamp_semantics)}),
                    item.raw_reference,
                ))
                written += max(0, result.rowcount)
        return written

    def retire_missing_instruments(
        self,
        *,
        exchange: str,
        underlying: str,
        active_symbols: Sequence[str],
        processed_at: datetime,
    ) -> int:
        if not exchange or underlying not in {"BTC", "ETH"}:
            raise ValueError("instrument reconciliation identity is invalid")
        if not active_symbols:
            raise ValueError("empty reconciliation set rejected to protect the current catalog")
        if len(active_symbols) > self._max_full_chain_per_underlying or any(
            not isinstance(symbol, str) or not symbol.strip() for symbol in active_symbols
        ):
            raise ValueError("instrument reconciliation symbols exceed the allowed contract")
        processed_at = _utc(processed_at, "processed_at")
        result = self.connection.execute(
            """UPDATE phase8_option_instruments
               SET is_active = FALSE, processed_at = %s
               WHERE exchange = %s AND underlying = %s AND is_active
                 AND NOT (symbol = ANY(%s))""",
            (processed_at, exchange, underlying, list(active_symbols)),
        )
        return max(0, result.rowcount)

    def append_instrument_events(self, events: Sequence[OptionInstrumentEvent]) -> int:
        self._bounded_count(events, maximum=self._max_full_chain_records)
        if any(not isinstance(item, OptionInstrumentEvent) for item in events):
            raise TypeError("instrument event batch contains a non-canonical value")
        if not events:
            return 0
        statement = """
            INSERT INTO phase8_option_instrument_events (
                instrument_id, exchange, source, symbol, event_type, instrument_state,
                event_identity, exchange_timestamp, timestamp_semantics, received_at,
                processed_at, status, raw_reference
            ) VALUES (
                (SELECT id FROM phase8_option_instruments WHERE exchange = %s AND symbol = %s),
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) ON CONFLICT ON CONSTRAINT phase8_option_instrument_events_identity_uq DO NOTHING
        """
        written = 0
        with self.connection.transaction():
            for event in events:
                event_identity = event.event_identity or sha256(_json_encode({
                    "exchange": event.exchange,
                    "source": event.source,
                    "symbol": event.symbol,
                    "event_type": _value(event.event_type),
                    "instrument_state": event.instrument_state,
                    "exchange_timestamp": event.exchange_timestamp,
                }).encode("utf-8")).hexdigest()
                result = self.connection.execute(statement, (
                    event.exchange, event.symbol, event.exchange, event.source, event.symbol,
                    _value(event.event_type), event.instrument_state, event_identity,
                    event.exchange_timestamp, "UNVERIFIED",
                    event.received_at, event.processed_at, _status(event.status), event.raw_reference,
                ))
                written += max(0, result.rowcount)
        return written

    def _insert_observations(self, observations: Sequence[OptionMarketObservation]) -> int:
        statement = """
            INSERT INTO phase8_option_market_snapshots (
                instrument_id, exchange, source, symbol, underlying, price_index,
                underlying_index, quote_currency, observation_kind, exchange_timestamp,
                timestamp_semantics, fetched_at, received_at, processed_at, status,
                schema_version, metrics, field_metadata, payload_hash, raw_reference
            ) VALUES (
                (SELECT id FROM phase8_option_instruments WHERE exchange = %s AND symbol = %s),
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            ) ON CONFLICT ON CONSTRAINT phase8_option_market_snapshots_dedup_uq DO NOTHING
        """
        written = 0
        for observation in observations:
            if not isinstance(observation, OptionMarketObservation):
                raise TypeError("market observation batch contains a non-canonical value")
            values, metadata = _metric_values_and_metadata(observation)
            result = self.connection.execute(statement, (
                observation.exchange, observation.symbol,
                observation.exchange, observation.source, observation.symbol, observation.underlying,
                observation.price_index, observation.underlying_index, observation.quote_currency,
                _value(observation.observation_kind), observation.exchange_timestamp,
                _observation_timestamp_semantics(observation), observation.fetched_at,
                observation.received_at, observation.processed_at, _status(observation.status),
                observation.schema_version, _jsonb(values), _jsonb(metadata),
                _observation_payload_hash(observation), observation.raw_reference,
            ))
            written += max(0, result.rowcount)
        return written

    def persist_observations(self, observations: Sequence[OptionMarketObservation]) -> int:
        self._bounded_count(observations, maximum=self._max_full_chain_records)
        if not observations:
            return 0
        if any(not isinstance(item, OptionMarketObservation) for item in observations):
            raise TypeError("market observation batch contains a non-canonical value")
        ticker_observations = [
            item for item in observations
            if _value(item.observation_kind) in {
                ObservationKind.WS_INCREMENTAL_TICKER_SNAPSHOT.value,
                ObservationKind.WS_INCREMENTAL_TICKER_CHANGE.value,
            }
        ]
        if len(ticker_observations) > self._max_ticker_total:
            raise ValueError("total ticker persistence cap exceeded")
        for underlying in {item.underlying for item in ticker_observations}:
            if sum(item.underlying == underlying for item in ticker_observations) > self._max_ticker_per_underlying:
                raise ValueError("per-underlying ticker persistence cap exceeded")
        full_chain_observations = [
            item for item in observations if _value(item.observation_kind) in {
                _value(kind) for kind in _FULL_CHAIN_KINDS
            }
        ]
        _enforce_per_underlying_cap(
            full_chain_observations,
            maximum=self._max_full_chain_per_underlying,
            label="full-chain",
        )
        with self.connection.transaction():
            return self._insert_observations(observations)

    def persist_full_chain_cycle(
        self,
        observations: Sequence[OptionMarketObservation],
        *,
        max_records: int,
    ) -> int:
        if (
            not isinstance(max_records, int) or isinstance(max_records, bool)
            or not 1 <= max_records <= self._max_full_chain_records
        ):
            raise ValueError("full-chain record cap is outside the supported bound")
        count = len(observations)
        if count > max_records:
            raise ValueError("full-chain cycle exceeds configured record cap")
        if count > self._max_full_chain_per_underlying:
            raise ValueError("per-underlying full-chain cap exceeded")
        self._bounded_count(observations, maximum=max_records)
        if count == 0:
            return 0
        if any(not isinstance(item, OptionMarketObservation) for item in observations):
            raise TypeError("full-chain cycle contains a non-canonical observation")
        kinds = {_value(item.observation_kind) for item in observations}
        underlyings = {item.underlying for item in observations}
        if len(kinds) != 1 or next(iter(kinds)) not in {_value(kind) for kind in _FULL_CHAIN_KINDS}:
            raise ValueError("full-chain cycle must contain one summary or markprice seed kind")
        if len(underlyings) != 1:
            raise ValueError("full-chain cycle must contain one underlying")
        with self.connection.transaction():
            return self._insert_observations(observations)

    def persist_context(self, snapshot: OptionContextSnapshot) -> bool:
        if not isinstance(snapshot, OptionContextSnapshot):
            raise TypeError("context snapshot must be canonical")
        metrics, source_timestamps, capture_times, coverage, provenance, input_ids, reason = _context_payload(snapshot)
        result = self.connection.execute(
            """INSERT INTO phase8_option_context_snapshots (
                   underlying, context_timestamp, processed_at, calculation_version, metrics,
                   source_timestamps, source_capture_times, coverage, provenance,
                   input_observation_ids, status, reason_code, context_only
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
               ON CONFLICT ON CONSTRAINT phase8_option_context_snapshots_replay_uq DO NOTHING""",
            (
                snapshot.underlying, snapshot.context_timestamp, snapshot.processed_at,
                snapshot.calculation_version, _jsonb(metrics), _jsonb(source_timestamps),
                _jsonb(capture_times), _jsonb(coverage), _jsonb(provenance), _jsonb(input_ids),
                _context_status(snapshot), reason,
            ),
        )
        return result.rowcount == 1

    @staticmethod
    def _delete_batches(
        connection: Any,
        *,
        table: str,
        time_column: str,
        predicate: str,
        cutoff: datetime,
        batch_size: int,
        max_batches: int,
    ) -> int:
        # Table, column, and predicate values are private constants at call sites.
        statement = f"""WITH expired AS (
                SELECT id FROM {table} WHERE {predicate} AND {time_column} < %s
                ORDER BY {time_column}, id LIMIT %s
            )
            DELETE FROM {table} target USING expired
            WHERE target.id = expired.id RETURNING target.id"""
        total = 0
        for _ in range(max_batches):
            result = connection.execute(statement, (cutoff, batch_size))
            deleted = len(result.fetchall())
            total += deleted
            if deleted < batch_size:
                break
        return total

    def cleanup_retention(
        self,
        settings: Phase8Settings,
        *,
        now: datetime,
        max_batches: int = 1,
    ) -> dict[str, int]:
        if not isinstance(settings, Phase8Settings):
            raise TypeError("retention cleanup requires validated Phase8Settings")
        if not settings.retention_enforcement:
            return {}
        now = _utc(now, "now")
        if isinstance(max_batches, bool) or not isinstance(max_batches, int) or not 1 <= max_batches <= MAX_RETENTION_BATCHES_PER_TABLE:
            raise ValueError("retention batch count is outside the supported bound")
        batch_size = settings.retention_delete_batch_rows
        if not 1 <= batch_size <= 1000:
            raise ValueError("retention batch size is outside the supported bound")

        snapshot_cutoff = now - timedelta(days=settings.snapshot_retention_days)
        context_cutoff = now - timedelta(days=settings.context_retention_days)
        lifecycle_cutoff = now - timedelta(days=settings.lifecycle_retention_days)
        deleted = {
            "phase8_option_instrument_events": 0,
            "phase8_option_market_snapshots": 0,
            "phase8_option_context_snapshots": 0,
        }
        snapshot_categories = (
            ("fetched_at", "observation_kind = 'REST_CHAIN_SUMMARY'"),
            ("received_at", "observation_kind IN ('WS_MARKPRICE_SNAPSHOT', 'WS_MARKPRICE_CHANGE')"),
            ("received_at", "observation_kind IN ('WS_INCREMENTAL_TICKER_SNAPSHOT', 'WS_INCREMENTAL_TICKER_CHANGE')"),
        )
        with self.connection.transaction():
            deleted["phase8_option_instrument_events"] = self._delete_batches(
                self.connection, table="phase8_option_instrument_events", time_column="processed_at",
                predicate="TRUE", cutoff=lifecycle_cutoff, batch_size=batch_size, max_batches=max_batches,
            )
            for time_column, predicate in snapshot_categories:
                deleted["phase8_option_market_snapshots"] += self._delete_batches(
                    self.connection, table="phase8_option_market_snapshots", time_column=time_column,
                    predicate=predicate, cutoff=snapshot_cutoff, batch_size=batch_size, max_batches=max_batches,
                )
            deleted["phase8_option_context_snapshots"] = self._delete_batches(
                self.connection, table="phase8_option_context_snapshots", time_column="processed_at",
                predicate="TRUE", cutoff=context_cutoff, batch_size=batch_size, max_batches=max_batches,
            )
        return {table: count for table, count in deleted.items() if count}
