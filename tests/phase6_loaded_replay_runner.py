"""Diagnostic PID-1 runner for disposable-Postgres loaded shutdown cycles."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import gzip
import json
import linecache
import logging
import os
from pathlib import Path
import signal
import time
from typing import Any

from quant_phase1.adapters.bitget_v3.parsers import (
    parse_candles_response, parse_instruments_response, parse_tickers_response,
)
from quant_phase1.config import Settings
from quant_phase1.contracts import Candle
from quant_phase1.entrypoints import collector as collector_entrypoint
from quant_phase1.freshness import INTERVAL_SECONDS
from quant_phase1.pipeline import MarketDataCollector
from quant_phase1.time import ensure_utc


FIXTURE = Path("/workspace/tests/fixtures/replays/phase6-bitget-rest-20260924.jsonl.gz")
LOG = logging.getLogger("phase6_replay_diagnostic")


def _dt(value: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _database_summary(dsn: str) -> dict[str, Any]:
    import psycopg
    from psycopg import sql

    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_database(), current_setting('TimeZone')")
            database, timezone_name = cursor.fetchone()
            cursor.execute(
                "SELECT relname, n_tup_ins, n_tup_upd, n_tup_del "
                "FROM pg_stat_user_tables ORDER BY relname"
            )
            tuple_stats = {
                name: {"inserted": inserted, "updated": updated, "deleted": deleted}
                for name, inserted, updated, deleted in cursor.fetchall()
            }
            row_counts: dict[str, int] = {}
            cursor.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY table_name"
            )
            for (table_name,) in cursor.fetchall():
                cursor.execute(sql.SQL("SELECT count(*) FROM {} ").format(sql.Identifier(table_name)))
                row_counts[table_name] = cursor.fetchone()[0]
    return {
        "database": database,
        "timezone": timezone_name,
        "tuple_stats": tuple_stats,
        "row_counts": row_counts,
    }


def _database_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    table_names = sorted(set(before["tuple_stats"]) | set(after["tuple_stats"]))
    return {
        table: {
            metric: after["tuple_stats"].get(table, {}).get(metric, 0)
            - before["tuple_stats"].get(table, {}).get(metric, 0)
            for metric in ("inserted", "updated", "deleted")
        }
        for table in table_names
    }


class ReplayRestClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.instruments: list[tuple[dict[str, Any], datetime]] = []
        self.tickers: list[tuple[dict[str, Any], datetime]] = []
        self.candles: dict[tuple[str, str], list[tuple[datetime, list[Any], datetime]]] = {}
        self.drop_keys: set[tuple[str, str, datetime]] = set()
        self.recovery_rows: dict[tuple[str, str, datetime], tuple[list[Any], datetime]] = {}
        self.ws_events: list[dict[str, Any]] = []
        self.input_event_count = 0
        self.fixed_ws_reset_count = 0
        self.phase3_event_totals: dict[str, int] = {}
        with gzip.open(FIXTURE, "rt", encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                self.input_event_count += 1
                event_type = row["event_type"]
                source = row["source"]
                if row.get("fault_event") == "WS_RESET":
                    self.fixed_ws_reset_count += 1
                fetched_at = _dt(row.get("fetched_at") or row["event_timestamp"])
                if row.get("fault_event") == "DROP_KLINE_DURING_BOOTSTRAP":
                    open_at = _dt(row["payload"]["bar_open_timestamp"])
                    self.drop_keys.add((row["symbol"], row["interval"], open_at))
                elif source == "bitget_v3_rest" and event_type == "instrument":
                    self.instruments.append((row["payload"], fetched_at))
                elif source == "bitget_v3_rest" and event_type == "ticker":
                    self.tickers.append((row["payload"], fetched_at))
                elif source == "bitget_v3_rest" and event_type == "kline":
                    key = (row["symbol"], row["interval"])
                    open_at = _dt(row["event_timestamp"])
                    value = (open_at, row["payload"], fetched_at)
                    self.candles.setdefault(key, []).append(value)
                    if (row["symbol"], row["interval"], open_at) in self.drop_keys:
                        self.recovery_rows[(row["symbol"], row["interval"], open_at)] = (row["payload"], fetched_at)
                elif source.startswith(("bitget-uta-v3-", "bybit-v5-", "hyperliquid-")):
                    self.ws_events.append(row)
        self.clock = max(
            [at for _, at in self.instruments]
            + [at for _, at in self.tickers]
            + [at for values in self.candles.values() for _, _, at in values]
        )
        for values in self.candles.values():
            values.sort(key=lambda value: value[0])
        self.instrument_count = len(self.instruments)
        self.ticker_count = len(self.tickers)
        self.kline_count = sum(map(len, self.candles.values()))
        self.ws_cursor = {"bitget": 0, "bybit": 0, "hyperliquid": 0}
        # Fixed test-only hold keeps recovery work outstanding through the
        # complete replay and controlled SIGTERM window.
        self.recovery_delay_seconds = 30.0

    async def __aenter__(self) -> "ReplayRestClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get_instruments(self):
        return [
            parse_instruments_response({"code": "00000", "data": [payload]}, fetched_at=fetched_at)[0]
            for payload, fetched_at in self.instruments
        ]

    async def get_tickers(self, symbol: str | None = None):
        rows = self.tickers if symbol is None else [row for row in self.tickers if row[0].get("symbol") == symbol]
        tickers = [
            parse_tickers_response({"code": "00000", "data": [payload]}, fetched_at=fetched_at)[0]
            for payload, fetched_at in rows
        ]
        return tickers

    async def get_candles(
        self, *, symbol: str, interval: str, limit: int = 100,
        start_time: datetime | None = None, end_time: datetime | None = None,
    ) -> list[Candle]:
        key = (symbol, interval)
        if (start_time is None) != (end_time is None):
            raise ValueError("replay recovery requires both range bounds")
        if start_time is not None and end_time is not None:
            start_at, end_at = ensure_utc(start_time), ensure_utc(end_time)
            candidates = [
                (opened, payload, fetched)
                for opened, payload, fetched in self.candles.get(key, ())
                if start_at <= opened <= end_at
            ]
            if candidates:
                # The delayed read keeps a non-zero, cancellable recovery backlog
                # during the loaded SIGTERM window. It still returns only cassette rows.
                if any((symbol, interval, opened) in self.drop_keys for opened, _, _ in candidates):
                    await asyncio.sleep(self.recovery_delay_seconds)
                rows = [payload for _, payload, _ in candidates]
                return parse_candles_response(
                    {"code": "00000", "data": rows}, symbol=symbol, interval=interval,
                    fetched_at=self.clock, now=self.clock,
                )
            return []

        values = self.candles.get(key, [])[-limit:]
        rows = [payload for opened, payload, _ in values
                if (symbol, interval, opened) not in self.drop_keys]
        if not values:
            return []
        return parse_candles_response(
            {"code": "00000", "data": rows}, symbol=symbol, interval=interval,
            fetched_at=values[-1][2], now=values[-1][2],
        )


class ReplayBitgetWebSocket:
    instances = 0
    opened_sockets: list["ReplayBitgetWebSocket"] = []

    def __init__(self, _settings: Settings) -> None:
        type(self).instances += 1
        self.instance_id = type(self).instances
        self.connection_generation = 0
        self.topic = ""
        self.interval: str | None = None
        self.symbols: set[str] = set()
        self.events: list[dict[str, Any]] = []
        self.cursor = 0
        self.closed = False
        type(self).opened_sockets.append(self)

    @staticmethod
    def ticker_arg(symbol: str) -> dict[str, str]:
        return {"instType": "usdt-futures", "topic": "ticker", "symbol": symbol}

    @staticmethod
    def kline_arg(symbol: str, interval: str) -> dict[str, str]:
        return {"instType": "usdt-futures", "topic": "kline", "symbol": symbol, "interval": interval}

    async def connect(self) -> int:
        self.connection_generation += 1
        return self.connection_generation

    async def subscribe_many(self, args: list[dict[str, str]], *, batch_size: int = 50) -> None:
        if not args:
            return
        self.topic = args[0]["topic"]
        self.interval = args[0].get("interval")
        self.symbols = {arg["symbol"] for arg in args}
        client = _REPLAY_CLIENT
        for event in client.ws_events:
            if self.topic == "ticker" and event["source"] == "bitget-uta-v3-ticker":
                if event["symbol"] in self.symbols:
                    self.events.append(event)
            elif self.topic == "kline" and event["source"] == "bitget-uta-v3-kline":
                if event["symbol"] in self.symbols and event.get("interval") == self.interval:
                    raw_open = event["payload"].get("data", [{}])[0].get("start")
                    if raw_open is not None:
                        opened = datetime.fromtimestamp(int(raw_open) / 1000, timezone.utc)
                        if (event["symbol"], self.interval, opened) in client.drop_keys:
                            continue
                    self.events.append(event)
        if self.topic == "ticker":
            self.events.extend(event for event in client.ws_events
                               if event.get("fault_event") == "WS_RESET"
                               and event["source"] == "bitget-uta-v3-public")
        self.events.sort(key=lambda event: event["receive_order"])

    async def receive(self, *, expected_generation: int | None = None) -> dict[str, Any]:
        while self.cursor < len(self.events):
            event = self.events[self.cursor]
            self.cursor += 1
            if event.get("fault_event") == "WS_RESET":
                raise ConnectionResetError("deterministic replay reset")
            await asyncio.sleep(0.001)
            return event["payload"]
        await asyncio.Future()

    async def ping(self) -> None:
        return None

    async def reconnect(self, *, expected_generation: int | None = None) -> int:
        self.connection_generation += 1
        return self.connection_generation

    async def close(self) -> None:
        self.closed = True


class ReplayAsyncSocket:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.cursor = 0

    async def __aenter__(self) -> "ReplayAsyncSocket":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def send(self, _payload: str) -> None:
        return None

    async def recv(self) -> str:
        while self.cursor < len(self.events):
            event = self.events[self.cursor]
            self.cursor += 1
            if event.get("fault_event") == "WS_RESET":
                raise ConnectionResetError("deterministic replay reset")
            await asyncio.sleep(0.001)
            return json.dumps(event["payload"], separators=(",", ":"))
        await asyncio.Future()


class Phase3ReplayConnector:
    def __init__(self, client: ReplayRestClient) -> None:
        self.client = client

    def __call__(self, url: str, **_kwargs: Any) -> ReplayAsyncSocket:
        exchange = "bitget" if "bitget" in url else "bybit" if "bybit" in url else "hyperliquid"
        source_prefix = {"bitget": "bitget-uta-v3-publicTrade", "bybit": "bybit-v5-public",
                         "hyperliquid": "hyperliquid-public"}[exchange]
        reset_source = {"bitget": "bitget-uta-v3-publicTrade", "bybit": "bybit-v5-public",
                        "hyperliquid": "hyperliquid-public"}[exchange]
        events = [event for event in self.client.ws_events
                  if ((event["event_type"] == "public_trade" and event["source"] == source_prefix)
                      or (event.get("fault_event") == "WS_RESET" and event["source"] == reset_source))]
        events.sort(key=lambda event: event["receive_order"])
        self.client.phase3_event_totals[exchange] = len(events)
        cursor_key = f"phase3:{exchange}"
        offset = self.client.ws_cursor.setdefault(cursor_key, 0)

        class CursorSocket(ReplayAsyncSocket):
            async def recv(self_inner) -> str:
                while offset + self_inner.cursor < len(events):
                    event = events[offset + self_inner.cursor]
                    self_inner.cursor += 1
                    self.client.ws_cursor[cursor_key] = offset + self_inner.cursor
                    if event.get("fault_event") == "WS_RESET":
                        raise ConnectionResetError("deterministic replay reset")
                    await asyncio.sleep(0.001)
                    return json.dumps(event["payload"], separators=(",", ":"))
                await asyncio.Future()

        return CursorSocket(())


class Phase4ReplayConnector:
    def __call__(self, _url: str, **_kwargs: Any) -> ReplayAsyncSocket:
        return ReplayAsyncSocket([])


_REPLAY_CLIENT: ReplayRestClient


async def _run() -> int:
    global _REPLAY_CLIENT
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    dsn = os.environ["POSTGRES_DSN"]
    settings = Settings.from_env({
        "TRADING_MODE": "paper", "POSTGRES_DSN": dsn, "UNIVERSE_LIMIT": "200",
        "KLINE_FETCH_LIMIT": "100", "TICKER_PERSIST_INTERVAL_SECONDS": "3600",
        "WS_RECONNECT_SECONDS": "0.01", "PHASE2_ENABLED": "0", "PHASE3_ENABLED": "1",
        "PHASE4_ENABLED": "1", "PHASE4_REST_CYCLE_SECONDS": "3600", "PHASE5_ENABLED": "1",
        "PHASE6_ENABLED": "1", "PHASE6_INGESTION_INTERVAL_SECONDS": "300",
        "PHASE7_BITCOIN_RPC_ENABLED": "0", "PHASE7_ETHEREUM_RPC_ENABLED": "0",
    })
    db_before = _database_summary(dsn)
    client = ReplayRestClient(settings)
    _REPLAY_CLIENT = client

    import quant_phase1.pipeline as pipeline_module
    import quant_phase1.service as service_module
    import quant_phase3.runtime as phase3_module
    import quant_phase4.runtime as phase4_module

    fixed_now = client.clock
    pipeline_module.utc_now = lambda: fixed_now
    service_module.utc_now = lambda: fixed_now
    collector_entrypoint.utc_now = lambda: fixed_now

    class ReplayDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    phase3_module.datetime = ReplayDateTime
    phase4_module.datetime = ReplayDateTime
    collector_entrypoint.BitgetV3UtaRestClient = lambda _settings: client
    collector_entrypoint.BitgetV3UtaWebSocket = ReplayBitgetWebSocket

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    started_at = time.monotonic()
    service = collector_entrypoint.CollectorService(settings, stop_event=stop_event)
    service.phase3_runner.connect_factory = Phase3ReplayConnector(client)
    service.phase4_runtime._connect_factory = Phase4ReplayConnector()
    service.phase6_runtime.now = lambda: fixed_now
    phase3_persist_completed = asyncio.Event()

    class Phase3PersistObserver(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.getMessage() == "phase3_flow_persisted":
                phase3_persist_completed.set()

    persist_observer = Phase3PersistObserver()
    phase1_logger = logging.getLogger("quant_phase1")
    phase1_logger.addHandler(persist_observer)

    signal_seen = False
    observer: asyncio.Task[None] | None = None
    service_task: asyncio.Task[None] | None = None
    gap_at_sigterm: dict[str, int] | None = None

    def phase3_reconnects() -> dict[str, int]:
        if service.phase3_runtime is None:
            return {}
        return {
            exchange: service.phase3_runtime.health_snapshot(exchange).reconnect_count
            for exchange in sorted(service.phase3_runtime.adapters)
        }

    def replay_progress() -> dict[str, Any]:
        uta_expected = sum(len(socket.events) for socket in ReplayBitgetWebSocket.opened_sockets)
        uta_consumed = sum(socket.cursor for socket in ReplayBitgetWebSocket.opened_sockets)
        phase3_expected = dict(sorted(client.phase3_event_totals.items()))
        phase3_consumed = {
            exchange: client.ws_cursor.get(f"phase3:{exchange}", 0)
            for exchange in phase3_expected
        }
        drained = (
            len(ReplayBitgetWebSocket.opened_sockets) == 5
            and uta_consumed == uta_expected
            and set(phase3_expected) == {"bitget", "bybit", "hyperliquid"}
            and phase3_consumed == phase3_expected
        )
        return {
            "drained": drained,
            "uta_consumed": uta_consumed,
            "uta_expected": uta_expected,
            "phase3_consumed": phase3_consumed,
            "phase3_expected": phase3_expected,
        }

    async def shutdown_snapshots() -> None:
        signal_started = time.monotonic()
        for target in (2.0, 5.0, 8.0, 9.5):
            await asyncio.sleep(max(0.0, signal_started + target - time.monotonic()))
            if service_task is not None and service_task.done():
                print(f"SHUTDOWN_SNAPSHOT t+{target:g}s already-exited", flush=True)
                return
            pending = [task for task in asyncio.all_tasks()
                       if not task.done() and task is not asyncio.current_task()]
            frames = []
            for task in pending:
                stack = task.get_stack(limit=1)
                if stack:
                    frame = stack[-1]
                    source_line = linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
                    frames.append(
                        f"{task.get_name()}:{frame.f_code.co_filename}:{frame.f_code.co_name}:"
                        f"{frame.f_lineno}:{source_line}"
                    )
            print(f"SHUTDOWN_SNAPSHOT t+{target:g}s pending={len(pending)} stacks={';'.join(sorted(frames)[:32])}", flush=True)

    def on_sigterm() -> None:
        nonlocal signal_seen, observer, gap_at_sigterm
        if signal_seen:
            return
        signal_seen = True
        print(f"SIGTERM_RECEIVED elapsed={time.monotonic()-started_at:.3f}s", flush=True)
        gap = service.gap_recovery
        if gap is not None:
            gap_at_sigterm = {
                "scans": gap.reconciliation_runs,
                "candidates": gap.actual_gap_total,
                "admissions": gap.admitted_count,
                "completions": gap.completed_count,
                "active": gap.active_count,
                "pending": gap.pending_count,
                "inflight": gap.inflight_count,
            }
        stop_event.set()
        observer = asyncio.create_task(shutdown_snapshots(), name="replay-shutdown-snapshot")

    loop.add_signal_handler(signal.SIGTERM, on_sigterm)
    loop.add_signal_handler(signal.SIGINT, on_sigterm)
    service_task = asyncio.create_task(service.run(), name="collector-service")

    deadline = loop.time() + 180
    final_store_persisted = False
    while loop.time() < deadline:
        if service_task.done():
            error = service_task.exception()
            print(f"REPLAY_STARTUP_FAILED error={type(error).__name__ if error else 'normal-exit'}", flush=True)
            return 2
        gap = service.gap_recovery
        phases_running = (
            service.phase3_runtime is not None and service.phase3_runtime.running
            and service.phase4_runtime is not None and service.phase4_runtime.running
            and service.phase6_task is not None
        )
        progress = replay_progress()
        if progress["drained"] and not final_store_persisted:
            await service._persist_current_store()
            final_store_persisted = True
        if (gap is not None and gap.active_count > 0 and gap.pending_count > 0
                and phases_running and progress["drained"] and phase3_persist_completed.is_set()
                and final_store_persisted):
            print(json.dumps({"REPLAY_DATASET_DRAINED": progress}, sort_keys=True), flush=True)
            print(json.dumps({
                "REPLAY_ACTIVE": True,
                "input_event_total": client.input_event_count,
                "fixed_ws_reset_count": client.fixed_ws_reset_count,
                "websocket_replay": progress,
                "symbol_count": len(service.selected_symbols),
                "rest_instruments": client.instrument_count,
                "rest_tickers": client.ticker_count,
                "rest_klines": client.kline_count,
                "dropped_closed_bars": len(client.drop_keys),
                "ws_messages": service.store.received_messages,
                "ws_reconnects": service.ws_reconnect_count,
                "recovery_active": gap.active_count,
                "recovery_pending": gap.pending_count,
                "recovery_inflight": gap.inflight_count,
                "recovery_scans": gap.reconciliation_runs,
                "gap_candidates": gap.actual_gap_total,
                "recovery_admissions": gap.admitted_count,
                "phase3_queued": sum(queue.depth for queue in service.phase3_runtime._queues.values()),
                "phase3_reconnects": phase3_reconnects(),
                "phase3_persistence_observed": phase3_persist_completed.is_set(),
                "paper_mode": settings.trading_mode,
            }, sort_keys=True), flush=True)
            break
        await asyncio.sleep(0.02)
    else:
        print("REPLAY_ACTIVE_TIMEOUT", flush=True)
        stop_event.set()

    await stop_event.wait()
    await service_task
    if observer is not None:
        observer.cancel()
        await asyncio.gather(observer, return_exceptions=True)
    phase1_logger.removeHandler(persist_observer)
    loop.remove_signal_handler(signal.SIGTERM)
    loop.remove_signal_handler(signal.SIGINT)
    db_after = _database_summary(dsn)
    print(json.dumps({
        "SHUTDOWN_COMPLETE": True,
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
        "gap_at_sigterm": gap_at_sigterm,
        "gap_reconciliation_runs": gap_at_sigterm["scans"] if gap_at_sigterm else None,
        "gap_candidates": gap_at_sigterm["candidates"] if gap_at_sigterm else None,
        "gap_admissions": gap_at_sigterm["admissions"] if gap_at_sigterm else None,
        "gap_completions": gap_at_sigterm["completions"] if gap_at_sigterm else None,
        "unjoined_tasks": [task.get_name() for task in asyncio.all_tasks()
                           if task is not asyncio.current_task() and not task.done()],
        "phase3_running": service.phase3_runtime.running if service.phase3_runtime else None,
        "phase4_running": service.phase4_runtime.running if service.phase4_runtime else None,
        "phase3_reconnects": phase3_reconnects(),
        "database": db_after["database"],
        "database_timezone": db_after["timezone"],
        "db_row_counts": db_after["row_counts"],
        "db_tuple_stats_delta": _database_delta(db_before, db_after),
    }, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
