from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from quant_phase1.contracts import DataStatus
from quant_phase1.db import apply_migrations
from quant_phase1.stage1 import Stage1Result
from quant_phase9.canonical import canonical_bytes, canonical_sha256
from quant_phase9.contracts import (
    EvaluationIdentityV1,
    EvidenceFreshnessV1,
    EvidenceQualityV1,
    PolicyDataStatusV1,
    SourcePhaseV1,
    SourceProjectionV1,
)
from quant_phase9.intake import admit_event, build_stage1_candidate_event, Phase9OutboxWriter
from quant_phase9.snapshot import (
    SnapshotIdentityConflictError,
    build_snapshot,
    persist_evaluation_snapshot,
)
from quant_phase9.sources import CorePhase9SourceReader


AS_OF = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
MANIFEST_VERSION = "1.0.0"
MANIFEST_DIGEST = "a" * 64
CODE_VERSION = "b" * 40


@pytest.fixture
def database():
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN is required for snapshot integration tests")
    info = conninfo_to_dict(dsn)
    assert info.get("host") in {"127.0.0.1", "localhost", "::1"}
    assert info.get("dbname") == "quant_phase9_test"
    schema = f"phase9_snapshot_test_{uuid4().hex}"
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
        inputs_used=("price",), indicators={"atr": 1}, structure="BULLISH",
        reason_codes=("STRUCTURE_ALIGNED",), timestamp=AS_OF,
    )
    return build_stage1_candidate_event(
        screening_result_id=1201, run_id=22, screening_result=result,
        market="USDT_PERPETUAL",
        instrument_scope={
            "category": "USDT-FUTURES", "quote_coin": "USDT", "contract_type": "perpetual",
            "status": "online", "in_scope": "true",
        },
        candidate_created_at=AS_OF - timedelta(minutes=1),
        candidate_valid_until=AS_OF + timedelta(minutes=30),
        stage1_policy_version="phase1-basic-v1", source_as_of=AS_OF,
    )


def _identity(candidate):
    return EvaluationIdentityV1(
        stage1_candidate_id=candidate.stage1_candidate_id,
        market=candidate.market,
        symbol=candidate.symbol,
        timeframe="15m",
        evaluation_window_start=AS_OF - timedelta(minutes=15),
        evaluation_window_end=AS_OF,
        policy_generation=f"{MANIFEST_VERSION}:{MANIFEST_DIGEST}",
        material_change_generation="0",
    )


def _seed_evaluation(conn, candidate, identity):
    Phase9OutboxWriter().emit(conn, event=candidate)
    assert admit_event(conn, event=candidate, identity=identity, now=AS_OF).value == "ADMITTED"
    conn.commit()


def _projection(*, event_time=AS_OF, available_at=AS_OF) -> SourceProjectionV1:
    payload = {"schema": "PHASE9_SOURCE_PROJECTION_V1", "price": "100"}
    return SourceProjectionV1(
        projection_id=uuid4(),
        evaluation_id=uuid4(),
        source_phase=SourcePhaseV1.PHASE1,
        source_type="PRICE_TICKER",
        source_ref="phase1:fixture:btc",
        symbol="BTCUSDT",
        market="USDT_PERPETUAL",
        event_time=event_time,
        observed_at=event_time,
        captured_at=AS_OF,
        processed_at=AS_OF,
        available_at=available_at,
        availability_status=PolicyDataStatusV1.AVAILABLE,
        freshness_status=EvidenceFreshnessV1.FRESH,
        quality_status=EvidenceQualityV1.VALID,
        coverage_status=None,
        canonical_payload=payload,
        source_schema_version="PHASE9_SOURCE_PROJECTION_V1",
        projection_version="1",
        canonical_digest=canonical_sha256(payload),
    )


class _Reader:
    def __init__(self, projection):
        self.projection = projection
        self.conn = None

    def read(self, conn, *, candidate, timeframe, as_of):
        self.conn = conn
        assert candidate.symbol == "BTCUSDT"
        assert timeframe == "15m"
        assert as_of == AS_OF
        return (self.projection,)


def _manifest():
    return SimpleNamespace(manifest_version=MANIFEST_VERSION, manifest_digest=MANIFEST_DIGEST)


def _begin_snapshot(conn):
    conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")


def _float_paths(value, path="snapshot"):
    from collections.abc import Mapping

    if isinstance(value, float):
        return [path]
    if isinstance(value, Mapping):
        return [found for key, item in value.items()
                for found in _float_paths(item, f"{path}.{key}")]
    if isinstance(value, (tuple, list)):
        return [found for index, item in enumerate(value)
                for found in _float_paths(item, f"{path}[{index}]")]
    return []


