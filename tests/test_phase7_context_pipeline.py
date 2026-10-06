from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_phase7.bitcoin import BitcoinBlockParser
from quant_phase7.contracts import Chain, DataStatus
from quant_phase7.context_pipeline import (
    aggregate_generic_flow_window,
    aggregate_stablecoin_context_rows,
    aggregate_whale_context_window,
    event_window_open,
)
from quant_phase7.context_service import aggregate_closed_context_window, closed_window_open
from quant_phase7.ethereum import EthereumBlockParser
from quant_phase7.labels import AddressLabel, LabelCategory, LabelSnapshot
from quant_phase7.whale import WhaleThresholdConfig, WhaleTier


NOW = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)


def _btc_event():
    return BitcoinBlockParser().parse_block({
        "hash": "ab" * 32,
        "height": 100,
        "time": int(NOW.timestamp()),
        "confirmations": 6,
        "tx": [{
            "txid": "01" * 32,
            "vin": [{"txid": "02" * 32, "vout": 0, "prevout": {"scriptPubKey": {"address": "bc1qsource"}}}],
            "vout": [{"n": 0, "value": "0.25000000", "scriptPubKey": {"address": "bc1qdestination"}}],
        }],
    }, observed_at=NOW, fetched_at=NOW, processed_at=NOW)[0]


def _btc_snapshot(event):
    effective = event.event_time - timedelta(days=1)
    return LabelSnapshot(
        chain=Chain.BITCOIN,
        source_id="operator-reviewed",
        source_version="registry-v1",
        label_version="labels-2026-09",
        source_urls=("https://labels.example/reviewed.json",),
        reviewer="operator",
        snapshot_hash="a" * 64,
        effective_from=effective,
        effective_to=None,
        coverage_denominator=2,
        reviewed=True,
        labels=tuple(AddressLabel(
            chain=Chain.BITCOIN,
            address=address,
            category=category,
            source_id="operator-reviewed",
            source_version="registry-v1",
            label_version="labels-2026-09",
            confidence=Decimal("1"),
            snapshot_hash="a" * 64,
            source_reference="https://labels.example/reviewed.json",
            effective_from=effective,
            effective_to=None,
            observed_at=NOW,
            updated_at=NOW,
            status=DataStatus.AVAILABLE,
            reason=None,
        ) for address, category in (
            ("bc1qsource", LabelCategory.KNOWN_EXTERNAL),
            ("bc1qdestination", LabelCategory.KNOWN_EXCHANGE),
        )),
    )


def _eth_snapshot(event, from_category, to_category):
    effective = event.event_time - timedelta(days=1)
    return LabelSnapshot(
        chain=Chain.ETHEREUM,
        source_id="operator-reviewed",
        source_version="registry-v1",
        label_version="labels-2026-09",
        source_urls=("https://labels.example/reviewed.json",),
        reviewer="operator",
        snapshot_hash="b" * 64,
        effective_from=effective,
        effective_to=None,
        coverage_denominator=2,
        reviewed=True,
        labels=tuple(AddressLabel(
            chain=Chain.ETHEREUM,
            address=address,
            category=category,
            source_id="operator-reviewed",
            source_version="registry-v1",
            label_version="labels-2026-09",
            confidence=Decimal("1"),
            snapshot_hash="b" * 64,
            source_reference="https://labels.example/reviewed.json",
            effective_from=effective,
            effective_to=None,
            observed_at=NOW,
            updated_at=NOW,
            status=DataStatus.AVAILABLE,
            reason=None,
        ) for address, category in (
            (event.from_address, from_category), (event.to_address, to_category),
        )),
    )


def _eth_token_event():
    block_hash = "0x" + "ab" * 32
    tx_hash = "0x" + "01" * 32
    usdt = "0xdac17f958d2ee523a2206206994597c13d831ec7"
    source = "0x1111111111111111111111111111111111111111"
    destination = "0x2222222222222222222222222222222222222222"
    topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    log = {
        "address": usdt,
        "topics": [topic, "0x" + "0" * 24 + source[2:], "0x" + "0" * 24 + destination[2:]],
        "data": "0x" + f"{1_000_000:064x}",
        "logIndex": "0x2",
        "transactionHash": tx_hash,
        "transactionIndex": "0x0",
        "blockNumber": "0x64",
        "blockHash": block_hash,
    }
    block = {
        "number": "0x64", "hash": block_hash, "timestamp": hex(int(NOW.timestamp())),
        "confirmations": "0x20", "finalityTag": "finalized",
        "transactions": [{
            "hash": tx_hash, "transactionIndex": "0x0", "from": source,
            "to": destination, "value": "0x0",
        }],
    }
    receipt = {
        "transactionHash": tx_hash, "blockHash": block_hash, "blockNumber": "0x64",
        "transactionIndex": "0x0", "status": "0x1", "logs": [log],
    }
    return EthereumBlockParser().parse_block(
        block, chain_id="0x1", logs=[log], receipts=[receipt], finality_tag="finalized",
        observed_at=NOW, fetched_at=NOW, processed_at=NOW,
    )[1]


