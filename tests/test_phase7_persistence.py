from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
from decimal import Decimal
from itertools import repeat
import json
import os

import pytest

from quant_phase7.contracts import (
    DataStatus,
    FinalityStatus,
    OnChainTransferEvent,
    ReasonCode,
)
from quant_phase7.persistence import MAX_TRANSFER_BATCH, Phase7Repository, _context_event
from quant_phase7.labels import AddressLabel, LabelCategory, LabelSnapshot
from quant_phase7.contracts import Chain

from tests.test_phase7_contracts import _btc_event_values


NOW = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)


@pytest.mark.skipif(not os.environ.get("TEST_POSTGRES_DSN"), reason="TEST_POSTGRES_DSN is not configured")
def test_postgres_persists_ethereum_uint256_amount_with_exact_decimal_scale():
    import psycopg

    from quant_phase1.db import apply_migrations
    from quant_phase7.ethereum import EthereumBlockParser

    now = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
    raw_amount = 123456789012345678901234567890123456789
    block_hash = "ab" * 32
    tx_hash = "cd" * 32
    block = {
        "number": "0x64",
        "hash": "0x" + block_hash,
        "timestamp": "0x6ab2b800",
        "finalityTag": "finalized",
        "transactions": [{
            "hash": "0x" + tx_hash,
            "transactionIndex": "0x0",
            "from": "0x1111111111111111111111111111111111111111",
            "to": "0x2222222222222222222222222222222222222222",
            "value": hex(raw_amount),
        }],
    }
    receipt = {
        "transactionHash": "0x" + tx_hash,
        "blockHash": "0x" + block_hash,
        "blockNumber": "0x64",
        "transactionIndex": "0x0",
        "status": "0x1",
        "logs": [],
    }
    event = EthereumBlockParser().parse_block(
        block, chain_id="0x1", receipts=[receipt], observed_at=now,
        fetched_at=now, processed_at=now,
    )[0]
    expected = Decimal("123456789012345678901.234567890123456789")
    assert event.amount.amount_normalized == expected

    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as connection:
        apply_migrations(connection)
        repository = Phase7Repository(connection)
        connection.execute("SAVEPOINT exact_uint256_amount")
        repository.upsert_asset_registry([{
            "asset_id": "ETHEREUM:NATIVE:NATIVE:ethereum-native-v1",
            "chain": "ETHEREUM", "asset_kind": "NATIVE", "contract_address": None,
            "symbol": "ETH", "decimals": 18,
            "registry_version": "ethereum-native-v1", "effective_from": now,
            "effective_to": None, "source_id": "test.ethereum",
            "source_version": "integration-test-v1", "source_reference": "fixture:eth:native",
            "snapshot_hash": "ef" * 32, "status": "AVAILABLE",
            "created_at": now, "updated_at": now,
        }])
        repository.upsert_transfer_events((event,))
        stored = connection.execute(
            "SELECT amount_raw, amount_normalized FROM phase7_onchain_transfer_events WHERE event_id=%s",
            (event.event_id,),
        ).fetchone()
        assert stored == (str(raw_amount), expected)
        connection.execute("ROLLBACK TO SAVEPOINT exact_uint256_amount")


class Cursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.rowcount = 1
        self.returning_rows: list[tuple[str, str]] = [("btc:old-event", "0" * 64)]
        self.one_row = (Decimal("12.5"),)

    def executemany(self, sql, values):
        self.calls.append((str(sql), list(values)))

    def execute(self, sql, values=None):
        self.calls.append((str(sql), values))

    def fetchall(self):
        return self.returning_rows if self.rowcount else []

    def fetchone(self):
        return self.one_row


class Connection:
    def __init__(self) -> None:
        self.cursor_instance = Cursor()
        self.calls: list[tuple[str, object]] = []

    def cursor(self):
        return _CursorContext(self.cursor_instance)

    def execute(self, sql, values=None):
        self.calls.append((str(sql), values))
        return type("Result", (), {"rowcount": 1})()

    @contextmanager
    def transaction(self):
        yield self


class _CursorContext:
    def __init__(self, cursor: Cursor) -> None:
        self.cursor = cursor

    def __enter__(self):
        return self.cursor

    def __exit__(self, *args):
        return False


