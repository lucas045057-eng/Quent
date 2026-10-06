from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import weakref

import pytest
from pydantic import SecretStr

from quant_phase1.logging import SecretRedactingFilter
from quant_phase1.config import Phase7RpcSourceSettings, Settings
from quant_phase1.entrypoints.collector import CollectorService
from quant_phase7.runtime import (
    Phase7CollectorRuntime,
    Phase7EngineRuntime,
    Phase7JsonRpcClient,
    Phase7RateLimitedError,
    Phase7RpcError,
)
from quant_phase7.bitcoin import BitcoinBlockParser
from quant_phase7.contracts import DataStatus
from quant_data_layer.admission import ReplayClass
from quant_data_layer.db_admission import (
    DbAdmissionDeferred,
    DbWorkClass as DbTransactionWorkClass,
)
from quant_data_layer.observability import WorkClass


def _settings() -> Settings:
    return Settings.from_env(
        {
            "TRADING_MODE": "paper",
            "PHASE2_ENABLED": "0",
            "PHASE3_ENABLED": "0",
            "PHASE4_ENABLED": "0",
            "PHASE5_ENABLED": "0",
            "PHASE6_ENABLED": "0",
            "PHASE7_BITCOIN_RPC_ENABLED": "0",
            "PHASE7_ETHEREUM_RPC_ENABLED": "0",
        }
    )


def test_collector_owns_phase7_runtime_without_starting_network_io():
    settings = _settings()

    collector = CollectorService(settings)

    assert isinstance(collector.phase7_runtime, Phase7CollectorRuntime)
    assert collector.phase7_runtime.settings is settings


@pytest.mark.asyncio
async def test_ethereum_receipt_block_parser_runs_under_bounded_admission_after_rpc():
    runtime = Phase7CollectorRuntime(_settings())
    state = {"active": False, "requests": [], "parser_active": []}

    class AdmissionSpy:
        @asynccontextmanager
        async def admit(self, request):
            state["requests"].append(request)
            state["active"] = True
            try:
                yield None
            finally:
                state["active"] = False

    class Client:
        response_bytes_total = 0

        async def call(self, method, _params):
            self.response_bytes_total += 128
            if method == "eth_getBlockByNumber":
                return {"hash": "0xabc", "transactions": []}
            return []

        async def call_batch(self, _calls):
            return []

    class Parser:
        def parse_block(self, *_args, **_kwargs):
            state["parser_active"].append(state["active"])
            return ()

    runtime.admission = AdmissionSpy()
    await runtime._ethereum_block(
        Client(), Parser(), 10, "0x1", datetime(2026, 9, 26, 7, 0, tzinfo=timezone.utc)
    )

    assert state["parser_active"] == [True]
    assert state["requests"][0].work_class is WorkClass.HEAVY
    assert state["requests"][0].replay_class is ReplayClass.RECOVERABLE_REPLAYABLE


@pytest.mark.asyncio
async def test_bitcoin_block_parse_valuation_and_atomic_checkpoint_share_admission():
    runtime = Phase7CollectorRuntime(_settings())
    state = {"active": False, "parse_active": [], "persist_active": [], "requests": []}

    class AdmissionSpy:
        @asynccontextmanager
        async def admit(self, request):
            state["requests"].append(request)
            state["active"] = True
            try:
                yield None
            finally:
                state["active"] = False

    class Parser:
        finalized_depth = 6

        def parse_block(self, *_args, **_kwargs):
            state["parse_active"].append(state["active"])
            return ("event",)

    class Repository:
        def persist_transfer_chunks_and_checkpoint(self, events, row):
            state["persist_active"].append(state["active"])
            assert tuple(events) == ("event",)
            assert row == {"cursor": 10}

    runtime.admission = AdmissionSpy()
    runtime._iter_event_time_prices = lambda _repository, events, _symbol: events
    runtime._checkpoint = lambda *_args, **_kwargs: SimpleNamespace(to_row=lambda: {"cursor": 10})

    assert hasattr(runtime, "_process_bitcoin_block")
    count = await runtime._process_bitcoin_block(
        Repository(), Parser(), {"transactions": []}, block_hash="hash-10", height=10,
        observed_at=datetime(2026, 9, 26, 7, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 9, 26, 7, 0, tzinfo=timezone.utc),
        response_bytes=1024,
    )

    assert count == 1
    assert state["parse_active"] == [True]
    assert state["persist_active"] == [True]
    assert state["requests"][0].work_class is WorkClass.HEAVY
    assert state["requests"][0].replay_class is ReplayClass.RECOVERABLE_REPLAYABLE


@pytest.mark.asyncio
async def test_ethereum_valuation_and_checkpoint_persistence_share_admission():
    runtime = Phase7CollectorRuntime(_settings())
    state = {"active": False, "valuation_active": [], "persist_active": [], "requests": []}

    class AdmissionSpy:
        @asynccontextmanager
        async def admit(self, request):
            state["requests"].append(request)
            state["active"] = True
            try:
                yield None
            finally:
                state["active"] = False

    class Repository:
        def persist_transfer_batch_and_checkpoint(self, events, row):
            state["persist_active"].append(state["active"])
            assert tuple(events) == ("priced-event",)
            assert row == {"cursor": 11}

    runtime.admission = AdmissionSpy()
    runtime._apply_event_time_prices = lambda _repository, events, _symbol: (
        state["valuation_active"].append(state["active"]) or ("priced-event",)
    )
    runtime._checkpoint = lambda *_args, **_kwargs: SimpleNamespace(to_row=lambda: {"cursor": 11})

    assert hasattr(runtime, "_persist_ethereum_block")
    count = await runtime._persist_ethereum_block(
        Repository(), ("event",), height=11, block_hash="eth-hash"
    )

    assert count == 1
    assert state["valuation_active"] == [True]
    assert state["persist_active"] == [True]
    assert state["requests"][0].replay_class is ReplayClass.RECOVERABLE_REPLAYABLE


@pytest.mark.asyncio
async def test_bitcoin_block_and_cursor_persistence_use_cross_process_db_admission(monkeypatch):
    runtime = Phase7CollectorRuntime(_settings())
    state = {"db_active": False, "calls": [], "persisted": []}

    class LocalAdmission:
        @asynccontextmanager
        async def admit(self, _request):
            yield None

    class DatabaseAdmission:
        @contextmanager
        def transaction(self, dsn, *, work_class, timeout_seconds, identity, business_connection):
            state["calls"].append((dsn, work_class, timeout_seconds, identity, business_connection))
            state["db_active"] = True
            try:
                yield object()
            finally:
                state["db_active"] = False

    class Parser:
        finalized_depth = 6

        def parse_block(self, *_args, **_kwargs):
            return ("event-1", "event-2")

    class Writer:
        def persist_transfer_chunks_and_checkpoint(self, events, checkpoint):
            state["persisted"].append((tuple(events), checkpoint, state["db_active"]))

    monkeypatch.setattr("quant_phase7.runtime.Phase7Repository", lambda _connection: Writer())
    runtime.admission = LocalAdmission()
    runtime.db_admission = DatabaseAdmission()
    runtime._iter_event_time_prices = lambda _repository, events, _symbol: events
    runtime._checkpoint = lambda *_args, **_kwargs: SimpleNamespace(to_row=lambda: {"cursor": 10})
    read_repository = SimpleNamespace(connection=object())

    await runtime._process_bitcoin_block(
        read_repository, Parser(), {"transactions": []}, block_hash="hash-10", height=10,
        observed_at=datetime(2026, 9, 26, 7, 0, tzinfo=timezone.utc),
        fetched_at=datetime(2026, 9, 26, 7, 0, tzinfo=timezone.utc), response_bytes=1024,
    )

    assert state["calls"] == [(
        _settings().postgres_dsn,
        DbTransactionWorkClass.HEAVY,
        2.0,
        "phase7-bitcoin-block-10",
        read_repository.connection,
    )]
    assert state["persisted"] == [(("event-1", "event-2"), {"cursor": 10}, True)]