def test_generic_flow_without_reviewed_labels_is_persistable_not_available():
    event = _btc_event()
    window_open = event_window_open(event.event_time, "5m")

    row = aggregate_generic_flow_window(
        [event], timeframe="5m", window_open=window_open, snapshot=None, processed_at=NOW,
    )

    assert row["status"] == "NOT_AVAILABLE"
    assert row["reason"] == "LABEL_SNAPSHOT_NOT_CONFIGURED"
    assert row["inbound_amount"] == Decimal("0")
    assert row["outbound_amount"] == Decimal("0")


def test_generic_flow_uses_reviewed_labels_for_exchange_inflow():
    event = _btc_event()
    row = aggregate_generic_flow_window(
        [event], timeframe="5m", window_open=event_window_open(event.event_time, "5m"),
        snapshot=_btc_snapshot(event), processed_at=NOW,
    )

    assert row["status"] == "AVAILABLE"
    assert row["inbound_amount"] == Decimal("0.25")
    assert row["outbound_amount"] == Decimal("0")
    assert row["exchange_inflow_amount"] == Decimal("0.25")


def test_whale_without_versioned_threshold_is_not_available():
    event = _btc_event()
    row = aggregate_whale_context_window(
        [event], timeframe="5m", window_open=event_window_open(event.event_time, "5m"),
        snapshot=_btc_snapshot(event), threshold=None, processed_at=NOW,
    )

    assert row is not None
    assert row["status"] == "NOT_AVAILABLE"
    assert row["reason"] == "THRESHOLD_NOT_CONFIGURED"
    assert row["large_inflow_count"] == row["large_outflow_count"] == 0


def test_whale_with_threshold_does_not_treat_missing_event_time_price_as_zero():
    event = _btc_event()
    threshold = WhaleThresholdConfig(
        chain=event.identity.chain,
        asset_id=event.identity.asset_id,
        threshold_version="btc-large-v1",
        tiers=(WhaleTier("LARGE", Decimal("10000"), None),),
    )

    row = aggregate_whale_context_window(
        [event], timeframe="5m", window_open=event_window_open(event.event_time, "5m"),
        snapshot=_btc_snapshot(event), threshold=threshold, processed_at=NOW,
    )

    assert row is not None
    assert row["status"] == "NOT_AVAILABLE"
    assert row["large_inflow_count"] == row["large_outflow_count"] == 0
    assert row["large_inflow_usd"] is None and row["large_outflow_usd"] is None


def test_stablecoin_runtime_projection_keeps_allowlisted_transfer_category():
    event = _eth_token_event()
    rows = aggregate_stablecoin_context_rows(
        [event], timeframe="5m", window_open=event_window_open(event.event_time, "5m"),
        snapshot=None, processed_at=NOW,
    )

    assert len(rows) == 1
    assert rows[0]["category"] == "ORDINARY_TRANSFER"
    assert rows[0]["amount_normalized"] == Decimal("1")
    assert rows[0]["aggregation_eligible"] is True


def test_reviewed_bridge_label_keeps_stablecoin_leg_out_of_net_flow():
    event = _eth_token_event()
    snapshot = _eth_snapshot(event, LabelCategory.KNOWN_BRIDGE, LabelCategory.KNOWN_EXTERNAL)
    rows = aggregate_stablecoin_context_rows(
        [event], timeframe="5m", window_open=event_window_open(event.event_time, "5m"),
        snapshot=snapshot, processed_at=NOW,
    )

    assert len(rows) == 1
    assert rows[0]["category"] == "BRIDGE_TRANSFER"
    assert rows[0]["aggregation_eligible"] is False
    assert rows[0]["amount_normalized"] == Decimal("0")


def test_engine_context_service_persists_canonical_flow_and_whale_rows_idempotently():
    event = _btc_event()

    class Repository:
        def __init__(self):
            self.windows = {}

        def load_context_asset_ids(self, _opened, _closed):
            return ({"chain": "BITCOIN", "asset_id": "BITCOIN:NATIVE:NATIVE:btc-v1"},)

        def load_context_events(self, _chain, _asset_id, _opened, _closed):
            return (event,)

        def upsert_windows(self, table, rows):
            for row in rows:
                key = (table, row["chain"], row["asset_id"], row["timeframe"], row["window_open"])
                self.windows[key] = row
            return len(rows)

    repository = Repository()
    window_open = closed_window_open(NOW + timedelta(minutes=1), "1m")
    first = aggregate_closed_context_window(
        repository, timeframe="1m", window_open=window_open, processed_at=NOW,
        label_snapshots={}, whale_thresholds={},
    )
    second = aggregate_closed_context_window(
        repository, timeframe="1m", window_open=window_open, processed_at=NOW,
        label_snapshots={}, whale_thresholds={},
    )

    assert first == second == {"assets": 1, "events": 1, "flow_rows": 1, "whale_rows": 1, "stablecoin_rows": 0}
    assert len(repository.windows) == 2
    assert {row["status"] for row in repository.windows.values()} == {"NOT_AVAILABLE"}


def test_closed_window_uses_latest_fully_closed_utc_interval():
    now = datetime(2026, 9, 23, 10, 30, tzinfo=timezone.utc)

    assert closed_window_open(now, "1H") == datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)
