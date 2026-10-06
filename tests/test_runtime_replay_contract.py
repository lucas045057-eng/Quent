from pathlib import Path
import asyncio
import json

import pytest

from tests.runtime_replay import load_replay, replay_signature


FIXTURE = Path(__file__).parent / "fixtures" / "runtime_replay_v1.jsonl"
CAPTURE = Path(__file__).parent / "fixtures" / "replays" / "phase6-bitget-rest-20260924.jsonl.gz"


def test_replay_contract_is_complete_ordered_and_repeatable():
    first = load_replay(FIXTURE)
    second = load_replay(FIXTURE)

    assert replay_signature(first) == replay_signature(second)
    summary = replay_signature(first)
    assert summary["input_event_total"] == 22
    assert summary["reconnect_count"] == 15
    assert summary["event_type_counts"] == {
        "fault": 15,
        "kline": 1,
        "liquidation": 2,
        "public_trade": 3,
        "ticker": 1,
    }
    assert summary["symbol_counts"] == {"BTC": 1, "BTCUSDT": 6}
    assert [fault[1] for fault in summary["fault_sequence"]] == list(range(8, 23))


def test_public_rest_cassette_has_top200_and_fixed_reset_sequence():
    events = load_replay(CAPTURE)
    summary = replay_signature(events)
    distinct_kline_symbols = {event.symbol for event in events if event.event_type == "kline"}
    assert len(distinct_kline_symbols) == 200
    assert summary["event_type_counts"]["fault"] == 35
    assert summary["event_type_counts"]["instrument"] == 805
    assert summary["event_type_counts"]["kline"] >= 79136
    assert summary["event_type_counts"]["public_trade"] > 0
    assert summary["event_type_counts"]["ticker"] > 0
    assert summary["reconnect_count"] == 15
    assert summary["fault_counts"] == {"DROP_KLINE_DURING_BOOTSTRAP": 20, "WS_RESET": 15}
    reset_events = [event for event in events if event.fault_event == "WS_RESET"]
    drop_events = [event for event in events if event.fault_event == "DROP_KLINE_DURING_BOOTSTRAP"]
    fault_orders = [event.receive_order for event in reset_events]
    assert fault_orders == sorted(fault_orders)
    assert fault_orders[0] > 80746  # reset faults begin after all captured REST bootstrap rows
    assert fault_orders[-1] < summary["input_event_total"]
    assert len({(event.symbol, event.payload["bar_open_timestamp"]) for event in drop_events}) == 20
    assert all(event.interval == "5m" for event in drop_events)
    assert [event.source for event in reset_events] == [
        "bitget-uta-v3-public", "bybit-v5-public", "hyperliquid-public",
    ] * 5


def test_captured_public_cassette_payloads_are_accepted_by_current_adapters():
    from quant_phase1.adapters.bitget_v3.parsers import (
        parse_candles_response, parse_instruments_response, parse_tickers_response,
    )
    from quant_phase1.adapters.bitget_v3.websocket import parse_kline_message, parse_ticker_message
    from quant_phase3.adapters.bitget import BitgetUTA3PublicTradeAdapter
    from quant_phase3.adapters.bybit import BybitPublicTradeAdapter
    from quant_phase3.adapters.hyperliquid import HyperliquidPublicTradeAdapter
    from quant_phase4.adapters.bitget_uta_v3 import BitgetUTA3LiquidationAdapter
    from quant_phase4.adapters.bybit_v5 import BybitV5LiquidationAdapter

    accepted = {"instrument": 0, "ticker": 0, "kline": 0, "public_trade": 0, "liquidation": 0}
    for event in load_replay(CAPTURE):
        if event.fault_event:
            continue
        fetched_at = event.fetched_at or event.event_timestamp
        if event.event_type == "instrument":
            parse_instruments_response({"code": "00000", "data": [event.payload]}, fetched_at=fetched_at)
        elif event.event_type == "ticker" and isinstance(event.payload, dict) and "arg" in event.payload:
            parse_ticker_message(event.payload, fetched_at=fetched_at)
        elif event.event_type == "ticker":
            parse_tickers_response({"code": "00000", "data": [event.payload]}, fetched_at=fetched_at)
        elif event.event_type == "kline" and isinstance(event.payload, dict):
            parse_kline_message(event.payload, fetched_at=fetched_at, now=fetched_at)
        elif event.event_type == "kline":
            parse_candles_response(
                {"code": "00000", "data": [event.payload]}, symbol=event.symbol,
                interval=event.interval or "", fetched_at=fetched_at, now=fetched_at,
            )
        elif event.event_type == "public_trade":
            if event.source.startswith("bitget-"):
                adapter = BitgetUTA3PublicTradeAdapter()
            elif event.source.startswith("bybit-"):
                adapter = BybitPublicTradeAdapter()
            else:
                adapter = HyperliquidPublicTradeAdapter()
            adapter.parse_ws_message(event.payload, fetched_at)
        elif event.event_type == "liquidation":
            adapter = (
                BitgetUTA3LiquidationAdapter()
                if event.source.startswith("bitget-") else BybitV5LiquidationAdapter()
            )
            adapter.parse_ws_message(event.payload, fetched_at)
        else:
            continue
        accepted[event.event_type] += 1
    expected = {kind: sum(event.event_type == kind and event.fault_event is None for event in load_replay(CAPTURE))
                for kind in accepted}
    assert accepted == expected


