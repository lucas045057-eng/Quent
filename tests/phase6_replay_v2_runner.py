"""Run the official Collector or Engine lifecycle against frozen test replay inputs.

This is disposable-PostgreSQL diagnostic tooling. All exchange transports are
replaced at their public-input boundaries; Phase1-6 runtime entrypoints,
processors, lifecycle owners, and persistence paths remain the production ones.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import gzip
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
MANIFEST_PATH = TESTS / "fixtures/replays/phase6-replay-v2-manifest.json"
PHASE2_PATH = TESTS / "fixtures/replays/phase6-replay-v2-phase2.jsonl"
PHASE1_PATH = TESTS / "fixtures/replays/phase6-bitget-rest-20260924.jsonl.gz"
RESULTS = Path(os.environ.get("REPLAY_RESULTS_DIR", "/tmp/phase6-replay-v2-results"))

spec = importlib.util.spec_from_file_location(
    "phase6_loaded_replay_runner", TESTS / "phase6_loaded_replay_runner.py"
)
if spec is None or spec.loader is None:
    raise RuntimeError("could not import the existing Replay V1 harness")
v1 = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = v1
spec.loader.exec_module(v1)

from quant_phase1.config import Settings  # noqa: E402
from quant_phase1.contracts import DataStatus as Phase1DataStatus  # noqa: E402

LOGGER = logging.getLogger("phase6_replay_v2")


def _write_marker(name: str, value: Any) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    destination = RESULTS / name
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(destination)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _verify_manifest() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    phase1_bytes = PHASE1_PATH.read_bytes()
    phase2_bytes = PHASE2_PATH.read_bytes()
    composite = hashlib.sha256(phase1_bytes + b"\0" + phase2_bytes).hexdigest()
    if hashlib.sha256(phase1_bytes).hexdigest() != manifest["phase1_cassette"]["sha256"]:
        raise RuntimeError("Phase1 replay cassette differs from the frozen manifest")
    if hashlib.sha256(phase2_bytes).hexdigest() != manifest["phase2_fixture"]["sha256"]:
        raise RuntimeError("Phase2 replay fixture differs from the frozen manifest")
    if composite != manifest["dataset_sha256"]:
        raise RuntimeError("composite Replay V2 dataset hash mismatch")
    phase2_rows = _read_jsonl(PHASE2_PATH)
    if len(phase2_rows) != manifest["phase2_fixture"]["event_total"]:
        raise RuntimeError("Phase2 replay input count differs from the frozen manifest")
    if any(row.get("synthetic_test_fixture") is not True for row in phase2_rows):
        raise RuntimeError("Phase2 fixture contains an unmarked test input")
    return manifest


def _fixed_settings(dsn: str) -> Settings:
    return Settings.from_env({
        "TRADING_MODE": "paper",
        "POSTGRES_DSN": dsn,
        "UNIVERSE_LIMIT": "200",
        "KLINE_FETCH_LIMIT": "100",
        "TICKER_PERSIST_INTERVAL_SECONDS": "3600",
        "UNIVERSE_INTERVAL_SECONDS": "3600",
        "WS_RECONNECT_SECONDS": "0.01",
        "PHASE2_ENABLED": "1",
        "PHASE2_INTERVAL_SECONDS": "300",
        "PHASE3_ENABLED": "1",
        "PHASE4_ENABLED": "1",
        "PHASE4_REST_CYCLE_SECONDS": "3600",
        "PHASE5_ENABLED": "1",
        "PHASE6_ENABLED": "1",
        "PHASE6_INGESTION_INTERVAL_SECONDS": "300",
        "PHASE6_AI_QUEUE_CAPACITY": "32",
        "PHASE6_AI_CONCURRENCY": "2",
        "PHASE7_BITCOIN_RPC_ENABLED": "0",
        "PHASE7_ETHEREUM_RPC_ENABLED": "0",
    })


def _load_replay(settings: Settings):
    v1.FIXTURE = PHASE1_PATH
    client = v1.ReplayRestClient(settings)
    v1._REPLAY_CLIENT = client
    # Recovery reads exactly the dropped closed-bar rows from the cassette;
    # the short delay preserves cancellability without suppressing recovery.
    client.recovery_delay_seconds = 0.05
    v1.ReplayBitgetWebSocket.instances = 0
    v1.ReplayBitgetWebSocket.opened_sockets.clear()
    return client


def _patch_clock(client) -> datetime:
    fixed_now = client.clock
    import quant_phase1.pipeline as pipeline_module
    import quant_phase1.service as service_module
    import quant_phase1.time as time_module
    import quant_phase3.runtime as phase3_module
    import quant_phase4.runtime as phase4_module

    pipeline_module.utc_now = lambda: fixed_now
    service_module.utc_now = lambda: fixed_now
    time_module.utc_now = lambda: fixed_now

    class ReplayDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is not None else fixed_now.replace(tzinfo=None)

    phase3_module.datetime = ReplayDateTime
    phase4_module.datetime = ReplayDateTime
    return fixed_now


async def _phase2_fixture_fetch(runtime, now, configured):
    from dataclasses import replace

    from quant_phase2.adapters.bitget import BitgetUTAAdapter
    from quant_phase2.adapters.bybit import BybitV5Adapter
    from quant_phase2.adapters.hyperliquid import HyperliquidAdapter
    from quant_phase2.normalization import apply_derivative_freshness, apply_funding_freshness

    manifest = _verify_manifest()
    expected_symbols = tuple(manifest["phase2_fixture"]["selected_symbols"])
    actual_symbols = tuple(str(symbol).upper() for symbol in configured)
    if actual_symbols != expected_symbols:
        raise RuntimeError("Phase2 universe does not match frozen Replay V2 symbol set")

    rows = _read_jsonl(PHASE2_PATH)
    by_type: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_type.setdefault(row["event_type"], []).append(row)
    bitget = BitgetUTAAdapter(registry=runtime.registry)
    bybit = BybitV5Adapter(registry=runtime.registry)
    hyperliquid = HyperliquidAdapter(registry=runtime.registry)
    instruments = []
    oi_rows = []
    funding_rows = []

    bg_instrument_payload = next(
        row["payload"] for row in by_type["instrument_payload"] if row["exchange"] == "bitget"
    )
    instruments.extend(
        replace(item, canonical_symbol=runtime.registry.canonical_for("bitget", item.exchange_symbol))
        for item in bitget.parse_instruments(bg_instrument_payload, now)
        if item.exchange_symbol in set(actual_symbols)
    )
    for row in by_type["ticker_payload"]:
        symbol = row["symbol"]
        if row["exchange"] == "bitget":
            oi, funding = bitget.parse_tickers({"code": "00000", "data": [row["payload"]]}, now)
            oi_rows.extend(oi)
            funding_rows.extend(funding)
        elif row["exchange"] == "bybit":
            payload = row["payload"]
            runtime.registry.resolve("bybit", symbol)
            # Instrument interval is fixed by the source-shaped Bybit metadata.
            interval = 480 * 60
            oi, funding = bybit.parse_ticker(payload, now, symbol=symbol, interval_seconds=interval)
            oi_rows.append(oi)
            funding_rows.append(funding)
    for row in by_type["current_funding_payload"]:
        funding_rows.append(bitget.parse_current_funding(
            {"code": "00000", "data": [row["payload"]]}, now, symbol=row["symbol"]
        ))

    bybit_instrument_payload = next(
        row["payload"] for row in by_type["instrument_payload"] if row["exchange"] == "bybit"
    )
    instruments.extend(
        item for item in bybit.parse_instruments(bybit_instrument_payload, now)
        if item.exchange_symbol in set(actual_symbols)
    )
    hl_payload = by_type["meta_and_asset_context_payload"][0]["payload"]
    hl_instruments, hl_oi, hl_funding = hyperliquid.parse_meta_and_contexts(hl_payload, now)
    selected_hl = {
        runtime.registry.resolve("hyperliquid", symbol.removesuffix("USDT")).exchange_symbols["hyperliquid"]
        for symbol in actual_symbols
    }
    instruments.extend(item for item in hl_instruments if item.exchange_symbol in selected_hl)
    oi_rows.extend(row for row in hl_oi if row.symbol in selected_hl)
    funding_rows.extend(row for row in hl_funding if row.symbol in selected_hl)

    oi_rows = apply_derivative_freshness(
        oi_rows, now, max_age_seconds=runtime.settings.phase2_oi_freshness_seconds
    )
    funding_rows = apply_funding_freshness(
        funding_rows, now, max_age_seconds=runtime.settings.phase2_funding_freshness_seconds
    )
    counts = Counter(row["event_type"] for row in rows)
    _write_marker("phase2-input-consumed.json", {
        "fixture_rows": len(rows),
        "event_type_counts": dict(sorted(counts.items())),
        "symbols": len(actual_symbols),
        "instrument_metadata": len(instruments),
        "open_interest_observations": len(oi_rows),
        "funding_observations": len(funding_rows),
        "source_funding_rate_is_neutral_zero": True,
        "bitget_oi_unit_semantics_preserved": all(
            row.exchange != "bitget" or (row.raw_unit == "UNCONFIRMED" and row.open_interest_base is None)
            for row in oi_rows
        ),
    })
    return instruments, oi_rows, funding_rows, []


def _install_phase2_fixture() -> None:
    import quant_phase2.runtime as phase2_runtime

    phase2_runtime.Phase2DerivativeRuntime._fetch = _phase2_fixture_fetch


def _install_engine_clock_and_observers(client, settings: Settings) -> None:
    import quant_phase1.entrypoints.engine as engine
    import quant_phase1.pipeline as pipeline_module
    from quant_phase1.runtime import PeriodicScheduler
    import quant_phase2.runtime as phase2_module
    import quant_phase6.runtime as phase6_runtime

    fixed_now = client.clock
    engine.utc_now = lambda: fixed_now
    engine.BitgetV3UtaRestClient = lambda _settings: client
    pipeline_module.utc_now = lambda: fixed_now

    original_once = engine.run_once

    async def observed_once(*args, **kwargs):
        started = time.perf_counter()
        result = await original_once(*args, **kwargs)
        _write_marker("engine-bootstrap.json", {
            **result,
            "runtime_seconds": round(time.perf_counter() - started, 6),
            "phase5_enabled": settings.phase5_enabled,
            "phase6_enabled": settings.phase6_enabled,
        })
        return result

    engine.run_once = observed_once

    original_phase2_cycle = phase2_module.Phase2DerivativeRuntime.run_cycle

    async def timed_phase2_cycle(self):
        started = time.perf_counter()
        result = await original_phase2_cycle(self)
        _write_marker("engine-phase2-cycle-runtime.json", {
            "duration_seconds": round(time.perf_counter() - started, 6),
            "symbols": result.symbols,
            "oi_observations": result.oi_observations,
            "funding_observations": result.funding_observations,
            "snapshots": result.snapshots,
            "errors": list(result.errors),
        })
        return result

    phase2_module.Phase2DerivativeRuntime.run_cycle = timed_phase2_cycle

    original_init = phase6_runtime.Phase6EngineRuntime.__init__

    def fixed_phase6_init(self, *args, **kwargs):
        kwargs.setdefault("now", lambda: fixed_now)
        original_init(self, *args, **kwargs)

    phase6_runtime.Phase6EngineRuntime.__init__ = fixed_phase6_init

    class EngineEventObserver(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.getMessage() == "phase2_derivatives_cycle_complete":
                _write_marker("engine-phase2-cycle.json", {
                    "symbols": getattr(record, "symbol", None),
                    "classification": getattr(record, "classification", {}),
                    "status": getattr(record, "status", None),
                })

    logging.getLogger("quant_phase1").addHandler(EngineEventObserver())
    # Assert we are observing, not replacing, the production scheduler.
    if not isinstance(engine.PeriodicScheduler, type) or engine.PeriodicScheduler is not PeriodicScheduler:
        raise RuntimeError("Engine scheduler identity changed unexpectedly")


async def _run_collector(settings: Settings, client) -> None:
    import quant_phase1.entrypoints.collector as entrypoint

    fixed_now = _patch_clock(client)
    entrypoint.utc_now = lambda: fixed_now
    entrypoint.BitgetV3UtaRestClient = lambda _settings: client
    entrypoint.BitgetV3UtaWebSocket = v1.ReplayBitgetWebSocket
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    phase3_persisted = False

    class Phase3PersistObserver(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            nonlocal phase3_persisted
            if record.getMessage() == "phase3_flow_persisted":
                phase3_persisted = True

    logging.getLogger("quant_phase1").addHandler(Phase3PersistObserver())
    service_ref = None
    class ReplayCollectorService(entrypoint.CollectorService):
        def __init__(self, *args, **kwargs):
            nonlocal service_ref
            super().__init__(*args, **kwargs)
            service_ref = self
            self.phase3_runner.connect_factory = v1.Phase3ReplayConnector(client)
            self.phase4_runtime._connect_factory = v1.Phase4ReplayConnector()
            if self.phase6_runtime is not None:
                self.phase6_runtime.now = lambda: fixed_now

        async def _persist_batch(self, batch):
            started = time.perf_counter()
            result = await super()._persist_batch(batch)
            if result and not (RESULTS / "collector-bootstrap.json").exists():
                _write_marker("collector-bootstrap.json", {
                    "selected_symbols": list(self.selected_symbols),
                    "selected_symbol_count": len(self.selected_symbols),
                    "persisted": True,
                    "persist_duration_seconds": round(time.perf_counter() - started, 6),
                })
            return result

        async def run(self):
            task = asyncio.create_task(super().run(), name="official-collector-run-service")
            # Preserve the bounded default Replay V2 run. Resource Replay V3
            # may set a longer external-supervision window so the host can
            # measure a fixed-duration Collector lifecycle before SIGTERM.
            max_lifetime = max(240, int(os.environ.get("REPLAY_COLLECTOR_MAX_LIFETIME_SECONDS", "240")))
            deadline = asyncio.get_running_loop().time() + max_lifetime
            final_persisted = False
            ready_written = False
            while not task.done() and asyncio.get_running_loop().time() < deadline:
                service = service_ref
                if service is not None and len(service.selected_symbols) == 200:
                    gap = service.gap_recovery
                    uta_sockets = v1.ReplayBitgetWebSocket.opened_sockets
                    uta_expected = sum(len(socket.events) for socket in uta_sockets)
                    uta_consumed = sum(socket.cursor for socket in uta_sockets)
                    phase3_expected = dict(sorted(client.phase3_event_totals.items()))
                    phase3_consumed = {
                        exchange: client.ws_cursor.get(f"phase3:{exchange}", 0)
                        for exchange in phase3_expected
                    }
                    drained = (
                        len(uta_sockets) == 5 and uta_consumed == uta_expected
                        and set(phase3_expected) == {"bitget", "bybit", "hyperliquid"}
                        and phase3_consumed == phase3_expected
                    )
                    recovered = gap is not None and gap.active_count == 0 and gap.pending_count == 0 and gap.inflight_count == 0
                    if drained and recovered and phase3_persisted and not final_persisted:
                        await service._persist_current_store()
                        final_persisted = True
                    phase6_started = service.phase6_task is not None
                    if drained and recovered and phase3_persisted and final_persisted and phase6_started and not ready_written:
                        _write_marker("collector-replay-ready.json", {
                            "dataset_event_total": client.input_event_count,
                            "ws_reset_count": client.fixed_ws_reset_count,
                            "uta_expected": uta_expected,
                            "uta_consumed": uta_consumed,
                            "phase3_expected": phase3_expected,
                            "phase3_consumed": phase3_consumed,
                            "selected_symbol_count": len(service.selected_symbols),
                            "rest_instruments": client.instrument_count,
                            "rest_tickers": client.ticker_count,
                            "rest_klines": client.kline_count,
                            "dropped_closed_bars": len(client.drop_keys),
                            "gap_candidates": gap.actual_gap_total,
                            "recovery_admissions": gap.admitted_count,
                            "recovery_completions": gap.completed_count,
                            "phase3_reconnects": {
                                exchange: service.phase3_runtime.health_snapshot(exchange).reconnect_count
                                for exchange in sorted(service.phase3_runtime.adapters)
                            },
                            "phase3_persistence_observed": phase3_persisted,
                            "phase4_running": service.phase4_runtime.running,
                            "phase6_task_started": phase6_started,
                            "paper_mode": settings.trading_mode,
                        })
                        ready_written = True
                await asyncio.sleep(0.02)
            if not task.done():
                raise TimeoutError("Collector deterministic replay did not reach its ready gate")
            await task
            service = service_ref
            _write_marker("collector-shutdown.json", {
                "phase3_running": service.phase3_runtime.running if service and service.phase3_runtime else None,
                "phase4_running": service.phase4_runtime.running if service and service.phase4_runtime else None,
                "remaining_async_tasks": sorted(task.get_name() for task in asyncio.all_tasks()
                                                 if task is not asyncio.current_task() and not task.done()),
            })

    entrypoint.CollectorService = ReplayCollectorService
    await entrypoint.run_service(settings, stop_event=stop_event)


async def _run_engine(settings: Settings, client) -> None:
    _patch_clock(client)
    _install_phase2_fixture()
    _install_engine_clock_and_observers(client, settings)
    import quant_phase1.entrypoints.engine as entrypoint

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, stop_event.set)
    loop.add_signal_handler(signal.SIGINT, stop_event.set)
    await entrypoint.run_service(settings, stop_event=stop_event)
    _write_marker("engine-shutdown.json", {
        "remaining_async_tasks": sorted(task.get_name() for task in asyncio.all_tasks()
                                         if task is not asyncio.current_task() and not task.done()),
    })


async def main(role: str) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not MANIFEST_PATH.exists() or not PHASE2_PATH.exists():
        raise RuntimeError("frozen Replay V2 manifest/fixture missing")
    dsn = os.environ["POSTGRES_DSN"]
    settings = _fixed_settings(dsn)
    manifest = _verify_manifest()
    client = _load_replay(settings)
    if client.input_event_count != manifest["phase1_cassette"]["event_total"]:
        raise RuntimeError("Phase1 replay record count differs from the frozen manifest")
    _patch_clock(client)
    if role == "collector":
        _write_marker("collector-start.json", {
            "dataset_sha256": manifest["dataset_sha256"],
            "input_event_total": client.input_event_count,
            "fault_counts": {"kline_drops": len(client.drop_keys), "ws_resets": client.fixed_ws_reset_count},
            "paper_mode": settings.trading_mode,
        })
        await _run_collector(settings, client)
    else:
        _write_marker("engine-start.json", {
            "dataset_sha256": manifest["dataset_sha256"],
            "input_event_total": client.input_event_count,
            "paper_mode": settings.trading_mode,
        })
        await _run_engine(settings, client)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("collector", "engine"), required=True)
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(main(arguments.role)))
