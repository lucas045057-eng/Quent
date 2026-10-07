from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

from quant_phase1.contracts import DataStatus
from quant_phase1.db import apply_migrations
from quant_phase1.stage1 import Stage1Result
from quant_instruments import resolve_core_instrument
from quant_phase9.intake import build_stage1_candidate_event
from quant_phase9.sources.phase2 import select_phase2
from quant_phase9.sources.phase3 import select_phase3
from quant_phase9.sources.phase4 import select_phase4


AS_OF = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
CANONICAL = "BTC-USDT-PERP"


@pytest.fixture
def database():
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip('TEST_POSTGRES_DSN is required for isolated database tests')
    info = conninfo_to_dict(dsn)
    assert info.get("host") in {"127.0.0.1", "localhost", "::1"}
    assert info.get("dbname") == "quant_phase9_test"
    schema = f"core_symbol_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    conn = psycopg.connect(dsn, options=f"-c search_path={schema},public")
    try:
        apply_migrations(conn)
        conn.execute(
            """INSERT INTO symbols (
                 symbol, category, base_coin, quote_coin, symbol_type, contract_type, status,
                 price_precision, quantity_precision, min_order_qty, source, exchange, fetched_at
               ) VALUES ('BTCUSDT','USDT-FUTURES','BTC','USDT','PERPETUAL','perpetual',
                         'online',2,3,0.001,'fixture','bitget',%s)""",
            (AS_OF - timedelta(minutes=2),),
        )
        conn.commit()
        yield conn
    finally:
        conn.close()
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def candidate(symbol: str = "BTCUSDT"):
    result = Stage1Result(
        symbol=symbol, category="A", reason="fixture", status=DataStatus.AVAILABLE,
        inputs_used=("price",), indicators={"atr": Decimal("1")}, structure="BULLISH",
        reason_codes=("STRUCTURE_ALIGNED",), timestamp=AS_OF,
    )
    return build_stage1_candidate_event(
        screening_result_id=91, run_id=22, screening_result=result,
        market="USDT_PERPETUAL",
        instrument_scope={
            "category": "USDT-FUTURES", "quote_coin": "USDT",
            "contract_type": "perpetual", "status": "online", "in_scope": "true",
        },
        candidate_created_at=AS_OF - timedelta(minutes=1),
        candidate_valid_until=AS_OF + timedelta(minutes=30),
        stage1_policy_version="phase1-basic-v1", source_as_of=AS_OF,
    )


def test_phase2_aggregate_uses_registered_canonical_identity(database):
    database.execute(
        """INSERT INTO cross_exchange_derivative_snapshots
           (canonical_symbol,snapshot_timestamp,status,oi_exchange_count,
            funding_exchange_count,oi_total_usd,snapshot)
           VALUES (%s,%s,'AVAILABLE',1,1,1000,%s)""",
        (CANONICAL, AS_OF - timedelta(minutes=1), Jsonb({})),
    )
    database.commit()
    rows = select_phase2(database, candidate(), "15m", AS_OF)
    summary = [row for row in rows if row.source_type == "DERIVATIVE_SUMMARY"]
    assert len(summary) == 1
    assert summary[0].canonical_payload["canonical_symbol"] == CANONICAL
    assert summary[0].symbol == "BTCUSDT"


def test_phase3_flow_uses_same_identity_as_real_producer(database):
    database.execute(
        """INSERT INTO trade_flow_windows (
             exchange,canonical_symbol,timeframe,window_open,window_close,total_trade_count,
             buy_trade_count,sell_trade_count,unknown_trade_count,total_volume_base,
             buy_volume_base,sell_volume_base,unknown_volume_base,average_trade_size,
             trade_frequency,delta_base,delta_ratio,first_trade_at,last_trade_at,freshness,
             status,processed_at
           ) VALUES ('bybit',%s,'15m',%s,%s,10,6,4,0,20,12,8,0,2,0.1,4,0.2,
                     %s,%s,'AVAILABLE','AVAILABLE',%s)""",
        (CANONICAL, AS_OF - timedelta(minutes=15), AS_OF,
         AS_OF - timedelta(minutes=14), AS_OF - timedelta(seconds=1), AS_OF),
    )
    database.commit()
    rows = select_phase3(database, candidate(), "15m", AS_OF)
    flow = [row for row in rows if row.source_type == "TRADE_FLOW_WINDOW"]
    assert len(flow) == 1
    assert flow[0].canonical_payload["canonical_symbol"] == CANONICAL
    assert flow[0].symbol == "BTCUSDT"


def test_phase4_liquidation_uses_registered_canonical_identity(database):
    database.execute(
        """INSERT INTO liquidation_windows
           (exchange,canonical_symbol,timeframe,window_open,window_close,event_count,
            convertible_notional_usd,source_exchange_count,source_granularity,
            coverage_semantics,status,processed_at)
           VALUES ('bitget',%s,'15m',%s,%s,1,100,1,
                   'AGGREGATED_MAX_PER_SECOND','PARTIAL_AGGREGATED','AVAILABLE',%s)""",
        (CANONICAL, AS_OF - timedelta(minutes=15), AS_OF, AS_OF),
    )
    database.commit()
    rows = select_phase4(database, candidate(), "15m", AS_OF)
    windows = [row for row in rows if row.source_type == "LIQUIDATION_WINDOW"]
    assert len(windows) == 1
    assert CANONICAL in windows[0].source_ref
    assert windows[0].symbol == "BTCUSDT"


