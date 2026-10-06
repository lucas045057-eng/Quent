"""Bounded, credential-safe Phase 7 JSON-RPC response-shape probe."""

from __future__ import annotations

import asyncio
import argparse
import json
import re
import time
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiohttp

from quant_phase1.config import Settings
from quant_phase7.bitcoin import BitcoinBlockParser
from quant_phase7.contracts import Chain, EventIndexKind
from quant_phase7.runtime import Phase7JsonRpcClient, Phase7RpcError, _read_bounded_body
from quant_phase7.spot import BinanceSpotAdapter


def _safe_content_type(headers) -> str:
    value = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    return value if value in {"application/json", "text/plain", "text/html", "application/octet-stream"} else "OTHER"


def _safe_content_length(headers) -> int | None:
    value = headers.get("Content-Length", "").strip()
    if not value.isdigit() or len(value) > 20:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _local_environment() -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = Path(".env.local").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return result
    for line in lines:
        item = line.strip()
        if not item or item.startswith("#"):
            continue
        if item.startswith("export "):
            item = item[7:].lstrip()
        key, separator, value = item.partition("=")
        if not separator:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        result[key.strip()] = value
    return result


class _BodyTap:
    def __init__(self, response: Any, record: dict[str, Any]) -> None:
        self.response = response
        self.record = record

    async def read(self, limit: int) -> bytes:
        body = await self.response.content.read(limit)
        self._record_body(body)
        return body

    async def iter_chunked(self, chunk_size: int):
        body = bytearray()
        try:
            async for chunk in self.response.content.iter_chunked(chunk_size):
                body.extend(chunk)
                yield chunk
        finally:
            self._record_body(bytes(body))

    def _record_body(self, body: bytes) -> None:
        stripped = body.lstrip()
        if not body:
            body_type = "EMPTY_RESPONSE"
        else:
            try:
                json.loads(body)
                body_type = "JSON"
            except (UnicodeDecodeError, json.JSONDecodeError):
                body_type = (
                    "HTML_ERROR_PAGE"
                    if stripped.startswith((b"<html", b"<!DOCTYPE"))
                    else "PLAIN_TEXT_ERROR"
                )
        self.record.update({
            "http": self.response.status,
            "content_type": _safe_content_type(self.response.headers),
            "bytes": len(body),
            "body_type": body_type,
        })


class _ResponseTap:
    def __init__(self, response: Any, record: dict[str, Any]) -> None:
        self.response = response
        self.record = record

    async def __aenter__(self):
        self.started = time.monotonic()
        self.entered = await self.response.__aenter__()
        self.record.update({
            "http": self.entered.status,
            "content_type": _safe_content_type(self.entered.headers),
            "content_length": _safe_content_length(self.entered.headers),
        })
        return self

    async def __aexit__(self, *args):
        result = await self.response.__aexit__(*args)
        self.record["latency_ms"] = round((time.monotonic() - self.started) * 1000, 1)
        return result

    @property
    def status(self):
        return self.entered.status

    @property
    def headers(self):
        return self.entered.headers

    @property
    def content(self):
        return _BodyTap(self.entered, self.record)


class _SessionTap:
    def __init__(self, session: aiohttp.ClientSession, records: list[dict[str, Any]]) -> None:
        self.session = session
        self.records = records
        self.current_probe = 0

    def post(self, *args, **kwargs):
        record: dict[str, Any] = {"attempt": len(self.records) + 1, "probe_id": self.current_probe}
        self.records.append(record)
        return _ResponseTap(self.session.post(*args, **kwargs), record)