@pytest.mark.asyncio
async def test_ethereum_receipt_and_checkpoint_persistence_use_cross_process_db_admission(monkeypatch):
    runtime = Phase7CollectorRuntime(_settings())
    state = {"db_active": False, "calls": [], "persisted": []}

    class LocalAdmission:
        @asynccontextmanager
        async def admit(self, _request):
            yield None

    class DatabaseAdmission:
        @contextmanager
        def transaction(self, dsn, *, work_class, timeout_seconds, identity, business_connection):
            state["calls"].append((dsn, work_class, timeout_seconds, identity, business_connection))
            state["db_active"] = True
            try:
                yield object()
            finally:
                state["db_active"] = False

    class Writer:
        def persist_transfer_batch_and_checkpoint(self, events, checkpoint):
            state["persisted"].append((tuple(events), checkpoint, state["db_active"]))

    monkeypatch.setattr("quant_phase7.runtime.Phase7Repository", lambda _connection: Writer())
    runtime.admission = LocalAdmission()
    runtime.db_admission = DatabaseAdmission()
    runtime._apply_event_time_prices = lambda _repository, events, _symbol: events
    runtime._checkpoint = lambda *_args, **_kwargs: SimpleNamespace(to_row=lambda: {"cursor": 11})
    read_repository = SimpleNamespace(connection=object())

    result = await runtime._persist_ethereum_block(
        read_repository, ("receipt-event",), height=11, block_hash="eth-hash"
    )

    assert result == 1
    assert state["calls"] == [(
        _settings().postgres_dsn,
        DbTransactionWorkClass.HEAVY,
        2.0,
        "phase7-ethereum-block-11",
        read_repository.connection,
    )]
    assert state["persisted"] == [(("receipt-event",), {"cursor": 11}, True)]


@pytest.mark.asyncio
async def test_database_admission_deferral_prevents_bitcoin_checkpoint_write(monkeypatch):
    runtime = Phase7CollectorRuntime(_settings())
    writes = []

    class LocalAdmission:
        @asynccontextmanager
        async def admit(self, _request):
            yield None

    class DatabaseAdmission:
        @contextmanager
        def transaction(self, *_args, **_kwargs):
            from quant_data_layer.db_admission import DbAdmissionDeferredReason

            raise DbAdmissionDeferred(DbAdmissionDeferredReason.SLOT_TIMEOUT)
            yield  # pragma: no cover - make this a generator context manager

    class Parser:
        finalized_depth = 6

        def parse_block(self, *_args, **_kwargs):
            return ("event",)

    class Writer:
        def persist_transfer_chunks_and_checkpoint(self, *_args):
            writes.append("checkpoint")

    monkeypatch.setattr("quant_phase7.runtime.Phase7Repository", lambda _connection: Writer())
    runtime.admission = LocalAdmission()
    runtime.db_admission = DatabaseAdmission()
    runtime._iter_event_time_prices = lambda _repository, events, _symbol: events
    runtime._checkpoint = lambda *_args, **_kwargs: SimpleNamespace(to_row=lambda: {"cursor": 10})
    read_repository = SimpleNamespace(connection=object())
    with pytest.raises(DbAdmissionDeferred):
        await runtime._process_bitcoin_block(
            read_repository, Parser(), {"transactions": []}, block_hash="hash-10", height=10,
            observed_at=datetime(2026, 9, 26, 7, 0, tzinfo=timezone.utc),
            fetched_at=datetime(2026, 9, 26, 7, 0, tzinfo=timezone.utc), response_bytes=1024,
        )
    assert writes == []


def test_phase7_diagnostics_snapshot_is_read_only_and_secret_safe():
    runtime = Phase7CollectorRuntime(_settings())
    runtime._source_stages["bitcoin_rpc"] = "BLOCK_PARSE"
    runtime._active_rpc_clients["bitcoin_rpc"] = SimpleNamespace(
        active_requests=1,
        request_count=7,
        last_response_bytes=1234,
        response_bytes_total=5678,
        last_method="getblock",
    )
    runtime._spot_events["BTCUSDT"].append(SimpleNamespace(
        event_timestamp=datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    ))
    runtime._spot_event_keys["BTCUSDT"].add(("safe", "id", "1"))

    first = runtime.diagnostics_snapshot()
    second = runtime.diagnostics_snapshot()

    assert first == second
    assert first["phase7_spot_pending_trades"] == 1
    assert first["phase7_spot_pending_windows"] == 1
    assert first["phase7_bitcoin_active_rpc"] == 1
    assert first["phase7_bitcoin_active_block_processing"] == 1
    assert first["phase7_bitcoin_response_bytes_cycle"] == 5678
    assert first["phase7_bitcoin_last_rpc_method"] == "getblock"
    assert first["phase7_bitcoin_stage"] == "BLOCK_PARSE"
    assert "endpoint" not in repr(first).lower()
    assert len(runtime._spot_events["BTCUSDT"]) == 1


def test_collector_diagnostics_snapshot_counts_buffers_without_consuming_them(tmp_path):
    from quant_phase1.gap_recovery import GapRecoveryCoordinator

    collector = CollectorService(_settings())
    collector.events.append({"payload": "must-not-escape"})
    async def recover(_symbol, _interval):
        return ()

    collector.gap_recovery = GapRecoveryCoordinator(
        recover,
        symbols_provider=lambda: (),
        on_candles=lambda _result: None,
        max_work_items=5,
    )
    collector.gap_recovery._queue.put_nowait(("BTCUSDT", "5m"))
    collector.gap_recovery._pending.add(("BTCUSDT", "5m"))

    first = collector.diagnostics_snapshot()
    second = collector.diagnostics_snapshot()

    assert first == second
    assert first["collector_event_buffer_depth"] == 1
    assert first["phase1_recovery_queue_depth"] == 1
    assert first["phase1_recovery_queue_capacity"] == 5
    assert first["phase1_db_writer_pending_batches"] == 0
    assert first["phase1_db_writer_mode"] == "SYNCHRONOUS"
    assert "must-not-escape" not in repr(first)
    assert len(collector.events._items) == 1
    assert collector.gap_recovery._queue.qsize() == 1

    collector.diagnostics_path = tmp_path / "phase7-diagnostics.json"
    collector._write_diagnostics_snapshot()
    saved = json.loads(collector.diagnostics_path.read_text(encoding="utf-8"))
    assert saved["collector_event_buffer_depth"] == 1
    assert "must-not-escape" not in repr(saved)


def test_collector_starts_phase7_before_blocking_market_bootstrap(monkeypatch):
    import quant_phase1.entrypoints.collector as collector_module

    settings = Settings.from_env({
        "TRADING_MODE": "paper",
        "PHASE2_ENABLED": "0",
        "PHASE3_ENABLED": "0",
        "PHASE4_ENABLED": "0",
        "PHASE5_ENABLED": "0",
        "PHASE6_ENABLED": "0",
        "PHASE7_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_ENABLED": "0",
        "PHASE7_ETHEREUM_RPC_ENABLED": "0",
    })
    bootstrap_entered = asyncio.Event()
    phase7_started = asyncio.Event()
    phase7_stopped = asyncio.Event()

    class FakeRestClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class BlockingMarketDataCollector:
        def __init__(self, *_args, **_kwargs):
            pass

        async def collect_once(self):
            bootstrap_entered.set()
            await asyncio.Future()

    class FakePhase7Runtime:
        async def run(self, stop_event):
            phase7_started.set()
            try:
                await stop_event.wait()
            finally:
                phase7_stopped.set()

    monkeypatch.setattr(collector_module, "BitgetV3UtaRestClient", FakeRestClient)
    monkeypatch.setattr(collector_module, "MarketDataCollector", BlockingMarketDataCollector)
    collector = CollectorService(settings)
    collector.phase7_runtime = FakePhase7Runtime()

    async def exercise():
        task = asyncio.create_task(collector.run())
        try:
            await asyncio.wait_for(bootstrap_entered.wait(), timeout=0.2)
            assert phase7_started.is_set()
        finally:
            collector.stop_event.set()
            await asyncio.wait_for(task, timeout=1)

    asyncio.run(exercise())
    assert phase7_stopped.is_set()


class _Content:
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def read(self, limit: int) -> bytes:
        return self.body[:limit]

    async def iter_chunked(self, chunk_size: int):
        for start in range(0, len(self.body), chunk_size):
            yield self.body[start:start + chunk_size]


