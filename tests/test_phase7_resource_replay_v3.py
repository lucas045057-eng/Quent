from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from phase7_resource_replay_v3_runner import (
    BTC_CATCHUP_BLOCKS,
    BTC_FINAL_CURSOR,
    ETHEREUM_START_HEIGHT,
    ETH_FINAL_CURSOR,
    GENERATOR_SPEC,
    LARGE_BLOCK_EVENTS,
    SPOT_TRADES_PER_SYMBOL,
    SPOT_FINAL_CURSOR,
    ResourceReplayJsonRpcClient,
    _phase7_failure_marker_payload,
    _bitcoin_block,
    _ethereum_hash,
    _install_phase7_replay,
    _spot_payloads,
    dataset_identity,
)
from quant_data_layer.admission import WorkAdmissionController
from quant_data_layer.observability import ProcessRole
from quant_phase7.bitcoin import BitcoinBlockParser
from quant_phase7.ethereum import EthereumBlockParser
from quant_phase7.runtime import Phase7CollectorRuntime
from quant_phase7.spot import BinanceSpotAdapter
from run_phase7_resource_replay_v3_docker import _summarize_window

FIXED_NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)

def test_dataset_identity_is_stable_and_covers_frozen_phase1_phase2_and_v3_generator():
    first = dataset_identity()
    second = dataset_identity()
    assert first == second
    assert len(first["dataset_sha256"]) == 64
    assert first["phase1_input_rows"] == 84_660
    assert first["phase2_input_rows"] == 603
    assert first["generator"] == GENERATOR_SPEC
    assert first["generator"]["bitcoin"]["catchup_blocks"] == BTC_CATCHUP_BLOCKS
    assert first["generator"]["expected_final_cursors"] == {
        "bitcoin": BTC_FINAL_CURSOR,
        "ethereum": ETH_FINAL_CURSOR,
        "binance_spot_per_symbol": SPOT_FINAL_CURSOR,
    }

def test_bitcoin_large_block_is_deterministic_and_has_exact_event_count():
    block = _bitcoin_block(968_443)
    parser = BitcoinBlockParser()
    arguments = {"observed_at": FIXED_NOW, "fetched_at": FIXED_NOW, "processed_at": FIXED_NOW,
                 "expected_block_hash": block["hash"]}
    first = parser.parse_block(block, **arguments)
    second = parser.parse_block(_bitcoin_block(968_443), **arguments)
    first_identity = tuple((event.event_id, event.amount.amount_raw) for event in first)
    second_identity = tuple((event.event_id, event.amount.amount_raw) for event in second)
    assert len(first) == LARGE_BLOCK_EVENTS
    assert len({event.event_id for event in first}) == LARGE_BLOCK_EVENTS
    assert first_identity == second_identity

def test_ethereum_mainnet_block_and_receipt_parse_deterministically(monkeypatch):
    import quant_phase7.runtime as runtime_module
    monkeypatch.setattr(runtime_module, "utc_now", lambda: FIXED_NOW)
    client = ResourceReplayJsonRpcClient(SimpleNamespace(source_id="ethereum_rpc"), None)
    runtime = object.__new__(Phase7CollectorRuntime)
    runtime._diagnostics = {
        "ethereum_active_block_height": 0,
        "ethereum_pending_logs": 0,
        "ethereum_pending_receipts": 0,
    }
    runtime._source_stages = {}
    runtime.diagnostics_sink = None
    runtime.admission = WorkAdmissionController(role=ProcessRole.COLLECTOR)
    parser = EthereumBlockParser()
    async def parse_twice():
        block, first = await runtime._ethereum_block(client, parser, ETHEREUM_START_HEIGHT, "0x1", FIXED_NOW)
        _, second = await runtime._ethereum_block(client, parser, ETHEREUM_START_HEIGHT, "0x1", FIXED_NOW)
        return block, first, second

    block, first, second = asyncio.run(parse_twice())
    assert block["hash"] == _ethereum_hash(ETHEREUM_START_HEIGHT)
    assert len(first) == 1
    assert first[0].amount.amount_raw == str(10**18)
    assert tuple(event.event_id for event in first) == tuple(event.event_id for event in second)
    assert client.response_bytes_total > 0
    assert client.last_response_bytes > 0


def test_resource_rpc_replay_exposes_runtime_diagnostics_contract():
    client = ResourceReplayJsonRpcClient(SimpleNamespace(source_id="ethereum_rpc"), None)

    assert client.active_requests == 0
    assert client.request_count == 0
    assert client.last_method is None
    assert asyncio.run(client.call("eth_chainId")) == "0x1"
    assert client.active_requests == 0
    assert client.request_count == 1
    assert client.last_method == "eth_chainId"
    assert client.last_response_bytes > 0
    assert client.response_bytes_total == client.last_response_bytes


