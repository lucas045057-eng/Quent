from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from quant_phase1.contracts import DataStatus
from quant_phase1.db import apply_migrations
from quant_phase1.stage1 import Stage1Result
from quant_phase9.contracts import EvaluationIdentityV1
from quant_phase9.intake import admit_event, build_stage1_candidate_event, Phase9OutboxWriter
from quant_phase9.snapshot import build_snapshot, persist_evaluation_snapshot
from quant_phase9.sources import CorePhase9SourceReader


AS_OF = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def database():
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for snapshot replay tests")
    info = conninfo_to_dict(dsn)
    assert info.get("host") in {"127.0.0.1", "localhost", "::1"}
    assert info.get("dbname") == "quant_phase9_test"
    schema = f"phase9_replay_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    conn = psycopg.connect(dsn, options=f"-c search_path={schema},public")
    try:
        apply_migrations(conn)
        conn.commit()
        yield conn
    finally:
        conn.close()
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def _candidate():
    result = Stage1Result(
        symbol="BTCUSDT", category="A", reason="fixture", status=DataStatus.AVAILABLE,
        inputs_used=("price",), indicators={"atr": Decimal("1")}, structure="BULLISH",
        reason_codes=("STRUCTURE_ALIGNED",), timestamp=AS_OF,
    )
    return build_stage1_candidate_event(
        screening_result_id=1301, run_id=23, screening_result=result,
        market="USDT_PERPETUAL",
        instrument_scope={
            "category": "USDT-FUTURES", "quote_coin": "USDT", "contract_type": "perpetual",
            "status": "online", "in_scope": "true",
        },
        candidate_created_at=AS_OF - timedelta(minutes=1),
        candidate_valid_until=AS_OF + timedelta(minutes=30),
        stage1_policy_version="phase1-basic-v1", source_as_of=AS_OF,
    )


def test_replay_reads_immutable_snapshot_after_source_mutation_and_deletion(database):
    conn = database
    candidate = _candidate()
    manifest_version = "1.0.0"
    manifest_digest = "d" * 64
    identity = EvaluationIdentityV1(
        stage1_candidate_id=candidate.stage1_candidate_id,
        market=candidate.market,
        symbol=candidate.symbol,
        timeframe="15m",
        evaluation_window_start=AS_OF - timedelta(minutes=15),
        evaluation_window_end=AS_OF,
        policy_generation=f"{manifest_version}:{manifest_digest}",
        material_change_generation="0",
    )
    Phase9OutboxWriter().emit(conn, event=candidate)
    assert admit_event(conn, event=candidate, identity=identity, now=AS_OF).value == "ADMITTED"
    conn.execute(
        """INSERT INTO symbols (
               symbol,category,base_coin,quote_coin,symbol_type,contract_type,status,
               price_precision,quantity_precision,min_order_qty,source,exchange,fetched_at
           ) VALUES ('BTCUSDT','USDT-FUTURES','BTC','USDT','PERPETUAL','perpetual','online',
                     2,3,0.001,'fixture','bitget',%s)""",
        (AS_OF,),
    )
    conn.execute(
        """INSERT INTO market_snapshots (
               symbol,snapshot_timestamp,source,exchange,exchange_timestamp,fetched_at,
               processed_at,status,snapshot
           ) VALUES ('BTCUSDT',%s,'fixture','bitget',%s,%s,%s,'AVAILABLE',%s)""",
        (AS_OF, AS_OF - timedelta(seconds=1), AS_OF, AS_OF, Jsonb({"last_price": "100"})),
    )
    conn.execute(
        """INSERT INTO klines (
               symbol,interval,bar_open_timestamp,open,high,low,close,volume,turnover,
               exchange_timestamp,fetched_at,processed_at,status,source,exchange
           ) VALUES ('BTCUSDT','5m',%s,99,101,98,100,4,400,%s,%s,%s,
                    'AVAILABLE','fixture','bitget')""",
        (AS_OF - timedelta(minutes=5), AS_OF - timedelta(minutes=5), AS_OF, AS_OF),
    )
    conn.execute(
        """INSERT INTO phase8_option_market_snapshots
               (exchange,source,symbol,underlying,observation_kind,exchange_timestamp,
                timestamp_semantics,fetched_at,processed_at,status,schema_version,metrics,
                field_metadata,payload_hash)
           VALUES ('fixture','recorded','BTC-30SEP26-100000-C','BTC','REST_CHAIN_SUMMARY',
                   %s,'VERIFIED',%s,%s,'AVAILABLE','v1',%s,%s,%s)""",
        (AS_OF - timedelta(seconds=1), AS_OF, AS_OF,
         Jsonb({"mark_price":"100"}),
         Jsonb({"mark_price":{"source_field":"mark_price","status":"AVAILABLE",
                              "unit_code":"BTC","unit_status":"VERIFIED",
                              "field_last_updated_at":(AS_OF-timedelta(seconds=1)).isoformat(),
                              "provenance":"SOURCE_PROVIDED"}}),
         "f" * 64),
    )
    conn.commit()

    conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
    snapshot = build_snapshot(
        conn,
        identity=identity,
        stage1_candidate=candidate,
        as_of=AS_OF,
        source_reader=CorePhase9SourceReader(),
        policy_manifest=SimpleNamespace(manifest_version=manifest_version, manifest_digest=manifest_digest),
        code_version="e" * 40,
    )
    persist_evaluation_snapshot(conn, snapshot=snapshot)
    conn.commit()
    frozen = conn.execute(
        "SELECT payload FROM phase9_evaluation_snapshots WHERE evaluation_id=%s",
        (snapshot.evaluation_id,),
    ).fetchone()[0]
    ticker_before = next(
        item for item in snapshot.source_projections if item.source_type == "PRICE_TICKER"
    )
    assert ticker_before.canonical_payload["last_price"] == Decimal("100")
    assert any(item.source_type == "OPTION_MARKET_SNAPSHOT" for item in snapshot.source_projections)

    conn.execute(
        "UPDATE market_snapshots SET snapshot=%s WHERE symbol='BTCUSDT'",
        (Jsonb({"last_price": "999"}),),
    )
    conn.execute("DELETE FROM klines WHERE symbol='BTCUSDT' AND interval='5m'")
    conn.execute("DELETE FROM phase8_option_market_snapshots WHERE underlying='BTC'")
    conn.commit()

    conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
    rebuilt = build_snapshot(
        conn,
        identity=identity,
        stage1_candidate=candidate,
        as_of=AS_OF,
        source_reader=CorePhase9SourceReader(),
        policy_manifest=SimpleNamespace(manifest_version=manifest_version, manifest_digest=manifest_digest),
        code_version="e" * 40,
    )
    conn.rollback()
    ticker_after = next(
        item for item in rebuilt.source_projections if item.source_type == "PRICE_TICKER"
    )
    assert ticker_after.canonical_payload["last_price"] == Decimal("999")
    assert not any(item.source_type == "OPTION_MARKET_SNAPSHOT" for item in rebuilt.source_projections)
    assert rebuilt.snapshot_digest != snapshot.snapshot_digest
    assert conn.execute(
        "SELECT payload FROM phase9_evaluation_snapshots WHERE evaluation_id=%s",
        (snapshot.evaluation_id,),
    ).fetchone()[0] == frozen