def test_recorded_payloads_reach_current_phase1_to_phase4_parsers_deterministically():
    from datetime import timedelta

    from quant_phase1.adapters.bitget_v3.websocket import parse_kline_message, parse_ticker_message
    from quant_phase3.adapters.bitget import BitgetUTA3PublicTradeAdapter
    from quant_phase3.adapters.bybit import BybitPublicTradeAdapter
    from quant_phase3.adapters.hyperliquid import HyperliquidPublicTradeAdapter
    from quant_phase3.runtime import Phase3TradeRuntime
    from quant_phase4.adapters.bitget_uta_v3 import BitgetUTA3LiquidationAdapter
    from quant_phase4.adapters.bybit_v5 import BybitV5LiquidationAdapter

    events = load_replay(FIXTURE)

    async def replay_once():
        runtime = Phase3TradeRuntime({
            "bitget": BitgetUTA3PublicTradeAdapter(),
            "bybit": BybitPublicTradeAdapter(),
            "hyperliquid": HyperliquidPublicTradeAdapter(),
        })
        await runtime.start()
        output = {"ticker": 0, "kline": 0, "public_trade": 0, "liquidation": 0, "reconnect_count": 0, "flow_windows": 0}
        for event in events:
            if event.event_type == "ticker":
                output["ticker"] += int(parse_ticker_message(event.payload, fetched_at=event.event_timestamp).status == "AVAILABLE")
            elif event.event_type == "kline":
                output["kline"] += len(parse_kline_message(
                    event.payload, fetched_at=event.event_timestamp, now=event.event_timestamp
                ))
            elif event.event_type == "public_trade":
                exchange = {
                    "fixture:bitget-v3-public-trade": "bitget",
                    "fixture:bybit-v5-public-trade": "bybit",
                    "fixture:hyperliquid-public-trade": "hyperliquid",
                }[event.source]
                output["public_trade"] += runtime.ingest_ws_message(
                    exchange, event.payload, received_at=event.event_timestamp
                )
            elif event.event_type == "liquidation":
                adapter = (
                    BitgetUTA3LiquidationAdapter()
                    if event.source.startswith("fixture:bitget")
                    else BybitV5LiquidationAdapter()
                )
                output["liquidation"] += len(adapter.parse_ws_message(event.payload, event.event_timestamp))
            if event.fault_event == "WS_RESET":
                output["reconnect_count"] += 1
        watermark = max(event.event_timestamp for event in events) + timedelta(minutes=2)
        output["flow_windows"] = len(runtime.process_all_pending(watermark=watermark, processed_at=watermark))
        await runtime.stop()
        return output

    first = asyncio.run(replay_once())
    second = asyncio.run(replay_once())
    assert first == second
    assert first == {
        "ticker": 1, "kline": 1, "public_trade": 3, "liquidation": 2,
        "reconnect_count": 15, "flow_windows": 3,
    }


def test_replay_contract_rejects_naive_timestamp_and_nonmonotonic_order(tmp_path):
    naive = tmp_path / "naive.jsonl"
    naive.write_text(
        '{"event_id":"e","source":"s","symbol":"BTC","event_type":"ticker",'
        '"event_timestamp":"2026-09-20T10:30:00","receive_order":1,"payload":{}}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="UTC"):
        load_replay(naive)

    reordered = tmp_path / "reordered.jsonl"
    reordered.write_text("\n".join(json.dumps({
        "event_id": f"e{index}", "source": "s", "symbol": "BTC", "event_type": "ticker",
        "event_timestamp": f"2026-09-20T10:30:0{index - 1}Z", "receive_order": order, "payload": {},
    }) for index, order in ((1, 2), (2, 1))), encoding="utf-8")
    with pytest.raises(ValueError, match="strictly increasing"):
        load_replay(reordered)
