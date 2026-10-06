#!/usr/bin/env python3
"""Build the deterministic, secret-free Data Layer Replay V1 fixture."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_data_layer.backpressure import STREAM_CONTRACTS  # noqa: E402

FIXTURE_DIR = ROOT / "tests/fixtures/data_layer_replay_v1"
AS_OF = datetime(2026, 9, 26, tzinfo=timezone.utc)
EVENT_TIME = AS_OF - timedelta(minutes=2)
SEED = 20_260_926
BTC_BLOCK_HEIGHT = 968_443
BTC_BLOCK_EVENT_COUNT = 12_665


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _record(
    phase: int,
    stream_id: str,
    source_id: str,
    identity: str,
    *,
    fields: dict[str, dict[str, Any]] | None = None,
    payload: dict[str, Any] | None = None,
    status: str = "AVAILABLE",
    offset_seconds: int = 0,
) -> dict[str, Any]:
    event_time = EVENT_TIME + timedelta(seconds=offset_seconds)
    return {
        "phase": phase,
        "stream_id": stream_id,
        "source_id": source_id,
        "canonical_identity": identity,
        "event_timestamp_utc": _timestamp(event_time),
        "source_timestamp_utc": _timestamp(event_time),
        "fetched_at_utc": _timestamp(event_time + timedelta(seconds=1)),
        "processed_at_utc": _timestamp(event_time + timedelta(seconds=2)),
        "provenance": "SYNTHETIC_FIXTURE",
        "status": status,
        "fields": fields or {
            "fixture_value": {
                "value": "1",
                "status": "AVAILABLE",
                "provenance": "SYNTHETIC_FIXTURE",
                "source_field": "fixture.value",
                "unit_status": "UNCONFIRMED",
            }
        },
        "payload": {"synthetic_test_fixture": True, **(payload or {})},
    }


def _available(value: Any, *, source_field: str, unit_status: str = "NOT_APPLICABLE") -> dict[str, Any]:
    return {
        "value": value,
        "status": "AVAILABLE",
        "provenance": "SYNTHETIC_FIXTURE",
        "source_field": source_field,
        "unit_status": unit_status,
    }


def _missing(source_field: str, *, unit_status: str = "UNCONFIRMED") -> dict[str, Any]:
    return {
        "value": None,
        "status": "NOT_AVAILABLE",
        "provenance": "NOT_AVAILABLE",
        "source_field": source_field,
        "unit_status": unit_status,
    }


def build_records() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    contracts = [item for item in STREAM_CONTRACTS if item.phase is not None]
    for ordinal, contract in enumerate(contracts):
        phase = int(contract.phase.value.removeprefix("phase"))
        records.append(_record(
            phase,
            contract.stream_id,
            contract.source_id.value,
            f"fixture:{contract.stream_id}:seed",
            payload={"stream_contract_sample": True},
            offset_seconds=ordinal,
        ))

    records.extend((
        _record(1, "phase1.closed_klines", "phase1.market_data", "BTCUSDT:5m:2026-09-25T23:55Z",
                fields={"close": _available("67123.45", source_field="close", unit_status="USDT")},
                payload={"symbol": "BTCUSDT", "timeframe": "5m", "closed": True}),
        _record(1, "phase1.stage1_results", "engine.stage1", "BTCUSDT:stage1:2026-09-25T23:55Z",
                fields={"eligible": _available(False, source_field="eligible")},
                payload={"symbol": "BTCUSDT", "decision": "INELIGIBLE", "context_only": True}),
        _record(2, "phase2.open_interest", "phase2.derivatives", "BTCUSDT:oi:2026-09-25T23:57Z",
                fields={"open_interest": _missing("openInterest", unit_status="UNCONFIRMED")},
                payload={"symbol": "BTCUSDT", "raw_unit": "UNCONFIRMED"}),
        _record(2, "phase2.funding_rates", "phase2.derivatives", "ETHUSDT:funding:2026-09-25T23:57Z",
                fields={"funding_rate": _available("0", source_field="fundingRate", unit_status="RATE")},
                payload={"symbol": "ETHUSDT", "explicit_zero": True}),
        _record(3, "phase3.bitget_trades", "phase3.bitget_trades", "bitget:trade:fixture-1",
                payload={"symbol": "BTCUSDT", "trade_id": "fixture-1", "side": "buy", "price": "67123.45", "size": "0.01"}),
        _record(3, "phase3.flow_windows", "phase3.flow_processing", "BTCUSDT:flow:2026-09-25T23:55Z",
                fields={"cvd": _available("0.01", source_field="delta", unit_status="BTC")},
                payload={"symbol": "BTCUSDT", "window_seconds": 300, "status": "AVAILABLE"}),
        _record(4, "phase4.liquidation_events", "phase4.liquidation", "bitget:liquidation:fixture-1",
                payload={"symbol": "BTCUSDT", "side": "sell", "notional": "1000"}),
        _record(4, "phase4.long_short_basis", "phase4.liquidation", "BTCUSDT:long-short:2026-09-25T23:57Z",
                fields={"long_short_ratio": _missing("longShortRatio", unit_status="RATIO")},
                payload={"symbol": "BTCUSDT"}),
        _record(5, "phase5.market_regime", "phase5.context", "market-regime:2026-09-25T23:57Z",
                fields={"regime": _available("NEUTRAL", source_field="derived_regime")},
                payload={"context_only": True}),
        _record(6, "phase6.news_feed", "phase6.external_context", "fixture:news:1",
                payload={"source_kind": "news", "title": "synthetic event"}),
        _record(6, "phase6.macro_feed", "phase6.external_context", "fixture:macro:1",
                payload={"source_kind": "macro", "indicator": "CPI"}),
        _record(6, "phase6.unlock_feed", "phase6.external_context", "fixture:unlock:1",
                payload={"source_kind": "unlock", "asset": "BTC"}),
        _record(6, "phase6.ai_analyses", "phase6.ai", "fixture:ai:not-configured",
                fields={"analysis": _missing("ai_provider", unit_status="NOT_APPLICABLE")},
                payload={"provider_state": "NOT_CONFIGURED"}, status="NOT_AVAILABLE"),
        _record(7, "phase7.bitcoin_blocks", "phase7.onchain", f"bitcoin:main:block:{BTC_BLOCK_HEIGHT}",
                payload={"chain": "BITCOIN", "height": BTC_BLOCK_HEIGHT, "event_count": BTC_BLOCK_EVENT_COUNT}),
        _record(7, "phase7.ethereum_blocks", "phase7.onchain", "ethereum:main:block:20000119",
                payload={"chain": "ETHEREUM", "height": 20000119, "logs": 1, "receipts": 1}),
        _record(7, "phase7.onchain_events", "phase7.onchain", "ethereum:main:fixture-tx:log:0",
                payload={"chain": "ETHEREUM", "event_index_kind": "LOG_INDEX", "receipt_status": "0x1"}),
        _record(7, "phase7.spot_trades", "phase7.spot", "binance:BTCUSDT:aggTrade:1",
                payload={"exchange": "BINANCE", "symbol": "BTCUSDT", "cursor": 1}),
        _record(8, "phase8.option_instruments", "phase8.options_market", "BTC-27NOV26-100000-C",
                payload={"underlying": "BTC", "option_type": "call", "expiry": "2026-11-27", "strike": "100000"}),
        _record(8, "phase8.option_instruments", "phase8.options_market", "BTC-27NOV26-100000-P",
                payload={"underlying": "BTC", "option_type": "put", "expiry": "2026-11-27", "strike": "100000"}),
        _record(8, "phase8.option_instruments", "phase8.options_market", "ETH-27NOV26-4000-C",
                payload={"underlying": "ETH", "option_type": "call", "expiry": "2026-11-27", "strike": "4000"}),
        _record(8, "phase8.option_instruments", "phase8.options_market", "ETH-27NOV26-4000-P",
                payload={"underlying": "ETH", "option_type": "put", "expiry": "2026-11-27", "strike": "4000"}),
        _record(8, "phase8.markprice_latest", "phase8.options_market", "BTC:markprice:fixture:1",
                fields={"iv": _missing("mark_iv", unit_status="UNCONFIRMED")},
                payload={"underlying": "BTC", "source_field": "mark_iv"}),
        _record(8, "phase8.bounded_ticker_state", "phase8.options_market", "BTC-27NOV26-100000-C:ticker:snapshot",
                fields={"delta": _available("0.502", source_field="delta", unit_status="SOURCE_PROVIDED")},
                payload={"message_kind": "snapshot", "instrument": "BTC-27NOV26-100000-C"}),
        _record(8, "phase8.bounded_ticker_state", "phase8.options_market", "BTC-27NOV26-100000-C:ticker:sparse:2",
                fields={"bid_iv": _missing("bid_iv", unit_status="UNCONFIRMED")},
                payload={"message_kind": "sparse_update", "present_fields": ["mark_price"], "must_preserve_unmentioned": True}),
        _record(8, "phase8.options_context", "phase8.options_context", "BTC:options-context:2026-09-25T23:57Z",
                fields={"atm_iv": _missing("iv", unit_status="UNCONFIRMED")},
                payload={"underlying": "BTC", "status": "NOT_AVAILABLE", "context_only": True}),
    ))

    # This realistic event-cardinality fixture exercises bounded Bitcoin block
    # chunking without carrying verbose RPC payloads into the archive.
    for index in range(BTC_BLOCK_EVENT_COUNT):
        txid = hashlib.sha256(f"data-layer-replay-v1:btc:{BTC_BLOCK_HEIGHT}:{index}".encode()).hexdigest()
        records.append(_record(
            7,
            "phase7.onchain_events",
            "phase7.onchain",
            f"bitcoin:main:{BTC_BLOCK_HEIGHT}:{txid}:vout:0",
            fields={"amount_raw": _available(str(1_000 + index), source_field="vout.value", unit_status="SATOSHI")},
            payload={"chain": "BITCOIN", "block_height": BTC_BLOCK_HEIGHT, "tx_index": index, "event_index": 0,
                     "event_index_kind": "OUTPUT_INDEX", "txid": txid},
            offset_seconds=index % 30,
        ))
    return sorted(records, key=lambda row: (row["phase"], row["stream_id"], row["canonical_identity"]))


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("utf-8")


def _write_manifest(args: argparse.Namespace, raw: bytes, compressed: bytes, record_count: int) -> dict[str, Any]:
    if not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", args.git_sha):
        raise SystemExit("--git-sha must be a full hexadecimal commit SHA")
    image_digest = re.compile(r"sha256:[a-f0-9]{64}")
    for name in ("collector_image_digest", "engine_image_digest", "postgres_image_digest"):
        if not image_digest.fullmatch(getattr(args, name)):
            raise SystemExit(f"--{name.replace('_', '-')} must be a pinned sha256 digest")
    config = {
        "trading_mode": "paper",
        "phases": list(range(1, 9)),
        "phase6_ai_provider": "NOT_CONFIGURED",
        "phase8_retention_enforcement": False,
        "provider_network": "DISABLED_FOR_REPLAY",
        "seed": SEED,
    }
    component_paths = [
        "tests/phase6_replay_v2_runner.py",
        "tests/phase6_loaded_replay_runner.py",
        "tests/phase7_resource_replay_v3_runner.py",
        "scripts/phase7_collector_memory_profile.py",
        "tests/test_phase8_replay.py",
        "tests/test_data_layer_replay.py",
        "tests/test_data_layer_replay_runner.py",
        "tests/data_layer_replay_v1_db_runner.py",
        "tests/fixtures/replays/phase6-replay-v2-manifest.json",
        "tests/fixtures/replays/phase6-replay-v2-phase2.jsonl",
        "tests/fixtures/replays/phase6-bitget-rest-20260924.jsonl.gz",
        "tests/fixtures/phase8/manifest.json",
        "src/quant_data_layer/backpressure.py",
        "scripts/build_data_layer_replay_v1_fixture.py",
        "scripts/run_data_layer_replay_v1.py",
    ]
    phase8_manifest = json.loads((ROOT / "tests/fixtures/phase8/manifest.json").read_text(encoding="utf-8"))
    component_paths.extend(f"tests/fixtures/phase8/{name}" for name in phase8_manifest["fixtures"])
    components = {relative: _sha(ROOT / relative) for relative in sorted(set(component_paths))}
    return {
        "replay_version": "DATA_LAYER_REPLAY_V1",
        "git_sha": args.git_sha,
        "collector_image_digest": args.collector_image_digest,
        "engine_image_digest": args.engine_image_digest,
        "postgres_image_digest": args.postgres_image_digest,
        "postgres_server_version": "16.15",
        "migration_version": 15,
        "config_sha256": hashlib.sha256(_canonical_json(config)).hexdigest(),
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "seed": SEED,
        "as_of_utc": _timestamp(AS_OF),
        "fixture": {
            "filename": "records.jsonl.gz",
            "compressed_sha256": hashlib.sha256(compressed).hexdigest(),
            "compressed_bytes": len(compressed),
            "uncompressed_bytes": len(raw),
            "record_count": record_count,
        },
        "required_phases": list(range(1, 9)),
        "registered_stream_count": len(contracts_with_phase()),
        "replay_components_sha256": components,
        "fixture_origin": "synthetic_test_data_only",
        "external_provider_calls": False,
        "private_api_calls": False,
        "trading_enabled": False,
    }


def contracts_with_phase():
    return tuple(item for item in STREAM_CONTRACTS if item.phase is not None)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--git-sha")
    parser.add_argument("--collector-image-digest")
    parser.add_argument("--engine-image-digest")
    parser.add_argument("--postgres-image-digest")
    args = parser.parse_args()

    records = build_records()
    raw = b"".join(_canonical_json(record) for record in records)
    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    (FIXTURE_DIR / "records.jsonl.gz").write_bytes(compressed)
    result = {
        "record_count": len(records),
        "uncompressed_bytes": len(raw),
        "compressed_bytes": len(compressed),
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "compressed_sha256": hashlib.sha256(compressed).hexdigest(),
        "phase_counts": {
            str(phase): sum(record["phase"] == phase for record in records)
            for phase in range(1, 9)
        },
        "bitcoin_block_event_count": sum(
            record["phase"] == 7 and record["payload"].get("block_height") == BTC_BLOCK_HEIGHT
            for record in records
        ),
    }
    if all((args.git_sha, args.collector_image_digest, args.engine_image_digest, args.postgres_image_digest)):
        manifest = _write_manifest(args, raw, compressed, len(records))
        (FIXTURE_DIR / "manifest.json").write_bytes(_canonical_json(manifest))
        result["manifest_sha256"] = hashlib.sha256(_canonical_json(manifest)).hexdigest()
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