class _Response:
    def __init__(
        self,
        payload: dict[str, object],
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = _Content(json.dumps(payload).encode())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls = []

    def post(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        return self.response


def test_json_rpc_client_is_read_only_and_parses_bounded_response():
    session = _Session(_Response({"result": {"chain": "main"}, "error": None, "id": 1}))
    client = Phase7JsonRpcClient(
        Phase7RpcSourceSettings(
            "bitcoin_rpc", True, "https://rpc.example.invalid/opaque", requests_per_second=100,
        ),
        session,
        sleep=lambda _delay: asyncio.sleep(0),
    )

    result = asyncio.run(client.call("getblockchaininfo"))

    assert result == {"chain": "main"}
    assert session.calls[0][1]["json"]["method"] == "getblockchaininfo"
    assert session.calls[0][1]["json"]["jsonrpc"] == "1.0"
    assert "Authorization" not in session.calls[0][1]
    with pytest.raises(Phase7RpcError, match="method is not approved"):
        asyncio.run(client.call("sendrawtransaction", ["never-used"]))
    assert len(session.calls) == 1


def test_json_rpc_transport_reports_active_call_and_response_bytes_read_only():
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingResponse(_Response):
        async def __aenter__(self):
            entered.set()
            await release.wait()
            return self

    session = _Session(BlockingResponse({"result": {"chain": "main"}, "error": None, "id": 1}))
    client = Phase7JsonRpcClient(
        Phase7RpcSourceSettings(
            "bitcoin_rpc", True, "https://rpc.example.invalid/opaque", requests_per_second=100,
        ),
        session,
        sleep=lambda _delay: asyncio.sleep(0),
    )

    async def exercise():
        request = asyncio.create_task(client.call("getblockchaininfo"))
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert client.active_requests == 1
        release.set()
        assert await request == {"chain": "main"}

    asyncio.run(exercise())

    assert client.active_requests == 0
    assert client.request_count == 1
    assert client.last_response_bytes == len(session.response.content.body)
    assert client.last_method == "getblockchaininfo"


def test_bitcoin_api_key_header_auth_is_sent_by_the_formal_rpc_transport():
    secret = "test-secret-value"
    config = Settings.from_env({
        "PHASE7_BITCOIN_RPC_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_URL": "https://rpc.example.invalid/opaque",
        "PHASE7_BITCOIN_RPC_AUTH_MODE": "api_key_header",
        "PHASE7_BITCOIN_RPC_API_KEY": secret,
        "PHASE7_BITCOIN_RPC_REQUESTS_PER_SECOND": "100",
    }).phase7_bitcoin_rpc
    assert isinstance(config.api_key, SecretStr)
    session = _Session(_Response({"result": {"chain": "main"}, "error": None, "id": 1}))
    client = Phase7JsonRpcClient(config, session, sleep=lambda _delay: asyncio.sleep(0))

    assert asyncio.run(client.call("getblockchaininfo")) == {"chain": "main"}
    request_kwargs = session.calls[0][1]
    assert request_kwargs["headers"]["api-key"] == secret


@pytest.mark.parametrize("auth_mode", ["none", "url", "basic"])
def test_existing_bitcoin_auth_modes_keep_their_transport_contract(auth_mode):
    endpoint = (
        "https://reader:password@rpc.example.invalid/path"
        if auth_mode == "url" else "https://rpc.example.invalid/path"
    )
    env = {
        "PHASE7_BITCOIN_RPC_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_URL": endpoint,
        "PHASE7_BITCOIN_RPC_AUTH_MODE": auth_mode,
    }
    if auth_mode == "basic":
        env.update({
            "PHASE7_BITCOIN_RPC_USERNAME": "reader",
            "PHASE7_BITCOIN_RPC_PASSWORD": "test-password",
        })
    config = Settings.from_env(env).phase7_bitcoin_rpc
    session = _Session(_Response({"result": {"chain": "main"}, "error": None, "id": 1}))
    client = Phase7JsonRpcClient(config, session, sleep=lambda _delay: asyncio.sleep(0))

    assert asyncio.run(client.call("getblockchaininfo")) == {"chain": "main"}
    request_kwargs = session.calls[0][1]
    assert "headers" not in request_kwargs
    if auth_mode == "basic":
        assert request_kwargs["auth"].login == "reader"
        assert request_kwargs["auth"].password == "test-password"
    else:
        assert request_kwargs["auth"] is None


def test_api_key_is_absent_from_transport_repr_http_exception_and_retry_log():
    secret = "test-secret-value"
    config = Settings.from_env({
        "PHASE7_BITCOIN_RPC_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_URL": "https://rpc.example.invalid/opaque",
        "PHASE7_BITCOIN_RPC_AUTH_MODE": "api_key_header",
        "PHASE7_BITCOIN_RPC_API_KEY": secret,
        "PHASE7_BITCOIN_RPC_REQUESTS_PER_SECOND": "100",
    }).phase7_bitcoin_rpc

    class FailingSession:
        def post(self, *_args, **_kwargs):
            raise RuntimeError(f"connection failed api-key={secret}")

    client = Phase7JsonRpcClient(config, FailingSession(), sleep=lambda _delay: asyncio.sleep(0))
    assert secret not in repr(config)
    assert secret not in repr(client)
    with pytest.raises(Phase7RpcError) as raised:
        asyncio.run(client.call("getblockchaininfo"))
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)

    retry_record = logging.LogRecord(
        "quant_phase1.runtime", logging.WARNING, __file__, 1,
        "retry api-key=%s", (secret,), None,
    )
    SecretRedactingFilter().filter(retry_record)
    assert secret not in retry_record.getMessage()


def test_bounded_response_reader_consumes_full_stream_and_enforces_decompressed_cap():
    from quant_phase7.runtime import _read_bounded_body

    class ChunkedContent:
        def __init__(self, chunks):
            self.chunks = chunks

        async def iter_chunked(self, _chunk_size):
            for chunk in self.chunks:
                yield chunk

    response = SimpleNamespace(content=ChunkedContent((b'{"result":', b'"complete"}')))
    body = asyncio.run(_read_bounded_body(response, 32, "test_rpc"))

    assert body == b'{"result":"complete"}'

    oversized = SimpleNamespace(content=ChunkedContent((b"123456", b"789")))
    with pytest.raises(Phase7RpcError, match="response exceeds byte cap"):
        asyncio.run(_read_bounded_body(oversized, 8, "test_rpc"))


def test_bitcoin_getblockcount_is_an_approved_read_only_rpc_method():
    session = _Session(_Response({"result": 900_000, "error": None, "id": 1}))
    client = Phase7JsonRpcClient(
        Phase7RpcSourceSettings("bitcoin_rpc", True, "https://rpc.example.invalid/opaque", requests_per_second=100),
        session,
        sleep=lambda _delay: asyncio.sleep(0),
    )

    assert asyncio.run(client.call("getblockcount")) == 900_000
    assert session.calls[0][1]["json"]["method"] == "getblockcount"
    assert session.calls[0][1]["json"]["jsonrpc"] == "1.0"


def test_bitcoin_core_json_rpc_1_0_response_contract_is_supported():
    session = _Session(_Response({"result": {"chain": "main"}, "error": None, "id": 1}))
    client = Phase7JsonRpcClient(
        Phase7RpcSourceSettings("bitcoin_rpc", True, "https://rpc.example.invalid/opaque", requests_per_second=100),
        session,
        sleep=lambda _delay: asyncio.sleep(0),
    )

    assert asyncio.run(client.call("getblockchaininfo")) == {"chain": "main"}
    assert session.calls[0][1]["json"]["jsonrpc"] == "1.0"


def test_bitcoin_rpc_transport_preserves_decimal_values_for_official_parser():
    block_hash = "a" * 64
    txid = "c" * 64
    block = {
        "hash": block_hash,
        "previousblockhash": "b" * 64,
        "height": 900_000,
        "time": 1_700_000_000,
        "confirmations": 10,
        "tx": [{
            "txid": txid,
            "vin": [{"coinbase": "coinbase-bytes"}],
            "vout": [{
                "n": 0,
                "value": 1.00000001,
                "scriptPubKey": {"address": "bc1qexample"},
            }],
        }],
    }
    config = Settings.from_env({
        "PHASE7_BITCOIN_RPC_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_URL": "https://rpc.example.invalid/opaque",
        "PHASE7_BITCOIN_RPC_REQUESTS_PER_SECOND": "100",
    }).phase7_bitcoin_rpc
    assert config.max_response_bytes == 32 * 1024 * 1024
    session = _Session(_Response({"result": block, "error": None, "id": 1}))
    client = Phase7JsonRpcClient(config, session, sleep=lambda _delay: asyncio.sleep(0))

    result = asyncio.run(client.call("getblock", [block_hash, 2]))
    output_value = result["tx"][0]["vout"][0]["value"]
    assert isinstance(output_value, Decimal)

    at = datetime.now(timezone.utc)
    events = BitcoinBlockParser().parse_block(
        result,
        observed_at=at,
        fetched_at=at,
        processed_at=at,
        expected_block_hash=block_hash,
    )
    assert len(events) == 1
    assert events[0].amount.amount_raw == "100000001"
    assert events[0].identity.tx_hash == txid
    assert events[0].provenance.source_hash == block_hash


@pytest.mark.parametrize("invalid", [0, -1, 64 * 1024 * 1024 + 1, True])
def test_rpc_client_rejects_invalid_response_budget_override(invalid):
    config = Settings.from_env({
        "PHASE7_BITCOIN_RPC_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_URL": "https://rpc.example.invalid/opaque",
    }).phase7_bitcoin_rpc
    with pytest.raises(ValueError, match="max_response_bytes"):
        Phase7JsonRpcClient(config, _Session(_Response({"result": None, "error": None, "id": 1})),
                            max_response_bytes=invalid)


def test_ethereum_429_honors_retry_after_and_exhausts_bounded_attempts():
    config = Settings.from_env({
        "PHASE7_ETHEREUM_RPC_ENABLED": "1",
        "PHASE7_ETHEREUM_RPC_URL": "https://rpc.example.invalid/opaque",
        "PHASE7_ETHEREUM_RPC_REQUESTS_PER_SECOND": "100",
    }).phase7_ethereum_rpc
    session = _Session(_Response({}, status=429, headers={"Retry-After": "1.25"}))
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    client = Phase7JsonRpcClient(config, session, sleep=record_sleep)

    async def no_rate_wait() -> None:
        return None

    client._wait_for_rate = no_rate_wait
    with pytest.raises(Phase7RateLimitedError, match="RATE_LIMITED"):
        asyncio.run(client.call("eth_chainId"))

    assert len(session.calls) == 3
    assert delays == [1.25, 1.25]


def test_ethereum_429_with_retry_after_over_budget_fails_closed_without_retry():
    config = Settings.from_env({
        "PHASE7_ETHEREUM_RPC_ENABLED": "1",
        "PHASE7_ETHEREUM_RPC_URL": "https://rpc.example.invalid/opaque",
    }).phase7_ethereum_rpc
    session = _Session(_Response({}, status=429, headers={"Retry-After": "30"}))
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    client = Phase7JsonRpcClient(config, session, sleep=record_sleep)

    async def no_rate_wait() -> None:
        return None

    client._wait_for_rate = no_rate_wait
    with pytest.raises(Phase7RateLimitedError, match="wait budget exhausted"):
        asyncio.run(client.call("eth_blockNumber"))

    assert len(session.calls) == 1
    assert delays == []


def test_ethereum_429_without_retry_after_uses_bounded_exponential_backoff():
    config = Settings.from_env({
        "PHASE7_ETHEREUM_RPC_ENABLED": "1",
        "PHASE7_ETHEREUM_RPC_URL": "https://rpc.example.invalid/opaque",
    }).phase7_ethereum_rpc
    session = _Session(_Response({}, status=429))
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    client = Phase7JsonRpcClient(config, session, sleep=record_sleep)

    async def no_rate_wait() -> None:
        return None

    client._wait_for_rate = no_rate_wait
    with pytest.raises(Phase7RateLimitedError):
        asyncio.run(client.call("eth_blockNumber"))

    assert len(session.calls) == 3
    assert delays == [0.5, 1.5]


def test_ethereum_receipt_batch_429_uses_the_same_bounded_retry_policy():
    config = Settings.from_env({
        "PHASE7_ETHEREUM_RPC_ENABLED": "1",
        "PHASE7_ETHEREUM_RPC_URL": "https://rpc.example.invalid/opaque",
        "PHASE7_ETHEREUM_RPC_REQUESTS_PER_SECOND": "100",
    }).phase7_ethereum_rpc
    session = _Session(_Response({}, status=429))
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    client = Phase7JsonRpcClient(config, session, sleep=record_sleep)

    async def no_rate_wait() -> None:
        return None

    client._wait_for_rate = no_rate_wait
    with pytest.raises(Phase7RateLimitedError):
        asyncio.run(client.call_batch([("eth_getTransactionReceipt", ["0x" + "a" * 64])]))

    assert len(session.calls) == 3
    assert delays == [0.5, 1.5]


def test_ethereum_block_chunks_receipt_candidates_without_changing_block_semantics():
    runtime = Phase7CollectorRuntime(_settings())
    transactions = [
        {"hash": "0x" + f"{index:064x}", "value": "0x1"}
        for index in range(298)
    ]
    block = {"hash": "0x" + "a" * 64, "transactions": transactions}

    class Client:
        response_bytes_total = 0

        def __init__(self):
            self.batch_sizes: list[int] = []

        async def call(self, method, _params):
            if method == "eth_getBlockByNumber":
                return block
            assert method == "eth_getLogs"
            return []

        async def call_batch(self, calls):
            self.batch_sizes.append(len(calls))
            return [{"status": "0x1"} for _ in calls]

    class Parser:
        def parse_block(self, filtered_block, **kwargs):
            assert len(filtered_block["transactions"]) == 298
            assert len(kwargs["receipts"]) == 298
            return []

    client = Client()
    result_block, events = asyncio.run(
        runtime._ethereum_block(
            client,
            Parser(),
            123,
            "0x1",
            datetime(2026, 9, 25, tzinfo=timezone.utc),
        )
    )

    assert result_block is block
    assert events == []
    assert client.batch_sizes == [250, 48]


def test_ethereum_block_fails_closed_above_bounded_receipt_candidate_limit():
    runtime = Phase7CollectorRuntime(_settings())
    transactions = [
        {"hash": "0x" + f"{index:064x}", "value": "0x1"}
        for index in range(2_001)
    ]
    block = {"hash": "0x" + "b" * 64, "transactions": transactions}

    class Client:
        response_bytes_total = 0

        async def call(self, method, _params):
            if method == "eth_getBlockByNumber":
                return block
            assert method == "eth_getLogs"
            return []

        async def call_batch(self, _calls):
            pytest.fail("receipt requests must not start above the per-block cap")

    with pytest.raises(Phase7RpcError, match="receipt candidate count exceeds per-block cap"):
        asyncio.run(
            runtime._ethereum_block(
                Client(),
                SimpleNamespace(parse_block=lambda *_args, **_kwargs: []),
                124,
                "0x1",
                datetime(2026, 9, 25, tzinfo=timezone.utc),
            )
        )


def test_ethereum_cycle_marks_health_available_after_each_persisted_block(monkeypatch):
    settings = Settings.from_env({
        "TRADING_MODE": "paper",
        "PHASE7_ENABLED": "1",
        "PHASE7_ETHEREUM_RPC_ENABLED": "1",
        "PHASE7_ETHEREUM_RPC_URL": "https://rpc.example.invalid/opaque",
    })
    runtime = Phase7CollectorRuntime(settings)
    writes: list[tuple[str, str, dict[str, object]]] = []
    persisted = []

    class RpcClient:
        async def call(self, method, _params=None):
            if method == "eth_chainId":
                return "0x1"
            if method == "eth_blockNumber":
                return "0x0"
            assert method == "eth_getBlockByNumber"
            return {"number": "0x0"}

    class Repository:
        def upsert_asset_registry(self, _rows):
            return None

        def load_checkpoint(self, *_args):
            return None

        def persist_transfer_batch_and_checkpoint(self, events, _state):
            persisted.append(tuple(events))

    @contextmanager
    def repository_scope():
        yield Repository()

    @asynccontextmanager
    async def fake_session():
        yield object()

    async def fake_block(_client, _parser, height, _chain_id, _observed_at, expected_hash=None):
        assert height == 0
        assert expected_hash is None
        return {"hash": "0x" + "a" * 64}, []

    async def capture_health(component, status, _checked_at, details):
        assert len(persisted) == 1
        writes.append((component, status.value, details))

    monkeypatch.setattr("quant_phase7.runtime.Phase7JsonRpcClient", lambda *_args: RpcClient())
    monkeypatch.setattr(runtime, "_rpc_session", fake_session)
    monkeypatch.setattr(runtime, "_repository_scope", repository_scope)
    monkeypatch.setattr(runtime, "_ethereum_block", fake_block)
    monkeypatch.setattr(runtime, "_checkpoint", lambda *_args: SimpleNamespace(to_row=lambda: {}))
    monkeypatch.setattr(runtime, "_apply_event_time_prices", lambda _repo, events, _symbol: events)
    monkeypatch.setattr(runtime, "_write_health", capture_health)

    result = asyncio.run(runtime._ethereum_cycle())

    assert result[0] is DataStatus.AVAILABLE
    assert len(persisted) == 1
    assert writes == [
        (
            "ethereum_rpc",
            "AVAILABLE",
            {
                "chain": "ETHEREUM_MAINNET",
                "runtime_state": "RUNNING",
                "phase7_status": "AVAILABLE",
                "cursor": 0,
                "head_cursor": 0,
                "last_finalized_cursor": 0,
                "persisted_events": 0,
                "progress_source": "BLOCK_CHECKPOINT_COMMITTED",
            },
        )
    ]


def test_retry_after_accepts_http_date_without_exposing_provider_text():
    from email.utils import format_datetime

    from quant_phase7.runtime import _retry_after_seconds

    value = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    delay = _retry_after_seconds(value)

    assert delay is not None
    assert 0 <= delay <= 30


def test_ethereum_rate_limit_degrades_source_health_as_partial_not_error():
    settings = Settings.from_env({
        "TRADING_MODE": "paper",
        "PHASE7_ENABLED": "1",
        "PHASE7_ETHEREUM_RPC_ENABLED": "1",
        "PHASE7_ETHEREUM_RPC_URL": "https://rpc.example.invalid/opaque",
    })
    stop = asyncio.Event()
    health: list[tuple[str, str, dict[str, object]]] = []

    async def limited_worker():
        raise Phase7RateLimitedError("ethereum_rpc: RATE_LIMITED")

    def write_health(component, status, _checked, details):
        health.append((component, status.value, details))
        if component == "ethereum_rpc" and details.get("reason") == "RATE_LIMITED":
            stop.set()

    runtime = Phase7CollectorRuntime(
        settings,
        cycle_handlers={"ethereum_rpc": limited_worker},
        health_writer=write_health,
        spot_websocket_enabled=False,
    )
    asyncio.run(runtime._supervise("ethereum_rpc", stop))

    limited = [item for item in health if item[0] == "ethereum_rpc" and item[2].get("reason") == "RATE_LIMITED"]
    assert len(limited) == 1
    assert limited[0][1] == "NOT_AVAILABLE"
    assert limited[0][2]["phase7_status"] == "PARTIAL"
    assert limited[0][2]["reason"] == "RATE_LIMITED"
    assert limited[0][2]["failure_stage"] == "HTTP_429"
    assert not any(item[0] == "ethereum_rpc" and item[1] == "ERROR" for item in health)


def test_phase7_single_transient_failure_degrades_then_success_recovers(monkeypatch):
    from quant_phase7.contracts import DataStatus

    settings = _settings()
    stop = asyncio.Event()
    health = []
    attempts = 0

    async def source_cycle():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError()
        stop.set()
        return DataStatus.AVAILABLE, {"phase7_status": "AVAILABLE", "cursor": 42}

    def write_health(component, status, _checked, details):
        health.append((component, status.value, details))

    runtime = Phase7CollectorRuntime(
        settings,
        cycle_handlers={"bitcoin_rpc": source_cycle},
        health_writer=write_health,
        cycle_interval_seconds=0.01,
        spot_websocket_enabled=False,
    )
    asyncio.run(runtime._supervise("bitcoin_rpc", stop))

    btc = [item for item in health if item[0] == "bitcoin_rpc"]
    assert btc[0][1] == "NOT_AVAILABLE"
    assert btc[0][2]["phase7_status"] == "PARTIAL"
    assert btc[0][2]["data_quality"] == "PARTIAL"
    assert btc[0][2]["consecutive_failures"] == 1
    assert btc[1][1] == "AVAILABLE"
    assert btc[1][2]["phase7_status"] == "AVAILABLE"
    assert attempts == 2


def test_phase7_three_consecutive_transient_failures_become_error():
    stop = asyncio.Event()
    health = []
    attempts = 0

    async def source_cycle():
        nonlocal attempts
        attempts += 1
        if attempts <= 3:
            raise TimeoutError()
        stop.set()
        from quant_phase7.contracts import DataStatus
        return DataStatus.AVAILABLE, {"phase7_status": "AVAILABLE", "cursor": 43}

    def write_health(component, status, _checked, details):
        if component == "bitcoin_rpc":
            health.append((status.value, details))

    runtime = Phase7CollectorRuntime(
        _settings(),
        cycle_handlers={"bitcoin_rpc": source_cycle},
        health_writer=write_health,
        cycle_interval_seconds=0.01,
        spot_websocket_enabled=False,
    )
    asyncio.run(runtime._supervise("bitcoin_rpc", stop))

    assert [status for status, _details in health] == [
        "NOT_AVAILABLE", "NOT_AVAILABLE", "ERROR", "AVAILABLE",
    ]
    assert [details["phase7_status"] for _status, details in health] == [
        "PARTIAL", "PARTIAL", "ERROR", "AVAILABLE",
    ]
    assert health[2][1]["consecutive_failures"] == 3


def test_ethereum_rpc_transport_keeps_existing_float_json_decoding():
    session = _Session(_Response({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"fixture_decimal": 1.25},
    }))
    client = Phase7JsonRpcClient(
        Phase7RpcSourceSettings(
            "ethereum_rpc", True, "https://rpc.example.invalid/opaque", requests_per_second=100,
        ),
        session,
        sleep=lambda _delay: asyncio.sleep(0),
    )

    result = asyncio.run(client.call("eth_getBlockByNumber", ["latest", False]))

    assert type(result["fixture_decimal"]) is float


