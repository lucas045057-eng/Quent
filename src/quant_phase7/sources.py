"""Phase 7 source registry and bounded public transport.

The registry is declarative and contains no network side effects.  The
transport accepts an injected async session so deterministic tests never need
live endpoints.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from enum import StrEnum
import json
import time
from typing import Any, Mapping
from urllib.parse import urljoin, urlparse


class SourceKind(StrEnum):
    BITCOIN_RPC = "BITCOIN_RPC"
    ETHEREUM_RPC = "ETHEREUM_RPC"
    BINANCE_SPOT = "BINANCE_SPOT"
    BITGET_SPOT_UTA_V3 = "BITGET_SPOT_UTA_V3"


class SourceStatus(StrEnum):
    ENABLED = "ENABLED"
    NOT_CONFIGURED = "NOT_CONFIGURED"
    PENDING_CONTRACT = "PENDING_CONTRACT"


class AuthMode(StrEnum):
    NONE = "NONE"
    READ_ONLY_PROVIDER = "READ_ONLY_PROVIDER"


class TransportError(RuntimeError):
    """Base error for bounded source transport."""


class SourceUnavailable(TransportError):
    """Raised when a source is not enabled/configured for Phase 7."""


class ResponseTooLarge(TransportError):
    """Raised before retaining a response larger than the source cap."""


class RetryExhausted(TransportError):
    """Raised after finite retryable responses or transport failures."""


class HTTPStatusError(TransportError):
    """Raised for non-retryable HTTP responses."""


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    source_id: str
    kind: SourceKind
    status: SourceStatus
    auth_mode: AuthMode
    rest_base_url: str | None
    ws_url: str | None
    official_reference: str
    methods: tuple[str, ...]
    rest_paths: tuple[str, ...]
    local_requests_per_second: float | None
    connect_timeout_seconds: float
    timeout_seconds: float
    max_retries: int
    backoff_seconds: tuple[float, ...]
    max_bytes: int
    max_pages: int
    max_events: int
    catch_up_limit: int
    backfill_limit: int
    queue_capacity: int
    max_concurrency: int
    parser_version: str
    policy_version: str
    max_block_range: int | None = None
    ws_schema: Mapping[str, Any] | None = None
    rest_params: Mapping[str, str] = field(default_factory=dict)
    fallback_source_id: str | None = None

    def __post_init__(self) -> None:
        if not self.source_id or not self.official_reference.startswith("https://"):
            raise ValueError("source identity and official reference are required")
        if self.max_retries < 0 or self.max_retries > 4:
            raise ValueError("max_retries must be between 0 and 4")
        if len(self.backoff_seconds) != self.max_retries:
            raise ValueError("backoff_seconds must match max_retries")
        if any(delay < 0 for delay in self.backoff_seconds):
            raise ValueError("backoff_seconds must be non-negative")
        if self.max_bytes <= 0 or self.max_pages <= 0 or self.max_events <= 0:
            raise ValueError("source limits must be positive")
        if self.catch_up_limit <= 0 or self.backfill_limit <= 0:
            raise ValueError("catch-up/backfill limits must be positive")
        if self.queue_capacity <= 0 or self.max_concurrency <= 0:
            raise ValueError("queue/concurrency limits must be positive")
        if self.max_block_range is not None and self.max_block_range <= 0:
            raise ValueError("max_block_range must be positive")
        if self.connect_timeout_seconds <= 0 or self.timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        if self.rest_base_url is not None:
            parsed = urlparse(self.rest_base_url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise ValueError("REST base URL must be HTTPS")
        if self.ws_url is not None:
            parsed = urlparse(self.ws_url)
            if parsed.scheme != "wss" or not parsed.hostname:
                raise ValueError("WebSocket URL must be WSS")
        if self.fallback_source_id is not None:
            raise ValueError("Phase 7 sources do not use silent fallbacks")

    @property
    def enabled(self) -> bool:
        return self.status is SourceStatus.ENABLED

    def url_for(self, path: str) -> str:
        if self.rest_base_url is None:
            raise SourceUnavailable(f"{self.source_id} has no configured REST endpoint")
        if not path.startswith("/") or path not in self.rest_paths:
            raise ValueError(f"unapproved path for {self.source_id}: {path}")
        return urljoin(self.rest_base_url.rstrip("/") + "/", path.lstrip("/"))


class SourceRegistry:
    def __init__(self, definitions: tuple[SourceDefinition, ...] | None = None) -> None:
        definitions = definitions if definitions is not None else default_source_definitions()
        indexed: dict[str, SourceDefinition] = {}
        for definition in definitions:
            if definition.source_id in indexed:
                raise ValueError(f"duplicate source_id: {definition.source_id}")
            indexed[definition.source_id] = definition
        self._definitions = indexed

    def require(self, source_id: str) -> SourceDefinition:
        try:
            return self._definitions[source_id]
        except KeyError as exc:
            raise KeyError(f"unapproved source: {source_id}") from exc

    def all(self) -> tuple[SourceDefinition, ...]:
        return tuple(self._definitions.values())


def default_source_definitions() -> tuple[SourceDefinition, ...]:
    """Return the frozen V1 source matrix; this function performs no I/O."""
    return (
        SourceDefinition(
            source_id="btc_core_rpc",
            kind=SourceKind.BITCOIN_RPC,
            status=SourceStatus.NOT_CONFIGURED,
            auth_mode=AuthMode.READ_ONLY_PROVIDER,
            rest_base_url=None,
            ws_url=None,
            official_reference="https://developer.bitcoin.org/reference/rpc/",
            methods=("getblockchaininfo", "getblockhash", "getblock", "getrawtransaction"),
            rest_paths=(),
            local_requests_per_second=1.0,
            connect_timeout_seconds=2.0,
            timeout_seconds=8.0,
            max_retries=2,
            backoff_seconds=(0.5, 1.5),
            max_bytes=8 * 1024 * 1024,
            max_pages=1,
            max_events=10_000,
            catch_up_limit=12,
            backfill_limit=144,
            queue_capacity=256,
            max_concurrency=1,
            max_block_range=None,
            parser_version="phase7-btc-rpc-v1",
            policy_version="phase7-source-policy-v1",
        ),
        SourceDefinition(
            source_id="ethereum_rpc",
            kind=SourceKind.ETHEREUM_RPC,
            status=SourceStatus.NOT_CONFIGURED,
            auth_mode=AuthMode.READ_ONLY_PROVIDER,
            rest_base_url=None,
            ws_url=None,
            official_reference="https://ethereum.org/developers/docs/apis/json-rpc/",
            methods=("eth_chainId", "eth_blockNumber", "eth_getBlockByNumber", "eth_getLogs", "eth_getTransactionReceipt"),
            rest_paths=(),
            local_requests_per_second=1.0,
            connect_timeout_seconds=2.0,
            timeout_seconds=8.0,
            max_retries=2,
            backoff_seconds=(0.5, 1.5),
            max_bytes=16 * 1024 * 1024,
            max_pages=1_000,
            max_events=20_000,
            catch_up_limit=120,
            backfill_limit=2_400,
            queue_capacity=512,
            max_concurrency=1,
            max_block_range=1_000,
            parser_version="phase7-eth-rpc-v1",
            policy_version="phase7-source-policy-v1",
        ),
        SourceDefinition(
            source_id="binance_spot",
            kind=SourceKind.BINANCE_SPOT,
            status=SourceStatus.ENABLED,
            auth_mode=AuthMode.NONE,
            rest_base_url="https://api.binance.com",
            ws_url="wss://stream.binance.com:9443/ws",
            official_reference="https://github.com/binance/binance-spot-api-docs",
            methods=("GET /api/v3/aggTrades", "WS <symbol>@aggTrade"),
            rest_paths=("/api/v3/aggTrades",),
            local_requests_per_second=5.0,
            connect_timeout_seconds=1.0,
            timeout_seconds=5.0,
            max_retries=2,
            backoff_seconds=(0.25, 1.0),
            max_bytes=8 * 1024 * 1024,
            max_pages=3,
            max_events=3_000,
            catch_up_limit=120,
            backfill_limit=120,
            queue_capacity=2_048,
            max_concurrency=1,
            max_block_range=None,
            parser_version="phase7-binance-spot-v1",
            policy_version="phase7-source-policy-v1",
        ),
        SourceDefinition(
            source_id="bitget_spot_uta_v3",
            kind=SourceKind.BITGET_SPOT_UTA_V3,
            status=SourceStatus.PENDING_CONTRACT,
            auth_mode=AuthMode.NONE,
            rest_base_url="https://api.bitget.com",
            ws_url="wss://ws.bitget.com/v3/ws/public",
            official_reference="https://www.bitget.com/docs/uta/websocket/public/Trade-Channel",
            methods=("GET /api/v3/market/fills", "WS topic=publicTrade"),
            rest_paths=("/api/v3/market/fills",),
            local_requests_per_second=2.0,
            connect_timeout_seconds=1.0,
            timeout_seconds=5.0,
            max_retries=2,
            backoff_seconds=(0.5, 1.5),
            max_bytes=4 * 1024 * 1024,
            max_pages=3,
            max_events=1_000,
            catch_up_limit=120,
            backfill_limit=120,
            queue_capacity=1_024,
            max_concurrency=1,
            max_block_range=None,
            parser_version="phase7-bitget-spot-uta-v3-pending",
            policy_version="phase7-source-policy-v1",
            ws_schema={
                "op": "subscribe",
                "args": [{"instType": "spot", "topic": "publicTrade", "symbol": "BTCUSDT"}],
            },
            rest_params={"category": "SPOT"},
        ),
    )


@dataclass(frozen=True, slots=True)
class TransportResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    attempts: int

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def retry_delay(
    attempt: int,
    retry_after: str | None,
    definition: SourceDefinition,
) -> float:
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), 8.0)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(retry_after)
                if parsed.tzinfo is not None:
                    return min(max(parsed.timestamp() - time.time(), 0.0), 8.0)
            except (TypeError, ValueError, OverflowError):
                pass
    if not definition.backoff_seconds:
        return 0.0
    return definition.backoff_seconds[min(attempt, len(definition.backoff_seconds) - 1)]


class BoundedPublicTransport:
    """A finite, source-allowlisted async HTTP transport."""

    _RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

    def __init__(self, session: Any, registry: SourceRegistry | None = None) -> None:
        self.session = session
        self.registry = registry or SourceRegistry()
        self._semaphores = {
            definition.source_id: asyncio.BoundedSemaphore(definition.max_concurrency)
            for definition in self.registry.all()
        }
        self._rate_locks = {
            definition.source_id: asyncio.Lock()
            for definition in self.registry.all()
        }
        self._next_allowed = {definition.source_id: 0.0 for definition in self.registry.all()}

    async def request(
        self,
        source_id: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        page_number: int = 1,
        event_count: int = 0,
        catch_up_count: int = 0,
        backfill_count: int = 0,
        queue_size: int = 0,
        block_range: int | None = None,
    ) -> TransportResponse:
        definition = self.registry.require(source_id)
        if not definition.enabled:
            raise SourceUnavailable(f"{source_id} status={definition.status}")
        _validate_request_bounds(
            definition,
            page_number=page_number,
            event_count=event_count,
            catch_up_count=catch_up_count,
            backfill_count=backfill_count,
            queue_size=queue_size,
            block_range=block_range,
        )
        url = definition.url_for(path)
        request_params = dict(definition.rest_params)
        for key, value in (params or {}).items():
            if key in definition.rest_params and definition.rest_params[key] != value:
                raise ValueError(f"{source_id} requires {key}={definition.rest_params[key]}")
            request_params[key] = value
        semaphore = self._semaphores[source_id]
        async with semaphore:
            for attempt in range(definition.max_retries + 1):
                try:
                    await self._wait_for_rate(definition)
                    response = await self._request_once(definition, url, request_params)
                    if response.status in self._RETRYABLE_STATUS:
                        if attempt >= definition.max_retries:
                            raise RetryExhausted(
                                f"{source_id} HTTP {response.status} after {attempt + 1} attempts"
                            )
                        await asyncio.sleep(
                            retry_delay(attempt, response.headers.get("Retry-After"), definition)
                        )
                        continue
                    if response.status < 200 or response.status >= 300:
                        raise HTTPStatusError(f"{source_id} HTTP {response.status}")
                    body = await self._read_bounded(response, definition.max_bytes)
                    return TransportResponse(response.status, response.headers, body, attempt + 1)
                except RetryExhausted:
                    raise
                except HTTPStatusError:
                    raise
                except ResponseTooLarge:
                    raise
                except Exception as exc:  # noqa: BLE001 - finite transport retry boundary
                    if attempt >= definition.max_retries:
                        raise RetryExhausted(
                            f"{source_id} transport failure after {attempt + 1} attempts"
                        ) from exc
                    await asyncio.sleep(retry_delay(attempt, None, definition))
            raise AssertionError("finite retry loop exhausted unexpectedly")

    async def _wait_for_rate(self, definition: SourceDefinition) -> None:
        if definition.local_requests_per_second is None:
            return
        interval = 1.0 / definition.local_requests_per_second
        async with self._rate_locks[definition.source_id]:
            now = time.monotonic()
            delay = max(0.0, self._next_allowed[definition.source_id] - now)
            self._next_allowed[definition.source_id] = max(now, self._next_allowed[definition.source_id]) + interval
        if delay:
            await asyncio.sleep(delay)

    async def _request_once(
        self,
        definition: SourceDefinition,
        url: str,
        params: Mapping[str, str] | None,
    ) -> Any:
        response = await self.session.get(
            url,
            params=dict(params or {}),
            timeout=_timeout_budget(definition),
        )
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > definition.max_bytes:
                    raise ResponseTooLarge(f"{definition.source_id} Content-Length exceeds cap")
            except ValueError:
                pass
        return response

    async def _read_bounded(self, response: Any, max_bytes: int) -> bytes:
        content = getattr(response, "content", None)
        iter_chunked = getattr(content, "iter_chunked", None)
        if callable(iter_chunked):
            chunks: list[bytes] = []
            total = 0
            async for chunk in iter_chunked(min(64 * 1024, max_bytes + 1)):
                total += len(chunk)
                if total > max_bytes:
                    raise ResponseTooLarge("response body exceeds source byte cap")
                chunks.append(bytes(chunk))
            return b"".join(chunks)
        body = await response.read()
        if len(body) > max_bytes:
            raise ResponseTooLarge("response body exceeds source byte cap")
        return body


def _timeout_budget(definition: SourceDefinition) -> Any:
    """Use aiohttp's separate connect/total budget when available."""
    try:
        from aiohttp import ClientTimeout
    except ImportError:
        return definition.timeout_seconds
    return ClientTimeout(
        total=definition.timeout_seconds,
        connect=definition.connect_timeout_seconds,
    )


def _validate_request_bounds(
    definition: SourceDefinition,
    *,
    page_number: int,
    event_count: int,
    catch_up_count: int,
    backfill_count: int,
    queue_size: int,
    block_range: int | None,
) -> None:
    values = {
        "page_number": (page_number, 1, definition.max_pages),
        "event_count": (event_count, 0, definition.max_events),
        "catch_up_count": (catch_up_count, 0, definition.catch_up_limit),
        "backfill_count": (backfill_count, 0, definition.backfill_limit),
        "queue_size": (queue_size, 0, definition.queue_capacity),
    }
    for field_name, (value, lower, upper) in values.items():
        if value < lower or value > upper:
            raise ValueError(f"{definition.source_id} {field_name} exceeds bounded limit")
    if block_range is not None:
        if block_range <= 0 or (
            definition.max_block_range is not None and block_range > definition.max_block_range
        ):
            raise ValueError(f"{definition.source_id} block range exceeds bounded limit")
