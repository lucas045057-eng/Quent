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

from quant_phase1.db import apply_migrations
from quant_phase9.contracts import (
    EvidenceFreshnessV1,
    EvidenceQualityV1,
    PolicyCoverageStatusV1,
    PolicyDataStatusV1,
)
from quant_phase9.intake import build_stage1_candidate_event
from quant_phase1.stage1 import Stage1Result
from quant_phase1.contracts import DataStatus
from quant_phase9.sources.phase1 import select_phase1
from quant_phase9.sources.phase2 import select_phase2
from quant_phase9.sources.phase3 import select_phase3
from quant_phase9.sources.phase4 import select_phase4
from quant_phase9.sources.phase5 import select_phase5
from quant_phase9.sources.phase6 import select_phase6
from quant_phase9.sources.phase7 import select_phase7
from quant_phase9.sources.phase8 import select_phase8


AS_OF = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def database():
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for source projection integration tests")
    info = conninfo_to_dict(dsn)
    assert info.get("host") in {"127.0.0.1", "localhost", "::1"}
    assert info.get("dbname") == "quant_phase9_test"
    schema = f"phase9_source_test_{uuid4().hex}"
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    conn = psycopg.connect(dsn, options=f"-c search_path={schema},public")
    try:
        apply_migrations(conn)
        _insert_symbol(conn)
        conn.commit()
        yield conn
    finally:
        conn.close()
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema)))


def _candidate(symbol="BTCUSDT"):
    result = Stage1Result(
        symbol=symbol, category="A", reason="fixture", status=DataStatus.AVAILABLE,
        inputs_used=("price",), indicators={"atr": Decimal("1")}, structure="BULLISH",
        reason_codes=("STRUCTURE_ALIGNED",), timestamp=AS_OF,
    )
    return build_stage1_candidate_event(
        screening_result_id=91, run_id=22, screening_result=result,
        market="USDT_PERPETUAL",
        instrument_scope={
            "category": "USDT-FUTURES", "quote_coin": "USDT", "contract_type": "perpetual",
            "status": "online", "in_scope": "true",
        },
        candidate_created_at=AS_OF - timedelta(minutes=1),
        candidate_valid_until=AS_OF + timedelta(minutes=30),
        stage1_policy_version="phase1-basic-v1", source_as_of=AS_OF,
    )


def test_phase6_distinguishes_unconfigured_registry_from_missing_events(database):
    conn = database
    unconfigured = select_phase6(conn, _candidate(), "15m", AS_OF)
    marker = next(item for item in unconfigured if item.source_type == "SOURCE_REGISTRY")
    assert marker.availability_status is PolicyDataStatusV1.NOT_CONFIGURED
    assert marker.canonical_payload["configured"] is False

    conn.execute(
        """INSERT INTO phase6_source_registry
               (source_id,source_type,base_url,parser_version,policy_version,max_bytes,
                timeout_seconds,max_redirects,status,processed_at)
           VALUES ('fixture-news','PUBLIC_API','https://news.example.test','v1','p1',1024,5,0,
                   'AVAILABLE',%s)""",
        (AS_OF,),
    )
    conn.commit()
    configured = select_phase6(conn, _candidate(), "15m", AS_OF)
    marker = next(item for item in configured if item.source_type == "SOURCE_REGISTRY")
    assert marker.availability_status is PolicyDataStatusV1.AVAILABLE
    assert marker.canonical_payload["configured"] is True
    assert not any(item.canonical_payload.get("event_id") for item in configured)
    conn.execute(
        """INSERT INTO phase6_news_events
               (event_id,event_fingerprint,source,source_type,source_ref,observed_at,event_at,
                fetched_at,processed_at,event_type,headline,summary,importance,content_hash,
                parser_version,provenance,symbols,status)
           VALUES ('news-1','fingerprint-1','fixture','PUBLIC_API','news:1',%s,%s,%s,%s,
                   'LISTING','known event','bounded summary','MEDIUM',%s,'v1',%s,%s,'AVAILABLE')""",
        (AS_OF - timedelta(minutes=2), AS_OF - timedelta(minutes=3),
         AS_OF - timedelta(minutes=1), AS_OF, "c" * 64, Jsonb({"source_id":"fixture-news"}),
         Jsonb(["BTCUSDT"])),
    )
    conn.commit()
    event = next(item for item in select_phase6(conn, _candidate(), "15m", AS_OF)
                 if item.source_type == "NEWS_EVENT")
    assert event.event_time == AS_OF - timedelta(minutes=3)
    assert event.captured_at == AS_OF - timedelta(minutes=1)
    assert event.canonical_payload["headline"] == "known event"