def _safe_result(
    source_id: str, method: str, result: Any, params: list[Any]
) -> dict[str, Any]:
    if isinstance(result, dict):
        summary: dict[str, Any] = {
            "result_type": "object",
            "field_count": len(result),
        }
        if source_id == "bitcoin_rpc" and method == "getblockchaininfo":
            summary["chain"] = result.get("chain") if result.get("chain") in {
                "main", "test", "regtest", "signet",
            } else "OTHER"
            summary["height"] = result.get("blocks") if isinstance(result.get("blocks"), int) else None
        if method == "eth_getBlockByNumber":
            summary["has_number"] = isinstance(result.get("number"), str)
            summary["has_hash"] = isinstance(result.get("hash"), str)
            summary["transaction_count"] = (
                len(result["transactions"]) if isinstance(result.get("transactions"), list) else None
            )
        if method == "getblock":
            summary["response_shape"] = "OBJECT"
            transactions = result.get("tx")
            summary["transaction_count"] = len(transactions) if isinstance(transactions, list) else None
            summary["transaction_item_type"] = (
                "object" if transactions and isinstance(transactions[0], dict)
                else "string" if transactions and isinstance(transactions[0], str)
                else "empty_or_invalid"
            )
            summary["requested_verbosity"] = params[1] if len(params) > 1 else None
        return summary
    if isinstance(result, str):
        if method == "getblock":
            return {
                "result_type": "string", "response_shape": "HEX_STRING",
                "length": len(result), "requested_verbosity": params[1] if len(params) > 1 else None,
            }
        if method == "eth_chainId":
            return {
                "result_type": "string",
                "chain_id": "ETHEREUM_MAINNET" if result == "0x1" else "OTHER",
            }
        return {
            "result_type": "string",
            "length": len(result),
            "hash64": len(result.removeprefix("0x")) == 64,
            "height": int(result, 16) if method == "eth_blockNumber" and result.startswith("0x") else None,
        }
    return {
        "result_type": type(result).__name__,
        "value": result if isinstance(result, (int, bool)) else None,
    }


