from pathlib import Path

from datetime import datetime, timezone
from decimal import Decimal
import inspect
import json
import time
from types import SimpleNamespace

import aiohttp
from aiohttp import web
import pytest

from quant_phase8.adapters.deribit_rest import (
    parse_index_price_names_response,
    parse_instruments_response,
)
from quant_phase8.config import Phase8Settings
from quant_phase8.contracts import DataStatus, loads_decimal_json
from quant_phase8.adapters.deribit_ws import build_lifecycle_channels, derive_markprice_channels

from scripts import phase8_source_contract_probe as probe


FIXTURES = Path(__file__).parent / "fixtures" / "phase8"
T0 = datetime(2026, 9, 25, 16, 30, tzinfo=timezone.utc)


def test_bounded_public_probe_script_exists_as_a_manual_tool():
    repository = Path(__file__).parents[1]
    assert (repository / "scripts" / "phase8_source_contract_probe.py").is_file()


def test_probe_is_exactly_public_official_and_bounded_without_credentials():
    assert probe.REST_BASE_URL == "https://www.deribit.com/api/v2"
    assert probe.WS_URL == "wss://www.deribit.com/ws/api/v2"
    assert probe.PUBLIC_REST_METHODS == (
        "public/get_index_price_names",
        "public/get_instruments",
        "public/get_book_summary_by_currency",
    )
    assert probe.PROBE_MAX_REST_REQUESTS == 5
    assert probe.REST_MAX_ATTEMPTS == 1
    assert probe.REST_MIN_INTERVAL_SECONDS >= 1.25
    assert 0 < probe.PROBE_DEADLINE_SECONDS <= 90
    assert 0 < probe.REST_RESPONSE_BYTES_LIMIT <= 8 * 1024 * 1024
    assert 0 < probe.WS_MESSAGE_BYTES_LIMIT <= 1024 * 1024
    source = inspect.getsource(probe).lower()
    assert "private/" not in source
    assert "dotenv" not in source
    assert "os.environ" not in source
    assert "authorization" not in source


def _catalog():
    settings = Phase8Settings.from_env({
        "TRADING_MODE": "paper",
        "PHASE8_OPTIONS_ENABLED": "1",
        "PHASE8_OPTIONS_MAX_FULL_CHAIN_RECORDS_PER_UNDERLYING": "2048",
        "PHASE8_OPTIONS_MAX_FULL_CHAIN_RECORDS_TOTAL": "4096",
    })
    indexes = parse_index_price_names_response(
        loads_decimal_json((FIXTURES / "rest_index_names.json").read_text())
    )
    result = []
    for underlying in ("BTC", "ETH"):
        payload = loads_decimal_json(
            (FIXTURES / f"rest_instruments_{underlying.lower()}.json").read_text()
        )
        result.extend(parse_instruments_response(
            payload,
            requested_currency=underlying,
            supported_index_names=indexes,
            fetched_at=T0,
            processed_at=T0,
            max_records=settings.max_full_chain_records_per_underlying,
        ))
    return indexes, tuple(result)


def test_subscription_plan_derives_validated_markprice_indexes_and_only_selected_tickers():
    builder = getattr(probe, "build_subscription_plan", None)
    assert callable(builder), "probe must construct an explicit bounded subscription plan"
    indexes, catalog = _catalog()
    selected = {
        currency: (next(item.symbol for item in catalog if item.underlying == currency),)
        for currency in ("BTC", "ETH")
    }
    plan = builder(catalog, indexes, selected)
    assert plan.lifecycle_channels == build_lifecycle_channels()
    assert plan.markprice_channels == derive_markprice_channels(
        catalog, indexes, max_channels=4
    )
    assert plan.ticker_channels == tuple(
        f"incremental_ticker.{selected[currency][0]}" for currency in ("BTC", "ETH")
    )
    assert len(plan.ticker_channels) == 2
    assert all(channel.startswith("markprice.options.") for channel in plan.markprice_channels)


def test_subscription_plan_rejects_more_than_one_ticker_per_underlying():
    builder = getattr(probe, "build_subscription_plan", None)
    assert callable(builder), "probe must enforce the one-ticker-per-underlying smoke cap"
    indexes, catalog = _catalog()
    btc_symbols = tuple(item.symbol for item in catalog if item.underlying == "BTC")[:2]
    with pytest.raises(ValueError):
        builder(catalog, indexes, {"BTC": btc_symbols, "ETH": ()})


