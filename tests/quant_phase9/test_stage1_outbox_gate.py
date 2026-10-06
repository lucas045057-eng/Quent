from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from quant_phase1.contracts import DataStatus, Instrument
from quant_phase1.db import apply_migrations
from quant_phase1.repositories import Phase1Repository
from quant_phase1.stage1 import Stage1Result
from quant_phase9.intake import persist_stage1_candidate_with_event


NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def connection():
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for Stage 1 outbox integration tests")

    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    info = conninfo_to_dict(dsn)
    assert info.get("host") in {"127.0.0.1", "localhost", "::1"}, "test DSN must use loopback"
    assert info.get("dbname") == "quant_phase9_test", "test requires disposable database"
    schema = f"phase9_stage1_test_{uuid4().hex}"
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


def _instrument(
    symbol: str,
    *,
    category: str = "USDT-FUTURES",
    quote_coin: str = "USDT",
    contract_type: str = "perpetual",
    status: str = "online",
) -> Instrument:
    return Instrument(
        symbol, category, symbol.removesuffix(quote_coin), quote_coin, "crypto", contract_type,
        status, 2, 3, Decimal("0.001"), None, NOW, {},
    )


def _result(symbol: str, category: str, status: DataStatus = DataStatus.AVAILABLE, *, detail: str = "") -> Stage1Result:
    return Stage1Result(
        symbol=symbol,
        category=category,
        reason="fixture result",
        status=status,
        inputs_used=("price", "closed_5m"),
        indicators={"atr": Decimal("1.25")},
        structure="BULLISH",
        reason_codes=("STRUCTURE_ALIGNED",),
        key_metrics={"detail": detail},
        timestamp=NOW,
    )


def _persist(connection, run_id: int, result: Stage1Result, *, policy_version="phase1-basic-v1") -> int:
    return persist_stage1_candidate_with_event(
        connection,
        run_id=run_id,
        screening_result=result,
        candidate_created_at=NOW,
        candidate_valid_until=None,
        stage1_policy_version=policy_version,
        source_as_of=NOW,
    )


def test_ab_events_preserve_category_and_actual_status_while_cd_remain_phase1_only(connection):
    repo = Phase1Repository(connection)
    symbols = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT")
    repo.upsert_instruments([_instrument(symbol) for symbol in symbols])
    run_id = repo.create_screening_run(NOW)

    _persist(connection, run_id, _result("BTCUSDT", "A", DataStatus.AVAILABLE))
    _persist(connection, run_id, _result("ETHUSDT", "B", DataStatus.STALE))
    _persist(connection, run_id, _result("XRPUSDT", "C", DataStatus.AVAILABLE))
    _persist(connection, run_id, _result("SOLUSDT", "D", DataStatus.ERROR))
    connection.commit()

    events = connection.execute(
        "SELECT payload FROM outbox_events WHERE event_id IS NOT NULL ORDER BY event_id"
    ).fetchall()
    by_symbol = {row[0]["canonical_payload"]["stage1_projection"]["symbol"]: row[0] for row in events}
    assert set(by_symbol) == {"BTCUSDT", "ETHUSDT"}
    assert by_symbol["BTCUSDT"]["canonical_payload"]["stage1_projection"]["category"] == "A"
    assert by_symbol["BTCUSDT"]["canonical_payload"]["stage1_projection"]["status"] == "AVAILABLE"
    assert by_symbol["ETHUSDT"]["canonical_payload"]["stage1_projection"]["category"] == "B"
    assert by_symbol["ETHUSDT"]["canonical_payload"]["stage1_projection"]["status"] == "STALE"


def test_v2_b_stays_persisted_as_observation_and_only_a_emits_research_event(connection):
    from dataclasses import replace
    repo = Phase1Repository(connection)
    repo.upsert_instruments([_instrument("BTCUSDT"), _instrument("ETHUSDT")])
    run_id = repo.create_screening_run(NOW)
    for symbol, category in (("BTCUSDT", "A"), ("ETHUSDT", "B")):
        result = replace(_result(symbol, category), strategy_version="QUANT_PAPER_V2")
        _persist(connection, run_id, result, policy_version="QUANT_PAPER_V2")
    connection.commit()
    assert connection.execute("SELECT count(*) FROM screening_results WHERE run_id=%s", (run_id,)).fetchone()[0] == 2
    events = connection.execute("SELECT payload FROM outbox_events WHERE event_id IS NOT NULL").fetchall()
    assert len(events) == 1
    assert events[0][0]["canonical_payload"]["stage1_projection"]["category"] == "A"
    assert events[0][0]["symbol"] == "BTCUSDT"


def test_duplicate_canonical_input_has_one_outbox_identity(connection):
    repo = Phase1Repository(connection)
    repo.upsert_instrument(_instrument("BTCUSDT"))
    run_id = repo.create_screening_run(NOW)
    result = _result("BTCUSDT", "A")

    first_id = _persist(connection, run_id, result)
    second_id = _persist(connection, run_id, result)
    connection.commit()

    assert first_id == second_id
    rows = connection.execute(
        "SELECT event_id, payload FROM outbox_events WHERE event_id IS NOT NULL"
    ).fetchall()
    assert len(rows) == 1
    assert len(rows[0][0]) == 64


