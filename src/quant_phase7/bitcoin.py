"""Pure Bitcoin block/UTXO normalization for Phase 7.

The parser deliberately accepts provider payloads only at this boundary.  It
does not perform RPC, infer wallet ownership, or turn a Bitcoin transaction
into a single transfer: every output is its own canonical event.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any

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


class BitcoinPayloadError(ValueError):
    """Raised when a Bitcoin provider payload cannot be normalized safely."""


_HASH_LENGTH = 64
_SATOSHIS_PER_BTC = Decimal(100_000_000)
_MAX_SATOSHIS = 21_000_000 * 100_000_000
_MAX_TXS = 10_000
_MAX_INPUTS = 10_000
_MAX_OUTPUTS = 10_000


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BitcoinPayloadError(f"{field_name} must be an object")
    return value


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BitcoinPayloadError(f"{field_name} must be non-empty text")
    return value.strip()


def _hash(value: Any, field_name: str) -> str:
    normalized = _text(value, field_name)
    if len(normalized) != _HASH_LENGTH or any(char not in "0123456789abcdef" for char in normalized):
        raise BitcoinPayloadError(f"{field_name} must be lowercase 256-bit hex")
    return normalized


def _nonnegative_int(value: Any, field_name: str, *, allow_negative_one: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BitcoinPayloadError(f"{field_name} must be an integer")
    if allow_negative_one and value == -1:
        return value
    if value < 0:
        raise BitcoinPayloadError(f"{field_name} must be non-negative")
    return value


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise BitcoinPayloadError(f"{field_name} must be timezone-aware UTC")
    return value


def _amount_satoshis(value: Any) -> tuple[str, Decimal]:
    # The JSON boundary must use ``parse_float=Decimal`` (or an equivalent
    # exact decoder).  Accepting Python float here would make an already-lost
    # source precision look canonical merely because ``str(float)`` is short.
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise BitcoinPayloadError("output value must be numeric")
    try:
        decimal_value = Decimal(str(value))
        scaled = decimal_value * _SATOSHIS_PER_BTC
        satoshis = scaled.to_integral_exact()
    except (InvalidOperation, ValueError, OverflowError):
        raise BitcoinPayloadError("output value must be a finite whole-satoshi amount") from None
    if not decimal_value.is_finite() or decimal_value < 0 or scaled != satoshis:
        raise BitcoinPayloadError("output value must be a finite whole-satoshi amount")
    integer_satoshis = int(satoshis)
    if integer_satoshis > _MAX_SATOSHIS:
        raise BitcoinPayloadError("output value exceeds Bitcoin supply bound")
    return str(integer_satoshis), decimal_value


def _script_address(script: Any) -> str | None:
    if not isinstance(script, Mapping):
        return None
    address = script.get("address")
    if isinstance(address, str) and address.strip():
        return address.strip()
    addresses = script.get("addresses")
    if isinstance(addresses, list):
        candidates = [item.strip() for item in addresses if isinstance(item, str) and item.strip()]
        if len(candidates) == 1:
            return candidates[0]
    return None


def _input_addresses(vin: list[Any]) -> tuple[str, ...]:
    addresses: set[str] = set()
    for item in vin:
        entry = _mapping(item, "vin entry")
        prevout = entry.get("prevout")
        if not isinstance(prevout, Mapping):
            continue
        address = _script_address(prevout.get("scriptPubKey"))
        if address is not None:
            addresses.add(address)
    return tuple(sorted(addresses))


def _sender_reference(txid: str, addresses: tuple[str, ...]) -> RawReference | None:
    if not addresses:
        return None
    serialized = json.dumps(addresses, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(serialized).hexdigest()
    return RawReference(
        reference=f"bitcoin:{txid}:inputs",
        content_hash=digest,
        byte_size=len(serialized),
    )


class BitcoinBlockParser:
    """Normalize a structurally bounded Bitcoin Core-compatible block."""

    def __init__(
        self,
        *,
        registry_version: str = "btc-v1",
        confirmed_depth: int = 3,
        finalized_depth: int = 6,
        source_version: str = "bitcoin-core-v1",
        normalization_version: str = "phase7-bitcoin-v1",
        schema_version: str = "phase7-onchain-v1",
        max_valuation_skew: timedelta = timedelta(seconds=300),
    ) -> None:
        if isinstance(confirmed_depth, bool) or not isinstance(confirmed_depth, int) or confirmed_depth < 1:
            raise ValueError("confirmed_depth must be positive")
        if isinstance(finalized_depth, bool) or not isinstance(finalized_depth, int):
            raise ValueError("finalized_depth must be an integer")
        if finalized_depth < confirmed_depth:
            raise ValueError("finalized_depth cannot precede confirmed_depth")
        if not isinstance(max_valuation_skew, timedelta) or max_valuation_skew <= timedelta(0):
            raise ValueError("max_valuation_skew must be positive")
        self.registry_version = registry_version
        self.confirmed_depth = confirmed_depth
        self.finalized_depth = finalized_depth
        self.source_version = source_version
        self.normalization_version = normalization_version
        self.schema_version = schema_version
        self.max_valuation_skew = max_valuation_skew

    def parse_block(
        self,
        block: Mapping[str, Any],
        *,
        observed_at: datetime,
        fetched_at: datetime,
        processed_at: datetime,
        expected_block_hash: str | None = None,
    ) -> tuple[OnChainTransferEvent, ...]:
        observed_at = _utc(observed_at, "observed_at")
        fetched_at = _utc(fetched_at, "fetched_at")
        processed_at = _utc(processed_at, "processed_at")
        if not observed_at <= fetched_at <= processed_at:
            raise BitcoinPayloadError("provenance timestamps must be ordered")

        block = _mapping(block, "block")
        block_hash = _hash(block.get("hash"), "block hash")
        if expected_block_hash is not None:
            expected_block_hash = _hash(expected_block_hash, "expected block hash")
        height = _nonnegative_int(block.get("height"), "block height")
        block_time_raw = _nonnegative_int(block.get("time"), "block time")
        try:
            event_time = datetime.fromtimestamp(block_time_raw, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise BitcoinPayloadError("block time is outside supported UTC range") from None

        confirmations = _nonnegative_int(
            block.get("confirmations"), "confirmations", allow_negative_one=True
        )
        transactions = block.get("tx")
        if not isinstance(transactions, list) or not transactions or len(transactions) > _MAX_TXS:
            raise BitcoinPayloadError("tx must be a bounded list")

        reorged = confirmations == -1 or (
            expected_block_hash is not None and block_hash != expected_block_hash
        )
        finality, status, reason = self._state(confirmations, reorged)
        asset_id = CanonicalAssetId(
            chain=Chain.BITCOIN,
            kind=AssetKind.NATIVE,
            contract_or_native="NATIVE",
            registry_version=self.registry_version,
        )
        provenance = Provenance(
            source="bitcoin-core",
            source_version=self.source_version,
            source_reference=f"bitcoin:block:{height}:{block_hash}",
            source_hash=block_hash,
            observed_at=observed_at,
            fetched_at=fetched_at,
            processed_at=processed_at,
        )
        valuation = UsdValuation(
            asset_id=asset_id,
            event_time=event_time,
            amount_usd=None,
            valuation_price=None,
            valuation_exchange=None,
            valuation_source=None,
            valuation_symbol="BTCUSDT",
            market_kind=MarketKind.SPOT,
            valuation_exchange_timestamp=None,
            valuation_fetched_at=None,
            valuation_skew=None,
            max_valuation_skew=self.max_valuation_skew,
            status=DataStatus.NOT_AVAILABLE,
            reason_code=ReasonCode.VALUATION_NOT_AVAILABLE,
        )

        events: list[OnChainTransferEvent] = []
        for transaction in transactions:
            parsed = self._parse_transaction(
                transaction,
                block_hash=block_hash,
                block_height=height,
                event_time=event_time,
                asset_id=asset_id,
                valuation=valuation,
                provenance=provenance,
                confirmations=confirmations,
                status=status,
                finality=finality,
                reason=reason,
            )
            events.extend(parsed)
        return tuple(events)

    def _state(
        self, confirmations: int, reorged: bool
    ) -> tuple[FinalityStatus, DataStatus, ReasonCode]:
        if reorged:
            return FinalityStatus.REORGED, DataStatus.STALE, ReasonCode.REORGED_EVENT
        if confirmations >= self.finalized_depth:
            return FinalityStatus.FINALIZED, DataStatus.AVAILABLE, ReasonCode.COMPLETE
        if confirmations >= self.confirmed_depth:
            return FinalityStatus.CONFIRMED, DataStatus.AVAILABLE, ReasonCode.COMPLETE
        return FinalityStatus.OBSERVED, DataStatus.PARTIAL, ReasonCode.PARTIAL_COVERAGE

    def _parse_transaction(
        self,
        transaction: Any,
        *,
        block_hash: str,
        block_height: int,
        event_time: datetime,
        asset_id: CanonicalAssetId,
        valuation: UsdValuation,
        provenance: Provenance,
        confirmations: int,
        status: DataStatus,
        finality: FinalityStatus,
        reason: ReasonCode,
    ) -> list[OnChainTransferEvent]:
        transaction = _mapping(transaction, "transaction")
        txid = _hash(transaction.get("txid"), "transaction txid")
        vin = transaction.get("vin")
        vout = transaction.get("vout")
        if not isinstance(vin, list) or not vin or len(vin) > _MAX_INPUTS:
            raise BitcoinPayloadError("vin must be a bounded non-empty list")
        if not isinstance(vout, list) or not vout or len(vout) > _MAX_OUTPUTS:
            raise BitcoinPayloadError("vout must be a bounded list")

        coinbase_entries = [entry for entry in vin if isinstance(entry, Mapping) and "coinbase" in entry]
        coinbase = bool(coinbase_entries)
        if coinbase:
            if len(vin) != 1 or len(coinbase_entries) != 1:
                raise BitcoinPayloadError("coinbase transaction must have exactly one coinbase input")
            coinbase_value = vin[0].get("coinbase")
            if not isinstance(coinbase_value, str) or not coinbase_value.strip():
                raise BitcoinPayloadError("coinbase field must be non-empty text")
            if any(key in vin[0] for key in ("txid", "vout", "prevout")):
                raise BitcoinPayloadError("coinbase input cannot carry a prevout")

        input_addresses = () if coinbase else _input_addresses(vin)
        sender_reference = _sender_reference(txid, input_addresses)
        from_address = input_addresses[0] if len(input_addresses) == 1 else None

        seen_indexes: set[int] = set()
        events: list[OnChainTransferEvent] = []
        for output in vout:
            output = _mapping(output, "vout entry")
            index = _nonnegative_int(output.get("n"), "vout index")
            if index in seen_indexes:
                raise BitcoinPayloadError("vout indexes must be unique")
            seen_indexes.add(index)
            raw_amount, _ = _amount_satoshis(output.get("value"))
            amount = CanonicalAmount(asset_id=asset_id, amount_raw=raw_amount, decimals=8)
            destination = _script_address(output.get("scriptPubKey"))
            details = (
                ("coinbase", coinbase),
                ("confirmations", confirmations),
                ("main_chain", finality is not FinalityStatus.REORGED),
                ("vout_index", index),
            )
            events.append(
                OnChainTransferEvent(
                    event_id=f"btc:{block_hash}:{txid}:{index}",
                    identity=OnChainEventIdentity(
                        chain=Chain.BITCOIN,
                        tx_hash=txid,
                        tx_index=None,
                        event_index_kind=EventIndexKind.VOUT_INDEX,
                        event_index=index,
                        asset_id=asset_id,
                        contract_address=None,
                        block_hash=block_hash,
                    ),
                    block_number=block_height,
                    block_hash=block_hash,
                    event_time=event_time,
                    from_address=from_address,
                    to_address=destination,
                    from_address_set_reference=sender_reference,
                    amount=amount,
                    valuation=valuation,
                    provenance=provenance,
                    raw_reference=None,
                    schema_version=self.schema_version,
                    normalization_version=self.normalization_version,
                    details=details,
                    status=status,
                    finality_status=finality,
                    reason_code=reason,
                )
            )
        return events