def _bitcoin_probe_block():
    return {
        "hash": "ab" * 32,
        "previousblockhash": "bc" * 32,
        "height": 900_000,
        "time": 1_700_000_000,
        "confirmations": 10,
        "tx": [{
            "txid": "cd" * 32,
            "vin": [{
                "txid": "de" * 32,
                "vout": 0,
                "prevout": {
                    "value": "0.30000000",
                    "scriptPubKey": {"address": "bc1qsource"},
                },
            }],
            "vout": [{
                "n": 0,
                "value": "0.25000001",
                "scriptPubKey": {"address": "bc1qdestination"},
            }],
        }],
    }


def test_bitcoin_source_probe_parser_pass_requires_all_contract_checks():
    from scripts.phase7_source_contract_probe import _bitcoin_parser_report

    report = _bitcoin_parser_report(_bitcoin_probe_block(), "ab" * 32)

    assert report["BITCOIN_PARSER_PASS"] is True
    assert report["status"] == "PASS"
    assert report["hash_valid"] is True
    assert report["height_valid"] is True
    assert report["transaction_identity_valid"] is True
    assert report["satoshi_valid"] is True
    assert report["provenance_valid"] is True


def test_bitcoin_source_probe_accepts_a_block_larger_than_one_persistence_chunk():
    from scripts.phase7_source_contract_probe import _bitcoin_parser_report
    from tests.test_phase7_bitcoin import _block_with_output_event_count

    block = _block_with_output_event_count(12_665)
    report = _bitcoin_parser_report(block, block["hash"])

    assert report["BITCOIN_PARSER_PASS"] is True
    assert report["status"] == "PASS"


