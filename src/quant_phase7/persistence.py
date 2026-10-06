"""Bounded, idempotent PostgreSQL persistence for Phase 7 context."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from itertools import chain, islice
import json
from typing import Any, Iterable, Mapping, Sequence

from psycopg.types.json import Jsonb

from .contracts import (
    AssetKind,
    CanonicalAmount,
    CanonicalAssetId,
    Chain,
    DataStatus,
    EventIndexKind,
    FinalityStatus,
    MarketKind,
    OnChainEventIdentity,
    OnChainTransferEvent,
    Provenance,
    RawReference,
    ReasonCode,
    UsdValuation,
)
from .labels import LabelSnapshot
from .recovery import CheckpointState


MAX_CONTEXT_BYTES = 65_536
MAX_TRANSFER_BATCH = 10_000
MAX_REORG_EVENTS = 10_000
MAX_CONTEXT_WINDOWS = 50_000
_WINDOW_STATUS_VALUES = {"AVAILABLE", "PARTIAL", "STALE", "NOT_AVAILABLE", "ERROR"}
_WINDOW_TIMEFRAMES = {"1m": 60, "5m": 300, "15m": 900, "1H": 3600, "4H": 14400}


class Phase7ContextLimitError(ValueError):
    """A bounded Phase 7 aggregation query exceeded its explicit work budget."""


def _window_decimal(value: Any, field: str, *, nonnegative: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be Decimal-compatible") from exc
    if not result.is_finite() or (nonnegative and result < 0):
        raise ValueError(f"{field} must be finite and non-negative" if nonnegative else f"{field} must be finite")
    return result


def _validate_window_row(table: str, row: Mapping[str, Any]) -> None:
    status = row.get("status")
    if status not in _WINDOW_STATUS_VALUES:
        raise ValueError(f"{table} status is invalid")
    if not isinstance(row.get("reason"), str) or not row["reason"].strip():
        raise ValueError(f"{table} reason is required")
    if "timeframe" in row and row["timeframe"] in _WINDOW_TIMEFRAMES:
        if row["window_close"] - row["window_open"] != timedelta(seconds=_WINDOW_TIMEFRAMES[row["timeframe"]]):
            raise ValueError(f"{table} window bounds do not match timeframe")
    if table == "phase7_spot_flow_windows":
        if row.get("market_kind") != "SPOT":
            raise ValueError("spot window market_kind must be SPOT")
        from .flow_scope import approved_sbe_spot_window
        if not (row.get("symbol") in {"BTCUSDT", "ETHUSDT"} or approved_sbe_spot_window(row.get('symbol'),
                exchange=row.get('exchange'),aggregation_version=row.get('aggregation_version'),
                normalization_version=row.get('normalization_version'),source_reference=row.get('source_reference'))):
            raise ValueError("spot window symbol is not approved")
        base = _window_decimal(row["base_volume"], "base_volume", nonnegative=True)
        if _window_decimal(row["unknown_volume"], "unknown_volume", nonnegative=True) > base:
            raise ValueError("unknown_volume cannot exceed base_volume")
        if _window_decimal(row["quote_volume"], "quote_volume", nonnegative=True) < 0:
            raise ValueError("quote_volume must be non-negative")
        if int(row["directional_trade_count"]) > int(row["trade_count"]):
            raise ValueError("directional_trade_count cannot exceed trade_count")
    elif table in {"phase7_onchain_flow_windows", "phase7_whale_flow_windows"}:
        expected_scope = {"GENERIC_ONCHAIN": "GENERIC", "STABLECOIN": "STABLECOIN", "BRIDGE": "BRIDGE"}
        if row.get("aggregation_scope") != expected_scope.get(row.get("flow_domain")):
            raise ValueError(f"{table} flow domain and aggregation scope are incompatible")
        if row.get("aggregation_scope") in {"BRIDGE", "STABLECOIN"} and row.get("aggregation_eligible"):
            raise ValueError(f"{table} excluded domain cannot be aggregation eligible")
        if row.get("known_address_count", 0) < 0 or row.get("labeled_address_count", 0) < 0:
            raise ValueError(f"{table} address counts must be non-negative")
        if row.get("labeled_address_count", 0) > row.get("known_address_count", 0):
            raise ValueError(f"{table} labeled_address_count cannot exceed known_address_count")
        if row.get("labeled_count") != row.get("labeled_address_count"):
            raise ValueError(f"{table} labeled count aliases must agree")
        if not isinstance(row.get("source_quality"), str) or not row["source_quality"].strip():
            raise ValueError(f"{table} source_quality is required")
        if row.get("coverage_status") not in _WINDOW_STATUS_VALUES:
            raise ValueError(f"{table} coverage_status is invalid")
        if table == "phase7_onchain_flow_windows":
            inbound = _window_decimal(row["inbound_amount"], "inbound_amount", nonnegative=True)
            outbound = _window_decimal(row["outbound_amount"], "outbound_amount", nonnegative=True)
            if _window_decimal(row["net_amount"], "net_amount") != inbound - outbound:
                raise ValueError("net_amount must equal inbound_amount - outbound_amount")
        elif not row.get("aggregation_eligible"):
            if any(_window_decimal(row[field], field, nonnegative=True) != 0 for field in (
                "large_inflow_amount", "large_outflow_amount",
            )) or any(int(row[field]) != 0 for field in (
                "large_inflow_count", "large_outflow_count", "threshold_not_evaluable_count",
            )):
                raise ValueError("ineligible whale context cannot contain whale totals")
    elif table == "phase7_stablecoin_context":
        if row.get("flow_domain") != "STABLECOIN" or row.get("aggregation_scope") != "STABLECOIN":
            raise ValueError("stablecoin context domain is fixed")
        if row.get("category") not in {
            "ORDINARY_TRANSFER", "MINT", "BURN", "EXCHANGE_DEPOSIT",
            "EXCHANGE_WITHDRAWAL", "BRIDGE_TRANSFER", "UNKNOWN",
        }:
            raise ValueError("stablecoin category is invalid")
        if row.get("category") == "BRIDGE_TRANSFER" and row.get("aggregation_eligible"):
            raise ValueError("bridge stablecoin context cannot be aggregation eligible")
        _window_decimal(row["amount_normalized"], "amount_normalized", nonnegative=True)


def _asset_id_text(asset_id: Any) -> str:
    """Persist the canonical asset identity without losing its kind or version."""
    return (
        f"{asset_id.chain.value}:{asset_id.kind.value}:"
        f"{asset_id.contract_or_native}:{asset_id.registry_version}"
    )


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _bounded_json(value: Any, field: str, *, max_bytes: int = MAX_CONTEXT_BYTES) -> Jsonb:
    encoded = json.dumps(value, default=str, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ValueError(f"{field} exceeds bounded size")
    lowered = encoded.lower()
    if "raw_payload" in lowered or "full_payload" in lowered:
        raise ValueError(f"{field} cannot contain a raw payload")
    # Store the validated JSON-safe representation.  In particular, RawReference
    # may contain a timezone-aware expiry datetime which psycopg cannot encode
    # from the original dataclass object without an explicit conversion.
    return Jsonb(json.loads(encoded))


def _reference(reference: Any) -> str | None:
    if reference is None:
        return None
    if hasattr(reference, "reference"):
        reference = asdict(reference)
    encoded = json.dumps(reference, default=str, separators=(",", ":"), ensure_ascii=False)
    if len(encoded.encode("utf-8")) > MAX_CONTEXT_BYTES:
        raise ValueError("raw_reference exceeds bounded size")
    if "raw_payload" in encoded.lower() or "full_payload" in encoded.lower():
        raise ValueError("raw_reference cannot contain a raw payload")
    return encoded


def _require_keys(row: Mapping[str, Any], keys: Sequence[str], table: str) -> None:
    missing = [key for key in keys if key not in row]
    if missing:
        raise ValueError(f"{table} missing required fields: {', '.join(missing)}")


def _require_event_identity(event: OnChainTransferEvent) -> None:
    block_hash = event.identity.block_hash
    if block_hash is None:
        raise ValueError("canonical transfer identity requires block_hash")
    if block_hash not in event.event_id or event.identity.tx_hash not in event.event_id:
        raise ValueError("event_id must contain canonical block_hash and tx_hash")


def _context_event(row: Mapping[str, Any]) -> OnChainTransferEvent:
    """Rehydrate the canonical subset needed by pure context aggregators."""
    chain = Chain(row["chain"])
    kind = AssetKind(row["asset_kind"])
    contract_or_native = "NATIVE" if kind is AssetKind.NATIVE else row["contract_address"]
    asset = CanonicalAssetId(
        chain=chain, kind=kind, contract_or_native=contract_or_native,
        registry_version=row["registry_version"],
    )
    event_time = row["event_time"]
    amount = CanonicalAmount(asset_id=asset, amount_raw=row["amount_raw"], decimals=int(row["decimals"]))
    valuation_status = DataStatus(row["valuation_status"])
    valuation_reason = (
        ReasonCode(row["valuation_reason"]) if row["valuation_reason"] is not None else None
    )
    skew_seconds = row["valuation_skew_seconds"]
    valuation = UsdValuation(
        asset_id=asset,
        event_time=event_time,
        amount_usd=row["amount_usd"] if valuation_status is DataStatus.AVAILABLE else None,
        valuation_price=row["valuation_price"] if valuation_status is DataStatus.AVAILABLE else None,
        valuation_exchange=row["valuation_exchange"] if valuation_status is DataStatus.AVAILABLE else None,
        valuation_source=row["valuation_source"] if valuation_status is DataStatus.AVAILABLE else None,
        valuation_symbol=(
            "BTCUSDT" if chain is Chain.BITCOIN else "ETHUSDT"
        ) if kind is AssetKind.NATIVE else None,
        market_kind=MarketKind.SPOT,
        valuation_exchange_timestamp=(
            row["valuation_exchange_timestamp"] if valuation_status is DataStatus.AVAILABLE else None
        ),
        valuation_fetched_at=(
            row["valuation_fetched_at"] if valuation_status is DataStatus.AVAILABLE else None
        ),
        valuation_skew=(
            timedelta(seconds=int(skew_seconds)) if valuation_status is DataStatus.AVAILABLE else None
        ),
        max_valuation_skew=timedelta(seconds=max(300, int(skew_seconds or 0))),
        status=valuation_status,
        reason_code=valuation_reason,
    )
    identity = OnChainEventIdentity(
        chain=chain,
        tx_hash=row["tx_hash"],
        tx_index=row["tx_index"],
        event_index_kind=EventIndexKind(row["event_index_kind"]),
        event_index=row["event_index"],
        asset_id=asset,
        contract_address=row["contract_address"],
        block_hash=row["block_hash"],
    )
    def raw_reference(value: Any) -> RawReference | None:
        if value is None:
            return None
        reference_data = json.loads(value) if isinstance(value, str) else value
        if not isinstance(reference_data, Mapping):
            raise ValueError("canonical raw reference is invalid")
        expires_at = reference_data.get("expires_at")
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        return RawReference(
            reference=reference_data["reference"],
            content_hash=reference_data["content_hash"],
            byte_size=reference_data.get("byte_size"),
            expires_at=expires_at,
        )

    event_raw_reference = raw_reference(row.get("raw_reference"))
    address_set_reference = raw_reference(row.get("from_address_set_ref"))
    provenance = Provenance(
        source=row["source_id"], source_version=row["source_version"],
        source_reference=row["source_reference"], source_hash=row["source_hash"],
        observed_at=row["observed_at"], fetched_at=row["fetched_at"],
        processed_at=row["processed_at"], raw_reference=event_raw_reference,
    )
    details = row["details"] or {}
    if not isinstance(details, Mapping):
        raise ValueError("canonical event details are invalid")
    return OnChainTransferEvent(
        event_id=row["event_id"], identity=identity, block_number=int(row["block_number"]),
        block_hash=row["block_hash"], event_time=event_time,
        from_address=row["from_address"], to_address=row["to_address"],
        from_address_set_reference=address_set_reference, amount=amount, valuation=valuation,
        provenance=provenance, raw_reference=event_raw_reference, schema_version=row["schema_version"],
        normalization_version=row["normalization_version"],
        details=tuple((str(key), value) for key, value in details.items()),
        status=DataStatus(row["status"]), finality_status=FinalityStatus(row["finality_status"]),
        reason_code=ReasonCode(row["reason"]),
    )


class Phase7Repository:
    """Persistence boundary; SQL is kept separate from normalization contracts."""

    _WINDOW_TABLES = {
        "phase7_onchain_flow_windows": (
            "chain", "asset_id", "timeframe", "window_open", "window_close",
            "context_version", "flow_domain", "aggregation_scope", "aggregation_eligible",
            "bridge_leg_id", "inbound_amount", "outbound_amount", "net_amount",
            "inbound_amount_usd", "outbound_amount_usd", "net_amount_usd",
            "exchange_inflow_amount", "exchange_outflow_amount", "unknown_transfer_count",
            "sample_count", "source_count", "available_count", "missing_count", "labeled_count",
            "known_address_count", "labeled_address_count", "source_quality", "coverage_status",
            "coverage_ratio", "label_coverage_ratio", "status", "reason", "source_version",
            "normalization_version", "source_reference", "processed_at", "created_at",
        ),
        "phase7_whale_flow_windows": (
            "chain", "asset_id", "timeframe", "window_open", "window_close",
            "aggregation_version", "flow_domain", "aggregation_scope", "aggregation_eligible",
            "bridge_leg_id", "threshold_version", "threshold_tier", "large_inflow_count",
            "large_outflow_count", "large_inflow_amount", "large_outflow_amount", "large_inflow_usd",
            "large_outflow_usd", "threshold_not_evaluable_count", "unknown_transfer_count",
            "sample_count", "source_count", "available_count", "missing_count", "coverage_ratio",
            "known_address_count", "labeled_address_count", "labeled_count", "source_quality", "coverage_status",
            "label_coverage_ratio", "status", "reason", "source_reference", "normalization_version",
            "processed_at", "created_at",
        ),
        "phase7_spot_flow_windows": (
            "exchange", "symbol", "market_kind", "timeframe", "window_open", "window_close",
            "aggregation_version", "base_volume", "quote_volume", "buy_volume", "sell_volume",
            "unknown_volume", "delta", "cvd", "trade_count", "directional_trade_count",
            "event_time_first", "event_time_last", "cursor_first", "cursor_last", "sample_count",
            "source_count", "available_count", "missing_count", "coverage_ratio", "status", "reason",
            "source_reference", "normalization_version", "processed_at", "created_at",
        ),
        "phase7_stablecoin_context": (
            "chain", "asset_id", "contract_address", "category", "timeframe", "window_open",
            "window_close", "aggregation_version", "flow_domain", "aggregation_scope", "bridge_leg_id",
            "aggregation_eligible", "transfer_count", "amount_normalized", "amount_usd", "mint_count",
            "burn_count", "exchange_deposit_count", "exchange_withdrawal_count", "sample_count",
            "source_count", "available_count", "missing_count", "coverage_ratio", "finality_status",
            "freshness_status", "status", "reason", "source_reference", "normalization_version",
            "processed_at", "created_at",
        ),
    }

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def load_checkpoint(
        self,
        source_id: str,
        scope_kind: str,
        scope_key: str,
    ) -> dict[str, Any] | None:
        """Load one durable cursor without materializing other sources."""
        columns = (
            "source_id", "scope_kind", "scope_key", "cursor_kind", "cursor_value",
            "last_observed_cursor", "last_finalized_cursor", "last_block_hash",
            "parser_version", "schema_version", "status", "reason", "updated_at",
        )
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT source_id,scope_kind,scope_key,cursor_kind,cursor_value,
                       last_observed_cursor,last_finalized_cursor,last_block_hash,
                       parser_version,schema_version,status,reason,updated_at
                FROM phase7_ingestion_checkpoints
                WHERE source_id=%s AND scope_kind=%s AND scope_key=%s
                """,
                (source_id, scope_kind, scope_key),
            )
            row = cursor.fetchone()
        return dict(zip(columns, row, strict=True)) if row is not None else None

    def load_block_hashes(self, chain: str, start_block: int, end_block: int) -> dict[int, str]:
        if start_block < 0 or end_block < start_block or end_block - start_block > 2_400:
            raise ValueError("block hash query exceeds bounded range")
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT block_number, block_hash
                FROM phase7_onchain_transfer_events
                WHERE chain=%s AND block_number BETWEEN %s AND %s
                GROUP BY block_number,block_hash
                ORDER BY block_number
                """,
                (chain, start_block, end_block),
            )
            rows = cursor.fetchall()
        result: dict[int, str] = {}
        for block_number, block_hash in rows:
            current = result.get(int(block_number))
            if current is not None and current != block_hash:
                raise ValueError("multiple canonical block hashes exist for one height")
            result[int(block_number)] = str(block_hash)
        return result

    def load_event_ids_for_block_range(
        self,
        chain: str,
        start_block: int,
        end_block: int,
    ) -> tuple[str, ...]:
        if start_block < 0 or end_block < start_block or end_block - start_block > 2_400:
            raise ValueError("event identity query exceeds bounded range")
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT event_id FROM phase7_onchain_transfer_events
                WHERE chain=%s AND block_number BETWEEN %s AND %s
                ORDER BY block_number,event_id LIMIT %s
                """,
                (chain, start_block, end_block, MAX_REORG_EVENTS + 1),
            )
            rows = cursor.fetchall()
        if len(rows) > MAX_REORG_EVENTS:
            raise ValueError("reorg event identity range exceeds bounded cap")
        return tuple(str(row[0]) for row in rows)

    def load_event_time_price(
        self,
        symbol: str,
        event_time: datetime,
        *,
        max_skew_seconds: int = 300,
    ) -> dict[str, Any] | None:
        event_time = _utc(event_time, "event_time")
        if max_skew_seconds <= 0:
            raise ValueError("max_skew_seconds must be positive")
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT source,exchange,exchange_timestamp,fetched_at,
                       snapshot->>'last_price' AS price
                FROM market_snapshots
                WHERE symbol=%s AND status='AVAILABLE'
                  AND exchange_timestamp IS NOT NULL
                  AND exchange_timestamp <= %s AND fetched_at <= %s
                  AND snapshot_timestamp <= %s
                  AND exchange_timestamp >= %s - (%s * interval '1 second')
                  AND snapshot ? 'last_price'
                ORDER BY exchange_timestamp DESC,fetched_at DESC
                LIMIT 1
                """,
                (symbol, event_time, event_time, event_time, event_time, max_skew_seconds),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return dict(zip(("source", "exchange", "exchange_timestamp", "fetched_at", "price"), row, strict=True))

    def load_context_asset_ids(
        self, window_open: datetime, window_close: datetime, *, limit: int = 8,
    ) -> tuple[dict[str, str], ...]:
        window_open = _utc(window_open, "window_open")
        window_close = _utc(window_close, "window_close")
        if window_close <= window_open or not 1 <= limit <= 32:
            raise ValueError("context asset query bounds are invalid")
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT chain,asset_id
                FROM phase7_asset_registry
                WHERE status='AVAILABLE'
                  AND ((chain='BITCOIN' AND asset_kind='NATIVE' AND symbol='BTC')
                    OR (chain='ETHEREUM' AND asset_kind='NATIVE' AND symbol='ETH')
                    OR (chain='ETHEREUM' AND asset_kind='ERC20' AND symbol IN ('USDT','USDC')))
                  AND effective_from < %s AND (effective_to IS NULL OR effective_to >= %s)
                ORDER BY chain,asset_id LIMIT %s
                """,
                (window_close, window_open, limit + 1),
            )
            rows = cursor.fetchall()
        if len(rows) > limit:
            raise Phase7ContextLimitError("context asset count exceeds bounded cap")
        return tuple({"chain": str(row[0]), "asset_id": str(row[1])} for row in rows)

    def load_context_window_keys(
        self,
        lookback_start: datetime,
        upper_bound: datetime,
        *,
        per_asset_timeframe_limit: int = 2_500,
    ) -> tuple[dict[str, Any], ...]:
        """Find bounded event-time windows using the canonical asset/time index."""
        lookback_start = _utc(lookback_start, "lookback_start")
        upper_bound = _utc(upper_bound, "upper_bound")
        if upper_bound <= lookback_start or not 1 <= per_asset_timeframe_limit <= 5_000:
            raise ValueError("context window scan bounds are invalid")
        assets = self.load_context_asset_ids(lookback_start, upper_bound)
        result: list[dict[str, Any]] = []
        for asset in assets:
            for timeframe, seconds in _WINDOW_TIMEFRAMES.items():
                with self.connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT window_open, max(processed_at) AS latest_processed_at
                        FROM (
                            SELECT to_timestamp(
                                       floor(extract(epoch FROM event_time) / %s) * %s
                                   ) AS window_open,
                                   processed_at
                            FROM phase7_onchain_transfer_events
                            WHERE asset_id=%s AND event_time >= %s AND event_time < %s
                        ) event_windows
                        GROUP BY window_open
                        ORDER BY window_open DESC LIMIT %s
                        """,
                        (seconds, seconds, asset["asset_id"], lookback_start, upper_bound,
                         per_asset_timeframe_limit + 1),
                    )
                    windows = cursor.fetchall()
                if len(windows) > per_asset_timeframe_limit:
                    raise Phase7ContextLimitError("context window scan exceeds per-asset cap")
                result.extend({
                    "chain": asset["chain"], "asset_id": asset["asset_id"],
                    "timeframe": timeframe, "window_open": row[0],
                    "latest_processed_at": row[1],
                } for row in windows)
                if len(result) > MAX_CONTEXT_WINDOWS:
                    raise Phase7ContextLimitError("context window scan exceeds total cap")
        timeframe_order = {name: index for index, name in enumerate(_WINDOW_TIMEFRAMES)}
        result.sort(key=lambda item: (
            -item["window_open"].timestamp(), timeframe_order[item["timeframe"]],
            item["chain"], item["asset_id"],
        ))
        return tuple(result)

    def load_context_events(
        self,
        chain: str,
        asset_id_text: str,
        window_open: datetime,
        window_close: datetime,
        *,
        limit: int = 10_000,
    ) -> tuple[OnChainTransferEvent, ...]:
        """Load one bounded canonical event window for deterministic Engine aggregation."""
        window_open = _utc(window_open, "window_open")
        window_close = _utc(window_close, "window_close")
        if window_close <= window_open or not 1 <= limit <= 10_000:
            raise ValueError("context event query bounds are invalid")
        columns = (
            "event_id", "chain", "block_number", "block_hash", "tx_hash", "tx_index",
            "event_index", "event_index_kind", "asset_kind", "contract_address", "registry_version",
            "from_address", "to_address", "from_address_set_ref", "raw_reference",
            "amount_raw", "decimals", "amount_usd",
            "valuation_price", "valuation_exchange", "valuation_source", "valuation_reason",
            "valuation_status", "valuation_exchange_timestamp", "valuation_fetched_at",
            "valuation_skew_seconds", "event_time", "observed_at", "fetched_at", "processed_at",
            "status", "reason", "finality_status", "source_id", "source_version",
            "source_reference", "source_hash", "schema_version", "normalization_version", "details",
        )
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT e.event_id,e.chain,e.block_number,e.block_hash,e.tx_hash,e.tx_index,
                       e.event_index,e.event_index_kind,ar.asset_kind,e.contract_address,
                       ar.registry_version,e.from_address,e.to_address,e.from_address_set_ref,
                       e.raw_reference,e.amount_raw,e.decimals,
                       e.amount_usd,e.valuation_price,e.valuation_exchange,e.valuation_source,
                       e.valuation_reason,e.valuation_status,e.valuation_exchange_timestamp,
                       e.valuation_fetched_at,e.valuation_skew_seconds,e.event_time,e.observed_at,
                       e.fetched_at,e.processed_at,e.status,e.reason,e.finality_status,e.source_id,
                       e.source_version,e.source_reference,e.source_hash,e.schema_version,
                       e.normalization_version,e.details
                FROM phase7_onchain_transfer_events e
                JOIN phase7_asset_registry ar ON ar.asset_id=e.asset_id
                WHERE e.chain=%s AND e.asset_id=%s
                  AND e.event_time >= %s AND e.event_time < %s
                ORDER BY e.event_time,e.block_number,e.tx_index NULLS FIRST,e.event_index NULLS FIRST
                LIMIT %s
                """,
                (chain, asset_id_text, window_open, window_close, limit + 1),
            )
            rows = cursor.fetchall()
        if len(rows) > limit:
            raise Phase7ContextLimitError("context event window exceeds bounded event cap")
        return tuple(_context_event(dict(zip(columns, row, strict=True))) for row in rows)

    def load_latest_spot_cvd(self, exchange: str, symbol: str) -> Decimal | None:
        if exchange != "binance" or symbol not in {"BTCUSDT", "ETHUSDT"}:
            raise ValueError("spot CVD lookup identity is not approved")
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT cvd FROM phase7_spot_flow_windows
                WHERE exchange=%s AND symbol=%s AND market_kind='SPOT'
                  AND timeframe='1m' AND cvd IS NOT NULL
                ORDER BY window_close DESC LIMIT 1
                """,
                (exchange, symbol),
            )
            row = cursor.fetchone()
        return Decimal(str(row[0])) if row is not None else None

    def upsert_asset_registry(self, rows: Iterable[Mapping[str, Any]]) -> int:
        values = []
        for row in rows:
            _require_keys(row, (
                "asset_id", "chain", "asset_kind", "contract_address", "symbol", "decimals",
                "registry_version", "effective_from", "effective_to", "source_id", "source_version",
                "source_reference", "snapshot_hash", "status", "created_at", "updated_at",
            ), "phase7_asset_registry")
            normalized = dict(row)
            for key in ("effective_from", "effective_to", "created_at", "updated_at"):
                if normalized[key] is not None:
                    normalized[key] = _utc(normalized[key], key)
            values.append(tuple(normalized[key] for key in (
                "asset_id", "chain", "asset_kind", "contract_address", "symbol", "decimals",
                "registry_version", "effective_from", "effective_to", "source_id", "source_version",
                "source_reference", "snapshot_hash", "status", "created_at", "updated_at",
            )))
        if not values:
            return 0
        with self.connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO phase7_asset_registry (
                    asset_id, chain, asset_kind, contract_address, symbol, decimals,
                    registry_version, effective_from, effective_to, source_id, source_version,
                    source_reference, snapshot_hash, status, created_at, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (asset_id) DO UPDATE SET
                    effective_to=EXCLUDED.effective_to,
                    source_id=EXCLUDED.source_id, source_version=EXCLUDED.source_version,
                    source_reference=EXCLUDED.source_reference, snapshot_hash=EXCLUDED.snapshot_hash,
                    status=EXCLUDED.status, updated_at=EXCLUDED.updated_at
                """,
                values,
            )
            affected = getattr(cursor, "rowcount", -1)
        return affected if affected >= 0 else len(values)

    def upsert_address_labels(self, snapshot: LabelSnapshot) -> int:
        if not isinstance(snapshot, LabelSnapshot):
            raise ValueError("address labels require a reviewed LabelSnapshot")
        columns = (
            "chain", "address", "category", "source_id", "source_version", "label_version",
            "confidence", "snapshot_hash", "source_reference", "effective_from", "effective_to",
            "observed_at", "updated_at", "status", "reason",
        )
        values = []
        for row in snapshot.to_rows():
            _require_keys(row, columns, "phase7_address_labels")
            normalized = dict(row)
            for key in ("effective_from", "effective_to", "observed_at", "updated_at"):
                if normalized[key] is not None:
                    normalized[key] = _utc(normalized[key], key)
            values.append(tuple(normalized[key] for key in columns))
        if not values:
            return 0
        with self.connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO phase7_address_labels (
                    chain,address,category,source_id,source_version,label_version,confidence,
                    snapshot_hash,source_reference,effective_from,effective_to,observed_at,updated_at,
                    status,reason
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (chain,address,label_version) DO UPDATE SET
                    category=EXCLUDED.category, source_id=EXCLUDED.source_id,
                    source_version=EXCLUDED.source_version, confidence=EXCLUDED.confidence,
                    snapshot_hash=EXCLUDED.snapshot_hash, source_reference=EXCLUDED.source_reference,
                    effective_from=EXCLUDED.effective_from, effective_to=EXCLUDED.effective_to,
                    observed_at=EXCLUDED.observed_at, updated_at=EXCLUDED.updated_at,
                    status=EXCLUDED.status, reason=EXCLUDED.reason
                """,
                values,
            )
        return len(values)

    def upsert_transfer_events(self, events: Iterable[OnChainTransferEvent]) -> int:
        bounded_events = tuple(islice(events, MAX_TRANSFER_BATCH + 1))
        if len(bounded_events) > MAX_TRANSFER_BATCH:
            raise ValueError("transfer event batch exceeds bounded cap")
        values = []
        for event in bounded_events:
            _require_event_identity(event)
            valuation = event.valuation
            values.append((
                event.event_id, event.identity.chain.value, event.block_number, event.block_hash,
                event.identity.tx_hash, event.identity.tx_index, event.identity.event_index,
                event.identity.event_index_kind.value, _asset_id_text(event.identity.asset_id),
                event.identity.asset_id.kind.value,
                event.identity.contract_address, event.from_address, event.to_address,
                _reference(event.from_address_set_reference), str(event.amount.amount_raw), event.amount.decimals,
                event.amount.amount_normalized, valuation.amount_usd, valuation.valuation_price,
                valuation.valuation_exchange, valuation.valuation_source,
                valuation.reason_code.value if valuation.reason_code is not None else None,
                valuation.status.value,
                _utc(valuation.valuation_exchange_timestamp, "valuation_exchange_timestamp")
                if valuation.valuation_exchange_timestamp is not None else None,
                _utc(valuation.valuation_fetched_at, "valuation_fetched_at")
                if valuation.valuation_fetched_at is not None else None,
                int(valuation.valuation_skew.total_seconds()) if valuation.valuation_skew is not None else None,
                _utc(event.event_time, "event_time"), _utc(event.provenance.observed_at, "observed_at"),
                _utc(event.provenance.fetched_at, "fetched_at"), _utc(event.provenance.processed_at, "processed_at"),
                event.status.value, event.reason_code.value, event.finality_status.value,
                event.provenance.source, event.provenance.source_version, event.provenance.source_reference,
                event.provenance.source_hash, event.schema_version, event.normalization_version,
                _reference(event.raw_reference), _bounded_json(dict(event.details), "details", max_bytes=8192),
            ))
        if not values:
            return 0
        with self.connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO phase7_onchain_transfer_events (
                    event_id,chain,block_number,block_hash,tx_hash,tx_index,event_index,event_index_kind,
                    asset_id,asset_kind,contract_address,from_address,to_address,from_address_set_ref,amount_raw,
                    decimals,amount_normalized,amount_usd,valuation_price,valuation_exchange,valuation_source,
                    valuation_reason,valuation_status,valuation_exchange_timestamp,valuation_fetched_at,
                    valuation_skew_seconds,event_time,
                    observed_at,fetched_at,processed_at,status,reason,finality_status,source_id,source_version,
                    source_reference,source_hash,schema_version,normalization_version,raw_reference,details
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (event_id) DO UPDATE SET
                    block_number=EXCLUDED.block_number, block_hash=EXCLUDED.block_hash,
                    tx_index=EXCLUDED.tx_index, event_index=EXCLUDED.event_index,
                    status=EXCLUDED.status, reason=EXCLUDED.reason,
                    finality_status=EXCLUDED.finality_status, processed_at=EXCLUDED.processed_at,
                    amount_usd=EXCLUDED.amount_usd, valuation_price=EXCLUDED.valuation_price,
                    valuation_exchange=EXCLUDED.valuation_exchange,
                    valuation_source=EXCLUDED.valuation_source,
                    valuation_reason=EXCLUDED.valuation_reason,
                    valuation_status=EXCLUDED.valuation_status,
                    valuation_exchange_timestamp=EXCLUDED.valuation_exchange_timestamp,
                    valuation_fetched_at=EXCLUDED.valuation_fetched_at,
                    valuation_skew_seconds=EXCLUDED.valuation_skew_seconds,
                    raw_reference=EXCLUDED.raw_reference, details=EXCLUDED.details
                WHERE phase7_onchain_transfer_events.chain = EXCLUDED.chain
                  AND phase7_onchain_transfer_events.block_hash = EXCLUDED.block_hash
                  AND phase7_onchain_transfer_events.tx_hash = EXCLUDED.tx_hash
                  AND phase7_onchain_transfer_events.event_index_kind = EXCLUDED.event_index_kind
                  AND phase7_onchain_transfer_events.event_index IS NOT DISTINCT FROM EXCLUDED.event_index
                  AND phase7_onchain_transfer_events.tx_index IS NOT DISTINCT FROM EXCLUDED.tx_index
                  AND phase7_onchain_transfer_events.asset_id = EXCLUDED.asset_id
                  AND phase7_onchain_transfer_events.asset_kind = EXCLUDED.asset_kind
                  AND phase7_onchain_transfer_events.contract_address IS NOT DISTINCT FROM EXCLUDED.contract_address
                """,
                values,
            )
            affected = getattr(cursor, "rowcount", -1)
        return affected if affected >= 0 else len(values)

    def persist_transfer_batch_and_checkpoint(
        self,
        events: Iterable[OnChainTransferEvent],
        checkpoint: Mapping[str, Any],
    ) -> tuple[int, int]:
        """Commit canonical events and their cursor as one database transaction."""
        with self.connection.transaction():
            event_count = self.upsert_transfer_events(events)
            checkpoint_count = self.upsert_checkpoint(checkpoint)
            if checkpoint_count != 1:
                raise ValueError("checkpoint did not advance; event batch rolled back")
        return event_count, checkpoint_count

    def persist_transfer_chunks_and_checkpoint(
        self,
        events: Iterable[OnChainTransferEvent],
        checkpoint: Mapping[str, Any],
    ) -> tuple[int, int]:
        """Persist a block through bounded insert calls and one cursor commit.

        The existing transfer batch cap applies to each executemany call, not
        the complete block. All chunks and the checkpoint share one transaction.
        """
        iterator = iter(events)
        event_count = 0
        with self.connection.transaction():
            while True:
                try:
                    first = next(iterator)
                except StopIteration:
                    break
                chunk = chain((first,), islice(iterator, MAX_TRANSFER_BATCH - 1))
                event_count += self.upsert_transfer_events(chunk)
            checkpoint_count = self.upsert_checkpoint(checkpoint)
            if checkpoint_count != 1:
                raise ValueError("checkpoint did not advance; transfer chunks rolled back")
        return event_count, checkpoint_count

    def persist_spot_windows_and_checkpoint(
        self,
        rows: Iterable[Mapping[str, Any]],
        checkpoint: Mapping[str, Any],
    ) -> tuple[int, int]:
        """Commit completed SPOT windows and their cursor as one idempotent unit."""
        with self.connection.transaction():
            row_count = self.upsert_windows("phase7_spot_flow_windows", rows)
            checkpoint_count = self.upsert_checkpoint(checkpoint)
            if checkpoint_count != 1:
                raise ValueError("spot checkpoint did not advance; windows rolled back")
        return row_count, checkpoint_count

    def persist_reorg_recovery(
        self,
        old_event_ids: Iterable[str],
        replacement_events: Iterable[OnChainTransferEvent],
        checkpoint: Mapping[str, Any],
        *,
        processed_at: datetime,
    ) -> tuple[int, int, int]:
        """Mark old observations and persist replacements/cursor atomically."""
        event_ids = tuple(islice(old_event_ids, MAX_REORG_EVENTS + 1))
        if not event_ids or any(not isinstance(event_id, str) or not event_id.strip() for event_id in event_ids):
            raise ValueError("reorg recovery requires bounded event IDs")
        if len(event_ids) > 10_000:
            raise ValueError("reorg recovery event IDs exceed bounded window")
        if len(set(event_ids)) != len(event_ids):
            raise ValueError("reorg recovery event IDs must be unique")
        replacement_iterator = iter(replacement_events)
        old_id_set = set(event_ids)
        replacement_id_set: set[str] = set()
        processed_at = _utc(processed_at, "processed_at")
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE phase7_onchain_transfer_events
                    SET status='STALE', reason='REORGED_EVENT', finality_status='REORGED', processed_at=%s
                    WHERE event_id = ANY(%s)
                    RETURNING event_id, block_hash
                    """,
                    (processed_at, list(event_ids)),
                )
                marked_rows = cursor.fetchall()
                marked_count = len(marked_rows)
                if marked_count != len(event_ids):
                    raise ValueError("reorg recovery did not mark every old event")
                old_block_hashes = {row[1] for row in marked_rows}
            replacement_count = 0
            sentinel = object()
            while True:
                first = next(replacement_iterator, sentinel)
                if first is sentinel:
                    break

                def validated_chunk(first=first):
                    for event in chain((first,), islice(replacement_iterator, MAX_TRANSFER_BATCH - 1)):
                        event_id = event.event_id
                        if event_id in old_id_set:
                            raise ValueError("reorg replacement must use new event identities")
                        if event_id in replacement_id_set:
                            raise ValueError("reorg replacement event IDs must be unique")
                        _require_event_identity(event)
                        if event.block_hash in old_block_hashes:
                            raise ValueError("reorg replacement must use a new block identity")
                        replacement_id_set.add(event_id)
                        yield event

                replacement_count += self.upsert_transfer_events(validated_chunk())
            checkpoint_count = self.upsert_checkpoint(checkpoint)
            if checkpoint_count != 1:
                raise ValueError("checkpoint did not advance; reorg recovery rolled back")
        return marked_count, replacement_count, checkpoint_count

    def upsert_checkpoint(self, row: Mapping[str, Any]) -> int:
        columns = (
            "source_id", "scope_kind", "scope_key", "cursor_kind", "cursor_value",
            "last_observed_cursor", "last_finalized_cursor", "last_block_hash", "parser_version",
            "schema_version", "status", "reason", "updated_at",
        )
        _require_keys(row, columns, "phase7_ingestion_checkpoints")
        normalized = dict(row)
        normalized["updated_at"] = _utc(normalized["updated_at"], "updated_at")
        CheckpointState.from_row(normalized)
        values = tuple(normalized[key] for key in columns)
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO phase7_ingestion_checkpoints (
                    source_id,scope_kind,scope_key,cursor_kind,cursor_value,last_observed_cursor,
                    last_finalized_cursor,last_block_hash,parser_version,schema_version,status,reason,updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (source_id,scope_kind,scope_key) DO UPDATE SET
                    cursor_kind=EXCLUDED.cursor_kind, cursor_value=EXCLUDED.cursor_value,
                    last_observed_cursor=COALESCE(
                        EXCLUDED.last_observed_cursor,
                        phase7_ingestion_checkpoints.last_observed_cursor
                    ),
                    last_finalized_cursor=COALESCE(
                        EXCLUDED.last_finalized_cursor,
                        phase7_ingestion_checkpoints.last_finalized_cursor
                    ),
                    last_block_hash=EXCLUDED.last_block_hash, parser_version=EXCLUDED.parser_version,
                    schema_version=EXCLUDED.schema_version, status=EXCLUDED.status,
                    reason=EXCLUDED.reason, updated_at=EXCLUDED.updated_at
                WHERE EXCLUDED.cursor_kind = phase7_ingestion_checkpoints.cursor_kind
                  AND EXCLUDED.cursor_value ~ '^[0-9]+$'
                  AND phase7_ingestion_checkpoints.cursor_value ~ '^[0-9]+$'
                  AND EXCLUDED.cursor_value::numeric >= phase7_ingestion_checkpoints.cursor_value::numeric
                  AND EXCLUDED.last_observed_cursor IS NOT NULL
                  AND (
                      phase7_ingestion_checkpoints.last_observed_cursor IS NULL
                      OR (
                          EXCLUDED.last_observed_cursor ~ '^[0-9]+$'
                          AND phase7_ingestion_checkpoints.last_observed_cursor ~ '^[0-9]+$'
                          AND EXCLUDED.last_observed_cursor::numeric >= phase7_ingestion_checkpoints.last_observed_cursor::numeric
                      )
                  )
                  AND (
                      EXCLUDED.last_finalized_cursor IS NULL
                      OR phase7_ingestion_checkpoints.last_finalized_cursor IS NULL
                      OR (
                          EXCLUDED.last_finalized_cursor ~ '^[0-9]+$'
                          AND phase7_ingestion_checkpoints.last_finalized_cursor ~ '^[0-9]+$'
                          AND EXCLUDED.last_finalized_cursor::numeric >= phase7_ingestion_checkpoints.last_finalized_cursor::numeric
                      )
                  )
                """,
                values,
            )
            return getattr(cursor, "rowcount", 1)

    def upsert_windows(self, table: str, rows: Iterable[Mapping[str, Any]]) -> int:
        """Persist aggregate rows through a fixed table/column allowlist."""
        if table not in self._WINDOW_TABLES:
            raise ValueError("unsupported Phase 7 window table")
        columns = self._WINDOW_TABLES[table]
        values = []
        for row in rows:
            normalized = dict(row)
            if table in {"phase7_onchain_flow_windows", "phase7_whale_flow_windows"}:
                normalized.setdefault("known_address_count", 0)
                normalized.setdefault("labeled_address_count", normalized.get("labeled_count", 0))
                normalized.setdefault("labeled_count", normalized.get("labeled_address_count", 0))
                normalized.setdefault("source_quality", "UNSPECIFIED")
                normalized.setdefault("coverage_status", normalized.get("status", "NOT_AVAILABLE"))
            for key in (
                "window_open", "window_close", "processed_at", "created_at",
                "event_time_first", "event_time_last",
            ):
                if key in normalized and normalized[key] is not None:
                    normalized[key] = _utc(normalized[key], key)
            _require_keys(normalized, columns, table)
            _validate_window_row(table, normalized)
            values.append(tuple(
                _bounded_json(normalized[key], key) if key in {"context_reference", "coverage"} else normalized[key]
                for key in columns
            ))
        if not values:
            return 0
        placeholders = ",".join(["%s"] * len(columns))
        column_sql = ",".join(columns)
        if table == "phase7_onchain_flow_windows":
            conflict = "chain,asset_id,timeframe,window_open,context_version,aggregation_scope"
        elif table == "phase7_whale_flow_windows":
            conflict = "chain,asset_id,threshold_version,timeframe,window_open,aggregation_version,aggregation_scope"
        elif table == "phase7_spot_flow_windows":
            conflict = "exchange,symbol,market_kind,timeframe,window_open,aggregation_version"
        else:
            conflict = "chain,asset_id,category,timeframe,window_open,aggregation_version"
        update_columns = ",".join(f"{column}=EXCLUDED.{column}" for column in columns if column not in {
            "chain", "asset_id", "timeframe", "window_open", "context_version", "aggregation_scope",
            "threshold_version", "aggregation_version", "exchange", "symbol", "market_kind", "category",
        })
        with self.connection.cursor() as cursor:
            cursor.executemany(
                f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders}) "
                f"ON CONFLICT ({conflict}) DO UPDATE SET {update_columns}",
                values,
            )
        return len(values)

    def upsert_stage1_enrichment(self, row: Mapping[str, Any]) -> int:
        columns = (
            "screening_run_id", "symbol", "context_reference", "coverage", "status", "reason",
            "normalization_version", "created_at", "processed_at",
        )
        _require_keys(row, columns, "stage1_phase7_context_enrichment")
        normalized = dict(row)
        for key in ("created_at", "processed_at"):
            normalized[key] = _utc(normalized[key], key)
        values = tuple(
            _bounded_json(normalized[key], key) if key in {"context_reference", "coverage"} else (
                normalized[key]
            )
            for key in columns
        )
        with self.connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO stage1_phase7_context_enrichment (
                    screening_run_id,symbol,context_reference,coverage,status,reason,
                    normalization_version,created_at,processed_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (screening_run_id,symbol) DO UPDATE SET
                    context_reference=EXCLUDED.context_reference, coverage=EXCLUDED.coverage,
                    status=EXCLUDED.status, reason=EXCLUDED.reason,
                    normalization_version=EXCLUDED.normalization_version,
                    processed_at=EXCLUDED.processed_at
                """,
                values,
            )
        return 1

    def cleanup(self, table: str, cutoff: datetime, *, batch_size: int = 1000, max_batches: int = 1) -> int:
        retention_columns = {
            "phase7_onchain_transfer_events": "processed_at",
            "phase7_address_labels": "updated_at",
            "phase7_onchain_flow_windows": "processed_at",
            "phase7_whale_flow_windows": "processed_at",
            "phase7_spot_flow_windows": "processed_at",
            "phase7_stablecoin_context": "processed_at",
            "phase7_ingestion_checkpoints": "updated_at",
            "stage1_phase7_context_enrichment": "processed_at",
        }
        if table not in retention_columns:
            raise ValueError("unsupported Phase 7 retention table")
        if batch_size <= 0 or max_batches <= 0:
            raise ValueError("batch bounds must be positive")
        total = 0
        for _ in range(max_batches):
            result = self.connection.execute(
                f"DELETE FROM {table} WHERE ctid IN ("
                f"SELECT ctid FROM {table} WHERE {retention_columns[table]} < %s "
                "ORDER BY " + retention_columns[table] + " LIMIT %s)",
                (_utc(cutoff, "cutoff"), batch_size),
            )
            deleted = int(getattr(result, "rowcount", 0) or 0)
            total += deleted
            if deleted < batch_size:
                break
        return total
