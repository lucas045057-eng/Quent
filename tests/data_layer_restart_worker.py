from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from types import SimpleNamespace
from typing import Any


def _settings():
    from quant_phase1.config import Settings

    return Settings.from_env({
        "TRADING_MODE": "paper",
        "POSTGRES_DSN": os.environ["POSTGRES_DSN"],
        "PHASE2_ENABLED": "0",
        "PHASE3_ENABLED": "0",
        "PHASE4_ENABLED": "0",
        "PHASE5_ENABLED": "0",
        "PHASE6_ENABLED": "0",
        "PHASE7_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_URL": "https://restart-fixture.invalid/rpc",
        "PHASE7_BITCOIN_RPC_REQUESTS_PER_SECOND": "100",
        "PHASE7_ETHEREUM_RPC_ENABLED": "0",
        "PHASE8_ENABLED": "0",
    })


def _block(height: int, block_hash: str, confirmations: int) -> dict[str, Any]:
    tx_hash = hashlib.sha256(f"data-layer-restart:tx:{height}".encode()).hexdigest()
    previous_tx = hashlib.sha256(f"data-layer-restart:prev:{height}".encode()).hexdigest()
    return {
        "hash": block_hash,
        "height": height,
        "time": 1_790_035_200,
        "confirmations": confirmations,
        "tx": [{
            "txid": tx_hash,
            "vin": [{
                "txid": previous_tx,
                "vout": 0,
                "prevout": {"value": "0.50000000", "scriptPubKey": {"address": "bc1qrestartsource"}},
            }],
            "vout": [{"n": 0, "value": "0.25000000", "scriptPubKey": {"address": "bc1qrestartdest"}}],
        }],
    }


class _FakeBitcoinClient:
    def __init__(self, config, _session):
        self.source_id = config.source_id
        self.tip = 1_000
        self.active_requests = 0
        self.request_count = 0
        self.response_bytes_total = 0
        self.last_response_bytes = 0
        self.last_method = None

    async def call(self, method: str, params: list[Any] | None = None):
        self.request_count += 1
        self.last_method = method
        params = params or []
        if method == "getblockchaininfo":
            result = {"chain": "main", "blocks": self.tip}
        elif method == "getblockhash":
            result = f"{int(params[0]):064x}"
        elif method == "getblock":
            height = int(str(params[0]), 16)
            result = _block(height, str(params[0]), self.tip - height + 1)
        else:
            raise AssertionError("unexpected deterministic RPC method")
        self.last_response_bytes = len(json.dumps(result, separators=(",", ":")).encode())
        self.response_bytes_total += self.last_response_bytes
        return result


class _FakeSocket:
    def __init__(self, attempt: int, factory):
        self.attempt = attempt
        self.factory = factory
        self.sent: list[dict[str, Any]] = []
        self.ack_index = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)
        self.factory.subscription_count += 1

    async def receive(self):
        import aiohttp

        if self.attempt == 1:
            return SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None)
        if self.ack_index < 2:
            self.ack_index += 1
            self.factory.ack_count += 1
            if self.ack_index == 2:
                self.factory.recovered.set()
            return SimpleNamespace(
                type=aiohttp.WSMsgType.TEXT,
                data=json.dumps({"id": self.ack_index, "result": None}),
            )
        await asyncio.Future()

    async def close(self) -> None:
        return None


class _SocketFactory:
    def __init__(self):
        self.attempts = 0
        self.subscription_count = 0
        self.ack_count = 0
        self.recovered = asyncio.Event()

    def __call__(self, _session, _endpoint):
        self.attempts += 1
        return _FakeSocket(self.attempts, self)


@asynccontextmanager
async def _fake_rpc_session():
    yield object()


