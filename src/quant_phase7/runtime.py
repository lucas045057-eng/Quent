"""Owned Phase 7 runtime lifecycles for the existing Collector and Engine."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
import math
from typing import Any, Awaitable, Callable, Mapping

from quant_phase1.config import (
    PHASE7_RPC_MAX_RESPONSE_BYTES,
    Phase7RpcSourceSettings,
    Settings,
)
from quant_phase1.contracts import DataStatus as HealthDataStatus
from quant_phase1.health import ComponentHealth, HealthRegistry
from quant_phase1.time import utc_now
from quant_data_layer.admission import (
    AdmissionDeferred,
    ReplayClass,
    WorkAdmissionController,
    make_work_request,
)
from quant_data_layer.db_admission import (
    DbAdmissionDeferred,
    DbWorkClass as DbTransactionWorkClass,
    PostgresWriteAdmission,
)
from quant_data_layer.observability import ProcessRole, SourceId, SourcePhase, WorkClass

from .source_config import (
    SourceConfigurationStatus,
    phase7_source_diagnostics,
    register_phase7_source_health,
)
from .sources import SourceRegistry, SourceStatus, default_source_definitions
from .bitcoin import BitcoinBlockParser
from .ethereum import DEFAULT_ETHEREUM_ASSETS, EthereumBlockParser, TRANSFER_TOPIC
from .contracts import (
    AssetKind,
    Chain,
    DataStatus,
    MarketKind,
    UsdValuation,
)
from .persistence import MAX_TRANSFER_BATCH, Phase7ContextLimitError, Phase7Repository
from .recovery import CheckpointState
from .retention import Phase7HealthTracker, Phase7RetentionPolicy, cleanup_phase7_retention
from .spot import BinanceSpotAdapter
from .spot_flow import aggregate_spot_window


LOGGER = logging.getLogger("quant_phase7.runtime")
_RPC_MAX_ATTEMPTS = 3
_RPC_MAX_TOTAL_RETRY_WAIT_SECONDS = 8.0
_RPC_BACKOFF_SECONDS = (0.5, 1.5, 3.0)
_RPC_MIN_429_RETRY_SECONDS = 0.25
_PHASE7_CONSECUTIVE_FAILURE_ERROR_THRESHOLD = 3
_ETHEREUM_RECEIPT_BATCH_SIZE = 250
_ETHEREUM_RECEIPT_CANDIDATE_LIMIT = 2_000
_PHASE7_DB_ADMISSION_TIMEOUT_SECONDS = 2.0  # provisional until DATA_LAYER_REPLAY_V1 freezes capacity


class Phase7RpcError(RuntimeError):
    """A sanitized, bounded public JSON-RPC failure."""


class Phase7TransientRpcError(Phase7RpcError):
    """A bounded transport failure eligible for recovery-aware degradation."""


class Phase7RateLimitedError(Phase7TransientRpcError):
    """A provider rate limit exhausted the bounded retry policy."""


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value.strip())
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            return None
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


async def _read_bounded_body(response: Any, max_bytes: int, source_id: str) -> bytes:
    """Read the complete decompressed response incrementally under a strict cap."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    body = bytearray()
    async for chunk in response.content.iter_chunked(min(65_536, max_bytes + 1)):
        body.extend(chunk)
        if len(body) > max_bytes:
            raise Phase7RpcError(f"{source_id}: response exceeds byte cap")
    return bytes(body)


