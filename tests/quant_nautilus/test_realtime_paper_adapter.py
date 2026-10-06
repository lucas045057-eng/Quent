from datetime import datetime, timezone
from decimal import Decimal as D
import os
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest
from nautilus_trader.model.data import QuoteTick
from nautilus_trader.model.objects import Quantity

from quant_execution.contracts import PositionSnapshotV1
from quant_execution.persistence import ExecutionStore, ReservationRejected
from quant_nautilus.acceptance import fixture_case, install_fixture_decision
from quant_phase1.contracts import DataStatus, Ticker
from quant_phase1.db import apply_migrations
from quant_nautilus.realtime_paper_adapter import NautilusLocalPaperExecutionAdapter


@pytest.fixture
def adapter_db(tmp_path):
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    assert dsn, "TEST_POSTGRES_DSN is required"
    schema = "paper_restart_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    with psycopg.connect(dsn, options=f"-c search_path={schema}") as connection:
        apply_migrations(connection)
        yield connection, dsn, schema
    with psycopg.connect(dsn, autocommit=True) as cleanup:
        cleanup.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def _real_quote(intent, *, status=DataStatus.AVAILABLE, processed_at=None):
    now = intent.quote_as_of
    bid = intent.reference_price - D("1") if intent.side == "LONG" else intent.reference_price - D("2")
    ask = intent.reference_price + D("1") if intent.side == "LONG" else intent.reference_price
    return Ticker(
        symbol=intent.core_symbol,
        last_price=(bid + ask) / 2,
        bid_price=bid,
        ask_price=ask,
        bid_size=D("2"),
        ask_size=D("2"),
        volume24h=D("1000"),
        turnover24h=D("50000000"),
        index_price=None,
        mark_price=None,
        exchange_timestamp=now,
        fetched_at=now,
        processed_at=processed_at or now,
        status=status,
        raw_payload={"source": "fixture_public_payload"},
        source="bitget_v3_rest",
        exchange="bitget",
    )


def _seed_adapter_state(connection, tmp_path, *, now):
    case = fixture_case(tmp_path / "case", mode="PAPER", now=now)
    install_fixture_decision(connection, case)
    store = ExecutionStore(connection)
    store.record_account(case.account)
    flat = PositionSnapshotV1(
        account_id=case.account.account_id,
        venue=case.account.venue,
        canonical_symbol=case.intent.canonical_symbol,
        mode="PAPER",
        side="FLAT",
        quantity=D("0"),
        quantity_unit="BASE",
        average_entry=None,
        equity=case.account.equity,
        available_balance=case.account.available_balance,
        margin=D("0"),
        mark_price=None,
        unrealized_pnl=None,
        realized_trade_pnl=D("0"),
        fees=D("0"),
        funding_cash=None,
        protection_status="NOT_REQUIRED",
        source_order_ids=(),
        source_fill_ids=(),
        reconciliation_status="RECONCILED",
        as_of=now,
        adapter_version="test-provisioned-flat-v1",
    )
    store.record_position(flat)
    connection.commit()
    return case, store, flat


def test_local_paper_uses_exact_canonical_quote_tick_not_midpoint_fixture(adapter_db, tmp_path):
    from quant_nautilus.paper import LocalPaper

    connection, _, schema = adapter_db
    now = datetime.now(timezone.utc)
    case, store, _ = _seed_adapter_state(connection, tmp_path, now=now)
    store.reserve(case.intent, case.risk_policy, now=now)
    runner = LocalPaper(connection, case.intent.intent_id)
    quote = _real_quote(case.intent)
    try:
        runner.command("QUOTE", {
            "at": quote.exchange_timestamp.isoformat(),
            "bid": str(quote.bid_price),
            "ask": str(quote.ask_price),
            "bid_size": str(quote.bid_size),
            "ask_size": str(quote.ask_size),
        })
        tick = runner.session.adapter._last_tick
        assert isinstance(tick, QuoteTick)
        assert tick.bid_price.as_decimal() == quote.bid_price
        assert tick.ask_price.as_decimal() == quote.ask_price
        assert tick.bid_size.as_decimal() == quote.bid_size
        assert tick.ask_size.as_decimal() == quote.ask_size
    finally:
        runner.close()