def test_phase7_spot_projection_is_limited_to_btc_eth_candidate_scope(database):
    conn = database
    window_open = AS_OF - timedelta(minutes=15)
    conn.execute(
        """INSERT INTO phase7_asset_registry
               (asset_id,chain,asset_kind,contract_address,symbol,decimals,registry_version,
                effective_from,effective_to,source_id,source_version,source_reference,snapshot_hash,
                status,created_at,updated_at)
           VALUES ('BITCOIN:NATIVE:NATIVE:btc-v1','BITCOIN','NATIVE',NULL,'BTC',8,'btc-v1',
                   %s,NULL,'fixture','v1','fixture',%s,'AVAILABLE',%s,%s)""",
        (AS_OF - timedelta(days=365), "a" * 64, AS_OF, AS_OF),
    )
    conn.execute(
        """INSERT INTO phase7_onchain_flow_windows
               (chain,asset_id,timeframe,window_open,window_close,context_version,flow_domain,
                aggregation_scope,aggregation_eligible,inbound_amount,outbound_amount,net_amount,
                unknown_transfer_count,sample_count,source_count,available_count,missing_count,
                known_address_count,labeled_address_count,labeled_count,source_quality,coverage_status,
                coverage_ratio,label_coverage_ratio,status,reason,source_version,normalization_version,
                source_reference,processed_at,created_at)
           VALUES ('BITCOIN','BITCOIN:NATIVE:NATIVE:btc-v1','15m',%s,%s,'v1','GENERIC_ONCHAIN',
                   'GENERIC',TRUE,10,5,5,1,3,1,1,0,2,1,1,'fixture','PARTIAL',0.5,0.5,
                   'PARTIAL','partial coverage','v1','v1','fixture',%s,%s)""",
        (window_open, AS_OF, AS_OF, AS_OF),
    )
    conn.execute(
        """INSERT INTO phase7_spot_flow_windows
               (exchange,symbol,market_kind,timeframe,window_open,window_close,aggregation_version,
                base_volume,quote_volume,unknown_volume,trade_count,directional_trade_count,
                sample_count,source_count,available_count,missing_count,coverage_ratio,status,reason,
                normalization_version,processed_at,created_at)
           VALUES ('binance','BTCUSDT','SPOT','15m',%s,%s,'v1',1,100,1,4,2,4,1,1,0,1,
                   'PARTIAL','unknown side','v1',%s,%s)""",
        (window_open, AS_OF, AS_OF, AS_OF),
    )
    conn.commit()

    btc = select_phase7(conn, _candidate(), "15m", AS_OF)
    assert any(item.source_type == "SPOT_FLOW_WINDOW" for item in btc)
    onchain = next(item for item in btc if item.source_type == "ONCHAIN_FLOW_WINDOW")
    assert onchain.canonical_payload["chain"] == "BITCOIN"
    assert onchain.canonical_payload["asset_id"] == "BITCOIN:NATIVE:NATIVE:btc-v1"
    assert onchain.coverage_status is PolicyCoverageStatusV1.PARTIAL
    alt = select_phase7(conn, _candidate("XRPUSDT"), "15m", AS_OF)
    assert not any(item.source_type == "SPOT_FLOW_WINDOW" for item in alt)
    eth = select_phase7(conn, _candidate("ETHUSDT"), "15m", AS_OF)
    assert not any(item.canonical_payload.get("chain") == "BITCOIN" for item in eth)