def test_build_snapshot_uses_same_connection_binds_context_and_excludes_future_data(database):
    conn = database
    candidate = _candidate()
    identity = _identity(candidate)
    _seed_evaluation(conn, candidate, identity)
    reader = _Reader(_projection())
    _begin_snapshot(conn)

    snapshot = build_snapshot(
        conn,
        identity=identity,
        stage1_candidate=candidate,
        as_of=AS_OF,
        source_reader=reader,
        policy_manifest=_manifest(),
        code_version=CODE_VERSION,
    )

    assert reader.conn is conn
    assert snapshot.evaluation_id == snapshot.source_projections[0].evaluation_id
    assert snapshot.source_projections[0].event_time == AS_OF
    assert snapshot.identity.policy_generation == f"{MANIFEST_VERSION}:{MANIFEST_DIGEST}"
    assert snapshot.created_at == AS_OF
    conn.rollback()

    future_reader = _Reader(_projection(available_at=AS_OF + timedelta(seconds=1)))
    _begin_snapshot(conn)
    with pytest.raises(ValueError, match="later than as_of"):
        build_snapshot(
            conn,
            identity=identity,
            stage1_candidate=candidate,
            as_of=AS_OF,
            source_reader=future_reader,
            policy_manifest=_manifest(),
            code_version=CODE_VERSION,
        )
    conn.rollback()


def test_snapshot_insert_is_idempotent_and_conflicts_without_update(database):
    conn = database
    candidate = _candidate()
    identity = _identity(candidate)
    _seed_evaluation(conn, candidate, identity)
    reader = _Reader(_projection())
    _begin_snapshot(conn)
    first = build_snapshot(
        conn, identity=identity, stage1_candidate=candidate, as_of=AS_OF,
        source_reader=reader, policy_manifest=_manifest(), code_version=CODE_VERSION,
    )
    persist_evaluation_snapshot(conn, snapshot=first)
    persist_evaluation_snapshot(conn, snapshot=first)
    conn.commit()
    stored = conn.execute(
        "SELECT snapshot_digest,payload FROM phase9_evaluation_snapshots WHERE evaluation_id=%s",
        (first.evaluation_id,),
    ).fetchone()
    conn.commit()

    _begin_snapshot(conn)
    second = build_snapshot(
        conn, identity=identity, stage1_candidate=candidate, as_of=AS_OF,
        source_reader=reader, policy_manifest=_manifest(), code_version="c" * 40,
    )
    assert second.snapshot_digest != first.snapshot_digest
    with pytest.raises(SnapshotIdentityConflictError):
        persist_evaluation_snapshot(conn, snapshot=second)
    conn.rollback()

    assert conn.execute(
        "SELECT snapshot_digest,payload FROM phase9_evaluation_snapshots WHERE evaluation_id=%s",
        (first.evaluation_id,),
    ).fetchone() == stored
    assert conn.execute(
        "SELECT count(*) FROM phase9_evaluation_snapshots WHERE evaluation_id=%s",
        (first.evaluation_id,),
    ).fetchone()[0] == 1


def test_snapshot_persistence_requires_repeatable_read_and_rolls_back_on_error(database):
    conn = database
    candidate = _candidate()
    identity = _identity(candidate)
    _seed_evaluation(conn, candidate, identity)
    _begin_snapshot(conn)
    snapshot = build_snapshot(
        conn, identity=identity, stage1_candidate=candidate, as_of=AS_OF,
        source_reader=_Reader(_projection()), policy_manifest=_manifest(), code_version=CODE_VERSION,
    )
    conn.rollback()

    conn.execute("SELECT 1").fetchone()
    with pytest.raises(ValueError, match="REPEATABLE READ"):
        persist_evaluation_snapshot(conn, snapshot=snapshot)

    assert conn.info.transaction_status.name == "IDLE"
    assert conn.execute(
        "SELECT count(*) FROM phase9_evaluation_snapshots WHERE evaluation_id=%s",
        (snapshot.evaluation_id,),
    ).fetchone()[0] == 0


