from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_phase7.bitcoin import BitcoinBlockParser, BitcoinPayloadError
from quant_phase7.contracts import DataStatus, EventIndexKind, FinalityStatus, ReasonCode


NOW = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
BLOCK_HASH = "ab" * 32
TX_HASH = "01" * 32


def _block(*, confirmations: int = 6, txs=None) -> dict:
    return {
        "hash": BLOCK_HASH,
        "height": 100,
        "time": 1790035200,
        "confirmations": confirmations,
        "tx": txs if txs is not None else [{
            "txid": TX_HASH,
            "vin": [{
                "txid": "02" * 32,
                "vout": 3,
                "prevout": {"value": "0.40000000", "scriptPubKey": {"address": "bc1qsource"}},
            }],
            "vout": [
                {"n": 0, "value": "0.25000000", "scriptPubKey": {"address": "bc1qdest"}},
                {"n": 1, "value": "0.10000000", "scriptPubKey": {"addresses": ["bc1qchange"]}},
            ],
        }],
    }


def _block_with_output_event_count(event_count: int) -> dict:
    """Build a bounded Core-shaped block with a deterministic output count."""
    if event_count < 1:
        raise ValueError("event_count must be positive")
    txs = [{
        "txid": TX_HASH,
        "vin": [{"coinbase": "coinbase-data"}],
        "vout": [{"n": 0, "value": "6.25000000", "scriptPubKey": {"address": "bc1qminer"}}],
    }]
    remaining = event_count - 1
    tx_index = 1
    while remaining:
        output_count = min(2, remaining)
        txid = f"{tx_index + 1:064x}"
        previous_txid = f"{tx_index + 100_000:064x}"
        txs.append({
            "txid": txid,
            "vin": [{"txid": previous_txid, "vout": 0}],
            "vout": [
                {"n": index, "value": "0.00000001", "scriptPubKey": {"address": "bc1qdest"}}
                for index in range(output_count)
            ],
        })
        remaining -= output_count
        tx_index += 1
    return _block(txs=txs)


def test_bitcoin_outputs_use_vout_identity_and_exact_satoshis():
    # 2026-09-22T00:00:00Z, kept as an integer source timestamp fixture.
    block = _block()
    events = BitcoinBlockParser().parse_block(
        block,
        observed_at=NOW,
        fetched_at=NOW,
        processed_at=NOW,
    )
    assert len(events) == 2
    assert [event.identity.event_index_kind for event in events] == [
        EventIndexKind.VOUT_INDEX, EventIndexKind.VOUT_INDEX,
    ]
    assert [event.identity.event_index for event in events] == [0, 1]
    assert events[0].amount.amount_raw == "25000000"
    assert events[0].amount.amount_normalized == Decimal("0.25")
    assert events[0].to_address == "bc1qdest"
    assert events[1].to_address == "bc1qchange"
    assert all(event.status is DataStatus.AVAILABLE for event in events)
    assert all(event.finality_status is FinalityStatus.FINALIZED for event in events)
    assert events[0].valuation.status is DataStatus.NOT_AVAILABLE
    assert events[0].valuation.reason_code is ReasonCode.VALUATION_NOT_AVAILABLE
    assert events[0].provenance.source == "bitcoin-core"
    assert events[0].provenance.observed_at == NOW


@pytest.mark.parametrize(
    ("event_count", "expected_chunks"),
    [(9_999, 1), (10_000, 1), (10_001, 2), (12_665, 2)],
)
def test_legal_bitcoin_block_can_exceed_the_persistence_chunk_size(event_count, expected_chunks):
    events = BitcoinBlockParser().parse_block(
        _block_with_output_event_count(event_count),
        observed_at=NOW,
        fetched_at=NOW,
        processed_at=NOW,
    )

    assert len(events) == event_count
    assert len({event.event_id for event in events}) == event_count
    assert expected_chunks == (event_count + 9_999) // 10_000


def test_multiple_inputs_are_retained_as_a_bounded_sender_set():
    block = _block()
    block["tx"][0]["vin"].append({
        "txid": "03" * 32,
        "vout": 1,
        "prevout": {"value": "0.30000000", "scriptPubKey": {"address": "bc1qsource2"}},
    })
    events = BitcoinBlockParser().parse_block(block, observed_at=NOW, fetched_at=NOW, processed_at=NOW)
    assert events[0].from_address is None
    assert events[0].from_address_set_reference is not None
    assert events[0].from_address_set_reference.content_hash == events[1].from_address_set_reference.content_hash