def test_phase5_keeps_context_and_input_window_clocks(database):
    conn = database
    _insert_symbol(conn)
    context_time = AS_OF - timedelta(minutes=5)
    window_start = context_time - timedelta(minutes=15)
    conn.execute(
        """INSERT INTO phase5_market_leader_context
               (symbol,timeframe,context_timestamp,input_window_start,input_window_end,return_pct,
                trend_state,structure_state,volatility_state,volume_state,volatility_value,
                volume_ratio,freshness_status,data_quality,source_count,missing_count,
                support_evidence,conflict_evidence,missing_evidence,status,reason_code,
                calculation_version,input_reference,processed_at)
           VALUES ('BTCUSDT','15m',%s,%s,%s,1.25,'TREND_UP','HIGHER_HIGH_HIGHER_LOW',
                   'NORMAL','NORMAL',0.5,1.1,'AVAILABLE',%s,2,0,'[]','[]','[]','AVAILABLE',
                   NULL,'v1',%s,%s)""",
        (context_time, window_start, context_time, Jsonb({"quality":"confirmed"}),
         Jsonb({"kline_ids":[1,2]}), AS_OF),
    )
    conn.commit()

    leader = next(item for item in select_phase5(conn, _candidate(), "15m", AS_OF)
                  if item.source_type == "MARKET_LEADER_CONTEXT")
    assert leader.event_time == context_time
    assert leader.canonical_payload["input_window_start"] == window_start
    assert leader.canonical_payload["input_window_end"] == context_time
    assert leader.canonical_payload["return_pct"] == Decimal("1.25")
    assert leader.freshness_status is EvidenceFreshnessV1.FRESH


def test_phase8_keeps_metric_event_times_and_does_not_promote_unknown_iv_unit(database):
    conn = database
    old_metric_time = AS_OF - timedelta(hours=2)
    conn.execute(
        """INSERT INTO phase8_option_market_snapshots
               (exchange,source,symbol,underlying,observation_kind,exchange_timestamp,
                timestamp_semantics,fetched_at,processed_at,status,schema_version,metrics,
                field_metadata,payload_hash)
           VALUES ('deribit','fixture','BTC-30SEP26-100000-C','BTC','REST_CHAIN_SUMMARY',%s,
                   'VERIFIED',%s,%s,'AVAILABLE','v1',%s,%s,%s)""",
        (
            AS_OF, AS_OF, AS_OF,
            Jsonb({"open_interest": "12.5", "volume_24h": "3.25", "mark_iv": "0.42"}),
            Jsonb({
                "open_interest": {"source_field": "open_interest", "status": "AVAILABLE",
                                  "unit_code": "CONTRACTS", "unit_status": "CONFIRMED",
                                  "field_last_updated_at": old_metric_time.isoformat(),
                                  "provenance": "SOURCE_PROVIDED"},
                "volume_24h": {"source_field": "volume_24h", "status": "AVAILABLE",
                               "unit_code": "CONTRACTS", "unit_status": "CONFIRMED",
                               "field_last_updated_at": old_metric_time.isoformat(),
                               "provenance": "SOURCE_PROVIDED"},
                "mark_iv": {"source_field": "mark_iv", "status": "AVAILABLE",
                            "unit_code": None, "unit_status": "SOURCE_NATIVE_UNVERIFIED",
                            "field_last_updated_at": AS_OF.isoformat(),
                            "provenance": "SOURCE_PROVIDED"},
            }),
            "a" * 64,
        ),
    )
    conn.commit()

    projections = select_phase8(conn, _candidate(), "15m", AS_OF)
    market = next(item for item in projections if item.source_type == "OPTION_MARKET_SNAPSHOT")
    metrics = market.canonical_payload["metrics"]
    assert metrics["open_interest"]["source_timestamp"] == old_metric_time
    assert metrics["volume_24h"]["source_timestamp"] == old_metric_time
    assert metrics["mark_iv"]["value"] == Decimal("0.42")
    assert metrics["mark_iv"]["availability_status"] == "NOT_AVAILABLE"
    assert metrics["mark_iv"]["unit_status"] == "SOURCE_NATIVE_UNVERIFIED"
    assert market.event_time == AS_OF
    assert select_phase8(conn, _candidate("XRPUSDT"), "15m", AS_OF) == ()


