"""Bounded orchestration layer for public trade adapters.

Network ownership is intentionally injectable at this layer.  WebSocket
clients can feed decoded public messages here without coupling flow logic to
an exchange payload or adding a service.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from collections.abc import Callable
from typing import Any, Mapping

from websockets.asyncio.client import connect

from quant_phase1.config import PUBLIC_WS_CLOSE_TIMEOUT_SECONDS

from .adapters.base import AdapterSchemaError, PublicTradeAdapter
from .dedup import BoundedTradeDeduplicator
from .flow import TradeFlowWindow, TradeFlowWindowBuilder
from .health import TradeHealthRegistry
from .queue import BoundedTradeQueue


class Phase3TradeRuntime:
    def __init__(
        self,
        adapters: Mapping[str, PublicTradeAdapter],
        *,
        queue_capacity: int = 2000,
        dedup_max_entries_per_exchange: int = 10_000,
        dedup_ttl_seconds: int = 300,
        allowed_lateness_seconds: int = 5,
    ) -> None:
        if not adapters:
            raise ValueError("at least one public trade adapter is required")
        self.adapters = dict(adapters)
        self.queue_capacity = queue_capacity
        self.allowed_lateness_seconds = allowed_lateness_seconds
        self.health = TradeHealthRegistry()
        self._dedup = {
            exchange: BoundedTradeDeduplicator(
                identity_key=adapter.identity_key,
                max_entries_per_exchange=dedup_max_entries_per_exchange,
                ttl_seconds=dedup_ttl_seconds,
            )
            for exchange, adapter in self.adapters.items()
        }
        self._queues: dict[tuple[str, str], BoundedTradeQueue] = {}
        self._builders: dict[tuple[str, str], TradeFlowWindowBuilder] = {}
        self._queue_builder_keys: dict[tuple[str, str], tuple[str, str]] = {}
        self._pending_backpressure_builders: set[tuple[str, str]] = set()
        self._active_symbols: dict[str, set[str]] = {exchange: set() for exchange in self.adapters}
        self._process_all_pending_cursor = 0
        self._running = False
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def running(self) -> bool:
        return self._running

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        for task in tuple(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def ingest_ws_message(self, exchange: str, payload: Mapping[str, Any], *, received_at: datetime) -> int:
        self._require_running()
        adapter = self._adapter(exchange)
        trades = adapter.parse_ws_message(payload, received_at)
        return self._ingest(exchange, trades, received_at=received_at)

    def ingest_recent_trades(
        self,
        exchange: str,
        payload: Mapping[str, Any],
        *,
        received_at: datetime,
        exchange_symbol: str | None = None,
    ) -> int:
        self._require_running()
        adapter = self._adapter(exchange)
        trades = adapter.parse_recent_trades(payload, received_at, exchange_symbol=exchange_symbol)
        return self._ingest(exchange, trades, received_at=received_at)

    def process_pending(
        self,
        exchange: str,
        symbol: str,
        *,
        watermark: datetime,
        processed_at: datetime,
        max_trades: int | None = None,
    ) -> tuple[TradeFlowWindow, ...]:
        if max_trades is not None and max_trades <= 0:
            raise ValueError("max_trades must be positive when provided")
        queue_key = (exchange, symbol)
        queue = self._queues.get(queue_key)
        if queue is None:
            return ()
        builder_keys: set[tuple[str, str]] = set()
        processed_trades = 0
        while True:
            if max_trades is not None and processed_trades >= max_trades:
                break
            try:
                trade = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            processed_trades += 1
            canonical = trade.canonical_symbol or trade.exchange_symbol
            key = (exchange, canonical)
            builder = self._builders.setdefault(
                key,
                TradeFlowWindowBuilder(
                    timeframe="1m",
                    window_seconds=60,
                    allowed_lateness_seconds=self.allowed_lateness_seconds,
                ),
            )
            builder.add(trade, now=processed_at)
            builder_keys.add(key)
        gap_builder_key = self._queue_builder_keys.get(queue_key)
        if gap_builder_key in self._pending_backpressure_builders:
            builder_keys.add(gap_builder_key)
        if max_trades is not None and queue.depth:
            return ()
        if max_trades is not None and any(
            pending_queue.depth and self._queue_builder_keys.get(pending_key) in builder_keys
            for pending_key, pending_queue in self._queues.items()
            if pending_key != queue_key
        ):
            return ()
        windows: list[TradeFlowWindow] = []
        for key in sorted(builder_keys):
            builder = self._builders[key]
            windows.extend(builder.finalize(watermark, processed_at=processed_at))
            if not builder.has_pending_partials:
                self._pending_backpressure_builders.discard(key)
        return tuple(windows)

    def process_all_pending(
        self,
        *,
        watermark: datetime,
        processed_at: datetime,
    ) -> tuple[TradeFlowWindow, ...]:
        """Finalize every bounded exchange/symbol queue currently registered."""
        windows: list[TradeFlowWindow] = []
        for exchange in sorted(self._active_symbols):
            for symbol in sorted(self._active_symbols[exchange]):
                windows.extend(
                    self.process_pending(
                        exchange,
                        symbol,
                        watermark=watermark,
                        processed_at=processed_at,
                    )
                )
        return tuple(sorted(windows, key=lambda row: (row.window_open, row.exchange, row.canonical_symbol)))

    async def process_all_pending_async(
        self,
        *,
        watermark: datetime,
        processed_at: datetime,
    ) -> tuple[TradeFlowWindow, ...]:
        """Drain bounded work fairly without starving WS tasks or delaying flow writes."""
        windows: list[TradeFlowWindow] = []
        queue_keys = tuple(
            (exchange, symbol)
            for exchange in sorted(self._active_symbols)
            for symbol in sorted(tuple(self._active_symbols[exchange]))
        )
        if not queue_keys:
            return ()
        per_symbol_budget = max(1, min(256, self.queue_capacity))
        cycle_budget = max(per_symbol_budget, min(20_000, self.queue_capacity * 8))
        remaining_budget = cycle_budget
        start_index = self._process_all_pending_cursor % len(queue_keys)
        for offset in range(len(queue_keys)):
            index = (start_index + offset) % len(queue_keys)
            exchange, symbol = queue_keys[index]
            queue = self._queues.get((exchange, symbol))
            before_depth = queue.depth if queue is not None else 0
            windows.extend(
                self.process_pending(
                    exchange,
                    symbol,
                    watermark=watermark,
                    processed_at=processed_at,
                    max_trades=min(per_symbol_budget, remaining_budget),
                )
            )
            after_depth = queue.depth if queue is not None else 0
            remaining_budget -= max(0, before_depth - after_depth)
            self._process_all_pending_cursor = (index + 1) % len(queue_keys)
            await asyncio.sleep(0)
            if remaining_budget <= 0:
                break
        return tuple(sorted(windows, key=lambda row: (row.window_open, row.exchange, row.canonical_symbol)))

    def register_active_symbol(self, exchange: str, symbol: str) -> None:
        self._adapter(exchange)
        self._active_symbols.setdefault(exchange, set()).add(symbol)

    def reconnect_exchange(self, exchange: str, *, now: datetime) -> tuple[Mapping[str, Any], ...]:
        adapter = self._adapter(exchange)
        self.health.record_reconnect(exchange, now)
        return tuple(adapter.subscription(symbol) for symbol in sorted(self._active_symbols.get(exchange, set())))

    def dedup(self, exchange: str) -> BoundedTradeDeduplicator:
        return self._dedup[exchange]

    def health_snapshot(self, exchange: str):
        return self.health.snapshot(exchange)

    def _ingest(self, exchange: str, trades: Any, *, received_at: datetime) -> int:
        self.health.mark_connected(exchange, received_at)
        accepted = 0
        for trade in trades:
            if trade.canonical_symbol is None:
                trade = replace(trade, canonical_symbol=_canonical_symbol(trade.exchange_symbol))
            if not self._dedup[exchange].add(trade, now=received_at):
                continue
            self.register_active_symbol(exchange, trade.exchange_symbol)
            queue_key = (exchange, trade.exchange_symbol)
            queue = self._queues.setdefault(
                queue_key,
                BoundedTradeQueue(exchange=exchange, symbol=trade.exchange_symbol, capacity=self.queue_capacity),
            )
            builder_key = (exchange, trade.canonical_symbol or trade.exchange_symbol)
            self._queue_builder_keys[queue_key] = builder_key
            if queue.put_nowait(trade, now=received_at):
                accepted += 1
            else:
                builder = self._builders.setdefault(
                    builder_key,
                    TradeFlowWindowBuilder(
                        timeframe="1m",
                        window_seconds=60,
                        allowed_lateness_seconds=self.allowed_lateness_seconds,
                    ),
                )
                window_open = builder.router.window_open(trade.exchange_timestamp)
                if window_open is not None:
                    builder.mark_partial(
                        exchange=exchange,
                        canonical_symbol=builder_key[1],
                        window_open=window_open,
                        reason="BACKPRESSURE_EVENT",
                    )
                    self._pending_backpressure_builders.add(builder_key)
                self.health.record_backpressure(exchange, dropped=1, now=received_at)
        return accepted

    def _adapter(self, exchange: str) -> PublicTradeAdapter:
        try:
            return self.adapters[exchange]
        except KeyError as exc:
            raise ValueError(f"unknown Phase 3 exchange: {exchange}") from exc

    def _require_running(self) -> None:
        if not self._running:
            raise RuntimeError("Phase 3 trade runtime is not running")


def _canonical_symbol(exchange_symbol: str) -> str:
    """Map the initial USDT perpetual universe into the Phase 3 key space."""
    normalized = exchange_symbol.strip().upper()
    if normalized.endswith("USDT"):
        return f"{normalized[:-4]}-USDT-PERP"
    return normalized


class Phase3PublicStreamRunner:
    """Reconnectable public-only WebSocket runner for all Phase 3 adapters.

    The runner owns only transport and subscription lifecycle. Payload parsing,
    deduplication, queue bounds and canonicalization remain in the runtime.
    """

    def __init__(self, runtime: Phase3TradeRuntime, *, reconnect_seconds: float = 5.0) -> None:
        if reconnect_seconds < 0:
            raise ValueError("reconnect_seconds must be non-negative")
        self.runtime = runtime
        self.reconnect_seconds = reconnect_seconds
        self.connect_factory = connect

    def subscription_payloads(self, exchange: str, symbols: tuple[str, ...]) -> tuple[Mapping[str, Any], ...]:
        adapter = self.runtime.adapters[exchange]
        return tuple(adapter.subscription(symbol) for symbol in symbols)

    async def run_exchange(
        self,
        exchange: str,
        symbols: tuple[str, ...],
        *,
        stop_event: asyncio.Event,
    ) -> None:
        adapter = self.runtime.adapters[exchange]
        normalized_symbols = tuple(dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip()))
        if not normalized_symbols:
            return
        for symbol in normalized_symbols:
            self.runtime.register_active_symbol(exchange, symbol)
            self.runtime.health.record_subscription(exchange, symbol)
        connected_once = False
        while not stop_event.is_set():
            try:
                async with self.connect_factory(
                    adapter.ws_url,
                    open_timeout=15,
                    ping_interval=20,
                    close_timeout=PUBLIC_WS_CLOSE_TIMEOUT_SECONDS,
                ) as socket:
                    for subscription in self.subscription_payloads(exchange, normalized_symbols):
                        await socket.send(json.dumps(subscription, separators=(",", ":")))
                    connected_once = True
                    while not stop_event.is_set():
                        raw = await socket.recv()
                        payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                        if self._is_control_message(payload):
                            continue
                        try:
                            self.runtime.ingest_ws_message(
                                exchange,
                                payload,
                                received_at=datetime.now(timezone.utc),
                            )
                        except AdapterSchemaError:
                            # Control/heartbeat envelopes are ignored above;
                            # any trade envelope that fails the strict adapter
                            # contract is recorded as a degraded connection and
                            # causes a reconnect rather than being normalized.
                            raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                now = datetime.now(timezone.utc)
                self.runtime.health.mark_disconnected(exchange, now, reason=type(exc).__name__)
                if stop_event.is_set():
                    return
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.reconnect_seconds)
                except asyncio.TimeoutError:
                    if connected_once:
                        self.runtime.health.record_reconnect(exchange, datetime.now(timezone.utc))
                    continue
        self.runtime.health.mark_disconnected(exchange, datetime.now(timezone.utc), reason="STOPPED")

    async def run_all(
        self,
        symbols_by_exchange: Mapping[str, tuple[str, ...]],
        *,
        stop_event: asyncio.Event,
    ) -> None:
        tasks = [
            asyncio.create_task(self.run_exchange(exchange, tuple(symbols), stop_event=stop_event))
            for exchange, symbols in sorted(symbols_by_exchange.items())
            if symbols
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def run_dynamic(
        self,
        symbols_provider: Callable[[], Mapping[str, tuple[str, ...]]],
        *,
        stop_event: asyncio.Event,
        refresh_seconds: float,
    ) -> None:
        """Refresh the bounded subscription set without adding a service.

        Keep public sockets open while the active symbol set is unchanged.
        When the bounded set changes, cancel the old receive workers promptly
        so their blocked ``recv`` calls cannot delay applying the new set.
        """
        if refresh_seconds <= 0:
            raise ValueError("refresh_seconds must be positive")
        worker_stop = asyncio.Event()
        worker: asyncio.Task[None] | None = None
        active_symbols: dict[str, tuple[str, ...]] = {}
        try:
            while not stop_event.is_set():
                desired_symbols = {
                    exchange: tuple(sorted({
                        symbol.strip().upper()
                        for symbol in symbols
                        if symbol.strip()
                    }))
                    for exchange, symbols in sorted(symbols_provider().items())
                }
                desired_symbols = {
                    exchange: symbols
                    for exchange, symbols in desired_symbols.items()
                    if symbols
                }
                changed = desired_symbols != active_symbols
                failed = worker is not None and worker.done()
                if changed or failed:
                    if worker is not None:
                        worker_stop.set()
                        if not worker.done():
                            worker.cancel()
                        await asyncio.gather(worker, return_exceptions=True)
                    active_symbols = desired_symbols
                    worker = None
                    if active_symbols:
                        worker_stop = asyncio.Event()
                        worker = asyncio.create_task(
                            self.run_all(active_symbols, stop_event=worker_stop)
                        )
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=refresh_seconds)
                except asyncio.TimeoutError:
                    pass
        finally:
            worker_stop.set()
            if worker is not None:
                # A receiver may be blocked in websocket.recv(), which does
                # not observe worker_stop until recv returns. Explicitly
                # cancel the owned run_all task so its finally block cancels
                # and gathers each exchange stream during shutdown.
                if not worker.done():
                    worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    @staticmethod
    def _is_control_message(payload: Any) -> bool:
        if not isinstance(payload, Mapping):
            return True
        if payload.get("channel") == "subscriptionResponse":
            return True
        if payload.get("event") in {"subscribe", "unsubscribe", "error"}:
            return True
        if payload.get("op") in {"subscribe", "unsubscribe", "ping", "pong"} and "data" not in payload:
            return True
        return False