def test_event_time_pricing_is_lazy_and_reuses_one_quote_per_block_timestamp():
    event = BitcoinBlockParser().parse_block(
        _bitcoin_probe_block(),
        observed_at=datetime.now(timezone.utc),
        fetched_at=datetime.now(timezone.utc),
        processed_at=datetime.now(timezone.utc),
    )[0]

    class QuoteRepository:
        def __init__(self):
            self.calls = []

        def load_event_time_price(self, symbol, event_time, *, max_skew_seconds):
            self.calls.append((symbol, event_time, max_skew_seconds))
            return None

    repository = QuoteRepository()
    priced = Phase7CollectorRuntime._iter_event_time_prices(repository, (event, event), "BTCUSDT")

    assert iter(priced) is priced
    assert repository.calls == [("BTCUSDT", event.event_time, 300)]
    assert list(priced) == [event, event]


def test_bitcoin_cycle_sends_large_block_to_bounded_chunk_persistence(monkeypatch):
    from contextlib import contextmanager
    from itertools import islice

    import quant_phase7.runtime as runtime_module
    from tests.test_phase7_bitcoin import _block_with_output_event_count

    previous_hash = "cd" * 32
    current_hash = "ab" * 32
    block = _block_with_output_event_count(12_665)
    block["height"] = 101
    block["hash"] = current_hash

    settings = Settings.from_env({
        "TRADING_MODE": "paper",
        "PHASE7_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_URL": "https://rpc.example.invalid/opaque",
        "PHASE7_BITCOIN_RPC_REQUESTS_PER_SECOND": "100",
    })

    class FakeClient:
        def __init__(self, _config, _session):
            self.last_response_bytes = 0

        async def call(self, method, params=None):
            if method == "getblockchaininfo":
                return {"chain": "main", "blocks": 107}
            if method == "getblockhash":
                return {100: previous_hash, 101: current_hash}[params[0]]
            if method == "getblock":
                assert params == [current_hash, 2]
                return block
            raise AssertionError("unexpected Bitcoin RPC method")

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class FakeRepository:
        chunk_sizes = []
        checkpoint = {
            "cursor_value": "100",
            "last_block_hash": previous_hash,
        }

        def upsert_asset_registry(self, _rows):
            pass

        def load_checkpoint(self, *_args):
            return self.checkpoint

        def load_event_time_price(self, *_args, **_kwargs):
            return None

        def persist_transfer_chunks_and_checkpoint(self, events, checkpoint):
            iterator = iter(events)
            while chunk := tuple(islice(iterator, 10_000)):
                self.chunk_sizes.append(len(chunk))
            self.checkpoint = checkpoint
            return (sum(self.chunk_sizes), 1)

    repository = FakeRepository()
    runtime = Phase7CollectorRuntime(settings, spot_websocket_enabled=False)

    @contextmanager
    def repository_scope():
        yield repository

    monkeypatch.setattr(runtime_module, "Phase7JsonRpcClient", FakeClient)
    monkeypatch.setattr(runtime, "_rpc_session", lambda: FakeSession())
    monkeypatch.setattr(runtime, "_repository_scope", repository_scope)

    status, summary = asyncio.run(runtime._bitcoin_cycle())

    assert status is DataStatus.AVAILABLE
    assert repository.chunk_sizes == [10_000, 2_665]
    assert repository.checkpoint["cursor_value"] == "101"
    assert summary["processed_blocks"] == 1
    assert summary["persisted_events"] == 12_665


