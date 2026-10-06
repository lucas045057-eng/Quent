#!/usr/bin/env python3
"""Profile the production Bitcoin cycle with a bounded deterministic fixture.

This is a diagnostics-only tool. It makes no network requests and prints only
stage names, counts, process/cgroup memory, and the fixture byte count.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import json
import tracemalloc
from typing import Any

from quant_phase1.config import Settings
from quant_phase7.bitcoin import BitcoinBlockParser
from quant_phase7.contracts import DataStatus
from quant_phase7.persistence import Phase7Repository
from quant_phase7.runtime import Phase7CollectorRuntime


BASE_HEIGHT = 968_443
PREVIOUS_HASH = f"{BASE_HEIGHT - 1:064x}"


def _build_block(event_count: int) -> dict[str, Any]:
    if not 1 <= event_count <= 20_000:
        raise ValueError("event count must be between 1 and 20,000")
    transactions: list[dict[str, Any]] = [{
        "txid": f"{1:064x}",
        "hash": f"{1:064x}",
        "version": 2,
        "size": 250,
        "vsize": 141,
        "weight": 560,
        "locktime": 0,
        "vin": [{"coinbase": "deterministic-phase7-profile"}],
        "vout": [{
            "n": 0, "value": 6.25,
            "scriptPubKey": {
                "address": "bc1qdeterministicminer",
                "type": "witness_v0_keyhash",
                "asm": "OP_DUP OP_HASH160 " + "11" * 20 + " OP_EQUALVERIFY OP_CHECKSIG",
                "desc": "addr(bc1qdeterministicminer)#" + "a" * 40,
                "hex": "76a914" + "11" * 20 + "88ac",
            },
        }],
    }]
    remaining = event_count - 1
    transaction_index = 1
    while remaining:
        outputs = min(2, remaining)
        txid = f"{transaction_index + 1:064x}"
        previous_txid = f"{transaction_index + 100_000:064x}"
        input_address = f"bc1qsrc{transaction_index:08x}"
        transactions.append({
            "txid": txid,
            "hash": txid,
            "version": 2,
            "size": 250,
            "vsize": 141,
            "weight": 560,
            "locktime": 0,
            "vin": [{
                "txid": previous_txid,
                "vout": 0,
                "scriptSig": {"asm": "0014" + "aa" * 20, "hex": "00" * 100},
                "txinwitness": ["bb" * 32, "cc" * 32],
                "sequence": 4_294_967_295,
                "prevout": {
                    "value": 0.125,
                    "height": BASE_HEIGHT - 100,
                    "generated": False,
                    "scriptPubKey": {
                        "address": input_address,
                        "type": "witness_v0_keyhash",
                        "asm": "OP_DUP OP_HASH160 " + "22" * 20 + " OP_EQUALVERIFY OP_CHECKSIG",
                        "desc": f"addr({input_address})#" + "b" * 40,
                        "hex": "76a914" + "22" * 20 + "88ac",
                    },
                },
            }],
            "vout": [{
                "n": index,
                "value": 0.00000001,
                "scriptPubKey": {
                    "address": f"bc1qdest{transaction_index:08x}{index}",
                    "type": "witness_v0_keyhash",
                    "asm": "OP_DUP OP_HASH160 " + "33" * 20 + " OP_EQUALVERIFY OP_CHECKSIG",
                    "desc": f"addr(bc1qdest{transaction_index:08x}{index})#" + "c" * 40,
                    "hex": "76a914" + (f"{transaction_index:040x}"[-40:]) + "88ac",
                },
            } for index in range(outputs)],
        })
        remaining -= outputs
        transaction_index += 1
    return {
        "hash": f"{BASE_HEIGHT:064x}",
        "height": BASE_HEIGHT,
        "time": int(datetime(2026, 9, 20, tzinfo=timezone.utc).timestamp()),
        "confirmations": 20,
        "tx": transactions,
    }


def _read_key_values(path: str) -> dict[str, int]:
    try:
        result: dict[str, int] = {}
        with open(path, encoding="ascii") as stream:
            for line in stream:
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    result[parts[0].rstrip(":")] = int(parts[1])
        return result
    except (OSError, ValueError):
        return {}


def _memory_sample() -> dict[str, int | None]:
    cgroup = "/sys/fs/cgroup"
    status = _read_key_values("/proc/self/status")
    stat = _read_key_values(f"{cgroup}/memory.stat")
    events = _read_key_values(f"{cgroup}/memory.events")
    smaps = _read_key_values("/proc/self/smaps_rollup")
    try:
        current = int(open(f"{cgroup}/memory.current", encoding="ascii").read().strip())
    except (OSError, ValueError):
        current = None
    try:
        peak = int(open(f"{cgroup}/memory.peak", encoding="ascii").read().strip())
    except (OSError, ValueError):
        peak = None
    traced_current, traced_peak = tracemalloc.get_traced_memory()
    result: dict[str, int | None] = {
        "memory_current_bytes": current,
        "memory_peak_bytes": peak,
        "anon_bytes": stat.get("anon"),
        "file_bytes": stat.get("file"),
        "kernel_bytes": stat.get("kernel"),
        "sock_bytes": stat.get("sock"),
        "shmem_bytes": stat.get("shmem"),
        "slab_bytes": stat.get("slab"),
        "pagetables_bytes": stat.get("pagetables"),
        "inactive_file_bytes": stat.get("inactive_file"),
        "active_file_bytes": stat.get("active_file"),
        "memory_events_max": events.get("max"),
        "memory_events_oom": events.get("oom"),
        "memory_events_oom_kill": events.get("oom_kill"),
        "rss_bytes": status.get("VmRSS", 0) * 1024 if "VmRSS" in status else None,
        "vmsize_bytes": status.get("VmSize", 0) * 1024 if "VmSize" in status else None,
        "threads": status.get("Threads"),
        "pss_bytes": smaps.get("Pss", 0) * 1024 if "Pss" in smaps else None,
        "tracemalloc_current_bytes": traced_current,
        "tracemalloc_peak_bytes": traced_peak,
    }
    return result


class _Profiler:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.cycle = 0
        self.block = 0
        self.transaction_total = 0
        self.transactions_parsed = 0
        self.events_total = 0

    def sample(self, stage: str, **counts: int) -> None:
        self.records.append({
            "stage": stage,
            "cycle": self.cycle,
            "block_in_cycle": self.block,
            **counts,
            **_memory_sample(),
        })

    def summary(self) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        for record in self.records:
            grouped[(record["stage"], record["block_in_cycle"])].append(record)
        memory_keys = (
            "memory_current_bytes", "memory_peak_bytes", "anon_bytes", "file_bytes",
            "kernel_bytes", "sock_bytes", "shmem_bytes", "slab_bytes", "pagetables_bytes",
            "inactive_file_bytes", "active_file_bytes", "memory_events_max", "memory_events_oom",
            "memory_events_oom_kill", "rss_bytes", "pss_bytes", "tracemalloc_current_bytes",
            "tracemalloc_peak_bytes",
        )
        result = []
        for (stage, block), records in sorted(grouped.items()):
            entry: dict[str, Any] = {
                "stage": stage,
                "block_in_cycle": block,
                "samples": len(records),
            }
            for key in memory_keys:
                values = [record[key] for record in records if record.get(key) is not None]
                if values:
                    entry[key + "_min"] = min(values)
                    entry[key + "_max"] = max(values)
                    entry[key + "_last"] = values[-1]
            for key in ("fixture_bytes", "transaction_count", "response_bytes", "events",
                        "chunk_index", "chunk_rows", "chunks", "processed_blocks", "persisted_events",
                        "transaction_count", "events_in_final_transaction"):
                values = [record[key] for record in records if key in record]
                if values:
                    entry[key] = values[-1]
            result.append(entry)
        return result


class _ReplayClient:
    def __init__(self, _config: Any, _session: Any, *, payload: bytes, profiler: _Profiler) -> None:
        self.payload = payload
        self.profiler = profiler
        self.height = BASE_HEIGHT - 1
        self.block_in_cycle = 0

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        if method == "getblockchaininfo":
            return {"chain": "main", "blocks": BASE_HEIGHT + 7}
        if method == "getblockhash":
            self.height = int(params[0])
            if self.height == BASE_HEIGHT - 1:
                return PREVIOUS_HASH
            self.block_in_cycle = self.height - BASE_HEIGHT + 1
            return f"{self.height:064x}"
        if method == "getblock":
            raw_body = bytes(bytearray(self.payload))
            self.profiler.block = self.block_in_cycle
            self.profiler.sample("stage_1_response_bytes_received", response_bytes=len(raw_body))
            envelope = json.loads(raw_body, parse_float=Decimal)
            block = envelope["result"]
            block["height"] = self.height
            block["hash"] = f"{self.height:064x}"
            self.profiler.sample(
                "stage_2_json_decoded",
                response_bytes=len(raw_body),
                transaction_count=len(block["tx"]),
            )
            self.profiler.sample(
                "stage_3_block_object_ready",
                response_bytes=len(raw_body),
                transaction_count=len(block["tx"]),
            )
            return block
        raise AssertionError("unexpected Bitcoin RPC method")


class _FakeCursor:
    def __init__(self, connection: "_FakeConnection") -> None:
        self.connection = connection
        self.rowcount = 0

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_args: Any) -> bool:
        return False

    def execute(self, *_args: Any) -> None:
        self.rowcount = 1

    def executemany(self, _sql: str, rows: list[tuple[Any, ...]]) -> None:
        self.rowcount = len(rows)
        if rows and rows[0][1] == "BITCOIN":
            self.connection.chunk_index += 1
            self.connection.profiler.sample(
                "stage_7_persistence_chunk_prepared",
                chunk_index=self.connection.chunk_index,
                chunk_rows=len(rows),
            )


class _FakeConnection:
    def __init__(self, profiler: _Profiler) -> None:
        self.profiler = profiler
        self.chunk_index = 0

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)

    @contextmanager
    def transaction(self):
        try:
            yield self
        except BaseException:
            raise
        else:
            if self.chunk_index:
                self.profiler.sample("stage_8_transaction_commit_complete", chunks=self.chunk_index)
                self.chunk_index = 0


class _FakeRepository:
    def __init__(self, profiler: _Profiler, event_time: datetime) -> None:
        self.profiler = profiler
        self.repository = Phase7Repository(_FakeConnection(profiler))
        self.event_time = event_time

    def upsert_asset_registry(self, _rows: Any) -> None:
        pass

    def load_checkpoint(self, *_args: Any) -> dict[str, str]:
        return {"cursor_value": str(BASE_HEIGHT - 1), "last_block_hash": PREVIOUS_HASH}

    def load_event_time_price(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "price": Decimal("65000"),
            "exchange": "binance",
            "source": "deterministic-profile",
            "exchange_timestamp": self.event_time,
            "fetched_at": self.event_time,
        }

    def persist_transfer_chunks_and_checkpoint(self, events: Any, checkpoint: Any) -> tuple[int, int]:
        return self.repository.persist_transfer_chunks_and_checkpoint(events, checkpoint)


class _FakeSession:
    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *_args: Any) -> bool:
        return False


class _MeasuredIterator:
    def __init__(self, iterator: Any, total: int, profiler: _Profiler) -> None:
        self.iterator = iter(iterator)
        self.total = total
        self.profiler = profiler
        self.count = 0

    def __iter__(self) -> "_MeasuredIterator":
        return self

    def __next__(self) -> Any:
        event = next(self.iterator)
        self.count += 1
        if self.count == self.total:
            self.profiler.sample("stage_6_event_time_valuation_complete", events=self.count)
        return event


async def _run_profile(cycles: int, event_count: int) -> dict[str, Any]:
    block = _build_block(event_count)
    event_time = datetime.fromtimestamp(block["time"], tz=timezone.utc)
    payload = json.dumps({"jsonrpc": "1.0", "id": 1, "result": block, "error": None},
                         separators=(",", ":")).encode("utf-8")
    transaction_count = len(block["tx"])
    del block

    profiler = _Profiler()
    tracemalloc.start(1)
    profiler.sample("stage_0_collector_baseline", fixture_bytes=len(payload), transaction_count=transaction_count)

    settings = Settings.from_env({
        "TRADING_MODE": "paper",
        "PHASE7_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_URL": "https://rpc.example.invalid/opaque",
        "PHASE7_BITCOIN_RPC_REQUESTS_PER_SECOND": "100",
    })
    runtime = Phase7CollectorRuntime(settings, spot_websocket_enabled=False)
    repository = _FakeRepository(profiler, event_time)

    @contextmanager
    def repository_scope():
        yield repository

    original_client = __import__("quant_phase7.runtime", fromlist=["Phase7JsonRpcClient"]).Phase7JsonRpcClient
    original_parse_block = BitcoinBlockParser.parse_block
    original_parse_transaction = BitcoinBlockParser._parse_transaction
    original_valuation_iterator = Phase7CollectorRuntime._iter_event_time_prices

    class ReplayClient(_ReplayClient):
        def __init__(self, config: Any, session: Any) -> None:
            super().__init__(config, session, payload=payload, profiler=profiler)

    def parse_transaction(parser: BitcoinBlockParser, transaction: Any, **kwargs: Any) -> Any:
        result = original_parse_transaction(parser, transaction, **kwargs)
        profiler.transactions_parsed += 1
        if profiler.transactions_parsed == profiler.transaction_total:
            profiler.sample(
                "stage_4_transactions_parsed",
                transaction_count=profiler.transactions_parsed,
                events_in_final_transaction=len(result),
            )
        return result

    def parse_block(parser: BitcoinBlockParser, source_block: Any, **kwargs: Any) -> Any:
        profiler.block = int(source_block["height"] - BASE_HEIGHT + 1)
        profiler.transactions_parsed = 0
        profiler.transaction_total = len(source_block["tx"])
        result = original_parse_block(parser, source_block, **kwargs)
        profiler.events_total = len(result)
        profiler.sample("stage_5_canonical_events_generated", events=len(result))
        return result

    def valuation_iterator(repository_arg: Any, events: Any, symbol: str) -> Any:
        return _MeasuredIterator(original_valuation_iterator(repository_arg, events, symbol),
                                 len(events), profiler)

    import quant_phase7.runtime as runtime_module

    runtime_module.Phase7JsonRpcClient = ReplayClient
    BitcoinBlockParser.parse_block = parse_block
    BitcoinBlockParser._parse_transaction = parse_transaction
    Phase7CollectorRuntime._iter_event_time_prices = staticmethod(valuation_iterator)
    runtime._rpc_session = lambda: _FakeSession()
    runtime._repository_scope = repository_scope
    try:
        cycles_result = []
        for cycle in range(1, cycles + 1):
            profiler.cycle = cycle
            profiler.block = 0
            profiler.sample("cycle_start", cycle=cycle)
            status, summary = await runtime._bitcoin_cycle()
            if status is not DataStatus.AVAILABLE:
                raise RuntimeError("deterministic Bitcoin cycle did not complete")
            if summary["processed_blocks"] != 2 or summary["persisted_events"] != event_count * 2:
                raise RuntimeError("deterministic Bitcoin cycle violated event/count contract")
            profiler.sample("stage_9_references_released_cycle_return", cycle=cycle,
                            processed_blocks=summary["processed_blocks"],
                            persisted_events=summary["persisted_events"])
            cycles_result.append({"cycle": cycle, "processed_blocks": summary["processed_blocks"],
                                  "persisted_events": summary["persisted_events"]})
        return {
            "profile": "deterministic_production_bitcoin_cycle",
            "block_height_anchor": BASE_HEIGHT,
            "event_count_per_block": event_count,
            "transaction_count_per_block": transaction_count,
            "fixture_response_bytes": len(payload),
            "repeated_cycles": cycles_result,
            "stage_summary": profiler.summary(),
            "profiler_enabled": "tracemalloc (diagnostic only)",
        }
    finally:
        runtime_module.Phase7JsonRpcClient = original_client
        BitcoinBlockParser.parse_block = original_parse_block
        BitcoinBlockParser._parse_transaction = original_parse_transaction
        Phase7CollectorRuntime._iter_event_time_prices = staticmethod(original_valuation_iterator)
        tracemalloc.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--events", type=int, default=12_665)
    args = parser.parse_args()
    if not 5 <= args.cycles <= 10:
        raise SystemExit("cycles must be between 5 and 10")
    if not 1 <= args.events <= 20_000:
        raise SystemExit("events must be between 1 and 20,000")
    result = asyncio.run(_run_profile(args.cycles, args.events))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
