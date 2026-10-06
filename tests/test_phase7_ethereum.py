from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from quant_phase7.contracts import DataStatus, EventIndexKind, FinalityStatus, ReasonCode
from quant_phase7.ethereum import EthereumBlockParser, EthereumPayloadError


NOW = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
BLOCK_HASH = "0x" + "ab" * 32
TX_HASH = "0x" + "01" * 32
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
FROM = "0x1111111111111111111111111111111111111111"
TO = "0x2222222222222222222222222222222222222222"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def _topic_address(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


def _log(*, failed: bool = False) -> dict:
    return {
        "address": USDT,
        "topics": [TRANSFER_TOPIC, _topic_address(FROM), _topic_address(TO)],
        "data": "0x" + f"{1_000_000:064x}",
        "logIndex": "0x2",
        "transactionHash": TX_HASH,
        "transactionIndex": "0x0",
        "blockNumber": "0x64",
        "blockHash": BLOCK_HASH,
        "receiptStatus": "0x0" if failed else "0x1",
    }


def _block(*, tag: str = "finalized", transactions=None) -> dict:
    return {
        "number": "0x64",
        "hash": BLOCK_HASH,
        "timestamp": "0x6ab2b800",
        "transactions": transactions if transactions is not None else [{
            "hash": TX_HASH,
            "transactionIndex": "0x0",
            "from": FROM,
            "to": TO,
            "value": "0xde0b6b3a7640000",
        }],
        "finalityTag": tag,
    }


def _receipt(log: dict | None = None, *, status: str = "0x1", block_hash: str = BLOCK_HASH) -> dict:
    return {
        "transactionHash": TX_HASH,
        "blockHash": block_hash,
        "blockNumber": "0x64",
        "transactionIndex": "0x0",
        "status": status,
        "logs": [] if log is None else [log],
    }


def test_native_value_and_allowlisted_erc20_log_are_distinct_events():
    log = _log()
    events = EthereumBlockParser().parse_block(
        _block(),
        chain_id="0x1",
        logs=[log],
        receipts=[_receipt(log)],
        observed_at=NOW,
        fetched_at=NOW,
        processed_at=NOW,
    )
    assert len(events) == 2
    native, token = events
    assert native.identity.event_index_kind is EventIndexKind.TX_VALUE
    assert native.identity.event_index is None
    assert native.identity.tx_index == 0
    assert native.amount.amount_raw == "1000000000000000000"
    assert native.amount.decimals == 18
    assert native.from_address == FROM
    assert native.to_address == TO
    assert dict(native.details)["internal_trace_coverage"] == "NOT_AVAILABLE"

    assert token.identity.event_index_kind is EventIndexKind.LOG_INDEX
    assert token.identity.event_index == 2
    assert token.identity.contract_address == USDT
    assert token.amount.amount_raw == "1000000"
    assert token.amount.amount_normalized == Decimal("1")
    assert token.amount.decimals == 6
    assert token.from_address == FROM
    assert token.to_address == TO
    assert token.finality_status is FinalityStatus.FINALIZED
    assert token.status is DataStatus.AVAILABLE
    assert token.valuation.status is DataStatus.NOT_AVAILABLE
    assert token.valuation.reason_code is ReasonCode.VALUATION_NOT_AVAILABLE


def test_registry_version_and_decimals_are_not_inferred_from_symbol():
    log = _log()
    parser = EthereumBlockParser(
        asset_registry={USDT: {"symbol": "USDT", "decimals": 6, "registry_version": "eth-reg-2"}}
    )
    event = parser.parse_block(
        _block(), chain_id="0x1", logs=[log], receipts=[_receipt(log)],
        observed_at=NOW, fetched_at=NOW, processed_at=NOW,
    )[1]
    assert event.identity.asset_id.registry_version == "eth-reg-2"
    assert event.identity.asset_id.contract_or_native == USDT


def test_safe_and_latest_have_different_finality_semantics():
    safe = EthereumBlockParser(confirmed_depth=12).parse_block(
        _block(tag="safe"), chain_id="0x1", receipts=[_receipt()], observed_at=NOW, fetched_at=NOW, processed_at=NOW,
    )[0]
    latest = EthereumBlockParser(confirmed_depth=12).parse_block(
        _block(tag="latest"), chain_id="0x1", receipts=[_receipt()], observed_at=NOW, fetched_at=NOW, processed_at=NOW,
    )[0]
    assert safe.finality_status is FinalityStatus.CONFIRMED
    assert safe.status is DataStatus.AVAILABLE
    assert latest.finality_status is FinalityStatus.OBSERVED
    assert latest.status is DataStatus.PARTIAL


def test_block_hash_replacement_is_reorged_and_not_available():
    original = EthereumBlockParser().parse_block(
        _block(), chain_id="0x1", receipts=[_receipt()],
        observed_at=NOW, fetched_at=NOW, processed_at=NOW,
    )[0]
    replacement = _block()
    replacement["hash"] = "0x" + "cd" * 32
    event = EthereumBlockParser().parse_block(
        replacement,
        chain_id="0x1",
        expected_block_hash=BLOCK_HASH,
        receipts=[_receipt(block_hash=replacement["hash"])],
        observed_at=NOW,
        fetched_at=NOW,
        processed_at=NOW,
    )[0]
    assert event.status is DataStatus.STALE
    assert event.finality_status is FinalityStatus.REORGED
    assert event.reason_code is ReasonCode.REORGED_EVENT
    assert event.identity.identity_key != original.identity.identity_key
    assert event.event_id != original.event_id


@pytest.mark.parametrize("chain_id", ["0x2", 5, "bad"])
def test_non_mainnet_chain_id_is_rejected(chain_id):
    with pytest.raises(EthereumPayloadError):
        EthereumBlockParser().parse_block(
            _block(), chain_id=chain_id, receipts=[_receipt()], observed_at=NOW, fetched_at=NOW, processed_at=NOW,
        )


def test_failed_receipt_and_unallowlisted_contract_are_rejected():
    log = _log()
    with pytest.raises(EthereumPayloadError):
        EthereumBlockParser().parse_block(
            _block(), chain_id="0x1", logs=[log], receipts=[_receipt(log, status="0x0")],
            observed_at=NOW, fetched_at=NOW, processed_at=NOW,
        )

    unknown = _log()
    unknown["address"] = "0x" + "33" * 20
    with pytest.raises(EthereumPayloadError):
        EthereumBlockParser().parse_block(
            _block(), chain_id="0x1", logs=[unknown], receipts=[_receipt(unknown)],
            observed_at=NOW, fetched_at=NOW, processed_at=NOW,
        )


def test_transfer_topic_and_log_identity_are_validated():
    log = _log()
    log["topics"] = [TRANSFER_TOPIC, _topic_address(FROM)]
    with pytest.raises(EthereumPayloadError):
        EthereumBlockParser().parse_block(
            _block(), chain_id="0x1", logs=[log], receipts=[_receipt(log)],
            observed_at=NOW, fetched_at=NOW, processed_at=NOW,
        )

    log = _log()
    log["logIndex"] = "0x"
    with pytest.raises(EthereumPayloadError):
        EthereumBlockParser().parse_block(
            _block(), chain_id="0x1", logs=[log], receipts=[_receipt(log)],
            observed_at=NOW, fetched_at=NOW, processed_at=NOW,
        )


def test_internal_eth_trace_is_not_fabricated_and_receipt_is_required_for_logs():
    log = _log()
    with pytest.raises(EthereumPayloadError):
        EthereumBlockParser().parse_block(
            _block(), chain_id="0x1", logs=[log], receipts=[],
            observed_at=NOW, fetched_at=NOW, processed_at=NOW,
        )

    event = EthereumBlockParser().parse_block(
        _block(), chain_id="0x1", receipts=[_receipt()], observed_at=NOW, fetched_at=NOW, processed_at=NOW,
    )[0]
    assert dict(event.details)["internal_trace_coverage"] == "NOT_AVAILABLE"


def test_native_value_requires_a_successful_receipt():
    with pytest.raises(EthereumPayloadError):
        EthereumBlockParser().parse_block(
            _block(), chain_id="0x1", receipts=[],
            observed_at=NOW, fetched_at=NOW, processed_at=NOW,
        )
    with pytest.raises(EthereumPayloadError):
        EthereumBlockParser().parse_block(
            _block(), chain_id="0x1", receipts=[_receipt(status="0x0")],
            observed_at=NOW, fetched_at=NOW, processed_at=NOW,
        )


def test_json_rpc_quantities_must_be_canonical_hex():
    with pytest.raises(EthereumPayloadError):
        EthereumBlockParser().parse_block(
            _block(), chain_id="0x01", receipts=[_receipt()],
            observed_at=NOW, fetched_at=NOW, processed_at=NOW,
        )
    block = _block()
    block["transactions"][0]["value"] = 1
    with pytest.raises(EthereumPayloadError):
        EthereumBlockParser().parse_block(
            block, chain_id="0x1", receipts=[_receipt()],
            observed_at=NOW, fetched_at=NOW, processed_at=NOW,
        )


def test_receipt_must_match_block_and_bound_receipt_logs():
    receipt = _receipt()
    receipt["blockHash"] = "0x" + "cd" * 32
    with pytest.raises(EthereumPayloadError):
        EthereumBlockParser().parse_block(
            _block(), chain_id="0x1", receipts=[receipt],
            observed_at=NOW, fetched_at=NOW, processed_at=NOW,
        )
    receipt = _receipt()
    receipt["transactionIndex"] = "0x1"
    with pytest.raises(EthereumPayloadError):
        EthereumBlockParser().parse_block(
            _block(), chain_id="0x1", receipts=[receipt],
            observed_at=NOW, fetched_at=NOW, processed_at=NOW,
        )


def test_receipt_log_must_match_transfer_topics_and_data():
    log = _log()
    receipt_log = dict(log)
    receipt_log["data"] = "0x" + f"{2_000_000:064x}"
    with pytest.raises(EthereumPayloadError):
        EthereumBlockParser().parse_block(
            _block(), chain_id="0x1", logs=[log], receipts=[_receipt(receipt_log)],
            observed_at=NOW, fetched_at=NOW, processed_at=NOW,
        )

    receipt_log = dict(log)
    receipt_log["topics"] = [TRANSFER_TOPIC, _topic_address(TO), _topic_address(FROM)]
    with pytest.raises(EthereumPayloadError):
        EthereumBlockParser().parse_block(
            _block(), chain_id="0x1", logs=[log], receipts=[_receipt(receipt_log)],
            observed_at=NOW, fetched_at=NOW, processed_at=NOW,
        )
    receipt = _receipt()
    receipt["blockNumber"] = "0x65"
    with pytest.raises(EthereumPayloadError):
        EthereumBlockParser().parse_block(
            _block(), chain_id="0x1", receipts=[receipt],
            observed_at=NOW, fetched_at=NOW, processed_at=NOW,
        )


def test_empty_ethereum_block_is_valid_and_has_no_transfer_events():
    block = _block()
    block["transactions"] = []
    assert EthereumBlockParser().parse_block(
        block, chain_id="0x1", observed_at=NOW, fetched_at=NOW, processed_at=NOW,
    ) == ()
