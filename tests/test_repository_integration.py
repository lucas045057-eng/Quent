import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from quant_phase1.contracts import Candle, DataStatus, Instrument
from quant_phase1.db import apply_migrations
from quant_phase1.repositories import Phase1Repository
from quant_phase1.service import persist_stage1
from quant_phase1.stage1 import Stage1Result
from quant_phase7.bitcoin import BitcoinBlockParser
from quant_phase7.persistence import Phase7Repository


DSN = os.environ.get("TEST_POSTGRES_DSN")


@pytest.fixture(autouse=True)
def isolated_repository_schema(monkeypatch):
    if not DSN:
        yield
        return
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    original=DSN
    schema='repository_fixture_'+uuid4().hex
    with psycopg.connect(original,autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    monkeypatch.setattr(__import__(__name__,fromlist=['DSN']),'DSN',
        make_conninfo(original,options='-c search_path='+schema+',public'))
    try:
        yield
    finally:
        with psycopg.connect(original,autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def _instrument(now: datetime) -> Instrument:
    return Instrument("BTCUSDT", "USDT-FUTURES", "BTC", "USDT", "PERPETUAL", "perpetual", "online", 2, 3, Decimal("0.001"), None, now, {"symbolType": "crypto"})


def _candle(now: datetime) -> Candle:
    return Candle("BTCUSDT", "5m", datetime(2026, 9, 20, 10, 25, tzinfo=timezone.utc), Decimal("99"), Decimal("102"), Decimal("98"), Decimal("101"), Decimal("10"), Decimal("1000"), now, now, now, DataStatus.AVAILABLE, True, [])


@pytest.mark.skipif(not DSN, reason="TEST_POSTGRES_DSN is not configured")
def test_postgres_migrations_and_kline_upsert_are_restart_idempotent():
    import psycopg
    from psycopg import sql

    now = datetime.now(timezone.utc)
    schema = f"repository_integration_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    connection = psycopg.connect(DSN, autocommit=True, options=f"-c search_path={schema},public")
    try:
        applied_first = apply_migrations(connection)
        applied_second = apply_migrations(connection)
        repository = Phase1Repository(connection)
        repository.upsert_instrument(_instrument(now))
        repository.upsert_candle(_candle(now))
        repository.upsert_candle(_candle(now))
        expected_migrations = sorted(
            path.name for path in (Path(__file__).parents[1] / "migrations").glob("*.sql")
        )
        assert applied_first == expected_migrations
        assert applied_second == []
        assert repository.count("klines") == 1
    finally:
        connection.close()
        with psycopg.connect(DSN, autocommit=True) as cleanup:
            cleanup.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.skipif(not DSN, reason="TEST_POSTGRES_DSN is not configured")
def test_phase7_checkpoint_trigger_and_atomic_reorg_cursor_rollback():
    import psycopg

    now = datetime(2026, 9, 23, tzinfo=timezone.utc)
    block_hash = "ab" * 32
    with psycopg.connect(DSN) as connection:
        apply_migrations(connection)
        asset_repo = Phase7Repository(connection)
        asset_repo.upsert_asset_registry([{
            "asset_id": "BITCOIN:NATIVE:NATIVE:btc-v1", "chain": "BITCOIN",
            "asset_kind": "NATIVE", "contract_address": None, "symbol": "BTC", "decimals": 8,
            "registry_version": "btc-v1", "effective_from": now, "effective_to": None,
            "source_id": "btc_core_rpc", "source_version": "bitcoin-core-v1",
            "source_reference": "fixture:btc:asset", "snapshot_hash": "a" * 64,
            "status": "AVAILABLE", "created_at": now, "updated_at": now,
        }])
        event = BitcoinBlockParser().parse_block({
            "hash": block_hash, "height": 100, "time": 1790035200, "confirmations": 6,
            "tx": [{
                "txid": "01" * 32,
                "vin": [{"txid": "02" * 32, "vout": 0, "prevout": {"scriptPubKey": {"address": "bc1qsource"}}}],
                "vout": [{"n": 0, "value": "0.25000000", "scriptPubKey": {"address": "bc1qdestination"}}],
            }],
        }, observed_at=now, fetched_at=now, processed_at=now)[0]
        checkpoint = {
            "source_id": "btc_core_rpc", "scope_kind": "CHAIN", "scope_key": "BITCOIN_MAINNET",
            "cursor_kind": "BLOCK", "cursor_value": "100", "last_observed_cursor": "100",
            "last_finalized_cursor": "99", "last_block_hash": block_hash,
            "parser_version": "phase7-bitcoin-v1", "schema_version": "phase7-onchain-v1",
            "status": "AVAILABLE", "reason": None, "updated_at": now,
        }
        repository = Phase7Repository(connection)
        assert repository.persist_transfer_batch_and_checkpoint([event], checkpoint) == (1, 1)
        regressed = {**checkpoint, "cursor_value": "99", "last_observed_cursor": "99", "last_finalized_cursor": "98"}
        with pytest.raises(ValueError, match="checkpoint did not advance"):
            repository.persist_transfer_batch_and_checkpoint([event], regressed)
        assert connection.execute(
            "SELECT cursor_value FROM phase7_ingestion_checkpoints WHERE source_id=%s",
            ("btc_core_rpc",),
        ).fetchone()[0] == "100"
        assert connection.execute("SELECT count(*) FROM phase7_onchain_transfer_events").fetchone()[0] == 1

        connection.execute("SAVEPOINT invalid_checkpoint")
        with pytest.raises(psycopg.Error):
            connection.execute(
                """
                INSERT INTO phase7_ingestion_checkpoints (
                    source_id,scope_kind,scope_key,cursor_kind,cursor_value,last_observed_cursor,
                    last_finalized_cursor,last_block_hash,parser_version,schema_version,status,reason,updated_at
                ) VALUES ('bad','CHAIN','BAD','BLOCK','opaque','opaque',NULL,%s,'v1','v1','AVAILABLE',NULL,%s)
                """, (block_hash, now),
            )
        connection.execute("ROLLBACK TO SAVEPOINT invalid_checkpoint")

        connection.execute("SAVEPOINT initial_checkpoint")
        connection.execute(
            """
            INSERT INTO phase7_ingestion_checkpoints (
                source_id,scope_kind,scope_key,cursor_kind,cursor_value,last_observed_cursor,
                last_finalized_cursor,last_block_hash,parser_version,schema_version,status,reason,updated_at
            ) VALUES ('initial','CHAIN','INITIAL','BLOCK','0',NULL,NULL,%s,'v1','v1','AVAILABLE',NULL,%s)
            """, (block_hash, now),
        )
        assert connection.execute(
            "SELECT last_observed_cursor FROM phase7_ingestion_checkpoints WHERE source_id='initial'",
        ).fetchone()[0] is None
        connection.execute("ROLLBACK TO SAVEPOINT initial_checkpoint")

        connection.execute("SAVEPOINT malformed_hash")
        with pytest.raises(psycopg.Error):
            connection.execute(
                """
                INSERT INTO phase7_ingestion_checkpoints (
                    source_id,scope_kind,scope_key,cursor_kind,cursor_value,last_observed_cursor,
                    last_finalized_cursor,last_block_hash,parser_version,schema_version,status,reason,updated_at
                ) VALUES ('bad-hash','CHAIN','BAD-HASH','BLOCK','1','1',NULL,%s,'v1','v1','AVAILABLE',NULL,%s)
                """, ("z" * 64, now),
            )
        connection.execute("ROLLBACK TO SAVEPOINT malformed_hash")


@pytest.mark.skipif(not DSN, reason="TEST_POSTGRES_DSN is not configured")
def test_phase1_service_writes_stage1_result_and_phase9_outbox_in_one_connection():
    import psycopg

    now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    with psycopg.connect(DSN) as connection:
        apply_migrations(connection)
        repository = Phase1Repository(connection)
        repository.upsert_instrument(_instrument(now))
        result = Stage1Result(
            "BTCUSDT", "A", "HIGH_CONFIDENCE", DataStatus.AVAILABLE,
            ("price", "closed_5m"), {"atr": Decimal("1.25")}, "BULLISH",
            ("STRUCTURE_ALIGNED",), {"range_to_atr": Decimal("2.5")}, None, now,
        )

        assert persist_stage1(repository, SimpleNamespace(collected_at=now), [result]) == 1
        assert connection.execute("SELECT count(*) FROM screening_results").fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM outbox_events WHERE event_id IS NOT NULL AND phase9_state = 'PENDING'"
        ).fetchone()[0] == 1


@pytest.mark.skipif(not DSN, reason="TEST_POSTGRES_DSN is not configured")
def test_canonical_market_batch_retains_persisted_instrument_for_v2_execution():
    import psycopg
    from psycopg import sql
    from dataclasses import replace
    from quant_phase1.contracts import Ticker
    from strategies.market_view import build_market_view

    now = datetime(2026, 10, 3, 16, 0, tzinfo=timezone.utc)
    raw = {"symbol": "BTCUSDT", "symbolType": "crypto", "category": "USDT-FUTURES",
           "status": "online", "type": "perpetual", "priceMultiplier": "0.1",
           "quantityMultiplier": "0.001", "maxMarketOrderQty": "150", "minOrderAmount": "5"}
    instrument = replace(_instrument(now), raw_payload=raw)
    ticker = Ticker("BTCUSDT", Decimal("100"), Decimal("99.9"), Decimal("100.1"),
                    Decimal("1"), Decimal("1"), Decimal("10"), Decimal("1000"),
                    None, None, now, now, now, DataStatus.AVAILABLE, {})
    schema = f"instrument_roundtrip_{uuid4().hex}"
    with psycopg.connect(DSN, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        with psycopg.connect(DSN, options=f"-c search_path={schema}") as conn:
            apply_migrations(conn)
            repo = Phase1Repository(conn)
            repo.upsert_instrument(instrument)
            repo.insert_market_snapshot(ticker, now)
            batch = repo.load_latest_market_batch(limit=1)
            assert batch is not None
            assert batch.instruments == [instrument]
            market = build_market_view(batch, as_of=now).symbols[0]
            assert market.instrument is not None
            assert market.instrument.tick == Decimal("0.1")
            assert market.instrument.lot == Decimal("0.001")
            assert market.instrument.max_quantity == Decimal("150")
            assert batch.instruments[0].fetched_at == now
            repo.upsert_instrument(replace(instrument, raw_payload={"category": "USDT-FUTURES"}))
            incomplete = repo.load_latest_market_batch(limit=1)
            assert incomplete is None  # Unknown crypto classification is outside the canonical universe.
            repo.upsert_instrument(instrument)
            # Historical/high-turnover non-crypto, offline, spot and unknown
            # instruments must not consume the bounded live universe slots.
            for symbol,changes in (
                ('OFFLINEUSDT',{'status':'offline'}),
                ('RWAUSDT',{'base_coin':'RWA','raw_payload':dict(raw,symbolType='rwa')}),
                ('SPOTUSDT',{'category':'SPOT'}),
                ('UNKNOWNUSDT',{'raw_payload':dict(raw,symbolType='unknown')}),
            ):
                repo.upsert_instrument(replace(instrument,symbol=symbol,**changes))
                repo.insert_market_snapshot(replace(ticker,symbol=symbol,turnover24h=Decimal('999999999')),now)
            eligible=repo.load_latest_market_batch(limit=1)
            assert eligible.selected_symbols == ('BTCUSDT',)
            assert eligible.instruments == [instrument]
    finally:
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.mark.skipif(not DSN, reason="TEST_POSTGRES_DSN is not configured")
def test_full_market_spot_window_database_guard_preserves_legacy_scope(monkeypatch,tmp_path):
    import psycopg,json
    from psycopg import sql
    from quant_phase7.persistence import Phase7Repository
    from quant_phase7.spot_flow import SpotWindowResult,SpotFlowError
    from quant_phase7.contracts import DataStatus,MarketKind
    from dataclasses import replace
    from datetime import timedelta
    from quant_phase7.bitget_sbe import SOURCE,VERSION
    scope=tmp_path/'scope.json'
    scope.write_text(json.dumps(dict(catalog_checked_at='SYNTHETIC_CATALOG',
        perpetual_symbols=['SOLUSDT','OTHERUSDT'],spot_symbols=['SOLUSDT'])))
    monkeypatch.setenv('BITGET_SBE_FLOW_SCOPE_PATH',str(scope))
    now=datetime(2026,10,4,12,0,tzinfo=timezone.utc)
    row=SpotWindowResult(exchange='bitget',source_id=SOURCE,symbol='SOLUSDT',market_kind=MarketKind.SPOT,
        timeframe='5m',window_open=now,window_close=now+timedelta(minutes=5),aggregation_version=VERSION,
        base_volume=Decimal('2'),quote_volume=Decimal('4'),buy_volume=Decimal('1.5'),sell_volume=Decimal('.5'),
        unknown_volume=Decimal('0'),delta=Decimal('1'),cvd=None,trade_count=2,directional_trade_count=2,
        event_time_first=now,event_time_last=now+timedelta(seconds=2),cursor_first='1',cursor_last='2',
        sample_count=2,source_count=1,available_count=2,missing_count=0,coverage_ratio=Decimal('1'),
        status=DataStatus.AVAILABLE,reason='SYNTHETIC_TEST',source_reference='a'*64,normalization_version=VERSION)
    schema='full_spot_'+uuid4().hex
    with psycopg.connect(DSN,autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        with psycopg.connect(DSN,options=f'-c search_path={schema}') as conn:
            apply_migrations(conn)
            conn.commit()
            repo=Phase7Repository(conn)
            assert repo.upsert_windows('phase7_spot_flow_windows',[row.to_row(processed_at=now,created_at=now)])==1
            conn.commit()
            assert conn.execute('SELECT symbol FROM phase7_spot_flow_windows').fetchone()==('SOLUSDT',)
            for changes in ({'symbol':'OTHERUSDT'},{'exchange':'binance'},{'source_id':'UNKNOWN'},
                            {'aggregation_version':'unknown'},{'source_reference':'UNKNOWN'}):
                with pytest.raises(SpotFlowError):replace(row,**changes)
            for changes in ({'exchange':'binance'},{'source_reference':None},{'source_reference':'UNKNOWN'}):
                forged=row.to_row(processed_at=now,created_at=now)
                forged.update(changes)
                with pytest.raises(psycopg.errors.CheckViolation),conn.transaction():
                    conn.execute('INSERT INTO phase7_spot_flow_windows ('+','.join(forged)+') VALUES ('+
                        ','.join(['%s']*len(forged))+')',tuple(forged.values()))
    finally:
        with psycopg.connect(DSN,autocommit=True) as admin:
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