def test_adapter_runs_local_paper_once_from_real_public_quote_and_refuses_retry(adapter_db, tmp_path):
    connection, _, _ = adapter_db
    now = datetime.now(timezone.utc)
    case, store, prior_position = _seed_adapter_state(connection, tmp_path, now=now)
    quote = _real_quote(case.intent, processed_at=datetime.now(timezone.utc))
    adapter = NautilusLocalPaperExecutionAdapter(
        connection,
        case.intent,
        instrument=case.instrument,
        quote=quote,
        data_source="REAL_PUBLIC_DATA",
        quote_max_age_seconds=60,
    )

    adapter.preflight(case.intent, datetime.now(timezone.utc))
    assert adapter.reconcile() == (prior_position,)
    store.reserve(case.intent, case.risk_policy, now=now)
    adapter.submit(case.intent)

    results = tuple(adapter.events())
    position = adapter.position_snapshot(case.intent.canonical_symbol)
    assert results
    assert all(result.intent_id == case.intent.intent_id for result in results)
    assert position.reconciliation_status == "RECONCILED"
    assert position.mode == "PAPER"
    assert adapter.reconcile() == (position,)
    journal_quote = connection.execute(
        "SELECT payload FROM execution_local_events WHERE account_id=%s AND event_kind='QUOTE'",
        (case.intent.account_id,),
    ).fetchone()[0]
    assert journal_quote["bid"] == str(quote.bid_price)
    assert journal_quote["ask"] == str(quote.ask_price)
    assert journal_quote["bid_size"] == str(quote.bid_size)
    assert journal_quote["ask_size"] == str(quote.ask_size)
    with pytest.raises(ReservationRejected, match="QUERY_BEFORE_RETRY"):
        adapter.submit(case.intent)


def test_adapter_fails_closed_for_non_public_or_stale_quote(adapter_db, tmp_path):
    connection, _, _ = adapter_db
    now = datetime.now(timezone.utc)
    case, _, _ = _seed_adapter_state(connection, tmp_path, now=now)
    quote = _real_quote(case.intent)
    adapter = NautilusLocalPaperExecutionAdapter(
        connection, case.intent, instrument=case.instrument, quote=quote,
        data_source="FIXTURE_DRIVEN_ACCEPTANCE", quote_max_age_seconds=60,
    )
    with pytest.raises(ValueError, match="REAL_PUBLIC_DATA"):
        adapter.preflight(case.intent, now)

    stale = _real_quote(case.intent, processed_at=now.replace(year=now.year - 1))
    adapter = NautilusLocalPaperExecutionAdapter(
        connection, case.intent, instrument=case.instrument, quote=stale,
        data_source="REAL_PUBLIC_DATA", quote_max_age_seconds=60,
    )
    with pytest.raises(ValueError, match="STALE_MARKET_DATA"):
        adapter.preflight(case.intent, now)


def test_operating_preflight_restores_real_journal_without_new_market_or_order_commands(adapter_db,tmp_path):
    connection,_,_=adapter_db
    now=datetime.now(timezone.utc)
    case,store,_=_seed_adapter_state(connection,tmp_path,now=now)
    quote=_real_quote(case.intent,processed_at=datetime.now(timezone.utc))
    adapter=NautilusLocalPaperExecutionAdapter(connection,case.intent,instrument=case.instrument,
        quote=quote,data_source='REAL_PUBLIC_DATA',quote_max_age_seconds=5)
    store.reserve(case.intent,case.risk_policy,now=now)
    adapter.submit(case.intent)
    def commands():
        return connection.execute("SELECT event_kind,payload FROM execution_local_events WHERE account_id=%s AND event_kind<>'CHECKPOINT' ORDER BY seq",(case.intent.account_id,)).fetchall()
    before=commands()
    total=connection.execute('SELECT count(*) FROM execution_local_events').fetchone()[0]
    before_positions=adapter.reconcile()
    adapter.preflight_existing_account()
    adapter.preflight_existing_account()
    assert connection.execute('SELECT count(*) FROM execution_local_events').fetchone()[0]==total
    assert commands()==before
    assert adapter.reconcile()==before_positions
    assert adapter.reconcile()[0].reconciliation_status=='RECONCILED'