def test_bitcoin_rpc_replay_measures_decimal_block_without_coercing_values():
    client = ResourceReplayJsonRpcClient(SimpleNamespace(source_id="bitcoin_rpc"), None)

    async def fetch_block():
        await client.call("getblockchaininfo")
        block_hash = await client.call("getblockhash", [968_443])
        return await client.call("getblock", [block_hash, 2])

    block = asyncio.run(fetch_block())

    assert block["height"] == 968_443
    assert isinstance(block["tx"][0]["vout"][0]["value"], Decimal)
    assert block["tx"][1]["vin"][0]["prevout"]["value"] == Decimal("0.125")
    assert client.last_response_bytes > 0
    assert client.response_bytes_total >= client.last_response_bytes


def test_phase7_failure_marker_contains_only_bounded_safe_diagnostics():
    marker = _phase7_failure_marker_payload(
        "bitcoin_rpc",
        "ERROR",
        {
            "reason": "TypeError",
            "failure_stage": "BLOCK_PERSISTENCE",
            "endpoint": "https://user:secret@private.example/path?token=secret",
            "raw_payload": {"secret": "must-not-escape"},
        },
        runtime_stage="BLOCK_PERSISTENCE",
    )

    assert marker == {
        "component": "bitcoin_rpc",
        "status": "ERROR",
        "exception_type": "TypeError",
        "failure_stage": "BLOCK_PERSISTENCE",
        "runtime_stage": "BLOCK_PERSISTENCE",
    }
    assert "secret" not in json.dumps(marker)


def test_binance_spot_replay_has_two_thousand_unique_parseable_trades():
    adapter = BinanceSpotAdapter()
    payloads = _spot_payloads(FIXED_NOW)
    events = [adapter.parse_ws_message(payload, fetched_at=FIXED_NOW, processed_at=FIXED_NOW)
              for payload in payloads]
    assert len(payloads) == 2 * SPOT_TRADES_PER_SYMBOL
    assert len(events) == len(payloads)
    assert {event.symbol for event in events} == {"BTCUSDT", "ETHUSDT"}
    assert len({event.identity for event in events}) == len(events)
    assert {max(int(event.trade_id) for event in events if event.symbol == symbol)
            for symbol in ("BTCUSDT", "ETHUSDT")} == {SPOT_TRADES_PER_SYMBOL}


def test_collector_phase7_runtime_factory_hook_targets_the_lazy_import():
    import quant_phase7.runtime as runtime_module
    import phase7_resource_replay_v3_runner as replay_module

    original_runtime = runtime_module.Phase7CollectorRuntime
    original_client = runtime_module.Phase7JsonRpcClient
    original_clock = runtime_module.utc_now
    original_fixed_now = replay_module._FIXED_NOW
    original_spot_messages = replay_module._SPOT_MESSAGES
    try:
        _install_phase7_replay(None, FIXED_NOW)
        assert issubclass(runtime_module.Phase7CollectorRuntime, original_runtime)
        assert runtime_module.Phase7CollectorRuntime.__bases__ == (original_runtime,)
    finally:
        runtime_module.Phase7CollectorRuntime = original_runtime
        runtime_module.Phase7JsonRpcClient = original_client
        runtime_module.utc_now = original_clock
        replay_module._FIXED_NOW = original_fixed_now
        replay_module._SPOT_MESSAGES = original_spot_messages


def test_capacity_summary_separates_reclaimable_inactive_file_cache(tmp_path):
    samples = []
    for index in range(3):
        timestamp = FIXED_NOW + timedelta(seconds=10 * index)
        collector = {
            "memory_current": 240 * 1024 * 1024,
            "memory_peak": 245 * 1024 * 1024,
            "memory_limit": 256 * 1024 * 1024,
            "anon": 175 * 1024 * 1024,
            "file": 55 * 1024 * 1024,
            "inactive_file": 35 * 1024 * 1024,
            "kernel": 4 * 1024 * 1024,
            "sock": 0,
            "shmem": 0,
            "slab": 2 * 1024 * 1024,
            "pagetables": 1 * 1024 * 1024,
            "process_rss": 185 * 1024 * 1024,
            "process_pss": 180 * 1024 * 1024,
            "process_smaps_rss": 185 * 1024 * 1024,
            "process_vmsize": 300 * 1024 * 1024,
            "process_threads": 8,
            "tasks": 8,
            "events_max": 0,
            "events_oom": 0,
            "events_oom_kill": 0,
            "cpu_usage_usec": index * 10_000,
        }
        other = {"cpu_usage_usec": index * 3_000}
        samples.append({
            "sampled_at_utc": timestamp.isoformat(),
            "resources": {"quant-collector": collector, "quant-engine": other, "postgres": other},
            "database": {"checkpoints": {}, "health": {}, "application_metrics": {}},
        })
    path = tmp_path / "resource-samples.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in samples), encoding="utf-8")

    summary = _summarize_window(path)

    assert summary["last5_minutes"]["memory_current"]["p95"] == 240 * 1024 * 1024
    assert summary["last5_minutes"]["effective_working_set_after_inactive_file"]["p95"] == 205 * 1024 * 1024