def test_outbox_projection_is_independent_of_source_result_upsert_and_delete(connection):
    repo = Phase1Repository(connection)
    repo.upsert_instrument(_instrument("BTCUSDT"))
    run_id = repo.create_screening_run(NOW)
    result_id = _persist(connection, run_id, _result("BTCUSDT", "A"))
    connection.commit()
    original = connection.execute(
        "SELECT payload FROM outbox_events WHERE event_id IS NOT NULL"
    ).fetchone()[0]

    connection.execute(
        "UPDATE screening_results SET category = 'D', reason = 'mutated source' WHERE id = %s",
        (result_id,),
    )
    connection.commit()
    assert connection.execute(
        "SELECT payload FROM outbox_events WHERE event_id IS NOT NULL"
    ).fetchone()[0] == original

    connection.execute("DELETE FROM screening_results WHERE id = %s", (result_id,))
    connection.commit()
    assert connection.execute(
        "SELECT payload FROM outbox_events WHERE event_id IS NOT NULL"
    ).fetchone()[0] == original


def test_ab_out_of_scope_instrument_is_emitted_with_explicit_scope_rejection_evidence(connection):
    repo = Phase1Repository(connection)
    repo.upsert_instruments(
        [
            _instrument("DOGEUSDT", category="SPOT", contract_type="spot"),
            _instrument("LTCUSDT", status="offline"),
        ]
    )
    run_id = repo.create_screening_run(NOW)
    _persist(connection, run_id, _result("DOGEUSDT", "A"))
    _persist(connection, run_id, _result("LTCUSDT", "B"))
    connection.commit()

    rows = connection.execute(
        "SELECT payload FROM outbox_events WHERE event_id IS NOT NULL ORDER BY event_id"
    ).fetchall()
    assert len(rows) == 2
    by_symbol = {row[0]["symbol"]: row[0] for row in rows}
    assert by_symbol["DOGEUSDT"]["market"] == "UNSUPPORTED"
    assert by_symbol["DOGEUSDT"]["canonical_payload"]["instrument_scope"]["in_scope"] == "false"
    assert by_symbol["LTCUSDT"]["market"] == "USDT_PERPETUAL"
    assert by_symbol["LTCUSDT"]["canonical_payload"]["instrument_scope"]["in_scope"] == "false"


def test_oversized_event_fails_closed_and_transaction_rolls_back_without_truncation(connection):
    repo = Phase1Repository(connection)
    repo.upsert_instrument(_instrument("BTCUSDT"))
    run_id = repo.create_screening_run(NOW)
    connection.commit()
    result = _result("BTCUSDT", "A", detail="x" * (64 * 1024))

    with pytest.raises(ValueError, match="64 KiB"):
        with connection.transaction():
            _persist(connection, run_id, result)

    assert connection.execute(
        "SELECT count(*) FROM screening_results WHERE run_id = %s", (run_id,)
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT count(*) FROM outbox_events WHERE event_id IS NOT NULL"
    ).fetchone()[0] == 0


def test_policy_derived_stage1_ttl_is_persisted_to_outbox_without_direction_inference(connection):
    from quant_phase9.intake import (
        Stage1IntakeTTLPolicyV1,
        resolve_stage1_candidate_expiry,
    )

    repo = Phase1Repository(connection)
    repo.upsert_instrument(_instrument("BTCUSDT"))
    run_id = repo.create_screening_run(NOW)
    result = _result("BTCUSDT", "A")
    expiry = resolve_stage1_candidate_expiry(
        NOW, policy=Stage1IntakeTTLPolicyV1(stage1_candidate_ttl_seconds=300)
    )
    persist_stage1_candidate_with_event(
        connection,
        run_id=run_id,
        screening_result=result,
        candidate_created_at=NOW,
        candidate_valid_until=expiry,
        stage1_policy_version="phase1-basic-v1",
        source_as_of=NOW,
    )
    connection.commit()

    payload = connection.execute(
        "SELECT payload FROM outbox_events WHERE event_id IS NOT NULL"
    ).fetchone()[0]
    event = payload["canonical_payload"]
    assert event["candidate_valid_until"].startswith("2026-09-27T12:05:00")
    assert event["canonical_payload"]["stage1_projection"]["category"] == "A"
    assert "direction" not in event["canonical_payload"]["stage1_projection"]


def test_policy_derived_stage1_ttl_is_persisted_to_outbox_without_direction_inference(connection):
    from quant_phase9.intake import (
        Stage1IntakeTTLPolicyV1,
        resolve_stage1_candidate_expiry,
    )

    repo = Phase1Repository(connection)
    repo.upsert_instrument(_instrument("BTCUSDT"))
    run_id = repo.create_screening_run(NOW)
    result = _result("BTCUSDT", "A")
    expiry = resolve_stage1_candidate_expiry(
        NOW, policy=Stage1IntakeTTLPolicyV1(stage1_candidate_ttl_seconds=300)
    )
    persist_stage1_candidate_with_event(
        connection,
        run_id=run_id,
        screening_result=result,
        candidate_created_at=NOW,
        candidate_valid_until=expiry,
        stage1_policy_version="phase1-basic-v1",
        source_as_of=NOW,
    )
    connection.commit()

    payload = connection.execute(
        "SELECT payload FROM outbox_events WHERE event_id IS NOT NULL"
    ).fetchone()[0]
    projection = payload["canonical_payload"]["stage1_projection"]
    assert payload["candidate_valid_until"].startswith("2026-09-27T12:05:00")
    assert projection["category"] == "A"
    assert "direction" not in projection