def test_bitcoin_cycle_releases_previous_block_events_before_fetching_next(monkeypatch):
    from contextlib import contextmanager

    import quant_phase7.runtime as runtime_module

    previous_hash = "cd" * 32
    block_hashes = {100: previous_hash, 101: "ab" * 32, 102: "ef" * 32}
    settings = Settings.from_env({
        "TRADING_MODE": "paper",
        "PHASE7_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_URL": "https://rpc.example.invalid/opaque",
        "PHASE7_BITCOIN_RPC_REQUESTS_PER_SECOND": "100",
    })
    previous_event = None

    class FakeClient:
        def __init__(self, _config, _session):
            self.block_fetches = 0
            self.last_response_bytes = 0

        async def call(self, method, params=None):
            nonlocal previous_event
            if method == "getblockchaininfo":
                return {"chain": "main", "blocks": 108}
            if method == "getblockhash":
                return block_hashes[params[0]]
            if method == "getblock":
                self.block_fetches += 1
                if self.block_fetches == 2:
                    assert previous_event is not None
                    assert previous_event() is None, (
                        "the prior canonical event remains retained while the next block is fetched"
                    )
                return {"hash": params[0], "height": 100 + self.block_fetches}
            raise AssertionError("unexpected Bitcoin RPC method")

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class FakeRepository:
        checkpoint = {"cursor_value": "100", "last_block_hash": previous_hash}

        def upsert_asset_registry(self, _rows):
            pass

        def load_checkpoint(self, *_args):
            return self.checkpoint

        def persist_transfer_chunks_and_checkpoint(self, events, checkpoint):
            nonlocal previous_event
            persisted = list(events)
            previous_event = weakref.ref(persisted[0])
            self.checkpoint = checkpoint
            return (len(persisted), 1)

    repository = FakeRepository()
    runtime = Phase7CollectorRuntime(settings, spot_websocket_enabled=False)

    @contextmanager
    def repository_scope():
        yield repository

    class ParsedEvent:
        __slots__ = ("status", "__weakref__")

        def __init__(self):
            self.status = DataStatus.PARTIAL

    def parse_one_event(self, *_args, **_kwargs):
        return (ParsedEvent(),)

    monkeypatch.setattr(runtime_module, "Phase7JsonRpcClient", FakeClient)
    monkeypatch.setattr(runtime_module.BitcoinBlockParser, "parse_block", parse_one_event)
    monkeypatch.setattr(runtime, "_rpc_session", lambda: FakeSession())
    monkeypatch.setattr(runtime, "_repository_scope", repository_scope)

    status, summary = asyncio.run(runtime._bitcoin_cycle())

    assert status is DataStatus.AVAILABLE
    assert summary["processed_blocks"] == 2
    assert summary["persisted_events"] == 2


def test_collector_repository_scope_checks_schema_then_leaves_atomic_units_top_level(monkeypatch):
    import quant_phase1.db as db_module

    class FakeConnection:
        def __init__(self):
            self.autocommit = False
            self.commits = 0
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def commit(self):
            self.commits += 1

        def close(self):
            self.closed = True

    connection = FakeConnection()
    checks = []
    monkeypatch.setattr(db_module, "assert_schema_ready", lambda conn, *, required_version: checks.append((conn, required_version)))
    runtime = Phase7CollectorRuntime(
        _settings(), connection_factory=lambda _dsn: connection, spot_websocket_enabled=False,
    )

    with runtime._repository_scope() as repository:
        assert repository.connection is connection
        assert connection.autocommit is True
        assert connection.commits == 1

    assert checks == [(connection, "014_phase7_exact_amount_constraint.sql")]
    assert connection.closed is True


def test_source_probe_records_only_numeric_content_length_metadata():
    from scripts.phase7_source_contract_probe import _safe_content_length

    assert _safe_content_length({"Content-Length": "123456"}) == 123456
    assert _safe_content_length({"Content-Length": "chunked"}) is None
    assert _safe_content_length({"Content-Length": "9" * 21}) is None


@pytest.mark.parametrize(
    ("broken_check", "expected_field"),
    [
        ("identity", "transaction_identity_valid"),
        ("satoshi", "satoshi_valid"),
        ("provenance", "provenance_valid"),
    ],
)
def test_bitcoin_source_probe_parser_pass_fails_on_contract_mismatch(
    monkeypatch, broken_check, expected_field,
):
    from dataclasses import replace

    from quant_phase7.bitcoin import BitcoinBlockParser
    from scripts.phase7_source_contract_probe import _bitcoin_parser_report

    real_parse = BitcoinBlockParser.parse_block

    def parse_with_contract_mismatch(self, *args, **kwargs):
        events = real_parse(self, *args, **kwargs)
        event = events[0]
        if broken_check == "identity":
            identity = replace(event.identity, tx_hash="ef" * 32)
            event = replace(event, identity=identity)
        elif broken_check == "satoshi":
            amount = replace(event.amount, amount_raw="25000000")
            event = replace(event, amount=amount)
        else:
            provenance = replace(event.provenance, source_hash="ef" * 32)
            event = replace(event, provenance=provenance)
        return (event, *events[1:])

    monkeypatch.setattr(BitcoinBlockParser, "parse_block", parse_with_contract_mismatch)
    report = _bitcoin_parser_report(_bitcoin_probe_block(), "ab" * 32)

    assert report["BITCOIN_PARSER_PASS"] is False
    assert report["status"] == "FAIL"
    assert report[expected_field] is False


def test_json_rpc_errors_do_not_echo_endpoint_or_provider_message():
    endpoint = "https://rpc.example.invalid/path-with-secret"
    session = _Session(_Response({
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32000, "message": endpoint},
    }))
    client = Phase7JsonRpcClient(
        Phase7RpcSourceSettings("ethereum_rpc", True, endpoint, requests_per_second=100),
        session,
        sleep=lambda _delay: asyncio.sleep(0),
    )

    with pytest.raises(Phase7RpcError) as raised:
        asyncio.run(client.call("eth_chainId"))

    assert endpoint not in str(raised.value)
    assert "path-with-secret" not in str(raised.value)
    assert "-32000" in str(raised.value)


def test_json_rpc_batch_is_bounded_read_only_and_correlates_ids():
    session = _Session(_Response([
        {"jsonrpc": "2.0", "id": 2, "result": "0x2"},
        {"jsonrpc": "2.0", "id": 1, "result": "0x1"},
    ]))
    client = Phase7JsonRpcClient(
        Phase7RpcSourceSettings("ethereum_rpc", True, "https://rpc.example.invalid/token", requests_per_second=100),
        session,
        sleep=lambda _delay: asyncio.sleep(0),
    )

    result = asyncio.run(client.call_batch([("eth_chainId", []), ("eth_blockNumber", [])]))

    assert result == ["0x1", "0x2"]
    assert [call["method"] for call in session.calls[0][1]["json"]] == ["eth_chainId", "eth_blockNumber"]
    with pytest.raises(Phase7RpcError, match="method is not approved"):
        asyncio.run(client.call_batch([("eth_sendTransaction", [])]))