def _native_journal_over_4096(connection,tmp_path,*,fault=None):
    from psycopg.types.json import Jsonb
    from quant_phase9.canonical import canonical_sha256
    now=datetime.now(timezone.utc)
    case,store,_=_seed_adapter_state(connection,tmp_path,now=now)
    quote=_real_quote(case.intent,processed_at=datetime.now(timezone.utc))
    adapter=NautilusLocalPaperExecutionAdapter(connection,case.intent,instrument=case.instrument,
        quote=quote,data_source='REAL_PUBLIC_DATA',quote_max_age_seconds=60)
    store.reserve(case.intent,case.risk_policy,now=now)
    adapter.submit(case.intent)
    checkpoint=connection.execute("SELECT payload FROM execution_local_events WHERE event_kind='CHECKPOINT' ORDER BY seq DESC LIMIT 1").fetchone()[0]
    # Explicit isolated native fixture history: repeated real native state
    # checkpoints model restoration/management; never production Goal fills.
    import copy
    alternate=copy.deepcopy(checkpoint)
    if fault=='STATE':alternate['entry_order_count']+=1
    alternate_digest='0'*64 if fault=='DIGEST' else str(canonical_sha256(alternate))
    connection.execute("""INSERT INTO execution_local_events(account_id,event_key,event_kind,payload,content_digest,intent_id)
        SELECT %s,'fixture-checkpoint-'||n::text,'CHECKPOINT',
        CASE WHEN n=1024 THEN %s ELSE %s END,
        CASE WHEN n=1024 THEN %s ELSE %s END,%s FROM generate_series(1,4097) n""",
        (case.intent.account_id,Jsonb(alternate),Jsonb(checkpoint),alternate_digest,
         str(canonical_sha256(checkpoint)),case.intent.intent_id))
    connection.commit()
    return case,adapter,quote


def test_native_replay_and_reconcile_cross_4096_without_dropping_history(adapter_db,tmp_path):
    from quant_nautilus.paper import LocalPaper
    connection,_,_=adapter_db
    case,adapter,quote=_native_journal_over_4096(connection,tmp_path)
    before=connection.execute('SELECT count(*) FROM execution_local_events').fetchone()[0]
    results_before=connection.execute('SELECT count(*) FROM execution_results').fetchone()[0]
    position_before=adapter.reconcile()
    runner=LocalPaper(connection,case.intent.intent_id,recover_pending_on_start=False,verify_only=True)
    try:
        assert runner.event_count==before>4096
        assert runner.health()['state']=='RECONCILED'
    finally:runner.close()
    assert connection.execute('SELECT count(*) FROM execution_local_events').fetchone()[0]==before
    runner=LocalPaper(connection,case.intent.intent_id,recover_pending_on_start=False)
    try:
        runner.command('QUOTE',{'at':quote.exchange_timestamp.isoformat(),
            'processed_at':quote.processed_at.isoformat(),'bid':str(quote.bid_price),'ask':str(quote.ask_price),
            'bid_size':str(quote.bid_size),'ask_size':str(quote.ask_size)})
        assert runner.event_count>before
    finally:runner.close()
    assert connection.execute('SELECT count(*) FROM execution_results').fetchone()[0]==results_before
    assert adapter.reconcile()==position_before


def test_digest_corruption_after_first_page_is_not_skipped(adapter_db,tmp_path):
    from quant_nautilus.paper import LocalPaper
    connection,_,_=adapter_db
    case,adapter,_=_native_journal_over_4096(connection,tmp_path,fault='DIGEST')
    with pytest.raises(ReservationRejected,match='LOCAL_EVENT_DIGEST_MISMATCH'):
        LocalPaper(connection,case.intent.intent_id,recover_pending_on_start=False,verify_only=True)
    with pytest.raises(ReservationRejected,match='LOCAL_EVENT_DIGEST_MISMATCH'):
        adapter.reconcile()


def test_historic_checkpoint_state_mismatch_after_first_page_is_not_skipped(adapter_db,tmp_path):
    from quant_nautilus.paper import LocalPaper
    from psycopg.types.json import Jsonb
    from quant_phase9.canonical import canonical_sha256
    connection,_,_=adapter_db
    case,_,_=_native_journal_over_4096(connection,tmp_path,fault='STATE')
    with pytest.raises(ReservationRejected,match='LOCAL_RECONCILIATION_MISMATCH'):
        LocalPaper(connection,case.intent.intent_id,recover_pending_on_start=False,verify_only=True)