def _bitcoin_parser_report(block: dict[str, Any], expected_hash: str) -> dict[str, Any]:
    """Run the production parser and return only bounded, non-secret evidence."""
    observed_at = datetime.now(timezone.utc)
    try:
        events = BitcoinBlockParser().parse_block(
            block,
            observed_at=observed_at,
            fetched_at=observed_at,
            processed_at=observed_at,
            expected_block_hash=expected_hash,
        )
        transactions = block.get("tx", [])
        coinbase_tx = next((
            tx for tx in transactions
            if isinstance(tx, dict)
            and isinstance(tx.get("vin"), list)
            and any(isinstance(item, dict) and "coinbase" in item for item in tx["vin"])
        ), None)
        normal_tx = next((
            tx for tx in transactions
            if isinstance(tx, dict)
            and isinstance(tx.get("vin"), list)
            and not any(isinstance(item, dict) and "coinbase" in item for item in tx["vin"])
        ), None)
        sample_event = next((
            event for event in events
            if normal_tx is not None and event.identity.tx_hash == normal_tx.get("txid")
        ), events[0] if events else None)
        sample_transaction = next((
            tx for tx in transactions
            if isinstance(tx, dict)
            and sample_event is not None
            and tx.get("txid") == sample_event.identity.tx_hash
        ), None)
        sample_output = None
        exact_satoshi_match = None
        raw_value_btc = None
        if sample_event is not None and sample_transaction is not None:
            sample_output = next((
                output for output in sample_transaction.get("vout", [])
                if isinstance(output, dict) and output.get("n") == sample_event.identity.event_index
            ), None)
            if sample_output is not None and "value" in sample_output:
                raw_value_btc = str(sample_output["value"])
                exact_satoshi_match = (
                    Decimal(str(sample_output["value"])) * Decimal(100_000_000)
                    == Decimal(sample_event.amount.amount_raw)
                )
        block_time = block.get("time")
        block_hash = block.get("hash")
        identity = sample_event.identity if sample_event is not None else None
        provenance = sample_event.provenance if sample_event is not None else None
        hash_valid = (
            isinstance(block_hash, str)
            and re.fullmatch(r"[0-9a-f]{64}", block_hash) is not None
            and block_hash == expected_hash
        )
        height = block.get("height")
        height_valid = isinstance(height, int) and not isinstance(height, bool) and height >= 0
        transaction_identity_valid = bool(
            sample_event is not None
            and sample_transaction is not None
            and sample_output is not None
            and identity.chain is Chain.BITCOIN
            and identity.event_index_kind is EventIndexKind.VOUT_INDEX
            and identity.tx_hash == sample_transaction.get("txid")
            and identity.block_hash == block_hash
            and identity.event_index == sample_output.get("n")
            and sample_event.event_id == (
                f"btc:{block_hash}:{sample_transaction.get('txid')}:{sample_output.get('n')}"
            )
        )
        satoshi_valid = exact_satoshi_match is True
        provenance_valid = bool(
            provenance is not None
            and provenance.source == "bitcoin-core"
            and provenance.source_hash == block_hash
            and provenance.observed_at <= provenance.fetched_at <= provenance.processed_at
            and all(
                timestamp.tzinfo is not None and timestamp.utcoffset().total_seconds() == 0
                for timestamp in (
                    provenance.observed_at, provenance.fetched_at, provenance.processed_at,
                )
            )
        )
        parser_pass = all((
            hash_valid,
            height_valid,
            transaction_identity_valid,
            satoshi_valid,
            provenance_valid,
        ))
        return {
            "status": "PASS" if parser_pass else "FAIL",
            "BITCOIN_PARSER_PASS": parser_pass,
            "hash_valid": hash_valid,
            "height_valid": height_valid,
            "transaction_identity_valid": transaction_identity_valid,
            "satoshi_valid": satoshi_valid,
            "provenance_valid": provenance_valid,
            "block_height": block.get("height"),
            "block_hash": block_hash,
            "requested_hash_matches": block_hash == expected_hash,
            "previous_block_hash": block.get("previousblockhash"),
            "block_time_utc": datetime.fromtimestamp(block_time, timezone.utc).isoformat(),
            "transaction_count": len(transactions),
            "coinbase_txid": coinbase_tx.get("txid") if coinbase_tx else None,
            "coinbase_vin_count": len(coinbase_tx.get("vin", [])) if coinbase_tx else 0,
            "coinbase_vout_count": len(coinbase_tx.get("vout", [])) if coinbase_tx else 0,
            "normal_txid": normal_tx.get("txid") if normal_tx else None,
            "normal_vin_count": len(normal_tx.get("vin", [])) if normal_tx else 0,
            "normal_vout_count": len(normal_tx.get("vout", [])) if normal_tx else 0,
            "sample_event_id": sample_event.event_id if sample_event else None,
            "sample_event_txid_matches": bool(
                sample_event and sample_transaction
                and sample_event.identity.tx_hash == sample_transaction.get("txid")
            ),
            "sample_output_index": sample_event.identity.event_index if sample_event else None,
            "sample_raw_value_btc": raw_value_btc,
            "sample_amount_raw_satoshis": sample_event.amount.amount_raw if sample_event else None,
            "sample_value_matches_satoshis": exact_satoshi_match,
            "provenance_source": sample_event.provenance.source if sample_event else None,
            "provenance_hash_matches_block": bool(
                sample_event and sample_event.provenance.source_hash == block.get("hash")
            ),
            "valuation_status": sample_event.valuation.status.value if sample_event else None,
        }
    except Exception as exc:  # noqa: BLE001 - do not expose provider payloads
        return {
            "status": "FAIL",
            "BITCOIN_PARSER_PASS": False,
            "hash_valid": False,
            "height_valid": False,
            "transaction_identity_valid": False,
            "satoshi_valid": False,
            "provenance_valid": False,
            "error_type": type(exc).__name__,
        }


