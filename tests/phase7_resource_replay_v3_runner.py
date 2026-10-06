"""Deterministic, production-lifecycle Phase 1-7 inputs for capacity review.

Only external market/RPC input boundaries are replayed. Collector/Engine
lifecycles, processors, repositories, persistence transactions, migrations,
queues, and health writers remain the installed production implementations.
The generator is diagnostic evidence, not a provider or consensus fixture.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from decimal import Decimal
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from typing import Any

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
SCRIPTS = Path(os.environ.get("RESOURCE_REPLAY_V3_SCRIPTS", ROOT / "scripts"))
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(SCRIPTS))

import phase6_replay_v2_runner as replay_v2  # noqa: E402
from phase7_collector_memory_profile import BASE_HEIGHT, _build_block  # noqa: E402
from quant_phase1.config import Phase7RpcSourceSettings  # noqa: E402


LARGE_BLOCK_EVENTS = 12_665
BTC_CATCHUP_BLOCKS = 144
ETHEREUM_START_HEIGHT = 20_000_000
ETHEREUM_BLOCKS = 120
SPOT_TRADES_PER_SYMBOL = 1_000
BTC_FINAL_CURSOR = BASE_HEIGHT + BTC_CATCHUP_BLOCKS - 1
ETH_FINAL_CURSOR = ETHEREUM_START_HEIGHT + ETHEREUM_BLOCKS - 1
SPOT_FINAL_CURSOR = SPOT_TRADES_PER_SYMBOL
GENERATOR_SPEC = {
    "version": "resource-replay-v3-diagnostic-1",
    "phase1_fixture": "phase6-bitget-rest-20260924.jsonl.gz",
    "phase2_fixture": "phase6-replay-v2-phase2.jsonl",
    "bitcoin": {
        "chain": "main",
        "catchup_blocks": BTC_CATCHUP_BLOCKS,
        "large_block_height_offset": 0,
        "large_block_event_count": LARGE_BLOCK_EVENTS,
        "other_block_event_count": 1,
    },
    "ethereum": {
        "chain_id": "0x1",
        "first_height": ETHEREUM_START_HEIGHT,
        "block_count": ETHEREUM_BLOCKS,
        "native_transfers_per_block": 1,
    },
    "binance_spot": {
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "agg_trades_per_symbol": SPOT_TRADES_PER_SYMBOL,
    },
    "expected_final_cursors": {
        "bitcoin": BTC_FINAL_CURSOR,
        "ethereum": ETH_FINAL_CURSOR,
        "binance_spot_per_symbol": SPOT_FINAL_CURSOR,
    },
}

_FIXED_NOW = None
_SPOT_MESSAGES: list[dict[str, Any]] = []


def _phase7_failure_marker_payload(component, status, details, *, runtime_stage: str) -> dict[str, str] | None:
    status_value = getattr(status, "value", status)
    if component != "bitcoin_rpc" or status_value != "ERROR":
        return None
    reason = details.get("reason") if isinstance(details, dict) else None
    failure_stage = details.get("failure_stage") if isinstance(details, dict) else None
    exception_type = reason if isinstance(reason, str) and re.fullmatch(r"[A-Z][A-Za-z0-9]{0,79}", reason) else "OTHER"

    def safe_stage(value: Any) -> str:
        return value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value) else "UNKNOWN"

    return {
        "component": "bitcoin_rpc",
        "status": "ERROR",
        "exception_type": exception_type,
        "failure_stage": safe_stage(failure_stage),
        "runtime_stage": safe_stage(runtime_stage),
    }


def dataset_identity() -> dict[str, Any]:
    manifest = replay_v2._verify_manifest()
    phase1_bytes = replay_v2.PHASE1_PATH.read_bytes()
    phase2_bytes = replay_v2.PHASE2_PATH.read_bytes()
    spec_bytes = json.dumps(GENERATOR_SPEC, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(phase1_bytes + b"\0" + phase2_bytes + b"\0" + spec_bytes).hexdigest()
    return {
        "dataset_sha256": digest,
        "phase1_fixture_sha256": manifest["phase1_cassette"]["sha256"],
        "phase2_fixture_sha256": manifest["phase2_fixture"]["sha256"],
        "phase1_input_rows": manifest["phase1_cassette"]["event_total"],
        "phase2_input_rows": manifest["phase2_fixture"]["event_total"],
        "generator": GENERATOR_SPEC,
    }


def _fixed_settings(dsn: str):
    from dataclasses import replace

    settings = replay_v2._fixed_settings(dsn)
    return replace(
        settings,
        phase7_enabled=True,
        phase7_bitcoin_rpc=Phase7RpcSourceSettings(
            source_id="bitcoin_rpc", enabled=True,
            endpoint="https://bitcoin-resource-replay.invalid/rpc",
        ),
        phase7_ethereum_rpc=Phase7RpcSourceSettings(
            source_id="ethereum_rpc", enabled=True,
            endpoint="https://ethereum-resource-replay.invalid/rpc",
        ),
    )


def _hex_hash(prefix: str) -> str:
    return hashlib.sha256(prefix.encode("ascii")).hexdigest()


def _bitcoin_block(height: int) -> dict[str, Any]:
    count = LARGE_BLOCK_EVENTS if height == BASE_HEIGHT else 1
    block = _build_block(count)
    block["height"] = height
    block["hash"] = f"{height:064x}"
    for index, transaction in enumerate(block["tx"]):
        txid = _hex_hash(f"resource-replay-v3:btc:{height}:{index}")
        transaction["txid"] = txid
        transaction["hash"] = txid
        for input_index, item in enumerate(transaction["vin"]):
            if isinstance(item, dict) and isinstance(item.get("txid"), str):
                item["txid"] = _hex_hash(f"resource-replay-v3:btc-prev:{height}:{index}:{input_index}")
    # Match the production JSON-RPC decoder: JSON numeric literals become
    # Decimal before Bitcoin amount normalization, never binary float.
    return json.loads(json.dumps(block, separators=(",", ":")), parse_float=Decimal)


def _ethereum_hash(height: int) -> str:
    return "0x" + _hex_hash(f"resource-replay-v3:eth:block:{height}")


def _response_size_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError("unsupported deterministic RPC response value")


def _ethereum_transaction(height: int) -> dict[str, Any]:
    return {
        "hash": "0x" + _hex_hash(f"resource-replay-v3:eth:tx:{height}"),
        "transactionIndex": "0x0",
        "from": "0x1111111111111111111111111111111111111111",
        "to": "0x2222222222222222222222222222222222222222",
        "value": "0xde0b6b3a7640000",
    }


class ResourceReplayJsonRpcClient:
    """Source-shaped deterministic responses; no network session is used."""

    def __init__(self, config, _session):
        self.source_id = config.source_id
        self.active_requests = 0
        self.request_count = 0
        self.response_bytes_total = 0
        self.last_response_bytes = 0
        self.last_method: str | None = None
        from quant_phase7.bitcoin import BitcoinBlockParser

        self.btc_tip = BASE_HEIGHT + BTC_CATCHUP_BLOCKS - 1 + BitcoinBlockParser().finalized_depth
        self.eth_finalized = ETHEREUM_START_HEIGHT + ETHEREUM_BLOCKS - 1

    def _record_response(self, payload: Any) -> Any:
        # Decimal values mirror the production Bitcoin decoder. Stringifying
        # only for this synthetic byte estimate keeps the source payload exact
        # while conservatively accounting for numeric token bytes.
        self.last_response_bytes = len(json.dumps(
            payload, separators=(",", ":"), default=_response_size_default,
        ).encode())
        self.response_bytes_total += self.last_response_bytes
        return payload

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        self.request_count += 1
        self.last_method = method
        params = params or []
        if self.source_id == "bitcoin_rpc":
            if method == "getblockchaininfo":
                return self._record_response({"chain": "main", "blocks": self.btc_tip})
            if method == "getblockhash":
                self._last_height = int(params[0])
                return self._record_response(f"{self._last_height:064x}")
            if method == "getblock":
                return self._record_response(
                    _bitcoin_block(
                        int(params[0]) if len(params) > 0 and str(params[0]).isdigit() else self._last_height
                    )
                )
            raise AssertionError(f"unexpected deterministic Bitcoin RPC method: {method}")

        if method == "eth_chainId":
            return self._record_response("0x1")
        if method == "eth_blockNumber":
            return self._record_response(hex(self.eth_finalized + 32))
        if method == "eth_getLogs":
            return self._record_response([])
        if method == "eth_getBlockByNumber":
            tag = params[0]
            height = self.eth_finalized if tag == "finalized" else int(str(tag), 16)
            block = {
                "number": hex(height),
                "hash": _ethereum_hash(height),
                "timestamp": hex(1_790_035_200 + (height - ETHEREUM_START_HEIGHT) * 12),
                "transactions": [_ethereum_transaction(height)] if params[1] else [],
            }
            return self._record_response(block)
        raise AssertionError(f"unexpected deterministic Ethereum RPC method: {method}")

    async def call_batch(self, calls: list[tuple[str, list[Any]]]) -> list[dict[str, Any]]:
        receipts = []
        for method, params in calls:
            if method != "eth_getTransactionReceipt":
                raise AssertionError(f"unexpected deterministic Ethereum batch method: {method}")
            tx_hash = params[0]
            height = next(
                height for height in range(ETHEREUM_START_HEIGHT, self.eth_finalized + 1)
                if _ethereum_transaction(height)["hash"] == tx_hash
            )
            receipts.append({
                "transactionHash": tx_hash,
                "blockHash": _ethereum_hash(height),
                "blockNumber": hex(height),
                "transactionIndex": "0x0",
                "status": "0x1",
                "logs": [],
            })
        return self._record_response(receipts)


class _ReplaySocket:
    def __init__(self, messages: list[dict[str, Any]]):
        import aiohttp

        self._text_type = aiohttp.WSMsgType.TEXT
        self._messages = [
            {"id": 1, "result": None}, {"id": 2, "result": None}, *messages,
        ]
        self._index = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def send_json(self, _payload: dict[str, Any]) -> None:
        return None

    async def receive(self):
        if self._index < len(self._messages):
            payload = self._messages[self._index]
            self._index += 1
            await asyncio.sleep(0)
            return SimpleNamespace(type=self._text_type, data=json.dumps(payload, separators=(",", ":")))
        await asyncio.Future()

    async def close(self) -> None:
        return None


def _spot_payloads(now) -> list[dict[str, Any]]:
    event_ms = int((now - timedelta(minutes=2)).timestamp() * 1000)
    result = []
    for symbol in ("BTCUSDT", "ETHUSDT"):
        for trade_id in range(1, SPOT_TRADES_PER_SYMBOL + 1):
            result.append({
                "e": "aggTrade", "E": event_ms + 1, "s": symbol, "a": trade_id,
                "p": "100000", "q": "0.001", "f": trade_id, "l": trade_id,
                "T": event_ms, "m": bool(trade_id % 2), "M": True,
            })
    return result


def _install_phase7_replay(settings, fixed_now) -> None:
    import quant_phase7.runtime as phase7_runtime
    from quant_phase7.contracts import DataStatus

    global _FIXED_NOW, _SPOT_MESSAGES
    _FIXED_NOW = fixed_now
    _SPOT_MESSAGES = _spot_payloads(fixed_now)
    phase7_runtime.utc_now = lambda: _FIXED_NOW
    phase7_runtime.Phase7JsonRpcClient = ResourceReplayJsonRpcClient
    original_runtime = phase7_runtime.Phase7CollectorRuntime

    class ResourceReplayPhase7Runtime(original_runtime):
        def __init__(self, *args, **kwargs):
            kwargs["spot_ws_connector"] = lambda _session, _endpoint: _ReplaySocket(_SPOT_MESSAGES)
            super().__init__(*args, **kwargs)
            self.cycle_handlers["binance_spot"] = self._resource_spot_cycle

        async def _process_bitcoin_block(self, *args, **kwargs):
            event_count = await super()._process_bitcoin_block(*args, **kwargs)
            if kwargs.get("height") == BASE_HEIGHT and event_count == LARGE_BLOCK_EVENTS:
                # Parent returns only after the event chunks and block cursor
                # commit atomically. This gates Collector shutdown on actual
                # Phase 7 persistence, not merely Phase 1/3/6 readiness.
                replay_v2._write_marker("phase7-resource-replay-ready.json", {
                    "block_height": BASE_HEIGHT,
                    "event_count": event_count,
                    "checkpoint_committed": True,
                })
            return event_count

        async def _write_health(self, component, status, checked_at, details):
            diagnostic = _phase7_failure_marker_payload(
                component,
                status,
                details,
                runtime_stage=self._source_stages.get(component, "UNKNOWN"),
            )
            if diagnostic is not None:
                replay_v2._write_marker("phase7-source-failure.json", diagnostic)
            await super()._write_health(component, status, checked_at, details)

        async def _resource_spot_cycle(self):
            expected = len(_SPOT_MESSAGES)
            deadline = asyncio.get_running_loop().time() + 30
            while self._spot_ws_metrics["accepted_trades"] < expected:
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("deterministic spot replay did not drain")
                await asyncio.sleep(0.01)
            persisted = 0
            now = _FIXED_NOW
            with self._repository_scope() as repository:
                for symbol in ("BTCUSDT", "ETHUSDT"):
                    checkpoint = repository.load_checkpoint("binance_spot", "EXCHANGE", symbol)
                    persisted += self._persist_closed_spot_windows(
                        repository, symbol, self._spot_events[symbol], checkpoint, now,
                    )
            return DataStatus.AVAILABLE, {
                "exchange": "BINANCE_SPOT", "symbols": 2,
                "rest_responses": 0, "replay_ws_trades": expected,
                "persisted_windows": persisted, "pending_trades": 0,
                "queue_depth": sum(len(events) for events in self._spot_events.values()),
                "runtime_stage": "PERSISTED" if persisted else "REPLAY_DRAINED",
            }

    phase7_runtime.Phase7CollectorRuntime = ResourceReplayPhase7Runtime


async def _run(role: str) -> int:
    if os.environ.get("PHASE7_RESOURCE_REPLAY_V3") != "1":
        raise RuntimeError("explicit PHASE7_RESOURCE_REPLAY_V3=1 is required")
    dsn = os.environ["POSTGRES_DSN"]
    settings = _fixed_settings(dsn)
    identity = dataset_identity()
    client = replay_v2._load_replay(settings)
    if client.input_event_count != identity["phase1_input_rows"]:
        raise RuntimeError("frozen Phase 1 input count changed")
    fixed_now = replay_v2._patch_clock(client)
    if role == "collector":
        replay_v2._write_marker("resource-replay-v3-manifest.json", identity)
        _install_phase7_replay(settings, fixed_now)
        await replay_v2._run_collector(settings, client)
    else:
        await replay_v2._run_engine(settings, client)
    return 0


def main() -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("collector", "engine"), required=True)
    args = parser.parse_args()
    return asyncio.run(_run(args.role))


if __name__ == "__main__":
    raise SystemExit(main())