async def _run_cycle() -> dict[str, Any]:
    import psycopg

    from quant_phase1.db import apply_migrations
    from quant_phase7.persistence import Phase7Repository
    import quant_phase7.runtime as phase7_runtime

    dsn = os.environ["POSTGRES_DSN"]
    cycle = int(os.environ["FAILURE_MATRIX_CYCLE"])
    with psycopg.connect(dsn, autocommit=True) as connection:
        migrations_first = apply_migrations(connection)
        migrations_repeat = apply_migrations(connection)
        before = Phase7Repository(connection).load_checkpoint("btc_core_rpc", "CHAIN", "BITCOIN")
        cursor_before = int(before["cursor_value"]) if before is not None else None

    phase7_runtime.Phase7JsonRpcClient = _FakeBitcoinClient
    phase7_runtime.Phase7CollectorRuntime._rpc_session = staticmethod(_fake_rpc_session)
    settings = _settings()
    socket_factory = _SocketFactory()
    runtime = phase7_runtime.Phase7CollectorRuntime(
        settings, cycle_interval_seconds=60, spot_ws_connector=socket_factory,
        spot_websocket_enabled=True,
    )
    stop_event = asyncio.Event()
    health_calls: list[dict[str, str]] = []
    original_write_health = runtime._write_health

    async def record_health(component, status, checked_at, details):
        health_calls.append({
            "component": component,
            "status": str(getattr(status, "value", status)),
            "runtime_state": str(details.get("runtime_state", "")),
        })
        await original_write_health(component, status, checked_at, details)

    runtime._write_health = record_health

    async def bitcoin_cycle():
        await asyncio.wait_for(socket_factory.recovered.wait(), timeout=8)
        result = await runtime._bitcoin_cycle()
        stop_event.set()
        return result

    async def no_network_spot_cycle():
        await asyncio.Future()

    runtime.cycle_handlers["bitcoin_rpc"] = bitcoin_cycle
    runtime.cycle_handlers["binance_spot"] = no_network_spot_cycle
    initial_queue = runtime.admission.snapshot()
    await asyncio.wait_for(runtime.run(stop_event), timeout=90)
    final_queue = runtime.admission.snapshot()

    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*),count(DISTINCT event_id),min(block_number),max(block_number) "
                "FROM phase7_onchain_transfer_events WHERE chain='BITCOIN'"
            )
            event_count, distinct_event_count, first_block, last_block = cursor.fetchone()
            cursor.execute(
                "SELECT cursor_value,last_block_hash FROM phase7_ingestion_checkpoints "
                "WHERE source_id='btc_core_rpc' AND scope_kind='CHAIN' AND scope_key='BITCOIN'"
            )
            checkpoint = cursor.fetchone()
            cursor.execute("SELECT status,details FROM system_health WHERE component='bitcoin_rpc'")
            bitcoin_health = cursor.fetchone()

    if checkpoint is None or bitcoin_health is None:
        raise RuntimeError("runtime did not persist its checkpoint and health")
    cursor_after = int(checkpoint[0])
    health_snapshot = runtime.health_registry.snapshot()
    bitcoin_runtime_state = health_snapshot["bitcoin_rpc"].details.get("runtime_state")
    websocket_statuses = [
        item["status"] for item in health_calls
        if item["component"] == "binance_spot_websocket"
    ]
    btc_statuses = [item["status"] for item in health_calls if item["component"] == "bitcoin_rpc"]
    recovered = (
        "ERROR" in websocket_statuses
        and "AVAILABLE" in websocket_statuses
        and "AVAILABLE" in btc_statuses
        and bitcoin_health[0] == "NOT_AVAILABLE"
        and bitcoin_runtime_state == "STOPPED"
    )
    queue_clean = (
        initial_queue.active == initial_queue.pending == 0
        and final_queue.active == final_queue.pending == 0
        and all(value == 0 for value in initial_queue.oldest_pending_wait_seconds_by_class.values())
        and all(value == 0 for value in final_queue.oldest_pending_wait_seconds_by_class.values())
        and runtime.task_count == 0
        and not runtime._running
    )
    expected_cursor_after = 862 if cursor_before is None else cursor_before + 12
    if cursor_after != expected_cursor_after:
        raise RuntimeError("restart cycle did not advance exactly one bounded 12-block window")
    if event_count != cycle * 12 or event_count != distinct_event_count:
        raise RuntimeError("persisted canonical event count or identity deduplication failed")
    if socket_factory.attempts < 2 or socket_factory.subscription_count != 4 or socket_factory.ack_count != 2:
        raise RuntimeError("WebSocket reconnect/resubscribe did not complete")
    if not recovered or not queue_clean or migrations_repeat:
        raise RuntimeError("restart lifecycle contract failed: " + json.dumps({
            "health_recovered": recovered,
            "queue_clean": queue_clean,
            "migration_repeat_count": len(migrations_repeat),
            "websocket_statuses": websocket_statuses,
            "bitcoin_statuses": btc_statuses,
            "final_health_state": bitcoin_runtime_state,
            "initial_active": initial_queue.active,
            "initial_pending": initial_queue.pending,
            "final_active": final_queue.active,
            "final_pending": final_queue.pending,
            "runtime_task_count": runtime.task_count,
            "runtime_running": runtime._running,
        }, sort_keys=True, separators=(",", ":")))

    return {
        "cycle": cycle,
        "cursor_before": cursor_before,
        "cursor_after": cursor_after,
        "first_block": first_block,
        "last_block": last_block,
        "event_count": event_count,
        "distinct_event_count": distinct_event_count,
        "migration_first_count": len(migrations_first),
        "migration_repeat_count": len(migrations_repeat),
        "ws_connect_attempts": socket_factory.attempts,
        "ws_subscription_count": socket_factory.subscription_count,
        "ws_ack_count": socket_factory.ack_count,
        "health_recovered": recovered,
        "health_final": bitcoin_runtime_state,
        "queue_clean": queue_clean,
        "status": "PASS",
    }


def main() -> int:
    logging.disable(logging.CRITICAL)
    result = asyncio.run(_run_cycle())
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