def test_smoke_selection_accepts_bounded_partial_but_rejects_stale_or_empty():
    indexes, catalog = _catalog()
    btc = next(item for item in catalog if item.underlying == "BTC")
    eth = next(item for item in catalog if item.underlying == "ETH")
    selection = SimpleNamespace(
        coverage={
            currency: SimpleNamespace(
                status=DataStatus.PARTIAL,
                reason="BOUNDED_TICKER_SAMPLE",
                eligible_count=100,
                selected_count=64,
            )
            for currency in ("BTC", "ETH")
        },
        selected={"BTC": (btc,), "ETH": (eth,)},
    )
    selected, summary = probe.select_smoke_tickers(selection)
    assert selected == {"BTC": (btc.symbol,), "ETH": (eth.symbol,)}
    assert summary["BTC"]["status"] == "PARTIAL"
    selection.coverage["BTC"].status = DataStatus.STALE
    with pytest.raises(ValueError):
        probe.select_smoke_tickers(selection)
    selection.coverage["BTC"].status = DataStatus.PARTIAL
    selection.selected["ETH"] = ()
    with pytest.raises(ValueError):
        probe.select_smoke_tickers(selection)


def test_subscription_ack_must_match_request_and_exact_channel_set():
    validator = getattr(probe, "validate_subscription_ack", None)
    assert callable(validator), "probe must validate every public subscription acknowledgement"
    channels = ["instrument.creation.option.BTC", "instrument.state.option.BTC"]
    ack = {"jsonrpc": "2.0", "id": 11, "result": channels}
    validator(ack, request_id=11, expected_channels=channels)
    with pytest.raises(ValueError):
        validator({**ack, "result": channels[:1]}, request_id=11, expected_channels=channels)
    with pytest.raises(ValueError):
        validator({**ack, "id": 12}, request_id=11, expected_channels=channels)
    with pytest.raises(ValueError):
        validator({"jsonrpc": "2.0", "id": 11, "error": {"message": "denied"}},
                  request_id=11, expected_channels=channels)


def test_ticker_smoke_merge_preserves_sparse_fields_and_discards_out_of_order_update():
    merge = getattr(probe, "apply_ticker_message", None)
    assert callable(merge), "probe must use canonical snapshot/change merge semantics"
    symbol = "BTC-30OCT26-100000-C"
    selected = {symbol}
    state = {}
    snapshot = loads_decimal_json((FIXTURES / "ws_ticker_snapshot.json").read_text())
    state_by_symbol, event = merge(snapshot, state, selected_symbols=selected, received_at=T0, processed_at=T0)
    assert event.is_snapshot is True
    before = state_by_symbol[symbol]
    prior_mark_iv = before["mark_iv"]
    changed = loads_decimal_json((FIXTURES / "ws_ticker_change.json").read_text())
    state_by_symbol, event = merge(changed, state_by_symbol, selected_symbols=selected,
                                   received_at=T0, processed_at=T0)
    assert event.is_snapshot is False
    assert state_by_symbol[symbol]["mark_iv"] == prior_mark_iv
    assert state_by_symbol[symbol]["delta"].value == Decimal("0.502")
    assert event.merge_verified is True
    assert event.omitted_field_count > 0
    out_of_order = loads_decimal_json((FIXTURES / "ws_ticker_out_of_order.json").read_text())
    state_by_symbol, event = merge(out_of_order, state_by_symbol, selected_symbols=selected,
                                   received_at=T0, processed_at=T0)
    assert event.is_snapshot is False
    assert state_by_symbol[symbol]["delta"].value == Decimal("0.502")


def test_bounded_websocket_decoder_rejects_oversized_and_non_json_messages():
    decoder = getattr(probe, "decode_bounded_ws_message", None)
    assert callable(decoder), "probe must enforce a byte cap before decoding each frame"
    assert decoder(b'{"jsonrpc":"2.0"}', max_bytes=32)["jsonrpc"] == "2.0"
    with pytest.raises(ValueError):
        decoder(b"x" * 33, max_bytes=32)
    with pytest.raises(ValueError):
        decoder(b"not-json", max_bytes=32)