def test_phase7_enabled_is_independent_of_rpc_secret_configuration():
    settings = Settings.from_env({"TRADING_MODE": "paper", "PHASE7_ENABLED": "1"})

    assert settings.phase7_enabled is True
    assert settings.phase7_bitcoin_rpc.enabled is False
    assert settings.phase7_ethereum_rpc.enabled is False


def test_collector_runtime_owns_and_cancels_enabled_source_workers():
    settings = Settings.from_env({
        "TRADING_MODE": "paper",
        "PHASE7_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_ENABLED": "1",
        "PHASE7_BITCOIN_RPC_URL": "https://rpc.example.invalid/secret-path",
    })
    calls: list[str] = []
    health: list[tuple[str, str, dict[str, object]]] = []

    async def bitcoin_worker():
        calls.append("started")
        return DataStatus.AVAILABLE, {"head_cursor": 100}

    async def spot_worker():
        return DataStatus.NOT_AVAILABLE, {"reason": "TEST_DISABLED"}

    runtime = Phase7CollectorRuntime(
        settings,
        cycle_handlers={"bitcoin_rpc": bitcoin_worker, "binance_spot": spot_worker},
        spot_websocket_enabled=False,
        health_writer=lambda component, status, _checked, details: health.append(
            (component, status.value, details)
        ),
    )
    stop = asyncio.Event()

    async def exercise() -> None:
        task = asyncio.create_task(runtime.run(stop))
        for _ in range(20):
            if calls:
                break
            await asyncio.sleep(0)
        assert calls == ["started"]
        assert runtime.task_count == 2
        stop.set()
        await task

    asyncio.run(exercise())

    assert runtime.task_count == 0
    assert any(name == "bitcoin_rpc" and state == "AVAILABLE" for name, state, _ in health)
    assert any(state == "NOT_AVAILABLE" and details.get("runtime_state") == "STOPPED" for _, state, details in health)
    assert all("secret-path" not in str(details) for _, _, details in health)


def test_engine_runtime_has_heartbeat_and_graceful_shutdown():
    health: list[tuple[str, str, dict[str, object]]] = []
    runtime = Phase7EngineRuntime(
        _settings(),
        health_writer=lambda component, status, _checked, details: health.append(
            (component, status.value, details)
        ),
        heartbeat_seconds=0.01,
    )
    stop = asyncio.Event()

    async def exercise() -> None:
        task = asyncio.create_task(runtime.run(stop))
        await asyncio.sleep(0.03)
        stop.set()
        await task

    asyncio.run(exercise())

    assert sum(component == "phase7-context-engine" and details.get("runtime_state") == "ACTIVE"
               for component, _, details in health) >= 2
    assert health[-1][2]["runtime_state"] == "STOPPED"


def test_engine_runtime_owns_context_aggregation_cycle_and_shutdown(monkeypatch):
    from quant_phase7 import runtime as runtime_module

    settings = Settings.from_env({
        "TRADING_MODE": "paper", "PHASE7_ENABLED": "1",
        "PHASE7_CONTEXT_INTERVAL_SECONDS": "1",
    })
    health = []
    checks = []

    class Connection:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *args):
            self.calls.append(args)

    connection = Connection()

    class Repository:
        def __init__(self, _connection):
            pass

        def load_context_window_keys(self, *_args):
            return ()

    monkeypatch.setattr(runtime_module, "Phase7Repository", Repository)
    monkeypatch.setattr("quant_phase1.db.assert_schema_ready", lambda _connection, *, required_version: checks.append(required_version))

    runtime = Phase7EngineRuntime(
        settings, connection_factory=lambda _dsn: connection,
        health_writer=lambda component, status, _checked, details: health.append(
            (component, status.value, details)
        ),
        heartbeat_seconds=0.01,
    )
    stop = asyncio.Event()

    async def exercise():
        task = asyncio.create_task(runtime.run(stop))
        await asyncio.sleep(0.04)
        stop.set()
        await task

    asyncio.run(exercise())

    assert "014_phase7_exact_amount_constraint.sql" in checks
    assert connection.calls[0][0].startswith("SET LOCAL statement_timeout")
    assert any(component == "phase7-context-engine" for component, _, _ in health)
    assert health[-1][2]["runtime_state"] == "STOPPED"


def test_engine_context_cycle_persists_reviewed_labels_before_aggregation(monkeypatch, tmp_path):
    from quant_phase7 import runtime as runtime_module

    label_path = tmp_path / "labels.json"
    from tests.test_phase7_context_config import _write_snapshot
    _write_snapshot(label_path)
    settings = Settings.from_env({
        "TRADING_MODE": "paper", "PHASE7_ENABLED": "1",
        "PHASE7_ADDRESS_LABELS_PATH": str(label_path),
    })

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            pass

    class Repository:
        persisted_snapshots = []

        def __init__(self, _connection):
            pass

        def upsert_address_labels(self, snapshot):
            self.persisted_snapshots.append(snapshot)
            return len(snapshot.labels)

        def load_context_window_keys(self, *_args):
            return ()

    monkeypatch.setattr(runtime_module, "Phase7Repository", Repository)
    monkeypatch.setattr("quant_phase1.db.assert_schema_ready", lambda _connection, *, required_version: None)
    runtime = Phase7EngineRuntime(settings, connection_factory=lambda _dsn: Connection())

    result = runtime._run_context_cycle()

    assert result["label_snapshots"] == 1
    assert Repository.persisted_snapshots[0].chain.value == "BITCOIN"
    assert len(Repository.persisted_snapshots[0].labels) == 2