def test_phase8_context_clock_does_not_refresh_metric_timestamp_or_unit(database):
    conn = database
    old_metric_time = AS_OF - timedelta(hours=3)
    conn.execute(
        """INSERT INTO phase8_option_context_snapshots
               (underlying,context_timestamp,processed_at,calculation_version,metrics,
                source_timestamps,source_capture_times,coverage,provenance,input_observation_ids,
                status,reason_code,unit_contract_version,context_only)
           VALUES ('BTC',%s,%s,'v1',%s,%s,%s,%s,%s,'[]','PARTIAL','UNIT_UNVERIFIED','u1',TRUE)""",
        (AS_OF, AS_OF,
         Jsonb({"atm_iv":{"value":"0.42","status":"AVAILABLE","unit_code":None,
                          "unit_status":"SOURCE_NATIVE_UNVERIFIED","data_age_seconds":10800,
                          "coverage_expected":10,"coverage_available":8}}),
         Jsonb({"atm_iv":[old_metric_time.isoformat()]}),
         Jsonb({"atm_iv":{"received_at":AS_OF.isoformat()}}),
         Jsonb({"atm_iv":{"expected":10,"available":8,"ratio":"0.8"}}),
         Jsonb({"atm_iv":"SOURCE_PROVIDED"})),
    )
    conn.commit()

    context = next(item for item in select_phase8(conn, _candidate(), "15m", AS_OF)
                   if item.source_type == "OPTION_CONTEXT_SNAPSHOT")
    metric = context.canonical_payload["metrics"]["atm_iv"]
    assert context.event_time is None
    assert context.observed_at == AS_OF
    assert metric["source_timestamp"] == old_metric_time
    assert metric["data_age_seconds"] == Decimal("10800")
    assert metric["availability_status"] == "NOT_AVAILABLE"


def test_phase8_source_category_overflow_fails_closed_without_truncation(database):
    conn = database
    for index in range(17):
        row_time = AS_OF - timedelta(seconds=index)
        conn.execute(
            """INSERT INTO phase8_option_market_snapshots
                   (exchange,source,symbol,underlying,observation_kind,exchange_timestamp,
                    timestamp_semantics,fetched_at,processed_at,status,schema_version,metrics,
                    field_metadata,payload_hash)
               VALUES ('fixture','recorded',%s,'BTC','REST_CHAIN_SUMMARY',%s,'VERIFIED',%s,%s,
                       'AVAILABLE','v1','{}','{}',%s)""",
            (f"BTC-30SEP26-{100000 + index}-C", row_time, AS_OF, AS_OF, f"{index:064x}"),
        )
    conn.commit()
    with pytest.raises(ValueError, match="bounded row limit"):
        select_phase8(conn, _candidate(), "15m", AS_OF)


def _insert_symbol(conn) -> None:
    conn.execute(
        """INSERT INTO symbols (
               symbol, category, base_coin, quote_coin, symbol_type, contract_type, status,
               price_precision, quantity_precision, min_order_qty, source, exchange, fetched_at
           ) VALUES ('BTCUSDT','USDT-FUTURES','BTC','USDT','PERPETUAL','perpetual','online',
                     2,3,0.001,'fixture','bitget',%s)
           ON CONFLICT (symbol) DO NOTHING""",
        (AS_OF,),
    )