class Phase7JsonRpcClient:
    """Read-only JSON-RPC transport with a strict method allowlist."""

    _METHODS = {
        "bitcoin_rpc": frozenset({"getblockchaininfo", "getblockcount", "getblockhash", "getblock", "getrawtransaction"}),
        "ethereum_rpc": frozenset({
            "eth_chainId", "eth_blockNumber", "eth_getBlockByNumber", "eth_getLogs",
            "eth_getTransactionReceipt",
        }),
    }

    def __init__(
        self,
        config: Phase7RpcSourceSettings,
        session: Any,
        *,
        max_response_bytes: int | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        if config.source_id not in self._METHODS:
            raise ValueError("unsupported Phase 7 RPC source")
        if not config.enabled or not config.endpoint:
            raise ValueError("RPC client requires an enabled, configured source")
        if config.auth_mode == "API_KEY_HEADER":
            if config.source_id != "bitcoin_rpc":
                raise ValueError("API-key header auth is supported only for Bitcoin RPC")
            if config.api_key is None or not config.api_key.get_secret_value().strip():
                raise ValueError("Bitcoin RPC API key is required for API_KEY_HEADER auth")
        self.config = config
        self.session = session
        response_limit = config.max_response_bytes if max_response_bytes is None else max_response_bytes
        if (
            isinstance(response_limit, bool)
            or not isinstance(response_limit, int)
            or not 1 <= response_limit <= PHASE7_RPC_MAX_RESPONSE_BYTES
        ):
            raise ValueError(
                f"max_response_bytes must be between 1 and {PHASE7_RPC_MAX_RESPONSE_BYTES}"
            )
        self.max_response_bytes = response_limit
        self.sleep = sleep
        self._request_id = 0
        self._rate_lock = asyncio.Lock()
        self._next_request_at = 0.0
        self.active_requests = 0
        self.request_count = 0
        self.response_bytes_total = 0
        self.last_response_bytes = 0
        self.last_method: str | None = None

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        if method not in self._METHODS[self.config.source_id]:
            raise Phase7RpcError(f"{self.config.source_id}: method is not approved")
        total_retry_wait = 0.0
        for attempt in range(_RPC_MAX_ATTEMPTS):
            await self._wait_for_rate()
            self._request_id += 1
            request_id = self._request_id
            request = {
                "jsonrpc": "1.0" if self.config.source_id == "bitcoin_rpc" else "2.0",
                "id": request_id,
                "method": method,
                "params": params or [],
            }
            try:
                result = await self._post(request)
                return result
            except _RetryableRpcFailure as exc:
                if attempt >= _RPC_MAX_ATTEMPTS - 1:
                    if exc.reason == "HTTP_429":
                        raise Phase7RateLimitedError(
                            f"{self.config.source_id}: RATE_LIMITED after {_RPC_MAX_ATTEMPTS} attempts"
                        ) from None
                    raise Phase7TransientRpcError(f"{self.config.source_id}: retry budget exhausted ({exc.reason})") from None
                delay = exc.delay if exc.delay is not None else _RPC_BACKOFF_SECONDS[attempt]
                if exc.reason == "HTTP_429":
                    delay = max(delay, _RPC_MIN_429_RETRY_SECONDS)
                if total_retry_wait + delay > _RPC_MAX_TOTAL_RETRY_WAIT_SECONDS:
                    if exc.reason == "HTTP_429":
                        raise Phase7RateLimitedError(
                            f"{self.config.source_id}: RATE_LIMITED retry wait budget exhausted"
                        ) from None
                    raise Phase7TransientRpcError(
                        f"{self.config.source_id}: retry wait budget exhausted ({exc.reason})"
                    ) from None
                total_retry_wait += delay
                await self.sleep(delay)
            except Phase7RpcError:
                raise
            except Exception as exc:  # noqa: BLE001 - do not surface URL-bearing client exception text
                if attempt >= 2:
                    raise Phase7TransientRpcError(
                        f"{self.config.source_id}: transport failed ({type(exc).__name__})"
                    ) from None
                await self.sleep((0.5, 1.5)[attempt])
        raise AssertionError("bounded JSON-RPC retry loop exhausted unexpectedly")

    async def call_batch(self, calls: list[tuple[str, list[Any]]]) -> list[Any]:
        if self.config.source_id == "bitcoin_rpc":
            raise Phase7RpcError("bitcoin_rpc: batch requests are not approved")
        if not calls or len(calls) > 250:
            raise Phase7RpcError(f"{self.config.source_id}: batch size exceeds cap")
        for method, _params in calls:
            if method not in self._METHODS[self.config.source_id]:
                raise Phase7RpcError(f"{self.config.source_id}: method is not approved")
        total_retry_wait = 0.0
        for attempt in range(_RPC_MAX_ATTEMPTS):
            await self._wait_for_rate()
            requests = []
            for method, params in calls:
                self._request_id += 1
                requests.append({
                    "jsonrpc": "2.0", "id": self._request_id,
                    "method": method, "params": params,
                })
            try:
                return await self._post(requests)
            except _RetryableRpcFailure as exc:
                if attempt >= _RPC_MAX_ATTEMPTS - 1:
                    if exc.reason == "HTTP_429":
                        raise Phase7RateLimitedError(
                            f"{self.config.source_id}: RATE_LIMITED after {_RPC_MAX_ATTEMPTS} attempts"
                        ) from None
                    raise Phase7TransientRpcError(f"{self.config.source_id}: retry budget exhausted ({exc.reason})") from None
                delay = exc.delay if exc.delay is not None else _RPC_BACKOFF_SECONDS[attempt]
                if exc.reason == "HTTP_429":
                    delay = max(delay, _RPC_MIN_429_RETRY_SECONDS)
                if total_retry_wait + delay > _RPC_MAX_TOTAL_RETRY_WAIT_SECONDS:
                    if exc.reason == "HTTP_429":
                        raise Phase7RateLimitedError(
                            f"{self.config.source_id}: RATE_LIMITED retry wait budget exhausted"
                        ) from None
                    raise Phase7TransientRpcError(
                        f"{self.config.source_id}: retry wait budget exhausted ({exc.reason})"
                    ) from None
                total_retry_wait += delay
                await self.sleep(delay)
            except Phase7RpcError:
                raise
            except Exception as exc:  # noqa: BLE001 - do not surface URL-bearing client exception text
                if attempt >= 2:
                    raise Phase7TransientRpcError(
                        f"{self.config.source_id}: transport failed ({type(exc).__name__})"
                    ) from None
                await self.sleep((0.5, 1.5)[attempt])
        raise AssertionError("bounded JSON-RPC batch retry loop exhausted unexpectedly")

    async def _wait_for_rate(self) -> None:
        interval = 1.0 / self.config.requests_per_second
        async with self._rate_lock:
            now = asyncio.get_running_loop().time()
            delay = max(0.0, self._next_request_at - now)
            self._next_request_at = max(now, self._next_request_at) + interval
        if delay:
            await self.sleep(delay)

    async def _post(self, request: Mapping[str, Any] | list[Mapping[str, Any]]) -> Any:
        try:
            import aiohttp
        except ImportError as exc:  # pragma: no cover - required runtime dependency
            raise Phase7RpcError("aiohttp is unavailable") from exc

        auth = None
        if self.config.auth_mode == "BASIC":
            auth = aiohttp.BasicAuth(self.config.username, self.config.password)
        request_options: dict[str, Any] = {
            "json": request if isinstance(request, list) else dict(request),
            "auth": auth,
            "timeout": aiohttp.ClientTimeout(
                total=self.config.timeout_seconds,
                connect=min(2.0, self.config.timeout_seconds),
            ),
        }
        if self.config.auth_mode == "API_KEY_HEADER":
            # SecretStr is unwrapped only at the HTTP boundary; it is never
            # included in settings, diagnostics, exceptions, or log messages.
            request_options["headers"] = {
                "api-key": self.config.api_key.get_secret_value()
            }
        request_method = request[0].get("method") if isinstance(request, list) else request.get("method")
        self.last_method = request_method if isinstance(request_method, str) else None
        self.request_count += 1
        self.active_requests += 1
        try:
            async with self.session.post(
                self.config.endpoint,
                **request_options,
            ) as response:
                if response.status in {408, 425, 429, 500, 502, 503, 504}:
                    delay = _retry_after_seconds(response.headers.get("Retry-After"))
                    raise _RetryableRpcFailure(f"HTTP_{response.status}", delay)
                if response.status < 200 or response.status >= 300:
                    raise Phase7RpcError(f"{self.config.source_id}: HTTP_{response.status}")
                body = await _read_bounded_body(response, self.max_response_bytes, self.config.source_id)
                self.last_response_bytes = len(body)
                self.response_bytes_total += len(body)
        except _RetryableRpcFailure:
            raise
        except Phase7RpcError:
            raise
        except Exception as exc:  # noqa: BLE001 - never include request URL in the error
            raise _RetryableRpcFailure(type(exc).__name__, 0.5) from None
        finally:
            self.active_requests -= 1
        try:
            payload = json.loads(
                body,
                parse_float=Decimal if self.config.source_id == "bitcoin_rpc" else float,
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise Phase7RpcError(f"{self.config.source_id}: invalid JSON-RPC response") from None
        if isinstance(request, list):
            if not isinstance(payload, list) or len(payload) != len(request):
                raise Phase7RpcError(f"{self.config.source_id}: invalid JSON-RPC batch envelope")
            expected_ids = {item["id"] for item in request}
            results: dict[int, Any] = {}
            for item in payload:
                if not isinstance(item, dict) or item.get("jsonrpc") != "2.0" or item.get("id") not in expected_ids:
                    raise Phase7RpcError(f"{self.config.source_id}: invalid JSON-RPC batch item")
                if "error" in item:
                    error = item.get("error")
                    code = error.get("code") if isinstance(error, dict) else None
                    safe_code = code if isinstance(code, int) and not isinstance(code, bool) else "UNKNOWN"
                    raise Phase7RpcError(f"{self.config.source_id}: JSON-RPC error code {safe_code}")
                if "result" not in item:
                    raise Phase7RpcError(f"{self.config.source_id}: JSON-RPC result is missing")
                results[item["id"]] = item["result"]
            if set(results) != expected_ids:
                raise Phase7RpcError(f"{self.config.source_id}: incomplete JSON-RPC batch")
            return [results[item["id"]] for item in request]
        if self.config.source_id == "bitcoin_rpc":
            if (
                not isinstance(payload, dict)
                or payload.get("id") != request["id"]
                or payload.get("jsonrpc") not in (None, "1.0")
                or "result" not in payload
                or "error" not in payload
            ):
                raise Phase7RpcError("bitcoin_rpc: invalid Bitcoin Core JSON-RPC 1.0 envelope")
            error = payload.get("error")
            if error is not None:
                code = error.get("code") if isinstance(error, dict) else None
                safe_code = code if isinstance(code, int) and not isinstance(code, bool) else "UNKNOWN"
                raise Phase7RpcError(f"bitcoin_rpc: JSON-RPC error code {safe_code}")
            return payload["result"]
        if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or payload.get("id") != request["id"]:
            raise Phase7RpcError(f"{self.config.source_id}: invalid JSON-RPC envelope")
        if "error" in payload:
            error = payload.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            safe_code = code if isinstance(code, int) and not isinstance(code, bool) else "UNKNOWN"
            raise Phase7RpcError(f"{self.config.source_id}: JSON-RPC error code {safe_code}")
        if "result" not in payload:
            raise Phase7RpcError(f"{self.config.source_id}: JSON-RPC result is missing")
        return payload["result"]


class _RetryableRpcFailure(Exception):
    def __init__(self, reason: str, delay: float | None) -> None:
        self.reason = reason
        self.delay = delay


class Phase7CollectorRuntime:
    """Lifecycle owner for Phase 7 public sources inside the Collector."""

    def __init__(
        self,
        settings: Settings,
        *,
        connection_factory: Callable[[str], Any] | None = None,
        health_writer: Callable[[str, HealthDataStatus, datetime, dict[str, Any]], None] | None = None,
        source_registry: SourceRegistry | None = None,
        cycle_handlers: Mapping[str, Callable[[], Awaitable[tuple[DataStatus, dict[str, Any]]]]] | None = None,
        cycle_interval_seconds: float = 30.0,
        spot_ws_connector: Callable[[Any, str], Any] | None = None,
        spot_websocket_enabled: bool = True,
        admission: WorkAdmissionController | None = None,
        db_admission: PostgresWriteAdmission | None = None,
    ) -> None:
        self.settings = settings
        self.admission = admission or WorkAdmissionController(role=ProcessRole.COLLECTOR)
        if self.admission.role is not ProcessRole.COLLECTOR:
            raise ValueError("Phase 7 collector runtime requires Collector-owned admission")
        self.db_admission = db_admission
        self.connection_factory = connection_factory
        self.health_writer = health_writer
        if source_registry is None:
            configured = {
                "btc_core_rpc": settings.phase7_enabled and settings.phase7_bitcoin_rpc.enabled,
                "ethereum_rpc": settings.phase7_enabled and settings.phase7_ethereum_rpc.enabled,
            }
            definitions = tuple(
                replace(
                    definition,
                    status=SourceStatus.ENABLED if configured.get(definition.source_id, definition.enabled)
                    else SourceStatus.NOT_CONFIGURED if definition.source_id in configured
                    else definition.status,
                )
                for definition in default_source_definitions()
            )
            self.source_registry = SourceRegistry(definitions)
        else:
            self.source_registry = source_registry
        self.cycle_handlers = dict(cycle_handlers or {})
        self.cycle_interval_seconds = max(0.01, cycle_interval_seconds)
        self.spot_ws_connector = spot_ws_connector
        self.spot_websocket_enabled = spot_websocket_enabled
        self.diagnostics_sink: Callable[[], None] | None = None
        self.health_registry = HealthRegistry()
        self.health_tracker = Phase7HealthTracker()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._running = False
        self._stop_event: asyncio.Event | None = None
        self._spot_events: dict[str, list[Any]] = {"BTCUSDT": [], "ETHUSDT": []}
        self._spot_event_keys: dict[str, set[tuple[str, str, str]]] = {"BTCUSDT": set(), "ETHUSDT": set()}
        self._source_stages: dict[str, str] = {}
        self._source_consecutive_failures: dict[str, int] = {}
        self._spot_ws_metrics = {
            "text_messages": 0, "subscription_acks": 0, "agg_trade_messages": 0,
            "accepted_trades": 0, "duplicate_trades": 0,
        }
        self._active_rpc_clients: dict[str, Phase7JsonRpcClient] = {}
        self._rpc_last: dict[str, dict[str, int | str]] = {}
        self._diagnostics: dict[str, int | str] = {
            "bitcoin_pending_blocks": 0,
            "bitcoin_active_block_height": 0,
            "bitcoin_block_event_count": 0,
            "bitcoin_pending_persistence_chunks": 0,
            "ethereum_backfill_depth": 0,
            "ethereum_active_block_height": 0,
            "ethereum_pending_logs": 0,
            "ethereum_pending_receipts": 0,
            "ethereum_block_event_count": 0,
            "spot_active_rest_requests": 0,
            "spot_rest_response_bytes": 0,
            "spot_aggregation_active": 0,
            "spot_ws_connected": 0,
        }

    @property
    def task_count(self) -> int:
        return sum(not task.done() for task in self._tasks)

    def _publish_diagnostics(self) -> None:
        if self.diagnostics_sink is None:
            return
        try:
            self.diagnostics_sink()
        except Exception as exc:  # diagnostics must never change source behavior
            LOGGER.warning("acceptance_diagnostics_publish_failed exception=%s", type(exc).__name__)

    def diagnostics_snapshot(self) -> dict[str, int | str]:
        """Return scalar, secret-free source/work counters without mutating runtime state."""

        btc = self._active_rpc_clients.get("bitcoin_rpc")
        eth = self._active_rpc_clients.get("ethereum_rpc")
        spot_trade_count = sum(len(events) for events in self._spot_events.values())
        spot_window_count = sum(
            len({event.event_timestamp.replace(second=0, microsecond=0) for event in events})
            for events in self._spot_events.values()
        )
        btc_stage = self._source_stages.get("bitcoin_rpc", "IDLE")
        eth_stage = self._source_stages.get("ethereum_rpc", "IDLE")
        spot_stage = self._source_stages.get("binance_spot", "IDLE")
        btc_last = self._rpc_last.get("bitcoin_rpc", {})
        eth_last = self._rpc_last.get("ethereum_rpc", {})
        return {
            **(self._sbe_worker.diagnostics_snapshot() if getattr(self,'_sbe_worker',None) else {}),
            "phase7_task_count": self.task_count,
            "phase7_active_source_cycles": sum(
                stage not in {"IDLE", "CYCLE_COMPLETE", "CYCLE_BACKOFF", "STOPPED"}
                for stage in (btc_stage, eth_stage, spot_stage)
            ),
            "phase7_bitcoin_stage": btc_stage,
            "phase7_bitcoin_pending_blocks": int(self._diagnostics["bitcoin_pending_blocks"]),
            "phase7_bitcoin_backfill_depth": int(self._diagnostics["bitcoin_pending_blocks"]),
            "phase7_bitcoin_active_rpc": btc.active_requests if btc else 0,
            "phase7_bitcoin_rpc_requests_cycle": btc.request_count if btc else int(btc_last.get("requests", 0)),
            "phase7_bitcoin_last_response_bytes": btc.last_response_bytes if btc else int(btc_last.get("last_response_bytes", 0)),
            "phase7_bitcoin_response_bytes_cycle": btc.response_bytes_total if btc else int(btc_last.get("response_bytes_total", 0)),
            "phase7_bitcoin_last_rpc_method": btc.last_method or "" if btc else str(btc_last.get("last_method", "")),
            "phase7_bitcoin_active_block_processing": int(
                btc_stage in {"BLOCK_PARSE", "EVENT_VALUATION", "BLOCK_PERSISTENCE", "REORG_PERSISTENCE"}
            ),
            "phase7_bitcoin_active_block_height": int(self._diagnostics["bitcoin_active_block_height"]),
            "phase7_bitcoin_block_event_count": int(self._diagnostics["bitcoin_block_event_count"]),
            "phase7_bitcoin_pending_persistence_chunks": int(
                self._diagnostics["bitcoin_pending_persistence_chunks"]
            ),
            "phase7_ethereum_stage": eth_stage,
            "phase7_ethereum_backfill_depth": int(self._diagnostics["ethereum_backfill_depth"]),
            "phase7_ethereum_pending_blocks": int(self._diagnostics["ethereum_backfill_depth"]),
            "phase7_ethereum_active_rpc": eth.active_requests if eth else 0,
            "phase7_ethereum_rpc_requests_cycle": eth.request_count if eth else int(eth_last.get("requests", 0)),
            "phase7_ethereum_last_response_bytes": eth.last_response_bytes if eth else int(eth_last.get("last_response_bytes", 0)),
            "phase7_ethereum_response_bytes_cycle": eth.response_bytes_total if eth else int(eth_last.get("response_bytes_total", 0)),
            "phase7_ethereum_last_rpc_method": eth.last_method or "" if eth else str(eth_last.get("last_method", "")),
            "phase7_ethereum_active_block_processing": int(
                eth_stage in {"BLOCK_PROCESSING", "LOGS_REQUEST", "RECEIPTS_REQUEST", "BLOCK_PERSISTENCE"}
            ),
            "phase7_ethereum_active_block_height": int(self._diagnostics["ethereum_active_block_height"]),
            "phase7_ethereum_pending_logs": int(self._diagnostics["ethereum_pending_logs"]),
            "phase7_ethereum_pending_receipts": int(self._diagnostics["ethereum_pending_receipts"]),
            "phase7_ethereum_block_event_count": int(self._diagnostics["ethereum_block_event_count"]),
            "phase7_spot_stage": spot_stage,
            "phase7_spot_active_rest_requests": int(self._diagnostics["spot_active_rest_requests"]),
            "phase7_spot_response_bytes": int(self._diagnostics["spot_rest_response_bytes"]),
            "phase7_spot_ws_connected": int(self._diagnostics["spot_ws_connected"]),
            "phase7_spot_ws_application_queue_depth": 0,
            "phase7_spot_ws_buffer_mode": "INLINE_PARSE",
            "phase7_spot_ws_internal_queue_visibility": "NOT_EXPOSED_BY_AIOHTTP_PUBLIC_API",
            "phase7_spot_pending_trades": spot_trade_count,
            "phase7_spot_pending_trade_capacity": 6_000,
            "phase7_spot_pending_windows": spot_window_count,
            "phase7_spot_aggregation_active": int(self._diagnostics["spot_aggregation_active"]),
        }

    async def run(self, stop_event: asyncio.Event) -> None:
        if self._running:
            raise RuntimeError("Phase 7 Collector runtime is already running")
        if not self.settings.phase7_enabled and not self.settings.bitget_sbe_flow_enabled:
            return
        self._running = True
        self._stop_event = stop_event
        register_phase7_source_health(self.health_registry, self.settings)
        await self._persist_configuration_health()
        try:
            if self.settings.bitget_sbe_flow_enabled:
                from .bitget_sbe import BitgetSbeFlowWorker
                self._sbe_worker=BitgetSbeFlowWorker(self)
                self._spawn(self._sbe_worker.run(stop_event))
            if self.settings.phase7_enabled:
                for source_id, config in (
                    ("bitcoin_rpc", self.settings.phase7_bitcoin_rpc),
                    ("ethereum_rpc", self.settings.phase7_ethereum_rpc),
                ):
                    if config.enabled:
                        self._spawn(self._supervise(source_id, stop_event))
                self._spawn(self._supervise("binance_spot", stop_event))
                if self.spot_websocket_enabled:
                    self._spawn(self._spot_ws_supervisor(stop_event))
            await stop_event.wait()
        finally:
            tasks = tuple(self._tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks.clear()
            await self._mark_stopped()
            self._running = False

    def _spawn(self, coroutine: Awaitable[Any]) -> None:
        task = asyncio.create_task(coroutine, name="phase7-source-worker")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _supervise(self, source_id: str, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                self._source_stages[source_id] = "CYCLE_START"
                handler = self.cycle_handlers.get(source_id)
                try:
                    if handler is None:
                        result = await self._run_source_cycle(source_id)
                    else:
                        result = await handler()
                finally:
                    client = self._active_rpc_clients.pop(source_id, None)
                    if client is not None:
                        self._rpc_last[source_id] = {
                            "requests": client.request_count,
                            "last_response_bytes": client.last_response_bytes,
                            "response_bytes_total": client.response_bytes_total,
                            "last_method": client.last_method or "",
                        }
                status, details = result
                if status is DataStatus.AVAILABLE:
                    self._source_consecutive_failures[source_id] = 0
                success_details = {
                    **details,
                    "runtime_state": "RUNNING" if status is DataStatus.AVAILABLE else "DEGRADED",
                    "phase7_status": status.value,
                    "consecutive_failures": self._source_consecutive_failures.get(source_id, 0),
                }
                if status is DataStatus.PARTIAL:
                    success_details["data_quality"] = "PARTIAL"
                await self._write_health(source_id, HealthDataStatus(status.value), utc_now(), {
                    **success_details,
                })
            except asyncio.CancelledError:
                raise
            except AdmissionDeferred as exc:
                self._source_stages[source_id] = "ADMISSION_DEFERRED"
                await self._write_health(source_id, HealthDataStatus.NOT_AVAILABLE, utc_now(), {
                    "runtime_state": "DEGRADED",
                    "phase7_status": DataStatus.PARTIAL.value,
                    "reason": exc.reason.value,
                    "failure_stage": "LOCAL_ADMISSION",
                })
                LOGGER.info("phase7_source_admission_deferred source=%s reason=%s", source_id, exc.reason.value)
            except DbAdmissionDeferred as exc:
                self._source_stages[source_id] = "DB_ADMISSION_DEFERRED"
                await self._write_health(source_id, HealthDataStatus.NOT_AVAILABLE, utc_now(), {
                    "runtime_state": "DEGRADED",
                    "phase7_status": DataStatus.PARTIAL.value,
                    "reason": exc.reason.value,
                    "failure_stage": "DB_WRITE_ADMISSION",
                })
                LOGGER.info("phase7_source_db_admission_deferred source=%s reason=%s", source_id, exc.reason.value)
            except Phase7RateLimitedError:
                self._source_stages[source_id] = "RATE_LIMITED"
                await self._record_transient_failure(
                    source_id, reason="RATE_LIMITED", error_category="RATE_LIMIT",
                    failure_stage="HTTP_429",
                )
                LOGGER.warning("phase7_source_rate_limited source=%s", source_id)
                self._source_stages[source_id] = "CYCLE_BACKOFF"
            except (Phase7TransientRpcError, TimeoutError, asyncio.TimeoutError, ConnectionError, OSError) as exc:
                self._source_stages[source_id] = "TRANSPORT_FAILURE"
                await self._record_transient_failure(
                    source_id,
                    reason="NETWORK_TIMEOUT" if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) else "TRANSPORT_FAILURE",
                    error_category="NETWORK",
                    failure_stage="TRANSPORT_FAILURE",
                )
                LOGGER.warning("phase7_source_transient_failure source=%s exception=%s", source_id, type(exc).__name__)
                self._source_stages[source_id] = "CYCLE_BACKOFF"
            except Exception as exc:  # noqa: BLE001 - never echo provider errors or credentials
                if self._is_transient_network_error(exc):
                    await self._record_transient_failure(
                        source_id,
                        reason="NETWORK_TIMEOUT" if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) else "TRANSPORT_FAILURE",
                        error_category="NETWORK",
                        failure_stage="TRANSPORT_FAILURE",
                    )
                    LOGGER.warning("phase7_source_transient_failure source=%s exception=%s", source_id, type(exc).__name__)
                    self._source_stages[source_id] = "CYCLE_BACKOFF"
                else:
                    stage = self._source_stages.get(source_id, "UNKNOWN")
                    await self._write_health(source_id, HealthDataStatus.ERROR, utc_now(), {
                        "runtime_state": "DEGRADED",
                        "reason": type(exc).__name__,
                        "phase7_status": "ERROR",
                        "error_category": self._error_category_for_stage(stage),
                        "failure_stage": stage,
                    })
                    LOGGER.warning("phase7_source_cycle_failed source=%s exception=%s", source_id, type(exc).__name__)
                    self._source_stages[source_id] = "CYCLE_BACKOFF"
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.cycle_interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def _record_transient_failure(
        self,
        source_id: str,
        *,
        reason: str,
        error_category: str,
        failure_stage: str,
    ) -> None:
        failures = self._source_consecutive_failures.get(source_id, 0) + 1
        self._source_consecutive_failures[source_id] = failures
        terminal = failures >= _PHASE7_CONSECUTIVE_FAILURE_ERROR_THRESHOLD
        await self._write_health(
            source_id,
            HealthDataStatus.ERROR if terminal else HealthDataStatus.NOT_AVAILABLE,
            utc_now(),
            {
                "runtime_state": "DEGRADED",
                "phase7_status": "ERROR" if terminal else DataStatus.PARTIAL.value,
                "data_quality": "PARTIAL",
                "reason": reason,
                "error_category": error_category,
                "failure_stage": failure_stage,
                "consecutive_failures": failures,
                "retry_policy": "BOUNDED_RETRY_AFTER_OR_BACKOFF",
            },
        )

    @staticmethod
    def _error_category_for_stage(stage: str) -> str:
        normalized = stage.upper()
        if "PERSIST" in normalized or "CHECKPOINT" in normalized:
            return "PERSISTENCE"
        if "PARSE" in normalized or "SCHEMA" in normalized:
            return "PARSER"
        if "CONFIG" in normalized:
            return "CONFIGURATION"
        if "QUALITY" in normalized or "REORG" in normalized:
            return "DATA_QUALITY"
        return "PROVIDER_CONTRACT"

    async def _run_source_cycle(self, source_id: str) -> tuple[DataStatus, dict[str, Any]]:
        if source_id == "bitcoin_rpc":
            return await self._bitcoin_cycle()
        if source_id == "ethereum_rpc":
            return await self._ethereum_cycle()
        if source_id == "binance_spot":
            return await self._spot_cycle()
        raise ValueError("unknown Phase 7 source")

    @contextmanager
    def _repository_scope(self):
        import psycopg

        factory = self.connection_factory or psycopg.connect
        connection = factory(self.settings.postgres_dsn)
        try:
            from quant_phase1.db import assert_schema_ready

            assert_schema_ready(connection, required_version="014_phase7_exact_amount_constraint.sql")
            # End the read-only readiness transaction before source work begins.
            # Repository transaction() blocks must be top-level commits.
            if not connection.autocommit:
                connection.commit()
            connection.autocommit = True
            yield Phase7Repository(connection)
        finally:
            connection.close()

    @contextmanager
    def _database_write_scope(self, repository: Phase7Repository, identity: str):
        """Admit high-fanout event/cursor writes across Collector processes."""
        if self.db_admission is None:
            yield repository
            return
        with self.db_admission.transaction(
            self.settings.postgres_dsn,
            work_class=DbTransactionWorkClass.HEAVY,
            timeout_seconds=_PHASE7_DB_ADMISSION_TIMEOUT_SECONDS,
            identity=identity,
            business_connection=repository.connection,
        ) as connection:
            yield Phase7Repository(connection)

    @staticmethod
    def _rpc_session():
        import aiohttp

        return aiohttp.ClientSession()

    async def _process_bitcoin_block(
        self,
        repository,
        parser,
        block,
        *,
        block_hash: str,
        height: int,
        observed_at: datetime,
        fetched_at: datetime,
        response_bytes: int,
        reorg: bool = False,
    ) -> int:
        request = make_work_request(
            phase=SourcePhase.PHASE7,
            source_id=SourceId.PHASE7_ONCHAIN,
            work_class=WorkClass.HEAVY,
            estimated_items=1,
            estimated_bytes=max(65_536, response_bytes * 2),
            replay_class=ReplayClass.RECOVERABLE_REPLAYABLE,
            cancellation_owner=ProcessRole.COLLECTOR,
            stream_id="phase7.bitcoin_blocks",
            timeout_seconds=2.0,
        )
        async with self.admission.admit(request):
            events = parser.parse_block(
                block,
                observed_at=observed_at,
                fetched_at=fetched_at,
                processed_at=utc_now(),
                expected_block_hash=block_hash,
            )
            self._diagnostics["bitcoin_block_event_count"] = len(events)
            self._diagnostics["bitcoin_pending_persistence_chunks"] = math.ceil(
                len(events) / MAX_TRANSFER_BATCH
            )
            state = self._checkpoint(
                "btc_core_rpc", "BITCOIN", "BLOCK", height, block_hash,
                "phase7-btc-rpc-v1", DataStatus.AVAILABLE, None,
            )
            if reorg:
                self._source_stages["bitcoin_rpc"] = "REORG_PERSISTENCE"
                with self._database_write_scope(repository, f"phase7-bitcoin-block-{height}") as writer:
                    old_ids = writer.load_event_ids_for_block_range("BITCOIN", height, height)
                    if old_ids:
                        writer.persist_reorg_recovery(old_ids, events, state.to_row(), processed_at=utc_now())
                    else:
                        writer.persist_transfer_chunks_and_checkpoint(events, state.to_row())
            else:
                self._source_stages["bitcoin_rpc"] = "EVENT_VALUATION"
                priced_events = self._iter_event_time_prices(repository, events, "BTCUSDT")
                self._source_stages["bitcoin_rpc"] = "BLOCK_PERSISTENCE"
                with self._database_write_scope(repository, f"phase7-bitcoin-block-{height}") as writer:
                    writer.persist_transfer_chunks_and_checkpoint(priced_events, state.to_row())
                del priced_events
            self._diagnostics["bitcoin_pending_persistence_chunks"] = 0
            self._publish_diagnostics()
            return len(events)

    async def _bitcoin_cycle(self) -> tuple[DataStatus, dict[str, Any]]:
        config = self.settings.phase7_bitcoin_rpc
        self._diagnostics["bitcoin_pending_blocks"] = 0
        self._diagnostics["bitcoin_active_block_height"] = 0
        self._diagnostics["bitcoin_block_event_count"] = 0
        self._diagnostics["bitcoin_pending_persistence_chunks"] = 0
        async with self._rpc_session() as session:
            client = Phase7JsonRpcClient(config, session)
            self._active_rpc_clients["bitcoin_rpc"] = client
            self._source_stages["bitcoin_rpc"] = "TIP_RPC"
            info = await client.call("getblockchaininfo")
            if not isinstance(info, dict) or info.get("chain") != "main":
                raise Phase7RpcError("bitcoin_rpc: endpoint is not Bitcoin mainnet")
            tip = info.get("blocks")
            if isinstance(tip, bool) or not isinstance(tip, int) or tip < 1:
                raise Phase7RpcError("bitcoin_rpc: invalid mainnet height")
            parser = BitcoinBlockParser()
            now = utc_now()
            processed = 0
            event_count = 0
            with self._repository_scope() as repository:
                self._source_stages["bitcoin_rpc"] = "CHECKPOINT_READ"
                repository.upsert_asset_registry(self._asset_registry_rows())
                checkpoint = repository.load_checkpoint("btc_core_rpc", "CHAIN", "BITCOIN")
                current = int(checkpoint["cursor_value"]) if checkpoint else None
                finalized_height = max(0, tip - parser.finalized_depth)
                if current is None:
                    start = max(0, finalized_height - 144 + 1)
                else:
                    start = current + 1
                end = min(finalized_height, start + 11)
                self._diagnostics["bitcoin_pending_blocks"] = max(0, end - start + 1)
                if current is not None and current <= finalized_height:
                    self._diagnostics["bitcoin_active_block_height"] = current
                    self._source_stages["bitcoin_rpc"] = "REORG_HASH_RPC"
                    expected_hash = await client.call("getblockhash", [current])
                    if expected_hash != checkpoint["last_block_hash"]:
                        self._source_stages["bitcoin_rpc"] = "REORG_BLOCK_RPC"
                        block = await client.call("getblock", [expected_hash, 2])
                        event_count += await self._process_bitcoin_block(
                            repository,
                            parser,
                            block,
                            block_hash=str(expected_hash),
                            height=current,
                            observed_at=now,
                            fetched_at=utc_now(),
                            response_bytes=client.last_response_bytes,
                            reorg=True,
                        )
                        del block
                for height in range(start, end + 1):
                    self._diagnostics["bitcoin_active_block_height"] = height
                    self._source_stages["bitcoin_rpc"] = "BLOCK_HASH_RPC"
                    block_hash = await client.call("getblockhash", [height])
                    self._source_stages["bitcoin_rpc"] = "BLOCK_RPC"
                    block = await client.call("getblock", [block_hash, 2])
                    fetched = utc_now()
                    self._publish_diagnostics()
                    block_event_count = await self._process_bitcoin_block(
                        repository,
                        parser,
                        block,
                        block_hash=str(block_hash),
                        height=height,
                        observed_at=now,
                        fetched_at=fetched,
                        response_bytes=client.last_response_bytes,
                    )
                    del block
                    event_count += block_event_count
                    processed += 1
                    self._diagnostics["bitcoin_pending_blocks"] = max(0, end - height)
                    self._publish_diagnostics()
                self._diagnostics["bitcoin_pending_blocks"] = 0
                self._diagnostics["bitcoin_active_block_height"] = 0
            self._source_stages["bitcoin_rpc"] = "CYCLE_COMPLETE"
            return DataStatus.AVAILABLE, {
                "chain": "BITCOIN_MAINNET", "head_cursor": tip,
                "last_finalized_cursor": finalized_height, "cursor": current if not processed else end,
                "processed_blocks": processed, "persisted_events": event_count,
            }

    async def _persist_ethereum_block(
        self,
        repository,
        events,
        *,
        height: int,
        block_hash: str,
        reorg: bool = False,
    ) -> int:
        request = make_work_request(
            phase=SourcePhase.PHASE7,
            source_id=SourceId.PHASE7_ONCHAIN,
            work_class=WorkClass.HEAVY,
            estimated_items=max(1, len(events)),
            estimated_bytes=max(65_536, len(events) * 2_048),
            replay_class=ReplayClass.RECOVERABLE_REPLAYABLE,
            cancellation_owner=ProcessRole.COLLECTOR,
            stream_id="phase7.ethereum_blocks",
            timeout_seconds=2.0,
        )
        async with self.admission.admit(request):
            state = self._checkpoint(
                "ethereum_rpc", "ETHEREUM", "BLOCK", height, block_hash,
                "phase7-eth-rpc-v1", DataStatus.AVAILABLE, None,
            )
            if reorg:
                with self._database_write_scope(repository, f"phase7-ethereum-block-{height}") as writer:
                    old_ids = writer.load_event_ids_for_block_range("ETHEREUM", height, height)
                    if old_ids:
                        writer.persist_reorg_recovery(old_ids, events, state.to_row(), processed_at=utc_now())
                    else:
                        writer.persist_transfer_batch_and_checkpoint(events, state.to_row())
            else:
                valued_events = self._apply_event_time_prices(repository, events, "ETHUSDT")
                with self._database_write_scope(repository, f"phase7-ethereum-block-{height}") as writer:
                    writer.persist_transfer_batch_and_checkpoint(valued_events, state.to_row())
            return len(events)

    async def _ethereum_cycle(self) -> tuple[DataStatus, dict[str, Any]]:
        config = self.settings.phase7_ethereum_rpc
        self._diagnostics["ethereum_backfill_depth"] = 0
        self._diagnostics["ethereum_active_block_height"] = 0
        self._diagnostics["ethereum_pending_logs"] = 0
        self._diagnostics["ethereum_pending_receipts"] = 0
        self._diagnostics["ethereum_block_event_count"] = 0
        async with self._rpc_session() as session:
            client = Phase7JsonRpcClient(config, session)
            self._active_rpc_clients["ethereum_rpc"] = client
            self._source_stages["ethereum_rpc"] = "CHAIN_ID_RPC"
            self._diagnostics["ethereum_pending_logs"] = 0
            self._diagnostics["ethereum_pending_receipts"] = 0
            chain_id = await client.call("eth_chainId")
            if chain_id != "0x1":
                raise Phase7RpcError("ethereum_rpc: endpoint is not Ethereum mainnet")
            self._source_stages["ethereum_rpc"] = "HEAD_RPC"
            latest_hex = await client.call("eth_blockNumber")
            latest = self._hex_quantity(latest_hex, "Ethereum head")
            self._source_stages["ethereum_rpc"] = "FINALIZED_RPC"
            finalized = await client.call("eth_getBlockByNumber", ["finalized", False])
            if not isinstance(finalized, dict):
                raise Phase7RpcError("ethereum_rpc: finalized block unavailable")
            finalized_height = self._hex_quantity(finalized.get("number"), "Ethereum finalized height")
            parser = EthereumBlockParser()
            now = utc_now()
            processed = 0
            event_count = 0
            with self._repository_scope() as repository:
                self._source_stages["ethereum_rpc"] = "CHECKPOINT_READ"
                repository.upsert_asset_registry(self._asset_registry_rows())
                checkpoint = repository.load_checkpoint("ethereum_rpc", "CHAIN", "ETHEREUM")
                current = int(checkpoint["cursor_value"]) if checkpoint else None
                start = max(0, finalized_height - 120 + 1) if current is None else current + 1
                end = min(finalized_height, start + 119)
                self._diagnostics["ethereum_backfill_depth"] = max(0, end - start + 1)
                if current is not None and current <= finalized_height:
                    self._diagnostics["ethereum_active_block_height"] = current
                    self._source_stages["ethereum_rpc"] = "REORG_CHECK_RPC"
                    canonical = await client.call("eth_getBlockByNumber", [hex(current), False])
                    if not isinstance(canonical, dict) or canonical.get("hash") != checkpoint["last_block_hash"]:
                        block_hash = canonical.get("hash") if isinstance(canonical, dict) else None
                        if not isinstance(canonical, dict) or not isinstance(block_hash, str):
                            raise Phase7RpcError("ethereum_rpc: reorg block unavailable")
                        block, events = await self._ethereum_block(client, parser, current, chain_id, now, block_hash)
                        self._diagnostics["ethereum_block_event_count"] = len(events)
                        self._source_stages["ethereum_rpc"] = "BLOCK_PERSISTENCE"
                        self._publish_diagnostics()
                        event_count += await self._persist_ethereum_block(
                            repository, events, height=current, block_hash=block_hash, reorg=True
                        )
                        await self._record_ethereum_progress_health(
                            current, latest, finalized_height, len(events)
                        )
                for height in range(start, end + 1):
                    self._diagnostics["ethereum_active_block_height"] = height
                    block, events = await self._ethereum_block(client, parser, height, chain_id, now)
                    self._diagnostics["ethereum_block_event_count"] = len(events)
                    block_hash = str(block["hash"])
                    self._source_stages["ethereum_rpc"] = "BLOCK_PERSISTENCE"
                    self._publish_diagnostics()
                    event_count += await self._persist_ethereum_block(
                        repository, events, height=height, block_hash=block_hash
                    )
                    processed += 1
                    await self._record_ethereum_progress_health(
                        height, latest, finalized_height, len(events)
                    )
                    self._diagnostics["ethereum_backfill_depth"] = max(0, end - height)
                    self._publish_diagnostics()
                self._diagnostics["ethereum_backfill_depth"] = 0
                self._diagnostics["ethereum_active_block_height"] = 0
            self._source_stages["ethereum_rpc"] = "CYCLE_COMPLETE"
            return DataStatus.AVAILABLE, {
                "chain": "ETHEREUM_MAINNET", "head_cursor": latest,
                "last_finalized_cursor": finalized_height, "cursor": current if not processed else end,
                "processed_blocks": processed, "persisted_events": event_count,
            }

    async def _record_ethereum_progress_health(
        self,
        height: int,
        latest: int,
        finalized_height: int,
        block_event_count: int,
    ) -> None:
        await self._write_health("ethereum_rpc", HealthDataStatus.AVAILABLE, utc_now(), {
            "chain": "ETHEREUM_MAINNET",
            "runtime_state": "RUNNING",
            "phase7_status": DataStatus.AVAILABLE.value,
            "cursor": height,
            "head_cursor": latest,
            "last_finalized_cursor": finalized_height,
            "persisted_events": block_event_count,
            "progress_source": "BLOCK_CHECKPOINT_COMMITTED",
        })

    async def _ethereum_block(self, client, parser, height: int, chain_id: str, observed_at: datetime, expected_hash: str | None = None):
        response_bytes_before = client.response_bytes_total
        self._diagnostics["ethereum_active_block_height"] = height
        self._source_stages["ethereum_rpc"] = "BLOCK_RPC"
        block = await client.call("eth_getBlockByNumber", [hex(height), True])
        if not isinstance(block, dict) or not isinstance(block.get("transactions"), list):
            raise Phase7RpcError("ethereum_rpc: block payload unavailable")
        block_hash = block.get("hash")
        if not isinstance(block_hash, str) or (expected_hash is not None and block_hash != expected_hash):
            raise Phase7RpcError("ethereum_rpc: canonical block hash changed")
        self._source_stages["ethereum_rpc"] = "LOGS_REQUEST"
        self._diagnostics["ethereum_pending_logs"] = 1
        transfers = await client.call("eth_getLogs", [{
            "fromBlock": hex(height), "toBlock": hex(height), "address": list(DEFAULT_ETHEREUM_ASSETS),
            "topics": [TRANSFER_TOPIC],
        }])
        if not isinstance(transfers, list) or len(transfers) > 2_000:
            raise Phase7RpcError("ethereum_rpc: transfer log batch exceeds cap")
        self._diagnostics["ethereum_pending_logs"] = len(transfers)
        self._source_stages["ethereum_rpc"] = "LOGS_BUFFERED"
        self._publish_diagnostics()
        logs_by_tx: dict[str, list[dict[str, Any]]] = {}
        for row in transfers:
            if not isinstance(row, dict) or not isinstance(row.get("transactionHash"), str):
                raise Phase7RpcError("ethereum_rpc: transfer log schema mismatch")
            logs_by_tx.setdefault(row["transactionHash"], []).append(row)
        self._source_stages["ethereum_rpc"] = "RECEIPT_CANDIDATE_ADMISSION"
        candidates = []
        for tx in block["transactions"]:
            if not isinstance(tx, dict):
                raise Phase7RpcError("ethereum_rpc: transaction schema mismatch")
            value = self._hex_quantity(tx.get("value"), "Ethereum transaction value")
            if value > 0 or tx.get("hash") in logs_by_tx:
                candidates.append(tx)
                if len(candidates) > _ETHEREUM_RECEIPT_CANDIDATE_LIMIT:
                    raise Phase7RpcError("ethereum_rpc: receipt candidate count exceeds per-block cap")
        self._source_stages["ethereum_rpc"] = "RECEIPTS_REQUEST"
        self._diagnostics["ethereum_pending_receipts"] = len(candidates)
        self._publish_diagnostics()
        receipt_payloads = []
        for offset in range(0, len(candidates), _ETHEREUM_RECEIPT_BATCH_SIZE):
            batch = candidates[offset : offset + _ETHEREUM_RECEIPT_BATCH_SIZE]
            receipt_payloads.extend(await client.call_batch([
                ("eth_getTransactionReceipt", [tx["hash"]]) for tx in batch
            ]))
        self._diagnostics["ethereum_pending_receipts"] = 0
        valid_transactions = []
        receipts = []
        for tx, receipt in zip(candidates, receipt_payloads, strict=True):
            if not isinstance(receipt, dict):
                raise Phase7RpcError("ethereum_rpc: relevant receipt unavailable")
            receipt_status = self._hex_quantity(receipt.get("status"), "Ethereum receipt status")
            if receipt_status == 0:
                if tx.get("hash") in logs_by_tx:
                    raise Phase7RpcError("ethereum_rpc: reverted receipt contains a transfer log")
                continue
            valid_transactions.append(tx)
            receipts.append(receipt)
        valid_hashes = {tx.get("hash") for tx in valid_transactions}
        selected_logs = [row for tx_hash, rows in logs_by_tx.items() if tx_hash in valid_hashes for row in rows]
        filtered_block = {**block, "transactions": valid_transactions}
        fetched_at = utc_now()
        self._source_stages["ethereum_rpc"] = "BLOCK_PARSE"
        self._publish_diagnostics()
        request = make_work_request(
            phase=SourcePhase.PHASE7,
            source_id=SourceId.PHASE7_ONCHAIN,
            work_class=WorkClass.HEAVY,
            estimated_items=max(1, len(valid_transactions) + len(selected_logs)),
            estimated_bytes=max(65_536, (client.response_bytes_total - response_bytes_before) * 2),
            replay_class=ReplayClass.RECOVERABLE_REPLAYABLE,
            cancellation_owner=ProcessRole.COLLECTOR,
            stream_id="phase7.ethereum_blocks",
            timeout_seconds=2.0,
        )
        async with self.admission.admit(request):
            events = parser.parse_block(
                filtered_block,
                chain_id=chain_id,
                logs=selected_logs,
                receipts=receipts,
                finality_tag="finalized",
                expected_block_hash=expected_hash,
                observed_at=observed_at,
                fetched_at=fetched_at,
                processed_at=utc_now(),
            )
        self._diagnostics["ethereum_pending_logs"] = 0
        self._diagnostics["ethereum_block_event_count"] = len(events)
        self._source_stages["ethereum_rpc"] = "BLOCK_PARSED"
        self._publish_diagnostics()
        return block, events

    async def _spot_cycle(self) -> tuple[DataStatus, dict[str, Any]]:
        import aiohttp

        adapter = BinanceSpotAdapter()
        now = utc_now()
        persisted = 0
        symbols = 0
        received = parsed = 0
        self._diagnostics["spot_active_rest_requests"] = 0
        self._diagnostics["spot_aggregation_active"] = 0
        self._source_stages["binance_spot"] = "REST_REQUEST"
        async with aiohttp.ClientSession() as session:
            with self._repository_scope() as repository:
                for symbol in ("BTCUSDT", "ETHUSDT"):
                    checkpoint = repository.load_checkpoint("binance_spot", "EXCHANGE", symbol)
                    kwargs: dict[str, Any] = {"limit": 1000}
                    if checkpoint:
                        kwargs["from_id"] = int(checkpoint["cursor_value"]) + 1
                    else:
                        kwargs["start_time_ms"] = int((now.timestamp() - 300) * 1000)
                    path, params = adapter.rest_request(symbol, **kwargs)
                    timeout = aiohttp.ClientTimeout(total=5, connect=1)
                    self._diagnostics["spot_active_rest_requests"] = 1
                    try:
                        async with session.get("https://api.binance.com" + path, params=params, timeout=timeout) as response:
                            self._source_stages["binance_spot"] = "REST_HTTP_RESPONSE"
                            if response.status in {408, 425, 429, 500, 502, 503, 504}:
                                if response.status == 429:
                                    raise Phase7RateLimitedError("binance_spot: RATE_LIMITED")
                                raise Phase7TransientRpcError("binance_spot: retryable HTTP status")
                            if response.status < 200 or response.status >= 300:
                                raise Phase7RpcError(f"binance_spot: HTTP_{response.status}")
                            body = await _read_bounded_body(response, 8 * 1024 * 1024, "binance_spot")
                            self._diagnostics["spot_rest_response_bytes"] = len(body)
                    finally:
                        self._diagnostics["spot_active_rest_requests"] = 0
                    self._source_stages["binance_spot"] = "REST_PARSE"
                    payload = json.loads(body)
                    events = adapter.parse_rest_response(payload, symbol=symbol, fetched_at=now, processed_at=utc_now())
                    received += 1
                    parsed += len(events)
                    pending = self._spot_events[symbol]
                    seen = self._spot_event_keys[symbol]
                    for event in events:
                        if event.identity not in seen:
                            if len(pending) >= 3_000:
                                raise Phase7RpcError("binance_spot: bounded recovery buffer is full")
                            pending.append(event)
                            seen.add(event.identity)
                    symbols += 1
                    self._source_stages["binance_spot"] = "WINDOW_AGGREGATION"
                    self._diagnostics["spot_aggregation_active"] = 1
                    self._publish_diagnostics()
                    try:
                        persisted += self._persist_closed_spot_windows(repository, symbol, pending, checkpoint, now)
                    finally:
                        self._diagnostics["spot_aggregation_active"] = 0
                        self._publish_diagnostics()
                    self._source_stages["binance_spot"] = "PERSISTENCE"
        self._source_stages["binance_spot"] = "COMPLETE"
        return DataStatus.AVAILABLE, {
            "exchange": "BINANCE_SPOT", "symbols": symbols, "rest_responses": received,
            "parsed_trades": parsed, "persisted_windows": persisted,
            "pending_trades": sum(len(items) for items in self._spot_events.values()),
            "websocket_metrics": dict(self._spot_ws_metrics),
            "runtime_stage": "PERSISTED" if persisted else "NO_CLOSED_WINDOW_READY",
        }

    def ingest_binance_ws_message(self, payload: Mapping[str, Any]):
        event = BinanceSpotAdapter().parse_ws_message(payload, fetched_at=utc_now(), processed_at=utc_now())
        pending = self._spot_events[event.symbol]
        seen = self._spot_event_keys[event.symbol]
        if event.identity in seen:
            return None
        if len(pending) >= 3_000:
            raise Phase7RpcError("binance_spot: bounded WebSocket recovery buffer is full")
        pending.append(event)
        seen.add(event.identity)
        return event

    async def _spot_ws_supervisor(self, stop_event: asyncio.Event) -> None:
        import aiohttp

        adapter = BinanceSpotAdapter()
        attempt = 0
        async with aiohttp.ClientSession() as session:
            while not stop_event.is_set():
                try:
                    connector = self.spot_ws_connector or (
                        lambda client_session, endpoint: client_session.ws_connect(
                            endpoint, heartbeat=30, timeout=5, max_msg_size=1_048_576,
                        )
                    )
                    async with connector(session, adapter.ws_endpoint) as websocket:
                        self._source_stages["binance_spot_websocket"] = "CONNECTED"
                        self._diagnostics["spot_ws_connected"] = 1
                        for index, symbol in enumerate(("BTCUSDT", "ETHUSDT"), start=1):
                            await websocket.send_json(adapter.subscription(symbol, request_id=index))
                        self._source_stages["binance_spot_websocket"] = "SUBSCRIPTIONS_SENT"
                        await self._write_health("binance_spot_websocket", HealthDataStatus.AVAILABLE, utc_now(), {
                            "runtime_state": "RUNNING", "runtime_stage": "SUBSCRIPTIONS_SENT",
                            "phase7_status": "AVAILABLE", "consecutive_failures": 0,
                            "subscription_count": 2, **self._spot_ws_metrics,
                        })
                        self._source_consecutive_failures["binance_spot_websocket"] = 0
                        reader = asyncio.create_task(self._read_spot_ws(websocket, stop_event))
                        stopper = asyncio.create_task(stop_event.wait())
                        done, pending = await asyncio.wait(
                            {reader, stopper}, return_when=asyncio.FIRST_COMPLETED,
                        )
                        if stopper in done:
                            await websocket.close()
                        if reader in done and not stop_event.is_set():
                            exception = reader.exception()
                            if exception is not None:
                                raise exception
                            raise Phase7RpcError("binance_spot: WebSocket ended")
                        for task in pending:
                            task.cancel()
                        await asyncio.gather(*pending, return_exceptions=True)
                        await asyncio.gather(reader, stopper, return_exceptions=True)
                    self._diagnostics["spot_ws_connected"] = 0
                    attempt = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - provider error text may contain endpoint data
                    self._diagnostics["spot_ws_connected"] = 0
                    if self._is_transient_network_error(exc):
                        await self._record_transient_failure(
                            "binance_spot_websocket",
                            reason="NETWORK_TIMEOUT" if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) else "TRANSPORT_FAILURE",
                            error_category="NETWORK",
                            failure_stage="WEBSOCKET_TRANSPORT",
                        )
                    else:
                        await self._write_health("binance_spot_websocket", HealthDataStatus.ERROR, utc_now(), {
                            "runtime_state": "DEGRADED", "reason": type(exc).__name__,
                            "phase7_status": "ERROR", "error_category": "PROVIDER_CONTRACT",
                            "failure_stage": self._source_stages.get("binance_spot_websocket", "UNKNOWN"),
                            **self._spot_ws_metrics,
                        })
                    LOGGER.warning("phase7_spot_websocket_failed exception=%s", type(exc).__name__)
                    attempt += 1
                    delay = min(30.0, float(2 ** min(attempt - 1, 5)))
                    try:
                        await asyncio.wait_for(stop_event.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        continue
            await self._write_health("binance_spot_websocket", HealthDataStatus.NOT_AVAILABLE, utc_now(), {
                "runtime_state": "STOPPED",
                "runtime_stage": "STOPPED",
                **self._spot_ws_metrics,
            })

    async def _read_spot_ws(self, websocket: Any, stop_event: asyncio.Event) -> None:
        import aiohttp

        while not stop_event.is_set():
            message = await websocket.receive()
            if message.type is aiohttp.WSMsgType.TEXT:
                self._spot_ws_metrics["text_messages"] += 1
                try:
                    payload = json.loads(message.data)
                except (TypeError, json.JSONDecodeError):
                    raise Phase7RpcError("binance_spot: invalid WebSocket JSON") from None
                if isinstance(payload, dict) and payload.get("e") == "aggTrade":
                    self._spot_ws_metrics["agg_trade_messages"] += 1
                    accepted = self.ingest_binance_ws_message(payload)
                    self._spot_ws_metrics["accepted_trades" if accepted is not None else "duplicate_trades"] += 1
                elif isinstance(payload, dict) and payload.get("id") in {1, 2} and payload.get("result") is None:
                    self._spot_ws_metrics["subscription_acks"] += 1
                elif isinstance(payload, dict) and "code" in payload and payload.get("code") not in {0, "0"}:
                    raise Phase7RpcError("binance_spot: WebSocket subscription rejected")
            elif message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR}:
                raise Phase7TransientRpcError("binance_spot: WebSocket disconnected")

    @staticmethod
    def _is_transient_network_error(exc: Exception) -> bool:
        if isinstance(exc, (Phase7TransientRpcError, TimeoutError, asyncio.TimeoutError, ConnectionError, OSError)):
            return True
        try:
            import aiohttp
        except ImportError:
            return False
        return isinstance(exc, aiohttp.ClientError)

    def _persist_closed_spot_windows(self, repository, symbol: str, pending: list[Any], checkpoint, now: datetime) -> int:
        cutoff = now.timestamp() - 30
        eligible = [event for event in pending if event.event_timestamp.timestamp() < cutoff]
        if not eligible:
            return 0
        minute_starts = sorted({event.event_timestamp.replace(second=0, microsecond=0) for event in eligible})
        rows = []
        cursor_event = None
        previous_cvd = repository.load_latest_spot_cvd("binance", symbol)
        for window_open in minute_starts:
            items = [event for event in eligible if window_open <= event.event_timestamp < window_open + timedelta(minutes=1)]
            if not items:
                continue
            window = aggregate_spot_window(
                items, window_open=window_open, timeframe="1m", now=now,
                late_arrival_grace=timedelta(minutes=5),
                expected_cadence=timedelta(seconds=15),
                freshness_grace=timedelta(seconds=30),
                previous_cvd=previous_cvd,
            )
            rows.append(window.to_row(processed_at=utc_now(), created_at=utc_now()))
            if window.cvd is not None:
                previous_cvd = window.cvd
            cursor_event = max(items, key=lambda event: int(event.trade_id))
        if cursor_event is None:
            return 0
        cursor_value = cursor_event.trade_id
        state = self._checkpoint(
            "binance_spot", symbol, "TRADE_ID", int(cursor_value), None,
            "phase7-binance-spot-v1", DataStatus.AVAILABLE, None, scope_kind="EXCHANGE",
        )
        repository.persist_spot_windows_and_checkpoint(rows, state.to_row())
        persisted_ids = {event.identity for event in eligible if int(event.trade_id) <= int(cursor_value)}
        pending[:] = [event for event in pending if event.identity not in persisted_ids]
        self._spot_event_keys[symbol].difference_update(persisted_ids)
        return len(rows)

    @staticmethod
    def _checkpoint(source_id, scope_key, cursor_kind, cursor, block_hash, parser_version, status, reason, *, scope_kind="CHAIN"):
        value = str(cursor)
        return CheckpointState(
            source_id=source_id, scope_kind=scope_kind, scope_key=scope_key,
            cursor_kind=cursor_kind, cursor_value=value,
            last_observed_cursor=value, last_finalized_cursor=value,
            last_block_hash=block_hash, parser_version=parser_version,
            schema_version="phase7-onchain-v1", status=status, reason=reason, updated_at=utc_now(),
        )

    @staticmethod
    def _asset_registry_rows() -> tuple[dict[str, Any], ...]:
        now = utc_now()
        effective_from = datetime(2020, 1, 1, tzinfo=timezone.utc)
        definitions = [
            ("BITCOIN:NATIVE:NATIVE:btc-v1", "BITCOIN", "NATIVE", None, "BTC", 8, "btc-v1"),
            ("ETHEREUM:NATIVE:NATIVE:ethereum-native-v1", "ETHEREUM", "NATIVE", None, "ETH", 18, "ethereum-native-v1"),
        ]
        for address, definition in DEFAULT_ETHEREUM_ASSETS.items():
            symbol = str(definition["symbol"])
            version = str(definition["registry_version"])
            definitions.append((
                f"ETHEREUM:ERC20:{address}:{version}", "ETHEREUM", "ERC20", address,
                symbol, int(definition["decimals"]), version,
            ))
        snapshot = json.dumps(definitions, separators=(",", ":"), sort_keys=False)
        digest = hashlib.sha256(snapshot.encode()).hexdigest()
        rows = []
        for asset_id, chain, kind, address, symbol, decimals, version in definitions:
            rows.append({
                "asset_id": asset_id,
                "chain": chain,
                "asset_kind": kind,
                "contract_address": address,
                "symbol": symbol,
                "decimals": decimals,
                "registry_version": version,
                "effective_from": effective_from,
                "effective_to": None,
                "source_id": "phase7-asset-registry",
                "source_version": "phase7-assets-v1",
                "source_reference": (
                    "https://developer.bitcoin.org/reference/rpc/" if chain == "BITCOIN"
                    else "https://ethereum.org/developers/docs/standards/tokens/erc-20/"
                ),
                "snapshot_hash": digest,
                "status": "AVAILABLE",
                "created_at": now,
                "updated_at": now,
            })
        return tuple(rows)

    @staticmethod
    def _hex_quantity(value: Any, field: str) -> int:
        if not isinstance(value, str) or not value.startswith("0x") or len(value) < 3:
            raise ValueError(f"{field} is invalid")
        try:
            return int(value, 16)
        except ValueError:
            raise ValueError(f"{field} is invalid") from None

    @staticmethod
    def _apply_event_time_prices(repository, events, symbol: str):
        result = []
        for event in events:
            if event.status is not DataStatus.AVAILABLE or event.identity.asset_id.kind is not AssetKind.NATIVE:
                result.append(event)
                continue
            quote = repository.load_event_time_price(symbol, event.event_time, max_skew_seconds=300)
            if quote is None:
                result.append(event)
                continue
            try:
                price = Decimal(str(quote["price"]))
                if not price.is_finite() or price <= 0:
                    result.append(event)
                    continue
                with localcontext() as context:
                    context.prec = 160
                    amount_usd = event.amount.amount_normalized * price
                skew = event.event_time - quote["exchange_timestamp"]
                valuation = UsdValuation(
                    asset_id=event.identity.asset_id, event_time=event.event_time,
                    amount_usd=amount_usd, valuation_price=price,
                    valuation_exchange=quote["exchange"], valuation_source=quote["source"],
                    valuation_symbol=symbol, market_kind=MarketKind.SPOT,
                    valuation_exchange_timestamp=quote["exchange_timestamp"],
                    valuation_fetched_at=quote["fetched_at"], valuation_skew=skew,
                    max_valuation_skew=timedelta(seconds=300),
                    status=DataStatus.AVAILABLE,
                )
                result.append(replace(event, valuation=valuation))
            except (ArithmeticError, TypeError, ValueError):
                result.append(event)
        return tuple(result)

    @staticmethod
    def _iter_event_time_prices(repository, events, symbol: str):
        """Apply one block-time quote lazily, without materializing a second event list."""
        priceable = next((
            event for event in events
            if event.status is DataStatus.AVAILABLE
            and event.identity.asset_id.kind is AssetKind.NATIVE
        ), None)
        quote = (
            repository.load_event_time_price(symbol, priceable.event_time, max_skew_seconds=300)
            if priceable is not None else None
        )
        if priceable is not None and any(
            event.status is DataStatus.AVAILABLE
            and event.identity.asset_id.kind is AssetKind.NATIVE
            and event.event_time != priceable.event_time
            for event in events
        ):
            raise ValueError("one source block cannot contain multiple native event times")

        def apply(event):
            if (
                event.status is not DataStatus.AVAILABLE
                or event.identity.asset_id.kind is not AssetKind.NATIVE
                or priceable is None
                or event.event_time != priceable.event_time
                or quote is None
            ):
                return event
            try:
                price = Decimal(str(quote["price"]))
                if not price.is_finite() or price <= 0:
                    return event
                with localcontext() as context:
                    context.prec = 160
                    amount_usd = event.amount.amount_normalized * price
                skew = event.event_time - quote["exchange_timestamp"]
                valuation = UsdValuation(
                    asset_id=event.identity.asset_id, event_time=event.event_time,
                    amount_usd=amount_usd, valuation_price=price,
                    valuation_exchange=quote["exchange"], valuation_source=quote["source"],
                    valuation_symbol=symbol, market_kind=MarketKind.SPOT,
                    valuation_exchange_timestamp=quote["exchange_timestamp"],
                    valuation_fetched_at=quote["fetched_at"], valuation_skew=skew,
                    max_valuation_skew=timedelta(seconds=300),
                    status=DataStatus.AVAILABLE,
                )
                return replace(event, valuation=valuation)
            except (ArithmeticError, TypeError, ValueError):
                return event

        return map(apply, events)

    async def _persist_configuration_health(self) -> None:
        now = utc_now()
        for source_id, details in phase7_source_diagnostics(self.settings).items():
            status = HealthDataStatus.ERROR if details["source_status"] == SourceConfigurationStatus.SOURCE_CONFIG_INVALID else HealthDataStatus.NOT_AVAILABLE
            await self._write_health(source_id, status, now, {
                **details,
                "runtime_state": "STARTING" if details["configured"] else "NOT_CONFIGURED",
            })

    async def _mark_stopped(self) -> None:
        now = utc_now()
        for source_id in ("bitcoin_rpc", "ethereum_rpc", "binance_spot", "binance_spot_websocket"):
            await self._write_health(source_id, HealthDataStatus.NOT_AVAILABLE, now, {
                "runtime_state": "STOPPED",
                "source_status": "STOPPED",
            })

    async def _write_health(
        self,
        component: str,
        status: HealthDataStatus,
        checked_at: datetime,
        details: dict[str, Any],
    ) -> None:
        safe_details = {**details, **self.diagnostics_snapshot()}
        self.health_registry.set(ComponentHealth(component, status, checked_at, safe_details))
        normalized_status = DataStatus(status.value)
        tracker_details = {
            key: value for key, value in {
                "heartbeat": safe_details.get("runtime_state") not in {"STOPPED", "DEGRADED"},
                "reason": safe_details.get("reason") or safe_details.get("source_status"),
                "cursor": safe_details.get("cursor"),
                "head_cursor": safe_details.get("head_cursor"),
                "lag_cursor": safe_details.get("lag_cursor"),
                "queue_depth": safe_details.get("queue_depth"),
                "queue_capacity": safe_details.get("queue_capacity"),
                "phase7_status": safe_details.get("phase7_status"),
                "data_quality": safe_details.get("data_quality"),
                "error_category": safe_details.get("error_category"),
                "consecutive_failures": safe_details.get("consecutive_failures"),
                "failure_stage": safe_details.get("failure_stage"),
                "retry_policy": safe_details.get("retry_policy"),
            }.items() if value is not None
        }
        try:
            if self.health_writer is not None:
                self.health_tracker.update(component, normalized_status, checked_at, tracker_details)
                self.health_writer(component, status, checked_at, safe_details)
                return
            import psycopg

            from quant_phase1.repositories import Phase1Repository

            factory = self.connection_factory or psycopg.connect
            with factory(self.settings.postgres_dsn) as connection:
                self.health_tracker.repository = Phase1Repository(connection)
                self.health_tracker.update(component, normalized_status, checked_at, tracker_details)
                self.health_tracker.repository = None
        except Exception as exc:  # noqa: BLE001 - health failure must not leak a DSN or stop Collector
            LOGGER.warning(
                "phase7_health_persist_failed component=%s exception=%s",
                component,
                type(exc).__name__,
            )


class Phase7EngineRuntime:
    """Small owned heartbeat/retention lifecycle for Phase 7 context work."""

    def __init__(
        self,
        settings: Settings,
        *,
        connection_factory: Callable[[str], Any] | None = None,
        health_writer: Callable[[str, HealthDataStatus, datetime, dict[str, Any]], None] | None = None,
        heartbeat_seconds: float = 30.0,
        retention_seconds: float = 3_600.0,
    ) -> None:
        self.settings = settings
        self.connection_factory = connection_factory
        self.health_writer = health_writer
        self.heartbeat_seconds = max(0.01, heartbeat_seconds)
        self.retention_seconds = max(self.heartbeat_seconds, retention_seconds)
        self._running = False
        self._processed_context_windows: dict[tuple[str, str, str, datetime], datetime | None] = {}
        self._persisted_label_hashes: set[str] = set()
        self._context_config_signature: tuple[Any, ...] | None = None

    async def run(self, stop_event: asyncio.Event) -> None:
        if self._running:
            raise RuntimeError("Phase 7 Engine runtime is already running")
        self._running = True
        try:
            await self._write_health(HealthDataStatus.NOT_AVAILABLE, {"runtime_state": "ACTIVE", "reason": "CONTEXT_ONLY"})
            last_retention = asyncio.get_running_loop().time()
            last_context = asyncio.get_running_loop().time() - self.settings.phase7_context_interval_seconds
            while not stop_event.is_set():
                loop_now = asyncio.get_running_loop().time()
                if (
                    self.settings.phase7_enabled
                    and loop_now - last_context >= self.settings.phase7_context_interval_seconds
                ):
                    try:
                        result = await asyncio.to_thread(self._run_context_cycle)
                        status = (
                            HealthDataStatus.ERROR if result.get("config_error") else
                            HealthDataStatus.PARTIAL if result.get("overflow_assets") else
                            HealthDataStatus.AVAILABLE if result.get("events", 0) else
                            HealthDataStatus.NOT_AVAILABLE
                        )
                        await self._write_health(status, {
                            "runtime_state": "ACTIVE" if status is not HealthDataStatus.ERROR else "DEGRADED",
                            **result,
                        })
                    except Exception as exc:  # noqa: BLE001 - isolate context work from Stage1
                        LOGGER.warning("phase7_context_cycle_failed exception=%s", type(exc).__name__)
                        await self._write_health(HealthDataStatus.ERROR, {
                            "runtime_state": "DEGRADED", "reason": type(exc).__name__,
                        })
                    last_context = asyncio.get_running_loop().time()
                if (
                    self.settings.phase7_enabled
                    and asyncio.get_running_loop().time() - last_retention >= self.retention_seconds
                ):
                    await self._run_retention()
                    last_retention = asyncio.get_running_loop().time()
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.heartbeat_seconds)
                except asyncio.TimeoutError:
                    await self._write_health(HealthDataStatus.NOT_AVAILABLE, {
                        "runtime_state": "ACTIVE",
                        "reason": "CONTEXT_ONLY",
                    })
        finally:
            await self._write_health(HealthDataStatus.NOT_AVAILABLE, {"runtime_state": "STOPPED"})
            self._running = False

    def _run_context_cycle(self) -> dict[str, Any]:
        from .context_config import load_reviewed_label_snapshots, load_whale_thresholds
        from .context_pipeline import WINDOWS
        from .context_service import aggregate_closed_context_window, closed_window_open

        try:
            snapshots = load_reviewed_label_snapshots(self.settings.phase7_address_labels_path)
            thresholds = load_whale_thresholds(self.settings.phase7_whale_thresholds_path)
        except Exception as exc:  # noqa: BLE001 - never log config contents or paths
            return {
                "config_error": type(exc).__name__, "events": 0, "assets": 0,
                "flow_rows": 0, "whale_rows": 0, "stablecoin_rows": 0,
                "label_snapshots": 0, "whale_thresholds": 0,
            }
        import psycopg

        factory = self.connection_factory or psycopg.connect
        totals = {
            "events": 0, "assets": 0, "flow_rows": 0, "whale_rows": 0,
            "stablecoin_rows": 0, "overflow_assets": 0,
            "label_snapshots": len(snapshots), "whale_thresholds": len(thresholds),
            "windows_scanned": 0, "windows_processed": 0, "windows_pending": 0,
        }
        config_signature = (
            tuple(sorted((chain.value, snapshot.snapshot_hash) for chain, snapshot in snapshots.items())),
            tuple(sorted(
                (chain.value, asset_id, config.threshold_version)
                for (chain, asset_id), config in thresholds.items()
            )),
        )
        if config_signature != self._context_config_signature:
            self._processed_context_windows.clear()
            self._persisted_label_hashes.clear()
            self._context_config_signature = config_signature
        label_hashes_to_mark: list[str] = []
        processed_watermarks: list[tuple[tuple[str, str, str, datetime], datetime | None]] = []
        with factory(self.settings.postgres_dsn) as connection:
            connection.execute("SET LOCAL statement_timeout = '5000ms'")
            from quant_phase1.db import assert_schema_ready

            assert_schema_ready(connection, required_version="014_phase7_exact_amount_constraint.sql")
            repository = Phase7Repository(connection)
            for snapshot in snapshots.values():
                if snapshot.snapshot_hash not in self._persisted_label_hashes:
                    repository.upsert_address_labels(snapshot)
                    label_hashes_to_mark.append(snapshot.snapshot_hash)
            now = utc_now()
            lookback_start = now - timedelta(hours=self.settings.phase7_context_lookback_hours)
            windows = repository.load_context_window_keys(lookback_start, now)
            totals["windows_scanned"] = len(windows)
            closed_through = {timeframe: closed_window_open(now, timeframe) for timeframe in WINDOWS}
            eligible = []
            for item in windows:
                window_open = item["window_open"]
                timeframe = item["timeframe"]
                if window_open > closed_through[timeframe]:
                    continue
                identity = (item["chain"], item["asset_id"], timeframe, window_open)
                watermark = item.get("latest_processed_at")
                if identity not in self._processed_context_windows or self._processed_context_windows[identity] != watermark:
                    eligible.append((identity, item, watermark))
            pending_count = max(0, len(eligible) - self.settings.phase7_context_window_batch)
            eligible = eligible[:self.settings.phase7_context_window_batch]
            totals["windows_pending"] = pending_count
            for identity, item, watermark in eligible:
                try:
                    counts = aggregate_closed_context_window(
                        repository, timeframe=item["timeframe"], window_open=item["window_open"],
                        processed_at=now, label_snapshots=snapshots,
                        whale_thresholds=thresholds, chain=Chain(item["chain"]),
                        asset_id=item["asset_id"],
                    )
                except Phase7ContextLimitError:
                    totals["overflow_assets"] += 1
                    continue
                for count_key, count_value in counts.items():
                    if count_key in totals:
                        totals[count_key] += count_value
                processed_watermarks.append((identity, watermark))
                totals["windows_processed"] += 1
        self._persisted_label_hashes.update(label_hashes_to_mark)
        self._processed_context_windows.update(processed_watermarks)
        self._processed_context_windows = {
            identity: watermark
            for identity, watermark in self._processed_context_windows.items()
            if identity[3] >= lookback_start
        }
        return totals

    async def _write_health(self, status: HealthDataStatus, details: dict[str, Any]) -> None:
        now = utc_now()
        try:
            if self.health_writer is not None:
                self.health_writer("phase7-context-engine", status, now, details)
                return
            import psycopg

            from quant_phase1.repositories import Phase1Repository

            factory = self.connection_factory or psycopg.connect
            with factory(self.settings.postgres_dsn) as connection:
                Phase1Repository(connection).upsert_system_health(
                    "phase7-context-engine", status, now, details,
                )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("phase7_engine_health_persist_failed exception=%s", type(exc).__name__)

    async def _run_retention(self) -> None:
        try:
            import psycopg

            factory = self.connection_factory or psycopg.connect
            with factory(self.settings.postgres_dsn) as connection:
                from quant_phase1.db import assert_schema_ready

                assert_schema_ready(connection, required_version="014_phase7_exact_amount_constraint.sql")
                cleanup_phase7_retention(
                    Phase7Repository(connection),
                    Phase7RetentionPolicy(
                        onchain_transfer_events_days=self.settings.phase7_onchain_transfer_events_retention_days,
                        address_labels_days=self.settings.phase7_address_labels_retention_days,
                        onchain_flow_windows_days=self.settings.phase7_onchain_flow_windows_retention_days,
                        whale_flow_windows_days=self.settings.phase7_whale_flow_windows_retention_days,
                        spot_flow_windows_days=self.settings.phase7_spot_flow_windows_retention_days,
                        stablecoin_context_days=self.settings.phase7_stablecoin_context_retention_days,
                        ingestion_checkpoints_days=self.settings.phase7_ingestion_checkpoints_retention_days,
                        enrichment_days=self.settings.phase7_enrichment_retention_days,
                    ),
                    now=utc_now(),
                    batch_size=500,
                    max_batches=1,
                )
        except Exception as exc:  # noqa: BLE001 - retention cannot stop Stage1
            LOGGER.warning("phase7_retention_cycle_failed exception=%s", type(exc).__name__)