async def _call(client: Phase7JsonRpcClient, source_id: str, method: str, params: list[Any], records):
    client.session.current_probe += 1
    probe_id = client.session.current_probe
    before = len(records)
    started = time.monotonic()
    try:
        result = await client.call(method, params)
        outcome = {
            "status": "PASS", "json_parse": True, "jsonrpc_envelope": "VALID",
            **_safe_result(source_id, method, result, params),
        }
    except Phase7RpcError as exc:
        safe_error = str(exc)
        code_match = re.search(r"JSON-RPC error code (-?\d+|UNKNOWN)", safe_error)
        outcome, result = {"status": "FAIL", "error_type": type(exc).__name__}, None
        if code_match:
            outcome["jsonrpc_error_code"] = code_match.group(1)
            outcome["jsonrpc_error_message"] = "not exposed by sanitized transport"
            outcome["failure_class"] = "JSON_RPC_ERROR"
        elif "invalid JSON-RPC response" in safe_error:
            outcome.update({"json_parse": False, "jsonrpc_envelope": "NOT_REACHED"})
        elif "invalid Bitcoin Core JSON-RPC" in safe_error:
            outcome.update({
                "json_parse": True, "jsonrpc_envelope": "INVALID",
                "failure_class": "INVALID_JSON_RPC_ENVELOPE",
            })
        elif "response exceeds byte cap" in safe_error:
            outcome.update({
                "json_parse": None, "jsonrpc_envelope": "NOT_REACHED",
                "failure_class": "RESPONSE_BYTE_CAP",
            })
        elif "retry budget exhausted" in safe_error:
            outcome.update({
                "json_parse": None, "jsonrpc_envelope": "NOT_REACHED",
                "failure_class": "RETRY_BUDGET_EXHAUSTED",
            })
        elif "HTTP_" in safe_error:
            outcome.update({"json_parse": None, "jsonrpc_envelope": "NOT_REACHED"})
        else:
            outcome.update({"json_parse": None, "jsonrpc_envelope": "UNKNOWN"})
    except Exception as exc:  # noqa: BLE001 - deliberately suppress provider detail text
        outcome, result = {
            "status": "FAIL", "error_type": type(exc).__name__,
            "json_parse": None, "jsonrpc_envelope": "UNKNOWN",
        }, None
    outcome["duration_ms"] = round((time.monotonic() - started) * 1000, 1)
    outcome["_probe_id"] = probe_id
    records[before:] = [{**record, "method": method} for record in records[before:]]
    return outcome, result


async def _source_report(source_id: str, config, plan, *, max_response_bytes: int | None = None):
    if not config.enabled:
        return {"configured": False, "auth_mode": config.auth_mode, "probes": []}
    records: list[dict[str, Any]] = []
    probes = []
    async with aiohttp.ClientSession() as session:
        client = Phase7JsonRpcClient(
            config, _SessionTap(session, records), max_response_bytes=max_response_bytes,
        )
        for method, params in plan:
            outcome, result = await _call(client, source_id, method, params, records)
            outcome["method"] = method
            probe_id = outcome.pop("_probe_id")
            outcome["attempts"] = [record for record in records if record.get("probe_id") == probe_id]
            probes.append(outcome)
            if source_id == "bitcoin_rpc" and method == "getblockcount" and isinstance(result, int):
                if result >= 1:
                    plan.append(("getblockhash", [result]))
            if source_id == "bitcoin_rpc" and method == "getblockhash" and isinstance(result, str):
                plan.extend(("getblock", [result, verbosity]) for verbosity in (0, 1, 2))
            if (
                source_id == "bitcoin_rpc" and method == "getblock" and len(params) > 1
                and params[1] == 2 and isinstance(result, dict)
            ):
                outcome["parser"] = _bitcoin_parser_report(result, str(params[0]))
    return {
        "configured": True,
        "auth_mode": config.auth_mode,
        "timeout_seconds": config.timeout_seconds,
        "requests_per_second": config.requests_per_second,
        "max_concurrency": config.max_concurrency,
        "max_response_bytes": client.max_response_bytes,
        "probes": probes,
    }