def _event() -> OnChainTransferEvent:
    return OnChainTransferEvent(
        **_btc_event_values(),
        status=DataStatus.AVAILABLE,
        finality_status=FinalityStatus.CONFIRMED,
        reason_code=ReasonCode.COMPLETE,
    )


def _unique_event(index: int) -> OnChainTransferEvent:
    event = _event()
    tx_hash = f"{index + 1:064x}"
    identity = replace(event.identity, tx_hash=tx_hash)
    return replace(
        event,
        event_id=f"btc:{event.block_hash}:{tx_hash}:{identity.event_index}",
        identity=identity,
    )


def _unique_events(count: int):
    return (_unique_event(index) for index in range(count))


class TransactionalMemoryConnection:
    """Deterministic transaction model for chunk commit/rollback tests."""

    def __init__(self) -> None:
        self.event_ids: set[str] = set()
        self.cursor_value = "100"
        self.active: dict[str, object] | None = None
        self.commits = 0
        self.rollbacks = 0

    @contextmanager
    def transaction(self):
        assert self.active is None
        self.active = {"event_ids": set(self.event_ids), "cursor_value": self.cursor_value}
        try:
            yield self
        except BaseException:
            self.active = None
            self.rollbacks += 1
            raise
        else:
            self.event_ids = self.active["event_ids"]
            self.cursor_value = self.active["cursor_value"]
            self.active = None
            self.commits += 1