def test_phase1_uses_exchange_time_and_only_expected_closed_bars(database):
    conn = database
    _insert_symbol(conn)
    conn.execute(
        """INSERT INTO market_snapshots (
               symbol,snapshot_timestamp,source,exchange,exchange_timestamp,fetched_at,
               processed_at,status,snapshot
           ) VALUES ('BTCUSDT',%s,'fixture','bitget',NULL,%s,%s,'AVAILABLE',%s)""",
        (AS_OF, AS_OF, AS_OF, Jsonb({"last_price": "100", "volume24h": "3"})),
    )
    conn.execute(
        """INSERT INTO market_observations (
               symbol,metric,value,unit,source,exchange,exchange_timestamp,fetched_at,
               processed_at,status
           ) VALUES ('BTCUSDT','last_price',100,'USD','fixture','bitget',NULL,%s,%s,'AVAILABLE')""",
        (AS_OF, AS_OF),
    )
    conn.execute(
        """INSERT INTO klines (
               symbol,interval,bar_open_timestamp,open,high,low,close,volume,turnover,
               exchange_timestamp,fetched_at,processed_at,status,source,exchange
           ) VALUES
           ('BTCUSDT','5m',%s,99,101,98,100,4,400,%s,%s,%s,'AVAILABLE','fixture','bitget'),
           ('BTCUSDT','5m',%s,100,102,99,101,5,505,%s,%s,%s,'AVAILABLE','fixture','bitget')""",
        (
            AS_OF - timedelta(minutes=5), AS_OF - timedelta(minutes=5), AS_OF, AS_OF,
            AS_OF, AS_OF, AS_OF, AS_OF,
        ),
    )
    conn.commit()

    projections = select_phase1(conn, _candidate(), "15m", AS_OF)
    ticker = next(item for item in projections if item.source_type == "PRICE_TICKER")
    observation = next(item for item in projections if item.source_type == "PRICE_OBSERVATION")
    five_minute = next(item for item in projections if item.source_type == "CLOSED_KLINE" and item.canonical_payload.get("interval") == "5m" and item.event_time is not None)
    missing_fifteen = next(item for item in projections if item.source_type == "CLOSED_KLINE" and item.canonical_payload.get("interval") == "15m")

    assert ticker.event_time is None
    assert ticker.freshness_status is EvidenceFreshnessV1.UNKNOWN
    assert ticker.canonical_payload["snapshot_timestamp"] == AS_OF
    assert observation.event_time is None
    assert observation.canonical_payload["exchange_timestamp"] is None
    assert five_minute.event_time == AS_OF - timedelta(minutes=5)
    assert five_minute.freshness_status is EvidenceFreshnessV1.FRESH
    assert five_minute.canonical_payload["close"] == Decimal("100")
    assert missing_fifteen.availability_status is PolicyDataStatusV1.NOT_AVAILABLE
    assert missing_fifteen.canonical_payload["close"] is None
    assert all(item.canonical_payload.get("close") != Decimal("101") for item in projections)