def persist_stage1_phase7_context(
    connection: Any,
    *,
    screening_run_id: int,
    candidates: Any,
    processed_at: datetime,
) -> int:
    """Persist bounded Phase 7 context without reading or mutating Stage1 decisions."""
    from decimal import Decimal

    from .enrichment import Phase7Context, build_stage1_phase7_enrichment

    if processed_at.tzinfo is None or processed_at.utcoffset() != timedelta(0):
        raise ValueError("processed_at must be UTC-aware")
    processed_at = processed_at.astimezone(timezone.utc)
    by_symbol: dict[str, list[Phase7Context]] = {}
    cutoff = processed_at - timedelta(days=1)
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT ON (e.chain, e.asset_id)
                   ar.symbol, e.chain, e.asset_id, 'latest', e.event_time,
                   e.event_time, e.status,
                   CASE WHEN e.status='AVAILABLE' THEN 1.0 ELSE 0.0 END
            FROM phase7_onchain_transfer_events e
            JOIN phase7_asset_registry ar ON ar.asset_id=e.asset_id
            WHERE e.event_time <= %s AND e.event_time >= %s
            ORDER BY e.chain, e.asset_id, e.event_time DESC, e.block_number DESC LIMIT 16
            """,
            (processed_at, cutoff),
        )
        chain_rows = cursor.fetchall()
        cursor.execute(
            """
            SELECT DISTINCT ON (symbol, exchange, timeframe)
                   symbol, exchange, timeframe, window_open, window_close,
                   status, coverage_ratio
            FROM phase7_spot_flow_windows
            WHERE market_kind='SPOT' AND window_close <= %s AND window_close >= %s
            ORDER BY symbol, exchange, timeframe, window_close DESC LIMIT 2000
            """,
            (processed_at, cutoff),
        )
        spot_rows = cursor.fetchall()
    for symbol, chain, asset_id, timeframe, window_open, window_close, status, coverage in chain_rows:
        mapped = "BTCUSDT" if symbol == "BTC" else "ETHUSDT" if symbol == "ETH" else None
        if mapped is None:
            continue
        by_symbol.setdefault(mapped, []).append(Phase7Context(
            source_id=f"onchain:{str(chain).lower()}:{asset_id}:{timeframe}",
            status=status,
            reference=f"phase7:onchain:{chain}:{window_open.isoformat()}:{window_close.isoformat()}",
            coverage=Decimal(str(coverage or 0)),
        ))
    for symbol, exchange, timeframe, window_open, window_close, status, coverage in spot_rows:
        if symbol not in {"BTCUSDT", "ETHUSDT"}:
            continue
        by_symbol.setdefault(symbol, []).append(Phase7Context(
            source_id=f"spot:{str(exchange).lower()}:{timeframe}",
            status=status,
            reference=f"phase7:spot:{exchange}:{window_open.isoformat()}:{window_close.isoformat()}",
            coverage=Decimal(str(coverage or 0)),
        ))
    rows = build_stage1_phase7_enrichment(
        screening_run_id=screening_run_id,
        candidates=candidates,
        contexts=by_symbol,
        processed_at=processed_at,
    )
    repository = Phase7Repository(connection)
    return sum(repository.upsert_stage1_enrichment(row) for row in rows)


__all__ = [
    "Phase7CollectorRuntime",
    "Phase7EngineRuntime",
    "Phase7JsonRpcClient",
    "Phase7RpcError",
    "persist_stage1_phase7_context",
]
