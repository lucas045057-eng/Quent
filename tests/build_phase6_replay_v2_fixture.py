"""Build a frozen, test-only Phase 2 source-shaped fixture for Replay V2."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import timedelta
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
PHASE1_CASSETTE = TESTS / "fixtures/replays/phase6-bitget-rest-20260924.jsonl.gz"
PHASE2_FIXTURE = TESTS / "fixtures/replays/phase6-replay-v2-phase2.jsonl"
MANIFEST = TESTS / "fixtures/replays/phase6-replay-v2-manifest.json"

spec = importlib.util.spec_from_file_location(
    "phase6_loaded_replay_runner", TESTS / "phase6_loaded_replay_runner.py"
)
if spec is None or spec.loader is None:
    raise RuntimeError("could not import the existing Replay V1 harness")
v1 = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = v1
spec.loader.exec_module(v1)

from quant_phase1.config import Settings  # noqa: E402
from quant_phase1.pipeline import MarketDataCollector  # noqa: E402


def _dump(row: dict) -> str:
    return json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


async def _selected_symbols() -> tuple[tuple[str, ...], str]:
    v1.FIXTURE = PHASE1_CASSETTE
    settings = Settings.from_env({
        "TRADING_MODE": "paper",
        "UNIVERSE_LIMIT": "200",
        "KLINE_FETCH_LIMIT": "100",
        "PHASE2_ENABLED": "1",
        "PHASE3_ENABLED": "1",
        "PHASE4_ENABLED": "1",
        "PHASE5_ENABLED": "1",
        "PHASE6_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_ENABLED": "0",
        "PHASE7_ETHEREUM_RPC_ENABLED": "0",
    })
    client = v1.ReplayRestClient(settings)
    v1._REPLAY_CLIENT = client
    batch = await MarketDataCollector(
        client, max_symbols=settings.universe_limit,
        kline_limit=settings.kline_fetch_limit,
    ).collect_once()
    symbols = tuple(batch.selected_symbols)
    if len(symbols) != settings.universe_limit:
        raise RuntimeError(f"expected 200 selected symbols, got {len(symbols)}")
    return symbols, client.clock.isoformat().replace("+00:00", "Z")


def _build_records(symbols: tuple[str, ...], timestamp: str) -> list[dict]:
    epoch_ms = int((v1._dt(timestamp) + timedelta(seconds=1)).timestamp() * 1000)
    base_assets = tuple(symbol.removesuffix("USDT") for symbol in symbols)
    records: list[dict] = []

    def add(event_type: str, exchange: str, symbol: str, payload: object) -> None:
        records.append({
            "dataset_version": "phase6-replay-v2-phase2-1",
            "event_id": f"p2:{exchange}:{event_type}:{symbol}",
            "phase": "phase2",
            "event_type": event_type,
            "exchange": exchange,
            "symbol": symbol,
            "exchange_timestamp": timestamp,
            "fetched_at": timestamp,
            "payload": payload,
            "synthetic_test_fixture": True,
        })

    bitget_instruments = []
    bybit_instruments = []
    hl_universe = []
    hl_contexts = []
    for index, (symbol, base) in enumerate(zip(symbols, base_assets, strict=True), start=1):
        # Prices/OI are deterministic test values, explicitly not observations
        # from an exchange. Funding is neutral (zero), with no directional label.
        mark = str(100 + index)
        oi = str(10 + index)
        bitget_instruments.append({
            "symbol": symbol, "baseCoin": base, "quoteCoin": "USDT",
            "settleCoin": "USDT", "marginCoin": "USDT",
            "contractSize": "1", "sizeMultiplier": "1", "fundInterval": "8",
            "status": "normal", "category": "USDT-FUTURES",
            "fixtureOnly": True,
        })
        bybit_instruments.append({
            "symbol": symbol, "contractType": "LinearPerpetual",
            "baseCoin": base, "quoteCoin": "USDT", "settleCoin": "USDT",
            "fundingInterval": "480", "status": "Trading",
            "fixtureOnly": True,
        })
        ticker = {
            "symbol": symbol, "ts": str(epoch_ms), "openInterest": oi,
            "markPrice": mark, "fundingRate": "0", "fundingInterval": 480,
            "nextFundingTime": str(epoch_ms + 28_800_000), "fixtureOnly": True,
        }
        add("ticker_payload", "bitget", symbol, ticker)
        add("current_funding_payload", "bitget", symbol, {
            "symbol": symbol, "fundingRate": "0", "fundingRateInterval": "8",
            "nextUpdate": str(epoch_ms + 28_800_000), "fixtureOnly": True,
        })
        add("ticker_payload", "bybit", symbol, {
            "retCode": 0, "time": epoch_ms,
            "result": {"category": "linear", "list": [{
                "symbol": symbol, "openInterest": oi, "markPrice": mark,
                "fundingRate": "0", "nextFundingTime": str(epoch_ms + 28_800_000),
                "fixtureOnly": True,
            }]},
        })
        hl_universe.append({"name": base, "szDecimals": 4, "fixtureOnly": True})
        hl_contexts.append({
            "markPx": mark, "openInterest": oi, "funding": "0",
            "fixtureOnly": True,
        })

    add("instrument_payload", "bitget", "*", {
        "code": "00000", "data": bitget_instruments, "fixtureOnly": True,
    })
    add("instrument_payload", "bybit", "*", {
        "retCode": 0, "result": {"category": "linear", "list": bybit_instruments},
        "fixtureOnly": True,
    })
    add("meta_and_asset_context_payload", "hyperliquid", "*", [
        {"universe": hl_universe}, hl_contexts,
    ])
    records.sort(key=lambda row: row["event_id"])
    return records


def main() -> None:
    symbols, timestamp = asyncio.run(_selected_symbols())
    records = _build_records(symbols, timestamp)
    PHASE2_FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    fixture_bytes = ("\n".join(map(_dump, records)) + "\n").encode()
    PHASE2_FIXTURE.write_bytes(fixture_bytes)

    phase1_counts: Counter[str] = Counter()
    phase1_sources: Counter[str] = Counter()
    phase1_symbols: Counter[str] = Counter()
    phase1_events = 0
    phase1_times: list[str] = []
    phase1_faults: list[dict] = []
    receive_orders: list[int] = []
    with gzip.open(PHASE1_CASSETTE, "rt", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            phase1_events += 1
            phase1_counts[row["event_type"]] += 1
            phase1_sources[row["source"]] += 1
            phase1_symbols[str(row.get("symbol") or "*")] += 1
            phase1_times.append(str(row.get("event_timestamp") or ""))
            if isinstance(row.get("receive_order"), int):
                receive_orders.append(row["receive_order"])
            if row.get("fault_event"):
                phase1_faults.append({
                    "receive_order": row.get("receive_order"),
                    "fault_event": row["fault_event"],
                    "source": row.get("source"),
                    "symbol": row.get("symbol"),
                    "interval": row.get("interval"),
                })

    counts_by_type = Counter(row["event_type"] for row in records)
    per_symbol: dict[str, dict[str, int]] = {
        symbol: {"source_payloads": 3, "open_interest_observations": 3,
                 "funding_observations": 4}
        for symbol in symbols
    }
    phase1_bytes = PHASE1_CASSETTE.read_bytes()
    digest = hashlib.sha256(phase1_bytes + b"\0" + fixture_bytes).hexdigest()
    manifest = {
        "dataset_version": "phase6-integrated-replay-v2-1",
        "dataset_sha256": digest,
        "phase1_cassette": {
            "path": "tests/fixtures/replays/phase6-bitget-rest-20260924.jsonl.gz",
            "sha256": hashlib.sha256(phase1_bytes).hexdigest(),
            "event_total": phase1_events,
            "event_type_counts": dict(sorted(phase1_counts.items())),
            "source_counts": dict(sorted(phase1_sources.items())),
            "event_counts_by_symbol": dict(sorted(phase1_symbols.items())),
            "event_timestamp_min": min(value for value in phase1_times if value),
            "event_timestamp_max": max(value for value in phase1_times if value),
            "receive_order_min": min(receive_orders),
            "receive_order_max": max(receive_orders),
            "fault_sequence": phase1_faults,
        },
        "phase2_fixture": {
            "path": "tests/fixtures/replays/phase6-replay-v2-phase2.jsonl",
            "sha256": hashlib.sha256(fixture_bytes).hexdigest(),
            "event_total": len(records),
            "event_type_counts": dict(sorted(counts_by_type.items())),
            "selected_symbol_count": len(symbols),
            "selected_symbols": list(symbols),
            "selected_symbols_sha256": hashlib.sha256("\n".join(symbols).encode()).hexdigest(),
            "fixed_utc_timestamp": timestamp,
            "source_payloads_by_exchange": {
                exchange: sum(row["exchange"] == exchange for row in records)
                for exchange in ("bitget", "bybit", "hyperliquid")
            },
            "expected_adapter_outputs": {
                "instrument_metadata": 600,
                "open_interest_observations": 600,
                "funding_observations_before_idempotent_key_collapse": 800,
                "funding_logical_unique_keys_by_source_endpoint": 800,
                "cross_exchange_snapshots": 200,
            },
            "values_are_synthetic_test_only": True,
            "funding_semantics": "zero-rate neutral test value; no direction asserted",
            "bitget_open_interest_semantics": "raw ticker value retained; unit unconfirmed; no normalized OI asserted",
            "events_by_symbol": per_symbol,
        },
        "runtime_clock": {
            "exchange_event_range": "from phase1 cassette; Phase2 uses fixed_utc_timestamp",
            "receive_order": "immutable source receive_order in Phase1 cassette; Phase2 events sorted by event_id",
        },
        "event_totals_by_phase": {"phase1": phase1_events, "phase2": len(records)},
        "fault_sequence": {
            "phase1_kline_drops": 20,
            "phase1_ws_resets": 15,
            "phase1_reset_order": ["bitget-uta-v3-public", "bybit-v5-public", "hyperliquid-public"] * 5,
            "phase2_faults": 0,
        },
        "phase6_sources": {
            "news": "NOT_CONFIGURED", "macro": "NOT_CONFIGURED",
            "unlock": "NOT_CONFIGURED", "ai_provider": "NOT_CONFIGURED",
        },
        "phase3_runtime_paths": {
            "public_trade_ingestion": "ENABLED_AND_REPLAYED",
            "orderbook_ingestion": "NOT_PRESENT_IN_ENABLED_PHASE3_RUNTIME; intentionally no synthetic fixture",
        },
        "expected_logical_rows": {
            "phase2_open_interest": 600,
            "phase2_funding": 800,
            "phase2_cross_exchange_derivative_snapshots": 200,
            "phase2_instruments": 600,
            "phase6_news_macro_unlock_observations": 0,
            "phase6_ai_provider_executions": 0,
            "per_three_cycle_resource_arm": {
                "symbols": 805,
                "exchange_instruments": 600,
                "market_snapshots": 825,
                "market_observations": 2475,
                "klines": 79136,
                "open_interest": 600,
                "funding_rates": 800,
                "cross_exchange_derivative_snapshots": 200,
                "screening_results": 600,
                "screening_runs": 3,
                "universe_runs": 1,
                "universe_members": 200,
                "stage1_derivative_enrichment": 600,
                "stage1_flow_enrichment": 600,
                "trade_flow_windows": 50,
                "cross_exchange_flow_snapshots": 8,
                "stage1_phase4_enrichment": 600,
                "liquidation_events": 0,
                "long_short_observations": 0,
                "basis_snapshots": 0,
                "stage1_phase5_context_enrichment": 600,
                "phase5_market_leader_context": 8,
                "phase5_market_regime_snapshots": 4,
                "phase5_relative_strength_snapshots": 1800,
                "phase5_sector_context_snapshots": 6,
                "phase5_sector_membership": 200,
                "runtime_health_events": 3,
                "phase6_source_registry": 0,
                "phase6_news_events": 0,
                "phase6_macro_events": 0,
                "phase6_unlock_events": 0,
                "phase6_ai_analyses": 0,
                "phase6_ai_extractions": 0,
                "phase6_ai_usage": 0,
                "phase7_onchain_transfer_events": 0,
                "phase7_onchain_flow_windows": 0,
            },
        },
        "engine": {
            "entrypoint": "quant_phase1.entrypoints.engine.run_service",
            "minimum_lifecycle_evidence": ["bootstrap", "one Phase2 cycle", "Phase5 context", "Phase6 ACTIVE health"],
            "engine_scheduler_interval_seconds": 300,
            "engine_process_restarts_per_resource_arm": 3,
            "phase2_cycles_per_engine_process": 1,
            "phase2_cycles_per_resource_arm": 3,
        },
        "resource_replay": {
            "collector_engine_restart_cycles_per_resource_arm": 3,
            "postgres_seed": "fresh empty disposable database per arm",
        },
        "collector": {
            "entrypoint": "quant_phase1.entrypoints.collector.run_service",
            "paper_only": True,
            "universe_limit": 200,
        },
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "manifest": str(MANIFEST),
        "phase2_fixture": str(PHASE2_FIXTURE),
        "dataset_sha256": digest,
        "phase1_events": phase1_events,
        "phase2_events": len(records),
        "selected_symbols": len(symbols),
        "phase2_event_types": dict(sorted(counts_by_type.items())),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
