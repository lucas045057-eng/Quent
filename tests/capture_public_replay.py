"""Capture a secret-free Bitget public REST replay cassette for diagnostics.

Run manually; output is written only to the supplied path. This is test tooling,
not part of either production entrypoint.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import websockets

from quant_phase1.adapters.bitget_v3.rest import BitgetV3UtaRestClient
from quant_phase1.config import Settings
from quant_phase1.pipeline import INTERVALS, MarketDataCollector


def _row(event_id: str, source: str, symbol: str, event_type: str, timestamp: datetime,
         fetched_at: datetime, receive_order: int, payload: Any, *, interval: str | None = None) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "source": source,
        "exchange": "bitget",
        "symbol": symbol,
        "event_type": event_type,
        "event_timestamp": timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "exchange_timestamp": timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "fetched_at": fetched_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "processed_at": fetched_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "receive_order": receive_order,
        "interval": interval,
        "payload": payload,
    }


async def capture(output: Path, *, universe_limit: int, kline_limit: int) -> dict[str, Any]:
    settings = Settings.from_env({
        "TRADING_MODE": "paper",
        "UNIVERSE_LIMIT": str(universe_limit),
        "KLINE_FETCH_LIMIT": str(kline_limit),
    })
    async with BitgetV3UtaRestClient(settings) as client:
        batch = await MarketDataCollector(
            client, max_symbols=universe_limit, kline_limit=kline_limit
        ).collect_once()

    rows: list[dict[str, Any]] = []
    order = 0
    for instrument in sorted(batch.instruments, key=lambda item: item.symbol):
        order += 1
        rows.append(_row(
            f"rest-instrument:{instrument.symbol}", instrument.source, instrument.symbol,
            "instrument", instrument.exchange_timestamp or instrument.fetched_at,
            instrument.fetched_at, order, dict(instrument.raw_payload),
        ))
    for ticker in sorted(batch.tickers, key=lambda item: item.symbol):
        order += 1
        rows.append(_row(
            f"rest-ticker:{ticker.symbol}:{ticker.exchange_timestamp.isoformat()}", ticker.source,
            ticker.symbol, "ticker", ticker.exchange_timestamp, ticker.fetched_at, order,
            dict(ticker.raw_payload),
        ))
    for symbol in sorted(batch.candles_by_symbol):
        for interval in INTERVALS:
            for candle in batch.candles_by_symbol[symbol].get(interval, ()):
                order += 1
                rows.append(_row(
                    f"rest-kline:{symbol}:{interval}:{candle.bar_open_timestamp.isoformat()}",
                    candle.source, symbol, "kline", candle.exchange_timestamp, candle.fetched_at,
                    order, candle.raw_payload, interval=interval,
                ))

    ws_symbols = tuple(batch.selected_symbols[:20])
    bybit_symbols = tuple(symbol for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT") if symbol in ws_symbols)
    capture_end = asyncio.get_running_loop().time() + 20.0
    ws_results: dict[str, str] = {}

    async def capture_socket(source: str, url: str, subscriptions: list[dict[str, Any]]) -> None:
        nonlocal order
        try:
            async with websockets.connect(
                url, open_timeout=8, ping_interval=20, close_timeout=2, proxy=None
            ) as socket:
                for subscription in subscriptions:
                    await socket.send(json.dumps(subscription, separators=(",", ":")))
                while asyncio.get_running_loop().time() < capture_end:
                    try:
                        raw_message = await asyncio.wait_for(socket.recv(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    received_at = datetime.now(timezone.utc)
                    try:
                        payload = json.loads(raw_message)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if not isinstance(payload, dict):
                        continue
                    metadata = _ws_metadata(source, payload, received_at)
                    if metadata is None:
                        continue
                    symbol, event_type, event_at, interval = metadata
                    order += 1
                    row = _row(
                        f"ws:{source}:{order:08d}", source, symbol, event_type,
                        event_at, received_at, order, payload, interval=interval,
                    )
                    row["connection_generation"] = 1
                    rows.append(row)
            ws_results[source] = "CONNECTED"
        except Exception as exc:
            ws_results[source] = f"UNAVAILABLE:{type(exc).__name__}"

    bitget_topics = ("ticker", "publicTrade", "kline", "liquidation")
    bitget_jobs = []
    for topic in bitget_topics:
        args = []
        for symbol in ws_symbols:
            arg: dict[str, str] = {"instType": "usdt-futures", "topic": topic, "symbol": symbol}
            if topic == "kline":
                arg["interval"] = "5m"
            args.append(arg)
        bitget_jobs.append(capture_socket(
            f"bitget-uta-v3-{topic}", "wss://ws.bitget.com/v3/ws/public",
            [{"op": "subscribe", "args": args}],
        ))
    bybit_args = [f"publicTrade.{symbol}" for symbol in bybit_symbols]
    bybit_args.extend(f"allLiquidation.{symbol}" for symbol in bybit_symbols)
    jobs = [
        *bitget_jobs,
        capture_socket("bybit-v5-public", "wss://stream.bybit.com/v5/public/linear",
                       [{"op": "subscribe", "args": bybit_args}]),
    ]
    hl_coins = tuple(symbol.removesuffix("USDT") for symbol in bybit_symbols)
    jobs.append(capture_socket(
        "hyperliquid-public", "wss://api.hyperliquid.xyz/ws",
        [{"method": "subscribe", "subscription": {"type": "trades", "coin": coin}} for coin in hl_coins],
    ))
    await asyncio.gather(*jobs)

    # Insert deterministic dropped-bar controls before the corresponding
    # recorded raw row. During replay, REST recovery may return that same
    # captured row; no market value is synthesized.
    targets: dict[str, dict[str, Any]] = {}
    for symbol in sorted({row["symbol"] for row in rows
                          if row["event_type"] == "kline" and row.get("interval") == "5m"
                          and row["source"] == "bitget_v3_rest"})[:20]:
        matching = [row for row in rows if row["event_type"] == "kline" and row.get("interval") == "5m"
                    and row["source"] == "bitget_v3_rest" and row["symbol"] == symbol]
        target = max(matching, key=lambda row: row["event_timestamp"])
        targets[target["event_id"]] = target

    rest_rows: list[dict[str, Any]] = []
    ws_rows: list[dict[str, Any]] = []
    for row in rows:
        if row["source"].startswith(("bitget-uta-v3-", "bybit-v5-", "hyperliquid-")):
            ws_rows.append(row)
            continue
        target = targets.get(row["event_id"])
        if target is not None:
            drop = _row(
                f"fault-drop-kline:{target['symbol']}:{target['event_timestamp']}",
                "bitget_v3_rest", target["symbol"], "fault", datetime.fromisoformat(
                    target["fetched_at"].replace("Z", "+00:00")
                ), datetime.fromisoformat(target["fetched_at"].replace("Z", "+00:00")),
                0, {"bar_open_timestamp": target["event_timestamp"]}, interval="5m",
            )
            drop["fault_event"] = "DROP_KLINE_DURING_BOOTSTRAP"
            rest_rows.append(drop)
        rest_rows.append(row)

    # Interleave the same 15 deterministic reset controls with the captured
    # WS stream. They are test controls, never represented as exchange data.
    ws_sources = ("bitget-uta-v3-", "bybit-v5-", "hyperliquid-")
    fault_sources = ("bitget-uta-v3-public", "bybit-v5-public", "hyperliquid-public")
    interleaved = list(rest_rows)
    previous_cut = 0
    for reset_index in range(15):
        next_cut = ((reset_index + 1) * len(ws_rows)) // 16
        interleaved.extend(ws_rows[previous_cut:next_cut])
        previous_cut = next_cut
        source = fault_sources[reset_index % len(fault_sources)]
        anchor = ws_rows[next_cut - 1] if next_cut else (ws_rows[0] if ws_rows else None)
        fault_at = (
            datetime.fromisoformat(anchor["fetched_at"].replace("Z", "+00:00"))
            if anchor else datetime.now(timezone.utc)
        )
        row = _row(
            f"fault-ws-reset-{reset_index + 1:03d}", source, "*", "fault", fault_at,
            fault_at, 0, {},
        )
        row.update({
            "fault_event": "WS_RESET",
            "connection_generation": reset_index // len(fault_sources) + 1,
        })
        interleaved.append(row)
    interleaved.extend(ws_rows[previous_cut:])
    for receive_order, row in enumerate(interleaved, 1):
        row["receive_order"] = receive_order
    rows = interleaved

    output.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with gzip.open(output, "wt", encoding="utf-8", newline="\n", compresslevel=6) as stream:
        for row in rows:
            encoded = json.dumps(row, sort_keys=True, separators=(",", ":"))
            digest.update((encoded + "\n").encode("utf-8"))
            stream.write(encoded + "\n")
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["event_type"]] = counts.get(row["event_type"], 0) + 1
    capture_finished_at = datetime.now(timezone.utc)
    return {
        "path": str(output),
        "capture_finished_at": capture_finished_at.isoformat(),
        "event_total": len(rows),
        "symbol_count": len(batch.selected_symbols),
        "kline_count": counts.get("kline", 0),
        "event_type_counts": counts,
        "canonical_sha256": digest.hexdigest(),
        "compressed_bytes": output.stat().st_size,
        "selected_symbol_count": len(batch.selected_symbols),
        "fixed_ws_reset_count": 15,
        "fixed_kline_drop_count": len(targets),
        "public_ws_status": ws_results,
        "public_ws_event_count": sum(row["event_type"] in {"public_trade", "liquidation", "ticker", "kline"}
                                      for row in rows if row["source"].startswith(("bitget-", "bybit-", "hyperliquid-"))),
    }


def _ws_metadata(
    source: str, payload: dict[str, Any], received_at: datetime
) -> tuple[str, str, datetime, str | None] | None:
    def timestamp(value: Any) -> datetime:
        try:
            return datetime.fromtimestamp(int(value) / 1000, timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return received_at

    if source.startswith("bitget-"):
        arg = payload.get("arg")
        data = payload.get("data")
        if not isinstance(arg, dict) or not isinstance(data, list) or not data:
            return None
        topic = arg.get("topic")
        symbol = str(arg.get("symbol") or (data[0].get("symbol") if isinstance(data[0], dict) else "*"))
        interval = arg.get("interval")
        if topic == "ticker":
            event_at = timestamp(payload.get("ts"))
            event_type = "ticker"
        elif topic == "kline":
            event_at = timestamp(data[0].get("start") if isinstance(data[0], dict) else None)
            event_type = "kline"
        elif topic == "publicTrade":
            event_at = timestamp(data[0].get("T") if isinstance(data[0], dict) else None)
            event_type = "public_trade"
        elif topic == "liquidation":
            event_at = timestamp(data[0].get("ts") if isinstance(data[0], dict) else None)
            event_type = "liquidation"
        else:
            return None
        return symbol, event_type, event_at, interval
    if source == "bybit-v5-public":
        topic = payload.get("topic")
        data = payload.get("data")
        if not isinstance(topic, str) or not isinstance(data, list) or not data:
            return None
        if topic.startswith("publicTrade."):
            event_type = "public_trade"
            row_ts = data[0].get("T") if isinstance(data[0], dict) else None
        elif topic.startswith("allLiquidation."):
            event_type = "liquidation"
            row_ts = data[0].get("T") if isinstance(data[0], dict) else None
        else:
            return None
        return topic.rsplit(".", 1)[-1], event_type, timestamp(row_ts), None
    if source == "hyperliquid-public":
        if payload.get("channel") != "trades" or not isinstance(payload.get("data"), list) or not payload["data"]:
            return None
        row = payload["data"][0]
        if not isinstance(row, dict):
            return None
        return str(row.get("coin", "*")), "public_trade", timestamp(row.get("time")), None
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--universe-limit", type=int, default=200)
    parser.add_argument("--kline-limit", type=int, default=100)
    args = parser.parse_args()
    result = asyncio.run(capture(args.output, universe_limit=args.universe_limit, kline_limit=args.kline_limit))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