def test_phase2_is_bounded_and_never_substitutes_fetch_time_for_source_time(database):
    conn = database
    _insert_symbol(conn)
    for index in range(20):
        exchange_time = None if index == 0 else AS_OF - timedelta(minutes=index)
        conn.execute(
            """INSERT INTO open_interest (
                   symbol,canonical_symbol,exchange,contract_type,raw_unit,raw_open_interest,
                   open_interest_usd,normalization_method,exchange_timestamp,fetched_at,
                   processed_at,status,source_endpoint,observation_key,raw_payload
               ) VALUES (%s,'BTC-USDT-PERP','bitget','PERPETUAL','CONTRACTS',%s,%s,'VERIFIED',%s,%s,%s,
                         'AVAILABLE','fixture',%s,'{}')""",
            ("BTCUSDT", Decimal(index + 1), Decimal((index + 1) * 100), exchange_time,
             AS_OF, AS_OF, f"oi-{index}"),
        )
    conn.execute(
        """INSERT INTO open_interest (
               symbol,canonical_symbol,exchange,contract_type,raw_unit,exchange_timestamp,
               fetched_at,processed_at,status,source_endpoint,observation_key,raw_payload
           ) VALUES ('BTCUSDT','BTC-USDT-PERP','bitget','PERPETUAL','CONTRACTS',%s,%s,%s,
                    'AVAILABLE','fixture','future','{}')""",
        (AS_OF + timedelta(seconds=1), AS_OF, AS_OF),
    )
    conn.commit()

    projections = select_phase2(conn, _candidate(), "15m", AS_OF)
    oi = [item for item in projections if item.source_type == "OPEN_INTEREST"]
    missing_time = next(item for item in oi if item.event_time is None)

    assert len(oi) <= 16
    assert missing_time.captured_at == AS_OF
    assert missing_time.freshness_status is EvidenceFreshnessV1.UNKNOWN
    assert missing_time.canonical_payload["exchange_timestamp"] is None
    assert all(item.event_time is None or item.event_time <= AS_OF for item in oi)
    assert all(item.canonical_payload.get("observation_key") != "future" for item in oi)


def test_phase3_preserves_confirmed_and_unknown_flow_without_invented_direction(database):
    conn = database
    conn.execute(
        """INSERT INTO trade_flow_windows (
               exchange,canonical_symbol,timeframe,window_open,window_close,total_trade_count,
               buy_trade_count,sell_trade_count,unknown_trade_count,total_volume_base,
               buy_volume_base,sell_volume_base,unknown_volume_base,average_trade_size,
               trade_frequency,delta_base,delta_ratio,first_trade_at,last_trade_at,freshness,
               status,processed_at
           ) VALUES ('bitget','BTC-USDT-PERP','15m',%s,%s,10,4,3,3,20,8,6,6,2,0.1,2,0.1,
                     %s,%s,'AVAILABLE','PARTIAL',%s)""",
        (AS_OF - timedelta(minutes=15), AS_OF, AS_OF - timedelta(minutes=14),
         AS_OF - timedelta(seconds=1), AS_OF),
    )
    conn.commit()

    projections = select_phase3(conn, _candidate(), "15m", AS_OF)
    flow = next(item for item in projections if item.source_type == "TRADE_FLOW_WINDOW")

    assert flow.availability_status is PolicyDataStatusV1.PARTIAL
    assert flow.freshness_status is EvidenceFreshnessV1.FRESH
    assert flow.canonical_payload["buy_volume_base"] == Decimal("8")
    assert flow.canonical_payload["sell_volume_base"] == Decimal("6")
    assert flow.canonical_payload["unknown_volume_base"] == Decimal("6")
    assert "direction" not in flow.canonical_payload


def test_phase4_without_gap_health_remains_unknown(database):
    conn = database
    projections = select_phase4(conn, _candidate(), "15m", AS_OF)
    assert projections == ()


def test_phase4_health_preserves_partial_quality_without_claiming_a_gap(database):
    conn = database
    conn.execute(
        """INSERT INTO system_health (component, status, checked_at, details)
           VALUES ('phase4-liquidation', 'AVAILABLE', %s, %s)""",
        (AS_OF, Jsonb({
            "gap_detected": False, "gap_count": 0,
            "phase4_status": "PARTIAL", "data_quality": "PARTIAL",
        })),
    )
    conn.commit()

    health = next(
        item for item in select_phase4(conn, _candidate(), "15m", AS_OF)
        if item.source_type == "LIQUIDATION_HEALTH"
    )

    assert health.availability_status is PolicyDataStatusV1.AVAILABLE
    assert health.coverage_status is PolicyCoverageStatusV1.PARTIAL
    assert health.quality_status is EvidenceQualityV1.PARTIAL
    assert health.canonical_payload["gap_detected"] is False