def test_unregistered_candidate_fails_closed(database):
    with pytest.raises(ValueError, match="symbol|instrument|canonical"):
        select_phase3(database, candidate("ETHUSDT"), "15m", AS_OF)


def test_registered_bitget_spot_identity_is_rejected_by_phase9(database):
    database.execute(
        "UPDATE symbols SET category='SPOT', symbol_type='SPOT', contract_type='spot' WHERE symbol='BTCUSDT'"
    )

    with pytest.raises(ValueError, match="eligible USDT perpetual"):
        resolve_core_instrument(database, "BTCUSDT")


def test_normal_bybit_oi_conversion_is_valid_quality(database):
    from quant_phase2.adapters.bybit import BybitV5Adapter
    from quant_phase2.persistence import Phase2Repository
    from quant_phase2.symbols import registry_from_phase1_symbols
    from quant_phase9.contracts import EvidenceQualityV1, PolicyDataStatusV1

    registry = registry_from_phase1_symbols([{
        "symbol": "BTCUSDT", "base_coin": "BTC", "quote_coin": "USDT",
        "status": "online", "contract_type": "perpetual",
    }])
    adapter = BybitV5Adapter(registry=registry)
    observed_at = AS_OF - timedelta(minutes=1)
    oi, _ = adapter.parse_ticker(
        {"retCode": 0, "time": int(observed_at.timestamp() * 1000),
         "result": {"list": [{
             "markPrice": "100", "openInterest": "10",
             "fundingRate": "0.001",
         }]}},
        observed_at, symbol="BTCUSDT", interval_seconds=28800,
    )
    assert oi.normalization_method == "base_quantity_times_mark_price"
    assert oi.open_interest_usd == Decimal("1000")
    Phase2Repository(database).insert_open_interest((oi,))
    database.commit()
    rows = select_phase2(database, candidate(), "15m", AS_OF)
    observed = [row for row in rows if row.source_type == "OPEN_INTEREST"]
    assert len(observed) == 1
    assert observed[0].availability_status is PolicyDataStatusV1.AVAILABLE
    assert observed[0].quality_status is EvidenceQualityV1.VALID


def test_unrecognized_oi_method_stays_partial(database):
    from quant_phase9.contracts import EvidenceQualityV1
    observed_at = AS_OF - timedelta(minutes=1)
    database.execute(
        """INSERT INTO open_interest (
             symbol,canonical_symbol,exchange,contract_type,raw_open_interest,
             raw_unit,open_interest_base,open_interest_quote,open_interest_usd,
             mark_price,normalization_method,exchange_timestamp,fetched_at,
             processed_at,status,source_endpoint,observation_key,raw_payload
           ) VALUES ('BTCUSDT',%s,'bybit','PERPETUAL',10,'BASE_ASSET',10,1000,1000,
                     100,'UNKNOWN_METHOD',%s,%s,%s,'AVAILABLE','fixture',
                     'unknown-method',%s)""",
        (CANONICAL, observed_at, observed_at, observed_at, Jsonb({})),
    )
    database.commit()
    rows = select_phase2(database, candidate(), "15m", AS_OF)
    observed = [row for row in rows if row.source_type == "OPEN_INTEREST"]
    assert len(observed) == 1
    assert observed[0].quality_status is EvidenceQualityV1.PARTIAL

def test_identity_exposes_registered_venue_without_nautilus_types(database):
    from quant_instruments import resolve_core_instrument
    database.execute(
        """INSERT INTO exchange_instruments
           (exchange,exchange_symbol,canonical_symbol,contract_type,source_endpoint,
            fetched_at,status,raw_payload)
           VALUES ('bybit','BTCUSDT',%s,'PERPETUAL','fixture',%s,'AVAILABLE',%s)""",
        (CANONICAL, AS_OF, Jsonb({})),
    )
    database.commit()
    identity = resolve_core_instrument(database, "BTCUSDT")
    assert identity.core_symbol == "BTCUSDT"
    assert identity.canonical_symbol == CANONICAL
    assert identity.venue_symbol("bybit") == "BTCUSDT"
    assert identity.venue_symbol("bitget") == "BTCUSDT"
    with pytest.raises(ValueError, match="venue"):
        identity.venue_symbol("unknown")


def test_offline_instrument_is_not_eligible(database):
    from quant_instruments import resolve_core_instrument
    database.execute("UPDATE symbols SET status = 'offline' WHERE symbol = 'BTCUSDT'")
    database.commit()
    with pytest.raises(ValueError, match="eligible"):
        resolve_core_instrument(database, "BTCUSDT")
