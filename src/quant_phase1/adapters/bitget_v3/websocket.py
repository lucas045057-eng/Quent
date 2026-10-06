"""Bitget UTA v3 public WebSocket adapter."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from dataclasses import replace
import json
import logging
from typing import Any, Callable, Mapping

from websockets.asyncio.client import connect as ws_connect

from ...contracts import Candle, Ticker
from ...config import PUBLIC_WS_CLOSE_TIMEOUT_SECONDS, Settings
from ...time import utc_now
from .parsers import parse_candles_response, parse_tickers_response, INTERVAL_SECONDS

LOGGER = logging.getLogger("quant_phase1")
MAX_SUBSCRIPTION_ARGS_PER_MESSAGE = 40


class BitgetV3UtaWebSocket:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        connector: Callable[..., Any] = ws_connect,
    ) -> None:
        self.settings = settings or Settings.from_env({})
        self._connector = connector
        self._socket: Any = None
        self._subscriptions: dict[tuple[str, str | None, str], dict[str, str]] = {}
        self._connection_lock = asyncio.Lock()
        self._closing = False
        self._connection_generation = 0
        self._active_connection_attempts = 0
        self._max_active_connection_attempts = 0

    @property
    def connection_generation(self) -> int:
        return self._connection_generation

    @property
    def active_connection_attempts(self) -> int:
        return self._active_connection_attempts

    @property
    def max_active_connection_attempts(self) -> int:
        return self._max_active_connection_attempts

    @property
    def live_connection_generations(self) -> int:
        return int(self._socket is not None)

    @property
    def ticker_symbols(self) -> tuple[str, ...]:
        return tuple(sorted(
            arg["symbol"] for arg in self._subscriptions.values() if arg.get("topic") == "ticker"
        ))

    @staticmethod
    def ticker_arg(symbol: str) -> dict[str, str]:
        return {"instType": "usdt-futures", "topic": "ticker", "symbol": symbol}

    @staticmethod
    def kline_arg(symbol: str, interval: str) -> dict[str, str]:
        if interval not in INTERVAL_SECONDS:
            raise ValueError(f"unsupported interval {interval}")
        return {"instType": "usdt-futures", "topic": "kline", "symbol": symbol, "interval": interval}

    @staticmethod
    def validate_arg(arg: Mapping[str, Any]) -> None:
        if "channel" in arg or "instId" in arg:
            raise ValueError("v3 topic schema required; v2 channel schema is forbidden")
        if arg.get("instType") != "usdt-futures" or arg.get("topic") not in {"ticker", "kline"}:
            raise ValueError("invalid v3 topic subscription")
        if arg["topic"] == "kline" and arg.get("interval") not in INTERVAL_SECONDS:
            raise ValueError("v3 kline subscription requires interval")

    async def _connect_locked(self) -> int:
        if self._closing:
            raise RuntimeError("WebSocket is stopping")
        if self._socket is not None:
            return self._connection_generation
        self._active_connection_attempts += 1
        self._max_active_connection_attempts = max(
            self._max_active_connection_attempts, self._active_connection_attempts
        )
        try:
            socket = await self._connector(
                self.settings.ws_public_url,
                ping_interval=None,
                open_timeout=10,
                close_timeout=PUBLIC_WS_CLOSE_TIMEOUT_SECONDS,
            )
        finally:
            self._active_connection_attempts -= 1
        if self._closing:
            await socket.close()
            raise RuntimeError("WebSocket stopped while connection was opening")
        self._socket = socket
        self._connection_generation += 1
        return self._connection_generation

    async def connect(self) -> int:
        async with self._connection_lock:
            return await self._connect_locked()

    async def close(self) -> None:
        self._closing = True
        async with self._connection_lock:
            socket = self._socket
            self._socket = None
            if socket is not None:
                await socket.close()

    async def _send(
        self, payload: Mapping[str, Any], *, expected_generation: int | None = None
    ) -> None:
        async with self._connection_lock:
            if self._closing:
                raise RuntimeError("WebSocket is stopping")
            if expected_generation is not None and expected_generation != self._connection_generation:
                raise RuntimeError("WebSocket connection generation was replaced")
            if self._socket is None:
                if expected_generation is not None:
                    raise RuntimeError("WebSocket connection generation is no longer active")
                await self._connect_locked()
            socket = self._socket
            if socket is None:
                raise RuntimeError("WebSocket connection is unavailable")
            await socket.send(json.dumps(payload, separators=(",", ":")))

    async def _subscribe(self, arg: dict[str, str]) -> None:
        self.validate_arg(arg)
        key = (arg["topic"], arg.get("interval"), arg["symbol"])
        self._subscriptions[key] = dict(arg)
        await self._send({"op": "subscribe", "args": [arg]})

    async def subscribe_many(self, args: list[dict[str, str]], *, batch_size: int = 50) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        for arg in args:
            self.validate_arg(arg)
            self._subscriptions[(arg["topic"], arg.get("interval"), arg["symbol"])] = dict(arg)
        for start in range(0, len(args), batch_size):
            batch = args[start : start + batch_size]
            await self._send({"op": "subscribe", "args": batch})
            # Bitget limits messages per connection.  Keep subscription
            # bursts below that limit during a 200-symbol bootstrap.
            if start + batch_size < len(args):
                await asyncio.sleep(0.2)

    async def subscribe_ticker(self, symbol: str) -> None:
        await self._subscribe(self.ticker_arg(symbol))

    async def subscribe_kline(self, symbol: str, interval: str) -> None:
        await self._subscribe(self.kline_arg(symbol, interval))

    async def reconnect(self, *, expected_generation: int | None = None) -> int:
        requested_generation = (
            self._connection_generation if expected_generation is None else expected_generation
        )
        async with self._connection_lock:
            if self._closing:
                raise RuntimeError("WebSocket is stopping")
            if self._connection_generation != requested_generation:
                if self._socket is not None:
                    return self._connection_generation
                # A newer generation may have failed while restoring its subscriptions.
                # Retry from that generation instead of treating a closed socket as success.
                requested_generation = self._connection_generation
            socket = self._socket
            self._socket = None
            if socket is not None:
                await socket.close()
            generation = await self._connect_locked()
            values = list(self._subscriptions.values())
            reconnected_socket = self._socket
            if reconnected_socket is None:
                raise RuntimeError("WebSocket connection is unavailable for resubscription")
            try:
                for start in range(0, len(values), MAX_SUBSCRIPTION_ARGS_PER_MESSAGE):
                    if self._closing or generation != self._connection_generation:
                        return self._connection_generation
                    active_socket = self._socket
                    if active_socket is None:
                        raise RuntimeError("WebSocket disappeared during resubscription")
                    batch = values[start : start + MAX_SUBSCRIPTION_ARGS_PER_MESSAGE]
                    await active_socket.send(
                        json.dumps({"op": "subscribe", "args": batch}, separators=(",", ":"))
                    )
                    if start + MAX_SUBSCRIPTION_ARGS_PER_MESSAGE < len(values):
                        await asyncio.sleep(0.2)
            except BaseException:
                self._socket = None
                try:
                    await reconnected_socket.close()
                except Exception as close_exc:
                    LOGGER.warning(
                        "ws_failed_generation_close_error error_type=%s",
                        type(close_exc).__name__,
                    )
                raise
            return generation

    async def receive(self, *, expected_generation: int | None = None) -> dict[str, Any]:
        if self._socket is None and self._connection_generation > 0:
            raise RuntimeError("WebSocket connection is closed; reconnect is required")
        await self.connect()
        if expected_generation is not None and expected_generation != self._connection_generation:
            raise RuntimeError("WebSocket connection generation was replaced")
        socket = self._socket
        if socket is None:
            raise RuntimeError("WebSocket connection is unavailable")
        raw = await socket.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if raw == "pong":
            return {"event": "pong"}
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise ValueError("Bitget WS message must be an object")
        return message

    async def ping(self, *, expected_generation: int | None = None) -> int:
        async with self._connection_lock:
            if self._closing:
                raise RuntimeError("WebSocket is stopping")
            if expected_generation is not None and expected_generation != self._connection_generation:
                return self._connection_generation
            if self._socket is None:
                if expected_generation is not None:
                    return self._connection_generation
                await self._connect_locked()
            socket = self._socket
            if socket is None:
                raise RuntimeError("WebSocket connection is unavailable")
            await socket.send("ping")
            return self._connection_generation

    async def resubscribe_after_disconnect(self) -> None:
        await self.reconnect()


class TickerStreamWatchdog:
    """Track ticker data silence independently of socket and collector heartbeats."""

    def __init__(
        self,
        symbols: Any,
        *,
        idle_timeout_seconds: float,
        started_at: float,
        max_source_age_seconds: float | None = None,
    ) -> None:
        if idle_timeout_seconds <= 0:
            raise ValueError("ticker idle timeout must be positive")
        if max_source_age_seconds is not None and max_source_age_seconds <= 0:
            raise ValueError("ticker source age limit must be positive")
        self.idle_timeout_seconds = float(idle_timeout_seconds)
        self.max_source_age_seconds = (
            None if max_source_age_seconds is None else float(max_source_age_seconds)
        )
        self._last_ticker_at = {
            str(symbol).upper(): float(started_at) for symbol in symbols
        }

    def observe(
        self,
        message: Mapping[str, Any],
        *,
        now: float,
        wall_now: datetime | None = None,
    ) -> bool:
        arg = message.get("arg")
        data = message.get("data")
        if (
            not isinstance(arg, Mapping)
            or arg.get("topic") != "ticker"
            or not isinstance(data, list)
            or not data
        ):
            return False
        symbol = str(arg.get("symbol") or "").upper()
        if symbol not in self._last_ticker_at:
            return False
        if self.max_source_age_seconds is not None:
            item = data[0]
            source_ts = item.get("ts") if isinstance(item, Mapping) else None
            if source_ts is None:
                source_ts = message.get("ts")
            if source_ts is None or wall_now is None or wall_now.tzinfo is None:
                return False
            try:
                source_time = datetime.fromtimestamp(int(source_ts) / 1000, tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                return False
            if (wall_now - source_time).total_seconds() > self.max_source_age_seconds:
                return False
        self._last_ticker_at[symbol] = float(now)
        return True

    def expired_symbols(self, *, now: float) -> tuple[str, ...]:
        return tuple(sorted(
            symbol
            for symbol, last_ticker_at in self._last_ticker_at.items()
            if now - last_ticker_at >= self.idle_timeout_seconds
        ))

    def seconds_until_expiry(self, *, now: float) -> float | None:
        if not self._last_ticker_at:
            return None
        deadline = min(
            last_ticker_at + self.idle_timeout_seconds
            for last_ticker_at in self._last_ticker_at.values()
        )
        return max(0.0, deadline - now)

    def seconds_until_stream_expiry(self, *, now: float) -> float | None:
        """Reconnect only a silent connection, never refresh a silent coin.

        Tickers are change-driven. One quiet symbol must not cancel receives
        and disconnect the still-updating symbols sharing its connection.
        expired_symbols remains the independent per-symbol diagnostic; each
        canonical ticker retains its own original source/fetch timestamps.
        """
        if not self._last_ticker_at:
            return None
        deadline = max(
            last_ticker_at + self.idle_timeout_seconds
            for last_ticker_at in self._last_ticker_at.values()
        )
        return max(0.0, deadline - now)

    def reset(self, *, now: float) -> None:
        for symbol in self._last_ticker_at:
            self._last_ticker_at[symbol] = float(now)


def _outer_timestamp(message: Mapping[str, Any], fetched_at: datetime) -> str:
    value = message.get("ts")
    if value is None:
        return str(int(fetched_at.timestamp() * 1000))
    return str(value)


def parse_ticker_message(message: Mapping[str, Any], *, fetched_at: datetime) -> Ticker:
    arg = message.get("arg", {})
    if not isinstance(arg, Mapping) or arg.get("topic") != "ticker":
        raise ValueError("message is not a v3 ticker message")
    data = message.get("data")
    if not isinstance(data, list) or not data:
        raise ValueError("ticker message data is empty")
    payload = dict(data[0])
    payload.setdefault("symbol", arg.get("symbol"))
    payload.setdefault("ts", _outer_timestamp(message, fetched_at))
    return replace(
        parse_tickers_response({"code": "00000", "data": [payload]}, fetched_at=fetched_at)[0],
        source="bitget_v3_ws",
    )


def parse_kline_message(
    message: Mapping[str, Any], *, fetched_at: datetime, now: datetime
) -> list[Candle]:
    arg = message.get("arg", {})
    if not isinstance(arg, Mapping) or arg.get("topic") != "kline":
        raise ValueError("message is not a v3 kline message")
    symbol = str(arg.get("symbol"))
    interval = str(arg.get("interval"))
    data = message.get("data")
    if not isinstance(data, list):
        raise ValueError("kline message data must be a list")
    rows: list[list[str]] = []
    for item in data:
        if isinstance(item, Mapping):
            rows.append([
                str(item["start"]), str(item["open"]), str(item["high"]),
                str(item["low"]), str(item["close"]), str(item["volume"]),
                str(item["turnover"]),
            ])
        elif isinstance(item, (list, tuple)) and len(item) >= 7:
            rows.append([str(value) for value in item[:7]])
        else:
            raise ValueError("v3 kline data item must be an object or array")
    candles = parse_candles_response(
        {"code": "00000", "data": rows},
        symbol=symbol,
        interval=interval,
        fetched_at=fetched_at,
        now=now,
    )
    return [replace(candle, source="bitget_v3_ws") for candle in candles]