async def _spot_report() -> dict[str, Any]:
    adapter = BinanceSpotAdapter()
    result: dict[str, Any] = {"instrument_policy": "FIXED_ALLOWLIST_BTCUSDT_ETHUSDT"}
    async with aiohttp.ClientSession(trust_env=False) as session:
        path, params = adapter.rest_request("BTCUSDT", limit=1)
        try:
            async with session.get(
                "https://api.binance.com" + path,
                params=params,
                timeout=aiohttp.ClientTimeout(total=5, connect=1),
            ) as response:
                body = await _read_bounded_body(response, 262_144, "binance_spot")
                outcome: dict[str, Any] = {
                    "http": response.status,
                    "content_type": _safe_content_type(response.headers),
                    "bytes": len(body),
                }
                try:
                    payload = json.loads(body)
                    events = adapter.parse_rest_response(
                        payload, symbol="BTCUSDT", fetched_at=datetime.now(timezone.utc),
                    )
                    outcome.update({
                        "json_parse": True, "schema": "PASS", "event_count": len(events),
                        "side": events[0].side.value if events else None,
                        "event_timestamp_utc": events[0].event_timestamp.isoformat() if events else None,
                    })
                except Exception as exc:  # noqa: BLE001 - body and provider text stay private
                    outcome.update({"json_parse": False, "schema": "FAIL", "error_type": type(exc).__name__})
                result["rest"] = outcome
        except Exception as exc:  # noqa: BLE001 - never surface request URL or exception text
            result["rest"] = {"status": "FAIL", "error_type": type(exc).__name__}

        try:
            timeout = aiohttp.ClientTimeout(total=12, connect=3)
            async with session.ws_connect(
                adapter.ws_endpoint, heartbeat=30, timeout=timeout, max_msg_size=1_048_576,
            ) as websocket:
                await websocket.send_json(adapter.subscription("BTCUSDT", request_id=1))
                ws_result: dict[str, Any] = {"upgrade": "PASS", "ack": False, "trade": "NOT_SEEN"}
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        message = await asyncio.wait_for(
                            websocket.receive(), timeout=min(2, deadline - time.monotonic()),
                        )
                    except asyncio.TimeoutError:
                        continue
                    if message.type is not aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        payload = json.loads(message.data)
                    except json.JSONDecodeError:
                        ws_result["trade"] = "INVALID_JSON"
                        break
                    if isinstance(payload, dict) and payload.get("id") == 1 and payload.get("result") is None:
                        ws_result["ack"] = True
                    elif isinstance(payload, dict) and payload.get("e") == "aggTrade":
                        try:
                            event = adapter.parse_ws_message(
                                payload,
                                fetched_at=datetime.now(timezone.utc),
                            )
                            ws_result.update({
                                "trade": "PASS", "side": event.side.value,
                                "event_timestamp_utc": event.event_timestamp.isoformat(),
                            })
                        except Exception as exc:  # noqa: BLE001
                            ws_result["trade"] = type(exc).__name__
                        break
                result["websocket"] = ws_result
        except Exception as exc:  # noqa: BLE001 - never surface URL or provider body
            result["websocket"] = {"upgrade": "FAIL", "error_type": type(exc).__name__}
    return result


async def main(args: argparse.Namespace) -> None:
    settings = Settings.from_env(_local_environment())
    bitcoin_config = settings.phase7_bitcoin_rpc
    if args.bitcoin_timeout_seconds is not None:
        if not 0.1 <= args.bitcoin_timeout_seconds <= 60:
            raise ValueError("Bitcoin probe timeout must be between 0.1 and 60 seconds")
        bitcoin_config = replace(bitcoin_config, timeout_seconds=args.bitcoin_timeout_seconds)
    max_response_bytes = args.bitcoin_max_response_bytes
    if max_response_bytes is not None and not 1 <= max_response_bytes <= 64 * 1024 * 1024:
        raise ValueError("Bitcoin probe response cap must be between 1 byte and 64 MiB")
    bitcoin_plan = [("getblockchaininfo", []), ("getblockcount", [])]
    bitcoin = await _source_report(
        "bitcoin_rpc", bitcoin_config, bitcoin_plan,
        max_response_bytes=max_response_bytes,
    )
    ethereum_plan = [
        ("eth_chainId", []), ("eth_blockNumber", []),
        ("eth_getBlockByNumber", ["latest", False]),
        ("eth_getBlockByNumber", ["0x1", False]),
    ]
    ethereum = await _source_report("ethereum_rpc", settings.phase7_ethereum_rpc, ethereum_plan)
    print(json.dumps({"bitcoin": bitcoin, "ethereum": ethereum, "binance_spot": await _spot_report()}, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--bitcoin-timeout-seconds", type=float)
    parser.add_argument("--bitcoin-max-response-bytes", type=int)
    asyncio.run(main(parser.parse_args()))