def test_engine_context_cycle_replays_closed_event_time_windows_by_watermark(monkeypatch, tmp_path):
    from quant_phase7 import runtime as runtime_module
    from quant_phase7.context_service import closed_window_open

    fixed_now = datetime(2026, 9, 25, 10, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(runtime_module, "utc_now", lambda: fixed_now)
    label_path = tmp_path / "labels.json"
    from tests.test_phase7_context_config import _write_snapshot
    _write_snapshot(label_path)
    settings = Settings.from_env({
        "TRADING_MODE": "paper", "PHASE7_ENABLED": "1",
        "PHASE7_ADDRESS_LABELS_PATH": str(label_path),
        "PHASE7_CONTEXT_WINDOW_BATCH": "2",
    })
    old_processed_at = datetime(2026, 9, 25, 10, 10, tzinfo=timezone.utc)
    new_processed_at = datetime(2026, 9, 25, 10, 20, tzinfo=timezone.utc)
    window_open = closed_window_open(fixed_now, "1H")
    repository_state = {
        "watermark": old_processed_at,
        "persisted_labels": [],
        "aggregate_calls": [],
    }

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            pass

    class Repository:
        def __init__(self, _connection):
            pass

        def upsert_address_labels(self, snapshot):
            repository_state["persisted_labels"].append(snapshot.snapshot_hash)

        def load_context_window_keys(self, *_args):
            return ({
                "chain": "BITCOIN", "asset_id": "BITCOIN:NATIVE:NATIVE:btc-v1",
                "timeframe": "1H", "window_open": window_open,
                "latest_processed_at": repository_state["watermark"],
            },)

    def fake_aggregate(_repository, **kwargs):
        repository_state["aggregate_calls"].append(kwargs)
        return {
            "events": 2, "assets": 1, "flow_rows": 1,
            "whale_rows": 0, "stablecoin_rows": 0,
        }

    monkeypatch.setattr(runtime_module, "Phase7Repository", Repository)
    monkeypatch.setattr("quant_phase1.db.assert_schema_ready", lambda _connection, *, required_version: None)
    monkeypatch.setattr("quant_phase7.context_service.aggregate_closed_context_window", fake_aggregate)
    runtime = Phase7EngineRuntime(settings, connection_factory=lambda _dsn: Connection())

    first = runtime._run_context_cycle()
    second = runtime._run_context_cycle()
    repository_state["watermark"] = new_processed_at
    third = runtime._run_context_cycle()

    assert first["windows_processed"] == 1
    assert first["events"] == 2
    assert second["windows_processed"] == 0
    assert third["windows_processed"] == 1
    assert len(repository_state["aggregate_calls"]) == 2
    assert all(call["window_open"] == window_open for call in repository_state["aggregate_calls"])
    assert all(call["timeframe"] == "1H" for call in repository_state["aggregate_calls"])
    assert all(call["chain"].value == "BITCOIN" for call in repository_state["aggregate_calls"])
    assert len(repository_state["persisted_labels"]) == 1


def test_engine_context_cycle_does_not_cache_windows_before_transaction_commit(monkeypatch):
    from quant_phase7 import runtime as runtime_module
    from quant_phase7.context_service import closed_window_open

    fixed_now = datetime(2026, 9, 25, 10, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(runtime_module, "utc_now", lambda: fixed_now)
    settings = Settings.from_env({"TRADING_MODE": "paper", "PHASE7_ENABLED": "1"})

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, *_args):
            if exc_type is None:
                raise RuntimeError("commit failed")
            return False

        def execute(self, *_args):
            pass

    class Repository:
        def __init__(self, _connection):
            pass

        def load_context_window_keys(self, *_args):
            return ({
                "chain": "BITCOIN", "asset_id": "BITCOIN:NATIVE:NATIVE:btc-v1",
                "timeframe": "1H",
                "window_open": closed_window_open(fixed_now, "1H"),
                "latest_processed_at": fixed_now,
            },)

    monkeypatch.setattr(runtime_module, "Phase7Repository", Repository)
    monkeypatch.setattr("quant_phase1.db.assert_schema_ready", lambda _connection, *, required_version: None)
    monkeypatch.setattr(
        "quant_phase7.context_service.aggregate_closed_context_window",
        lambda *_args, **_kwargs: {"events": 1},
    )
    runtime = Phase7EngineRuntime(settings, connection_factory=lambda _dsn: Connection())

    with pytest.raises(RuntimeError, match="commit failed"):
        runtime._run_context_cycle()

    assert runtime._processed_context_windows == {}


def test_binance_websocket_trade_is_canonicalized_and_deduplicated():
    runtime = Phase7CollectorRuntime(_settings())
    payload = {
        "e": "aggTrade", "s": "BTCUSDT", "a": 42,
        "p": "65000.25", "q": "0.01", "f": 40, "l": 42,
        "T": 1_790_000_000_000, "m": False,
    }

    event = runtime.ingest_binance_ws_message(payload)
    duplicate = runtime.ingest_binance_ws_message(payload)

    assert event.source_id == "binance_spot"
    assert event.symbol == "BTCUSDT"
    assert event.source_channel == "aggTrade"
    assert duplicate is None
    assert len(runtime._spot_events["BTCUSDT"]) == 1


def test_formal_collector_spot_supervisor_persists_rest_trade_windows(monkeypatch):
    import aiohttp
    from contextlib import contextmanager

    from quant_phase7 import runtime as runtime_module

    now = datetime(2026, 9, 25, 12, 1, 0, tzinfo=timezone.utc)
    trade = [{
        "a": 901, "p": "65000.25", "q": "0.01", "f": 901, "l": 901,
        "T": int((now.timestamp() - 45) * 1000), "m": False, "M": True,
    }]
    body = json.dumps(trade).encode()

    class FakeResponse:
        status = 200
        headers = {"Content-Type": "application/json"}

        def __init__(self):
            self.content = _Content(body)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def get(self, _url, **_kwargs):
            return FakeResponse()

    class FakeRepository:
        def __init__(self):
            self.rows = []
            self.checkpoints = []

        def load_checkpoint(self, *_args):
            return None

        def load_latest_spot_cvd(self, *_args):
            return None

        def persist_spot_windows_and_checkpoint(self, rows, checkpoint):
            self.rows.extend(rows)
            self.checkpoints.append(checkpoint)
            return len(rows), 1

    repository = FakeRepository()

    @contextmanager
    def repository_scope():
        yield repository

    monkeypatch.setattr(aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(runtime_module, "utc_now", lambda: now)
    runtime = Phase7CollectorRuntime(_settings(), cycle_interval_seconds=3600)
    monkeypatch.setattr(runtime, "_repository_scope", repository_scope)
    available = asyncio.Event()
    health = []

    def write_health(component, status, _checked, details):
        health.append((component, status.value, details))
        if component == "binance_spot" and status.value == "AVAILABLE":
            available.set()

    runtime.health_writer = write_health
    stop = asyncio.Event()

    async def exercise():
        task = asyncio.create_task(runtime._supervise("binance_spot", stop))
        await asyncio.wait_for(available.wait(), timeout=1)
        stop.set()
        await task

    asyncio.run(exercise())

    assert len(repository.rows) == 2
    assert len(repository.checkpoints) == 2
    assert {row["symbol"] for row in repository.rows} == {"BTCUSDT", "ETHUSDT"}
    assert all(row["trade_count"] == 1 and row["buy_volume"] == Decimal("0.01") for row in repository.rows)
    completed = [details for component, status, details in health if component == "binance_spot" and status == "AVAILABLE"]
    assert completed[-1]["runtime_stage"] == "PERSISTED"
    assert completed[-1]["parsed_trades"] == 2


def test_binance_websocket_reconnects_and_resubscribes_after_transport_failure():
    import aiohttp

    calls = 0
    subscriptions = []
    health = []
    stop = asyncio.Event()
    trade = {
        "e": "aggTrade", "s": "ETHUSDT", "a": 7,
        "p": "3200.5", "q": "0.2", "f": 6, "l": 7,
        "T": 1_790_000_000_000, "m": True,
    }

    class FakeSocket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send_json(self, payload):
            subscriptions.append(payload)

        async def receive(self):
            stop.set()
            return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(trade))

        async def close(self):
            pass

    def connector(_session, _endpoint):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("sensitive-provider-token")
        return FakeSocket()

    runtime = Phase7CollectorRuntime(
        _settings(),
        health_writer=lambda component, status, _checked, details: health.append(
            (component, status.value, details)
        ),
        spot_ws_connector=connector,
    )

    asyncio.run(runtime._spot_ws_supervisor(stop))

    assert calls == 2
    assert len(subscriptions) == 2
    assert subscriptions[0] == {"method": "SUBSCRIBE", "params": ["btcusdt@aggTrade"], "id": 1}
    assert runtime._spot_events["ETHUSDT"][0].trade_id == "7"
    assert all("sensitive-provider-token" not in str(entry) for entry in health)
    first_failure = next(
        row for row in health if row[0] == "binance_spot_websocket" and row[2].get("error_category") == "NETWORK"
    )
    assert first_failure[1] == "NOT_AVAILABLE"
    assert first_failure[2]["phase7_status"] == "PARTIAL"
    assert first_failure[2]["data_quality"] == "PARTIAL"
    assert first_failure[2]["consecutive_failures"] == 1


def test_stage1_phase7_context_persists_only_additive_context_rows(monkeypatch):
    from quant_phase7 import runtime as runtime_module

    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    responses = [
        [("BTC", "BITCOIN", "asset-btc", "1m", now, now, "AVAILABLE", Decimal("0.8"))],
        [("ETHUSDT", "binance", "1m", now, now, "PARTIAL", Decimal("0.5"))],
    ]

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            pass

        def fetchall(self):
            return responses.pop(0)

    class Connection:
        def cursor(self):
            return Cursor()

    class Repo:
        rows = []

        def __init__(self, _connection):
            pass

        def upsert_stage1_enrichment(self, row):
            self.rows.append(row)
            return 1

    monkeypatch.setattr(runtime_module, "Phase7Repository", Repo)
    count = runtime_module.persist_stage1_phase7_context(
        Connection(),
        screening_run_id=7,
        candidates=[SimpleNamespace(symbol="BTCUSDT"), SimpleNamespace(symbol="ETHUSDT"), SimpleNamespace(symbol="XRPUSDT")],
        processed_at=now,
    )

    assert count == 3
    assert [row["status"] for row in Repo.rows] == ["AVAILABLE", "PARTIAL", "NOT_AVAILABLE"]
    assert all("category" not in row and "eligibility" not in row for row in Repo.rows)


def test_engine_stage1_phase7_hook_is_savepoint_isolated_and_context_only(monkeypatch):
    from quant_phase1.entrypoints import engine
    from quant_phase1.config import Settings

    settings = Settings.from_env({"TRADING_MODE": "paper", "PHASE7_ENABLED": "1"})
    connection = SimpleNamespace(calls=[], execute=lambda *args: connection.calls.append(args))
    monkeypatch.setattr(
        "quant_phase7.runtime.persist_stage1_phase7_context",
        lambda *_args, **_kwargs: 4,
    )

    result = engine.run_phase7_context_hook(
        settings, connection, 11, [SimpleNamespace(symbol="BTCUSDT")],
        datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc),
    )

    assert result == {"persisted": 4, "errors": 0}
    assert connection.calls == [("SAVEPOINT phase7_context_cycle",), ("RELEASE SAVEPOINT phase7_context_cycle",)]
