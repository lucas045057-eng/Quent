"""Pure Ethereum block and transfer-log normalization for Phase 7."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import re
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
    ReasonCode,
    UsdValuation,
)


class EthereumPayloadError(ValueError):
    """Raised when an Ethereum JSON-RPC payload is unsafe to normalize."""


TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
_HEX_256_RE = re.compile(r"^0x[0-9a-f]{64}$")
_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")
_MAX_UINT256 = 2**256 - 1
_MAX_EVENTS = 20_000
_MAX_TRANSACTIONS = 10_000
_MAX_LOGS = 20_000

DEFAULT_ETHEREUM_ASSETS: dict[str, dict[str, Any]] = {
    "0xdac17f958d2ee523a2206206994597c13d831ec7": {
        "symbol": "USDT", "decimals": 6, "registry_version": "ethereum-v1",
    },
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": {
        "symbol": "USDC", "decimals": 6, "registry_version": "ethereum-v1",
    },
}


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EthereumPayloadError(f"{field} must be an object")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EthereumPayloadError(f"{field} must be non-empty text")
    return value.strip()


def _quantity(value: Any, field: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) <= 2:
        raise EthereumPayloadError(f"{field} must be a 0x-prefixed quantity")
    digits = value[2:]
    if digits != "0" and digits.startswith("0"):
        raise EthereumPayloadError(f"{field} must not have leading zeroes")
    if any(char not in "0123456789abcdef" for char in digits):
        raise EthereumPayloadError(f"{field} must be lowercase hexadecimal")
    return int(digits, 16)


def _hash(value: Any, field: str) -> tuple[str, str]:
    text = _text(value, field)
    if not text.startswith("0x") or len(text) != 66 or any(char not in "0123456789abcdef" for char in text[2:]):
        raise EthereumPayloadError(f"{field} must be a lowercase 256-bit hash")
    return text, text[2:]


def _address(value: Any, field: str, *, allow_none: bool = False) -> str | None:
    if value is None and allow_none:
        return None
    text = _text(value, field)
    if not _ADDRESS_RE.fullmatch(text):
        raise EthereumPayloadError(f"{field} must be a lowercase EVM address")
    return text


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise EthereumPayloadError(f"{field} must be timezone-aware UTC")
    return value


def _uint256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _HEX_256_RE.fullmatch(value):
        raise EthereumPayloadError(f"{field} must be a 32-byte 0x-prefixed word")
    integer = int(value[2:], 16)
    if integer > _MAX_UINT256:
        raise EthereumPayloadError(f"{field} exceeds uint256")
    return str(integer)


class EthereumAssetRegistry:
    """Versioned allowlist used to resolve ERC-20 decimals without symbols."""

    def __init__(self, definitions: Mapping[str, Mapping[str, Any]] | None = None) -> None:
        source = DEFAULT_ETHEREUM_ASSETS if definitions is None else definitions
        self._definitions: dict[str, tuple[CanonicalAssetId, str, int]] = {}
        for address, definition in source.items():
            normalized = _address(address, "asset contract")
            assert normalized is not None
            definition = _mapping(definition, "asset definition")
            symbol = _text(definition.get("symbol"), "asset symbol")
            decimals = definition.get("decimals")
            if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 36:
                raise ValueError("asset decimals must be an integer between 0 and 36")
            version = _text(definition.get("registry_version"), "registry_version")
            asset_id = CanonicalAssetId(
                chain=Chain.ETHEREUM,
                kind=AssetKind.ERC20,
                contract_or_native=normalized,
                registry_version=version,
            )
            self._definitions[normalized] = (asset_id, symbol, decimals)

    def resolve(self, contract_address: str) -> tuple[CanonicalAssetId, str, int]:
        normalized = _address(contract_address, "contract address")
        assert normalized is not None
        try:
            return self._definitions[normalized]
        except KeyError:
            raise EthereumPayloadError("ERC-20 contract is not in the versioned allowlist") from None


class EthereumBlockParser:
    """Normalize Ethereum top-level value transfers and allowlisted ERC-20 logs."""

    def __init__(
        self,
        *,
        asset_registry: Mapping[str, Mapping[str, Any]] | EthereumAssetRegistry | None = None,
        native_registry_version: str = "ethereum-native-v1",
        confirmed_depth: int = 12,
        source_version: str = "ethereum-json-rpc-v1",
        normalization_version: str = "phase7-ethereum-v1",
        schema_version: str = "phase7-onchain-v1",
        max_valuation_skew: timedelta = timedelta(seconds=300),
    ) -> None:
        if isinstance(confirmed_depth, bool) or not isinstance(confirmed_depth, int) or confirmed_depth < 1:
            raise ValueError("confirmed_depth must be positive")
        if not isinstance(max_valuation_skew, timedelta) or max_valuation_skew <= timedelta(0):
            raise ValueError("max_valuation_skew must be positive")
        self.asset_registry = (
            asset_registry if isinstance(asset_registry, EthereumAssetRegistry)
            else EthereumAssetRegistry(asset_registry)
        )
        self.native_registry_version = native_registry_version
        self.confirmed_depth = confirmed_depth
        self.source_version = source_version
        self.normalization_version = normalization_version
        self.schema_version = schema_version
        self.max_valuation_skew = max_valuation_skew

    def parse_block(
        self,
        block: Mapping[str, Any],
        *,
        chain_id: int | str,
        logs: list[Mapping[str, Any]] | None = None,
        receipts: list[Mapping[str, Any]] | None = None,
        finality_tag: str | None = None,
        expected_block_hash: str | None = None,
        observed_at: datetime,
        fetched_at: datetime,
        processed_at: datetime,
    ) -> tuple[OnChainTransferEvent, ...]:
        if _quantity(chain_id, "chain_id") != 1:
            raise EthereumPayloadError("only Ethereum mainnet chain id 1 is supported")
        observed_at = _utc(observed_at, "observed_at")
        fetched_at = _utc(fetched_at, "fetched_at")
        processed_at = _utc(processed_at, "processed_at")
        if not observed_at <= fetched_at <= processed_at:
            raise EthereumPayloadError("provenance timestamps must be ordered")

        block = _mapping(block, "block")
        block_hash, source_hash = _hash(block.get("hash"), "block hash")
        if expected_block_hash is not None:
            expected, _ = _hash(expected_block_hash, "expected block hash")
        else:
            expected = None
        block_number = _quantity(block.get("number"), "block number")
        timestamp = _quantity(block.get("timestamp"), "block timestamp")
        try:
            event_time = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise EthereumPayloadError("block timestamp is outside supported UTC range") from None
        transactions = block.get("transactions")
        if not isinstance(transactions, list) or len(transactions) > _MAX_TRANSACTIONS:
            raise EthereumPayloadError("transactions must be a bounded list")
        tag = finality_tag or block.get("finalityTag")
        if tag not in {"latest", "safe", "finalized"}:
            raise EthereumPayloadError("finality_tag must be latest, safe, or finalized")
        confirmations = _quantity(block.get("confirmations", "0x0"), "confirmations")
        reorged = expected is not None and block_hash != expected
        finality, status, reason = self._state(tag, confirmations, reorged)
        provenance = Provenance(
            source="ethereum-json-rpc",
            source_version=self.source_version,
            source_reference=f"ethereum:block:{block_number}:{block_hash}",
            source_hash=source_hash,
            observed_at=observed_at,
            fetched_at=fetched_at,
            processed_at=processed_at,
        )

        receipt_map = self._receipts(receipts or [], block_hash, block_number)
        normalized_logs = logs or []
        if len(normalized_logs) > _MAX_LOGS:
            raise EthereumPayloadError("logs exceed bounded source batch")
        if normalized_logs and not receipt_map:
            raise EthereumPayloadError("ERC-20 logs require receipt verification")

        transaction_indexes: dict[str, int] = {}
        for position, transaction in enumerate(transactions):
            transaction = _mapping(transaction, "transaction")
            tx_hash, _ = _hash(transaction.get("hash"), "transaction hash")
            if tx_hash in transaction_indexes:
                raise EthereumPayloadError("block cannot contain duplicate transaction hashes")
            transaction_indexes[tx_hash] = _quantity(
                transaction.get("transactionIndex", f"0x{position:x}"), "transaction index"
            )
        missing_receipts = set(transaction_indexes) - set(receipt_map)
        if missing_receipts:
            raise EthereumPayloadError("every native transaction requires a verified receipt")
        extra_receipts = set(receipt_map) - set(transaction_indexes)
        if extra_receipts:
            raise EthereumPayloadError("receipt transaction is not present in block")
        for tx_hash, receipt in receipt_map.items():
            if _quantity(receipt.get("transactionIndex"), "receipt transaction index") != transaction_indexes[tx_hash]:
                raise EthereumPayloadError("receipt transaction index does not match block transaction")

        events: list[OnChainTransferEvent] = []
        for position, transaction in enumerate(transactions):
            events.extend(self._native_events(
                transaction, position=position, block_number=block_number,
                block_hash=block_hash, event_time=event_time, provenance=provenance,
                status=status, finality=finality, reason=reason,
            ))
            if len(events) > _MAX_EVENTS:
                raise EthereumPayloadError("event count exceeds bounded source batch")

        for log in normalized_logs:
            log = _mapping(log, "log")
            log_tx_hash, _ = _hash(log.get("transactionHash"), "log transaction hash")
            if log_tx_hash not in transaction_indexes:
                raise EthereumPayloadError("log transaction is not present in block")
            if _quantity(log.get("transactionIndex"), "log transaction index") != transaction_indexes[log_tx_hash]:
                raise EthereumPayloadError("log transaction index does not match block transaction")
            event = self._erc20_event(
                log, block_number=block_number, block_hash=block_hash, event_time=event_time,
                provenance=provenance, receipt_map=receipt_map, status=status,
                finality=finality, reason=reason,
            )
            events.append(event)
            if len(events) > _MAX_EVENTS:
                raise EthereumPayloadError("event count exceeds bounded source batch")
        return tuple(events)

    def _state(
        self, tag: str, confirmations: int, reorged: bool,
    ) -> tuple[FinalityStatus, DataStatus, ReasonCode]:
        if reorged:
            return FinalityStatus.REORGED, DataStatus.STALE, ReasonCode.REORGED_EVENT
        if tag == "finalized":
            return FinalityStatus.FINALIZED, DataStatus.AVAILABLE, ReasonCode.COMPLETE
        if tag == "safe" or confirmations >= self.confirmed_depth:
            return FinalityStatus.CONFIRMED, DataStatus.AVAILABLE, ReasonCode.COMPLETE
        return FinalityStatus.OBSERVED, DataStatus.PARTIAL, ReasonCode.PARTIAL_COVERAGE

    def _receipts(
        self, receipts: list[Mapping[str, Any]], block_hash: str, block_number: int,
    ) -> dict[str, Mapping[str, Any]]:
        if len(receipts) > _MAX_TRANSACTIONS:
            raise EthereumPayloadError("receipts exceed bounded source batch")
        result: dict[str, Mapping[str, Any]] = {}
        total_logs = 0
        for receipt in receipts:
            receipt = _mapping(receipt, "receipt")
            tx_hash, _ = _hash(receipt.get("transactionHash"), "receipt transaction hash")
            receipt_block_hash = receipt.get("blockHash")
            if receipt_block_hash is None or _hash(receipt_block_hash, "receipt block hash")[0] != block_hash:
                raise EthereumPayloadError("receipt block hash does not match block")
            if _quantity(receipt.get("blockNumber"), "receipt block number") != block_number:
                raise EthereumPayloadError("receipt block number does not match block")
            if _quantity(receipt.get("status"), "receipt status") != 1:
                raise EthereumPayloadError("reverted transaction receipt cannot provide transfer evidence")
            receipt_logs = receipt.get("logs", [])
            if not isinstance(receipt_logs, list) or len(receipt_logs) > _MAX_LOGS:
                raise EthereumPayloadError("receipt logs must be a list")
            total_logs += len(receipt_logs)
            if total_logs > _MAX_LOGS:
                raise EthereumPayloadError("receipt logs exceed bounded source batch")
            if tx_hash in result:
                raise EthereumPayloadError("duplicate transaction receipt")
            result[tx_hash] = receipt
        return result

    def _native_events(
        self, transaction: Any, *, position: int, block_number: int, block_hash: str,
        event_time: datetime, provenance: Provenance, status: DataStatus,
        finality: FinalityStatus, reason: ReasonCode,
    ) -> list[OnChainTransferEvent]:
        transaction = _mapping(transaction, "transaction")
        tx_hash, _ = _hash(transaction.get("hash"), "transaction hash")
        tx_index = _quantity(transaction.get("transactionIndex", f"0x{position:x}"), "transaction index")
        from_address = _address(transaction.get("from"), "from address", allow_none=True)
        to_address = _address(transaction.get("to"), "to address", allow_none=True)
        amount_raw = str(_quantity(transaction.get("value"), "transaction value"))
        asset_id = CanonicalAssetId(
            chain=Chain.ETHEREUM, kind=AssetKind.NATIVE, contract_or_native="NATIVE",
            registry_version=self.native_registry_version,
        )
        amount = CanonicalAmount(asset_id=asset_id, amount_raw=amount_raw, decimals=18)
        valuation = self._valuation(asset_id, event_time, "ETHUSDT")
        event = OnChainTransferEvent(
            event_id=f"eth:{block_hash}:{tx_hash}:tx-value",
            identity=OnChainEventIdentity(
                chain=Chain.ETHEREUM, tx_hash=tx_hash, tx_index=tx_index,
                event_index_kind=EventIndexKind.TX_VALUE, event_index=None,
                asset_id=asset_id, contract_address=None, block_hash=block_hash,
            ),
            block_number=block_number, block_hash=block_hash, event_time=event_time,
            from_address=from_address, to_address=to_address,
            from_address_set_reference=None, amount=amount, valuation=valuation,
            provenance=provenance, raw_reference=None, schema_version=self.schema_version,
            normalization_version=self.normalization_version,
            details=(("source_kind", "NATIVE_TX_VALUE"), ("internal_trace_coverage", "NOT_AVAILABLE")),
            status=status, finality_status=finality, reason_code=reason,
        )
        return [event]

    def _erc20_event(
        self, log: Any, *, block_number: int, block_hash: str, event_time: datetime,
        provenance: Provenance, receipt_map: Mapping[str, Mapping[str, Any]],
        status: DataStatus, finality: FinalityStatus, reason: ReasonCode,
    ) -> OnChainTransferEvent:
        log = _mapping(log, "log")
        contract = _address(log.get("address"), "log contract")
        assert contract is not None
        asset_id, _symbol, decimals = self.asset_registry.resolve(contract)
        topics = log.get("topics")
        if not isinstance(topics, list) or len(topics) != 3:
            raise EthereumPayloadError("ERC-20 Transfer requires exactly three topics")
        if topics[0] != TRANSFER_TOPIC:
            raise EthereumPayloadError("log topic is not ERC-20 Transfer")
        from_address = self._indexed_address(topics[1], "Transfer from topic")
        to_address = self._indexed_address(topics[2], "Transfer to topic")
        tx_hash, _ = _hash(log.get("transactionHash"), "log transaction hash")
        tx_index = _quantity(log.get("transactionIndex"), "log transaction index")
        log_index = _quantity(log.get("logIndex"), "log index")
        if _quantity(log.get("blockNumber"), "log block number") != block_number:
            raise EthereumPayloadError("log block number does not match block")
        log_block_hash = log.get("blockHash")
        if log_block_hash is not None and _hash(log_block_hash, "log block hash")[0] != block_hash:
            raise EthereumPayloadError("log block hash does not match block")
        receipt = receipt_map.get(tx_hash)
        if receipt is None or not self._receipt_contains_log(receipt, log):
            raise EthereumPayloadError("ERC-20 log is not verified by its transaction receipt")
        amount = CanonicalAmount(
            asset_id=asset_id, amount_raw=_uint256(log.get("data"), "Transfer data"), decimals=decimals,
        )
        valuation = self._valuation(asset_id, event_time, None)
        return OnChainTransferEvent(
            event_id=f"eth:{block_hash}:{tx_hash}:log:{contract}:{log_index}",
            identity=OnChainEventIdentity(
                chain=Chain.ETHEREUM, tx_hash=tx_hash, tx_index=tx_index,
                event_index_kind=EventIndexKind.LOG_INDEX, event_index=log_index,
                asset_id=asset_id, contract_address=contract, block_hash=block_hash,
            ),
            block_number=block_number, block_hash=block_hash, event_time=event_time,
            from_address=from_address, to_address=to_address,
            from_address_set_reference=None, amount=amount, valuation=valuation,
            provenance=provenance, raw_reference=None, schema_version=self.schema_version,
            normalization_version=self.normalization_version,
            details=(("source_kind", "ERC20_TRANSFER"), ("receipt_verified", True)),
            status=status, finality_status=finality, reason_code=reason,
        )

    @staticmethod
    def _indexed_address(topic: Any, field: str) -> str:
        if not isinstance(topic, str) or not _HEX_256_RE.fullmatch(topic) or topic[:26] != "0x" + "0" * 24:
            raise EthereumPayloadError(f"{field} must be a zero-padded indexed address")
        return "0x" + topic[-40:]

    @staticmethod
    def _receipt_contains_log(receipt: Mapping[str, Any], log: Mapping[str, Any]) -> bool:
        expected_tx_hash, _ = _hash(log.get("transactionHash"), "log transaction hash")
        expected_block_hash, _ = _hash(log.get("blockHash"), "log block hash")
        expected_block_number = _quantity(log.get("blockNumber"), "log block number")
        expected_tx_index = _quantity(log.get("transactionIndex"), "log transaction index")
        expected_log_index = _quantity(log.get("logIndex"), "log index")
        expected_contract = _address(log.get("address"), "log contract")
        expected_topics = log.get("topics")
        expected_data = log.get("data")
        if not isinstance(expected_topics, list) or not isinstance(expected_data, str):
            return False
        for receipt_log in receipt.get("logs", []):
            if not isinstance(receipt_log, Mapping):
                continue
            try:
                same_tx_hash, _ = _hash(receipt_log.get("transactionHash"), "receipt log transaction hash")
                same_block_hash, _ = _hash(receipt_log.get("blockHash"), "receipt log block hash")
                same_block_number = _quantity(receipt_log.get("blockNumber"), "receipt log block number")
                same_tx_index = _quantity(receipt_log.get("transactionIndex"), "receipt log transaction index")
                same_index = _quantity(receipt_log.get("logIndex"), "receipt log index")
                same_contract = _address(receipt_log.get("address"), "receipt log contract")
                same_topics = receipt_log.get("topics")
                same_data = receipt_log.get("data")
            except EthereumPayloadError:
                continue
            if (
                same_tx_hash == expected_tx_hash
                and same_block_hash == expected_block_hash
                and same_block_number == expected_block_number
                and same_tx_index == expected_tx_index
                and same_index == expected_log_index
                and same_contract == expected_contract
                and same_topics == expected_topics
                and same_data == expected_data
            ):
                return True
        return False

    def _valuation(
        self, asset_id: CanonicalAssetId, event_time: datetime, symbol: str | None,
    ) -> UsdValuation:
        return UsdValuation(
            asset_id=asset_id, event_time=event_time, amount_usd=None,
            valuation_price=None, valuation_exchange=None, valuation_source=None,
            valuation_symbol=symbol, market_kind=MarketKind.SPOT,
            valuation_exchange_timestamp=None, valuation_fetched_at=None,
            valuation_skew=None, max_valuation_skew=self.max_valuation_skew,
            status=DataStatus.NOT_AVAILABLE,
            reason_code=ReasonCode.VALUATION_NOT_AVAILABLE,
        )