def test_public_report_contains_no_raw_payload_or_metric_values():
    serializer = getattr(probe, "serialize_public_report", None)
    assert callable(serializer), "probe output must be an allowlisted sanitized summary"
    report = serializer({
        "status": "PASS",
        "request_count": 5,
        "response_bytes": 1234,
        "latency_ms": 456,
        "payload": {"secret_marker": "must-not-escape", "raw": "full payload"},
    })
    encoded = json.dumps(report)
    assert "must-not-escape" not in encoded
    assert "full payload" not in encoded
    assert "payload" not in report


@pytest.mark.asyncio
async def test_rest_429_is_single_attempt_and_failure_record_is_sanitized():
    calls = 0

    async def too_many_requests(_request):
        nonlocal calls
        calls += 1
        return web.json_response({"message": "secret-marker-must-not-escape"}, status=429)

    app = web.Application()
    app.router.add_post("/{method:.*}", too_many_requests)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        budget = probe.RestRequestBudget(max_requests=1, min_interval_seconds=0)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as session:
            with pytest.raises(probe.ProbeFailure) as captured:
                await probe.post_jsonrpc_once(
                    session,
                    base_url=f"http://127.0.0.1:{port}/api/v2",
                    method="public/get_index_price_names",
                    params={},
                    request_id=1,
                    budget=budget,
                )
        assert calls == 1
        assert budget.calls == 1
        assert captured.value.record["status"] == "HTTP_429"
        assert captured.value.record["error_category"] == "HTTP_STATUS"
        assert "secret-marker-must-not-escape" not in repr(captured.value.record)
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_rest_budget_enforces_spacing_and_hard_request_cap():
    budget = probe.RestRequestBudget(max_requests=2, min_interval_seconds=0.02)
    first = await budget.begin()
    budget.complete()
    second = await budget.begin()
    assert second - first >= 0.018
    assert budget.intervals_ms[0] >= 20
    budget.complete()
    with pytest.raises(ValueError, match="budget exhausted"):
        await budget.begin()


@pytest.mark.asyncio
async def test_rest_spacing_is_enforced_at_actual_http_dispatch():
    minimum_interval = 0.1
    arrivals = []

    async def respond(request):
        arrivals.append(time.monotonic())
        body = await request.json()
        return web.json_response({"jsonrpc": "2.0", "id": body["id"], "result": []})

    app = web.Application()
    app.router.add_post("/{method:.*}", respond)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    class DelayedFirstDispatch:
        def __init__(self, client):
            self.client = client
            self.calls = 0

        def post(self, *args, **kwargs):
            if self.calls == 0:
                time.sleep(minimum_interval * 0.8)
            self.calls += 1
            return self.client.post(*args, **kwargs)

    try:
        budget = probe.RestRequestBudget(max_requests=2, min_interval_seconds=minimum_interval)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as client:
            session = DelayedFirstDispatch(client)
            for request_id in (1, 2):
                await probe.post_jsonrpc_once(
                    session,
                    base_url=f"http://127.0.0.1:{port}/api/v2",
                    method="public/get_index_price_names",
                    params={},
                    request_id=request_id,
                    budget=budget,
                )
        assert len(arrivals) == 2
        assert arrivals[1] - arrivals[0] >= minimum_interval
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_rest_response_body_is_rejected_above_byte_cap_without_echoing_body():
    marker = "full-response-must-not-escape"

    async def oversized(_request):
        return web.json_response({"payload": marker})

    app = web.Application()
    app.router.add_post("/{method:.*}", oversized)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=2)) as session:
            with pytest.raises(probe.ProbeFailure) as captured:
                await probe.post_jsonrpc_once(
                    session,
                    base_url=f"http://127.0.0.1:{port}/api/v2",
                    method="public/get_index_price_names",
                    params={},
                    request_id=1,
                    budget=probe.RestRequestBudget(max_requests=1, min_interval_seconds=0),
                    max_response_bytes=8,
                )
        assert captured.value.record["error_category"] == "RESPONSE_TOO_LARGE"
        assert marker not in repr(captured.value.record)
    finally:
        await runner.cleanup()