def test_bitget_perpetual_normalization_reaches_phase9_snapshot_and_durable_candidate(database, tmp_path):
    import json

    from quant_instruments import resolve_core_instrument
    from quant_phase1.adapters.bitget_v3.parsers import parse_instruments_response
    from quant_phase1.repositories import Phase1Repository
    from quant_phase9.contracts import (
        EvidenceChainV1, PatternMatchStatusV1, PatternMatchV1, PolicyDirectionV1,
    )
    from quant_phase9.decision import build_decision_candidate, input_snapshot_hash_for
    from quant_phase9.intake import claim_pending
    from quant_phase9.persistence import persist_final
    from quant_phase9.policy import PolicyContentV1, load_approved_policy_manifest
    from quant_phase9.sources.paper_v1 import PaperV1SourceReader

    item = {
        "symbol": "BTCUSDT",
        "category": "USDT-FUTURES",
        "baseCoin": "BTC",
        "quoteCoin": "USDT",
        "symbolType": "crypto",
        "type": "perpetual",
        "status": "online",
        "pricePrecision": "1",
        "quantityPrecision": "3",
        "minOrderQty": "0.001",
    }
    instrument = parse_instruments_response(
        {"code": "00000", "data": [item]}, fetched_at=AS_OF,
    )[0]
    assert instrument.symbol_type == "PERPETUAL"
    Phase1Repository(database).upsert_instrument(instrument)
    eth_item = {**item, "symbol": "ETHUSDT", "baseCoin": "ETH"}
    eth_instrument = parse_instruments_response(
        {"code": "00000", "data": [eth_item]}, fetched_at=AS_OF,
    )[0]
    Phase1Repository(database).upsert_instrument(eth_instrument)
    quality = json.dumps({
        "source_status": "AVAILABLE",
        "runtime_age": {"as_seconds": 1.25, "series": [2.5, {"sample": 3.75}]},
    })
    for benchmark in ("BTCUSDT", "ETHUSDT"):
        for interval in ("1H", "4H"):
            database.execute("""INSERT INTO phase5_market_leader_context (
                symbol,timeframe,context_timestamp,trend_state,structure_state,volatility_state,
                volume_state,freshness_status,data_quality,source_count,missing_count,status,
                calculation_version,input_reference,processed_at
            ) VALUES (%s,%s,%s,'TREND_UP','HIGHER_HIGH_HIGHER_LOW','NORMAL','NORMAL',
                'AVAILABLE',%s::jsonb,1,0,'AVAILABLE','phase5-v1','{}'::jsonb,%s)""",
                (benchmark, interval, AS_OF - timedelta(seconds=15), quality,
                 AS_OF - timedelta(seconds=10)))
    database.execute("""INSERT INTO phase5_market_regime_snapshots (
        timeframe,context_timestamp,direction_regime,volatility_regime,breadth_regime,
        direction_status,volatility_status,breadth_status,status,calculation_version,processed_at
    ) VALUES ('15m',%s,'TREND_UP','NORMAL','BROAD_STRENGTH','AVAILABLE','AVAILABLE',
        'AVAILABLE','AVAILABLE','phase5-v1',%s)""",
        (AS_OF - timedelta(seconds=15), AS_OF - timedelta(seconds=10)))
    database.commit()

    policy_content = {name: [] for name in PolicyContentV1.model_fields}
    digest = str(canonical_sha256(policy_content))
    manifest_path = tmp_path / "manifest.json"
    approval_path = tmp_path / "approval.json"
    manifest_path.write_text(json.dumps({
        "schema": "PHASE9_POLICY_MANIFEST_V1",
        "manifest_version": MANIFEST_VERSION,
        "created_at": "2026-09-27T11:59:00Z",
        "policy_content": policy_content,
        "manifest_digest": digest,
    }), encoding="utf-8")
    approval_path.write_text(json.dumps({
        "schema": "PHASE9_POLICY_APPROVAL_V1",
        "manifest_version": MANIFEST_VERSION,
        "manifest_digest": digest,
        "approval_status": "APPROVED",
        "approved_at": "2026-09-27T11:59:30Z",
        "approved_by": "fixture-human",
        "approved_commit": CODE_VERSION,
    }), encoding="utf-8")
    policy = load_approved_policy_manifest(
        manifest_path, approval_path, expected_commit=CODE_VERSION,
    )

    candidate = _candidate()
    identity = EvaluationIdentityV1(
        stage1_candidate_id=candidate.stage1_candidate_id,
        market=candidate.market,
        symbol=candidate.symbol,
        timeframe="15m",
        evaluation_window_start=AS_OF - timedelta(minutes=15),
        evaluation_window_end=AS_OF,
        policy_generation=f"{policy.manifest_version}:{policy.manifest_digest}",
        material_change_generation="0",
    )
    _seed_evaluation(database, candidate, identity)

    resolved = resolve_core_instrument(database, "BTCUSDT")
    assert resolved.canonical_symbol == "BTC-USDT-PERP"
    database.commit()
    _begin_snapshot(database)
    snapshot = build_snapshot(
        database,
        identity=identity,
        stage1_candidate=candidate,
        as_of=AS_OF,
        source_reader=PaperV1SourceReader(),
        policy_manifest=policy,
        code_version=CODE_VERSION,
    )
    repeated_snapshot = build_snapshot(
        database,
        identity=identity,
        stage1_candidate=candidate,
        as_of=AS_OF,
        source_reader=PaperV1SourceReader(),
        policy_manifest=policy,
        code_version=CODE_VERSION,
    )
    market_context = next(
        projection for projection in snapshot.source_projections
        if projection.source_type == "PAPER_MARKET_CONTEXT"
    )
    nested_age = market_context.canonical_payload["sources"][0]["data_quality"]["runtime_age"]
    assert nested_age["as_seconds"] == Decimal("1.25")
    assert nested_age["series"][1]["sample"] == Decimal("3.75")
    assert _float_paths(market_context.canonical_payload) == []
    assert repeated_snapshot.snapshot_digest == snapshot.snapshot_digest
    assert canonical_bytes(repeated_snapshot) == canonical_bytes(snapshot)
    persist_evaluation_snapshot(database, snapshot=snapshot)
    database.commit()

    assert snapshot.symbol == "BTCUSDT"
    assert database.execute(
        "SELECT symbol_type FROM symbols WHERE symbol='BTCUSDT'"
    ).fetchone() == ("PERPETUAL",)
    assert database.execute(
        "SELECT snapshot_digest FROM phase9_evaluation_snapshots WHERE evaluation_id=%s",
        (snapshot.evaluation_id,),
    ).fetchone() == (snapshot.snapshot_digest,)

    match = PatternMatchV1(
        pattern_match_id=uuid4(),
        evaluation_id=snapshot.evaluation_id,
        pattern_type="TREND_CONTINUATION",
        direction=PolicyDirectionV1.LONG,
        status=PatternMatchStatusV1.NOT_CONFIGURED,
        required_evidence_ids=(),
        supporting_evidence_ids=(),
        conflicting_evidence_ids=(),
        missing=(),
        vetoes=(),
        pattern_policy_version=snapshot.pattern_policy_version,
    )
    input_hash = input_snapshot_hash_for(snapshot_hash=snapshot.snapshot_digest, jev_review=None)
    chain = EvidenceChainV1(
        stage1_candidate_id=snapshot.stage1_candidate_id,
        symbol=snapshot.symbol,
        evaluation_id=snapshot.evaluation_id,
        evaluation_time=snapshot.evaluation_time,
        timeframe=snapshot.timeframe,
        supporting=(),
        conflicting=(),
        neutral=(),
        missing=(),
        degraded=(),
        hard_vetoes=(),
        matched_patterns=(),
        jev_review_required=False,
        evidence_schema_version=snapshot.evidence_schema_version,
        evaluation_snapshot_hash=snapshot.snapshot_digest,
        input_snapshot_hash=input_hash,
    )
    decision, lifecycle = build_decision_candidate(
        snapshot=snapshot,
        evidence_chain=chain,
        pattern_matches=(match,),
        jev_review=None,
        policy_manifest=policy,
        now=AS_OF,
    )
    assert decision.eligible is False
    claimed = claim_pending(
        database,
        consumer_name="identity-test",
        limit=1,
        now=AS_OF,
        lease_until=AS_OF + timedelta(minutes=5),
    )
    assert len(claimed) == 1
    assert claimed[0].event_id == candidate.event_id
    database.commit()

    persist_final(
        database,
        evaluation_id=snapshot.evaluation_id,
        snapshot_digest=snapshot.snapshot_digest,
        evidence_items=(),
        evidence_chain=chain,
        pattern_matches=(match,),
        jev_review=None,
        decision=decision,
        lifecycle_event=lifecycle,
        event_id=candidate.event_id,
        consumer_name="identity-test",
        now=AS_OF,
    )
    database.commit()

    assert database.execute(
        "SELECT decision_id FROM phase9_decision_candidates WHERE decision_id=%s",
        (decision.decision_id,),
    ).fetchone() == (decision.decision_id,)


def test_core_reader_composes_phase1_through_phase8_in_fixed_order(database):
    database.execute("""INSERT INTO symbols (symbol,category,base_coin,quote_coin,symbol_type,contract_type,status,price_precision,quantity_precision,min_order_qty,source,exchange,fetched_at) VALUES ('BTCUSDT','USDT-FUTURES','BTC','USDT','PERPETUAL','perpetual','online',2,3,0.001,'fixture','bitget',%s)""", (AS_OF,))
    database.commit()
    projections = CorePhase9SourceReader().read(
        database, candidate=_candidate(), timeframe="15m", as_of=AS_OF
    )
    phases = [int(item.source_phase.value.removeprefix("PHASE")) for item in projections]
    assert phases == sorted(phases)
    assert set(phases) <= set(range(1, 9))
    registry = next(item for item in projections if item.source_type == "SOURCE_REGISTRY")
    assert registry.availability_status is PolicyDataStatusV1.NOT_CONFIGURED