class ChunkRecordingRepository(Phase7Repository):
    def __init__(
        self,
        connection,
        *,
        fail_on_chunk: int | None = None,
        checkpoint_result: int = 1,
    ) -> None:
        super().__init__(connection)
        self.chunk_sizes: list[int] = []
        self.fail_on_chunk = fail_on_chunk
        self.checkpoint_result = checkpoint_result
        self.checkpoint_calls = 0

    def upsert_transfer_events(self, events):
        batch = tuple(events)
        assert 0 < len(batch) <= MAX_TRANSFER_BATCH
        self.chunk_sizes.append(len(batch))
        active = self.connection.active
        assert active is not None
        if self.fail_on_chunk == len(self.chunk_sizes):
            partial = max(1, len(batch) // 2)
            active["event_ids"].update(event.event_id for event in batch[:partial])
            raise RuntimeError("simulated bounded chunk write failure")
        before = len(active["event_ids"])
        active["event_ids"].update(event.event_id for event in batch)
        return len(active["event_ids"]) - before

    def upsert_checkpoint(self, row):
        active = self.connection.active
        assert active is not None
        self.checkpoint_calls += 1
        active["cursor_value"] = row["cursor_value"]
        return self.checkpoint_result


def _checkpoint_row(cursor_value: str = "101") -> dict[str, object]:
    return {
        "source_id": "btc_core_rpc",
        "scope_kind": "CHAIN",
        "scope_key": "BITCOIN_MAINNET",
        "cursor_kind": "BLOCK",
        "cursor_value": cursor_value,
        "last_observed_cursor": cursor_value,
        "last_finalized_cursor": "99",
        "last_block_hash": "0" * 64,
        "parser_version": "phase7-bitcoin-v1",
        "schema_version": "phase7-onchain-v1",
        "status": "AVAILABLE",
        "reason": None,
        "updated_at": NOW,
    }


@pytest.mark.parametrize(
    ("event_count", "expected_sizes"),
    [
        (9_999, [9_999]),
        (10_000, [10_000]),
        (10_001, [10_000, 1]),
        (12_665, [10_000, 2_665]),
    ],
)
def test_block_chunks_are_bounded_and_cursor_is_written_after_all_chunks(event_count, expected_sizes):
    connection = TransactionalMemoryConnection()
    repository = ChunkRecordingRepository(connection)

    persisted, checkpoint_count = repository.persist_transfer_chunks_and_checkpoint(
        _unique_events(event_count), _checkpoint_row(),
    )

    assert repository.chunk_sizes == expected_sizes
    assert persisted == event_count
    assert checkpoint_count == 1
    assert repository.checkpoint_calls == 1
    assert connection.cursor_value == "101"
    assert len(connection.event_ids) == event_count
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_middle_chunk_failure_rolls_back_rows_and_leaves_cursor_unchanged():
    connection = TransactionalMemoryConnection()
    repository = ChunkRecordingRepository(connection, fail_on_chunk=2)

    with pytest.raises(RuntimeError, match="simulated bounded chunk write failure"):
        repository.persist_transfer_chunks_and_checkpoint(
            _unique_events(12_665), _checkpoint_row(),
        )

    assert repository.chunk_sizes == [10_000, 2_665]
    assert repository.checkpoint_calls == 0
    assert connection.event_ids == set()
    assert connection.cursor_value == "100"
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_checkpoint_failure_rolls_back_all_transfer_chunks():
    connection = TransactionalMemoryConnection()
    repository = ChunkRecordingRepository(connection, checkpoint_result=0)

    with pytest.raises(ValueError, match="checkpoint did not advance"):
        repository.persist_transfer_chunks_and_checkpoint(
            _unique_events(12_665), _checkpoint_row(),
        )

    assert repository.chunk_sizes == [10_000, 2_665]
    assert repository.checkpoint_calls == 1
    assert connection.event_ids == set()
    assert connection.cursor_value == "100"
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_replaying_same_block_chunks_keeps_canonical_event_identity_idempotent():
    connection = TransactionalMemoryConnection()
    repository = ChunkRecordingRepository(connection)

    repository.persist_transfer_chunks_and_checkpoint(_unique_events(12_665), _checkpoint_row())
    first_ids = set(connection.event_ids)
    repository.persist_transfer_chunks_and_checkpoint(_unique_events(12_665), _checkpoint_row())

    assert len(first_ids) == 12_665
    assert connection.event_ids == first_ids
    assert connection.cursor_value == "101"
    assert repository.chunk_sizes == [10_000, 2_665, 10_000, 2_665]
    assert connection.commits == 2


def test_persisted_canonical_event_rehydrates_for_engine_aggregation():
    event = _event()
    valuation = event.valuation
    row = {
        "event_id": event.event_id,
        "chain": event.identity.chain.value,
        "block_number": event.block_number,
        "block_hash": event.block_hash,
        "tx_hash": event.identity.tx_hash,
        "tx_index": event.identity.tx_index,
        "event_index": event.identity.event_index,
        "event_index_kind": event.identity.event_index_kind.value,
        "asset_kind": event.identity.asset_id.kind.value,
        "contract_address": event.identity.contract_address,
        "registry_version": event.identity.asset_id.registry_version,
        "from_address": event.from_address,
        "to_address": event.to_address,
        "from_address_set_ref": json.dumps(asdict(event.from_address_set_reference), default=str)
        if event.from_address_set_reference else None,
        "raw_reference": json.dumps(asdict(event.raw_reference), default=str) if event.raw_reference else None,
        "amount_raw": event.amount.amount_raw,
        "decimals": event.amount.decimals,
        "amount_usd": valuation.amount_usd,
        "valuation_price": valuation.valuation_price,
        "valuation_exchange": valuation.valuation_exchange,
        "valuation_source": valuation.valuation_source,
        "valuation_reason": valuation.reason_code.value if valuation.reason_code else None,
        "valuation_status": valuation.status.value,
        "valuation_exchange_timestamp": valuation.valuation_exchange_timestamp,
        "valuation_fetched_at": valuation.valuation_fetched_at,
        "valuation_skew_seconds": int(valuation.valuation_skew.total_seconds()) if valuation.valuation_skew else None,
        "event_time": event.event_time,
        "observed_at": event.provenance.observed_at,
        "fetched_at": event.provenance.fetched_at,
        "processed_at": event.provenance.processed_at,
        "status": event.status.value,
        "reason": event.reason_code.value,
        "finality_status": event.finality_status.value,
        "source_id": event.provenance.source,
        "source_version": event.provenance.source_version,
        "source_reference": event.provenance.source_reference,
        "source_hash": event.provenance.source_hash,
        "schema_version": event.schema_version,
        "normalization_version": event.normalization_version,
        "details": dict(event.details),
    }

    restored = _context_event(row)

    assert restored.event_id == event.event_id
    assert restored.identity == event.identity
    assert restored.amount == event.amount
    assert restored.valuation == event.valuation
    assert restored.provenance == event.provenance


def test_context_window_scan_returns_source_processed_at_watermark():
    connection = Connection()
    repository = Phase7Repository(connection)
    watermark = datetime(2026, 9, 23, 0, 4, tzinfo=timezone.utc)
    connection.cursor_instance.returning_rows = [(NOW, watermark)]
    repository.load_context_asset_ids = lambda *_args, **_kwargs: ({
        "chain": "BITCOIN", "asset_id": "BITCOIN:NATIVE:NATIVE:btc-v1",
    },)

    rows = repository.load_context_window_keys(NOW, NOW.replace(minute=5))

    assert len(rows) == 5
    assert all(row["window_open"] == NOW for row in rows)
    assert all(row["latest_processed_at"] == watermark for row in rows)
    assert all("max(processed_at)" in sql for sql, _params in connection.cursor_instance.calls)
    assert all("GROUP BY window_open" in sql for sql, _params in connection.cursor_instance.calls)


def _spot_window() -> dict[str, object]:
    return {
        "exchange": "BINANCE",
        "symbol": "BTCUSDT",
        "market_kind": "SPOT",
        "timeframe": "5m",
        "window_open": NOW,
        "window_close": NOW.replace(minute=5),
        "aggregation_version": "phase7-spot-v1",
        "base_volume": 1,
        "quote_volume": 50000,
        "buy_volume": 1,
        "sell_volume": 0,
        "unknown_volume": 0,
        "delta": 1,
        "cvd": 1,
        "trade_count": 1,
        "directional_trade_count": 1,
        "event_time_first": NOW,
        "event_time_last": NOW,
        "cursor_first": "1",
        "cursor_last": "1",
        "sample_count": 1,
        "source_count": 1,
        "available_count": 1,
        "missing_count": 0,
        "coverage_ratio": 1,
        "status": "AVAILABLE",
        "reason": "COMPLETE",
        "source_reference": "fixture:spot:1",
        "normalization_version": "phase7-v1",
        "processed_at": NOW,
        "created_at": NOW,
    }


def test_transfer_event_persistence_is_idempotent_and_preserves_units():
    connection = Connection()
    repo = Phase7Repository(connection)

    assert repo.upsert_transfer_events([_event()]) == 1
    sql, values = connection.cursor_instance.calls[-1]
    assert "on conflict (event_id)" in sql.lower()
    assert "amount_raw" in sql
    assert "raw_reference" in sql
    assert "asset_kind" in sql
    assert "from_address_set_ref" in sql
    assert "is not distinct from excluded.event_index" in sql.lower()
    assert values[0][0] == "btc:" + "0" * 64 + ":btc-tx-1:0"


def test_transfer_persistence_rejects_unbounded_iterables():
    connection = Connection()
    repo = Phase7Repository(connection)
    with pytest.raises(ValueError, match="bounded"):
        repo.upsert_transfer_events(repeat(_event(), 10_001))


def test_transfer_batch_and_checkpoint_share_one_transaction():
    connection = Connection()
    repo = Phase7Repository(connection)
    checkpoint = {
        "source_id": "btc_core_rpc",
        "scope_kind": "CHAIN",
        "scope_key": "BITCOIN_MAINNET",
        "cursor_kind": "BLOCK",
        "cursor_value": "101",
        "last_observed_cursor": "101",
        "last_finalized_cursor": "99",
        "last_block_hash": "0" * 64,
        "parser_version": "phase7-bitcoin-v1",
        "schema_version": "phase7-onchain-v1",
        "status": "AVAILABLE",
        "reason": None,
        "updated_at": NOW,
    }
    assert repo.persist_transfer_batch_and_checkpoint([_event()], checkpoint) == (1, 1)
    assert len(connection.cursor_instance.calls) == 2


def test_reorg_recovery_marks_old_rows_and_persists_replacement_atomically():
    connection = Connection()
    repo = Phase7Repository(connection)
    checkpoint = {
        "source_id": "btc_core_rpc", "scope_kind": "CHAIN", "scope_key": "BITCOIN_MAINNET",
        "cursor_kind": "BLOCK", "cursor_value": "101", "last_observed_cursor": "101",
        "last_finalized_cursor": "99", "last_block_hash": "0" * 64,
        "parser_version": "phase7-bitcoin-v1", "schema_version": "phase7-onchain-v1",
        "status": "AVAILABLE", "reason": None, "updated_at": NOW,
    }
    replacement_values = _btc_event_values()
    replacement_values.update({
        "event_id": "btc:" + "1" * 64 + ":btc-tx-replacement:0",
        "identity": _event().identity.__class__(
            chain=_event().identity.chain, tx_hash="btc-tx-replacement", tx_index=None,
            event_index_kind=_event().identity.event_index_kind, event_index=0,
            asset_id=_event().identity.asset_id, contract_address=None, block_hash="1" * 64,
        ),
        "block_hash": "1" * 64,
        "status": DataStatus.AVAILABLE,
        "finality_status": FinalityStatus.CONFIRMED,
        "reason_code": ReasonCode.COMPLETE,
    })
    marked, replacements, checkpoint_count = repo.persist_reorg_recovery(
        ["btc:old-event"], [OnChainTransferEvent(**replacement_values)], checkpoint, processed_at=NOW,
    )
    assert (marked, replacements, checkpoint_count) == (1, 1, 1)
    assert "reorged_event" in connection.cursor_instance.calls[0][0].lower()

    with pytest.raises(ValueError, match="new block identity"):
        repo.persist_reorg_recovery(["btc:old-event"], [_event()], checkpoint, processed_at=NOW)
    reused_identity = dict(replacement_values, event_id="btc:old-event")
    with pytest.raises(ValueError, match="new event identities"):
        repo.persist_reorg_recovery(
            ["btc:old-event"], [OnChainTransferEvent(**reused_identity)], checkpoint, processed_at=NOW,
        )

    failing = Connection()
    failing.cursor_instance.rowcount = 0
    with pytest.raises(ValueError, match="every old event"):
        Phase7Repository(failing).persist_reorg_recovery(
            ["btc:missing-event"], [_event()], checkpoint, processed_at=NOW,
        )


def test_reorg_replacement_block_is_persisted_in_bounded_chunks():
    connection = Connection()
    repository = Phase7Repository(connection)
    checkpoint = _checkpoint_row()

    def replacements():
        for index in range(10_001):
            event = _unique_event(index)
            new_hash = "1" * 64
            identity = replace(event.identity, block_hash=new_hash)
            tx_hash = identity.tx_hash
            yield replace(
                event,
                event_id=f"btc:{new_hash}:{tx_hash}:{identity.event_index}",
                identity=identity,
                block_hash=new_hash,
            )

    repository.persist_reorg_recovery(
        ["btc:old-event"], replacements(), checkpoint, processed_at=NOW,
    )

    insert_batches = [
        values for sql, values in connection.cursor_instance.calls
        if "INSERT INTO phase7_onchain_transfer_events" in sql
    ]
    assert [len(batch) for batch in insert_batches] == [10_000, 1]


def test_spot_windows_are_separate_and_market_kind_is_persisted():
    connection = Connection()
    repo = Phase7Repository(connection)

    assert repo.upsert_windows("phase7_spot_flow_windows", [_spot_window()]) == 1
    sql, values = connection.cursor_instance.calls[-1]
    assert "phase7_spot_flow_windows" in sql
    assert "trade_flow_windows" not in sql
    assert "on conflict (exchange,symbol,market_kind,timeframe,window_open,aggregation_version)" in sql.lower()
    assert "SPOT" in values[0]

    with pytest.raises(ValueError, match="unsupported"):
        repo.upsert_windows("trade_flow_windows", [_spot_window()])


def test_checkpoint_and_stage1_enrichment_are_idempotent_and_bounded():
    connection = Connection()
    repo = Phase7Repository(connection)
    checkpoint = {
        "source_id": "fixture.ethereum_rpc", "scope_kind": "CHAIN", "scope_key": "ETHEREUM",
        "cursor_kind": "BLOCK", "cursor_value": "100", "last_observed_cursor": "100",
        "last_finalized_cursor": "99", "last_block_hash": "a" * 64,
        "parser_version": "v1", "schema_version": "v1", "status": "AVAILABLE",
        "reason": "COMPLETE", "updated_at": NOW,
    }
    assert repo.upsert_checkpoint(checkpoint) == 1
    checkpoint_sql = connection.cursor_instance.calls[-1][0].lower()
    assert "on conflict (source_id,scope_kind,scope_key)" in checkpoint_sql
    assert "last_finalized_cursor" in checkpoint_sql
    assert "coalesce(" in checkpoint_sql
    with pytest.raises(ValueError):
        repo.upsert_checkpoint({**checkpoint, "cursor_value": "opaque", "last_observed_cursor": "opaque"})

    row = {
        "screening_run_id": 1, "symbol": "BTCUSDT", "context_reference": {"source": "fixture"},
        "coverage": {"status": "NOT_AVAILABLE"}, "status": "NOT_AVAILABLE",
        "reason": "MISSING_REQUIRED_DATA", "normalization_version": "phase7-v1",
        "created_at": NOW, "processed_at": NOW,
    }
    assert repo.upsert_stage1_enrichment(row) == 1
    assert "on conflict (screening_run_id,symbol)" in connection.cursor_instance.calls[-1][0].lower()


def test_address_label_persistence_requires_canonical_reviewed_row():
    connection = Connection()
    repo = Phase7Repository(connection)
    label = AddressLabel(
        chain=Chain.ETHEREUM,
        address="0x" + "11" * 20,
        category=LabelCategory.KNOWN_EXTERNAL, source_id="reviewed", source_version="registry-v1",
        label_version="snapshot-v1", confidence=Decimal("1.0"), snapshot_hash="a" * 64,
        source_reference="https://labels.example/snapshot-v1.json", effective_from=NOW,
        effective_to=None, observed_at=NOW, updated_at=NOW, status=DataStatus.AVAILABLE, reason=None,
    )
    snapshot = LabelSnapshot(
        chain=Chain.ETHEREUM, source_id="reviewed", source_version="registry-v1",
        label_version="snapshot-v1", source_urls=("https://labels.example/snapshot-v1.json",),
        reviewer="operator-1", snapshot_hash="a" * 64, effective_from=NOW,
        effective_to=None, coverage_denominator=1, reviewed=True, labels=(label,),
    )
    assert repo.upsert_address_labels(snapshot) == 1
    assert '"coverage_denominator":1' in connection.cursor_instance.calls[-1][1][0][8]
    with pytest.raises(ValueError, match="LabelSnapshot"):
        repo.upsert_address_labels({"not": "a snapshot"})


def test_asset_identity_is_not_mutated_and_sparse_spot_times_are_allowed():
    connection = Connection()
    repo = Phase7Repository(connection)
    asset = {
        "asset_id": "ETHEREUM:NATIVE:NATIVE:v1", "chain": "ETHEREUM",
        "asset_kind": "NATIVE", "contract_address": None, "symbol": "ETH", "decimals": 18,
        "registry_version": "v1", "effective_from": NOW, "effective_to": None,
        "source_id": "fixture", "source_version": "v1", "source_reference": "fixture:asset",
        "snapshot_hash": "a" * 64, "status": "AVAILABLE", "created_at": NOW, "updated_at": NOW,
    }
    assert repo.upsert_asset_registry([asset]) == 1
    asset_sql = connection.cursor_instance.calls[-1][0].lower()
    assert "contract_address=excluded.contract_address" not in asset_sql
    assert "decimals=excluded.decimals" not in asset_sql
    assert "registry_version=excluded.registry_version" not in asset_sql

    sparse = _spot_window()
    sparse["event_time_first"] = None
    sparse["event_time_last"] = None
    assert repo.upsert_windows("phase7_spot_flow_windows", [sparse]) == 1


def test_latest_spot_cvd_is_loaded_only_from_same_exchange_symbol_and_timeframe():
    connection = Connection()
    repo = Phase7Repository(connection)

    assert repo.load_latest_spot_cvd("binance", "BTCUSDT") == Decimal("12.5")

    sql, params = connection.cursor_instance.calls[-1]
    assert "timeframe='1m'" in sql.lower()
    assert "market_kind='spot'" in sql.lower()
    assert params == ("binance", "BTCUSDT")


def test_aggregate_persistence_rejects_invalid_canonical_semantics():
    connection = Connection()
    repo = Phase7Repository(connection)
    invalid = _spot_window()
    invalid["unknown_volume"] = 2
    with pytest.raises(ValueError, match="unknown_volume"):
        repo.upsert_windows("phase7_spot_flow_windows", [invalid])


def test_retention_is_allowlisted_and_bounded():
    connection = Connection()
    repo = Phase7Repository(connection)
    assert repo.cleanup("phase7_onchain_transfer_events", NOW, batch_size=10, max_batches=2) == 1
    sql, values = connection.calls[-1]
    assert "ctid" in sql.lower()
    assert "limit %s" in sql.lower()
    assert values == (NOW, 10)

    with pytest.raises(ValueError, match="unsupported"):
        repo.cleanup("market_snapshots", NOW)