def test_coinbase_has_no_fabricated_sender():
    block = _block(txs=[{
        "txid": TX_HASH,
        "vin": [{"coinbase": "03abcd"}],
        "vout": [{"n": 0, "value": "6.25000000", "scriptPubKey": {"address": "bc1qminer"}}],
    }])
    event = BitcoinBlockParser().parse_block(block, observed_at=NOW, fetched_at=NOW, processed_at=NOW)[0]
    assert event.from_address is None
    assert event.from_address_set_reference is None
    assert dict(event.details)["coinbase"] is True


def test_reorg_or_block_hash_replacement_is_stale_and_not_available():
    block = _block(confirmations=-1)
    event = BitcoinBlockParser().parse_block(block, observed_at=NOW, fetched_at=NOW, processed_at=NOW)[0]
    assert event.status is DataStatus.STALE
    assert event.finality_status is FinalityStatus.REORGED
    assert event.reason_code is ReasonCode.REORGED_EVENT

    replacement = _block()
    replacement["hash"] = "cd" * 32
    event = BitcoinBlockParser().parse_block(
        replacement, observed_at=NOW, fetched_at=NOW, processed_at=NOW,
        expected_block_hash=BLOCK_HASH,
    )[0]
    assert event.status is DataStatus.STALE
    assert event.finality_status is FinalityStatus.REORGED


def test_unconfirmed_block_is_partial_and_not_available_evidence():
    event = BitcoinBlockParser().parse_block(
        _block(confirmations=1), observed_at=NOW, fetched_at=NOW, processed_at=NOW,
    )[0]
    assert event.status is DataStatus.PARTIAL
    assert event.reason_code is ReasonCode.PARTIAL_COVERAGE
    assert event.finality_status is FinalityStatus.OBSERVED


@pytest.mark.parametrize("field,value", [
    ("hash", "bad"),
    ("height", -1),
    ("time", "not-an-int"),
])
def test_invalid_block_payload_is_rejected(field, value):
    block = _block()
    block[field] = value
    with pytest.raises(BitcoinPayloadError):
        BitcoinBlockParser().parse_block(block, observed_at=NOW, fetched_at=NOW, processed_at=NOW)


def test_invalid_output_amount_and_duplicate_vout_are_rejected():
    block = _block()
    block["tx"][0]["vout"][0]["value"] = "0.000000001"
    with pytest.raises(BitcoinPayloadError):
        BitcoinBlockParser().parse_block(block, observed_at=NOW, fetched_at=NOW, processed_at=NOW)


@pytest.mark.parametrize("coinbase", ["", None, 123])
def test_malformed_coinbase_is_rejected(coinbase):
    block = _block(txs=[{
        "txid": TX_HASH,
        "vin": [{"coinbase": coinbase}],
        "vout": [{"n": 0, "value": "1.00000000", "scriptPubKey": {}}],
    }])
    with pytest.raises(BitcoinPayloadError):
        BitcoinBlockParser().parse_block(block, observed_at=NOW, fetched_at=NOW, processed_at=NOW)


def test_empty_output_list_is_rejected():
    block = _block(txs=[{
        "txid": TX_HASH,
        "vin": [{"coinbase": "03abcd"}],
        "vout": [],
    }])
    with pytest.raises(BitcoinPayloadError):
        BitcoinBlockParser().parse_block(block, observed_at=NOW, fetched_at=NOW, processed_at=NOW)


def test_empty_transaction_list_is_rejected():
    block = _block(txs=[])
    with pytest.raises(BitcoinPayloadError):
        BitcoinBlockParser().parse_block(block, observed_at=NOW, fetched_at=NOW, processed_at=NOW)


def test_float_amount_is_rejected_to_preserve_exact_source_precision():
    block = _block()
    block["tx"][0]["vout"][0]["value"] = 0.25
    with pytest.raises(BitcoinPayloadError):
        BitcoinBlockParser().parse_block(block, observed_at=NOW, fetched_at=NOW, processed_at=NOW)


def test_provenance_must_be_utc_and_monotonic():
    parser = BitcoinBlockParser()
    with pytest.raises(BitcoinPayloadError):
        parser.parse_block(
            _block(),
            observed_at=NOW + timedelta(seconds=1),
            fetched_at=NOW,
            processed_at=NOW,
        )
    with pytest.raises(BitcoinPayloadError):
        parser.parse_block(
            _block(),
            observed_at=datetime(2026, 9, 23, tzinfo=timezone(timedelta(hours=8))),
            fetched_at=NOW,
            processed_at=NOW,
        )

    block = _block()
    block["tx"][0]["vout"][1]["n"] = 0
    with pytest.raises(BitcoinPayloadError):
        BitcoinBlockParser().parse_block(block, observed_at=NOW, fetched_at=NOW, processed_at=NOW)
